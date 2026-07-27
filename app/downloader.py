import os, re, time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

USER_AGENT = 'LunaDL/0.1'

def resolve_target(target):
    """Accept either a plain URL or a driver supplied download target."""
    url = getattr(target, 'url', None)
    if url is None:
        return str(target), {}, True
    headers = dict(getattr(target, 'headers', None) or {})
    return url, headers, bool(getattr(target, 'supports_range', True))

def build_headers(extra, **overrides):
    headers = {'User-Agent': USER_AGENT}
    headers.update(extra)
    headers.update(overrides)
    return headers

def probe_total(url, extra):
    try:
        r = urlopen(Request(url, headers=build_headers(extra, Range='bytes=0-0')), timeout=10)
        cr = r.headers.get('Content-Range')
        r.close()
        if cr:
            m = re.search(r'/(\d+)', cr)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None

def download_file(target, dest, on_progress=None, cancel=None, chunk=262144):
    url, extra, supports_range = resolve_target(target)
    os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
    partial = dest + '.part'
    name = os.path.basename(dest)

    if os.path.exists(dest) and not os.path.exists(partial):
        sz = os.path.getsize(dest)
        if on_progress:
            on_progress(name, sz, sz, 0)
        return dest

    total = probe_total(url, extra) if supports_range else None

    existing = os.path.getsize(partial) if os.path.exists(partial) else 0
    if existing and not supports_range:
        # The driver cannot resume, so a leftover fragment can only be restarted.
        os.remove(partial)
        existing = 0
    if total and existing >= total:
        os.replace(partial, dest)
        if on_progress:
            on_progress(name, total, total, 0)
        return dest

    headers = build_headers(extra)
    if existing > 0:
        headers['Range'] = 'bytes=' + str(existing) + '-'
    try:
        resp = urlopen(Request(url, headers=headers), timeout=30)
    except HTTPError as e:
        if existing > 0 and e.code == 416:
            if total and existing >= total:
                os.replace(partial, dest)
                return dest
            os.remove(partial)
            existing = 0
            resp = urlopen(Request(url, headers=build_headers(extra)), timeout=30)
        else:
            raise

    status = getattr(resp, 'status', 200)
    if existing > 0 and status == 200:
        os.remove(partial)
        existing = 0
    if total is None:
        cl = resp.headers.get('Content-Length')
        if cl:
            total = existing + int(cl) if status == 206 else int(cl)

    mode = 'ab' if existing > 0 else 'wb'
    dl = existing
    started = time.monotonic()
    last = 0.0
    with resp:
        with open(partial, mode) as f:
            while True:
                if cancel and cancel.is_set():
                    raise Exception('cancelled')
                ch = resp.read(chunk)
                if not ch:
                    break
                f.write(ch)
                dl += len(ch)
                now = time.monotonic()
                if on_progress and (now - last > 0.1 or (total and dl >= total)):
                    on_progress(name, dl, total, (dl - existing) / max(now - started, 0.001))
                    last = now
                if total and dl >= total:
                    break
    if total and dl < total:
        raise OSError('incomplete ' + str(dl) + '/' + str(total))
    os.replace(partial, dest)
    if on_progress:
        on_progress(name, dl, total or dl, 0)
    return dest
