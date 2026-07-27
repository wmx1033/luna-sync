import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from camera_driver import RemoteMedia, file_kind
from driver_registry import available_drivers, create_driver
from drivers import luna_ultra


class FakeLunaClient:
    def __init__(self, host):
        self.host = host
        self.connected = False
        self.closed = False

    def connect(self):
        self.connected = True

    def close(self):
        self.closed = True

    def list_files(self):
        return [{
            'id': 'internal/VID_0001.mp4',
            'name': 'VID_0001.mp4',
            'url': 'http://camera/VID_0001.mp4',
            'storage': 'internal',
            'storage_label': '内置存储',
            'bytes': 123,
            'bytes_exact': True,
            'kind': 'MP4',
        }]


class DriverTests(unittest.TestCase):
    def test_luna_driver_adapts_transport_items_to_remote_media(self):
        with patch.object(luna_ultra, 'LunaClient', FakeLunaClient):
            driver = create_driver('luna_ultra', '192.168.42.1')
            self.assertEqual(driver.probe()['driver'], 'luna_ultra')
            media = driver.list_media()
            self.assertEqual(len(media), 1)
            self.assertIsInstance(media[0], RemoteMedia)
            self.assertEqual(media[0].storage, 'internal')
            self.assertEqual(driver.open_download(media[0]), 'http://camera/VID_0001.mp4')
            driver.close()
            self.assertTrue(driver._client.closed)

    def test_registry_rejects_unknown_driver(self):
        with self.assertRaisesRegex(ValueError, 'unsupported camera driver'):
            create_driver('not-a-camera', '192.168.42.1')

    def test_shared_media_kind_classifies_lrv_sidecars(self):
        self.assertIn('luna_ultra', available_drivers())
        self.assertEqual(file_kind('LRV_0001.lrv.3ainfo.bin'), 'LRV')


if __name__ == '__main__':
    unittest.main()
