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

- `collect` saves each channel's profile metadata and posts as JSON, plus aggregate files about the chats found in that data (e.g., forward sources). You can collect one channel or a list of channels from a text file.
- `build-dataset` reads the collected files and writes `msgs_dataset.csv` and `collected_chats.xlsx`.
- `build-network` reads `collected_chats.csv` and writes a directed graph, `Graph.gexf`, and a preview image, `network.png`.

The two builders run offline: they read local files only and never connect to Telegram.

Contents:

- [Requirements](#requirements)
- [Installation and setup](#installation-and-setup)
- [First login](#first-login)
- [Commands](#commands)
- [Collecting channels](#collecting-channels)
- [Collecting part of a history](#collecting-part-of-a-history)
- [Updating saved histories](#updating-saved-histories---update)
- [Batches, concurrency, and rate limits](#batches-concurrency-and-rate-limits)
- [Output files](#output-files)
- [Collection status, locking, and recovery](#collection-status-locking-and-recovery)
- [Building the dataset and the network](#building-the-dataset-and-the-network)
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

- Values you typed are saved to `credentials.json` in the application home. `--no-save` skips saving.
- Values from the environment or an INI file are saved only if you answer `y` (default no). Without a terminal they are not saved; `--save` saves them without asking.
- Different values already saved are replaced only if you agree, or with `--save`.
- A failed or interrupted login saves nothing. If the login succeeds but saving fails, the command exits 1 and says so; the session stays logged in.

While a complete set is saved, `auth login` asks for nothing. To change the saved values, run `auth clear-credentials` and then `auth login`, or run `auth login` with the new values in the environment or in `--config` and agree to replace the saved ones. The complete new set is saved.

### Where the app credentials come from

The first source that provides a value wins:

| Order | Source | Notes |
|---|---|---|
| 1 | `TELEGRAM_TRACKER_API_ID`, `TELEGRAM_TRACKER_API_HASH`, `TELEGRAM_TRACKER_PHONE` | Each variable overrides one value. |
| 2 | `--config PATH` | An INI file with the `[Telegram API credentials]` section. A missing file is an error. |
| 3 | `credentials.json` in the application home | Saved by `auth login`. |
| 4 | `./config/config.ini` in the working directory | The earlier setup. |
| 5 | Prompts | `auth login` only. |

Only one file source (2, 3, or 4) is used, as a whole: the first one present. A value missing from it is not taken from another file. Template values (`api_id = api_id`, `<your api_id>`, or empty) count as missing, so the placeholder `config/config.ini` in the repository never hides saved credentials. An invalid value (for example an `api_id` that is not a positive number) stops the command with exit 2; it never falls back to another source. There are no command-line options for the API ID, hash, or phone, and none for the login code or password.

`collect` uses the same sources but never asks: with values missing it exits 2 and names the ways to provide them.

### Which session is used

The first match wins:

1. `--session PATH` (`collect` and `auth`).
2. `TELEGRAM_TRACKER_SESSION`.
3. `session_file.session` in the application home, if it exists.
4. `./session_file.session` in the working directory, if it exists. Earlier versions created the session there. The command prints a one-line notice when it uses it.
5. Otherwise `session_file.session` in the application home, created by the first login.

Nothing is moved or copied automatically. Once a session exists in the application home, it is used even when an older `./session_file.session` exists. A path without the `.session` suffix gets it, and links in the path are resolved, so every route to one session file shares its lock. The session file must not be inside the `--output` folder, and the `--output` folder must be outside the application home.

### Checking, logging out, and removing saved credentials

- `auth status` shows the application home, the selected session, the saved credentials file, and where each value comes from: source names only, never values. It works offline and creates nothing, so a present session file does not prove that it is still logged in. `auth status --check` connects to Telegram to find out (exit 0 logged in, 1 not); it never asks for a code and never creates a session. Telethon may update the session file while checking.
- `auth logout` asks for confirmation (`--yes` skips it), then logs the session out on Telegram. That revokes it and deletes the session file. Saved credentials stay. A session that is not logged in, or no session at all, is left as it is.
- `auth clear-credentials` asks for confirmation (`--yes` skips it), then deletes `credentials.json` only. It does not log the session out or touch environment variables, INI files, or collected data, and it removes a damaged file without reading it.

### How saved credentials are protected

`credentials.json` holds exactly `schema_version`, `api_id`, `api_hash`, and `phone`. It is plain text protected by file permissions, not encryption: the application home and its files are readable only by your account (mode 0700/0600 on Linux and macOS; on Windows an access list that grants only your account, with inheritance from the parent folder disabled). Administrators of the computer can still read them. A session created in the application home gets the same protection. The login code, the two-step verification password, and session keys are never saved.

Saving writes a new file next to the old one and replaces it in one step, so a failure leaves the earlier file intact. If the application home cannot be created or protected, `auth login` and `collect` stop with exit 1 before connecting; set `TELEGRAM_TRACKER_HOME` to another absolute folder, or pass `--session`. A `credentials.json` that is damaged, of another version, or readable by other accounts stops every command that needs it with exit 2; remove it with `auth clear-credentials` and log in again.

`TELEGRAM_TRACKER_HOME` replaces the application home for all of this (it must be an absolute path).

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
python main.py auth login | status | logout | clear-credentials
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
| `python build-datasets.py [--data-path DIR]` | `python main.py build-dataset [--data-path DIR]` | `-d` also works |
| `python channels-to-network.py [--data-path DIR]` | `python main.py build-network [--data-path DIR]` | `-d` also works |

The earlier collection form accepts only `--telegram-channel`, `--batch-file`, `--limit-download-to-channel-metadata`, `--output`, and `--min-id`. Its help (`python main.py --telegram-channel x --help`) lists `--min-id` under "history window".

### Exit codes

The same for every form:

| Code | Meaning |
|---|---|
| `0` | Every requested target was collected, the build succeeded, or the `auth` action completed (including "nothing to do"). Empty channels and empty inputs count as success. |
| `1` | A target failed, the login failed, the run stopped early (flood wait or rejected session), the output folder or the session is in use, writing an output or the credentials failed, the application home cannot be created, an `auth` confirmation was declined, `auth status --check` found the session logged out, the input data of a build is missing or malformed, or a library is missing. |
| `2` | Invalid arguments, including an unreadable batch file or an unsupported line in it, and missing or invalid app credentials (before any connection). |
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

**Metadata only** saves `<channel>.json` and the aggregate files, but no new `<channel>_messages.json`. A messages file from an earlier run stays in place; the summary and `collection-status.json` (`stale_history`) name those channels.

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

Known limits:

- Pages are kept whole, so the `chats` and `users` of the page that crosses a bound can include entries referenced only by excluded messages.
- `--since` stops paging at the first page older than the date. This assumes message dates follow id order, which imported histories can break.
- `count` in the saved JSON (`collected_actions` in the dataset) is Telegram's total for the channel, not the number of messages saved.

## Updating saved histories (`--update`)

```
python main.py collect --batch-file ./channels.txt --update -o ./output/data
```

Use the same `--output` as the earlier collection. For each channel, `--update` reads the saved `<channel>/<channel>_messages.json`, collects only posts with a higher id, and merges them in:

- Messages are merged by id. A fetched copy replaces a saved one, saved posts are kept, and the file stays ordered newest first.
- `chats` and `users` are merged by id, fetched copies first. `count` and the other fields come from the latest response.
- With no new posts, the file is left byte for byte as it was.
- A failed request leaves the saved file as it was.
- A saved file that cannot be read or parsed fails that channel and is left untouched.
- A channel with no saved file is collected in full; the summary lists those channels.

`--update` cannot be combined with the history window options or with `--limit-download-to-channel-metadata`. The earlier collection form does not accept it.

| | History window (`--min-id`, `--since`, ...) | `--update` |
|---|---|---|
| Saved `<channel>_messages.json` | Replaced by the selection | Kept; new posts merged in |
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

Login runs before this policy is created and follows the failure rules in [First login](#first-login). Telethon can also retry a server error once inside a policy attempt, so the interval and retry settings do not count every underlying Telegram request.

The summary prints the counts (given, after removing duplicates, succeeded, failed, duplicates, cancelled) and, per kind of request (`resolve`, `channel`, `history`, `enrichment`), the policy attempts sent, retries, flood waits, and flood-wait seconds. `collection-status.json` records the same numbers.

## Output files

```
output/data/                          (or the folder given with --output)
├── <channel>/
│   ├── <channel>.json                channel profile metadata
│   └── <channel>_messages.json       posts (not written by metadata-only runs)
├── chats.txt                         ids of chats found in the collected data
├── collected_chats.csv               metadata of the chats found in the data (e.g., forwards)
├── counter.csv                       how often each chat was found in this run
├── collection-status.json            state and per-target outcome of the last collection
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
| `chats.txt` | Keeps the earlier ids; new ids are added in the order found |
| `collected_chats.csv` | Keeps the earlier rows as they were; this run's rows are appended with this run's counts. No `_x`/`_y` columns are created. A run that adds no rows leaves it untouched |
| `counter.csv` | Replaced; describes this run only |
| `collection-status.json` | Replaced; describes this run only |
| `_exceptions-*.txt` | Appended to |

A channel's two JSON files are written only after the whole channel was collected, so a request failure keeps its earlier files. JSON and CSV files and `chats.txt` are written to a temporary file and then moved into place, so a reader sees either the old or the new version of each file. There is no transaction across files: a crash can leave files from different runs (see below).

## Collection status, locking, and recovery

Each collection writes `collection-status.json` in the output folder. It records the state, the parameters (full, window with its bounds, update, or metadata only), the request controls and statistics, and each target's outcome (`succeeded`, `failed` with a reason, `duplicate`, `cancelled`), with the derived `min_id` and history mode for `--update`. It holds no credentials or message text.

| State | Meaning | Builders |
|---|---|---|
| `running` | Collecting, or stopped without finishing (a crash) | Refuse |
| `complete` | Every target was collected | Run |
| `partial` | Finished; some targets failed | Run, with a note naming the failed targets |
| `interrupted` | Stopped early (Ctrl+C, a flood-wait or session stop, a storage error, or another fatal error); completed channels are saved | Refuse |

`build-dataset` also notes when the last collection used a history window or `--update`. Folders without a status file (from earlier versions) are accepted.

**One collection per folder.** A run acquires `.collection.lock` in the output folder after login and removes it when it ends. An existing lock is checked before connecting and causes exit 1 with `<folder> is in use by another collection (pid ..., started ...)`. If two runs pass that check before either acquires the lock, both can reach login; only one can hold the lock and write collection output at a time. The lock protects the folder, not the account: never run two collections with the same session, even into different folders.

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

Both builders refuse a folder whose `collection-status.json` says `running` or `interrupted` (exit 1, with the recovery steps above). Missing or malformed input files stop the build with exit 1 before anything is written. With no messages and no existing `msgs_dataset.csv`, the dataset has a header only; an existing dataset keeps its earlier rows. With no edges, the graph is empty and the image blank.

## Running the tests

The test suite runs offline with synthetic fixtures. It never connects to Telegram, reads `config/config.ini`, opens a session file, or touches your application home: every test process uses a temporary `TELEGRAM_TRACKER_HOME`. From the repository folder, in the project environment:

```
python -m unittest discover -s tests
python -m pip check
```

The tests import the `telegram_tracker` package from `src/` in this checkout, so the project does not need to be installed to run them, and an installed copy is not used. `python -m pytest` runs the same tests when pytest is installed. [docs/compatibility.md](docs/compatibility.md) describes the tested environment, the CLI contract, and the offline test harness.
