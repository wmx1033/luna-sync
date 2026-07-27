"""Luna Ultra adapter around the existing reverse-engineered transport."""

from camera_driver import CameraDriver, RemoteMedia
from luna_client import DEFAULT_HOST, LunaClient


class LunaUltraDriver(CameraDriver):
    id = 'luna_ultra'
    display_name = 'Luna Ultra'

    def __init__(self, host=DEFAULT_HOST):
        self.host = host
        self._client = LunaClient(host)

    def probe(self):
        self.connect()
        return {'driver': self.id, 'display_name': self.display_name, 'host': self.host}

    def connect(self):
        self._client.connect()

    def list_media(self):
        return [RemoteMedia.from_mapping(item) for item in self._client.list_files()]

    def open_download(self, media):
        if isinstance(media, RemoteMedia):
            return media.url
        return media['url']

    def close(self):
        self._client.close()
