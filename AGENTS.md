# AGENTS.md

CLI (`blink-sync-sentry`) that lists, inspects, and cleans **local-storage** clips on
Blink Sync Modules via `blinkpy`. Source lives in `src/blink_sync_sentry/`, tests in
`tests/`, canonical config example in `examples/config.yaml`.

## Setup and commands

- Python >= 3.11. No lockfile or committed venv; install dev deps with
  `pip install -e '.[dev]'` (adds pytest, pytest-asyncio, pytest-cov, ruff).
- Full suite: `pytest` (`testpaths=["tests"]`, `asyncio_mode="auto"`).
- Single test: `pytest tests/test_storage.py::TestFilterByAge::test_some_expired`
- Lint: `ruff check .` (ruff configured for py311, line length 100).
- No CI, typecheck, or codegen is configured.
- All tests are mocked; no Blink account, network, or credentials are needed. Do not
  make tests call the real API.

## Safety invariants (do not break)

- Dry-run is the default. `run_cleanup(force=False)` must never delete; only `--force`
  enables deletion, and `watch` refuses to run without it.
- Only local-storage clips are ever touched; cloud clips are out of scope.
- If archiving a clip fails, that clip must be skipped, not deleted (`cleanup.py`).
- Exit codes are a contract: `0` success, `1` error, `2` dry-run would-have-deleted.
  Scripts rely on `2`, so keep it stable.

## Architecture notes

- `cli.py` parses args, builds config, then dispatches to `_cmd_list`, `_cmd_status`,
  `_cmd_clean`, or `_cmd_watch`; all Blink work is async under `asyncio.run`.
- Config precedence is defaults < YAML < env < CLI, implemented in
  `config.build_config`. Flat single-account YAML is normalized into a one-item
  `accounts` list; use `AppConfig.effective_retention` / `effective_archive_dir` /
  `effective_storage_capacity` for per-account fallback behavior.
- Credentials come from env only (`BLINK_USERNAME`/`BLINK_PASSWORD`, or
  `BLINK_<ACCOUNT>_*`); never put them in YAML. `auth.create_blink` loads the token
  file first, prompts for 2FA unless `--no-prompt`, and saves tokens with `0600`.
  `watch` / `--no-prompt` require a pre-existing token from an interactive run.
- Retention policies are a union: when both `retention_days` and `max_usage_gb` are
  set, a clip is deleted if **either** matches (`storage.select_clips_for_deletion`).
- `storage.py` and `cleanup.py` depend on blinkpy internals: `sync._local_storage`,
  `sync.update_local_storage_manifest()`, and clip methods
  `prepare_download`/`download_video`/`delete_video`. `clip.size` is a string, so cast
  with `int()` before arithmetic. Check the installed blinkpy version before changing
  these calls.
- `output.py` supports plain text, `--json`, and optional `rich`. JSON must stay
  machine-parseable on stdout; logs go to stderr.

## Conventions

- Any new destructive path needs a `force` guard and tests proving dry-run is a no-op.
- Follow existing test style: build clips with `MagicMock` plus `AsyncMock` for async
  methods, and pass `now=` to time-dependent filters instead of sleeping or freezing time.
- When adding config keys, update `examples/config.yaml` and the README.
- `__version__` is hardcoded in `src/blink_sync_sentry/__init__.py`; bump it there
  (setuptools-scm is listed as a build requirement but is not configured).
