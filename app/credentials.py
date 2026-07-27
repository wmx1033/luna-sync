"""Per-device connection secrets, deliberately kept out of the sync database.

The sync store holds everything a user may safely read back; Wi-Fi passwords
live here instead, in a single owner-only file that no API ever echoes.  Keeping
them apart means a database copied out for debugging carries no credentials.
"""

import json
import os
import tempfile

FILE_MODE = 0o600


class CredentialStore:
    def __init__(self, path):
        self.path = os.fspath(path)

    def _load(self):
        try:
            with open(self.path) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data):
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        # Write and rename so a crash can never leave a half-written secret file.
        handle = tempfile.NamedTemporaryFile('w', dir=directory, delete=False)
        try:
            with handle:
                json.dump(data, handle)
            os.chmod(handle.name, FILE_MODE)
            os.replace(handle.name, self.path)
        except Exception:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise
        try:
            os.chmod(self.path, FILE_MODE)
        except OSError:
            pass

    def get(self, device_id):
        entry = self._load().get(str(device_id))
        if isinstance(entry, dict):
            return entry.get('password') or None
        return entry or None

    def has(self, device_id):
        return bool(self.get(device_id))

    def set(self, device_id, password):
        """Store a password; an empty value clears it rather than storing blanks."""
        data = self._load()
        key = str(device_id)
        if password:
            data[key] = {'password': password}
        else:
            data.pop(key, None)
        self._save(data)
        return bool(password)

    def delete(self, device_id):
        data = self._load()
        if data.pop(str(device_id), None) is None:
            return False
        self._save(data)
        return True

    def device_ids(self):
        return sorted(self._load())

    def prune(self, keep_ids):
        """Drop secrets for devices that no longer exist."""
        data = self._load()
        keep = {str(device_id) for device_id in keep_ids}
        removed = [key for key in data if key not in keep]
        if removed:
            for key in removed:
                data.pop(key)
            self._save(data)
        return removed
