# SQLite database schema

This document describes schema version 1 of the Telegram Tracker data database: a
single SQLite file that holds the same collected data as the JSON, CSV and TXT
outputs, plus a record of every collection run.

The schema is defined in [sqlite-schema.sql](sqlite-schema.sql).
`tests/test_sqlite_schema.py` applies it to an empty database and checks it against
the synthetic payloads in `tests/fixtures/sqlite/variants.json`.

**Status:** `collect --storage sqlite` writes this database only, and
`collect --storage both` writes it together with the file outputs; the database is
always `telegram-tracker.sqlite3` in the `--output` folder (see the README, "Storage
modes"). The writer is implemented in `telegram_tracker.storage.sqlite` (schema
checks, transactions, history rules, profile checks, own-phone rule and upgrades, as
described below); `telegram_tracker.storage.dual` records the publication state of
`both` runs (`files_state`, `aggregate_files`, `destination_pending`). Applying the
DDL alone provides none of the writer's behavior.

## What the database contains

| Data | Tables | File output it mirrors |
|---|---|---|
| Collection runs, per-target outcomes, request statistics | `collection_runs`, `run_targets`, `run_requests` | `collection-status.json` |
| Channel metadata (latest per channel) | `channel_snapshots` | `<target>/<target>.json` |
| Message histories (current per channel) | `histories`, `messages`, `history_entities`, `peers` | `<target>/<target>_messages.json` |
| Users, chats and channels seen in any payload | `peers` | the `chats` and `users` arrays |
| Chat observations, counters, enrichment rows | `run_observations`, `run_counters`, `collected_chat_rows` | `chats.txt`, `counter.csv`, `collected_chats.csv` |
| Not-found and skipped chats | `collection_errors` | `_exceptions-*.txt` |
| Database identity and upgrades | `schema_meta`, `migrations` | — |

Payloads are stored as the API returns them, with no fields removed. This includes
phone numbers, access hashes, file references, names, photos and online status. See
[Personal data](#personal-data).

## Requirements and identity

- SQLite 3.31 or newer with the JSON1 functions (`json_extract`, `json_valid`,
  `json_type`). Python's `sqlite3` module reports the library version in
  `sqlite3.sqlite_version`. This is what the schema needs; a Python 3.14 build's
  `sqlite3` module can need a newer library before it loads at all. The oldest
  library tested is listed under Platform support in `docs/compatibility.md`.
- Every connection sets `PRAGMA foreign_keys = ON` and `PRAGMA busy_timeout = 5000`.
  The journal mode is SQLite's default rollback journal (`DELETE`).
- `PRAGMA application_id` is `1413960786` (`0x54475452`, the ASCII bytes `TGTR`).
  It marks the file as a Telegram Tracker data database. A Telethon `.session` file
  is also an SQLite database, but it has a different application ID and tables.
- `PRAGMA user_version` holds the schema version, `1`. The `schema_meta` row
  repeats it in `schema_version`.
- Timestamps written by the application (`started_at`, `observed_at`, ...) are UTC
  ISO 8601 text. Dates inside payloads keep the collector's format; see below.

## Raw JSON and computed columns

Each payload table has a `raw` (or `metadata`, `envelope`) column with the payload
as JSON text. Everything else about a message or entity is derived from it.

- **Serialization.** `raw` is the collector's JSON-safe form of the Telethon object:
  `to_dict()`, with datetimes and bytes converted by `str()`. Dates read
  `2024-01-02 03:04:05+00:00` (space separator, UTC offset). Bytes read as Python
  literals such as `b'\x01\x02'`; `ast.literal_eval` turns them back into bytes.
  `collection_runs.serializer_version` records this format (`1`).
- **Validity.** JSON columns must pass `json_valid`, so they hold strict JSON. Text that
  only parses as JSON5 is refused, even on SQLite versions where `json_extract`
  can read it.
- **Computed columns.** Query columns such as `messages.date`, `messages.fwd_peer_id`
  or `peers.phone` are `GENERATED ALWAYS AS (json_extract(raw, ...)) VIRTUAL`.
  SQLite computes them from `raw` when they are read, so they can't disagree with it.
  They take no space in the table; only the indexes on them are stored. They can't
  be written directly.
- **Missing and null values.** A computed column is `NULL` when the field is absent,
  JSON `null`, or belongs to another constructor. JSON `true`/`false` read as `1`/`0`.
  Objects decoded from an API response carry every flag of their constructor as
  `true` or `false`; optional fields that the response omits are `null`.
- **Unknown fields.** Fields and constructors that this schema doesn't project are
  kept in `raw` unchanged, including ones from newer API layers. Read them with
  `json_extract(raw, '$.path')`.
- **Envelope.** `histories.envelope` is the history response without its `messages`,
  `chats` and `users` arrays: the constructor, `count`, `pts`, `inexact`,
  `offset_id_offset`, `topics` and any other response fields.

## Tables

Every `run_id` column references `collection_runs`. Keys are listed after each
table. Peer IDs are typed: `(peer_kind, peer_id)` with `peer_kind` one of `channel`,
`chat` or `user`, because a user, a basic group and a channel can share a numeric ID.

### schema_meta

One row describing the database.

| Column | Meaning |
|---|---|
| `singleton` | Always `1`; makes the table single-row. |
| `schema_version` | Schema version (`1`); equals `PRAGMA user_version`. |
| `dataset_uuid` | Random UUID identifying this dataset. |
| `created_at`, `updated_at` | UTC creation and last-change times. |
| `app_version` | Application version that created or last migrated the file. |
| `output_binding` | Canonical output folder this database is bound to, or `NULL`. A collection refuses a database bound to another folder. |

Key: `singleton`.

### migrations

One row per applied schema upgrade.

| Column | Meaning |
|---|---|
| `version` | Schema version the step produced. |
| `applied_at` | UTC time of the step. |
| `app_version` | Application version that applied it. |
| `backup_path` | Backup taken before the step, or `NULL`. |

Key: `version`.

### collection_runs

One row per collection run, append-only.

| Column | Meaning |
|---|---|
| `run_id` | Integer run key used by every other table. |
| `run_uuid` | Random UUID of the run; also in `collection-status.json`. |
| `profile_name` | Local name of the collecting profile, or `NULL` if unknown. Lowercase letter first, then lowercase letters, digits, `_` or `-`; at most 32 characters. |
| `profile_id` | Opaque profile UUID (36 characters), or `NULL`. |
| `state` | `running`, `complete`, `partial` or `interrupted`. |
| `mode` | `full`, `window`, `update` or `metadata_only`. |
| `storage` | `sqlite` (database only) or `both` (database and files). |
| `started_at`, `finished_at` | UTC start and end times; `finished_at` is `NULL` while running. |
| `parameters` | JSON object of the normalized run options (window, concurrency, request controls). |
| `summary` | JSON summary written at the end of the run, or `NULL`. |
| `stop_reason` | Why the run stopped early, or `NULL`. |
| `app_version`, `telethon_version` | Versions that ran the collection. |
| `serializer_version` | Payload serialization format (`1`). |
| `aggregate_files` | In `both` mode: `pending` while the aggregate files are being published, then `written`. Always `NULL` in `sqlite` mode. |

Key: `run_id`; `run_uuid` is unique. Indexes: `runs_by_state` (`state`, `started_at`);
`runs_aggregate_pending`, partial on `aggregate_files = 'pending'`.

The profile columns never hold the account's phone number, API ID or hash, or
Telegram user ID.

### run_targets

One row per input target of a run, in input order.

| Column | Meaning |
|---|---|
| `run_id`, `input_index` | Run and 0-based position of the target in the input. |
| `input` | Target as given (username or link). |
| `status` | `pending`, `succeeded`, `failed`, `duplicate` or `cancelled`. |
| `alias_of` | For a duplicate, the input it repeats. |
| `channel_id` | Resolved channel ID, or `NULL` if it didn't resolve. |
| `history_mode` | `replaced`, `created`, `merged`, `unchanged` or `not_collected`. |
| `min_id` | Lower message-ID bound used by `--update`. |
| `messages` | History size after this run. |
| `participants_count` | Channel size as seen by this run. |
| `reason` | Sanitized failure reason. |
| `takeover` | `1` when a full or window run replaced a history that belonged to another profile (see [Runs and provenance](#runs-and-provenance)). |
| `previous_profile_name`, `previous_profile_id` | That history's previous profile, in the `collection_runs` format; `NULL` unless `takeover = 1`. |
| `files_state` | In `both` mode: `pending` while the target's files are being published, then `written`. `NULL` in `sqlite` mode. |

Key: (`run_id`, `input_index`). Indexes: `run_targets_by_channel` (`channel_id`,
`run_id`); `run_targets_files_pending`, partial on `files_state = 'pending'`.
Triggers `run_targets_files_state_insert` and `run_targets_files_state_update`
reject a non-null `files_state` on a run whose `storage` isn't `both`.

### run_requests

Request statistics per run and method class.

| Column | Meaning |
|---|---|
| `run_id` | Run. |
| `method_class` | `resolve`, `channel`, `enrichment` or `history`. |
| `requests`, `retries` | Application request-policy attempts and retries; not a count of wire requests or Telethon-internal retries. |
| `flood_waits`, `flood_wait_seconds` | Flood waits received and their total length. |

Key: (`run_id`, `method_class`); a `WITHOUT ROWID` table.

### run_observations

Every chat observation of a run, in the order the writer replayed them.
`chats.txt` is derived from this table.

| Column | Meaning |
|---|---|
| `run_id`, `seq` | Run and observation order. |
| `input_index` | Target the observation came from; references `run_targets`. |
| `kind` | `channel_request` (the target itself), `from_messages` (a chat in a history page) or `linked` (a linked chat from enrichment). |
| `page` | History page number for `from_messages`. |
| `channel_id` | Observed public chat. |
| `username` | Its username as observed. |
| `parent_channel_id` | For `linked`, the chat whose response listed it. |

Key: (`run_id`, `seq`); a `WITHOUT ROWID` table. Index: `observations_by_channel`
(`channel_id`, `run_id`).

### run_counters

`counter.csv` of one run.

| Column | Meaning |
|---|---|
| `run_id`, `channel_id` | Run and counted chat. |
| `row_order` | Row position in `counter.csv`. |
| `username` | Chat username. |
| `counter`, `from_messages`, `channel_request` | The CSV's count columns. |
| `targeted_by` | JSON array: `"self"` or the usernames of the chats that led to this one, in order. |
| `sources` | JSON array of the target inputs that reached this chat, in order. |

Key: (`run_id`, `channel_id`); (`run_id`, `row_order`) is unique.

### collected_chat_rows

Rows of `collected_chats.csv` written by one run. A channel can appear more than
once in a run, so `channel_id` is not a key.

| Column | Meaning |
|---|---|
| `run_id`, `row_index` | Run and row position. |
| `channel_id` | Chat the row describes. |
| `metadata` | JSON object of the row's chat fields as written to the CSV, including `access_hash`. |

Key: (`run_id`, `row_index`).

### collection_errors

Targets and chats that weren't found or were skipped.

| Column | Meaning |
|---|---|
| `run_id`, `error_index` | Run and error order. |
| `category` | `target_not_found`, `enrichment_not_found` or `metadata_skipped`. |
| `input_index` | Target involved, if any. |
| `peer_kind`, `peer_id` | Chat involved, if known. |
| `reason` | Sanitized reason. |

Key: (`run_id`, `error_index`).

### peers

The latest stored object for every user, chat and channel listed in a history or a
channel snapshot (their `chats` and `users` arrays). Chats found through enrichment
are stored as rows in `collected_chat_rows`.

| Column | Meaning |
|---|---|
| `peer_kind`, `peer_id` | Typed key. |
| `constructor` | `User`, `UserEmpty`, `Chat`, `ChatForbidden`, `Channel`, `ChannelForbidden`, ... |
| `is_min` | `1` for a `min` object (a partial copy the API embeds in other payloads). |
| `raw` | The object as returned. |
| `run_id` | Run whose response supplied `raw`. |
| `first_seen_run`, `last_seen_run` | First and latest runs that saw the peer at all. |
| `access_hash` | Computed: the object's access hash. |
| `title` | Computed: chat or channel title. |
| `username` | Computed: main username (additional ones are in `raw` under `usernames`). |
| `first_name`, `last_name` | Computed: user names. |
| `phone` | Computed: user phone number, when the API returned it. |
| `photo_id` | Computed: current profile or chat photo ID. |
| `status` | Computed: online-status constructor, such as `UserStatusOffline` or `UserStatusRecently`. |
| `status_was_online` | Computed: last-seen time for `UserStatusOffline`. |
| `broadcast`, `megagroup` | Computed: channel flags. |
| `bot`, `deleted` | Computed: user flags. |

Key: (`peer_kind`, `peer_id`). Indexes: `peers_by_username` and `peers_by_phone`,
partial on non-null values. Usernames change and aren't unique over time, so they
are never keys.

A `min` object never replaces a full one. A newer full object replaces the stored one
and its `run_id`.

### channel_snapshots

The latest full-channel response (`<target>.json`) per channel.

| Column | Meaning |
|---|---|
| `channel_id` | Channel. |
| `run_id` | Run that wrote the snapshot. |
| `observed_at` | UTC time the run stored the snapshot. |
| `raw` | The response as returned, including its `chats` and `users` arrays. |
| `about` | Computed: channel description. |
| `participants_count` | Computed: member count. |
| `linked_chat_id` | Computed: linked discussion group or channel. |

Key: `channel_id`.

### histories

One row per channel whose history is stored. No row means no history; a row with
`message_count = 0` is a successfully collected empty history.

| Column | Meaning |
|---|---|
| `channel_id` | Channel. |
| `run_id` | Last run that changed the history. |
| `profile_name`, `profile_id` | Profile whose view of the channel this history is, in the `collection_runs` format; `NULL` = unknown. |
| `mode` | How it was last changed: `replaced`, `created` or `merged`. |
| `window_bounds` | JSON of the window options of the last replace; `NULL` for a full collection. |
| `envelope` | The response without `messages`, `chats` and `users`. |
| `message_count` | Messages currently stored for the channel. |
| `updated_at` | UTC time of the last change. |

Key: `channel_id`.

### history_entities

Which chats and users each history's payload listed, in payload order. The objects
themselves are in `peers`.

| Column | Meaning |
|---|---|
| `channel_id` | History. |
| `list` | `chats` or `users`. |
| `ordinal` | Position in that list. |
| `peer_kind`, `peer_id` | The entity; references `peers`. |

Key: (`channel_id`, `list`, `ordinal`); (`channel_id`, `peer_kind`, `peer_id`) is unique.

### messages

One row per stored message, keyed by channel and message ID.

| Column | Meaning |
|---|---|
| `channel_id`, `message_id` | Channel and message ID within it. |
| `run_id` | Run that wrote `raw`. |
| `raw` | The message as returned (`Message`, `MessageService` or `MessageEmpty`). |
| `constructor` | Computed: the message constructor. |
| `date`, `edit_date` | Computed: post and last-edit dates, in the payload format. |
| `text` | Computed: message text; `NULL` for service and empty messages. |
| `sender_kind`, `sender_id` | Computed: typed author from `from_id`; `NULL` for channel posts without an author. |
| `post_author` | Computed: author signature. |
| `fwd_peer_kind`, `fwd_peer_id` | Computed: typed origin of a forward; `NULL` when the origin is hidden or imported. |
| `fwd_channel_post` | Computed: original post ID of a forwarded channel post. |
| `fwd_date` | Computed: original date of a forward. |
| `reply_to_msg_id`, `reply_to_top_id` | Computed: replied message and thread top. |
| `views`, `forwards` | Computed: view and forward counts. |
| `replies` | Computed: comment or reply count. |
| `discussion_id` | Computed: discussion group holding the comments. |
| `grouped_id` | Computed: album ID shared by the messages of one album. |
| `media_type` | Computed: media constructor, such as `MessageMediaPhoto`. |
| `media_id`, `media_access_hash`, `media_file_reference` | Computed: ID, access hash and file reference of a photo or document. |
| `contact_phone`, `contact_user_id` | Computed: phone number and user ID of a shared contact. |
| `action_type` | Computed: service-message action constructor. |

Key: (`channel_id`, `message_id`); its index also serves the newest-ID lookup
(`max(message_id)`). Indexes: `messages_by_date` (`channel_id`, `date`,
`message_id`); `messages_by_forward` (`fwd_peer_kind`, `fwd_peer_id`), partial on
forwards; `messages_by_run` (`run_id`).

References from messages to peers (senders, forward origins, discussion groups) are
not foreign keys. A message can refer to a chat or user that no payload included.

## Views

| View | Contents |
|---|---|
| `collected_chats_all` | Every `collected_chat_rows` row of every run with that run's counters: the cumulative `collected_chats.csv`. |
| `chat_ids_all` | Observed chat IDs in first-occurrence order across runs: the cumulative `chats.txt`. |
| `destination_pending` | Targets and runs whose file publication is still `pending`. |
| `peer_access_hashes` | Every stored peer access hash with the profile of the run that collected it. |
| `message_media_references` | Every message photo or document reference with the profile of the run that collected it. |

## How runs change the data

Each channel is written in one transaction, so a failure leaves earlier channels
and the channel's previous state intact. Run tables (`collection_runs`, `run_*`,
`collected_chat_rows`, `collection_errors`) keep the rows of every run; a run adds
and updates only its own rows. `peers`, `channel_snapshots`, `histories`,
`history_entities` and `messages` hold the current state.

- **Full run.** The channel's `messages` and `history_entities` rows are deleted and
  the collected history is inserted. `histories` gets `mode = 'replaced'`,
  `window_bounds = NULL`, the envelope, the count, the run, and the run's profile.
- **Window run** (`--min-id`, `--max-id`, `--since`, `--until`, `--max-messages`).
  The same replace, with the window options in `window_bounds`. Messages outside the
  window are not kept. A window that matches nothing leaves an empty history
  (`message_count = 0`).
- **`--update`.** The newest stored ID is `max(message_id)` for the channel. Newer
  messages are fetched and upserted on (`channel_id`, `message_id`), so a fetched
  copy replaces the stored one and its `run_id`. Earlier messages stay. In
  `history_entities`, chats and users are united by typed key, the fetched ones first
  and then the earlier ones not fetched again, as in the messages file. The fetched
  envelope replaces the stored one. `histories.mode` becomes `merged`, and its
  profile and `window_bounds` stay unchanged. When no new message arrives,
  `histories`, `messages` and `history_entities` are not touched and the target's
  `history_mode` is `unchanged`; the channel snapshot and its peers are stored as in
  any run. A channel without a `histories` row is collected in full and recorded as
  `created`. A stored history that can't be read fails that target only.
- **Metadata-only run.** Writes `channel_snapshots` and `peers` only. A history is
  stale when its `run_id` is older than the snapshot's.
- **Peers.** A peer listed again gets `last_seen_run` of the new run. A full object
  replaces the stored one and its `run_id`; a `min` object replaces only another
  `min` object.
- **Run records.** A run's `collection_runs` row (state `running`) and its
  `run_targets` rows (`pending`) are written when the run opens the database. Each
  channel's transaction also sets its target's outcome. When the run ends, its
  observations are written in one transaction, then its counters, collected-chat
  rows, errors, request statistics, the remaining target outcomes and its end state in
  another. If that last transaction fails, the run is marked `interrupted` if
  possible; its observations stay. A run whose process stopped before recording its
  end (for example, killed) stays `running` until the next collection into the folder
  holds the database lease: that run records it as `interrupted`, with a
  `stop_reason` starting with `stale:` and `finished_at` left `NULL`, in the
  transaction that records itself. Its rows are kept.
- **Both sinks.** In `both` mode the files hold the same fields as the database. Before
  an `--update`, the stored history's newest ID and message-ID set must agree in the
  database and the files; a channel whose two copies disagree, or that exists in only
  one, fails with recovery guidance while the other channels continue.
  `files_state` and `aggregate_files` stay `pending` until the files are published; a
  `pending` row blocks further collection into that destination.

Edits and deletions of older messages are not detected by `--update`. A full or
window run refreshes them.

Entity rows are shared: `history_entities` records membership and order, and
`peers` holds one object per entity. A history read back from the database lists
its chats and users with their latest stored objects, which can be newer than the
copies in that history's original response.

## Runs and provenance

Every row that stores a payload has a `run_id`. Through `collection_runs`, it names
the run, its time, its options and its profile.

- **Profiles.** `collection_runs.profile_name` and `profile_id` identify the profile
  that ran the collection. Both are nullable: `NULL` means unknown, and a database
  works without profiles.
- **History ownership.** `histories.profile_name` and `profile_id` record whose view
  a history is. `--update` merges only into a history of the same profile:
  - if the history has a `profile_id`, the run's must match;
  - if it has a `profile_name` but no `profile_id`, the names must match;
  - if it has neither, only the `default` profile, or a run without a profile, may merge.

  Otherwise that channel fails before its first history request, with guidance to run
  a full or window collection or use a fresh destination. A merge never changes the
  history's profile.
- **Takeovers.** A full or window run replaces any history and records its own
  profile. If the previous profile would have refused this run's `--update`, the
  target row gets `takeover = 1` and the previous profile, and the run summary
  reports it.
- **Account-scoped values.** Access hashes and file references are valid only for
  the Telegram account that received them. A `min` object's access hash is also
  valid only in the context it came from. Other values are relative to the
  collecting account, too: `ChannelFull` read counters and `notify_settings`,
  `Channel.left` and admin rights, `Message.out`, `User.contact`. Their collection
  profile is recorded through the row's `run_id`:
  - for `peers`, the run that supplied `raw`;
  - for `messages`, the run that wrote the message;
  - for `channel_snapshots` and `collected_chat_rows`, the run that wrote the row.

  `peer_access_hashes` and `message_media_references` attach that profile to each
  value. When several profiles write to one database, `peers` and
  `channel_snapshots` keep the last writer's copy, so an entity's access hash can
  change account between runs. A profile name or opaque ID records local provenance;
  it does not verify the Telegram account currently using that profile. Unknown
  provenance stays unknown. Before reusing a stored hash, establish that the session
  belongs to the account that received it and that any required `min` context is
  available. The collector itself resolves targets through its session and
  doesn't read access hashes from stored outputs.

## Schema versioning and backups

- On opening, a file is classified from its 100-byte SQLite header (application ID
  and `user_version`) before any connection is made. Paths naming a Telethon session
  file (`*.session` and its `-journal`, `-wal` and `-shm` files) or `config.ini`, and
  paths that aren't a plain file path (`:memory:`, `file:` URIs, `.sql` files,
  directories), are refused without being opened. A missing file is created only
  when a run opens the database for writing: it is built in a temporary file next to
  it and published once complete, never over a file that appeared meanwhile.
  Publication uses a hard link, or a non-replacing rename on Windows. If hard-link
  publication fails on another platform, creation is refused; choose a filesystem
  supporting hard links.
  A file with this application ID and `user_version = 1` is used after its tables,
  indexes, views and triggers are checked against the schema and its `schema_meta`
  row is read. Collection refuses a file with another version, older or newer, and
  never upgrades it. Any other SQLite database, including a Telethon session, is
  refused and left untouched; an empty file is refused, and a file that isn't an
  SQLite database is refused as damaged. An SQLite library older than 3.31 or without
  JSON1 is refused.
- An upgrade from version N to N+1 runs offline, holding the database lease
  (`telegram-tracker.sqlite3.lock`, as a collection does), and makes a backup first: a consistent
  copy through SQLite's backup API, named `<name>.v<N>-<UTC time>.bak.sqlite3` next
  to the database, then checked with `PRAGMA integrity_check`. Each step runs in one
  transaction that also sets `user_version`, updates `schema_meta`, inserts its
  `migrations` row and checks foreign keys, so a failed step leaves the previous
  version. Backups are never deleted automatically, and there are no downgrades.
  Version 1 is the first schema version, so no upgrade exists yet and there is no
  command that runs one; a file with this application ID and a version below 1 is
  refused.
- To back up the database yourself, don't copy the file while a collection is
  running. Use SQLite's backup API, for example
  `sqlite3 telegram-tracker.sqlite3 ".backup copy.sqlite3"`, or Python's
  `sqlite3.Connection.backup`.

## Example queries

Posts per day of one channel:

```sql
SELECT substr(date, 1, 10) AS day, count(*) AS posts, sum(views) AS views
FROM messages
WHERE channel_id = 1001 AND constructor = 'Message'
GROUP BY day ORDER BY day;
```

Channels a channel forwards from most:

```sql
SELECT m.fwd_peer_id, p.title, p.username, count(*) AS forwards
FROM messages m
LEFT JOIN peers p ON p.peer_kind = m.fwd_peer_kind AND p.peer_id = m.fwd_peer_id
WHERE m.channel_id = 1001 AND m.fwd_peer_kind = 'channel'
GROUP BY m.fwd_peer_id ORDER BY forwards DESC;
```

Users with a phone number or a known last-seen time:

```sql
SELECT peer_id, first_name, last_name, username, phone, status, status_was_online
FROM peers
WHERE peer_kind = 'user' AND (phone IS NOT NULL OR status_was_online IS NOT NULL);
```

The same photo or document posted more than once:

```sql
SELECT media_type, media_id, count(*) AS posts, count(DISTINCT channel_id) AS channels
FROM messages
WHERE media_id IS NOT NULL
GROUP BY media_type, media_id HAVING count(*) > 1;
```

Access hashes with their recorded collection profile:

```sql
SELECT peer_kind, peer_id, is_min, access_hash, profile_name, profile_id
FROM peer_access_hashes
WHERE peer_kind = 'channel';
```

Recent reply authors referenced by channel posts (`replies.recent_repliers`);
this is not a complete list of commenters or a count of their comments:

```sql
SELECT json_extract(r.value, '$.user_id') AS user_id, count(*) AS posts
FROM messages m, json_each(m.raw, '$.replies.recent_repliers') r
WHERE m.channel_id = 1001 AND json_extract(r.value, '$._') = 'PeerUser'
GROUP BY user_id ORDER BY posts DESC;
```

Any field not projected into a column, here the reactions of each post:

```sql
SELECT message_id, json_extract(raw, '$.reactions.results') AS reactions
FROM messages
WHERE channel_id = 1001 AND json_extract(raw, '$.reactions') IS NOT NULL;
```

Targets that didn't succeed, per run:

```sql
SELECT r.run_id, r.started_at, r.state, t.input, t.status, t.reason
FROM run_targets t
JOIN collection_runs r ON r.run_id = t.run_id
WHERE t.status <> 'succeeded'
ORDER BY r.run_id, t.input_index;
```

History state and owner of every channel:

```sql
SELECT h.channel_id, p.username, h.mode, h.message_count, h.profile_name, r.started_at
FROM histories h
JOIN collection_runs r ON r.run_id = h.run_id
LEFT JOIN peers p ON p.peer_kind = 'channel' AND p.peer_id = h.channel_id;
```

## Personal data

The database stores collected payloads unfiltered. Besides public channel content,
it holds personal data about Telegram users who appear in that content:

| Data | Where |
|---|---|
| Phone numbers | `peers.phone` (users whose number the API returned to the collecting account); `messages.contact_phone` (shared contacts); `raw` of both. |
| Names and usernames | `peers.first_name`, `last_name`, `username` and `raw.usernames`; contact names in message `raw`. |
| Photos | `peers.photo_id`; profile and chat photo objects in `raw`, including `stripped_thumb`, a small embedded preview image. Message photos and documents in `messages.raw`. No media files are downloaded. |
| Online status | `peers.status` and `status_was_online` (last-seen time). |
| Access hashes and file references | `peers.access_hash`, `messages.media_access_hash` and `media_file_reference`; also nested in `raw`, `channel_snapshots.raw` and `collected_chat_rows.metadata`. They let the collecting account address the user, chat or file through the API. |
| Commenters | User IDs in `messages.raw` under `replies.recent_repliers`; their objects in `peers`. |
| Authors and mentions | `messages.sender_id`, `post_author`, forward names, mention entities, and the message text. |
| Collection activity | Runs, times, targets and profile names in the run tables. |

The run tables don't store the collecting account's phone number, API ID or hash,
session, or login codes. If the collecting account itself appears in collected
content, for example as a commenter, its `User` object (`is_self` set in `raw`) is
stored like any other except for its phone number: the writer sets `phone` to null.

The file outputs (`<target>_messages.json`, `<target>.json`, `collected_chats.csv`)
contain the same personal data. Handle the database file accordingly: keep it in a
location only you can read, share it only where you're allowed to, and delete it,
along with its `.bak.sqlite3` backups, when it's no longer needed.
