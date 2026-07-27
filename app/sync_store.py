"""SQLite persistence for device configuration and sync history.

The current web application still reads its single-camera configuration from
``config.json``.  This module creates a durable, multi-device representation
without changing that runtime path; later milestones will move the sync engine
to this store.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


LEGACY_LUNA_DEVICE_ID = 'luna-ultra-default'
MIGRATIONS = {}


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def config_text(value):
    text = str(value or '').strip()
    return '' if text.upper().startswith('YOUR_') else text


class SyncStore:
    """A small, connection-per-operation SQLite store suitable for web workers."""

    def __init__(self, path):
        self.path = os.fspath(path)

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        with self._connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)')
            applied = {
                row['version'] for row in conn.execute('SELECT version FROM schema_migrations')
            }
            unknown_versions = applied.difference(MIGRATIONS)
            if unknown_versions:
                raise RuntimeError(
                    'database schema is newer than this application: %s' % max(unknown_versions)
                )
            for version in sorted(MIGRATIONS):
                if version not in applied:
                    MIGRATIONS[version](conn)
                    conn.execute('INSERT INTO schema_migrations(version) VALUES (?)', (version,))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _create_schema(conn):
        conn.executescript(
            '''
            CREATE TABLE devices (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                driver TEXT NOT NULL,
                camera_host TEXT NOT NULL,
                ssid TEXT NOT NULL DEFAULT '',
                credential_ref TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL DEFAULT 100,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                archive_root TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                remote_id TEXT NOT NULL,
                storage_id TEXT NOT NULL DEFAULT '',
                remote_path TEXT NOT NULL,
                local_path TEXT NOT NULL DEFAULT '',
                size_bytes INTEGER,
                captured_at TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'FILE',
                status TEXT NOT NULL DEFAULT 'pending',
                completed_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (device_id, remote_id)
            );

            CREATE TABLE sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                scanned_count INTEGER NOT NULL DEFAULT 0,
                added_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                error_summary TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE sync_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sync_run_id INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL,
                device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                remote_id TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL,
                retryable INTEGER NOT NULL DEFAULT 1 CHECK (retryable IN (0, 1)),
                created_at TEXT NOT NULL
            );

            CREATE INDEX media_device_status_idx ON media(device_id, status);
            CREATE INDEX sync_runs_device_started_idx ON sync_runs(device_id, started_at DESC);
            CREATE INDEX sync_errors_device_created_idx ON sync_errors(device_id, created_at DESC);
            '''
        )

    def migrate_legacy_config(self, config):
        """Create the default Luna device once, without storing its password in SQLite."""
        self.initialize()
        now = utc_now()
        values = {
            'id': LEGACY_LUNA_DEVICE_ID,
            'display_name': config_text(config.get('device_name')) or 'Luna Ultra',
            'driver': 'luna_ultra',
            'camera_host': config_text(config.get('camera_host')) or '192.168.42.1',
            'ssid': config_text(config.get('camera_ssid')),
            'credential_ref': 'legacy-config' if config_text(config.get('camera_password')) else '',
            'priority': int(config.get('device_priority', 100)),
            'enabled': 1,
            'archive_root': config_text(config.get('download_dir')),
            'created_at': now,
            'updated_at': now,
        }
        with self._connection() as conn:
            conn.execute(
                '''
                INSERT INTO devices (
                    id, display_name, driver, camera_host, ssid, credential_ref,
                    priority, enabled, archive_root, created_at, updated_at
                ) VALUES (
                    :id, :display_name, :driver, :camera_host, :ssid, :credential_ref,
                    :priority, :enabled, :archive_root, :created_at, :updated_at
                )
                ON CONFLICT(id) DO NOTHING
                ''',
                values,
            )
        return self.get_device(LEGACY_LUNA_DEVICE_ID)

    def upsert_device(self, device):
        self.initialize()
        now = utc_now()
        values = {
            'id': device['id'],
            'display_name': device['display_name'],
            'driver': device['driver'],
            'camera_host': device['camera_host'],
            'ssid': device.get('ssid', ''),
            'credential_ref': device.get('credential_ref', ''),
            'priority': int(device.get('priority', 100)),
            'enabled': int(bool(device.get('enabled', True))),
            'archive_root': device.get('archive_root', ''),
            'created_at': now,
            'updated_at': now,
        }
        with self._connection() as conn:
            conn.execute(
                '''
                INSERT INTO devices (
                    id, display_name, driver, camera_host, ssid, credential_ref,
                    priority, enabled, archive_root, created_at, updated_at
                ) VALUES (
                    :id, :display_name, :driver, :camera_host, :ssid, :credential_ref,
                    :priority, :enabled, :archive_root, :created_at, :updated_at
                )
                ON CONFLICT(id) DO UPDATE SET
                    display_name = excluded.display_name,
                    driver = excluded.driver,
                    camera_host = excluded.camera_host,
                    ssid = excluded.ssid,
                    credential_ref = excluded.credential_ref,
                    priority = excluded.priority,
                    enabled = excluded.enabled,
                    archive_root = excluded.archive_root,
                    updated_at = excluded.updated_at
                ''',
                values,
            )
        return self.get_device(values['id'])

    def get_device(self, device_id):
        self.initialize()
        with self._connection() as conn:
            row = conn.execute('SELECT * FROM devices WHERE id = ?', (device_id,)).fetchone()
        return dict(row) if row else None

    def list_devices(self):
        self.initialize()
        with self._connection() as conn:
            rows = conn.execute('SELECT * FROM devices ORDER BY priority, display_name, id').fetchall()
        return [dict(row) for row in rows]

    def record_media(self, media):
        self.initialize()
        now = utc_now()
        values = {
            'device_id': media['device_id'],
            'remote_id': media['remote_id'],
            'storage_id': media.get('storage_id', ''),
            'remote_path': media['remote_path'],
            'local_path': media.get('local_path', ''),
            'size_bytes': media.get('size_bytes'),
            'captured_at': media.get('captured_at', ''),
            'kind': media.get('kind', 'FILE'),
            'status': media.get('status', 'pending'),
            'completed_at': media.get('completed_at', ''),
            'created_at': now,
            'updated_at': now,
        }
        with self._connection() as conn:
            conn.execute(
                '''
                INSERT INTO media (
                    device_id, remote_id, storage_id, remote_path, local_path, size_bytes,
                    captured_at, kind, status, completed_at, created_at, updated_at
                ) VALUES (
                    :device_id, :remote_id, :storage_id, :remote_path, :local_path, :size_bytes,
                    :captured_at, :kind, :status, :completed_at, :created_at, :updated_at
                )
                ON CONFLICT(device_id, remote_id) DO UPDATE SET
                    storage_id = excluded.storage_id,
                    remote_path = excluded.remote_path,
                    local_path = excluded.local_path,
                    size_bytes = excluded.size_bytes,
                    captured_at = excluded.captured_at,
                    kind = excluded.kind,
                    status = excluded.status,
                    completed_at = excluded.completed_at,
                    updated_at = excluded.updated_at
                ''',
                values,
            )
        return self.get_media(values['device_id'], values['remote_id'])

    def get_media(self, device_id, remote_id):
        self.initialize()
        with self._connection() as conn:
            row = conn.execute(
                'SELECT * FROM media WHERE device_id = ? AND remote_id = ?', (device_id, remote_id)
            ).fetchone()
        return dict(row) if row else None

    def start_sync_run(self, device_id):
        self.initialize()
        with self._connection() as conn:
            cursor = conn.execute(
                'INSERT INTO sync_runs(device_id, status, started_at) VALUES (?, ?, ?)',
                (device_id, 'running', utc_now()),
            )
            run_id = cursor.lastrowid
        return self.get_sync_run(run_id)

    def finish_sync_run(self, run_id, status, scanned_count=0, added_count=0, skipped_count=0,
                        downloaded_bytes=0, error_summary=''):
        self.initialize()
        with self._connection() as conn:
            conn.execute(
                '''
                UPDATE sync_runs
                SET status = ?, finished_at = ?, scanned_count = ?, added_count = ?,
                    skipped_count = ?, downloaded_bytes = ?, error_summary = ?
                WHERE id = ?
                ''',
                (status, utc_now(), scanned_count, added_count, skipped_count, downloaded_bytes,
                 error_summary, run_id),
            )
        return self.get_sync_run(run_id)

    def get_sync_run(self, run_id):
        self.initialize()
        with self._connection() as conn:
            row = conn.execute('SELECT * FROM sync_runs WHERE id = ?', (run_id,)).fetchone()
        return dict(row) if row else None

    def record_error(self, device_id, message, sync_run_id=None, remote_id='', error_code='', retryable=True):
        self.initialize()
        with self._connection() as conn:
            cursor = conn.execute(
                '''
                INSERT INTO sync_errors(
                    sync_run_id, device_id, remote_id, error_code, message, retryable, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (sync_run_id, device_id, remote_id, error_code, message, int(bool(retryable)), utc_now()),
            )
            error_id = cursor.lastrowid
            row = conn.execute('SELECT * FROM sync_errors WHERE id = ?', (error_id,)).fetchone()
        return dict(row)


MIGRATIONS[1] = SyncStore._create_schema
