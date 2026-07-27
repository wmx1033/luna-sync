import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from credentials import CredentialStore


class CredentialStoreTests(unittest.TestCase):
    def store(self, root):
        return CredentialStore(os.path.join(root, 'state', 'credentials.json'))

    def test_a_secret_round_trips_per_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            store.set('luna', 'first-secret')
            store.set('go-ultra', 'second-secret')

            self.assertEqual(store.get('luna'), 'first-secret')
            self.assertEqual(store.get('go-ultra'), 'second-secret')
            self.assertTrue(store.has('luna'))
            self.assertIsNone(store.get('missing'))
            self.assertFalse(store.has('missing'))

    def test_the_file_is_only_readable_by_its_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            store.set('luna', 'secret')
            mode = stat.S_IMODE(os.stat(store.path).st_mode)
            self.assertEqual(mode, 0o600)

    def test_an_empty_password_clears_rather_than_storing_blanks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            store.set('luna', 'secret')
            self.assertFalse(store.set('luna', ''))
            self.assertIsNone(store.get('luna'))
            with open(store.path) as handle:
                self.assertEqual(json.load(handle), {})

    def test_secrets_survive_reopening_and_can_be_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp).set('luna', 'secret')
            reopened = self.store(tmp)
            self.assertEqual(reopened.get('luna'), 'secret')
            self.assertTrue(reopened.delete('luna'))
            self.assertFalse(reopened.delete('luna'))
            self.assertIsNone(self.store(tmp).get('luna'))

    def test_pruning_drops_secrets_for_devices_that_are_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            store.set('luna', 'a')
            store.set('stale', 'b')
            self.assertEqual(store.prune(['luna']), ['stale'])
            self.assertEqual(store.device_ids(), ['luna'])

    def test_a_corrupt_file_does_not_take_the_service_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            os.makedirs(os.path.dirname(store.path), exist_ok=True)
            with open(store.path, 'w') as handle:
                handle.write('not json at all')
            self.assertIsNone(store.get('luna'))
            store.set('luna', 'recovered')
            self.assertEqual(store.get('luna'), 'recovered')


if __name__ == '__main__':
    unittest.main()
