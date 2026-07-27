import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from sync_store import LEGACY_LUNA_DEVICE_ID, SyncStore


class SyncStoreTests(unittest.TestCase):
    def store_at(self, root):
        return SyncStore(os.path.join(root, 'state', 'sync.db'))

    def test_legacy_config_migration_is_idempotent_and_does_not_store_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store_at(tmp)
            config = {
                'camera_host': '192.168.42.1',
                'camera_ssid': 'Luna Ultra TEST',
                'camera_password': 'do-not-store-me',
                'download_dir': '/downloads',
            }
            first = store.migrate_legacy_config(config)
            second = store.migrate_legacy_config(config)

            self.assertEqual(first['id'], LEGACY_LUNA_DEVICE_ID)
            self.assertEqual(first['credential_ref'], 'legacy-config')
            self.assertEqual(first['created_at'], second['created_at'])
            self.assertEqual(len(store.list_devices()), 1)

            with store._connection() as conn:
                columns = [row['name'] for row in conn.execute('PRAGMA table_info(devices)')]
            self.assertNotIn('wifi_password', columns)

    def test_same_remote_id_is_isolated_by_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store_at(tmp)
            for device_id in ('ace-pro-2', 'go-ultra'):
                store.upsert_device({
                    'id': device_id,
                    'display_name': device_id,
                    'driver': device_id.replace('-', '_'),
                    'camera_host': '192.168.42.1',
                })
                store.record_media({
                    'device_id': device_id,
                    'remote_id': 'internal/VID_0001.mp4',
                    'storage_id': 'internal',
                    'remote_path': 'VID_0001.mp4',
                    'local_path': '/downloads/%s/VID_0001.mp4' % device_id,
                    'size_bytes': 1024,
                    'kind': 'MP4',
                    'status': 'complete',
                })

            ace_media = store.get_media('ace-pro-2', 'internal/VID_0001.mp4')
            go_media = store.get_media('go-ultra', 'internal/VID_0001.mp4')
            self.assertNotEqual(ace_media['local_path'], go_media['local_path'])
            self.assertEqual(ace_media['status'], 'complete')
            self.assertEqual(go_media['status'], 'complete')

    def test_runs_and_errors_survive_store_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store_at(tmp)
            store.upsert_device({
                'id': 'luna',
                'display_name': 'Luna Ultra',
                'driver': 'luna_ultra',
                'camera_host': '192.168.42.1',
            })
            run = store.start_sync_run('luna')
            store.record_error('luna', 'camera offline', sync_run_id=run['id'], error_code='offline')
            store.finish_sync_run(run['id'], 'partial', scanned_count=5, added_count=2,
                                  skipped_count=3, downloaded_bytes=2048,
                                  error_summary='camera offline')

            reopened = self.store_at(tmp)
            persisted_run = reopened.get_sync_run(run['id'])
            self.assertEqual(persisted_run['status'], 'partial')
            self.assertEqual(persisted_run['downloaded_bytes'], 2048)
            with reopened._connection() as conn:
                error = conn.execute('SELECT * FROM sync_errors WHERE sync_run_id = ?', (run['id'],)).fetchone()
            self.assertEqual(error['error_code'], 'offline')


if __name__ == '__main__':
    unittest.main()
