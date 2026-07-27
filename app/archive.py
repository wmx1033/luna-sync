"""Archive layout: where a downloaded file finally lives.

The layout is ``{device}/{YYYY}/{MM}/{DD}/{storage}/{original_filename}`` so
that two cameras, two storage cards or two shooting days can never collide on
one name.  Nothing here rewrites file names or touches media content.
"""

import os
import re
import shutil
from datetime import datetime


UNKNOWN_DATE = 'unknown-date'
MONTHS = {name: index for index, name in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}
#: ``VID_20260710_101942_062.mp4`` and friends carry the capture day already.
FILENAME_DATE_RE = re.compile(r'(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)')
#: The camera directory index renders ``10-Jul-2026``.
INDEX_DATE_RE = re.compile(r'(?<!\d)(\d{1,2})-([A-Za-z]{3})-(20\d{2})(?!\d)')
ISO_DATE_RE = re.compile(r'(?<!\d)(20\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(?!\d)')
SLUG_RE = re.compile(r'[^a-z0-9]+')


def slugify(value):
    return SLUG_RE.sub('-', str(value or '').strip().lower()).strip('-')


def device_folder(device):
    """A stable per-device directory that stays readable for humans."""
    device_id = str(device.get('id') or '').strip()
    slug = slugify(device.get('display_name')) or 'camera'
    if not device_id:
        return slug
    # The id alone is already unique; only prefix it when it reads as opaque.
    if device_id.startswith(slug) or slug.startswith(device_id):
        return device_id
    return slug + '-' + device_id


def valid_date(year, month, day):
    try:
        datetime(int(year), int(month), int(day))
    except ValueError:
        return None
    return '%04d' % int(year), '%02d' % int(month), '%02d' % int(day)


def parse_capture_date(*candidates):
    """Return ``(YYYY, MM, DD)`` from the first candidate that yields a real date.

    Callers pass their sources best-first, for example the value recorded at
    scan time, then the file name, then the file modification time.
    """
    for candidate in candidates:
        if candidate is None or candidate == '':
            continue
        if isinstance(candidate, (int, float)):
            stamp = datetime.fromtimestamp(candidate)
            return '%04d' % stamp.year, '%02d' % stamp.month, '%02d' % stamp.day
        text = str(candidate)
        match = ISO_DATE_RE.search(text)
        if match:
            date = valid_date(*match.groups())
            if date:
                return date
        match = INDEX_DATE_RE.search(text)
        if match:
            day, month_name, year = match.groups()
            month = MONTHS.get(month_name.lower())
            if month:
                date = valid_date(year, month, day)
                if date:
                    return date
        match = FILENAME_DATE_RE.search(text)
        if match:
            date = valid_date(*match.groups())
            if date:
                return date
    return None


def archive_relpath(device, storage, filename, date=None):
    """Build the archive-relative path for one media file."""
    parts = [device_folder(device)]
    parts.extend(date if date else (UNKNOWN_DATE,))
    parts.append(slugify(storage) or 'internal')
    parts.append(os.path.basename(filename))
    return os.path.join(*parts)


def conflict_path(path, index):
    """Deterministic suffix so a clashing name never overwrites an existing file."""
    root, extension = os.path.splitext(path)
    return root + '.conflict-' + str(index) + extension


def same_file(path, size_bytes):
    return os.path.isfile(path) and (size_bytes is None or os.path.getsize(path) == size_bytes)


def resolve_destination(path, size_bytes=None, limit=100):
    """Pick a final path, reusing an identical file and never clobbering a different one.

    Returns ``(path, is_conflict)``.  A file matching the expected size is
    treated as the same media and reused.
    """
    if not os.path.exists(path) or same_file(path, size_bytes):
        return path, False
    for index in range(1, limit):
        candidate = conflict_path(path, index)
        if not os.path.exists(candidate) or same_file(candidate, size_bytes):
            return candidate, True
    raise OSError('too many archive conflicts for ' + path)


def move_into_place(source, destination):
    """Move within the archive, falling back to a copy across file systems."""
    os.makedirs(os.path.dirname(destination) or '.', exist_ok=True)
    try:
        os.replace(source, destination)
        return
    except OSError as exc:
        if getattr(exc, 'errno', None) != 18:  # EXDEV: different file systems
            raise
    staged = destination + '.part'
    shutil.copy2(source, staged)
    os.replace(staged, destination)
    os.remove(source)


def prune_empty_dirs(root, start):
    """Remove directories left behind by a move, never touching ``root`` itself."""
    root = os.path.abspath(root)
    current = os.path.abspath(start)
    while current.startswith(root + os.sep):
        try:
            os.rmdir(current)
        except OSError:
            return
        current = os.path.dirname(current)


def legacy_entries(root, device):
    """Yield files that still sit outside this device's archive folder.

    The v1 layout was ``{storage}/{name}`` (or a bare name in the very first
    releases), which is exactly how the media key is built, so the remote id can
    be reconstructed without contacting the camera.
    """
    device_dir = device_folder(device)
    if not os.path.isdir(root):
        return
    for current, _dirs, files in os.walk(root):
        relative = os.path.relpath(current, root)
        relative = '' if relative == '.' else relative
        top = relative.split(os.sep)[0] if relative else ''
        if top == device_dir:
            continue
        for name in sorted(files):
            partial = name.endswith('.part')
            base = name[:-len('.part')] if partial else name
            if not base:
                continue
            storage = top if top in ('internal', 'external') else 'internal'
            yield {
                'remote_id': storage + '/' + base,
                'source': os.path.join(current, name),
                'storage': storage,
                'filename': base,
                'partial': partial,
            }


def migrate_legacy_archive(root, device, captured_at=None):
    """Move a v1 archive into the current layout.

    Moves inside one file system are atomic renames, so an interrupted run
    leaves every file either at its old or its new path and simply continues on
    the next start.  Nothing is ever deleted: a destination that already holds
    an equally sized file is reported instead of being overwritten.
    """
    captured_at = captured_at or {}
    report = []
    for entry in list(legacy_entries(root, device)):
        source = entry['source']
        if not os.path.isfile(source):
            continue
        try:
            size = os.path.getsize(source)
            date = parse_capture_date(captured_at.get(entry['remote_id']), entry['filename'],
                                      os.path.getmtime(source))
            relative = archive_relpath(device, entry['storage'], entry['filename'], date)
            final = os.path.join(root, relative)
            if entry['partial']:
                # A fragment only matters while its final file is still missing;
                # moving one next to a finished download would risk truncating it.
                if os.path.exists(final):
                    report.append(dict(entry, destination=final, status='duplicate'))
                    continue
                destination, conflict = final + '.part', False
            else:
                destination, conflict = resolve_destination(final, size)
            if os.path.abspath(destination) == os.path.abspath(source):
                continue
            if os.path.exists(destination):
                report.append(dict(entry, destination=destination, status='duplicate'))
                continue
            move_into_place(source, destination)
            prune_empty_dirs(root, os.path.dirname(source))
            report.append(dict(entry, destination=destination,
                               status='conflict' if conflict else 'moved'))
        except OSError as exc:
            report.append(dict(entry, destination='', status='failed', error=str(exc)))
    return report
