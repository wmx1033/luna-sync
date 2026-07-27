"""Protocol-neutral contracts shared by all camera integrations."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping


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
class RemoteMedia:
    """A stable representation of an item exposed by a camera driver."""

    id: str
    name: str
    url: str
    date: str = ''
    time: str = ''
    size_text: str = ''
    bytes: int | None = None
    kind: str = 'FILE'
    storage: str = ''
    storage_label: str = ''
    bytes_exact: bool = False

    @classmethod
    def from_mapping(cls, item: Mapping[str, Any]):
        return cls(
            id=item.get('id') or item['name'],
            name=item['name'],
            url=item['url'],
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

    @abstractmethod
    def probe(self):
        """Return device metadata when the configured endpoint is reachable."""

    @abstractmethod
    def connect(self):
        """Open or refresh any session required before camera HTTP access."""

    @abstractmethod
    def list_media(self):
        """Return a list of :class:`RemoteMedia` objects."""

    @abstractmethod
    def open_download(self, media):
        """Return the downloadable URL or stream descriptor for a media item."""

    @abstractmethod
    def close(self):
        """Release protocol resources."""
