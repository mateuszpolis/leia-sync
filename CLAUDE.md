# leia-sync

Read-only mirror of a student's LeIA (PW Moodle) courses. One script: `leia_sync.py`.

- Run: `uv run leia_sync.py`. Check after any edit: `uv run leia_sync.py --selftest`.
- Before changing how pages are fetched or parsed, read `docs/platform.md`.
- All requests go through `Client._send`; keep it that way. Never weaken `DENY` or add
  non-read methods to `AJAX_READ_ONLY`: a wrong request can start a test or submit work.
- No course ids or names in code: other students use this repo.
- Output goes to `data_dir` from `config.toml`. Only `leia/` folders and the root generated
  files are written; `work/` folders are the student's.
- Guidance for agents working with the mirrored data is `templates/CLAUDE.md`, installed
  into the data folder on each run.
- `config.toml`, state and reports are gitignored. Never commit credentials or course content.
