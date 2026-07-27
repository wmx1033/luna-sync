import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import downloader
import luna_client
from camera_driver import (
    DownloadTarget,
    DriverUnreachableError,
    ProbeResult,
    RemoteMedia,
    file_kind,
)
from driver_registry import (
    available_drivers,
    create_driver,
    create_driver_for_device,
    device_endpoint,
    driver_catalog,
)
from drivers import luna_ultra


INDEX_HTML = '''<html><body>
<a href="../">../</a>                    10-Jul-2026 10:00       -
<a href="sub/">sub/</a>                  10-Jul-2026 10:05       -
<a href="VID_0001.mp4">VID_0001.mp4</a>  10-Jul-2026 10:19       12M
<a href="LRV_0001.lrv">LRV_0001.lrv</a>  10-Jul-2026 10:19       800K
</body></html>'''


class FakeLunaClient:
    """Stands in for the reverse-engineered transport during driver tests."""

    fail_connect = False
    fail_list = False

    def __init__(self, host):
        self.host = host
        self.connected = False
        self.closed = False

    def connect(self):
        if self.fail_connect:
            raise OSError('connection refused')
        self.connected = True

    def close(self):
        self.closed = True

    def list_files(self):
        if self.fail_list:
            raise OSError('no route to host')
        return [{
            'id': 'internal/VID_0001.mp4',
            'name': 'VID_0001.mp4',
            'href': 'VID_0001.mp4',
            'url': 'http://camera/VID_0001.mp4',
            'storage': 'internal',
            'storage_label': '内置存储',
            'bytes': 123,
            'bytes_exact': True,
            'kind': 'MP4',
        }]


class LunaIndexTests(unittest.TestCase):
    def test_index_parsing_keeps_files_and_drops_directory_entries(self):
        items = luna_client.parse_luna_index(
            INDEX_HTML, 'http://camera/storage_internal/', 'internal', '内置存储')
        self.assertEqual([item['name'] for item in items], ['VID_0001.mp4', 'LRV_0001.lrv'])
        self.assertEqual(items[0]['url'], 'http://camera/storage_internal/VID_0001.mp4')
        self.assertEqual(items[0]['bytes'], 12 * 1024 ** 2)
        self.assertEqual(items[1]['kind'], 'LRV')

    def test_stable_id_combines_device_storage_and_remote_path(self):
        items = luna_client.parse_luna_index(
            INDEX_HTML, 'http://camera/storage_external/', 'external', '存储卡')
        media = RemoteMedia.from_mapping(items[0])
        self.assertEqual(media.id, 'external/VID_0001.mp4')
        self.assertEqual(media.remote_path, 'VID_0001.mp4')
        self.assertEqual(media.stable_id('go-ultra'), 'go-ultra/external/VID_0001.mp4')

    def test_same_name_on_another_storage_is_a_different_item(self):
        internal = luna_client.parse_luna_index(INDEX_HTML, 'http://camera/i/', 'internal', '')
        external = luna_client.parse_luna_index(INDEX_HTML, 'http://camera/e/', 'external', '')
        self.assertNotEqual(internal[0]['id'], external[0]['id'])


class FakeSocket:
    def __init__(self, fail_after=None):
        self.sent = []
        self.closed = False
        self.fail_after = fail_after

    def settimeout(self, _):
        pass

    def sendall(self, payload):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise OSError('broken pipe')
        self.sent.append(payload)

    def recv(self, _):
        return b''

    def close(self):
        self.closed = True


class LunaAuthSessionTests(unittest.TestCase):
    def open_session(self, sockets):
        created = list(sockets)
        session = luna_client.LunaAuthSession('192.168.42.1')
        factory = lambda *args, **kwargs: created.pop(0)
        return session, patch.object(luna_client.socket, 'create_connection', factory)

    def test_handshake_sends_every_auth_payload(self):
        sock = FakeSocket()
        session, patched = self.open_session([sock])
        with patched:
            session.open()
        self.assertEqual(sock.sent, list(luna_client.AUTH_PAYLOADS))

    def test_refresh_reuses_the_open_socket(self):
        sock = FakeSocket()
        session, patched = self.open_session([sock])
        with patched:
            session.open()
            session.refresh()
        self.assertEqual(len(sock.sent), len(luna_client.AUTH_PAYLOADS) * 2)

    def test_refresh_reconnects_after_a_dropped_socket(self):
        dead = FakeSocket(fail_after=len(luna_client.AUTH_PAYLOADS))
        fresh = FakeSocket()
        session, patched = self.open_session([dead, fresh])
        with patched:
            session.open()
            session.refresh()
        self.assertTrue(dead.closed)
        self.assertEqual(fresh.sent, list(luna_client.AUTH_PAYLOADS))


class DriverTests(unittest.TestCase):
    def driver(self, **client_flags):
        for key, value in client_flags.items():
            setattr(FakeLunaClient, key, value)
        return create_driver('luna_ultra', '192.168.42.1')

    def setUp(self):
        FakeLunaClient.fail_connect = False
        FakeLunaClient.fail_list = False
        self.patcher = patch.object(luna_ultra, 'LunaClient', FakeLunaClient)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_probe_reports_identity_storages_and_media_count(self):
        result = self.driver().probe()
        self.assertIsInstance(result, ProbeResult)
        self.assertTrue(result.reachable)
        self.assertEqual(result.driver, 'luna_ultra')
        self.assertEqual(result.media_count, 1)
        self.assertEqual([s.id for s in result.storages], ['internal', 'external'])
        self.assertTrue(result.as_dict()['capabilities']['supports_range'])

    def test_list_media_adapts_transport_items_to_remote_media(self):
        driver = self.driver()
        media = driver.list_media()
        self.assertEqual(len(media), 1)
        self.assertIsInstance(media[0], RemoteMedia)
        self.assertEqual(media[0].storage, 'internal')
        driver.close()
        self.assertTrue(driver._client.closed)

    def test_open_download_reports_a_resumable_target(self):
        target = self.driver().open_download(
            RemoteMedia(id='internal/a.mp4', name='a.mp4', url='http://camera/a.mp4'), offset=64)
        self.assertIsInstance(target, DownloadTarget)
        self.assertEqual(target.url, 'http://camera/a.mp4')
        self.assertEqual(target.request_headers()['Range'], 'bytes=64-')

    def test_unreachable_camera_raises_a_typed_driver_error(self):
        with self.assertRaises(DriverUnreachableError) as ctx:
            self.driver(fail_connect=True).connect()
        self.assertEqual(ctx.exception.code, 'camera_unreachable')

        with self.assertRaises(DriverUnreachableError):
            self.driver(fail_connect=False, fail_list=True).list_media()


class RegistryTests(unittest.TestCase):
    def test_registry_rejects_unknown_driver(self):
        with self.assertRaisesRegex(ValueError, 'unsupported camera driver'):
            create_driver('not-a-camera', '192.168.42.1')

    def test_registry_uses_the_device_driver_and_host(self):
        with patch.object(luna_ultra, 'LunaClient', FakeLunaClient):
            driver = create_driver_for_device({
                'driver': 'luna_ultra',
                'camera_host': '192.168.42.99',
            })
        self.assertEqual(driver.host, '192.168.42.99')

    def test_endpoint_is_resolved_without_opening_a_session(self):
        endpoint = device_endpoint({'driver': 'luna_ultra', 'camera_host': '192.168.42.5'})
        self.assertEqual(endpoint, ('192.168.42.5', 80))

    def test_catalog_exposes_capabilities_for_every_driver(self):
        self.assertIn('luna_ultra', available_drivers())
        entry = next(d for d in driver_catalog() if d['id'] == 'luna_ultra')
        self.assertTrue(entry['capabilities']['supports_resume'])
        self.assertEqual(file_kind('LRV_0001.lrv.3ainfo.bin'), 'LRV')


class FakeResponse:
    def __init__(self, body=b'abcdef', status=200, headers=None):
        self.status = status
        self.headers = headers if headers is not None else {'Content-Length': str(len(body))}
        self._body = body
        self._sent = False

    def read(self, _):
        if self._sent:
            return b''
        self._sent = True
        return self._body

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class DownloadTargetTests(unittest.TestCase):
    def run_download(self, target, dest):
        requests = []

        def fake_urlopen(request, timeout=None):
            requests.append(request)
            return FakeResponse()

        with patch.object(downloader, 'urlopen', fake_urlopen):
            downloader.download_file(target, dest)
        return requests

    def test_driver_headers_are_sent_on_every_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'clip.mp4')
            target = DownloadTarget(url='http://camera/clip.mp4', headers={'X-Session': 'token'})
            requests = self.run_download(target, dest)
        self.assertTrue(requests)
        self.assertTrue(all(r.get_header('X-session') == 'token' for r in requests))

    def test_a_driver_without_range_restarts_instead_of_resuming(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'clip.mp4')
            with open(dest + '.part', 'wb') as f:
                f.write(b'xy')
            target = DownloadTarget(url='http://camera/clip.mp4', supports_range=False)
            requests = self.run_download(target, dest)
            with open(dest, 'rb') as f:
                self.assertEqual(f.read(), b'abcdef')
        self.assertEqual(len(requests), 1)
        self.assertIsNone(requests[0].get_header('Range'))

    def test_a_plain_url_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'clip.mp4')
            self.run_download('http://camera/clip.mp4', dest)
            self.assertTrue(os.path.exists(dest))


if __name__ == '__main__':
    unittest.main()
