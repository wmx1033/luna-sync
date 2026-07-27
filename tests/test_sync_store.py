import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from sync_store import LEGACY_LUNA_DEVICE_ID, MIGRATIONS, SyncStore


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

    def test_deleting_a_device_removes_its_media_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store_at(tmp)
            store.upsert_device({'id': 'luna', 'display_name': 'Luna Ultra',
                                 'driver': 'luna_ultra', 'camera_host': '192.168.42.1'})
            run = store.start_sync_run('luna')
            store.record_error('luna', 'boom', sync_run_id=run['id'])
            store.mark_media_complete('luna', 'internal/A.mp4', '/downloads/A.mp4', 10)

            self.assertTrue(store.delete_device('luna'))
            self.assertFalse(store.delete_device('luna'))
            self.assertEqual(store.list_devices(), [])
            self.assertEqual(store.count_media('luna'), 0)
            self.assertEqual(store.list_sync_runs('luna'), [])
            self.assertEqual(store.list_sync_errors('luna'), [])

    def test_a_scan_reflects_local_state_without_losing_completion_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store_at(tmp)
            store.upsert_device({'id': 'luna', 'display_name': 'Luna Ultra',
                                 'driver': 'luna_ultra', 'camera_host': '192.168.42.1'})
            done = {'remote_id': 'internal/A.mp4', 'storage_id': 'internal',
                    'remote_path': 'A.mp4', 'local_path': '/downloads/A.mp4',
                    'size_bytes': 10, 'kind': 'MP4', 'status': 'complete'}
            pending = {'remote_id': 'internal/B.mp4', 'storage_id': 'internal',
                       'remote_path': 'B.mp4', 'size_bytes': 20, 'kind': 'MP4'}
            store.record_scanned_media('luna', [done, pending])
            first_completion = store.get_media('luna', 'internal/A.mp4')['completed_at']

            store.record_scanned_media('luna', [done, pending])
            self.assertEqual(store.get_media('luna', 'internal/A.mp4')['completed_at'],
                             first_completion)
            self.assertEqual(store.count_media('luna', 'complete'), 1)
            self.assertEqual(store.count_media('luna', 'pending'), 1)

            # The file disappeared from the archive, so the next scan says so.
            store.record_scanned_media('luna', [dict(done, status='pending', local_path=''),
                                                pending])
            reverted = store.get_media('luna', 'internal/A.mp4')
            self.assertEqual(reverted['status'], 'pending')
            self.assertEqual(reverted['local_path'], '')
            self.assertEqual(reverted['completed_at'], '')

    def test_summary_reports_progress_and_the_latest_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store_at(tmp)
            store.upsert_device({'id': 'luna', 'display_name': 'Luna Ultra',
                                 'driver': 'luna_ultra', 'camera_host': '192.168.42.1'})
            store.record_scanned_media('luna', [
                {'remote_id': 'internal/A.mp4', 'remote_path': 'A.mp4', 'size_bytes': 10},
                {'remote_id': 'internal/B.mp4', 'remote_path': 'B.mp4', 'size_bytes': 20},
            ])
            store.mark_media_complete('luna', 'internal/A.mp4', '/downloads/A.mp4', 10)
            run = store.start_sync_run('luna')
            store.finish_sync_run(run['id'], 'success', scanned_count=2, added_count=1)
            store.record_error('luna', '相机离线', error_code='camera_unreachable')

            summary = store.device_summary('luna')
            self.assertEqual(summary['completed_count'], 1)
            self.assertEqual(summary['pending_count'], 1)
            self.assertEqual(summary['completed_bytes'], 10)
            self.assertTrue(summary['last_success_at'])
            self.assertEqual(summary['last_error']['error_code'], 'camera_unreachable')

    def test_bytes_credited_by_workers_survive_closing_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store_at(tmp)
            store.upsert_device({'id': 'luna', 'display_name': 'Luna Ultra',
                                 'driver': 'luna_ultra', 'camera_host': '192.168.42.1'})
            run = store.start_sync_run('luna')
            store.add_downloaded_bytes(run['id'], 512)
            store.finish_sync_run(run['id'], 'success', scanned_count=1, added_count=1)
            store.add_downloaded_bytes(run['id'], 512)
            self.assertEqual(store.get_sync_run(run['id'])['downloaded_bytes'], 1024)

    def test_new_schema_version_applies_only_its_own_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store_at(tmp)
            store.initialize()

            def add_marker_table(conn):
                conn.execute('CREATE TABLE migration_marker (id INTEGER PRIMARY KEY)')

            MIGRATIONS[2] = add_marker_table
            try:
                reopened = self.store_at(tmp)
                reopened.initialize()
                with reopened._connection() as conn:
                    versions = [row['version'] for row in conn.execute(
                        'SELECT version FROM schema_migrations ORDER BY version'
                    )]
                    marker = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'migration_marker'"
                    ).fetchone()
                self.assertEqual(versions, [1, 2])
                self.assertIsNotNone(marker)
            finally:
                MIGRATIONS.pop(2, None)


if __name__ == '__main__':
    unittest.main()
