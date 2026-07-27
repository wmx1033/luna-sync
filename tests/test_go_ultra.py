"""GO Ultra driver tests.

The fake camera replays the framing and field layout observed on a real device
(firmware v1.6.25), so these cover the protocol as it actually behaves rather
than as it is described.
"""

import os
import struct
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from camera_driver import DownloadTarget, DriverProtocolError, DriverUnreachableError, RemoteMedia
from driver_registry import available_drivers, create_driver, device_endpoint
from drivers import go_ultra
from drivers.go_ultra import GoUltraDriver, capture_moment, storage_of
from drivers.insta360_protocol import (
    SYNC_MAGIC,
    TYPE_MESSAGE,
    TYPE_SYNC,
    Insta360Session,
    ProtocolError,
    command_payload,
    encode_varint,
    frame,
    parse_fields,
    parse_packet,
    repeated_varint_field,
    varint_field,
)

SAMPLE_PATHS = [
    '/DCIM/Camera01/LRV_20251116_044541_001.lrv',
    '/DCIM/Camera01/VID_20251116_044541_001.mp4',
    '/DCIM/Camera01/LIV_20260721_203652_022.jpg',
]


def string_field(number, text):
    raw = text.encode()
    return encode_varint(number << 3 | 2) + encode_varint(len(raw)) + raw


def file_list_body(paths, total=None):
    body = b''.join(string_field(1, path) for path in paths)
    return body + varint_field(2, total if total is not None else len(paths))


def options_body(by_field):
    """Wrap option strings in the outer field-2 Options message, as the camera does."""
    inner = b''.join(string_field(number, text) for number, text in sorted(by_field.items()))
    return encode_varint(2 << 3 | 2) + encode_varint(len(inner)) + inner


class FakeCamera(threading.Thread):
    """A socket server that speaks just enough of the protocol to drive tests."""

    def __init__(self, paths=SAMPLE_PATHS, echo_sync=True, options=None, fail_code=None):
        super().__init__(daemon=True)
        self.paths = list(paths)
        self.echo_sync = echo_sync
        self.options = options or {15: 'IBEEA2509WHRC7', 16: 'insta360-uuid',
                                   30: 'v1.6.25', 48: 'Insta360 GO Ultra'}
        self.fail_code = fail_code
        self.requests = []
        self.error = None
        self.server = __import__('socket').socket()
        self.server.setsockopt(__import__('socket').SOL_SOCKET,
                               __import__('socket').SO_REUSEADDR, 1)
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]

    def run(self):
        try:
            conn, _ = self.server.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            try:
                while True:
                    header = conn.recv(4)
                    if len(header) < 4:
                        return
                    total = struct.unpack('<I', header)[0]
                    payload = b''
                    while len(payload) < total - 4:
                        piece = conn.recv(total - 4 - len(payload))
                        if not piece:
                            return
                        payload += piece
                    self.handle(conn, payload)
            except Exception as exc:  # noqa: BLE001  a helper bug must surface, not hang
                self.error = exc
                return

    def handle(self, conn, payload):
        kind = payload[0]
        if kind == TYPE_SYNC and self.echo_sync:
            conn.sendall(frame(payload))
            return
        if kind != TYPE_MESSAGE:
            return
        code = struct.unpack('<H', payload[3:5])[0]
        sequence = int.from_bytes(payload[6:9], 'little')
        self.requests.append({'code': code, 'sequence': sequence,
                              'fields': parse_fields(payload[12:]) if payload[12:] else []})
        if self.fail_code is not None:
            conn.sendall(frame(command_payload(self.fail_code, sequence, b'')))
            return
        if code == go_ultra.CMD_GET_FILE_LIST:
            start = self._request_value(payload[12:], 2) or 0
            count = self._request_value(payload[12:], 3) or len(self.paths)
            page = self.paths[start:start + count]
            body = file_list_body(page, total=len(self.paths))
        elif code == go_ultra.CMD_GET_OPTIONS:
            wanted = [value for number, _wire, value in parse_fields(payload[12:])
                      if number == 1]
            body = options_body({n: text for n, text in self.options.items() if n in wanted})
        else:
            body = b''
        conn.sendall(frame(command_payload(200, sequence, body)))

    @staticmethod
    def _request_value(body, number):
        for num, _wire, value in parse_fields(body):
            if num == number:
                return value
        return None

    def stop(self):
        try:
            self.server.close()
        except OSError:
            pass


class ProtocolTests(unittest.TestCase):
    def test_framing_length_includes_the_prefix(self):
        payload = bytes([0x06, 0, 0]) + SYNC_MAGIC
        self.assertEqual(frame(payload).hex(), '1100000006000073794e63654e64696e53')

    def test_command_header_layout(self):
        payload = command_payload(13, 1, b'')
        self.assertEqual(payload.hex(), '0400000d0002010000800000')
        kind, response = parse_packet(payload)
        self.assertEqual(kind, TYPE_MESSAGE)
        self.assertEqual((response.code, response.sequence), (13, 1))

    def test_varint_round_trip(self):
        body = varint_field(1, 2) + varint_field(2, 0) + varint_field(3, 200)
        self.assertEqual(parse_fields(body), [(1, 0, 2), (2, 0, 0), (3, 0, 200)])

    def test_repeated_field_encoding(self):
        body = repeated_varint_field(1, (15, 16))
        self.assertEqual(parse_fields(body), [(1, 0, 15), (1, 0, 16)])

    def test_a_truncated_field_is_rejected(self):
        with self.assertRaises(ProtocolError):
            parse_fields(encode_varint(1 << 3 | 2) + encode_varint(50) + b'short')


class SessionTests(unittest.TestCase):
    def camera(self, **kwargs):
        camera = FakeCamera(**kwargs)
        camera.start()
        self.addCleanup(camera.stop)
        return camera

    def test_handshake_and_command_round_trip(self):
        camera = self.camera()
        session = Insta360Session('127.0.0.1', port=camera.port, timeout=5)
        session.open()
        self.addCleanup(session.close)
        response = session.command(go_ultra.CMD_GET_FILE_LIST,
                                   varint_field(1, 2) + varint_field(3, 10))
        self.assertTrue(response.ok)
        self.assertEqual(camera.requests[0]['code'], go_ultra.CMD_GET_FILE_LIST)

    def test_a_silent_camera_reads_as_a_connection_problem(self):
        # An asleep camera still accepts TCP but never answers; that should
        # surface as "unreachable", not as a protocol mismatch.
        camera = self.camera(echo_sync=False)
        session = Insta360Session('127.0.0.1', port=camera.port, timeout=1)
        with self.assertRaises(ConnectionError):
            session.open()

    def test_a_sleeping_camera_is_reported_as_unreachable_by_the_driver(self):
        camera = self.camera(echo_sync=False)
        driver = GoUltraDriver('127.0.0.1')
        driver._session = Insta360Session('127.0.0.1', port=camera.port, timeout=1)
        self.addCleanup(driver.close)
        with self.assertRaises(DriverUnreachableError):
            driver.connect()

    def test_sequence_numbers_increase_and_are_matched(self):
        camera = self.camera()
        session = Insta360Session('127.0.0.1', port=camera.port, timeout=5)
        session.open()
        self.addCleanup(session.close)
        session.command(go_ultra.CMD_GET_FILE_LIST, varint_field(1, 2))
        session.command(go_ultra.CMD_GET_FILE_LIST, varint_field(1, 2))
        self.assertEqual([r['sequence'] for r in camera.requests], [1, 2])


class DriverTests(unittest.TestCase):
    def camera(self, **kwargs):
        camera = FakeCamera(**kwargs)
        camera.start()
        self.addCleanup(camera.stop)
        return camera

    def driver(self, camera, sizes=None):
        driver = GoUltraDriver('127.0.0.1')
        driver._session = Insta360Session('127.0.0.1', port=camera.port, timeout=5)
        self.addCleanup(driver.close)
        patcher = patch.object(go_ultra, 'probe_total',
                               lambda url, extra: (sizes or {}).get(url.rsplit('/', 1)[-1]))
        patcher.start()
        self.addCleanup(patcher.stop)
        return driver

    def test_listing_maps_paths_to_media(self):
        camera = self.camera()
        driver = self.driver(camera, sizes={'VID_20251116_044541_001.mp4': 94275199})
        driver.connect()
        media = driver.list_media()

        self.assertEqual(len(media), 3)
        video = next(item for item in media if item.name.endswith('.mp4'))
        self.assertEqual(video.id, 'camera01/VID_20251116_044541_001.mp4')
        self.assertEqual(video.remote_path, '/DCIM/Camera01/VID_20251116_044541_001.mp4')
        self.assertEqual(video.url,
                         'http://127.0.0.1/DCIM/Camera01/VID_20251116_044541_001.mp4')
        self.assertEqual((video.date, video.time), ('2025-11-16', '04:45:41'))
        self.assertEqual(video.storage, 'camera01')
        self.assertEqual(video.bytes, 94275199)
        self.assertTrue(video.bytes_exact)

    def test_sidecars_and_photos_are_classified(self):
        camera = self.camera()
        driver = self.driver(camera)
        driver.connect()
        kinds = {item.name: item.kind for item in driver.list_media()}
        self.assertEqual(kinds['LRV_20251116_044541_001.lrv'], 'LRV')
        self.assertEqual(kinds['LIV_20260721_203652_022.jpg'], 'JPG')

    def test_listing_pages_through_a_large_library(self):
        paths = ['/DCIM/Camera01/VID_20260101_00%04d_%03d.mp4' % (index, index)
                 for index in range(450)]
        camera = self.camera(paths=paths)
        driver = self.driver(camera)
        driver.connect()
        media = driver.list_media()
        self.assertEqual(len(media), 450)
        self.assertEqual(len({item.id for item in media}), 450)
        # 200 per page means three requests, and no page is fetched twice.
        starts = [next((v for n, _w, v in request['fields'] if n == 2), None)
                  for request in camera.requests]
        self.assertEqual(starts, [0, 200, 400])

    def test_identity_never_asks_for_the_wifi_password(self):
        camera = self.camera(options={15: 'IBEEA2509WHRC7', 16: 'insta360-uuid',
                                      30: 'v1.6.25', 36: 'super-secret-ap-password',
                                      48: 'Insta360 GO Ultra'})
        driver = self.driver(camera)
        driver.connect()
        details = driver.identity()

        self.assertEqual(details['model'], 'Insta360 GO Ultra')
        self.assertEqual(details['firmware'], 'v1.6.25')
        self.assertEqual(details['serial'], 'IBEEA2509WHRC7')
        requested = [value for request in camera.requests
                     for number, _wire, value in request['fields'] if number == 1]
        self.assertNotIn(36, requested)
        self.assertNotIn('super-secret-ap-password', repr(details))

    def test_probe_reports_identity_and_media_count(self):
        camera = self.camera()
        driver = self.driver(camera)
        result = driver.probe()
        self.assertTrue(result.reachable)
        self.assertEqual(result.driver, 'go_ultra')
        self.assertEqual(result.model, 'Insta360 GO Ultra')
        self.assertEqual(result.identifier, 'IBEEA2509WHRC7')
        self.assertEqual(result.media_count, 3)
        self.assertTrue(result.as_dict()['capabilities']['supports_range'])

    def test_download_target_is_plain_http_and_resumable(self):
        driver = GoUltraDriver('192.168.42.1')
        media = RemoteMedia(id='camera01/VID.mp4', name='VID.mp4',
                            url='http://192.168.42.1/DCIM/Camera01/VID.mp4')
        target = driver.open_download(media, offset=2048)
        self.assertIsInstance(target, DownloadTarget)
        self.assertTrue(target.supports_range)
        self.assertEqual(target.request_headers()['Range'], 'bytes=2048-')

    def test_an_error_response_becomes_a_driver_error(self):
        camera = self.camera(fail_code=500)
        driver = self.driver(camera)
        driver.connect()
        with self.assertRaises(DriverProtocolError):
            driver.list_media()

    def test_an_unreachable_camera_is_reported(self):
        driver = GoUltraDriver('127.0.0.1')
        driver._session = Insta360Session('127.0.0.1', port=1, timeout=1)
        with self.assertRaises(DriverUnreachableError):
            driver.connect()


class HelperTests(unittest.TestCase):
    def test_capture_moment_from_file_names(self):
        self.assertEqual(capture_moment('VID_20251116_044541_001.mp4'),
                         ('2025-11-16', '04:45:41'))
        self.assertEqual(capture_moment('LIV_20260721_203652_022.jpg'),
                         ('2026-07-21', '20:36:52'))
        self.assertEqual(capture_moment('random.mp4'), ('', ''))

    def test_storage_comes_from_the_camera_path(self):
        self.assertEqual(storage_of('/DCIM/Camera01/VID.mp4'), 'camera01')
        self.assertEqual(storage_of('/DCIM/Camera02/VID.mp4'), 'camera02')
        self.assertEqual(storage_of('VID.mp4'), 'internal')

    def test_the_registry_offers_the_driver(self):
        self.assertIn('go_ultra', available_drivers())
        self.assertIsInstance(create_driver('go_ultra', '192.168.42.1'), GoUltraDriver)
        self.assertEqual(device_endpoint({'driver': 'go_ultra',
                                          'camera_host': '192.168.42.1'}),
                         ('192.168.42.1', 6666))


if __name__ == '__main__':
    unittest.main()
