"""Driver-neutral sync orchestration.

The engine owns everything that is the same for every camera: which device runs
next, when to give up and retry, where a file lands and what gets written to the
store.  Drivers only speak their protocol; the network layer only makes an
address reachable.
"""

import errno
import logging
import os
import shutil
import threading
from datetime import datetime, timedelta, timezone

import archive
from camera_driver import DriverError
from sync_store import utc_now


#: Statuses surfaced per device, matching the vocabulary used by the UI.
DISABLED, IDLE, WAITING = 'disabled', 'idle', 'waiting'
CONNECTING, SCANNING, SYNCING = 'connecting', 'scanning', 'syncing'
SUCCESS, PARTIAL, ERROR = 'success', 'partial', 'error'

RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 1800
MAX_ATTEMPTS = 5
#: Refuse to start a download that would leave the archive volume this empty.
FREE_SPACE_MARGIN = 64 * 1024 * 1024

CANCELLED = 'cancelled'
NO_SPACE = 'no_space'
NETWORK_TIMEOUT = 'network_timeout'
FILE_ERROR = 'file_error'

#: Failures that make continuing with this camera pointless for now.
DEVICE_SCOPE_CODES = frozenset({
    'camera_unreachable', 'camera_auth_failed', 'camera_protocol_error', NO_SPACE,
})


class CancelledError(Exception):
    pass


def is_cancelled(exc):
    return isinstance(exc, CancelledError) or str(exc) == CANCELLED


def classify_error(exc):
    """Map any failure onto ``(code, retryable, device_scope)``."""
    if is_cancelled(exc):
        return CANCELLED, True, False
    if isinstance(exc, DriverError):
        return exc.code, exc.retryable, exc.code in DEVICE_SCOPE_CODES
    if isinstance(exc, OSError) and getattr(exc, 'errno', None) == errno.ENOSPC:
        return NO_SPACE, True, True
    if isinstance(exc, TimeoutError):
        return NETWORK_TIMEOUT, True, False
    return FILE_ERROR, True, False


def backoff_seconds(attempts):
    return min(RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1)), RETRY_MAX_SECONDS)


def retry_at(seconds, now=None):
    moment = now or datetime.now(timezone.utc)
    return (moment + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


class SyncEngine:
    """Runs devices one at a time and keeps the store as the single truth."""

    def __init__(self, store, download_root, driver_factory, connector=None, download=None,
                 log=None, on_progress=None, on_event=None, on_notice=None):
        self.store = store
        self.download_root = download_root
        self.driver_factory = driver_factory
        self.connector = connector or (lambda device: True)
        self._download = download
        self.log = log or logging.getLogger('sync_engine')
        self.on_progress = on_progress
        self.on_event = on_event
        self.on_notice = on_notice
        # One wireless card means one camera session at a time.
        self.radio = threading.Lock()
        self._states = {}
        self._states_lk = threading.RLock()

    # ------------------------------------------------------------------ state

    def set_state(self, device_id, status, **extra):
        with self._states_lk:
            state = self._states.setdefault(device_id, {})
            state['status'] = status
            state['updated_at'] = utc_now()
            state.update(extra)
            snapshot = dict(state)
        if self.on_event:
            self.on_event(device_id, snapshot)
        return snapshot

    def state(self, device_id):
        with self._states_lk:
            return dict(self._states.get(device_id) or {'status': IDLE})

    def states(self):
        with self._states_lk:
            return {device_id: dict(state) for device_id, state in self._states.items()}

    def notify(self, message):
        if self.on_notice:
            self.on_notice(message)
        else:
            self.log.info(message)

    # ------------------------------------------------------------- scheduling

    def schedulable_devices(self):
        """Enabled devices, highest priority first."""
        devices = []
        for device in self.store.list_devices():
            if not device['enabled']:
                self.set_state(device['id'], DISABLED)
                continue
            devices.append(device)
        return sorted(devices, key=lambda d: (d['priority'], d['display_name'], d['id']))

    def recover(self):
        """Startup recovery: anything left in flight belongs back in the queue."""
        requeued = self.store.requeue_in_flight()
        if requeued:
            self.notify('恢复 %d 个未完成的下载任务' % requeued)
        return requeued

    def test_device(self, device):
        """Probe one camera for the UI, without disturbing a running sync."""
        device_id = device['id']
        if not self.radio.acquire(blocking=False):
            return {'ok': False, 'status': WAITING, 'message': '无线网卡正被其他同步任务占用'}
        driver = None
        try:
            self.set_state(device_id, CONNECTING)
            if not self.connector(device):
                self.set_state(device_id, IDLE, message='相机未就绪')
                return {'ok': False, 'status': IDLE, 'code': 'camera_unreachable',
                        'message': '未能连接到相机，请确认相机已开机并开启 Wi-Fi'}
            driver = self.driver_factory(device)
            self.set_state(device_id, SCANNING)
            result = driver.probe()
            self.set_state(device_id, IDLE, message='')
            return {'ok': True, 'status': IDLE, 'probe': result.as_dict()}
        except Exception as exc:
            code, _retryable, _scope = classify_error(exc)
            message = self.record_error(device_id, exc, code=code)
            self.set_state(device_id, ERROR, message=message, code=code)
            return {'ok': False, 'status': ERROR, 'code': code, 'message': message}
        finally:
            if driver is not None:
                try:
                    driver.close()
                except Exception as exc:
                    self.log.warning('driver close: %s', str(exc)[:60])
            self.radio.release()

    def run_once(self, cancel=None, exclude_kinds=(), scan_only=False):
        results = []
        for device in self.schedulable_devices():
            if cancel is not None and cancel.is_set():
                break
            results.append(self.sync_device(device, cancel=cancel, exclude_kinds=exclude_kinds,
                                            scan_only=scan_only))
        return results

    # ------------------------------------------------------------ device sync

    def sync_device(self, device, cancel=None, exclude_kinds=(), scan_only=False,
                    only_ids=None):
        device_id = device['id']
        if not device['enabled']:
            self.set_state(device_id, DISABLED)
            return {'device_id': device_id, 'status': DISABLED}
        # Never let two cameras fight over the same wireless card.
        if not self.radio.acquire(blocking=False):
            self.set_state(device_id, WAITING, message='等待其他设备完成同步')
            return {'device_id': device_id, 'status': WAITING}
        try:
            return self._sync_locked(device, cancel, tuple(exclude_kinds), scan_only, only_ids)
        finally:
            self.radio.release()

    def _sync_locked(self, device, cancel, exclude_kinds, scan_only, only_ids=None):
        device_id = device['id']
        self.set_state(device_id, CONNECTING)
        if not self.connector(device):
            self.set_state(device_id, IDLE, message='相机未就绪')
            return {'device_id': device_id, 'status': IDLE, 'reason': 'offline'}

        run = self.store.start_sync_run(device_id)
        run_id = run['id'] if run else None
        summary = {'device_id': device_id, 'run_id': run_id, 'scanned': 0, 'added': 0,
                   'downloaded': 0, 'skipped': 0, 'failed': 0, 'bytes': 0}
        driver = None
        try:
            driver = self.driver_factory(device)
            self.set_state(device_id, SCANNING)
            driver.connect()
            media = list(driver.list_media())
            summary['scanned'] = len(media)
            catalog = {item.id: item for item in media}
            self.store.record_scanned_media(device_id, self.scan_records(device, media))
            snapshot = self.store.queue_snapshot(device_id)
            summary['added'] = snapshot.get('pending', 0)
            summary['skipped'] = summary['scanned'] - summary['added']

            if not scan_only:
                self.set_state(device_id, SYNCING)
                self.drain_queue(device, driver, catalog, cancel, exclude_kinds, run_id, summary,
                                 only_ids)
        except Exception as exc:
            code, _retryable, _scope = classify_error(exc)
            message = self.record_error(device_id, exc, run_id=run_id, code=code)
            self.store.finish_sync_run(run_id, ERROR, scanned_count=summary['scanned'],
                                       added_count=summary['downloaded'],
                                       skipped_count=summary['skipped'],
                                       error_summary=message)
            self.set_state(device_id, ERROR, message=message, code=code)
            summary['status'] = ERROR
            summary['error'] = message
            return summary
        finally:
            if driver is not None:
                try:
                    driver.close()
                except Exception as exc:
                    self.log.warning('driver close: %s', str(exc)[:60])

        status = PARTIAL if summary['failed'] else SUCCESS
        self.store.finish_sync_run(run_id, status, scanned_count=summary['scanned'],
                                   added_count=summary['downloaded'],
                                   skipped_count=summary['skipped'])
        self.set_state(device_id, status, message='', code='')
        summary['status'] = status
        return summary

    # ------------------------------------------------------------------ scan

    def scan_records(self, device, media):
        """Turn a camera listing into store rows, marking what is already archived."""
        known = {row['remote_id']: row for row in self.store.list_media(device['id'])}
        records = []
        for item in media:
            existing = known.get(item.id)
            local = self.existing_local_path(device, item, existing)
            records.append({
                'remote_id': item.id,
                'storage_id': item.storage or 'internal',
                'remote_path': item.remote_path,
                'local_path': local or '',
                'size_bytes': os.path.getsize(local) if local else item.bytes,
                'captured_at': (item.date + ' ' + item.time).strip(),
                'kind': item.kind,
                'status': 'complete' if local else 'pending',
            })
        return records

    def existing_local_path(self, device, item, existing=None):
        """Where this media already sits, honouring paths recorded before a relayout."""
        recorded = (existing or {}).get('local_path')
        if recorded and os.path.isfile(recorded):
            return recorded
        destination = self.destination_for(device, item)
        return destination if os.path.isfile(destination) else None

    def destination_for(self, device, item):
        date = archive.parse_capture_date((item.date + ' ' + item.time).strip(), item.name)
        return os.path.join(self.download_root,
                            archive.archive_relpath(device, item.storage, item.name, date))

    # -------------------------------------------------------------- downloads

    def drain_queue(self, device, driver, catalog, cancel, exclude_kinds, run_id, summary,
                    only_ids=None):
        device_id = device['id']
        while True:
            if cancel is not None and cancel.is_set():
                break
            entry = self.store.claim_next_media(device_id, exclude_kinds=exclude_kinds,
                                                include_ids=only_ids)
            if not entry:
                break
            item = catalog.get(entry['remote_id'])
            if item is None:
                # Queued earlier but no longer offered by the camera.
                self.store.fail_media(device_id, entry['remote_id'], message='相机上已不存在该文件',
                                      error_code='missing_on_camera', retryable=False)
                summary['failed'] += 1
                continue
            try:
                written = self.download_one(device, driver, item, cancel)
            except Exception as exc:
                if self.handle_download_error(device, entry, exc, run_id, summary):
                    break
                continue
            summary['downloaded'] += 1
            summary['bytes'] += written
            self.store.add_downloaded_bytes(run_id, written)

    def download_one(self, device, driver, item, cancel):
        destination = self.destination_for(device, item)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        expected = item.bytes if item.bytes_exact else None
        destination, conflict = archive.resolve_destination(destination, expected)
        if conflict:
            self.notify('同名文件内容不同，另存为 ' + os.path.basename(destination))
        self.ensure_free_space(destination, item.bytes)

        def progress(name, downloaded, total, speed):
            if self.on_progress:
                self.on_progress(device['id'], item, downloaded, total, speed)

        target = driver.open_download(item)
        self.download(target, destination, on_progress=progress, cancel=cancel)
        size = os.path.getsize(destination) if os.path.isfile(destination) else item.bytes
        self.store.complete_media(device['id'], item.id, destination, size)
        return size or 0

    def download(self, target, destination, on_progress=None, cancel=None):
        if self._download is None:
            from downloader import download_file
            self._download = download_file
        return self._download(target, destination, on_progress=on_progress, cancel=cancel)

    def ensure_free_space(self, destination, size_bytes):
        if not size_bytes:
            return
        try:
            free = shutil.disk_usage(os.path.dirname(destination)).free
        except OSError:
            return
        if free < size_bytes + FREE_SPACE_MARGIN:
            raise OSError(errno.ENOSPC, 'NAS 可用空间不足，无法下载该文件')

    def handle_download_error(self, device, entry, exc, run_id, summary):
        """Record one file failure; return True when the device run must stop."""
        device_id = device['id']
        code, retryable, device_scope = classify_error(exc)
        if code == CANCELLED:
            # A cancelled transfer keeps its .part and is not the file's fault,
            # so it goes straight back to the queue without burning an attempt.
            self.store.requeue_media(device_id, entry['remote_id'])
            return True
        attempts = entry.get('attempts', 0) + 1
        exhausted = attempts >= MAX_ATTEMPTS
        keep_trying = retryable and not exhausted
        message = self.record_error(device_id, exc, run_id=run_id, code=code,
                                    remote_id=entry['remote_id'], retryable=retryable)
        self.store.fail_media(device_id, entry['remote_id'], message=message, error_code=code,
                              next_attempt_at=retry_at(backoff_seconds(attempts)) if keep_trying else '',
                              retryable=keep_trying)
        summary['failed'] += 1
        if device_scope:
            self.set_state(device_id, ERROR, message=message, code=code)
            return True
        return False

    def record_error(self, device_id, exc, run_id=None, code='', remote_id='', retryable=True):
        message = (str(exc) or '同步失败')[:200]
        try:
            self.store.record_error(device_id, message, sync_run_id=run_id, remote_id=remote_id,
                                    error_code=code or FILE_ERROR, retryable=retryable)
        except Exception as store_exc:
            self.log.warning('record_error: %s', str(store_exc)[:60])
        self.log.warning('%s %s: %s', device_id, code, message)
        return message
