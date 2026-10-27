# Media downloads

Telegram Tracker can save the photos, videos, audio and documents attached to the
collected messages. This is off by default: without `--include-media` or the
`download-media` command, no file is ever requested.

- **Local copies.** Files are written under `<output>/downloaded-media/` and stay there.
  Nothing is uploaded, and no file is deleted automatically once it is complete.
- **Never opened.** Files are saved, never opened, previewed or executed. Their names
  are generated, and types whose usual viewer runs embedded code get the neutral
  extension `.bin` (see [Classification](media-ledger.md#classification)).
- **Sharing a folder shares the files.** Copying, syncing or archiving the output folder
  copies the downloaded media with it. Collected media can show people and personal
  data; store and share the folder accordingly.
- **The same account.** File references and channel access hashes belong to the account
  that collected the messages, so `download-media` uses the profile that collected each
  channel (see [Profiles](#profiles)).

The ledger file, its tables and the exact rules behind this page are in
[media-ledger.md](media-ledger.md).

Contents:

- [Two ways to download](#two-ways-to-download)
- [What is downloaded](#what-is-downloaded)
- [Where the files go](#where-the-files-go)
- [Size limits](#size-limits)
- [Concurrency and speed](#concurrency-and-speed)
- [Profiles](#profiles)
- [Dry run](#dry-run)
- [Checking a folder](#checking-a-folder)
- [Exit codes](#exit-codes)
- [Interruptions and failures](#interruptions-and-failures)
- [Known limits](#known-limits)

## Two ways to download

**While collecting: `collect --include-media`.** The run collects as usual. Once every
channel's messages are saved, it downloads the files of the messages it covered:

| Run | Files it downloads |
|---|---|
| Full or window run | the files of the messages it kept |
| `--update` | the files of the new messages it merged (all of them for a channel collected in full) |
| Metadata only | none: the media options are refused with `--limit-download-to-channel-metadata` |

```
python main.py collect --batch-file ./channels.txt --include-media
python main.py collect --telegram-channel example_channel --update --include-media --media-types image,video
```

**After collecting: `download-media`.** It reads the saved histories of a collected
folder (in any storage mode) and downloads every file of a selected type that is not
stored yet, including files that failed or were skipped by a size limit before. It
fetches each channel's messages again, 100 at a time, for fresh file references, and
downloads each batch's files before fetching the next.

```
# every saved history in the folder
python main.py download-media --output ./output/data

# one channel, images only, at most 2 GiB in this run
python main.py download-media -o ./output/data --telegram-channel example_channel --media-types image --max-media-total 2G
```

`--telegram-channel` and `--batch-file` name saved histories, as they were collected;
by default every saved history is covered. `--source auto|files|sqlite` chooses which
copy of the histories to read, as for the builders. When every selected file is
already stored, `download-media` says so and exits 0 without logging in.

Media options:

| Option | Commands | Effect |
|---|---|---|
| `--include-media` | `collect` | Turns media downloads on. The other media options need it. |
| `--media-types T[,T...]` | both | Only these kinds: `image`, `video`, `audio`, `file` (default: all four). |
| `--max-media-size SIZE` | both | Skip a file larger than SIZE. |
| `--max-media-total SIZE` | both | Receive at most SIZE in this run. |
| `--dry-run` | `download-media` | List what a run would request; offline, nothing written. |
| `--check` | `download-media` | Check the ledger, the stored files and the saved histories; offline, read only. |

Sizes are bytes, or a number with `K`, `M` or `G` (KiB, MiB, GiB). The request controls
(`--concurrency`, `--request-interval`, `--max-flood-wait`, `--max-retries`) and the
profile options work as for `collect`.

## What is downloaded

A message's own photo or document. Web page previews, thumbnails, other video
qualities, stories and paid media are not downloaded; contacts, locations, polls and
dice have no file. Everything stays in the saved message metadata either way.

| Kind | Folder | Includes |
|---|---|---|
| `image` | `images/` | photos (the largest size), image documents, image stickers and GIFs |
| `video` | `videos/` | videos, video notes, video stickers and GIFs |
| `audio` | `audios/` | audio files and voice notes |
| `file` | `files/` | every other document (PDF, archives, office files, ...) |

[media-ledger.md](media-ledger.md#classification) gives the exact rules and the
extension table.

## Where the files go

```
<output>/
├── collection-identity.json          files folders only: ties the ledger to this folder's data
└── downloaded-media/
    ├── media.sqlite3                 the media ledger
    ├── <channel_id>/images/<message_id>-<hash>.<ext>
    ├── <channel_id>/videos/...
    ├── <channel_id>/audios/...
    ├── <channel_id>/files/...
    └── .partial/<run_uuid>/          downloads in progress (never complete)
```

- `<channel_id>` is the numeric channel ID, so a renamed channel keeps its folder.
  `<hash>` is the first 16 hex digits of the file's SHA-256.
- A file is stored once per channel and content. Another message with the same file
  points at the same stored file through the ledger; no link is made.
- The ledger `media.sqlite3` records each run, each selected file (its message, type,
  state and reason), each stored file (size and SHA-256), and each download attempt.
  It is a separate SQLite file in every storage mode: the collected data (files or
  `telegram-tracker.sqlite3`) is never changed by media downloads.
- `collect --include-media` adds a `media` block to `collection-status.json` and a
  `> Media:` line to its summary. `download-media` writes nothing outside
  `downloaded-media/` except, in a files folder, `collection-identity.json` the first
  time.
- Leave `downloaded-media/` as it is and copy files out of it to work with them. A
  stored file that is moved, renamed or edited there is reported by `--check` as
  missing or differing, and no run downloads it again: its file stays recorded as
  stored.

## Size limits

There is no limit unless you set one.

- `--max-media-size SIZE`: a file whose reported size is larger is skipped with no
  request (`over_file_limit`). A file of unknown size is stopped once more than SIZE
  has arrived; its partial copy is removed and it is skipped the same way.
- `--max-media-total SIZE`: every byte received in the run counts, restarts included,
  and the run never receives more. Each download reserves its size, rounded up to whole
  512 KiB requests, before its first request; a file that does not fit is skipped
  (`over_run_limit`), and later smaller files can still fit. With several downloads at
  once, which files fit can differ from run to run.

Skipped files are recorded; a later `download-media` without the limit downloads them.

## Concurrency and speed

Files are requested 512 KiB at a time, and each of those requests goes through the
same request policy as the collection: the request interval, the flood-wait limit and
the retries apply to every one of them.

- **`collect --include-media`** starts downloading only after every channel's messages
  are saved. Run completion, aggregate finalization and lock release wait for the
  downloads to finish. It downloads as many files at
  once as it collected channels at once: `--concurrency`, capped at the number of
  channels in the run. A single `--telegram-channel` therefore downloads one file at a
  time, whatever `--concurrency` says.
- **`download-media`** downloads `--concurrency` files at once (1-10, default 1),
  whatever the number of channels.
- **Request pacing.** Each application-level file request covers up to 512 KiB.
  At the default interval (0.25 s), admitting only file requests corresponds to
  about 2 MiB/s of requested chunks within this run, or 5 MiB/s at the smallest
  interval (0.1 s). This is not a network bandwidth cap or a measured Telegram rate:
  other application requests share the policy, and Telethon's internal requests
  are outside it (see [Known limits](#known-limits)).

## Profiles

`download-media` downloads a channel's files only with the profile that collected its
saved history, by the rule `collect --update` uses: the same profile ID; else the same
profile name; a history without a profile record only with the default profile. Another
profile's channel is refused before any request for it, with the profile to use
(`--profile NAME`), and the other channels go on. A channel whose stored data holds no
access hash for it is refused too; collect it again.

## Dry run

`download-media --dry-run` lists, per channel and kind, what a run would request: the
number of files, the bytes of those with a known size, the files of unknown size, and
the files over `--max-media-size`. It reads only the saved histories and the ledger,
needs no login, and writes nothing.

## Checking a folder

`download-media --check` compares the ledger, the stored files and the saved histories,
offline and read only. It reports:

- whether the ledger belongs to this folder's data;
- each stored file whose size or SHA-256 no longer matches, or that is missing;
- each saved media reference by kind and state (stored, failed, skipped, requested, or
  not selected by any run), and references that a `collect --include-media` run selected
  but stopped before recording;
- files and records without a counterpart, entries that are not generated names (never
  followed), leftover partial downloads, and runs whose process ended while running.

It reads every stored file, so it takes time on large folders. It holds the output
folder's lock while it reads, like a collection, and takes only `--output` and
`--source`. [media-ledger.md](media-ledger.md#checking-a-folder) describes each check.

## Exit codes

| Code | `collect --include-media` | `download-media` | `download-media --dry-run` / `--check` |
|---|---|---|---|
| `0` | every target collected, and every selected file stored or skipped by a size limit | every selected file stored or skipped by a size limit | listed / no problem found |
| `1` | as without media, or a file failed, or the downloads stopped | a file failed, a channel was refused or could not be fetched again, a named history was not found, the run stopped, or the folder cannot be read | a named history was not found, or the folder cannot be read / problems found (listed on stderr) |
| `2` | argument errors | argument errors | argument errors |
| `130` | interrupted (Ctrl+C) | interrupted (Ctrl+C) | interrupted (Ctrl+C) |

## Interruptions and failures

A completed file is never deleted. What a problem does to the file in progress, and to
the run:

| Event | The file in progress | The run |
|---|---|---|
| Ctrl+C | its partial copy is removed; recorded as failed (`interrupted`) | stops, exit 130; the files not yet attempted stay requested; `collect` keeps every saved history |
| Process killed or crashed | its partial copy stays; still recorded as downloading | the next run that writes the ledger records the run as interrupted and removes its partial copies, then downloads what is missing |
| Flood wait within `--max-flood-wait` | waits, then continues where it stopped | all requests wait |
| Flood wait over it, or retries used up by flood waits | failed (`flood_stop`) | stops, exit 1 (`collect` keeps every saved history) |
| Telegram rejects the session | failed (`request_failed`) | stops, exit 1; log in again |
| A request keeps failing | failed (`request_failed`) | goes on |
| Expired file reference | the message is fetched again once and the file starts over | goes on |
| Message deleted, or its media changed | failed (`message_gone`, `media_changed`) | goes on |
| Disk full | failed (`disk_full`) | downloads stop, exit 1; `collect` still saves every channel |

After a killed `collect`, `collection-status.json` still says `running`.
`download-media` and `--check` then refuse the folder, because only a collection can
tell a stopped run from one still running: delete the leftover lock files the next run
names, run the collection into the same folder again, then run `download-media`.

Failed and skipped files keep their reason in the ledger; `download-media` tries them
again. `--check` lists what a stopped run left behind.

## Known limits

The media features are validated offline, with a fake Telegram client that serves real
Telethon message and file objects (see [compatibility.md](compatibility.md)). What that
does not cover:

- **Other platforms.** The recorded media tests ran on Windows 11 with Python 3.14.7.
  The earlier cross-OS support matrix predates media delivery; Linux/macOS media
  behavior needs its own recorded CI or operator result.
- **Live Telegram.** No file has been downloaded from Telegram by these tests. The
  number of messages that one `GetMessages` request may fetch again (100 here), the
  access hash stored in a real channel's data, and the refresh of an expired file
  reference after fetching a message again are confirmed only by a live check.
- **Throughput.** The chunk-rate calculation describes application request admission;
  real download rates and the per-account limits Telegram applies to files were
  not measured.
- **Requests not counted.** The network library connects to the data center that holds
  a file and repeats a request after a `FILE_MIGRATE` answer or a timeout on its own;
  those requests are not paced or counted.
- **Memory.** `collect --include-media` keeps where to fetch each selected file, from
  the page that brought it until the run's downloads end: about 300 bytes per file
  (30 MB for 100,000 files, measured on synthetic messages), on top of what the
  collection holds.
- **Files folders merged by `--update`.** Files record which run wrote a history only
  when that run replaced or created it, so for a history merged by `--update` the
  ledger cannot name the collection run of each file (`source_run` is empty), and
  `--check` cannot tell a reference a stopped run selected from one nothing selected.
