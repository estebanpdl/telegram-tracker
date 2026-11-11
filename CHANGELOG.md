# Changelog

All notable changes to Telegram Tracker are listed here.

## Unreleased

### Fixed

- A root-only output or data path no longer falls back to the current directory. `--output /` collected into the current directory, and `--output D:/` into the current directory of drive D:. `collect --output`, `download-media --output` and the builders' `--data-path` now refuse a filesystem root with exit code `2`, before anything is read or collected; choose a subfolder such as `/data/telegram`. On Windows, a trailing `\` is dropped like a trailing `/`.

## [1.0.0] - 2026-11-03

The first release. It turns the earlier collection and builder scripts into one command-line tool with resumable collection, optional SQLite storage, account profiles, media downloads and a URL dataset. Earlier checkouts upgrade in place: the earlier commands, folders, credentials and sessions keep working.

Licensed under Apache-2.0 ([LICENSE](LICENSE)).

### CLI and entry points

- One entry point with a subcommand per step: `collect`, `build-dataset`, `build-network`, `build-urls`, `download-media` and `auth`. Run it as `python main.py`, or install the project (`python -m pip install .`) to get the `tgtracker` command and `python -m telegram_tracker`. All three take the same arguments and give the same outputs and exit codes.
- The earlier forms still work and reach the same code: `python main.py --telegram-channel NAME` / `--batch-file FILE`, `python build-datasets.py` and `python channels-to-network.py`. They have no removal date.
- Documented exit codes for every command: `0` success, `1` a failed target, run or check, `2` invalid arguments or credentials, `130` Ctrl+C.
- Help never needs credentials, a session or the service libraries.
- Requires Python 3.14. Dependencies are pinned in `requirements.txt`.

### Collection

- Channels are given as a username, `@username`, a `t.me` link or a numeric ID. Batch files are checked line by line before anything connects; repeated targets are dropped.
- A missing, private or inaccessible channel fails only that channel; the others are still collected.
- History windows: `--min-id`, `--max-id`, `--since`, `--until` and `--max-messages`, combined as AND. A filtered run asks before replacing saved histories (`--yes` to skip the question).
- `--update` collects only posts newer than the saved history and merges them in, in every storage mode.
- Batches collect up to 3 channels at once by default (`--concurrency`, 1–10). Output is written in input order, so it does not depend on which channel finished first.
- One shared request policy paces every channel: a minimum gap between requests (`--request-interval`, default 0.25 s), flood waits sat out up to `--max-flood-wait` (default 60 s) and paused for every channel, and transient errors retried with backoff (`--max-retries`, default 3).
- `collection-status.json` records each run's state, parameters, profile, request statistics and per-target outcome. The output folder is locked while a run writes, and the builders refuse a folder whose last run is still running or was interrupted.
- One collection per Telegram account at a time, across profiles and session files in the same application home.
- The run summary lists the messages saved per channel (and the new ones after `--update`).

### Storage modes and builders

- `collect --storage files|sqlite|both`: the files as before, an SQLite database (`telegram-tracker.sqlite3`) instead, or both. A folder keeps the mode it was created with. The database schema is documented in `docs/sqlite-schema.md`, with example queries.
- The database is checked before connecting (identity, schema version, the folder it belongs to). A database of another version, another SQLite file, or a moved or copied folder is refused and left untouched.
- `build-dataset` and `build-network` read the files or the database (`--source auto|files|sqlite`) and never fall back to the other copy. Each build writes a record (`dataset-build.json`, `network-build.json`) of what it read.
- In `both` mode the files are written exactly as `files` mode writes them. If a run stops between writing the two copies, the folder is blocked from further collection until you recover it (see the README).

### Auth and profiles

- `auth login` logs in once and saves the app credentials to `credentials.json` in the application home (`~/.telegram_tracker`), protected by file permissions. The session lives there too, so commands work from any folder. Two-step verification is supported.
- `auth status` (offline; `--check` connects), `auth logout` and `auth clear-credentials`. Status output names sources, never values.
- Credentials come from environment variables, `--config PATH`, the saved `credentials.json` or the earlier `./config/config.ini`, in that order. An existing `./session_file.session` is still used, with a notice.
- Named profiles (`--profile NAME` or `TELEGRAM_TRACKER_PROFILE`), each with its own credentials, session and account binding. `auth profiles` lists them.
- `auth rename-profile OLD NEW` and `auth delete-profile NAME`, both with `--dry-run`. A renamed profile keeps its ID, so its histories continue under the new name. Deleting a profile is local only and does not log the session out on Telegram.
- The collecting account's own phone number is removed from collected data.

### Media download

- Off by default. `collect --include-media` downloads the files of the messages a run covered; `download-media` downloads the files of an existing folder, in any storage mode.
- Files go to `<output>/downloaded-media/<channel_id>/{images,videos,audios,files}/`, one copy per channel and content, verified with SHA-256 and recorded in a media ledger (`media.sqlite3`).
- `--media-types`, `--max-media-size` and `--max-media-total` limit a run; there is no size limit by default.
- `download-media --dry-run` shows what a run would request; `download-media --check` compares the ledger, the stored files and the saved messages without changing anything.
- Completed files are never deleted. A partly downloaded file is removed, and `download-media` tries failed and skipped files again.

### URL dataset

- `build-urls` lists the URLs in collected message text, hyperlink targets and link previews, with their registered domains. It writes `urls_dataset.csv`, `urls.sqlite3` (with per-domain and per-channel counts) and `urls-build.json`.
- Nothing is fetched: links are not opened or expanded, and the public suffix list is the one bundled with `tldextract`.

### Cross-OS support

- Tested on Windows 11, and on Windows Server 2025, Ubuntu 24.04 and macOS 15 in CI, including the installed commands, file permissions, locks and interruption.
- Saved credentials and the application home are readable only by your account (mode 0700/0600 on Linux and macOS, a private access list on Windows).
- Ctrl+C stops a run cleanly with exit 130; completed channels are kept.

### Known limits

- The test suite runs offline, with synthetic data and a fake Telegram client. Named profiles and media downloads have not yet been run against Telegram.
- The request defaults are project limits; the rates Telegram accepts for an account were not measured.
- Linux and macOS are tested only on the CI runners as a regular user. Other systems, network file systems, runs as root, and Ctrl+C sent to a Windows console are untested.
- A `sqlite` or `both` folder that was moved or copied can't be collected into (no rebind command yet); the builders still read it.
- `build-dataset` appends to `msgs_dataset.csv`, so building twice duplicates rows.
- Read from a database, a renamed channel's old-name history is not built, and rebuilding the collected chats slows down as a folder gathers many runs.
- `build-urls` doesn't read URL buttons, and it checks no link against the web.
- A single channel downloads one file at a time.
- An output file open in another program (such as Excel) makes a build fail with a traceback.
- The version recorded in a database or media ledger comes from the installed package: `python main.py` records the version of an installed copy, or `unknown` when the project isn't installed.
