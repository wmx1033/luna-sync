"""Wiring tests for the web runtime: the sync flow must reach the store.

Flask is provided by the container image (``python3-flask``); when it is not
installed locally these tests skip instead of failing.
"""

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app')
sys.path.insert(0, APP_DIR)

HAS_FLASK = importlib.util.find_spec('flask') is not None


def load_web_app(workdir):
    """Import a fresh web_app against ``workdir``, as a service restart would."""
    state = os.path.join(workdir, 'state')
    downloads = os.path.join(workdir, 'downloads')
    os.makedirs(state, exist_ok=True)
    os.makedirs(downloads, exist_ok=True)
    config_path = os.path.join(workdir, 'config.json')
    with open(config_path, 'w') as handle:
        json.dump({
            'camera_host': '192.168.42.1',
            'camera_ssid': 'Luna Ultra TEST',
            'camera_password': 'do-not-leak-me',
            'download_dir': downloads,
            'state_dir': state,
            'wifi_backend': 'none',
        }, handle)
    os.environ.update({
        'LUNA_CONFIG': config_path,
        'STATE_DIR': state,
        'DOWNLOAD_DIR': downloads,
        'LUNA_WIFI_BACKEND': 'none',
    })
    sys.modules.pop('web_app', None)
    return importlib.import_module('web_app')


class FakeDriver:
    def __init__(self, media):
        self.media = media
        self.closed = False

    def connect(self):
        pass

    def list_media(self):
        return self.media

    def close(self):
        self.closed = True


@unittest.skipUnless(HAS_FLASK, 'flask is not installed')
class WebAppStoreWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(sys.modules.pop, 'web_app', None)
        self.web_app = load_web_app(self.tmp.name)

    def remote_media(self):
        from camera_driver import RemoteMedia
        return [RemoteMedia(id='internal/VID_0001.mp4', name='VID_0001.mp4',
                            url='http://camera/VID_0001.mp4', path='VID_0001.mp4',
                            date='10-Jul-2026', time='10:19', bytes=6,
                            kind='MP4', storage='internal', storage_label='内置存储')]

    def scan(self):
        web_app = self.web_app
        driver = FakeDriver(self.remote_media())
        with patch.object(web_app, 'wifi_on_target', lambda: True), \
             patch.object(web_app, 'cam_on', lambda: True), \
             patch.object(web_app, 'active_camera_driver', lambda: driver):
            self.assertTrue(web_app.refresh(persist=True))
        return driver

    def test_legacy_config_creates_a_default_device_without_leaking_secrets(self):
        payload = self.web_app.app.test_client().get('/api/devices').get_json()
        self.assertEqual(payload['active'], 'luna-ultra-default')
        self.assertEqual(len(payload['items']), 1)
        self.assertTrue(payload['items'][0]['has_credential'])
        serialized = json.dumps(payload)
        self.assertNotIn('do-not-leak-me', serialized)
        self.assertNotIn('credential_ref', serialized)

    def test_a_scan_persists_media_and_reflects_local_files(self):
        web_app = self.web_app
        self.scan()
        record = web_app.SYNC_STORE.get_media(web_app.DEVICE_ID, 'internal/VID_0001.mp4')
        self.assertEqual(record['status'], 'pending')
        self.assertEqual(record['storage_id'], 'internal')
        self.assertEqual(record['captured_at'], '10-Jul-2026 10:19')

        dest = os.path.join(web_app.DLDIR, 'internal', 'VID_0001.mp4')
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'wb') as handle:
            handle.write(b'abcdef')
        self.scan()
        record = web_app.SYNC_STORE.get_media(web_app.DEVICE_ID, 'internal/VID_0001.mp4')
        self.assertEqual(record['status'], 'complete')
        self.assertEqual(record['local_path'], dest)

    def test_a_finished_download_is_recorded_and_credited_to_its_run(self):
        web_app = self.web_app
        run = web_app.start_sync_run()
        dest = os.path.join(web_app.DLDIR, 'internal', 'VID_0001.mp4')
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'wb') as handle:
            handle.write(b'abcdef')
        web_app.record_completed_media(self.remote_media()[0].as_dict(), dest, 6)
        web_app.finish_sync_run(run, 'success', scanned=1, added=1)

        stored_run = web_app.SYNC_STORE.get_sync_run(run['id'])
        self.assertEqual(stored_run['status'], 'success')
        self.assertEqual(stored_run['downloaded_bytes'], 6)
        summary = web_app.SYNC_STORE.device_summary(web_app.DEVICE_ID)
        self.assertEqual(summary['completed_count'], 1)
        self.assertTrue(summary['last_success_at'])

    def test_driver_failures_land_in_the_device_history(self):
        from camera_driver import DriverAuthError
        web_app = self.web_app
        web_app.record_sync_error(DriverAuthError('密码错误'), remote_id='internal/VID_0001.mp4')
        error = web_app.SYNC_STORE.list_sync_errors(web_app.DEVICE_ID)[0]
        self.assertEqual(error['error_code'], 'camera_auth_failed')
        self.assertEqual(error['retryable'], 0)
        self.assertEqual(error['remote_id'], 'internal/VID_0001.mp4')

    def test_a_failed_scan_is_recorded_and_keeps_the_service_running(self):
        from camera_driver import DriverUnreachableError
        web_app = self.web_app

        def explode():
            raise DriverUnreachableError('camera offline')

        with patch.object(web_app, 'wifi_on_target', lambda: True), \
             patch.object(web_app, 'cam_on', lambda: True), \
             patch.object(web_app, 'active_camera_driver', explode):
            self.assertFalse(web_app.refresh(persist=True))
        error = web_app.SYNC_STORE.list_sync_errors(web_app.DEVICE_ID)[0]
        self.assertEqual(error['error_code'], 'camera_unreachable')

    def test_reachability_uses_the_endpoint_declared_by_the_driver(self):
        self.assertEqual(self.web_app.camera_endpoint(), ('192.168.42.1', 80))

    def test_completed_media_and_history_survive_a_restart(self):
        web_app = self.web_app
        self.scan()
        dest = os.path.join(web_app.DLDIR, 'internal', 'VID_0001.mp4')
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'wb') as handle:
            handle.write(b'abcdef')
        run = web_app.start_sync_run()
        web_app.record_completed_media(self.remote_media()[0].as_dict(), dest, 6)
        web_app.finish_sync_run(run, 'success', scanned=1, added=1)

        restarted = load_web_app(self.tmp.name)
        self.assertEqual(restarted.ST['completed'], 1)
        self.assertEqual(len(restarted.SYNC_STORE.list_devices()), 1)
        self.assertEqual(len(restarted.SYNC_STORE.list_sync_runs(restarted.DEVICE_ID)), 1)
        self.assertEqual(
            restarted.SYNC_STORE.get_media(restarted.DEVICE_ID, 'internal/VID_0001.mp4')['status'],
            'complete')


if __name__ == '__main__':
    unittest.main()
