import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import archive


DEVICE = {'id': 'luna-ultra-default', 'display_name': 'Luna Ultra'}


class CaptureDateTests(unittest.TestCase):
    def test_reads_the_camera_index_format(self):
        self.assertEqual(archive.parse_capture_date('10-Jul-2026 10:19'), ('2026', '07', '10'))

    def test_reads_the_timestamp_embedded_in_the_file_name(self):
        self.assertEqual(archive.parse_capture_date('VID_20260710_101942_062.mp4'),
                         ('2026', '07', '10'))

    def test_reads_an_iso_timestamp(self):
        self.assertEqual(archive.parse_capture_date('2026-07-10T10:19:42+00:00'),
                         ('2026', '07', '10'))

    def test_falls_back_to_a_modification_time(self):
        stamp = time.mktime((2026, 7, 10, 10, 19, 0, 0, 0, -1))
        self.assertEqual(archive.parse_capture_date('', stamp), ('2026', '07', '10'))

    def test_prefers_the_earlier_candidate(self):
        self.assertEqual(
            archive.parse_capture_date('10-Jul-2026', 'VID_20250101_000000.mp4'),
            ('2026', '07', '10'))

    def test_rejects_impossible_dates_and_unrelated_digits(self):
        self.assertIsNone(archive.parse_capture_date('VID_20261340_101942.mp4'))
        self.assertIsNone(archive.parse_capture_date('clip.mp4'))
        self.assertIsNone(archive.parse_capture_date('serial 123456789012345'))


class LayoutTests(unittest.TestCase):
    def test_path_groups_by_device_day_and_storage(self):
        self.assertEqual(
            archive.archive_relpath(DEVICE, 'external', 'VID_0001.mp4', ('2026', '07', '10')),
            os.path.join('luna-ultra-default', '2026', '07', '10', 'external', 'VID_0001.mp4'))

    def test_media_without_a_date_is_still_archived(self):
        self.assertEqual(
            archive.archive_relpath(DEVICE, 'internal', 'VID_0001.mp4'),
            os.path.join('luna-ultra-default', 'unknown-date', 'internal', 'VID_0001.mp4'))

    def test_device_folder_stays_readable_and_unique(self):
        self.assertEqual(archive.device_folder(DEVICE), 'luna-ultra-default')
        self.assertEqual(archive.device_folder({'id': 'abc123', 'display_name': 'Ace Pro 2'}),
                         'ace-pro-2-abc123')

    def test_same_name_from_two_devices_lands_in_separate_trees(self):
        other = {'id': 'go-ultra-1', 'display_name': 'GO Ultra'}
        date = ('2026', '07', '10')
        self.assertNotEqual(archive.archive_relpath(DEVICE, 'internal', 'VID_0001.mp4', date),
                            archive.archive_relpath(other, 'internal', 'VID_0001.mp4', date))


class DestinationTests(unittest.TestCase):
    def test_an_identical_file_is_reused_rather_than_duplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'VID_0001.mp4')
            with open(path, 'wb') as handle:
                handle.write(b'abcdef')
            self.assertEqual(archive.resolve_destination(path, 6), (path, False))

    def test_a_different_file_gets_a_deterministic_conflict_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'VID_0001.mp4')
            with open(path, 'wb') as handle:
                handle.write(b'abcdef')
            resolved, conflict = archive.resolve_destination(path, 99)
            self.assertTrue(conflict)
            self.assertEqual(resolved, os.path.join(tmp, 'VID_0001.conflict-1.mp4'))
            self.assertEqual(archive.resolve_destination(path, 99)[0], resolved)


class MigrationTests(unittest.TestCase):
    def build(self, tmp, files):
        for relative, payload in files.items():
            path = os.path.join(tmp, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as handle:
                handle.write(payload)
        return tmp

    def test_v1_layout_moves_into_the_device_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.build(tmp, {
                os.path.join('internal', 'VID_20260710_101942.mp4'): b'a' * 8,
                os.path.join('external', 'VID_20260711_101942.mp4'): b'b' * 8,
                'IMG_20260709_090000.jpg': b'c' * 4,
            })
            report = archive.migrate_legacy_archive(tmp, DEVICE)

            self.assertEqual({item['status'] for item in report}, {'moved'})
            self.assertTrue(os.path.isfile(os.path.join(
                tmp, 'luna-ultra-default', '2026', '07', '10', 'internal', 'VID_20260710_101942.mp4')))
            self.assertTrue(os.path.isfile(os.path.join(
                tmp, 'luna-ultra-default', '2026', '07', '11', 'external', 'VID_20260711_101942.mp4')))
            self.assertTrue(os.path.isfile(os.path.join(
                tmp, 'luna-ultra-default', '2026', '07', '09', 'internal', 'IMG_20260709_090000.jpg')))
            self.assertFalse(os.path.exists(os.path.join(tmp, 'internal')))
            self.assertFalse(os.path.exists(os.path.join(tmp, 'external')))

    def test_the_recorded_capture_time_wins_over_the_file_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.build(tmp, {os.path.join('internal', 'clip.mp4'): b'a' * 8})
            archive.migrate_legacy_archive(tmp, DEVICE,
                                           captured_at={'internal/clip.mp4': '10-Jul-2026 10:19'})
            self.assertTrue(os.path.isfile(os.path.join(
                tmp, 'luna-ultra-default', '2026', '07', '10', 'internal', 'clip.mp4')))

    def test_running_twice_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.build(tmp, {os.path.join('internal', 'VID_20260710_101942.mp4'): b'a' * 8})
            archive.migrate_legacy_archive(tmp, DEVICE)
            before = sorted(os.path.join(r, f) for r, _, fs in os.walk(tmp) for f in fs)
            self.assertEqual(archive.migrate_legacy_archive(tmp, DEVICE), [])
            after = sorted(os.path.join(r, f) for r, _, fs in os.walk(tmp) for f in fs)
            self.assertEqual(before, after)

    def test_a_resumable_fragment_moves_with_its_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.build(tmp, {os.path.join('internal', 'VID_20260710_101942.mp4.part'): b'ab'})
            archive.migrate_legacy_archive(tmp, DEVICE)
            self.assertTrue(os.path.isfile(os.path.join(
                tmp, 'luna-ultra-default', '2026', '07', '10', 'internal',
                'VID_20260710_101942.mp4.part')))

    def test_a_fragment_is_left_alone_when_the_media_is_already_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.build(tmp, {
                os.path.join('internal', 'VID_20260710_101942.mp4.part'): b'ab',
                os.path.join('luna-ultra-default', '2026', '07', '10', 'internal',
                             'VID_20260710_101942.mp4'): b'a' * 8,
            })
            report = archive.migrate_legacy_archive(tmp, DEVICE)
            self.assertEqual([item['status'] for item in report], ['duplicate'])
            self.assertTrue(os.path.isfile(
                os.path.join(tmp, 'internal', 'VID_20260710_101942.mp4.part')))

    def test_a_different_file_at_the_destination_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join('luna-ultra-default', '2026', '07', '10', 'internal',
                                  'VID_20260710_101942.mp4')
            self.build(tmp, {
                os.path.join('internal', 'VID_20260710_101942.mp4'): b'a' * 8,
                target: b'z' * 4,
            })
            report = archive.migrate_legacy_archive(tmp, DEVICE)

            self.assertEqual([item['status'] for item in report], ['conflict'])
            with open(os.path.join(tmp, target), 'rb') as handle:
                self.assertEqual(handle.read(), b'z' * 4)
            conflict = os.path.join(tmp, 'luna-ultra-default', '2026', '07', '10', 'internal',
                                    'VID_20260710_101942.conflict-1.mp4')
            with open(conflict, 'rb') as handle:
                self.assertEqual(handle.read(), b'a' * 8)


if __name__ == '__main__':
    unittest.main()
