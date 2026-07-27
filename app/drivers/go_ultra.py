"""Insta360 GO Ultra driver.

The camera splits its two jobs across two ports, and the driver follows suit:

* listing goes over the binary session on TCP 6666 (protobuf ``GetFileList``),
* downloading is plain HTTP on port 80 and needs no session at all, which is
  why transfers can use the shared resumable downloader unchanged.

Verified on firmware v1.6.25.  Nothing here is shared with the Luna Ultra
driver: the handshake, the paths and the media layout were all confirmed
against a real GO Ultra.
"""

import re
from concurrent.futures import ThreadPoolExecutor

from camera_driver import (
    CameraDriver,
    DownloadTarget,
    DriverCapabilities,
    DriverError,
    DriverProtocolError,
    DriverUnreachableError,
    ProbeResult,
    RemoteMedia,
    StorageSource,
    file_kind,
)
from downloader import probe_total
from drivers.insta360_protocol import (
    Insta360Session,
    ProtocolError,
    field_values,
    first_value,
    parse_fields,
    repeated_varint_field,
    text_of,
    varint_field,
)

DEFAULT_HOST = '192.168.42.1'
CMD_GET_OPTIONS = 8
CMD_GET_FILE_LIST = 13

#: ``GetFileList`` media types, established on a real device: 0 video, 1 photo,
#: 2 everything.
MEDIA_ALL = 2
PAGE_SIZE = 200
MAX_ITEMS = 20000

#: Option ids double as response field numbers, so asking for exactly these
#: four keeps the camera from ever sending its Wi-Fi password (field 36) back.
OPTION_SERIAL = 15
OPTION_UUID = 16
OPTION_FIRMWARE = 30
OPTION_MODEL = 48
IDENTITY_OPTIONS = (OPTION_SERIAL, OPTION_UUID, OPTION_FIRMWARE, OPTION_MODEL)

FILE_LIST_PATHS_FIELD = 1
FILE_LIST_TOTAL_FIELD = 2
OPTIONS_BODY_FIELD = 2

#: ``VID_20251116_044541_001.mp4`` carries the capture moment in its name.
NAME_TIMESTAMP_RE = re.compile(r'_(\d{8})_(\d{6})(?:_|\.)')
SIZE_PROBE_WORKERS = 8


def capture_moment(name):
    """Return ``(YYYY-MM-DD, HH:MM:SS)`` when the file name carries a timestamp."""
    match = NAME_TIMESTAMP_RE.search(name)
    if not match:
        return '', ''
    day, clock = match.group(1), match.group(2)
    return ('%s-%s-%s' % (day[:4], day[4:6], day[6:]),
            '%s:%s:%s' % (clock[:2], clock[2:4], clock[4:]))


def storage_of(remote_path):
    """Derive the storage id from the camera path, e.g. /DCIM/Camera01/x -> camera01.

    Reading it from the path rather than hardcoding one means an extra card or a
    renamed folder shows up as its own storage instead of colliding.
    """
    parts = [part for part in remote_path.split('/') if part]
    return parts[-2].lower() if len(parts) >= 2 else 'internal'


class GoUltraDriver(CameraDriver):
    id = 'go_ultra'
    display_name = 'GO Ultra'
    #: Reachability is judged on the control port, which is what listing needs.
    probe_port = 6666
    capabilities = DriverCapabilities(
        supports_range=True,
        supports_resume=True,
        reports_exact_size=True,
        requires_session=True,
        storages=(StorageSource('camera01', '内置存储'),),
    )

    def __init__(self, host=DEFAULT_HOST):
        self.host = host
        self._session = None

    # -- session -----------------------------------------------------------

    def connect(self):
        if self._session is None:
            self._session = Insta360Session(self.host)
        try:
            self._session.open()
        except ProtocolError as exc:
            self._session = None
            raise DriverProtocolError('GO Ultra 握手失败: ' + str(exc)) from exc
        except OSError as exc:
            self._session = None
            raise DriverUnreachableError('GO Ultra 无法连接: ' + str(exc)) from exc

    def close(self):
        if self._session is not None:
            self._session.close()
            self._session = None

    def _command(self, code, body=b''):
        if self._session is None:
            self.connect()
        try:
            response = self._session.command(code, body)
        except ProtocolError as exc:
            raise DriverProtocolError('GO Ultra 通信失败: ' + str(exc)) from exc
        except OSError as exc:
            raise DriverUnreachableError('GO Ultra 连接中断: ' + str(exc)) from exc
        if not response.ok:
            raise DriverProtocolError('GO Ultra 命令 %d 返回 %d' % (code, response.code))
        return response

    # -- identity ----------------------------------------------------------

    def identity(self):
        """Model, firmware and serial. The Wi-Fi password is never requested."""
        response = self._command(CMD_GET_OPTIONS,
                                 repeated_varint_field(1, IDENTITY_OPTIONS))
        body = first_value(response.fields(), OPTIONS_BODY_FIELD, b'')
        options = parse_fields(body) if body else []

        def option_text(number):
            return text_of(first_value(options, number, b'')) or ''

        return {
            'model': option_text(OPTION_MODEL),
            'firmware': option_text(OPTION_FIRMWARE),
            'serial': option_text(OPTION_SERIAL),
            'uuid': option_text(OPTION_UUID),
        }

    def probe(self):
        self.connect()
        details = self.identity()
        return ProbeResult(
            driver=self.id,
            display_name=self.display_name,
            host=self.host,
            reachable=True,
            model=details['model'] or self.display_name,
            identifier=details['serial'] or details['uuid'] or self.host,
            storages=self.capabilities.storages,
            media_count=len(self.remote_paths()),
            capabilities=self.capabilities,
        )

    # -- listing -----------------------------------------------------------

    def remote_paths(self):
        """Walk the paginated file list; the camera reports the total up front."""
        paths, start = [], 0
        while start < MAX_ITEMS:
            body = (varint_field(1, MEDIA_ALL) + varint_field(2, start)
                    + varint_field(3, PAGE_SIZE))
            fields = self._command(CMD_GET_FILE_LIST, body).fields()
            page = [text_of(raw) for raw in field_values(fields, FILE_LIST_PATHS_FIELD)]
            page = [path for path in page if path]
            paths.extend(page)
            total = first_value(fields, FILE_LIST_TOTAL_FIELD)
            if len(page) < PAGE_SIZE or (total is not None and len(paths) >= total):
                break
            start += len(page)
        return paths

    def list_media(self):
        media = [self._media_for(path) for path in self.remote_paths()]
        self._fill_sizes(media)
        return media

    def _media_for(self, remote_path):
        name = remote_path.rsplit('/', 1)[-1]
        storage = storage_of(remote_path)
        date, clock = capture_moment(name)
        return RemoteMedia(
            id=storage + '/' + name,
            name=name,
            url='http://%s%s' % (self.host, remote_path),
            path=remote_path,
            date=date,
            time=clock,
            kind=file_kind(name),
            storage=storage,
            storage_label='内置存储',
        )

    def _fill_sizes(self, media):
        """The listing has no sizes, so ask HTTP for each Content-Length."""
        if not media:
            return media
        workers = min(SIZE_PROBE_WORKERS, len(media))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            sizes = list(pool.map(lambda item: probe_total(item.url, {}), media))
        for index, size in enumerate(sizes):
            if size is not None:
                media[index] = replace_size(media[index], size)
        return media

    # -- download ----------------------------------------------------------

    def open_download(self, media, offset=0):
        url = media.url if isinstance(media, RemoteMedia) else media['url']
        return DownloadTarget(url=url, offset=max(0, int(offset or 0)),
                              supports_range=self.capabilities.supports_range)


def replace_size(media, size):
    return RemoteMedia(
        id=media.id, name=media.name, url=media.url, path=media.path, date=media.date,
        time=media.time, size_text=media.size_text, bytes=size, kind=media.kind,
        storage=media.storage, storage_label=media.storage_label, bytes_exact=True)


__all__ = ['GoUltraDriver', 'DriverError', 'capture_moment', 'storage_of']
