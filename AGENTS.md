# AGENTS.md

## Cursor Cloud specific instructions

This repo is **Insta360 Sync**, a single Python 3 / Flask app (no `package.json`, no root
`requirements.txt`). The web app entrypoint is `app/web_app.py`; production uses `entrypoint.sh`
+ Docker, but for development you run the Flask app directly.

### Dependencies
- Runtime deps are `flask`, `pillow` (imported as `PIL`), and the `ffmpeg` binary. `ffmpeg` is
  already present on the base image; `flask`/`pillow` are installed by the update script (into
  the user site via `pip`). `flask` is only required for `tests/test_web_app.py` (those tests
  auto-skip when Flask is missing).

### Lint / syntax check
- `python3 -m compileall -q app tests` (this repo's only "lint"; matches CI in
  `.github/workflows/test.yml`).

### Tests
- Full suite: `python3 -m unittest discover -s tests -p 'test_*.py' -v` (128 tests, no camera or
  network needed — drivers/Wi-Fi are mocked).

### Running the web app (dev)
- Do NOT run Wi-Fi/hardware management in the cloud VM. Always run with `wifi_backend: none`.
- The app reads config from `$LUNA_CONFIG` (see `config.example.json` for the schema). `config.json`,
  `downloads/`, and `state/` are gitignored — create a local `config.json` with `"wifi_backend": "none"`
  and point `download_dir`/`state_dir` at repo-local dirs.
- Start it (bind to loopback in the VM):
  ```bash
  mkdir -p downloads state
  LUNA_CONFIG=/workspace/config.json LUNA_WIFI_BACKEND=none LUNA_BIND_HOST=127.0.0.1 \
    python3 app/web_app.py
  ```
- Serves on `http://127.0.0.1:8765`. Env overrides: `LUNA_WEB_PORT`, `LUNA_BIND_HOST`,
  `DOWNLOAD_DIR`, `STATE_DIR`, `LUNA_WIFI_BACKEND`.

### Non-obvious gotchas
- Full media sync/download needs a physical Insta360 camera on Wi-Fi + a wireless NIC, which the
  cloud VM does not have. Device management (add/edit/delete/list devices, backed by SQLite at
  `state/sync.db`), the local media library, thumbnails and transcode previews all work without a
  camera and are the way to exercise the app here.
- The Flask dev server (`app.run`) has no reloader enabled; restart the process after code changes.
- `state/sync.db` persists devices across restarts; delete the `state/` dir to reset local state.
