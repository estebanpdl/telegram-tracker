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
| (none) | `python main.py collect ... --concurrency/--request-interval/--max-flood-wait/--max-retries` | Request controls: `collect` only. Validated but not applied yet |
| `python build-datasets.py [--data-path DIR]` | `python main.py build-dataset [--data-path DIR]` | `-d` also works |
| `python channels-to-network.py [--data-path DIR]` | `python main.py build-network [--data-path DIR]` | `-d` also works |

Exit codes for every form: `0` success; `1` a target, the login, or the input data failed (or a dependency is missing); `2` invalid arguments, reported before any credentials are read; `130` interrupted.

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

request controls:
  Validated now; not applied yet. This version collects channels one at a
  time.

  --concurrency N       Channels collected at once, 1-10 (default: 3 for
                        --batch-file, 1 for --telegram-channel).
  --request-interval SECONDS
                        Minimum gap between Telegram requests, 0.1-60
                        (default: 0.25).
  --max-flood-wait SECONDS
                        Longest server flood wait to sit out, 0-3600; 0 stops
                        on any flood wait (default: 60).
  --max-retries N       Additional attempts per request, 0-10 (default: 3).
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

To collect only messages newer than one you already have, pass its id to `--min-id` (exclusive):

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
