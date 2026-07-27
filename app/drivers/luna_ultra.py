"""Luna Ultra adapter around the existing reverse-engineered transport."""

from camera_driver import (
    CameraDriver,
    DownloadTarget,
    DriverCapabilities,
    DriverProtocolError,
    DriverUnreachableError,
    ProbeResult,
    RemoteMedia,
    StorageSource,
)
from luna_client import DEFAULT_HOST, STORAGE_ROOTS, LunaClient


STORAGES = tuple(StorageSource(root['id'], root['label']) for root in STORAGE_ROOTS)


class LunaUltraDriver(CameraDriver):
    id = 'luna_ultra'
    display_name = 'Luna Ultra'
    probe_port = 80
    capabilities = DriverCapabilities(
        supports_range=True,
        supports_resume=True,
        reports_exact_size=True,
        requires_session=True,
        storages=STORAGES,
    )

    def __init__(self, host=DEFAULT_HOST):
        self.host = host
        self._client = LunaClient(host)

    def probe(self):
        self.connect()
        media = self.list_media()
        return ProbeResult(
            driver=self.id,
            display_name=self.display_name,
            host=self.host,
            reachable=True,
            model=self.display_name,
            identifier=self.host,
            storages=STORAGES,
            media_count=len(media),
            capabilities=self.capabilities,
        )

    def connect(self):
        try:
            self._client.connect()
        except OSError as exc:
            raise DriverUnreachableError('Luna 认证会话建立失败: ' + str(exc)) from exc

    def list_media(self):
        try:
            items = self._client.list_files()
        except OSError as exc:
            raise DriverUnreachableError('Luna 文件目录不可访问: ' + str(exc)) from exc
        try:
            return [RemoteMedia.from_mapping(item) for item in items]
        except (KeyError, TypeError) as exc:
            raise DriverProtocolError('Luna 目录条目无法解析: ' + str(exc)) from exc

    def open_download(self, media, offset=0):
        url = media.url if isinstance(media, RemoteMedia) else media['url']
        return DownloadTarget(
            url=url,
            offset=max(0, int(offset or 0)),
            supports_range=self.capabilities.supports_range,
        )

    def close(self):
        self._client.close()
