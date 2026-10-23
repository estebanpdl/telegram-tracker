# Media ledger

Downloaded photos, videos, audio and documents are stored as plain files under
`<output>/downloaded-media/`, and recorded in a separate SQLite file, the media
ledger: `<output>/downloaded-media/media.sqlite3`. This page is the contract for
that folder and that file. The DDL is [media-ledger.sql](media-ledger.sql).

The ledger is the same in every storage mode (`files`, `sqlite`, `both`). The
collected data is never changed by it: the data database stays schema version 1
(see [sqlite-schema.md](sqlite-schema.md)) and the history files stay as they are.
No file bytes are stored in SQLite, in any mode.

## Folder layout

```
<output>/downloaded-media/
  media.sqlite3                                   the ledger
  <channel_id>/images/<message_id>-<hash>.<ext>   one file per stored content
  <channel_id>/videos/...
  <channel_id>/audios/...
  <channel_id>/files/...
  .partial/<run_uuid>/<channel_id>-<message_id>-<media_id>.part
```

- `<channel_id>` is the numeric channel ID, stable across renames. `<hash>` is the
  first 16 hex digits of the file's SHA-256. `<ext>` comes from the classification
  below.
- Names are generated from those integers, hex digits and an allowed extension only.
  The file name sent with a document is never used for a path; it stays in the
  message's raw metadata.
- The folder is not `<output>/media/`: `media` is a valid channel name, and files
  mode writes each channel's history to `<output>/<channel>/`. No channel name can be
  `downloaded-media`.
- No generated name can match `*_messages.json`, the pattern of history files.
- `.partial/<run_uuid>/` holds a run's downloads in progress. A file there is never
  complete.

**Links and confinement.** Every folder from `downloaded-media` down
(`downloaded-media`, the channel and type folders, `.partial` and each
`.partial/<run_uuid>`) is checked with `lstat` before use. A symlink, a junction or
another reparse point, or anything that is not a folder, is refused, and the folder
must resolve inside `downloaded-media`. The output folder itself may be reached
through a link. A final file name is inspected with `lstat` as well: a link or a
non-regular file there is a conflict (`path_conflict`), never followed or adopted. A
regular file there with the same size and SHA-256 is adopted (a leftover of a run
that stopped before recording it); with other content it is a conflict and is never
overwritten.

**One file per content.** A file is stored once per channel and content, identified
by (`channel_id`, `sha256`). Another message with the same content gets its own
`items` row pointing at the same blob. No hard links or symlinks are made. The
media ID only hints that a file may already be stored: reuse needs the stored file's
size and SHA-256 to match.

## Classification

Only a message's own photo (`MessageMediaPhoto`) or document
(`MessageMediaDocument`) is downloaded. Web page previews, games, invoices, paid
media, stories, thumbnails, other video qualities and cached or stripped thumbnails
are not; contacts, locations, polls and dice have no file. All of them stay in the
raw metadata.

The first matching rule wins:

| Media | Detected by | `type` | `subtype` |
|---|---|---|---|
| Photo | `MessageMediaPhoto` with a `Photo` | `image` | `photo` |
| Sticker | `DocumentAttributeSticker` | by MIME | `sticker` |
| Voice note | `DocumentAttributeAudio` with `voice` | `audio` | `voice_note` |
| Video note | `DocumentAttributeVideo` with `round_message` | `video` | `video_note` |
| GIF | `DocumentAttributeAnimated` | by MIME | `gif` |
| Video | `DocumentAttributeVideo` | `video` | `video` |
| Audio | `DocumentAttributeAudio` | `audio` | `audio` |
| Other document | none of the above | by MIME | `document` |

"By MIME" means `image`, `video` or `audio` for those MIME prefixes, else `file`. The
`type` names the folder: `images`, `videos`, `audios` or `files`.

A photo is stored at its largest downloadable size (`PhotoSize`, or the largest step
of a `PhotoSizeProgressive`), as `jpg`; `photo_size` records that size's type. A
document's extension comes from its MIME type through this table. Any other MIME
type, or none, gets `bin`. Formats whose usual viewer runs embedded code
(executables, scripts, HTML, SVG, macro-capable Office formats) are left out on
purpose. Files are saved, never opened.

| MIME type | Extension |
|---|---|
| `image/jpeg` | `jpg` |
| `image/png` | `png` |
| `image/gif` | `gif` |
| `image/webp` | `webp` |
| `image/heic` | `heic` |
| `image/heif` | `heif` |
| `image/avif` | `avif` |
| `image/bmp` | `bmp` |
| `image/tiff` | `tif` |
| `video/mp4` | `mp4` |
| `video/webm` | `webm` |
| `video/quicktime` | `mov` |
| `video/x-matroska` | `mkv` |
| `video/mpeg` | `mpg` |
| `video/3gpp` | `3gp` |
| `video/x-msvideo` | `avi` |
| `audio/ogg` | `ogg` |
| `audio/opus` | `opus` |
| `audio/mpeg` | `mp3` |
| `audio/mp4` | `m4a` |
| `audio/x-m4a` | `m4a` |
| `audio/aac` | `aac` |
| `audio/flac` | `flac` |
| `audio/x-flac` | `flac` |
| `audio/wav` | `wav` |
| `audio/x-wav` | `wav` |
| `audio/webm` | `weba` |
| `application/x-tgsticker` | `tgs` |
| `application/pdf` | `pdf` |
| `application/epub+zip` | `epub` |
| `application/zip` | `zip` |
| `application/x-7z-compressed` | `7z` |
| `application/vnd.rar` | `rar` |
| `application/x-rar-compressed` | `rar` |
| `application/gzip` | `gz` |
| `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | `docx` |
| `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | `xlsx` |
| `application/vnd.openxmlformats-officedocument.presentationml.presentation` | `pptx` |
| `application/vnd.oasis.opendocument.text` | `odt` |
| `text/plain` | `txt` |
| `application/json` | `json` |

MIME types are compared in lower case, without parameters (`; charset=...`).

## Requirements and identity

- SQLite 3.31 or newer with the JSON functions, as for the data database. Each
  connection, the creating one included, turns on `PRAGMA foreign_keys`,
  `PRAGMA recursive_triggers` and a busy timeout. Recursive triggers make `REPLACE`
  conflict resolution fire the never-delete triggers, so `INSERT OR REPLACE` cannot
  rewrite a blob or a complete item.
- `PRAGMA application_id` is `1413960781` (`0x5447544D`, 'TGTM'). A file is
  classified from its header before any connection, so a file that is not a ledger
  (a data database, a Telethon session, anything else) is never opened.
- `PRAGMA user_version` holds the ledger version, `1`. A newer or older version is
  refused and left untouched.
- Every connection is checked against the DDL, object by object, and must hold
  exactly one `ledger_meta` row.
- A new ledger is built in a sibling temporary file, checked, and then published
  without replacing anything at the path. A file that appeared meanwhile is kept and
  the creation is refused.
- Ledger rows change only through single short transactions on one connection per
  run; no transaction stays open while a download waits for the network.

## Link to the collected data

**Dataset identity.** `ledger_meta.dataset_uuid` binds the ledger to the collected
data of its folder:

- `sqlite` and `both` folders: the `dataset_uuid` of `collection-storage.json`,
  which is the data database's `schema_meta.dataset_uuid`.
- `files` folders have no storage manifest, so they get
  `<output>/collection-identity.json`:

  ```
  {"format": 1, "dataset_uuid": "<uuid4>", "created_at": "<UTC ISO 8601>"}
  ```

  It is written once, before the folder's ledger is created, and never changed. It
  is not a storage manifest: the folder stays a `files` folder. It is read only in
  folders without `collection-storage.json`. A `files` folder that has a ledger but
  has lost its record gets no new one: a new UUID would never match the ledger, and
  since the record is never replaced, the original could no longer be restored.

The ledger is refused, and left untouched, when its `dataset_uuid` differs from the
folder's, when its `storage` differs from the folder's mode, or when a `files`
folder has a ledger but no `collection-identity.json`. Copying or moving the whole
output folder keeps the two together; a `downloaded-media` folder copied into
another output meets a missing or different identity.

**Join keys.** Each `items` row carries the message's `channel_id` and `message_id`,
the `media_id` (photo or document ID) and the raw `media_type`, which are the values
of `messages.channel_id`, `messages.message_id`, `messages.media_id` and
`messages.media_type` in the data database. `last_run_uuid` names the ledger run that
last handled the item, and `profile_name`/`profile_id` its profile; `source_run`
names the collection run whose history the item was selected from.

- **Data database:** `items (channel_id, message_id, media_id)` =
  `messages (channel_id, message_id, media_id)`. The view `message_media_references`
  has the same key and adds the profile of the run that collected the reference.
- **Files:** `<channel>/<channel>_messages.json` → `messages[].id` and
  `messages[].media.photo.id` or `messages[].media.document.id`. The channel ID is
  `channel_id` in `<channel>/<channel>_provenance.json`, else `full_chat.id` in
  `<channel>/<channel>.json`.

**What the ledger cannot guarantee.** A history write and the ledger rows for its
media are never one transaction: they live in different files. Ledger rows are
written only after the history they refer to is saved, so a row never names a
message that was not saved. A stop between the two leaves media references with no
ledger row; `media_runs.channels` records which channels a run wrote rows for, so
the two cases (not selected, and selected by a run that stopped) can be told apart.

## Tables

### ledger_meta

One row, written when the ledger is created and never changed (trigger
`ledger_meta_final`; no row is ever deleted, trigger `ledger_meta_kept`).

| Column | Meaning |
|---|---|
| `singleton` | Always 1. |
| `ledger_version` | 1. |
| `ledger_uuid` | This ledger's UUID. |
| `dataset_uuid` | The folder's dataset (see Link to the collected data). |
| `storage` | The folder's storage mode: `files`, `sqlite` or `both`. |
| `output_binding` | The canonical output folder when the ledger was created. Information only: a moved folder is not refused for it. |
| `created_at` | When the ledger was created (UTC). |
| `app_version` | The Telegram Tracker version that created it. |

### media_runs

One row per run that wrote to the ledger. Rows are never deleted (trigger
`media_runs_kept`). Index `media_runs_by_state` finds running runs.

| Column | Meaning |
|---|---|
| `run_uuid` | The run's UUID. |
| `command` | `collect` (collection with media) or `download-media`. |
| `collection_run` | For `collect`: the collection run, by its `run_uuid` in `sqlite`/`both` folders or its `started_at` in `files` folders. |
| `profile_name`, `profile_id` | The run's profile; NULL = default/unknown, as in the data database. |
| `state` | `running`, then `complete`, `partial` or `interrupted`. |
| `stop_reason` | Why the run stopped early, or how a dead run was found. |
| `started_at`, `finished_at` | UTC. `finished_at` stays NULL for a run whose process ended. |
| `selection` | JSON: `types`, `max_media_size`, `max_media_total` (bytes or null). |
| `channels` | JSON array of the channel IDs whose selected media the run recorded. |
| `app_requests` | File and lookup requests the application made. |
| `retries`, `flood_waits`, `refreshes` | Retried requests, flood waits, file reference refreshes. |
| `bytes_received` | Every byte received, restarts included. |
| `app_version` | The Telegram Tracker version of the run. |

### blobs

One row per stored file. A blob never changes and is never deleted (triggers
`blobs_final`, `blobs_kept`); the path is unique.

| Column | Meaning |
|---|---|
| `channel_id` | The channel the file belongs to. |
| `sha256` | SHA-256 of the file, lower-case hex. |
| `bytes` | File size. |
| `type` | `image`, `video`, `audio` or `file` (the folder). |
| `mime_type` | MIME type of the item that first stored it. |
| `ext` | Extension, without the dot. |
| `path` | Path relative to `downloaded-media`, with `/` separators. |
| `first_run_uuid` | The run that stored it. |
| `created_at` | When it was recorded (UTC). |

### items

One row per selected media reference of a collected message, keyed like the
message's media. Rows are never deleted (trigger `items_kept`). Indexes:
`items_by_state`, `items_by_media` (the reuse hint by media ID) and `items_by_blob`
(the messages that share a file).

| Column | Meaning |
|---|---|
| `channel_id`, `message_id` | The message. |
| `media_id` | The photo or document ID. |
| `media_type` | Raw media constructor, as `messages.media_type`. |
| `media_kind` | `photo` or `document`. |
| `photo_size` | For a photo, the type of the size stored; NULL for documents. |
| `type`, `subtype` | Classification (see Classification). |
| `mime_type` | Normalized MIME type; `image/jpeg` for photos. |
| `ext` | Extension the file gets. |
| `expected_bytes` | Size the API reported; NULL = unknown. |
| `actual_bytes` | Size of the stored file; set when complete. |
| `sha256` | The blob, set when complete. |
| `state` | See Item states. |
| `reason` | Why it was skipped or failed; NULL otherwise. |
| `attempts` | Download attempts so far, across runs. |
| `source_run` | The collection run whose history it was selected from. |
| `first_run_uuid`, `last_run_uuid` | The run that first requested it and the last run that handled it; for a complete item, the run that completed it. |
| `profile_name`, `profile_id` | The last run's profile. |
| `updated_at` | Last change (UTC). |

### attempts

One row per download attempt. `n` numbers an item's attempts across runs. An attempt
is written when its download starts and ended once (trigger `attempts_ended_final`);
rows are never deleted (trigger `attempts_kept`). Index `attempts_by_run` finds a
run's attempts.

| Column | Meaning |
|---|---|
| `channel_id`, `message_id`, `media_id` | The item. |
| `n` | Attempt number. |
| `run_uuid` | The run that made it. |
| `started_at`, `ended_at` | UTC; `ended_at` is NULL while it runs. |
| `outcome` | `complete`, `skipped` or `failed`. |
| `reason` | The item's reason for `skipped` and `failed`. |
| `bytes_received` | Bytes received in this attempt. |
| `app_requests` | Requests the application made for it. |
| `refreshed` | 1 when its file reference was refreshed. |

## Item states

An item starts as `requested` (trigger `items_insert_requested`) and moves only
along these transitions (trigger `items_state_transition`). `complete` is final: a
complete item never changes again (trigger `items_complete_final`).

| From | To |
|---|---|
| `requested` | `requested`, `downloading`, `complete`, `skipped`, `failed` |
| `downloading` | `complete`, `skipped`, `failed` |
| `skipped` | `requested` |
| `failed` | `requested` |
| `complete` | none |

- `requested` → `complete` reuses a stored blob without downloading.
- `requested` → `requested`, `skipped` → `requested` and `failed` → `requested` are a
  later run selecting the item again.

| State | Reasons |
|---|---|
| `skipped` | `over_file_limit`, `over_run_limit` |
| `failed` | `reference_expired`, `message_gone`, `media_changed`, `flood_stop`, `request_failed`, `disk_full`, `storage_error`, `size_mismatch`, `path_conflict`, `stored_file_differs`, `interrupted` |

## Runs and recovery

A run writes to the ledger only while it holds the output folder's lock
(`.collection.lock`), so at most one writes at a time. When a run starts, any run still recorded as running
belongs to a process that ended. In the same transaction that records the new run,
such runs become `interrupted` (with a `stop_reason`), their `downloading` items
become `failed` with `interrupted`, and their open attempts are ended. Items they
had only requested stay `requested`. When a run finishes, its own items still
downloading are recorded the same way.

**Partial files.** Each run downloads into `.partial/<run_uuid>/`, and a file there
is never complete. With the folder locked, the `.partial/<run_uuid>/` folders
of other runs the ledger knows are leftovers: their generated `.part` files are
removed, then each folder once empty. Anything else there (another name, a folder, a
link) is kept and reported; links are never followed. Folders of runs the ledger
does not know are reported, never removed. Complete files are outside `.partial` and
are never removed.

## Byte limits

There is no limit by default. A user can set a per-file and a per-run limit, as a
number of bytes or with K, M or G (KiB, MiB, GiB).

- **Per file.** A file whose known size is above it is skipped with no request
  (`over_file_limit`). A file of unknown size is stopped once more than the limit
  has arrived, and is skipped with the same reason.
- **Per run.** Every byte received counts, restarts included. Downloads reserve
  before they request: a known size reserves its size rounded up to whole 512 KiB
  pulls (at least one) before the first request, and a file that does not fit is
  skipped (`over_run_limit`); an unknown size reserves 512 KiB before each pull and
  gives back what the pull did not use. A download never receives more than it
  reserved, so the bytes received never exceed the limit. Up to one pull's worth of
  the limit can stay unused, and with several downloads at once which files fit can
  differ from run to run; each skip is recorded.

## Example queries

Each message's media with its ledger state, from the data database. Run it on a
read-only connection opened with `uri=True`. A relative path in `ATTACH` resolves
against the working directory, so run it from the output folder or give an absolute
path.

```sql
ATTACH 'file:downloaded-media/media.sqlite3?mode=ro' AS media;
SELECT m.channel_id, m.message_id, m.media_type, m.media_id,
       coalesce(i.state, 'not selected') AS state, i.reason,
       b.path, b.bytes, i.sha256, i.last_run_uuid, i.profile_name
FROM messages m
LEFT JOIN media.items i ON i.channel_id = m.channel_id AND i.message_id = m.message_id
                       AND i.media_id = m.media_id
LEFT JOIN media.blobs b ON b.channel_id = i.channel_id AND b.sha256 = i.sha256
WHERE m.media_id IS NOT NULL
ORDER BY m.channel_id, m.message_id;
```

The same for a `files` folder, in Python:

```python
import json
import os
import pathlib
import sqlite3


def read_json(path):
    with open(path, encoding='utf-8') as reader:
        return json.load(reader)


def channel_id_of(output, channel):
    provenance = os.path.join(output, channel, f'{channel}_provenance.json')
    if os.path.exists(provenance):
        return read_json(provenance)['channel_id']
    return read_json(os.path.join(output, channel, f'{channel}.json'))['full_chat']['id']


def media_of(output, channel):
    '''(message_id, media_id, state, path) for each photo or document in the history.'''
    ledger = pathlib.Path(output, 'downloaded-media', 'media.sqlite3').resolve()
    con = sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True)
    try:
        rows = {
            (message_id, media_id): (state, path)
            for message_id, media_id, state, path in con.execute(
                'SELECT i.message_id, i.media_id, i.state, b.path FROM items i '
                'LEFT JOIN blobs b ON b.channel_id = i.channel_id AND b.sha256 = i.sha256 '
                'WHERE i.channel_id = ?', (channel_id_of(output, channel),))
        }
    finally:
        con.close()
    history = read_json(os.path.join(output, channel, f'{channel}_messages.json'))
    for message in history['messages']:
        media = message.get('media') or {}
        file = media.get('photo') or media.get('document')
        if isinstance(file, dict) and 'id' in file:
            state, path = rows.get((message['id'], file['id']), ('not selected', None))
            yield message['id'], file['id'], state, path
```

Files of one channel stored more than once (one blob, several messages):

```sql
SELECT b.path, b.bytes, count(*) AS messages
FROM blobs b JOIN items i ON i.channel_id = b.channel_id AND i.sha256 = b.sha256
GROUP BY b.channel_id, b.sha256
HAVING count(*) > 1
ORDER BY messages DESC;
```

Items by state and reason, per run that last handled them:

```sql
SELECT last_run_uuid, state, reason, count(*) AS items
FROM items
GROUP BY last_run_uuid, state, reason
ORDER BY last_run_uuid, state, reason;
```

Runs that ended without finishing, and what they left:

```sql
SELECT r.run_uuid, r.started_at, r.stop_reason, count(a.n) AS interrupted_attempts
FROM media_runs r
LEFT JOIN attempts a ON a.run_uuid = r.run_uuid AND a.reason = 'interrupted'
WHERE r.state = 'interrupted'
GROUP BY r.run_uuid
ORDER BY r.started_at;
```
