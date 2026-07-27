"""Protocol-neutral contracts shared by all camera integrations.

A driver translates one vendor protocol into the types defined here.  Drivers
never touch the sync queue, the archive layout or the web state; the
orchestration layers own those concerns and only speak this vocabulary.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping


class DriverError(Exception):
    """Base class for every failure a driver is expected to raise."""

    code = 'driver_error'
    retryable = True


class DriverUnreachableError(DriverError):
    """The camera did not answer on its control endpoint."""

    code = 'camera_unreachable'


class DriverAuthError(DriverError):
    """The camera refused the session handshake."""

    code = 'camera_auth_failed'
    retryable = False


class DriverProtocolError(DriverError):
    """The camera answered with something this driver cannot parse."""

    code = 'camera_protocol_error'


def is_lrv_name(name):
    lower = str(name or '').lower()
    return lower.startswith('lrv_') or lower.endswith('.lrv') or '.lrv.' in lower


def file_kind(name):
    if is_lrv_name(name):
        return 'LRV'
    extension = name.rsplit('.', 1)[-1].upper() if '.' in name else ''
    if extension in ('MP4', 'LRV', 'MOV', 'JPG', 'JPEG', 'PNG', 'WEBP', 'GIF', 'LIV', 'INSP'):
        return extension
    return extension or 'FILE'


@dataclass(frozen=True)
class StorageSource:
    """One enumerable media origin on a camera, such as internal flash."""

    id: str
    label: str = ''

    def as_dict(self):
        return {'id': self.id, 'label': self.label or self.id}


@dataclass(frozen=True)
class DriverCapabilities:
    """What a driver can actually do, so callers never assume vendor behaviour."""

    supports_range: bool = False
    supports_resume: bool = False
    reports_exact_size: bool = False
    requires_session: bool = False
    storages: tuple = ()

    def as_dict(self):
        return {
            'supports_range': self.supports_range,
            'supports_resume': self.supports_resume,
            'reports_exact_size': self.reports_exact_size,
            'requires_session': self.requires_session,
            'storages': [storage.as_dict() for storage in self.storages],
        }


@dataclass(frozen=True)
class DownloadTarget:
    """Everything the generic downloader needs to fetch one media item."""

    url: str
    offset: int = 0
    headers: Mapping[str, str] = field(default_factory=dict)
    supports_range: bool = True

    def request_headers(self):
        headers = dict(self.headers)
        if self.offset and self.supports_range:
            headers['Range'] = 'bytes=' + str(self.offset) + '-'
        return headers


@dataclass(frozen=True)
class ProbeResult:
    """Answer to "is this device the one I think it is, and what can it give me?"."""

    driver: str
    display_name: str
    host: str
    reachable: bool = False
    model: str = ''
    identifier: str = ''
    storages: tuple = ()
    media_count: int | None = None
    capabilities: DriverCapabilities | None = None

    def as_dict(self):
        return {
            'driver': self.driver,
            'display_name': self.display_name,
            'host': self.host,
            'reachable': self.reachable,
            'model': self.model,
            'identifier': self.identifier,
            'storages': [storage.as_dict() for storage in self.storages],
            'media_count': self.media_count,
            'capabilities': self.capabilities.as_dict() if self.capabilities else None,
        }


@dataclass(frozen=True)
class RemoteMedia:
    """A stable representation of an item exposed by a camera driver."""

    id: str
    name: str
    url: str
    path: str = ''
    date: str = ''
    time: str = ''
    size_text: str = ''
    bytes: int | None = None
    kind: str = 'FILE'
    storage: str = ''
    storage_label: str = ''
    bytes_exact: bool = False

    @property
    def remote_path(self):
        """The device-side location, which forms part of the stable identity."""
        return self.path or self.name

    def stable_id(self, device_id):
        """Identity per PRD 6.2: device, storage and remote path together."""
        return '/'.join((str(device_id), self.storage or 'internal', self.remote_path))

    @classmethod
    def from_mapping(cls, item: Mapping[str, Any]):
        return cls(
            id=item.get('id') or item['name'],
            name=item['name'],
            url=item['url'],
            path=item.get('path') or item.get('href') or '',
            date=item.get('date', ''),
            time=item.get('time', ''),
            size_text=item.get('size_text', ''),
            bytes=item.get('bytes'),
            kind=item.get('kind') or file_kind(item['name']),
            storage=item.get('storage', ''),
            storage_label=item.get('storage_label', ''),
            bytes_exact=bool(item.get('bytes_exact', False)),
        )

    def as_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'url': self.url,
            'path': self.remote_path,
            'date': self.date,
            'time': self.time,
            'size_text': self.size_text,
            'bytes': self.bytes,
            'kind': self.kind,
            'storage': self.storage,
            'storage_label': self.storage_label,
            'bytes_exact': self.bytes_exact,
        }


class CameraDriver(ABC):
    id = ''
    display_name = ''
    capabilities = DriverCapabilities()
    #: Port that must answer for the camera to be considered reachable.  The
    #: Wi-Fi layer probes this without knowing anything else about the protocol.
    probe_port = 80

    def endpoint(self):
        return self.host, self.probe_port

    @abstractmethod
    def probe(self) -> ProbeResult:
        """Return device metadata when the configured endpoint is reachable."""

    @abstractmethod
    def connect(self):
        """Open or refresh any session required before camera media access."""

    @abstractmethod
    def list_media(self):
        """Return a list of :class:`RemoteMedia` objects."""

    @abstractmethod
    def open_download(self, media, offset: int = 0) -> DownloadTarget:
        """Return a :class:`DownloadTarget` for the given media item."""

    @abstractmethod
    def close(self):
        """Release protocol resources."""
