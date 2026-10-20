<div align="center">

# **Telegram Tracker**: `a Python-based open-source tool for Telegram`

---

[![GitHub forks](https://img.shields.io/github/forks/estebanpdl/telegram-tracker.svg?style=social&label=Fork&maxAge=2592000)](https://GitHub.com/estebanpdl/telegram-tracker/network/)
[![GitHub stars](https://img.shields.io/github/stars/estebanpdl/telegram-tracker?style=social)](https://github.com/estebanpdl/telegram-tracker/stargazers)
[![Open Source](https://badges.frapsoft.com/os/v1/open-source.svg?v=103)](https://twitter.com/estebanpdl)
[![Made-with-python](https://img.shields.io/badge/Made%20with-Python-1f425f.svg)](https://www.python.org/)
[![Twitter estebanpdl](https://badgen.net/badge/icon/twitter?icon=twitter&label)](https://twitter.com/estebanpdl)

---
</div>

## Overview

Telegram Tracker collects public Telegram channels through Telegram's API and turns the collected files into a messages dataset and a channel network.

- `collect` saves each channel's profile metadata and posts as JSON, plus aggregate files about the chats found in that data (e.g., forward sources). You can collect one channel or a list of channels from a text file. The same data can also go to an SQLite database, instead of the files or beside them (see [Storage modes](#storage-modes---storage)).
- `build-dataset` reads the collected files and writes `msgs_dataset.csv` and `collected_chats.xlsx`.
- `build-network` reads `collected_chats.csv` and writes a directed graph, `Graph.gexf`, and a preview image, `network.png`.

The two builders run offline: they read local files only and never connect to Telegram.

Contents:

- [Requirements](#requirements)
- [Installation and setup](#installation-and-setup)
- [Logging in and saved credentials](#logging-in-and-saved-credentials)
- [Account profiles](#account-profiles)
- [First login](#first-login)
- [Commands](#commands)
- [Collecting channels](#collecting-channels)
- [Collecting part of a history](#collecting-part-of-a-history)
- [Updating saved histories](#updating-saved-histories---update)
- [Batches, concurrency, and rate limits](#batches-concurrency-and-rate-limits)
- [Output files](#output-files)
- [Storage modes](#storage-modes---storage)
- [Reading the database](#reading-the-database)
- [Database backups and schema versions](#database-backups-and-schema-versions)
- [Collection status, locking, and recovery](#collection-status-locking-and-recovery)
- [Building the dataset and the network](#building-the-dataset-and-the-network)
- [Known limits](#known-limits)
- [Running the tests](#running-the-tests)

## Requirements

- [Python 3.14](https://www.python.org/). The project is tested with Python 3.14.7. Pinned library versions and the tested environment are in [docs/compatibility.md](docs/compatibility.md).
- [Telegram API credentials](https://my.telegram.org/auth?to=apps): a Telegram account, plus an app `api_id` and `api_hash`.
- The libraries in `requirements.txt`: [Telethon](https://docs.telethon.dev/en/stable/) 1.x, [pandas](https://pandas.pydata.org/), [openpyxl](https://openpyxl.readthedocs.io/en/stable/), [tqdm](https://tqdm.github.io/), [NetworkX](https://networkx.org/), [Matplotlib](https://matplotlib.org/), and [python-louvain](https://github.com/taynaud/python-louvain) for community detection.

## Installation and setup

**1. Get the code.** Clone the repository:

```
git clone https://github.com/estebanpdl/telegram-tracker.git
cd telegram-tracker
```

Or download the ZIP file from GitHub and unpack it.

**2. Create a Python 3.14 environment and install the requirements.** Using an isolated environment keeps the pinned versions apart from other projects. With conda or mamba:

```
conda create -n telegram-tracker python=3.14
conda activate telegram-tracker
python -m pip install -r requirements.txt
python -m pip check
```

Any other Python 3.14 virtual environment works the same way: activate it, then run the two `python -m pip` commands. `pip check` should report `No broken requirements found.` Always install with `python -m pip` from the activated environment, so packages go into that environment.

**3. Log in once.** Get an API ID and API hash at my.telegram.org, then run:

```
python main.py auth login
```

It asks for the API ID, the API hash (not shown while you type), and your phone number, logs in to Telegram, and saves the three values so that you never type them again (see [Logging in and saved credentials](#logging-in-and-saved-credentials)). If you prefer, fill in `config/config.ini` instead, as earlier versions required:

```ini
[Telegram API credentials]
api_id = <your api_id>
api_hash = <your api_hash>
phone = <your phone number, international format, e.g. +<country code><number>>
```

Replace each `<...>` value, without the angle brackets. Keep these values private and do not commit them.

**4. Where files go.** The saved credentials and the Telegram session live in the application home, `~/.telegram_tracker` (on Windows `%USERPROFILE%\.telegram_tracker`), so after logging in you can run commands from any folder. `config/config.ini` is read from the working directory when nothing is saved. Output goes to `./output/data` in the working directory unless you pass `--output`.

**5. Optional: install the `tgtracker` command.** The scripts above work without installing the project. To also get a `tgtracker` command, install the project into the activated environment from the repository folder:

```
python -m pip install .
```

`tgtracker` (and `python -m telegram_tracker`) take the same arguments as `python main.py` and use the same application home, saved credentials, and session. `./config/config.ini` and `./output/data` are relative to the folder you run them from. The installed command is a copy of the code: after updating the repository, run `python -m pip install .` again.

## Logging in and saved credentials

### `auth login`

`auth login` logs in to Telegram without collecting anything. It uses the session described under [Which session is used](#which-session-is-used) and the app credentials described under [Where the app credentials come from](#where-the-app-credentials-come-from), and asks for any that are missing. Before asking, it says where typed values will be saved. If the session is already logged in, Telegram asks for no code or password; the app credentials are still needed to connect.

After a successful login:

- When all three values were typed and nothing was saved before, they are saved to `credentials.json` in the application home. `--no-save` skips saving.
- Values from the environment or an INI file are saved only if you answer `y` (default no). Without a terminal they are not saved; `--save` saves them without asking.
- Different values already saved are replaced only if you agree, or with `--save`.
- A failed or interrupted login saves nothing. If the login succeeds but saving fails, the command exits 1 and says so; the session stays logged in.

While a complete set is saved, `auth login` does not ask for app credentials. A login code or two-step verification password may still be needed if the selected session is not authorized. To change the saved values, run `auth clear-credentials` and then `auth login`, or run `auth login` with the new values in the environment or in `--config` and agree to replace the saved ones. The complete new set is saved.

### Where the app credentials come from

The first source that provides a value wins:

| Order | Source | Notes |
|---|---|---|
| 1 | `TELEGRAM_TRACKER_API_ID`, `TELEGRAM_TRACKER_API_HASH`, `TELEGRAM_TRACKER_PHONE` | Each variable overrides one value. |
| 2 | `--config PATH` | An INI file with the `[Telegram API credentials]` section. A missing file is an error. |
| 3 | `credentials.json` of the profile (the application home for the default profile) | Saved by `auth login`. |
| 4 | `./config/config.ini` in the working directory | The earlier setup; default profile only. |
| 5 | Prompts | `auth login` only. |

Only one file source (2, 3, or 4) is used, as a whole: the first one present. A value missing from it is not taken from another file. Template values (`api_id = api_id`, `<your api_id>`, or empty) count as missing, so the placeholder `config/config.ini` in the repository never hides saved credentials. An invalid value (for example an `api_id` that is not a positive number) stops the command with exit 2; it never falls back to another source. There are no command-line options for the API ID, hash, or phone, and none for the login code or password.

`collect` uses the same sources but never asks for app credentials: with values missing it exits 2 and names the ways to provide them.

### Which session is used

For the default profile, the first match wins (a named profile always uses its own session; see [Account profiles](#account-profiles)):

1. `--session PATH` (`collect` and `auth`).
2. `TELEGRAM_TRACKER_SESSION`.
3. `session_file.session` in the application home, if it exists.
4. `./session_file.session` in the working directory, if it exists. Earlier versions created the session there. The command prints a one-line notice when it uses it.
5. Otherwise `session_file.session` in the application home, created by the first login.

Nothing is moved or copied automatically. Once a session exists in the application home, it is used even when an older `./session_file.session` exists. A path without the exact lowercase `.session` suffix gets it, matching Telethon. Symlinks and directory junctions in the path are resolved so those aliases share a lock; a resolved filename without that exact suffix is refused before connecting. Hard links are not identified as aliases. The session file must not be inside the `--output` folder. The `--output` folder must neither be inside the application home nor contain it.

### Checking, logging out, and removing saved credentials

- `auth status` shows the application home, the selected session, the saved credentials file, and where each value comes from: source names only, never values. It works offline and creates nothing, so a present session file does not prove that it is still logged in. `auth status --check` connects to Telegram to find out (exit 0 logged in, 1 not); it never asks for a code and never creates a session. Telethon may update the session file while checking.
- `auth logout` asks for confirmation (`--yes` skips it), then logs the session out on Telegram. That revokes it and deletes the session file. Saved credentials stay. A session that is not logged in, or no session at all, is left as it is.
- `auth clear-credentials` asks for confirmation (`--yes` skips it), then deletes `credentials.json` only. It does not log the session out or touch environment variables, INI files, or collected data, and it removes a damaged file without reading it.

### How saved credentials are protected

`credentials.json` holds exactly `schema_version`, `api_id`, `api_hash`, and `phone`. It is plain text protected by file permissions, not encryption: the application home and its files are readable only by your account (mode 0700/0600 on Linux and macOS; on Windows an access list that grants only your account, with inheritance from the parent folder disabled). Administrators of the computer can still read them. A session created in the application home gets the same protection. The login code and two-step verification password are never saved. The session's authorization key is stored in the session file, separate from `credentials.json`.

Saving writes a protected new file next to the old one and replaces it in one step. Failures before replacement leave the earlier file intact; a failed protection check after replacement reports an error with the new file already in place. If the application home cannot be created or protected, `collect` stops with exit 1 before connecting; set `TELEGRAM_TRACKER_HOME` to another absolute folder. `auth login` also stops when it needs to write there. A default-profile login with an external session and `--no-save` can proceed without a profile record. A `credentials.json` that is damaged, of another version, or readable by other accounts stops every command that needs it with exit 2; remove it with `auth clear-credentials` and log in again.

`TELEGRAM_TRACKER_HOME` replaces the application home for all of this (it must be an absolute path).

## Account profiles

A profile is a named set of saved credentials and a session, so one installation can collect with more than one Telegram account. Everything above describes the **default** profile, which is the application home itself: its `credentials.json`, its session (chosen as under [Which session is used](#which-session-is-used)), and nothing else. Existing setups are the default profile without any change or migration.

A **named** profile lives in `<application home>/profiles/<name>/` and holds its own `credentials.json`, `session_file.session`, and `profile.json`. Select it with `--profile NAME` (`collect` and `auth login`, `status`, `logout`, or `clear-credentials`) or the `TELEGRAM_TRACKER_PROFILE` variable; the option wins, and without either the default profile is used. The earlier collection form takes the profile from the variable only. Nothing remembers a selection between commands.

```
python main.py auth login --profile research     # creates the profile
python main.py collect --profile research --telegram-channel example_channel -o ./output/research
python main.py auth profiles                      # lists the profiles
```

- **Names.** A lowercase letter, then up to 31 lowercase letters, digits, `_`, or `-`. `default` selects the default profile. Windows device names (`con`, `prn`, `aux`, `nul`, `com1`-`com9`, `lpt1`-`lpt9`) are refused, and so is a profile folder that is a link or junction or whose name differs only in case. Invalid names stop with exit 2 before any file is touched.
- **Creating.** Only `auth login --profile NAME` creates a named profile record, after login and account verification. Errors before recording can leave folders or session files; a later credential-save failure can leave the profile record in place. Other single-profile commands stop with exit 2 for a profile that does not exist. `auth profiles` inventories all profiles without selecting one.
- **Separate sources.** A named profile uses only its own session and refuses `--session` and `TELEGRAM_TRACKER_SESSION` (exit 2; unset the variable or use the default profile). Its credentials come from the `TELEGRAM_TRACKER_API_*` variables, `--config PATH`, or its own `credentials.json`, never from `./config/config.ini`, `./session_file.session`, or another profile's files. A profile name is a local label: it does not prove that two profiles use different Telegram accounts.
- **`profile.json`.** It holds the profile's name, a random profile ID (created once and never changed), and the Telegram account the profile is bound to. Credentials are not in it. The default profile gets its `profile.json` at its next successful `auth login`; until then its profile ID is unknown. `credentials.json` keeps its format in every profile, so earlier versions still read the default profile's credentials.
- **Account binding.** After logging in, `auth login` asks Telegram which account the session belongs to and records it in `profile.json` the first time. A later login or collection through the same profile with a session of another account stops with exit 1 (`auth logout --profile NAME`, then log in with the bound account, or create a new profile); the binding is never replaced. `auth login` also stops when the session belongs to another account than the phone number in use. Only the profile's own session binds an account: a default-profile login or collection with `--session` or `TELEGRAM_TRACKER_SESSION` checks the account but leaves the binding as it is, and such a collection with another account is recorded without the profile ID. The verified account ID is kept as private metadata in `profile.json`; it is not added to diagnostics or collection provenance. Collected payloads can independently contain the same user ID.
- **Logout and clear.** `auth logout --profile NAME` revokes only that profile's session. `auth clear-credentials --profile NAME` removes only that profile's `credentials.json`; its `profile.json` and session stay, so a later login keeps the same profile ID and binding. For a named profile, clearing credentials takes its session lease and stops with exit 1 if that profile is collecting or logging in. Clearing the default profile uses the credentials-file lease only.
- **`auth profiles`** lists the default profile and every named profile with whether each has a profile record, saved credentials, and a session file. It reads no values, works offline, and creates nothing.
- Profile rename, delete, or copy commands do not exist; profile folders are not removed automatically.

**One collection per account.** Before its first channel request, every collection asks Telegram which account it is logged in to (one extra `get_me` request, counted under `account_checks` in `collection-status.json` and outside the request controls) and holds a lease for that account in `<application home>/account-leases/` until it ends. A second collection with the same account, through another profile or another session file, stops before its first channel request with exit 1 and `The Telegram account is in use by another process (pid ..., started ..., profile ...)`. Collections with different accounts can run at the same time, each into its own `--output` folder; each collection keeps its own request controls. Every collection therefore needs a writable application home, even with credentials and session from elsewhere; otherwise it stops with exit 1 before connecting. The coordination covers one application home: the same account used from a second home or another program at the same time is not detected. A lease left by a crashed run is not removed automatically: make sure no collection is running (check the process ID), then delete the `.lock` file named in the message.

## First login

The first login creates `session_file.session` in the application home. Later runs reuse that session and skip every prompt. `auth login`, the canonical `collect`, and the earlier form (`python main.py --telegram-channel ...`) log in the same way.

1. Run `python main.py auth login`, or any collection, for example `python main.py collect --telegram-channel example_channel`.
2. The tool prints `> Not Authorized! Sending code request...` and Telegram sends a login code to your Telegram app.
3. At `Enter the code: `, type the code.
4. If the account has two-step verification, the tool prints `> Two-step verification is enabled.` and asks `Enter your two-step verification password: `. The password is not shown while you type.
5. `> Authorized!` means the session is ready, and the collection continues.

Rules and failure cases:

- **Attempts.** You get at most 3 attempts for the code and 3 for the password. An empty entry counts as an attempt. After the third rejected attempt, the run exits 1.
- **Expired code.** The run stops at once (exit 1). Run the command again to request a new code.
- **Rate limit.** If Telegram rate-limits the code request or the sign-in, the run stops at once with `> Login failed: Telegram rate-limited the login; wait N seconds before trying again.` It does not retry automatically. Wait at least that long before you run it again.
- **Other refusals.** Exit 1 with the Telegram error name. Messages never include the phone number, code, or password.
- **Interrupting.** Ctrl+C at a prompt exits 130. In every case the connection to Telegram is closed once before the program exits.
- **Password handling.** The password is never a command-line option. The tool does not write it to a file or log, and keeps it only for the sign-in request. Never put it in `config.ini`, a script, or your shell history.
- **A real terminal is required.** The prompts read from the terminal. If input is unavailable, the run exits 1 with `No login code was entered because input is unavailable; run the command from an interactive terminal.` The password prompt refuses to fall back to visible input. Activate the environment (`conda activate telegram-tracker`) and run `python main.py ...` directly. `conda run` does not attach the terminal's input by default, so do not use it for the first login.

**Protect the session file.** `session_file.session` gives full access to your Telegram account. Do not share, copy, or commit it (it is listed in `.gitignore`). One command at a time uses a session: while `collect` or an `auth` command holds it, another one stops at once (exit 1) with `The session is in use by another process`, even from another output folder. If no other command is running, an interrupted one left `session_file.session.lock` behind; delete that file and run the command again.

## Commands

`main.py` is the entry point, with one subcommand per step:

```
python main.py collect --telegram-channel NAME [options]
python main.py collect --batch-file FILE [options]
python main.py build-dataset [--data-path DIR]
python main.py build-network [--data-path DIR]
python main.py auth login | status | logout | clear-credentials | profiles
```

Run `python main.py --help` or `python main.py <command> --help` for the options. Help never needs credentials, a session, or the service libraries. `python main.py` without arguments prints the overview and exits 2.

With the project installed (step 5 above), `tgtracker` and `python -m telegram_tracker` can replace `python main.py` in every example in this README, including the earlier collection form: arguments, outputs, and exit codes are the same. Only the program name shown in help and error messages differs.

### Earlier commands

The earlier commands still work and reach the same code. They are supported through the first release, with no removal date.

| Earlier command | Canonical command | Notes |
|---|---|---|
| `python main.py --telegram-channel NAME` | `python main.py collect --telegram-channel NAME` | |
| `python main.py --batch-file FILE` | `python main.py collect --batch-file FILE` | Collects up to 3 channels at once, like `collect` |
| `... --limit-download-to-channel-metadata` | `python main.py collect ... --limit-download-to-channel-metadata` | |
| `... --output DIR` / `-o DIR` | `python main.py collect ... --output DIR` | Default `./output/data`, relative to the working directory |
| `... --min-id N` | `python main.py collect ... --min-id N` | Same meaning: only messages with an id above N |
| (none) | `python main.py collect ... --since/--until/--max-id/--max-messages` | History window: **`collect` only** |
| (none) | `python main.py collect ... --update` | Merge new posts into saved histories: **`collect` only** |
| (none) | `python main.py collect ... --concurrency/--request-interval/--max-flood-wait/--max-retries` | Request controls: **`collect` only**. The earlier form uses the defaults |
| (none) | `python main.py collect ... --storage files\|sqlite\|both` | Storage mode: **`collect` only**. The earlier form writes files and refuses a `sqlite` or `both` folder |
| `python build-datasets.py [--data-path DIR]` | `python main.py build-dataset [--data-path DIR]` | `-d` also works |
| `python channels-to-network.py [--data-path DIR]` | `python main.py build-network [--data-path DIR]` | `-d` also works |

The earlier collection form accepts only `--telegram-channel`, `--batch-file`, `--limit-download-to-channel-metadata`, `--output`, and `--min-id`. Its help (`python main.py --telegram-channel x --help`) lists `--min-id` under "history window".

### Exit codes

The same for every form:

| Code | Meaning |
|---|---|
| `0` | Every requested target was collected, the build succeeded, or the `auth` action completed (including "nothing to do"). Empty channels and empty inputs count as success. |
| `1` | A target failed, the login failed, the run stopped early (flood wait or rejected session), the output folder, its database, the session, or the Telegram account is in use, a `sqlite` or `both` folder is blocked or its database cannot be used, the session belongs to another account than the profile or the phone number, writing an output or the credentials failed, the application home cannot be created, an `auth` confirmation was declined, `auth status --check` found the session logged out, the input data of a build is missing or malformed, or a library is missing. |
| `2` | Invalid arguments, including an unreadable batch file or an unsupported line in it, a `--storage` mode the folder cannot take (or the earlier collection form on a `sqlite` or `both` folder), an invalid or unknown profile, `--session` or `TELEGRAM_TRACKER_SESSION` with a named profile, and missing or invalid app credentials (before any connection). |
| `130` | Interrupted with Ctrl+C. |

## Collecting channels

```
# one channel
python main.py collect --telegram-channel example_channel

# every channel listed in a text file, into a chosen folder
python main.py collect --batch-file ./channels.txt --output ./output/project-a

# profile metadata only, without posts
python main.py collect --batch-file ./channels.txt --limit-download-to-channel-metadata
```

`--output` (or `-o`) chooses the folder; it is created if needed. A channel is given by its username, `@username`, a public link (`https://t.me/username`, `t.me/s/username`), or a numeric id. Links and `@` are reduced to the username, which also names the channel's folder: `@Example_Channel` and `https://t.me/Example_Channel` are both saved under `Example_Channel/`.

Each requested channel is collected separately. A channel that does not exist, is private, or cannot be accessed fails only that channel: it is listed in `_exceptions-channels-not-found.txt`, the other channels are still collected, and the exit code is 1. At the end, the run prints a summary of the targets and requests, and names the failed channels on stderr.

**Metadata only** saves `<channel>.json` and the aggregate files, but no new `<channel>_messages.json`. A messages file from an earlier run stays in place; the summary and `collection-status.json` (`stale_history`) name those channels. In a `sqlite` or `both` folder, the messages stored in the database stay in place the same way.

## Collecting part of a history

`collect` accepts five history window options. They combine as AND:

| Option | Selects |
|---|---|
| `--min-id N` | messages with an id above N (exclusive) |
| `--max-id N` | messages with an id below N (exclusive) |
| `--since DATE` | messages dated at or after DATE (inclusive) |
| `--until DATE` | messages dated before DATE (exclusive) |
| `--max-messages N` | at most N messages per channel, newest first |

Dates are ISO 8601: `2026-10-01`, `2026-10-01T12:00`, `2026-10-01T12:00:00Z`, or `2026-10-01T14:00+02:00`. A value without an offset is UTC, and a date alone means midnight UTC. The earlier collection form accepts only `--min-id`.

```
# September 2026 only, into a fresh folder
python main.py collect --telegram-channel example_channel --since 2026-09-01 --until 2026-10-01 -o ./output/sep-2026

# the newest 500 messages of each channel in a batch
python main.py collect --batch-file ./channels.txt --max-messages 500 -o ./output/sample

# messages after id 12345
python main.py collect --telegram-channel example_channel --min-id 12345 -o ./output/new-posts
```

**A filtered run replaces `<channel>_messages.json` with the selected messages only.** It does not merge them into the saved history. Before connecting, the command warns on stderr when a channel already has a saved history in the output folder, naming up to 10 channels. To keep earlier histories, write filtered runs to a fresh `--output`, or use `--update` to add new posts. The run header prints the active window. Window options cannot be combined with `--limit-download-to-channel-metadata` in `collect`.

In a `sqlite` or `both` folder, a filtered run also replaces the channel's history in the database with the selection. The warning before connecting looks for `<channel>_messages.json` files only, so a `sqlite` folder gets no warning even though its histories are replaced.

Known limits:

- Pages are kept whole, so the `chats` and `users` of the page that crosses a bound can include entries referenced only by excluded messages.
- `--since` stops paging at the first page older than the date. This assumes message dates follow id order, which imported histories can break.
- `count` in the saved JSON (`collected_actions` in the dataset) is Telegram's total for the channel, not the number of messages saved.

## Updating saved histories (`--update`)

```
python main.py collect --batch-file ./channels.txt --update -o ./output/data
```

Use the same `--output` as the earlier collection. For each channel, `--update` reads the saved history, collects only posts with a higher id, and merges them in. The saved history depends on the folder's [storage mode](#storage-modes---storage):

| Mode | Saved history read and updated |
|---|---|
| `files` | `<channel>/<channel>_messages.json` |
| `sqlite` | The channel's history in the database, found by the channel's numeric id after it is resolved |
| `both` | Both copies. They must hold the same messages first (see [`--update` in `both`](#storage-modes---storage)) |

In every mode:

- Messages are merged by id. A fetched copy replaces a saved one and saved posts are kept. The messages file stays ordered newest first.
- `chats` and `users` are merged by id, fetched copies first. `count` and the other fields come from the latest response.
- With no new posts, the saved history is left as it was: the file byte for byte, the database history with its earlier run and time.
- A failed request leaves the saved history as it was.
- A saved history that cannot be read or parsed fails that channel and is left untouched.
- A channel with no saved history is collected in full; the summary lists those channels.

`--update` cannot be combined with the history window options or with `--limit-download-to-channel-metadata`. The earlier collection form does not accept it.

| | History window (`--min-id`, `--since`, ...) | `--update` |
|---|---|---|
| Saved history (file or database) | Replaced by the selection | Kept; new posts merged in |
| Where to write | A fresh `--output` | The folder of the earlier collection |
| Lower bound | Given by you | The highest saved message id of each channel |

Limits:

- Only new posts are fetched, so posts deleted or edited on Telegram since the earlier run are not reflected.
- Duplicates are removed inside the merged file only. `build-dataset` appends to `msgs_dataset.csv`, so building again after an update adds the earlier posts a second time. Build into a fresh dataset if you need each post once (see [Building the dataset and the network](#building-the-dataset-and-the-network)).

## Batches, concurrency, and rate limits

A batch file holds one target per line, in any of the forms above. Blank lines are skipped. Repeated targets are dropped case-insensitively, keeping the first. Any other line (a path, an invite or post link, text with spaces) stops the command with exit 2 and the line number, before any credentials are read. Two targets that turn out to be the same channel (for example an old and a new username) are collected once; the later one is reported as a duplicate.

`--batch-file` collects up to 3 channels at once by default, in both command forms. The pages of one channel are still fetched in order. A single `--telegram-channel` always runs at 1. After login, channel resolution, profile metadata, history pages, and discovered-chat metadata lookups go through one shared request policy:

| Control | Default | Range | Effect |
|---|---|---|---|
| `--concurrency N` | 3 for `--batch-file` | 1–10 | Channels collected at once; also the most requests in flight |
| `--request-interval SECONDS` | 0.25 | 0.1–60 | Minimum gap between request attempts admitted by the shared policy |
| `--max-flood-wait SECONDS` | 60 | 0–3600 | Longest Telegram flood wait to sit out |
| `--max-retries N` | 3 | 0–10 | Additional attempts per request after a flood wait or a transient error |

- **Flood waits.** When Telegram asks the client to wait during collection, the policy pauses new request attempts across all channels until the wait ends, and then retries the request. Requests already in flight can finish. A wait longer than `--max-flood-wait`, or another flood wait after `--max-retries` retries, stops the whole run: completed channels are kept, the rest are reported as not collected, `collection-status.json` says `interrupted`, and the exit code is 1. Wait before collecting again.
- **Transient errors.** Server errors, timeouts (60 seconds per attempt), and dropped connections are retried after 1, 2, 4 ... seconds (at most 30) within `--max-retries`. If they keep failing, only that channel fails.
- **Access errors.** A missing or private channel fails only that channel. A session that Telegram rejects stops the run; log in again.
- **Serial fallback.** `--concurrency 1` collects one channel at a time with the same policy. Use it if you prefer the earlier one-by-one behavior, or when an account hits flood waits often.

**Concurrency is not a Telegram quota.** More workers do not raise Telegram's limits for an account: the request gap is shared across all channels, and a flood wait pauses new policy attempts for all of them. The defaults are project limits, not a rate that Telegram guarantees to be safe. On simulated 1-second requests, 3 workers took about 40% of the serial time, and 10 workers were limited by the 0.25-second gap (see [docs/compatibility.md](docs/compatibility.md)). Real timings depend on Telegram.

Login and the subsequent account check run before this policy is created; neither is paced or retried by it. Login follows the failure rules in [First login](#first-login). Telethon can also retry a server error once inside a policy attempt, so the interval and retry settings do not count every underlying Telegram request.

The summary prints the counts (given, after removing duplicates, succeeded, failed, duplicates, cancelled) and, per kind of request (`resolve`, `channel`, `history`, `enrichment`), the policy attempts sent, retries, flood waits, and flood-wait seconds. `collection-status.json` records the same numbers.

## Output files

```
output/data/                          (or the folder given with --output)
├── <channel>/
│   ├── <channel>.json                channel profile metadata
│   ├── <channel>_messages.json       posts (not written by metadata-only runs)
│   └── <channel>_provenance.json     which profile collected those posts
├── chats.txt                         ids of chats found in the collected data
├── collected_chats.csv               metadata of the chats found in the data (e.g., forwards)
├── counter.csv                       how often each chat was found in this run
├── collection-status.json            state and per-target outcome of the last collection
├── collection-storage.json           storage mode, for sqlite and both folders only
├── telegram-tracker.sqlite3          the database (--storage sqlite or both)
├── _exceptions-channels-not-found.txt   requested targets that were missing or inaccessible (when any)
├── _exceptions-users-not-found.txt      discovered chats whose metadata could not be retrieved (when any)
├── msgs_dataset.csv                  written by build-dataset
├── collected_chats.xlsx              written by build-dataset
├── Graph.gexf                        written by build-network
└── network.png                       written by build-network
```

What happens when you collect into a folder that already holds data:

| File | On a rerun into the same folder |
|---|---|
| `<channel>.json` | Replaced after the channel is collected |
| `<channel>_messages.json` | Replaced (full or window), merged (`--update`), or kept (metadata only) |
| `<channel>_provenance.json` | Written with the messages file; see [Profiles and shared folders](#profiles-and-shared-folders) |
| `chats.txt` | Keeps the earlier ids; new ids are added in the order found |
| `collected_chats.csv` | Keeps the earlier rows as they were; this run's rows are appended with this run's counts. No `_x`/`_y` columns are created. A run that adds no rows leaves it untouched |
| `counter.csv` | Replaced; describes this run only |
| `collection-status.json` | Replaced; describes this run only |
| `_exceptions-*.txt` | Appended to |

Channel payloads written by this version hold collected Telegram data unfiltered, including the names, usernames, and phone numbers (when Telegram returns them) of users who appear in it, for example as commenters. The one exception is the collecting account itself: when it appears, its `User` object is kept with `phone` set to null.

A channel's metadata, message history, and history provenance files are written only after the whole channel was collected, so a request failure keeps its earlier files. JSON and CSV files and `chats.txt` are written to a temporary file and then moved into place, so a reader sees either the old or the new version of each file. There is no transaction across files: a crash can leave files from different runs (see below).

### Profiles and shared folders

Several profiles can collect into the same `--output` folder, one run at a time (the folder lock serializes them). Each saved history records which profile collected it in `<channel>/<channel>_provenance.json`: the profile name and ID (`profile_name`, `profile_id`, null when unknown), how the history was last written (`replaced`, `created`, or `merged`), the run's start time, its message count and newest message ID, and whether it took over another profile's history. It never holds an account ID, phone number, or credential.

- **`--update` merges only into the same profile's history.** If the record has a profile ID, it must match; if it has only a name, the name must match. A history without a record (collected by an earlier version) can be updated only by the default profile, and it stays without a record. Otherwise that channel fails before any request for it, with guidance, and the other channels continue. A merge keeps the recorded profile. A record whose message count or newest ID no longer matches the messages file (a run stopped between the two files) refuses every `--update` of that channel.
- **A full or window run replaces any history** and records its own profile. When the previous profile could not have updated it, the record and the channel's entry in `collection-status.json` get `takeover: true` with the previous profile's name and ID, and the run summary lists the channel.
- Two profile names for the same Telegram account are still different profiles here, and a profile name alone cannot tell apart accounts in different application homes or on different computers. A full run resolves a refused update.
- Access hashes and file references in the collected data are valid only for the account that collected them; the provenance record and `collection-status.json` name the profile, not the account. Channel metadata files (`<channel>.json`) reflect the latest run that wrote each file. Aggregate files follow the append/replace rules above; older rows can reflect other profiles and are not individually profile-tagged.

## Storage modes (`--storage`)

`collect --storage MODE` chooses where the collected data goes:

| Mode | Writes | Builders |
|---|---|---|
| `files` | The files above. The default for a new folder | Run |
| `sqlite` | Only the database `<output>/telegram-tracker.sqlite3`, plus `collection-status.json` and `collection-storage.json` | Refuse: they read the files, and builders that read the database are not available yet |
| `both` | The same data in the files (exactly as `files` writes them) and in the database | Run on the files |

The database holds the same fields as the files, plus a record of every run; see [docs/sqlite-schema.md](docs/sqlite-schema.md) for its tables and example queries. Its path is always `telegram-tracker.sqlite3` inside the `--output` folder.

```
# a new folder that keeps both the files and the database
python main.py collect --batch-file ./channels.txt --output ./output/project-b --storage both

# later runs into that folder use the recorded mode without the option
python main.py collect --batch-file ./channels.txt --output ./output/project-b --update
```

**A folder keeps its mode.** A `sqlite` or `both` folder records its mode in `collection-storage.json` (with the database's dataset ID) when it is created. A run without `--storage` uses the recorded mode; a folder without that file is a `files` folder. Choosing another mode for a folder exits 2 before connecting, and so does `sqlite` or `both` for a folder that already holds other files: existing outputs are not converted, so start a new or empty `--output`. The earlier collection form (`python main.py --telegram-channel ...`) writes files only and refuses a `sqlite` or `both` folder with exit 2; use `collect` there.

**Checks before connecting.** For a `sqlite` or `both` folder, the database must exist, be a Telegram Tracker database of the current schema version, belong to the folder (the dataset ID in `collection-storage.json` and the folder it was created for), and have no unpublished files (see below). Otherwise the run exits 1 before connecting and leaves the folder untouched. A folder that was moved or copied is refused for this reason; collect into a fresh `--output`. The selected session file is never opened as the database, and any other SQLite file at that path is refused untouched.

**Locks.** Besides `.collection.lock`, a database run holds `telegram-tracker.sqlite3.lock`, taken after the folder lock (after login) and removed when the run ends. An existing one is reported before connecting with its own message (`The database ... is in use by another collection or upgrade (pid ..., started ...)`). A lock left by a killed run is not removed automatically: make sure no collection is running, then delete it. The checks above run again once both locks are held, before anything is written. A run that finds an earlier run still recorded as `running` in the database (its process ended without finishing) records that run as `interrupted`, with a stop reason starting with `stale:`, and says so on stderr.

**How `both` keeps the two copies detectable.** For each channel, the files are first written to temporary files; then the database stores the channel and marks its files `pending`; then the temporary files replace the channel's files; then the mark becomes `written`. The aggregate files follow the same order after the last channel. If channel staging or its database transaction fails, that channel's previous payloads stay in both copies; run status can still record the failure. A failure while replacing the files stops the run (exit 1), records that channel as failed in the database, and leaves the `pending` mark. On Windows, a replace that another program briefly refuses (an antivirus scanner, a file indexer) is retried up to 5 attempts, with 0.75 s of total configured retry delays per file, in `both` mode only; filesystem calls take additional time.

**Recovering a blocked folder.** A `pending` mark means the files and the database may disagree, so later collection into that folder is refused. Using the recorded mode exits 1 with `<folder> is blocked: ...`; choosing a different mode exits 2 under the existing mode rule. Deleting the lock files does not clear it. Keep the folder for inspection and collect into a fresh `--output`; there is no repair command. A process that ends after replacing the files but before recording it leaves the mark too, although the files then agree with the database: such a folder is blocked as well. A crash can also leave temporary files (`<name>.<pid>.tmp.<ext>`, `.telegram-tracker.sqlite3.<id>.creating`) next to the files; they are never read and can be deleted. A Ctrl+C or a flood-wait stop does not block the folder: the channels saved before it are in both copies. A crash during a new folder's setup, after the database was created and before `collection-storage.json` was written, leaves a database without a manifest: the folder then counts as a `files` folder, a run without `--storage` writes files beside that database (whose first run stays `running`), and `--storage sqlite` or `both` is refused because the folder is not empty. Delete the folder, or collect into a fresh `--output`.

**`--update` in `both`.** Before a channel's first history request, its saved history must be the same in both copies: the same newest message ID and the same message IDs. A channel whose copies differ, or that has a saved history in only one of them, fails with guidance, and the other channels continue; the larger copy is never taken as a repair. A channel without a history in either copy is collected in full.

**After a crash or a killed run.** The database stores each channel in its own transaction, so the channels saved before the crash stay in it; the run's aggregate rows and end state are missing. `collection-status.json` still says `running`, and `.collection.lock` and `telegram-tracker.sqlite3.lock` stay behind if the run had taken them. To continue:

1. Make sure no collection is running (check the process ID in the lock messages).
2. Delete both lock files.
3. Collect into the same folder again. The new run records the crashed one as `interrupted` (stop reason `stale: ...`). In a `both` folder, a run that stops with `is blocked` found a `pending` mark: keep that folder and collect into a fresh `--output` (see above).

## Reading the database

`telegram-tracker.sqlite3` is an SQLite database: one binary file that SQLite libraries and tools open and query directly. It is not an SQL dump, the text file of `CREATE TABLE` and `INSERT` statements that some tools export; `collect` neither reads nor writes dumps. [docs/sqlite-schema.md](docs/sqlite-schema.md) describes every table, column, and view, and has more example queries.

Open the database read-only, and preferably when no collection is writing to it. A connection inside an open read transaction makes a collection wait, and a write that waits more than 5 seconds stops the run with exit 1 (`the database is in use by another process`). Finish the query and close the connection before collecting.

From Python, with the standard library only (the paths below are relative to the folder you run from):

```python
import sqlite3

con = sqlite3.connect('file:output/project-b/telegram-tracker.sqlite3?mode=ro', uri=True)
query = '''
SELECT p.username, count(*) AS posts, min(m.date) AS first_post, max(m.date) AS last_post
FROM messages m
JOIN peers p ON p.peer_kind = 'channel' AND p.peer_id = m.channel_id
GROUP BY m.channel_id
ORDER BY posts DESC
'''
for row in con.execute(query):
    print(row)
con.close()
```

With pandas (installed with the requirements), `pandas.read_sql_query(query, con)` returns the same rows as a DataFrame. Columns not listed in the schema are still in each row's `raw` JSON; read them with `json_extract(raw, '$.field')`.

To export an SQL dump, for example to load the data into another database system or to keep a text archive:

```python
import sqlite3

con = sqlite3.connect('file:output/project-b/telegram-tracker.sqlite3?mode=ro', uri=True)
with open('project-b.sql', 'w', encoding='utf-8') as dump:
    for statement in con.iterdump():
        dump.write(statement + '\n')
con.close()
```

A dump does not carry the database's identity (its application ID and schema version), so a database rebuilt from a dump is refused by `collect` as not being a Telegram Tracker database. Use a backup (below) to keep a copy that `collect` accepts.

## Database backups and schema versions

**Schema version.** A database records its schema version, `1` in this release, and an application ID that marks it as a Telegram Tracker database. `collect` uses only a database of the current version: an older or newer one, another SQLite database, an empty file, or a damaged one is refused before connecting (exit 1) and left untouched. `collect` never upgrades a database.

**No upgrade command yet.** Version 1 is the first schema version, so there is nothing to upgrade, and this release has no `migrate` command. The package contains the upgrade procedure for later versions (a checked backup first, then one transaction per version step, under the database lock), but no upgrade step is defined.

**Backups.** Back up while no collection is running, through SQLite's backup API, which copies a consistent state:

```python
import sqlite3

source = sqlite3.connect('file:output/project-b/telegram-tracker.sqlite3?mode=ro', uri=True)
backup = sqlite3.connect('project-b-backup.sqlite3')
source.backup(backup)
backup.close()
source.close()
```

The `sqlite3` command-line shell, if you have it, does the same with `sqlite3 telegram-tracker.sqlite3 ".backup copy.sqlite3"`. Copying the file itself is safe only while no collection is running and no `telegram-tracker.sqlite3-journal` file is beside it.

A backup belongs to the folder its database was created for. Copied back over `telegram-tracker.sqlite3` in that folder (while no collection is running), it is accepted again. In any other folder, `collect` refuses it as a moved or copied folder, but it stays readable. In a `both` folder, restoring only the database makes the two copies differ, and `--update` then fails the channels whose histories differ; collect those with a full run, or start a fresh `--output`. The database holds personal data from the collected content (see [docs/sqlite-schema.md](docs/sqlite-schema.md#personal-data)); store backups as carefully as the database.

## Collection status, locking, and recovery

Each collection writes `collection-status.json` in the output folder. It records the state, the profile (`profile_name`, `profile_id`), the parameters (full, window with its bounds, update, or metadata only), the request controls and statistics, the account check (`account_checks`), and each target's outcome (`succeeded`, `failed` with a reason, `duplicate`, `cancelled`), with the derived `min_id` and history mode for `--update` and any takeover. It holds no credentials, account IDs, or message text. Status files written by earlier versions have no profile fields: their profile is unknown.

| State | Meaning | Builders |
|---|---|---|
| `running` | Collecting, or stopped without finishing (a crash) | Refuse |
| `complete` | Every target was collected | Run |
| `partial` | Finished; some targets failed | Run, with a note naming the failed targets |
| `interrupted` | Stopped early (Ctrl+C, a flood-wait or session stop, a storage error, or another fatal error); completed channels are saved | Refuse |

`build-dataset` also notes when the last collection used a history window or `--update`. Folders without a status file (from earlier versions) are accepted.

**One collection per folder.** A run acquires `.collection.lock` in the output folder after login and removes it when it ends. An existing lock is checked before connecting and causes exit 1 with `<folder> is in use by another collection (pid ..., started ...)`. If two runs pass that check before either acquires the lock, both can reach login; only one can hold the lock and write collection output at a time. The lock protects the folder; the session lease and the account lease (see [Account profiles](#account-profiles)) keep a second collection off the same session or Telegram account, even into another folder.

**Stale lock.** A lock left by a crashed or killed run is not removed automatically. To recover:

1. Make sure no collection is running (check the process id named in the message).
2. Either keep the folder for inspection and collect into a fresh `--output`, or delete `<folder>/.collection.lock` to reuse the folder.

**Interrupted or crashed run.** If `collection-status.json` says `interrupted`, or still says `running` after the process ended, the aggregate files may be incomplete or from different runs, and the builders refuse the folder. To recover:

1. Keep that folder as it is, for inspection.
2. Run the collection again into a fresh `--output`.
3. Build the dataset and the network from the new folder.

The completed `<channel>.json` and `<channel>_messages.json` files in the old folder stay readable. If a run is cancelled while a finished channel is still waiting to be saved, that channel may be reported as not collected; collect it again.

Memory: each channel's history is held in memory until it is saved, and the run keeps the chat observations of every completed channel until the end. Very large batches or histories need correspondingly more memory.

## Building the dataset and the network

Run the builders after a collection has finished, on the same folder:

```
python main.py build-dataset --data-path ./output/data
python main.py build-network --data-path ./output/data
```

Without `--data-path` (or `-d`), both use `./output/data`. They read local files only, need no credentials or session, and can run without a network connection.

**`build-dataset`** reads every `<channel>_messages.json` (in sorted path order) and `collected_chats.csv`, and writes:

- `msgs_dataset.csv`: one row per post. Rows are **appended** to an existing file, so building twice, or again after `--update`, duplicates rows. To get each post once, delete or move `msgs_dataset.csv` before building, or build from a fresh collection folder.
- `collected_chats.xlsx`: replaced on every build.

**`build-network`** reads `collected_chats.csv` (`username`, `counter`, `source`) and writes `Graph.gexf`, a directed, unweighted graph from each source channel to the chats found in its data (e.g., forwards), and `network.png`, a preview with Louvain communities. Both are replaced on every build. Open the GEXF file in tools such as [Gephi](https://gephi.org/).

Both builders refuse a folder whose `collection-status.json` says `running` or `interrupted` (exit 1, with the recovery steps above), and a folder collected with `--storage sqlite`, which has no file outputs (exit 1). A `both` folder is built from its files; the builders never open the database. Missing or malformed input files stop the build with exit 1 before anything is written. With no messages and no existing `msgs_dataset.csv`, the dataset has a header only; an existing dataset keeps its earlier rows. With no edges, the graph is empty and the image blank.

## Known limits

The behavior in this README is validated by the offline test suite, with synthetic data and a fake Telegram client, plus an install check of the built package. What that validation does not cover:

- **Platforms.** Tested on Windows, on local NTFS drives only. The Linux and macOS permission branch (0700/0600), the lock and lease files on those systems, and network file systems are untested.
- **Ctrl+C.** Interruption is tested by cancelling the collection inside the process, not by sending a real Ctrl+C (SIGINT) to a running command.
- **Database modes and profiles against Telegram.** `--storage sqlite` and `both`, and named profiles, are validated offline; this validation includes no collection from Telegram in those modes or profiles.
- **Request rates.** The concurrency and request-interval defaults are project limits. The rates that Telegram accepts for an account were not measured.
- **Builders.** `build-dataset` and `build-network` read the files only. A `sqlite` folder has nothing for them until builders that read the database exist.
- **Dataset appends.** `build-dataset` appends to `msgs_dataset.csv`, so building twice, or again after `--update`, duplicates rows (see [Building the dataset and the network](#building-the-dataset-and-the-network)).

## Running the tests

The test suite runs offline with synthetic fixtures. It never connects to Telegram, reads `config/config.ini`, opens a session file, or touches your application home: every test process uses a temporary `TELEGRAM_TRACKER_HOME`. From the repository folder, in the project environment:

```
python -m unittest discover -s tests
python -m pip check
```

The tests import the `telegram_tracker` package from `src/` in this checkout, so the project does not need to be installed to run them, and an installed copy is not used. `python -m pytest` runs the same tests when pytest is installed. [docs/compatibility.md](docs/compatibility.md) describes the tested environment, the CLI contract, and the offline test harness.
