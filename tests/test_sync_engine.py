import errno
import logging
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import sync_engine
from camera_driver import DownloadTarget, DriverAuthError, DriverUnreachableError, RemoteMedia
from sync_engine import SyncEngine
from sync_store import SyncStore


def quiet_logger():
    """Expected failures are part of these tests; keep them out of the report."""
    log = logging.getLogger('test_sync_engine')
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


def media(name, storage='internal', date='10-Jul-2026', time='10:19', size=6, kind='MP4'):
    return RemoteMedia(id=storage + '/' + name, name=name, url='http://camera/' + name,
                       path=name, date=date, time=time, bytes=size, bytes_exact=True,
                       kind=kind, storage=storage)


class FakeDriver:
    def __init__(self, items, fail_connect=None, fail_list=None):
        self.items = items
        self.fail_connect = fail_connect
        self.fail_list = fail_list
        self.closed = False

    def connect(self):
        if self.fail_connect:
            raise self.fail_connect

    def list_media(self):
        if self.fail_list:
            raise self.fail_list
        return list(self.items)

    def open_download(self, item, offset=0):
        return DownloadTarget(url=item.url, offset=offset)

    def close(self):
        self.closed = True


class EngineHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, 'downloads')
        os.makedirs(self.root)
        self.store = SyncStore(os.path.join(self.tmp.name, 'state', 'sync.db'))
        self.drivers = {}
        self.written = []
        self.failures = {}

    def add_device(self, device_id, name, priority=100, enabled=True, items=(), driver=None):
        self.store.upsert_device({'id': device_id, 'display_name': name, 'driver': 'fake',
                                  'camera_host': '192.168.42.1', 'priority': priority,
                                  'enabled': enabled})
        self.drivers[device_id] = driver or FakeDriver(list(items))
        return self.store.get_device(device_id)

    def fake_download(self, target, destination, on_progress=None, cancel=None):
        if cancel is not None and cancel.is_set():
            raise sync_engine.CancelledError('cancelled')
        failure = self.failures.pop(target.url, None)
        if failure:
            raise failure
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, 'wb') as handle:
            handle.write(b'abcdef')
        self.written.append(destination)
        if on_progress:
            on_progress(os.path.basename(destination), 6, 6, 0)
        return destination

    def engine(self, connector=None, download=None):
        return SyncEngine(
            store=self.store,
            download_root=self.root,
            driver_factory=lambda device: self.drivers[device['id']],
            connector=connector or (lambda device: True),
            download=download or self.fake_download,
            log=quiet_logger(),
        )

    def relative(self, path):
        return os.path.relpath(path, self.root)


class SyncFlowTests(EngineHarness):
    def test_a_sync_scans_downloads_and_archives_by_day(self):
        self.add_device('luna', 'Luna Ultra', items=[media('VID_0001.mp4')])
        summary = self.engine().sync_device(self.store.get_device('luna'))

        self.assertEqual(summary['status'], sync_engine.SUCCESS)
        self.assertEqual((summary['scanned'], summary['downloaded'], summary['failed']), (1, 1, 0))
        self.assertEqual(self.relative(self.written[0]),
                         os.path.join('luna', '2026', '07', '10', 'internal', 'VID_0001.mp4'))
        record = self.store.get_media('luna', 'internal/VID_0001.mp4')
        self.assertEqual(record['status'], 'complete')
        self.assertEqual(record['size_bytes'], 6)

    def test_media_without_a_date_still_lands_in_the_archive(self):
        self.add_device('luna', 'Luna Ultra', items=[media('clip.mp4', date='', time='')])
        self.engine().sync_device(self.store.get_device('luna'))
        self.assertEqual(self.relative(self.written[0]),
                         os.path.join('luna', 'unknown-date', 'internal', 'clip.mp4'))

    def test_sidecars_and_raw_files_are_synced_too(self):
        self.add_device('luna', 'Luna Ultra', items=[
            media('VID_0001.mp4'),
            media('LRV_0001.lrv', kind='LRV'),
            media('IMG_0001.dng', kind='DNG'),
            media('VID_0001.mp4.3ainfo.bin', kind='BIN'),
        ])
        summary = self.engine().sync_device(self.store.get_device('luna'))
        self.assertEqual(summary['downloaded'], 4)

    def test_a_second_sync_does_not_download_again(self):
        self.add_device('luna', 'Luna Ultra', items=[media('VID_0001.mp4')])
        engine = self.engine()
        engine.sync_device(self.store.get_device('luna'))
        summary = engine.sync_device(self.store.get_device('luna'))

        self.assertEqual(len(self.written), 1)
        self.assertEqual(summary['downloaded'], 0)
        self.assertEqual(summary['skipped'], 1)

    def test_the_same_name_from_two_devices_is_kept_apart(self):
        self.add_device('luna', 'Luna Ultra', items=[media('VID_0001.mp4')])
        self.add_device('ace', 'Ace Pro 2', items=[media('VID_0001.mp4')])
        self.engine().run_once()

        self.assertEqual(len(self.written), 2)
        self.assertEqual(len(set(self.written)), 2)
        for record in ('luna', 'ace'):
            self.assertEqual(self.store.get_media(record, 'internal/VID_0001.mp4')['status'],
                             'complete')

    def test_scan_only_records_without_downloading(self):
        self.add_device('luna', 'Luna Ultra', items=[media('VID_0001.mp4')])
        summary = self.engine().sync_device(self.store.get_device('luna'), scan_only=True)
        self.assertEqual(self.written, [])
        self.assertEqual(summary['scanned'], 1)
        self.assertEqual(self.store.queue_snapshot('luna'), {'pending': 1})

    def test_an_offline_camera_is_not_reported_as_a_sync(self):
        self.add_device('luna', 'Luna Ultra', items=[media('VID_0001.mp4')])
        engine = self.engine(connector=lambda device: False)
        summary = engine.sync_device(self.store.get_device('luna'))

        self.assertEqual(summary['status'], sync_engine.IDLE)
        self.assertEqual(self.store.list_sync_runs('luna'), [])
        self.assertEqual(engine.state('luna')['status'], sync_engine.IDLE)


class SchedulingTests(EngineHarness):
    def test_devices_run_in_priority_order(self):
        self.add_device('slow', 'Slow', priority=200, items=[media('B.mp4')])
        self.add_device('fast', 'Fast', priority=10, items=[media('A.mp4')])
        order = [summary['device_id'] for summary in self.engine().run_once()]
        self.assertEqual(order, ['fast', 'slow'])

    def test_disabled_devices_are_skipped(self):
        self.add_device('off', 'Off', enabled=False, items=[media('A.mp4')])
        engine = self.engine()
        self.assertEqual(engine.run_once(), [])
        self.assertEqual(engine.state('off')['status'], sync_engine.DISABLED)

    def test_one_radio_means_the_second_device_waits(self):
        self.add_device('first', 'First', priority=10, items=[media('A.mp4')])
        self.add_device('second', 'Second', priority=20, items=[media('B.mp4')])
        engine = self.engine()
        observed = []
        started = threading.Event()
        release = threading.Event()

        def blocking_download(target, destination, on_progress=None, cancel=None):
            started.set()
            # Hold the radio while another sync tries to start.
            observed.append(engine.sync_device(self.store.get_device('second')))
            release.set()
            return self.fake_download(target, destination, on_progress, cancel)

        engine._download = blocking_download
        engine.sync_device(self.store.get_device('first'))

        self.assertTrue(started.is_set())
        self.assertEqual(observed[0]['status'], sync_engine.WAITING)
        self.assertEqual(engine.state('second')['status'], sync_engine.WAITING)
        # The blocked device is untouched and still has everything to do.
        self.assertEqual(self.store.queue_snapshot('second'), {})


class FailureTests(EngineHarness):
    def test_an_unreachable_camera_ends_the_run_as_an_error(self):
        self.add_device('luna', 'Luna Ultra',
                        driver=FakeDriver([], fail_connect=DriverUnreachableError('相机离线')))
        engine = self.engine()
        summary = engine.sync_device(self.store.get_device('luna'))

        self.assertEqual(summary['status'], sync_engine.ERROR)
        run = self.store.list_sync_runs('luna')[0]
        self.assertEqual(run['status'], 'error')
        self.assertEqual(self.store.list_sync_errors('luna')[0]['error_code'], 'camera_unreachable')
        self.assertEqual(engine.state('luna')['code'], 'camera_unreachable')

    def test_a_file_failure_is_retried_later_while_the_run_continues(self):
        self.add_device('luna', 'Luna Ultra', items=[media('A.mp4'), media('B.mp4')])
        self.failures['http://camera/A.mp4'] = OSError('connection reset')
        summary = self.engine().sync_device(self.store.get_device('luna'))

        self.assertEqual(summary['status'], sync_engine.PARTIAL)
        self.assertEqual((summary['downloaded'], summary['failed']), (1, 1))
        record = self.store.get_media('luna', 'internal/A.mp4')
        self.assertEqual(record['status'], 'pending')
        self.assertEqual(record['attempts'], 1)
        self.assertTrue(record['next_attempt_at'])

    def test_a_rejected_password_stops_the_device_without_retrying(self):
        self.add_device('luna', 'Luna Ultra', items=[media('A.mp4'), media('B.mp4')])
        self.failures['http://camera/A.mp4'] = DriverAuthError('密码错误')
        summary = self.engine().sync_device(self.store.get_device('luna'))

        self.assertEqual(summary['status'], sync_engine.PARTIAL)
        failed = self.store.get_media('luna', 'internal/A.mp4')
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['next_attempt_at'], '')
        # The run stopped there, so the queued file was never attempted.
        self.assertEqual(self.store.get_media('luna', 'internal/B.mp4')['status'], 'pending')
        self.assertEqual(self.written, [])

    def test_a_full_disk_stops_the_device_run(self):
        self.add_device('luna', 'Luna Ultra', items=[media('A.mp4')])
        self.failures['http://camera/A.mp4'] = OSError(errno.ENOSPC, 'no space left')
        summary = self.engine().sync_device(self.store.get_device('luna'))
        self.assertEqual(summary['status'], sync_engine.PARTIAL)
        self.assertEqual(self.store.list_sync_errors('luna')[0]['error_code'], 'no_space')

    def test_repeated_failures_eventually_park_the_file(self):
        self.add_device('luna', 'Luna Ultra', items=[media('A.mp4')])
        engine = self.engine()
        for _ in range(sync_engine.MAX_ATTEMPTS):
            self.failures['http://camera/A.mp4'] = OSError('connection reset')
            self.store.requeue_media('luna', 'internal/A.mp4')
            engine.sync_device(self.store.get_device('luna'))

        record = self.store.get_media('luna', 'internal/A.mp4')
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['attempts'], sync_engine.MAX_ATTEMPTS)

    def test_backoff_grows_and_is_capped(self):
        self.assertEqual(sync_engine.backoff_seconds(1), sync_engine.RETRY_BASE_SECONDS)
        self.assertEqual(sync_engine.backoff_seconds(2), sync_engine.RETRY_BASE_SECONDS * 2)
        self.assertEqual(sync_engine.backoff_seconds(99), sync_engine.RETRY_MAX_SECONDS)

    def test_a_file_removed_from_the_camera_leaves_the_queue(self):
        self.add_device('luna', 'Luna Ultra', items=[media('A.mp4')])
        self.engine().sync_device(self.store.get_device('luna'), scan_only=True)
        self.drivers['luna'].items = []
        summary = self.engine().sync_device(self.store.get_device('luna'))

        self.assertEqual(summary['failed'], 1)
        record = self.store.get_media('luna', 'internal/A.mp4')
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['last_error_code'], 'missing_on_camera')


class RecoveryTests(EngineHarness):
    def test_a_cancelled_download_returns_to_the_queue_without_penalty(self):
        self.add_device('luna', 'Luna Ultra', items=[media('A.mp4')])
        cancel = threading.Event()
        cancel.set()
        self.engine().sync_device(self.store.get_device('luna'), cancel=cancel)

        record = self.store.get_media('luna', 'internal/A.mp4')
        self.assertEqual(record['status'], 'pending')
        self.assertEqual(record['attempts'], 0)

    def test_a_restart_requeues_interrupted_downloads(self):
        self.add_device('luna', 'Luna Ultra', items=[media('A.mp4')])
        self.engine().sync_device(self.store.get_device('luna'), scan_only=True)
        self.store.claim_next_media('luna')
        self.assertEqual(self.store.queue_snapshot('luna'), {'downloading': 1})

        restarted = SyncStore(self.store.path)
        engine = SyncEngine(store=restarted, download_root=self.root,
                            driver_factory=lambda device: self.drivers[device['id']],
                            download=self.fake_download)
        self.assertEqual(engine.recover(), 1)
        self.assertEqual(restarted.queue_snapshot('luna'), {'pending': 1})

        engine.sync_device(restarted.get_device('luna'))
        self.assertEqual(restarted.get_media('luna', 'internal/A.mp4')['status'], 'complete')

    def test_a_file_already_on_disk_is_adopted_instead_of_downloaded(self):
        item = media('VID_0001.mp4')
        self.add_device('luna', 'Luna Ultra', items=[item])
        engine = self.engine()
        destination = engine.destination_for(self.store.get_device('luna'), item)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, 'wb') as handle:
            handle.write(b'abcdef')

        summary = engine.sync_device(self.store.get_device('luna'))
        self.assertEqual(self.written, [])
        self.assertEqual(summary['downloaded'], 0)
        self.assertEqual(self.store.get_media('luna', 'internal/VID_0001.mp4')['status'],
                         'complete')

    def test_a_file_kept_at_its_pre_relayout_path_is_not_downloaded_again(self):
        item = media('VID_0001.mp4')
        self.add_device('luna', 'Luna Ultra', items=[item])
        legacy = os.path.join(self.root, 'internal', 'VID_0001.mp4')
        os.makedirs(os.path.dirname(legacy), exist_ok=True)
        with open(legacy, 'wb') as handle:
            handle.write(b'abcdef')
        self.store.record_scanned_media('luna', [{
            'remote_id': 'internal/VID_0001.mp4', 'remote_path': 'VID_0001.mp4',
            'local_path': legacy, 'status': 'complete', 'size_bytes': 6, 'kind': 'MP4',
        }])

        summary = self.engine().sync_device(self.store.get_device('luna'))
        self.assertEqual(self.written, [])
        self.assertEqual(summary['downloaded'], 0)
        self.assertEqual(self.store.get_media('luna', 'internal/VID_0001.mp4')['local_path'],
                         legacy)


if __name__ == '__main__':
    unittest.main()
