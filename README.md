<div align="center">

# **Telegram-API**: `a Python-based open-source tool for Telegram`

---

[![GitHub forks](https://img.shields.io/github/forks/estebanpdl/telegram-tracker.svg?style=social&label=Fork&maxAge=2592000)](https://GitHub.com/estebanpdl/telegram-tracker/network/)
[![GitHub stars](https://img.shields.io/github/stars/estebanpdl/telegram-tracker?style=social)](https://github.com/estebanpdl/telegram-tracker/stargazers)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://github.com/estebanpdl/telegram-tracker/blob/main/LICENCE)
[![Open Source](https://badges.frapsoft.com/os/v1/open-source.svg?v=103)](https://twitter.com/estebanpdl)
[![Made-with-python](https://img.shields.io/badge/Made%20with-Python-1f425f.svg)](https://www.python.org/)
[![Twitter estebanpdl](https://badgen.net/badge/icon/twitter?icon=twitter&label)](https://twitter.com/estebanpdl)

---
</div>

## Overview

This tool connects to Telegram's API. It generates JSON files containing channel's data, including channel's information and posts. You can search for a specific channel, or a set of channels provided in a text file (one channel per line.)

Files are saved by default in a folder called *output/data*. These folders are created by the script. You can also give a specific output directory to store collected data.

**Software required**

* [Python 3.14](https://www.python.org/) (tested with 3.14.7; pinned library versions and known limitations are in [docs/compatibility.md](docs/compatibility.md))
* [Telegram API credentials](https://my.telegram.org/auth?to=apps)
	+ Telegram account
	+ App `api_id`
	+ App `api_hash`

**Python required libraries**

* [Telethon](https://docs.telethon.dev/en/stable/)
* [Pandas](https://pandas.pydata.org/)
* [Openpyxl](https://openpyxl.readthedocs.io/en/stable/)
* [tqdm](https://tqdm.github.io/)
* [Networkx](https://networkx.org/)
* [Matplotlib](https://matplotlib.org/)
* [Louvain Community Detection](https://github.com/taynaud/python-louvain)


Installing
----------

- **Via git clone**

```
git clone https://github.com/estebanpdl/telegram-api.git
```

This will create a directory called `telegram-tracker` which contains the Python scripts. Cloning allows you to easily upgrade and switch between available releases.

- **From the github download button**

Download the ZIP file from github and use your favorite zip utility to unpack the file `telegram-tracker.zip` on your preferred location.

**After cloning or downloding the repository, install the libraries from `requirements.txt`.**

```
pip install -r requirements.txt
```

or

```
pip3 install -r requirements.txt
```

**Once you obtain an API ID and API hash on my.telegram.org, populate the `config/config.ini` file with the described values.**

```ini

[Telegram API credentials]
api_id = api_id
api_hash = api_hash
phone = phone
```

*Note: Your phone must be included to authenticate for the first time. Use the format +\<code>\<number> (e.g., +19876543210). Telegram API will send you a code via Telegram app that you will need to include.*

<br />

---

# Example usage

## Commands

`main.py` is the single entry point, with one subcommand per step. Builders run offline and only read local files.

```
python main.py collect --telegram-channel NAME [options]
python main.py collect --batch-file FILE [options]
python main.py build-dataset [--data-path DIR]
python main.py build-network [--data-path DIR]
```

Run `python main.py --help` or `python main.py <command> --help` for the options. Help never needs credentials or a session.

### Migrating from the earlier commands

The earlier commands still work and reach the same code. They are supported through the first release, with no removal date.

| Earlier command | Canonical command | Notes |
|---|---|---|
| `python main.py --telegram-channel NAME` | `python main.py collect --telegram-channel NAME` | |
| `python main.py --batch-file FILE` | `python main.py collect --batch-file FILE` | |
| `... --limit-download-to-channel-metadata` | `python main.py collect ... --limit-download-to-channel-metadata` | |
| `... --output DIR` / `-o DIR` | `python main.py collect ... --output DIR` | Default `./output/data`, relative to the working directory |
| `... --min-id N` | `python main.py collect ... --min-id N` | Same meaning: only messages with an id above N |
| (none) | `python main.py collect ... --since/--until/--max-id/--max-messages` | History window: **`collect` only**. The earlier form accepts only `--min-id` |
| (none) | `python main.py collect ... --update` | Merge new posts into saved histories: **`collect` only** |
| (none) | `python main.py collect ... --concurrency/--request-interval/--max-flood-wait/--max-retries` | Request controls: `collect` only. The earlier form runs with the defaults (3 channels at once for `--batch-file`) |
| `python build-datasets.py [--data-path DIR]` | `python main.py build-dataset [--data-path DIR]` | `-d` also works |
| `python channels-to-network.py [--data-path DIR]` | `python main.py build-network [--data-path DIR]` | `-d` also works |

Exit codes for every form: `0` success; `1` a target, the login, the input data, or writing the output failed, the output folder is locked, or a dependency is missing; `2` invalid arguments, reported before any credentials are read; `130` interrupted.

### `collect --help` (excerpt)

```
history window:
  Select which messages to collect; bounds combine (AND). A filtered run
  replaces each channel's saved messages with the selection, so write it to
  a fresh --output to keep earlier histories. Not allowed with --limit-
  download-to-channel-metadata.

  --since DATE          Only messages dated at or after DATE (UTC unless an
                        offset is given; a date alone means midnight).
                        Example: --since 2026-09-01
  --until DATE          Only messages dated before DATE (exclusive). Example:
                        --until 2026-10-01 excludes 1 October (UTC).
  --max-id N            Only messages with an id below N (exclusive).
  --max-messages N      At most N messages per channel, newest first.

update:
  Add new posts to saved histories instead of replacing them. Not allowed
  with the history window options or --limit-download-to-channel-metadata.

  --update              For each channel, collect only posts newer than the
                        newest one in its saved <channel>_messages.json and
                        merge them into that file (fetched copies win). A
                        channel without a saved history is collected in full.
                        Posts deleted or edited on Telegram since the earlier
                        run are not reflected, and build-dataset appends the
                        merged history again, duplicating earlier rows.

request controls:
  Shared by every Telegram request of the run, including metadata lookups.
  These are project limits, not a safe rate guaranteed by Telegram. The
  earlier `python main.py --batch-file ...` form uses the defaults.

  --concurrency N       Channels collected at once, 1-10; also the most
                        requests in flight (default: 3 for --batch-file;
                        --telegram-channel always runs at 1). Use 1 to collect
                        one channel at a time.
  --request-interval SECONDS
                        Minimum gap between Telegram requests, 0.1-60
                        (default: 0.25).
  --max-flood-wait SECONDS
                        Longest server flood wait to sit out, 0-3600; a longer
                        one stops the whole run, keeping completed channels. 0
                        stops on any flood wait (default: 60).
  --max-retries N       Additional attempts per request after a flood wait or
                        a transient error, 0-10 (default: 3).
```

### Collecting part of a history

`--min-id`, `--max-id`, `--since`, `--until`, and `--max-messages` combine as AND.

- ID bounds are exclusive.
- `--since` is inclusive and `--until` is exclusive.
- Dates are ISO 8601 (`2026-10-01`, `2026-10-01T12:00`, `2026-10-01T12:00:00Z`, `2026-10-01T14:00+02:00`). A value without an offset is UTC.

```
# September 2026 only, into a fresh folder
python main.py collect --telegram-channel channelname --since 2026-09-01 --until 2026-10-01 -o ./output/sep-2026

# the newest 500 messages of each channel in a batch
python main.py collect --batch-file ./channels.txt --max-messages 500 -o ./output/sample

# messages after id 12345 (the earlier "update" use of --min-id)
python main.py collect --telegram-channel channelname --min-id 12345 -o ./output/new-posts
```

**A filtered run replaces `<channel>_messages.json` with the selected messages only.** It does not merge them into the saved history. Before connecting, the command warns on stderr when a channel already has a saved history in the output folder. Write filtered runs to a fresh `--output` to keep earlier histories. The run header prints the active window.

Known limits:
- Pages are counted whole, so the chats and users of the page that crosses a bound may include some referenced only by excluded messages.
- `--since` stops paging at the first page older than the date. This assumes message dates follow id order, which imported or forwarded-in-bulk histories can break.
- `count` in the saved JSON (`collected_actions` in the dataset) is Telegram's total for the channel, not the number of messages saved.

### Batch targets and concurrency

A batch file holds one target per line: a username, `@username`, a public link (`https://t.me/username`, `t.me/s/username`), or a numeric id. Links and `@` are reduced to the username, which also names the output folder. Blank lines are skipped. Repeated targets are dropped case-insensitively, keeping the first. Anything else (paths, invite or post links, spaces) stops the command with exit 2 and the line number, before connecting. Two targets that turn out to be the same channel (for example an old and a new username) are collected once; the later one is reported as a duplicate.

`--batch-file` collects up to 3 channels at once by default (`--concurrency 1` to `10`). The pages of one channel are still fetched in order. Every request of the run, including metadata lookups of discovered chats, goes through one shared policy:

- at most `--concurrency` requests in flight, and at least `--request-interval` seconds (0.25) between any two;
- a flood wait from Telegram pauses every channel until it ends, then the request is retried. A wait longer than `--max-flood-wait` (60 s), or one more flood wait after `--max-retries` (3) retries, stops the whole run: completed channels are kept, the rest are reported as not collected, and the exit code is 1;
- server errors, timeouts (60 s per attempt), and dropped connections are retried after 1, 2, 4 ... seconds (at most 30) within `--max-retries`; when they keep failing, only that channel fails;
- a missing or private channel fails only that channel; a rejected session stops the run.

The summary prints the counts (given, after removing duplicates, succeeded, failed, duplicates, cancelled) and, per kind of request (`resolve`, `channel`, `history`, `enrichment`), the requests sent, retries, flood waits, and flood-wait seconds. `collection-status.json` records the same numbers.

More concurrency does not raise Telegram's limits for an account: the request gap applies to the whole run, and flood waits pause everything. Only run one collection per account session at a time. On simulated 1-second requests, 3 workers took about 40% of the serial time and 10 workers were limited by the 0.25 s gap (see `docs/compatibility.md`); real timings depend on Telegram.

### Updating saved histories (`--update`)

```
python main.py collect --batch-file ./channels.txt --update -o ./output/data
```

For each channel, `--update` reads the saved `<channel>/<channel>_messages.json`, collects only posts with a higher id, and merges them in:

- Messages are merged by id. A fetched copy replaces a saved one, saved posts are kept, and the file stays ordered newest first.
- `chats` and `users` are merged by id, fetched copies first. `count` and the other fields come from the latest response.
- With no new posts the file is left byte for byte as it was.
- A failed request leaves the saved file as it was.
- A saved file that cannot be read or parsed fails that channel and is left untouched.
- A channel with no saved file is collected in full; the summary lists those channels.

`--update` cannot be combined with `--since`, `--until`, `--min-id`, `--max-id`, `--max-messages`, or `--limit-download-to-channel-metadata`. The earlier `python main.py --telegram-channel ...` form does not accept it.

Limits:
- Only new posts are fetched, so posts deleted or edited on Telegram since the earlier run are not reflected.
- Deduplication happens inside the merged file only. `build-dataset` appends to `msgs_dataset.csv`, so building again after an update duplicates the rows of earlier posts.

### Collection status, locking, and reruns

Each collection writes `collection-status.json` in the output folder. It records the state, the parameters (full, window with its bounds, update, or metadata only), and each target's outcome (`succeeded`, `failed` with a reason, `cancelled`), with the derived `min_id` and history mode for `--update`. It holds no credentials or message text.

| State | Meaning | Builders |
|---|---|---|
| `running` | Collecting, or stopped without finishing (a crash) | Refuse |
| `complete` | Every target was collected | Run |
| `partial` | Finished; some targets failed | Run, with a note naming the failed targets |
| `interrupted` | Stopped early (Ctrl+C, a storage error, or another fatal error); completed channels are saved | Refuse |

`build-dataset` also notes when the last collection used a history window or `--update`. Folders without a status file (earlier versions) are accepted.

- **One collection per folder.** A run holds `.collection.lock` in the output folder and removes it when it ends. A second run into the same folder exits 1 before connecting. A lock left by a crash is not removed automatically: if no collection is running, delete the file, or collect into a fresh `--output`. Never run two collections with the same account session, even into different folders.
- **Files are replaced atomically, one at a time.** Each JSON and CSV file is written to a temporary file and moved into place, so a reader sees the old or the new version. A hard crash can still leave files from different runs, with the status file saying `running`. To recover, keep that folder for inspection and collect into a fresh `--output`; the completed channel JSON files stay readable.
- **A failed channel keeps its earlier files.** Its `<channel>.json` and `<channel>_messages.json` are written only after the whole channel was collected.
- **Rerunning into a folder:** `chats.txt` adds new ids, `collected_chats.csv` keeps its earlier rows as they were and appends this run's rows with this run's counts, `counter.csv` describes this run, and the exception logs are appended to. No `_x`/`_y` columns are created.
- **Metadata only** leaves an earlier `<channel>_messages.json` in place; the summary and the status file (`stale_history`) say which channels have messages from an earlier run.

## `main.py`

This Python script will connect to Telegram's API and handle your API request.

### Options

* `--telegram-channel` Specifies Telegram Channel to download data from.
* `--batch-file` File containing Telegram Channels to download data from, one channel per line.
* `--limit-download-to-channel-metadata` Will collect channels metadata only, not channel's messages. (default = False)
* `--output, -o` Specifies a folder to save collected data. If not given, script will generate a default folder called `./output/data`
* `--min-id` Collect only messages with an id above the given id (exclusive). The saved messages file is replaced with those messages; see below.

<br />

### Structure of output data

```
├──🗂 output
|   └──🗂 data
|   	└──🗂 <channel_name>
|   		└──<channel_name>.json
|   		└──<channel_name>_messages.json
|   	└──chats.txt // TM channels, groups, or users' IDs found in data.
|   	└──collected_chats.csv // TM channels or groups found in data (e.g., forwards)
|   	└──collected_chats.xlsx // TM channels or groups found in data (e.g., forwards)
|   	└──counter.csv // TM channels, groups or users found in data (e.g., forwards)
|   	└──collection-status.json // State and per-channel outcome of the last collection
|   	└──user_exceptions.txt // From collected_chats, these are mostly TM users' which 
|									metadata was not possible to retrieve via the API
|   	└──msgs_dataset.csv // Posts and messages from the requested channels
```

<br />

## **Examples**

<br />

### **Basic request**

```
python main.py --telegram-channel channel_name`
```

**Expected output**

- Files of collected channels:
	- chats.txt
	- collected_chats.csv
	- user_exceptions.txt
	- counter.csv
- A new folder: *<channel_name>* containing
	- A JSON file containing channel's profile metadata
	- A JSON file containing posts from the requested channel

<br />

### **Request using a text file containing a set of channels**

```
python main.py --batch-file './path/to/channels_text_file.txt'
```

**Expected output**

- Files of collected channels:
	- chats.txt
	- collected_chats.csv
	- user_exceptions.txt
	- counter.csv
- New folders - based on the number of requested channels: *<channel_name>* containing
	- A JSON file containing channel's profile metadata
	- A JSON file containing posts from the requested channel

These examples will retrieve all posts available through the API from the requested channel. If you want to collect channel's information only, without posts, you can run:

<br />

### **Limit download to channel's metadata only**

```
python main.py --telegram-channel channelname --limit-download-to-channel-metadata
```

or, using a set of telegram channels via a text file:

```
python main.py --batch-file './path/to/channels_text_file.txt' --limit-download-to-channel-metadata
```

<br />

### **Updating channel's data**

To add new posts to a collection you already have, use `--update` with the canonical command and the same `--output`:

```
python main.py collect --telegram-channel channelname --update --output ./path/to/collection
```

See [Updating saved histories](#updating-saved-histories---update). To collect only messages after a given id into a separate folder, pass it to `--min-id` (exclusive):

```
python main.py --telegram-channel channelname --min-id 12345 --output ./path/to/new/directory
```

The saved `<channel_name>_messages.json` then holds **only** the messages after that id. Rerunning into the folder of an earlier collection replaces its saved history (a warning names the affected channels), so use a fresh `--output`. See [Collecting part of a history](#collecting-part-of-a-history).

**Expected output**

- Files of collected channels:
	- chats.txt
	- collected_chats.csv
	- user_exceptions.txt
	- counter.csv
- A new folder: *<channel_name>* containing
	- A JSON file containing channel's profile metadata
	- A JSON file containing posts from the requested channel

<br />

### **Specify output folder**

The script allows you to specify a specific output directory to save collected data. The sxcript will create those folders in case do not exist.

```
python main.py --telegram-channel channelname --output ./path/to/chosen/directory`
```

The expected output is the same a described above but data will be save using the chosen directory.

<br />

---

## `build-datasets.py`

This Python script reads the collected files and creates a new dataset containing messages from the requested channels. By default, the created dataset will be located in the `output` folder.

If you provided a specific directory to save collected data, you need to provide the same path to use this script.

### Options

* `--data-path` Path were data is located. Will use `./output/data` if not given.

If a specific directory was not provided in `main.py`, run:

```
python build-datasets.py
```

If you provided a specific directory using the option `--output` in `main.py`, run:

```
python build-datasets.py --data-path ./path/to/chosen/directory
```

These option will create the above-mentioned dataset: `msgs_dataset.csv`, a file containing posts and messages from the requested channels.

<br />

---

## `channels-to-network.py`

This Python script builds a network graph. By default, the file will be located in the `output` folder. The script also saves a preliminary graph: `network.png` using the modules matplotlib, networkx, and python-louvain, which implements community detection. You can import the GEFX Graph File using different softwares, including Gephi.

### Options

* `--data-path` Path were data is located. Will use `./output/data` if not given.

If a specific directory was not provided in `main.py`, run:

```
python channels-to-network.py
```

If you provided a specific directory using the option `--output` in `main.py`, run:

```
python channels-to-network.py --data-path ./path/to/chosen/directory
```
