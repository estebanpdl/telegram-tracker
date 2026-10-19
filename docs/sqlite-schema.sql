-- Telegram Tracker data database, schema version 1.
-- Requires SQLite >= 3.31 (generated columns) with JSON1 (json_extract, json_valid).
-- Per connection: PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 5000.
-- Journal mode: DELETE (the default rollback journal).
-- Timestamps written by the application: UTC ISO 8601 text.
-- Raw payloads: the collector's JSON-safe form of each API object (to_dict(), with
-- datetimes and bytes as str()), stored unfiltered.

PRAGMA application_id = 1413960786;  -- 0x54475452, 'TGTR'
PRAGMA user_version = 1;

CREATE TABLE schema_meta (
  singleton       INTEGER PRIMARY KEY CHECK (singleton = 1),
  schema_version  INTEGER NOT NULL,
  dataset_uuid    TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  app_version     TEXT NOT NULL,
  output_binding  TEXT                          -- canonical output folder bound to this database
);

CREATE TABLE migrations (
  version     INTEGER PRIMARY KEY,
  applied_at  TEXT NOT NULL,
  app_version TEXT NOT NULL,
  backup_path TEXT
);

-- Runs: append-only. Mirrors collection-status.json.
CREATE TABLE collection_runs (
  run_id             INTEGER PRIMARY KEY,
  run_uuid           TEXT NOT NULL UNIQUE,
  -- Collecting profile: local name and opaque UUID, both nullable (NULL = unknown).
  -- Never the account's phone number, API ID/hash or Telegram user ID.
  profile_name       TEXT CHECK (profile_name IS NULL OR (length(profile_name) <= 32
                       AND profile_name GLOB '[a-z]*' AND profile_name NOT GLOB '*[^a-z0-9_-]*')),
  profile_id         TEXT CHECK (profile_id IS NULL OR length(profile_id) = 36),
  state              TEXT NOT NULL CHECK (state IN ('running', 'complete', 'partial', 'interrupted')),
  mode               TEXT NOT NULL CHECK (mode IN ('full', 'window', 'update', 'metadata_only')),
  storage            TEXT NOT NULL CHECK (storage IN ('sqlite', 'both')),
  started_at         TEXT NOT NULL,
  finished_at        TEXT,
  parameters         TEXT NOT NULL CHECK (json_valid(parameters)),
  summary            TEXT CHECK (summary IS NULL OR json_valid(summary)),
  stop_reason        TEXT,
  app_version        TEXT NOT NULL,
  telethon_version   TEXT NOT NULL,
  serializer_version INTEGER NOT NULL,             -- 1 = str(datetime), bytes as Python literals
  aggregate_files    TEXT CHECK (aggregate_files IN ('pending', 'written')),
  CHECK (storage = 'both' OR aggregate_files IS NULL)
);
CREATE INDEX runs_by_state ON collection_runs (state, started_at);
CREATE INDEX runs_aggregate_pending ON collection_runs (run_id) WHERE aggregate_files = 'pending';

CREATE TABLE run_targets (
  run_id             INTEGER NOT NULL REFERENCES collection_runs (run_id),
  input_index        INTEGER NOT NULL CHECK (input_index >= 0),
  input              TEXT NOT NULL,
  status             TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed', 'duplicate', 'cancelled')),
  alias_of           TEXT,
  channel_id         INTEGER,                      -- resolved channel
  history_mode       TEXT CHECK (history_mode IN ('replaced', 'created', 'merged', 'unchanged', 'not_collected')),
  min_id             INTEGER,                      -- --update lower bound
  messages           INTEGER,                      -- history size after this run
  participants_count INTEGER,                      -- channel size as seen by this run
  reason             TEXT,                         -- sanitized failure reason
  -- A full/window replace of a history that this run's profile could not have
  -- updated; the previous profile is kept here.
  takeover           INTEGER NOT NULL DEFAULT 0 CHECK (takeover IN (0, 1)),
  previous_profile_name TEXT,
  previous_profile_id   TEXT,
  files_state        TEXT CHECK (files_state IN ('pending', 'written')),
  CHECK (takeover = 1 OR (previous_profile_name IS NULL AND previous_profile_id IS NULL)),
  PRIMARY KEY (run_id, input_index)
);
CREATE INDEX run_targets_by_channel ON run_targets (channel_id, run_id);
CREATE INDEX run_targets_files_pending ON run_targets (run_id) WHERE files_state = 'pending';

-- Publication state exists only for runs that write both the database and files.
CREATE TRIGGER run_targets_files_state_insert BEFORE INSERT ON run_targets
  WHEN NEW.files_state IS NOT NULL
   AND (SELECT storage FROM collection_runs WHERE run_id = NEW.run_id) IS NOT 'both'
BEGIN
  SELECT RAISE(ABORT, 'files_state requires storage both');
END;
CREATE TRIGGER run_targets_files_state_update BEFORE UPDATE OF files_state ON run_targets
  WHEN NEW.files_state IS NOT NULL
   AND (SELECT storage FROM collection_runs WHERE run_id = NEW.run_id) IS NOT 'both'
BEGIN
  SELECT RAISE(ABORT, 'files_state requires storage both');
END;

CREATE TABLE run_requests (
  run_id             INTEGER NOT NULL REFERENCES collection_runs (run_id),
  method_class       TEXT NOT NULL,                -- resolve, channel, enrichment, history
  requests           INTEGER NOT NULL,
  retries            INTEGER NOT NULL,
  flood_waits        INTEGER NOT NULL,
  flood_wait_seconds INTEGER NOT NULL,
  PRIMARY KEY (run_id, method_class)
) WITHOUT ROWID;

-- Every chat observation in replay order; chats.txt is derived from it.
CREATE TABLE run_observations (
  run_id            INTEGER NOT NULL REFERENCES collection_runs (run_id),
  seq               INTEGER NOT NULL,
  input_index       INTEGER NOT NULL,             -- source target
  kind              TEXT NOT NULL CHECK (kind IN ('channel_request', 'from_messages', 'linked')),
  page              INTEGER,                      -- history page for from_messages
  channel_id        INTEGER NOT NULL,             -- observed public chat
  username          TEXT,                         -- as observed
  parent_channel_id INTEGER,                      -- linked: chat whose response listed it
  PRIMARY KEY (run_id, seq),
  FOREIGN KEY (run_id, input_index) REFERENCES run_targets (run_id, input_index)
) WITHOUT ROWID;
CREATE INDEX observations_by_channel ON run_observations (channel_id, run_id);

-- counter.csv for one run; list columns keep their order as JSON arrays.
CREATE TABLE run_counters (
  run_id          INTEGER NOT NULL REFERENCES collection_runs (run_id),
  channel_id      INTEGER NOT NULL,
  row_order       INTEGER NOT NULL,
  username        TEXT,
  counter         INTEGER NOT NULL,
  from_messages   INTEGER NOT NULL,
  channel_request INTEGER NOT NULL,
  targeted_by     TEXT NOT NULL CHECK (json_type(targeted_by) = 'array'),  -- 'self' or parent usernames
  sources         TEXT NOT NULL CHECK (json_type(sources) = 'array'),      -- target inputs
  PRIMARY KEY (run_id, channel_id),
  UNIQUE (run_id, row_order)
);

-- collected_chats.csv rows; a channel may repeat within a run.
CREATE TABLE collected_chat_rows (
  run_id     INTEGER NOT NULL REFERENCES collection_runs (run_id),
  row_index  INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  metadata   TEXT NOT NULL CHECK (json_valid(metadata)),  -- enrichment row as in the CSV, access_hash included
  PRIMARY KEY (run_id, row_index)
);

CREATE TABLE collection_errors (
  run_id      INTEGER NOT NULL REFERENCES collection_runs (run_id),
  error_index INTEGER NOT NULL,
  category    TEXT NOT NULL CHECK (category IN ('target_not_found', 'enrichment_not_found', 'metadata_skipped')),
  input_index INTEGER,
  peer_kind   TEXT CHECK (peer_kind IN ('channel', 'chat', 'user')),
  peer_id     INTEGER,
  reason      TEXT,
  PRIMARY KEY (run_id, error_index)
);

-- Current state: latest entity per typed key. A min object never replaces a full one.
-- run_id is the run whose response supplied raw: access_hash and the other
-- viewer-relative values in raw belong to that run's account.
CREATE TABLE peers (
  peer_kind         TEXT NOT NULL CHECK (peer_kind IN ('channel', 'chat', 'user')),
  peer_id           INTEGER NOT NULL CHECK (peer_id > 0),
  constructor       TEXT NOT NULL,
  is_min            INTEGER NOT NULL CHECK (is_min IN (0, 1)),
  raw               TEXT NOT NULL CHECK (json_valid(raw)),
  run_id            INTEGER NOT NULL REFERENCES collection_runs (run_id),
  first_seen_run    INTEGER NOT NULL REFERENCES collection_runs (run_id),
  last_seen_run     INTEGER NOT NULL REFERENCES collection_runs (run_id),
  access_hash       INTEGER GENERATED ALWAYS AS (json_extract(raw, '$.access_hash')) VIRTUAL,
  title             TEXT GENERATED ALWAYS AS (json_extract(raw, '$.title')) VIRTUAL,
  username          TEXT GENERATED ALWAYS AS (json_extract(raw, '$.username')) VIRTUAL,
  first_name        TEXT GENERATED ALWAYS AS (json_extract(raw, '$.first_name')) VIRTUAL,
  last_name         TEXT GENERATED ALWAYS AS (json_extract(raw, '$.last_name')) VIRTUAL,
  phone             TEXT GENERATED ALWAYS AS (json_extract(raw, '$.phone')) VIRTUAL,
  photo_id          INTEGER GENERATED ALWAYS AS (json_extract(raw, '$.photo.photo_id')) VIRTUAL,
  status            TEXT GENERATED ALWAYS AS (json_extract(raw, '$.status._')) VIRTUAL,
  status_was_online TEXT GENERATED ALWAYS AS (json_extract(raw, '$.status.was_online')) VIRTUAL,
  broadcast         INTEGER GENERATED ALWAYS AS (json_extract(raw, '$.broadcast')) VIRTUAL,
  megagroup         INTEGER GENERATED ALWAYS AS (json_extract(raw, '$.megagroup')) VIRTUAL,
  bot               INTEGER GENERATED ALWAYS AS (json_extract(raw, '$.bot')) VIRTUAL,
  deleted           INTEGER GENERATED ALWAYS AS (json_extract(raw, '$.deleted')) VIRTUAL,
  PRIMARY KEY (peer_kind, peer_id)
);
CREATE INDEX peers_by_username ON peers (username) WHERE username IS NOT NULL;
CREATE INDEX peers_by_phone ON peers (phone) WHERE phone IS NOT NULL;

-- Latest <target>.json per channel.
CREATE TABLE channel_snapshots (
  channel_id         INTEGER PRIMARY KEY,
  run_id             INTEGER NOT NULL REFERENCES collection_runs (run_id),
  observed_at        TEXT NOT NULL,
  raw                TEXT NOT NULL CHECK (json_valid(raw)),
  about              TEXT GENERATED ALWAYS AS (json_extract(raw, '$.full_chat.about')) VIRTUAL,
  participants_count INTEGER GENERATED ALWAYS AS (json_extract(raw, '$.full_chat.participants_count')) VIRTUAL,
  linked_chat_id     INTEGER GENERATED ALWAYS AS (json_extract(raw, '$.full_chat.linked_chat_id')) VIRTUAL
);

-- Current history per channel. No row = absent; a row with 0 messages = empty.
-- profile_*: whose view this history is. Set by a full/window replace and by the
-- created fallback; --update merges only into a history of the same profile.
CREATE TABLE histories (
  channel_id    INTEGER PRIMARY KEY,
  run_id        INTEGER NOT NULL REFERENCES collection_runs (run_id),  -- last run that changed it
  profile_name  TEXT,                       -- NULL = unknown
  profile_id    TEXT CHECK (profile_id IS NULL OR length(profile_id) = 36),
  mode          TEXT NOT NULL CHECK (mode IN ('replaced', 'created', 'merged')),
  window_bounds TEXT CHECK (window_bounds IS NULL OR json_valid(window_bounds)),  -- last replace; NULL = full
  envelope      TEXT NOT NULL CHECK (json_valid(envelope)),            -- response minus messages/chats/users
  message_count INTEGER NOT NULL CHECK (message_count >= 0),
  updated_at    TEXT NOT NULL
);

-- Which entities a history carries, in payload order (no copies).
CREATE TABLE history_entities (
  channel_id INTEGER NOT NULL REFERENCES histories (channel_id),
  list       TEXT NOT NULL CHECK (list IN ('chats', 'users')),
  ordinal    INTEGER NOT NULL,
  peer_kind  TEXT NOT NULL,
  peer_id    INTEGER NOT NULL,
  PRIMARY KEY (channel_id, list, ordinal),
  UNIQUE (channel_id, peer_kind, peer_id),
  FOREIGN KEY (peer_kind, peer_id) REFERENCES peers (peer_kind, peer_id)
);

-- One row per current message; raw stored once, query columns derived from it.
CREATE TABLE messages (
  channel_id           INTEGER NOT NULL REFERENCES histories (channel_id),
  message_id           INTEGER NOT NULL CHECK (message_id > 0),
  run_id               INTEGER NOT NULL REFERENCES collection_runs (run_id),  -- run that wrote raw
  raw                  TEXT NOT NULL CHECK (json_valid(raw)),
  constructor          TEXT GENERATED ALWAYS AS (json_extract(raw, '$._')) VIRTUAL,
  date                 TEXT GENERATED ALWAYS AS (json_extract(raw, '$.date')) VIRTUAL,
  edit_date            TEXT GENERATED ALWAYS AS (json_extract(raw, '$.edit_date')) VIRTUAL,
  text                 TEXT GENERATED ALWAYS AS (json_extract(raw, '$.message')) VIRTUAL,
  sender_kind          TEXT GENERATED ALWAYS AS (CASE json_extract(raw, '$.from_id._')
                         WHEN 'PeerChannel' THEN 'channel' WHEN 'PeerUser' THEN 'user'
                         WHEN 'PeerChat' THEN 'chat' END) VIRTUAL,
  sender_id            INTEGER GENERATED ALWAYS AS (coalesce(json_extract(raw, '$.from_id.channel_id'),
                         json_extract(raw, '$.from_id.user_id'), json_extract(raw, '$.from_id.chat_id'))) VIRTUAL,
  post_author          TEXT GENERATED ALWAYS AS (json_extract(raw, '$.post_author')) VIRTUAL,
  fwd_peer_kind        TEXT GENERATED ALWAYS AS (CASE json_extract(raw, '$.fwd_from.from_id._')
                         WHEN 'PeerChannel' THEN 'channel' WHEN 'PeerUser' THEN 'user'
                         WHEN 'PeerChat' THEN 'chat' END) VIRTUAL,
  fwd_peer_id          INTEGER GENERATED ALWAYS AS (coalesce(json_extract(raw, '$.fwd_from.from_id.channel_id'),
                         json_extract(raw, '$.fwd_from.from_id.user_id'), json_extract(raw, '$.fwd_from.from_id.chat_id'))) VIRTUAL,
  fwd_channel_post     INTEGER GENERATED ALWAYS AS (json_extract(raw, '$.fwd_from.channel_post')) VIRTUAL,
  fwd_date             TEXT GENERATED ALWAYS AS (json_extract(raw, '$.fwd_from.date')) VIRTUAL,
  reply_to_msg_id      INTEGER GENERATED ALWAYS AS (json_extract(raw, '$.reply_to.reply_to_msg_id')) VIRTUAL,
  reply_to_top_id      INTEGER GENERATED ALWAYS AS (json_extract(raw, '$.reply_to.reply_to_top_id')) VIRTUAL,
  views                INTEGER GENERATED ALWAYS AS (json_extract(raw, '$.views')) VIRTUAL,
  forwards             INTEGER GENERATED ALWAYS AS (json_extract(raw, '$.forwards')) VIRTUAL,
  replies              INTEGER GENERATED ALWAYS AS (json_extract(raw, '$.replies.replies')) VIRTUAL,
  discussion_id        INTEGER GENERATED ALWAYS AS (json_extract(raw, '$.replies.channel_id')) VIRTUAL,
  grouped_id           INTEGER GENERATED ALWAYS AS (json_extract(raw, '$.grouped_id')) VIRTUAL,
  media_type           TEXT GENERATED ALWAYS AS (json_extract(raw, '$.media._')) VIRTUAL,
  media_id             INTEGER GENERATED ALWAYS AS (coalesce(json_extract(raw, '$.media.photo.id'),
                         json_extract(raw, '$.media.document.id'))) VIRTUAL,
  media_access_hash    INTEGER GENERATED ALWAYS AS (coalesce(json_extract(raw, '$.media.photo.access_hash'),
                         json_extract(raw, '$.media.document.access_hash'))) VIRTUAL,
  media_file_reference TEXT GENERATED ALWAYS AS (coalesce(json_extract(raw, '$.media.photo.file_reference'),
                         json_extract(raw, '$.media.document.file_reference'))) VIRTUAL,
  contact_phone        TEXT GENERATED ALWAYS AS (json_extract(raw, '$.media.phone_number')) VIRTUAL,
  contact_user_id      INTEGER GENERATED ALWAYS AS (json_extract(raw, '$.media.user_id')) VIRTUAL,
  action_type          TEXT GENERATED ALWAYS AS (json_extract(raw, '$.action._')) VIRTUAL,
  PRIMARY KEY (channel_id, message_id)
);
CREATE INDEX messages_by_date ON messages (channel_id, date, message_id);
CREATE INDEX messages_by_forward ON messages (fwd_peer_kind, fwd_peer_id) WHERE fwd_peer_id IS NOT NULL;
CREATE INDEX messages_by_run ON messages (run_id);

-- Cumulative views matching the file outputs' append semantics.
CREATE VIEW collected_chats_all AS
  SELECT r.run_id, r.row_index, r.channel_id, r.metadata,
         c.counter, c.from_messages, c.channel_request, c.targeted_by, c.sources
  FROM collected_chat_rows r
  LEFT JOIN run_counters c ON c.run_id = r.run_id AND c.channel_id = r.channel_id
  ORDER BY r.run_id, r.row_index;

CREATE VIEW chat_ids_all AS            -- chats.txt: first occurrence across runs
  SELECT channel_id FROM (
    SELECT channel_id, row_number() OVER (ORDER BY run_id, seq) AS pos FROM run_observations
  ) GROUP BY channel_id ORDER BY min(pos);

CREATE VIEW destination_pending AS
  SELECT 'target' AS kind, run_id FROM run_targets WHERE files_state = 'pending'
  UNION ALL
  SELECT 'aggregates', run_id FROM collection_runs WHERE aggregate_files = 'pending';

-- Account-scoped values with the profile of the run that collected them. An access
-- hash or file reference is valid only for that account; NULL profile = unknown.
CREATE VIEW peer_access_hashes AS
  SELECT p.peer_kind, p.peer_id, p.is_min, p.access_hash, p.run_id, r.profile_name, r.profile_id
  FROM peers p JOIN collection_runs r ON r.run_id = p.run_id
  WHERE p.access_hash IS NOT NULL;

CREATE VIEW message_media_references AS
  SELECT m.channel_id, m.message_id, m.media_type, m.media_id, m.media_access_hash, m.media_file_reference,
         m.run_id, r.profile_name, r.profile_id
  FROM messages m JOIN collection_runs r ON r.run_id = m.run_id
  WHERE m.media_id IS NOT NULL;
