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
    def __init__(self, items, fail=None):
        self.items = items
        self.fail = fail
        self.closed = False

    def connect(self):
        if self.fail:
            raise self.fail

    def list_media(self):
        if self.fail:
            raise self.fail
        return list(self.items)

    def open_download(self, item, offset=0):
        from camera_driver import DownloadTarget
        return DownloadTarget(url=item.url, offset=offset)

    def close(self):
        self.closed = True


@unittest.skipUnless(HAS_FLASK, 'flask is not installed')
class WebAppEngineWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(sys.modules.pop, 'web_app', None)
        self.web_app = load_web_app(self.tmp.name)
        self.written = []

    def remote_media(self, name='VID_20260710_101942.mp4'):
        from camera_driver import RemoteMedia
        return RemoteMedia(id='internal/' + name, name=name,
                           url='http://camera/' + name, path=name,
                           date='10-Jul-2026', time='10:19', bytes=6, bytes_exact=True,
                           kind='MP4', storage='internal', storage_label='内置存储')

    def fake_download(self, target, destination, on_progress=None, cancel=None):
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, 'wb') as handle:
            handle.write(b'abcdef')
        self.written.append(destination)
        return destination

    def arm_engine(self, items, fail=None, download=None):
        driver = FakeDriver(list(items), fail=fail)
        engine = self.web_app.ENGINE
        engine.driver_factory = lambda device: driver
        engine.connector = lambda device: True
        engine._download = download or self.fake_download
        return driver

    def relative(self, path):
        return os.path.relpath(path, self.web_app.DLDIR)

    def test_legacy_config_creates_a_default_device_without_leaking_secrets(self):
        payload = self.web_app.app.test_client().get('/api/devices').get_json()
        self.assertEqual(payload['active'], 'luna-ultra-default')
        self.assertTrue(payload['items'][0]['has_credential'])
        serialized = json.dumps(payload)
        self.assertNotIn('do-not-leak-me', serialized)
        self.assertNotIn('credential_ref', serialized)

    def test_a_sync_archives_by_day_and_records_the_run(self):
        web_app = self.web_app
        item = self.remote_media()
        self.arm_engine([item])
        self.assertEqual(web_app.run_sync(manual=True), 1)

        self.assertEqual(
            self.relative(self.written[0]),
            os.path.join('luna-ultra-default', '2026', '07', '10', 'internal', item.name))
        record = web_app.SYNC_STORE.get_media(web_app.DEVICE_ID, item.id)
        self.assertEqual(record['status'], 'complete')
        run = web_app.SYNC_STORE.list_sync_runs(web_app.DEVICE_ID)[0]
        self.assertEqual(run['status'], 'success')
        self.assertEqual(run['downloaded_bytes'], 6)

    def test_the_media_library_finds_files_at_their_archived_path(self):
        web_app = self.web_app
        item = self.remote_media()
        self.arm_engine([item])
        web_app.run_sync(manual=True)

        resolved = web_app.local_path(item.id)
        self.assertEqual(resolved, self.written[0])
        entry = next(row for row in web_app.local_items() if row['id'] == item.id)
        self.assertEqual(entry['device'], 'Luna Ultra')
        self.assertEqual(entry['storage'], 'internal')
        self.assertEqual(entry['bytes'], 6)

    def test_an_offline_camera_does_not_record_a_sync(self):
        web_app = self.web_app
        self.arm_engine([self.remote_media()])
        web_app.ENGINE.connector = lambda device: False
        self.assertEqual(web_app.run_sync(manual=True), 0)
        self.assertEqual(web_app.SYNC_STORE.list_sync_runs(web_app.DEVICE_ID), [])
        self.assertEqual(self.written, [])

    def test_a_driver_failure_lands_in_the_device_history(self):
        from camera_driver import DriverUnreachableError
        web_app = self.web_app
        self.arm_engine([], fail=DriverUnreachableError('相机离线'))
        web_app.run_sync(manual=True)

        error = web_app.SYNC_STORE.list_sync_errors(web_app.DEVICE_ID)[0]
        self.assertEqual(error['error_code'], 'camera_unreachable')
        self.assertEqual(web_app.SYNC_STORE.list_sync_runs(web_app.DEVICE_ID)[0]['status'], 'error')

    def test_the_queue_length_comes_from_the_store(self):
        web_app = self.web_app
        self.arm_engine([self.remote_media(), self.remote_media('VID_20260711_101942.mp4')])
        web_app.ENGINE.sync_device(web_app.active_device(), scan_only=True)
        self.assertEqual(web_app.queue_length(), 2)
        web_app.run_sync(manual=True)
        self.assertEqual(web_app.queue_length(), 0)

    def test_reachability_uses_the_endpoint_declared_by_the_driver(self):
        self.assertEqual(self.web_app.camera_endpoint(), ('192.168.42.1', 80))

    def test_completed_media_and_history_survive_a_restart(self):
        web_app = self.web_app
        item = self.remote_media()
        self.arm_engine([item])
        web_app.run_sync(manual=True)

        restarted = load_web_app(self.tmp.name)
        self.assertEqual(restarted.ST['completed'], 1)
        self.assertEqual(len(restarted.SYNC_STORE.list_sync_runs(restarted.DEVICE_ID)), 1)
        self.assertEqual(restarted.SYNC_STORE.get_media(restarted.DEVICE_ID, item.id)['status'],
                         'complete')
        self.assertEqual(restarted.local_path(item.id), self.written[0])

    def test_an_interrupted_download_is_requeued_on_restart(self):
        web_app = self.web_app
        item = self.remote_media()
        self.arm_engine([item])
        web_app.ENGINE.sync_device(web_app.active_device(), scan_only=True)
        web_app.SYNC_STORE.claim_next_media(web_app.DEVICE_ID)

        restarted = load_web_app(self.tmp.name)
        self.assertEqual(restarted.ENGINE.recover(), 1)
        self.assertEqual(restarted.queue_length(), 1)


@unittest.skipUnless(HAS_FLASK, 'flask is not installed')
class DeviceManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(sys.modules.pop, 'web_app', None)
        self.web_app = load_web_app(self.tmp.name)
        self.client = self.web_app.app.test_client()

    def create(self, **overrides):
        payload = {'display_name': 'GO Ultra', 'driver': 'go_ultra',
                   'camera_host': '192.168.42.1', 'ssid': 'GO Ultra TEST',
                   'password': 'hotspot-secret'}
        payload.update(overrides)
        return self.client.post('/api/devices', json=payload)

    def test_a_device_can_be_added_without_editing_any_file(self):
        response = self.create()
        self.assertEqual(response.status_code, 201)
        device = response.get_json()['device']
        self.assertEqual(device['id'], 'go-ultra')
        self.assertEqual(device['driver'], 'go_ultra')
        self.assertTrue(device['has_credential'])
        self.assertTrue(device['enabled'])

        listed = self.client.get('/api/devices').get_json()['items']
        self.assertIn('go-ultra', [item['id'] for item in listed])

    def test_the_password_is_stored_outside_the_database_and_never_echoed(self):
        self.create()
        serialized = json.dumps(self.client.get('/api/devices').get_json())
        self.assertNotIn('hotspot-secret', serialized)
        self.assertNotIn('password', serialized)

        self.assertEqual(self.web_app.CREDENTIALS.get('go-ultra'), 'hotspot-secret')
        with self.web_app.SYNC_STORE._connection() as conn:
            dumped = str(conn.execute('SELECT * FROM devices').fetchall())
        self.assertNotIn('hotspot-secret', dumped)

    def test_invalid_input_is_rejected_with_readable_errors(self):
        cases = [
            ({'display_name': ''}, '请填写设备名称'),
            ({'driver': 'not-a-driver'}, '不支持的驱动'),
            ({'camera_host': ''}, '请填写相机地址'),
            ({'camera_host': 'bad host!'}, '相机地址格式不正确'),
        ]
        for overrides, expected in cases:
            response = self.create(**overrides)
            self.assertEqual(response.status_code, 400, overrides)
            errors = ' '.join(response.get_json()['errors'])
            self.assertIn(expected, errors)
        self.assertEqual(self.client.get('/api/devices').get_json()['items'].__len__(), 1)

    def test_editing_keeps_the_existing_password_when_none_is_sent(self):
        self.create()
        response = self.client.patch('/api/devices/go-ultra',
                                     json={'display_name': 'GO Ultra 客厅', 'priority': 10})
        self.assertEqual(response.status_code, 200)
        device = response.get_json()['device']
        self.assertEqual(device['display_name'], 'GO Ultra 客厅')
        self.assertEqual(device['priority'], 10)
        self.assertTrue(device['has_credential'])
        self.assertEqual(self.web_app.CREDENTIALS.get('go-ultra'), 'hotspot-secret')

    def test_a_device_can_be_disabled_and_re_enabled(self):
        self.create()
        disabled = self.client.patch('/api/devices/go-ultra', json={'enabled': False})
        self.assertFalse(disabled.get_json()['device']['enabled'])
        enabled = self.client.patch('/api/devices/go-ultra', json={'enabled': True})
        self.assertTrue(enabled.get_json()['device']['enabled'])

    def test_deleting_removes_the_device_and_its_secret(self):
        self.create()
        self.assertEqual(self.client.delete('/api/devices/go-ultra').status_code, 200)
        self.assertIsNone(self.web_app.CREDENTIALS.get('go-ultra'))
        self.assertNotIn('go-ultra',
                         [d['id'] for d in self.client.get('/api/devices').get_json()['items']])
        self.assertEqual(self.client.delete('/api/devices/go-ultra').status_code, 404)

    def test_names_that_collide_still_get_distinct_ids(self):
        self.create()
        second = self.create().get_json()['device']
        self.assertEqual(second['id'], 'go-ultra-2')

    def test_test_connection_reports_a_readable_failure(self):
        self.create()
        self.web_app.ENGINE.connector = lambda device: False
        result = self.client.post('/api/devices/go-ultra/test').get_json()
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'camera_unreachable')
        self.assertTrue(result['message'])

    def test_test_connection_reports_the_probed_camera(self):
        from camera_driver import ProbeResult

        self.create()

        class Probed:
            def probe(self):
                return ProbeResult(driver='go_ultra', display_name='GO Ultra',
                                   host='192.168.42.1', reachable=True,
                                   model='Insta360 GO Ultra', identifier='SERIAL123',
                                   media_count=51)

            def close(self):
                pass

        self.web_app.ENGINE.connector = lambda device: True
        self.web_app.ENGINE.driver_factory = lambda device: Probed()
        result = self.client.post('/api/devices/go-ultra/test').get_json()
        self.assertTrue(result['ok'])
        self.assertEqual(result['probe']['model'], 'Insta360 GO Ultra')
        self.assertEqual(result['probe']['media_count'], 51)

    def test_a_disabled_device_refuses_a_manual_sync(self):
        self.create(enabled=False)
        response = self.client.post('/api/devices/go-ultra/sync')
        self.assertEqual(response.status_code, 400)

    def test_unknown_devices_answer_404(self):
        self.assertEqual(self.client.post('/api/devices/nope/test').status_code, 404)
        self.assertEqual(self.client.post('/api/devices/nope/sync').status_code, 404)
        self.assertEqual(self.client.patch('/api/devices/nope', json={}).status_code, 404)

    def test_the_migrated_default_device_keeps_working(self):
        items = self.client.get('/api/devices').get_json()['items']
        legacy = next(item for item in items if item['id'] == 'luna-ultra-default')
        self.assertEqual(legacy['ssid'], 'Luna Ultra TEST')
        self.assertTrue(legacy['has_credential'])
        # It came from config.json, so deleting it would just resurrect it on restart.
        self.assertEqual(self.client.delete('/api/devices/luna-ultra-default').status_code, 400)


@unittest.skipUnless(HAS_FLASK, 'flask is not installed')
class ArchiveMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(sys.modules.pop, 'web_app', None)

    def seed_v1_archive(self, files):
        downloads = os.path.join(self.tmp.name, 'downloads')
        for relative, payload in files.items():
            path = os.path.join(downloads, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as handle:
                handle.write(payload)

    def test_a_v1_archive_is_relocated_and_counted_as_already_downloaded(self):
        self.seed_v1_archive({
            os.path.join('internal', 'VID_20260710_101942.mp4'): b'abcdef',
            os.path.join('external', 'IMG_20260709_090000.jpg'): b'xy',
        })
        web_app = load_web_app(self.tmp.name)
        web_app.prepare_archive()

        moved = os.path.join(web_app.DLDIR, 'luna-ultra-default', '2026', '07', '10', 'internal',
                             'VID_20260710_101942.mp4')
        self.assertTrue(os.path.isfile(moved))
        self.assertFalse(os.path.exists(os.path.join(web_app.DLDIR, 'internal')))

        record = web_app.SYNC_STORE.get_media(web_app.DEVICE_ID, 'internal/VID_20260710_101942.mp4')
        self.assertEqual(record['status'], 'complete')
        self.assertEqual(record['local_path'], moved)
        self.assertEqual(record['size_bytes'], 6)
        self.assertEqual(web_app.local_path('internal/VID_20260710_101942.mp4'), moved)

    def test_relocated_media_is_not_downloaded_again(self):
        name = 'VID_20260710_101942.mp4'
        self.seed_v1_archive({os.path.join('internal', name): b'abcdef'})
        web_app = load_web_app(self.tmp.name)
        web_app.prepare_archive()

        from camera_driver import RemoteMedia
        item = RemoteMedia(id='internal/' + name, name=name, url='http://camera/' + name,
                           path=name, date='10-Jul-2026', time='10:19', bytes=6,
                           bytes_exact=True, kind='MP4', storage='internal')
        downloads = []
        web_app.ENGINE.driver_factory = lambda device: FakeDriver([item])
        web_app.ENGINE.connector = lambda device: True
        web_app.ENGINE._download = lambda *args, **kwargs: downloads.append(args)

        self.assertEqual(web_app.run_sync(manual=True), 0)
        self.assertEqual(downloads, [])
        self.assertEqual(web_app.SYNC_STORE.get_media(web_app.DEVICE_ID, item.id)['status'],
                         'complete')

    def test_running_the_migration_twice_is_harmless(self):
        self.seed_v1_archive({os.path.join('internal', 'VID_20260710_101942.mp4'): b'abcdef'})
        web_app = load_web_app(self.tmp.name)
        web_app.prepare_archive()
        self.assertEqual(web_app.prepare_archive(), [])
        self.assertEqual(web_app.SYNC_STORE.count_media(web_app.DEVICE_ID, 'complete'), 1)


if __name__ == '__main__':
    unittest.main()
