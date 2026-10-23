-- Telegram Tracker media ledger, version 1: <output>/downloaded-media/media.sqlite3.
-- Requires SQLite >= 3.31 with JSON1 (json_valid), like the data database.
-- Per connection: PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 5000.
-- Journal mode: DELETE (the default rollback journal).
-- Timestamps written by the application: UTC ISO 8601 text.
-- Paths: relative to downloaded-media, '/' separators, generated names only.
-- Rows are never deleted; ledger_meta, blobs and complete items never change.

PRAGMA application_id = 1413960781;  -- 0x5447544D, 'TGTM'
PRAGMA user_version = 1;

CREATE TABLE ledger_meta (
  singleton       INTEGER PRIMARY KEY CHECK (singleton = 1),
  ledger_version  INTEGER NOT NULL CHECK (ledger_version = 1),
  ledger_uuid     TEXT NOT NULL CHECK (length(ledger_uuid) = 36),
  dataset_uuid    TEXT NOT NULL CHECK (length(dataset_uuid) = 36),  -- the folder's dataset (see the guide)
  storage         TEXT NOT NULL CHECK (storage IN ('files', 'sqlite', 'both')),
  output_binding  TEXT,                         -- canonical output folder when the ledger was created
  created_at      TEXT NOT NULL,
  app_version     TEXT NOT NULL
);

-- Runs that wrote to the ledger: collect with media, or download-media.
CREATE TABLE media_runs (
  run_uuid         TEXT PRIMARY KEY CHECK (length(run_uuid) = 36),
  command          TEXT NOT NULL CHECK (command IN ('collect', 'download-media')),
  collection_run   TEXT,                        -- collect: run_uuid (sqlite/both) or started_at (files)
  profile_name     TEXT CHECK (profile_name IS NULL OR (length(profile_name) <= 32
                     AND profile_name GLOB '[a-z]*' AND profile_name NOT GLOB '*[^a-z0-9_-]*')),
  profile_id       TEXT CHECK (profile_id IS NULL OR length(profile_id) = 36),
  state            TEXT NOT NULL CHECK (state IN ('running', 'complete', 'partial', 'interrupted')),
  stop_reason      TEXT,
  started_at       TEXT NOT NULL,
  finished_at      TEXT,
  selection        TEXT NOT NULL CHECK (json_valid(selection)),   -- {"types", "max_media_size", "max_media_total"}
  channels         TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(channels) AND channels LIKE '[%'),  -- channel IDs covered
  app_requests     INTEGER NOT NULL DEFAULT 0 CHECK (app_requests >= 0),
  retries          INTEGER NOT NULL DEFAULT 0 CHECK (retries >= 0),
  flood_waits      INTEGER NOT NULL DEFAULT 0 CHECK (flood_waits >= 0),
  refreshes        INTEGER NOT NULL DEFAULT 0 CHECK (refreshes >= 0),
  bytes_received   INTEGER NOT NULL DEFAULT 0 CHECK (bytes_received >= 0),
  app_version      TEXT NOT NULL
);
CREATE INDEX media_runs_by_state ON media_runs (state, started_at);

-- Stored files: one per distinct content within a channel.
CREATE TABLE blobs (
  channel_id      INTEGER NOT NULL CHECK (typeof(channel_id) = 'integer' AND channel_id > 0),
  sha256          TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
  bytes           INTEGER NOT NULL CHECK (typeof(bytes) = 'integer' AND bytes >= 0),
  type            TEXT NOT NULL CHECK (type IN ('image', 'video', 'audio', 'file')),
  mime_type       TEXT,
  ext             TEXT NOT NULL,
  path            TEXT NOT NULL UNIQUE CHECK (path GLOB '[1-9]*/*/*' AND path NOT GLOB '*[^a-z0-9./-]*'
                    AND path NOT LIKE '%..%'),
  first_run_uuid  TEXT NOT NULL REFERENCES media_runs (run_uuid),
  created_at      TEXT NOT NULL,
  PRIMARY KEY (channel_id, sha256)
) WITHOUT ROWID;

-- One row per selected media reference of a collected message.
CREATE TABLE items (
  channel_id      INTEGER NOT NULL CHECK (typeof(channel_id) = 'integer' AND channel_id > 0),
  message_id      INTEGER NOT NULL CHECK (typeof(message_id) = 'integer' AND message_id > 0),
  media_id        INTEGER NOT NULL CHECK (typeof(media_id) = 'integer'),
  media_type      TEXT NOT NULL,                -- raw constructor, as messages.media_type
  media_kind      TEXT NOT NULL CHECK (media_kind IN ('photo', 'document')),
  photo_size      TEXT CHECK ((media_kind = 'photo') = (photo_size IS NOT NULL)),
  type            TEXT NOT NULL CHECK (type IN ('image', 'video', 'audio', 'file')),
  subtype         TEXT NOT NULL CHECK (subtype IN ('photo', 'sticker', 'voice_note', 'video_note', 'gif',
                    'video', 'audio', 'document')),
  mime_type       TEXT,
  ext             TEXT NOT NULL,
  expected_bytes  INTEGER CHECK (expected_bytes IS NULL OR expected_bytes >= 0),  -- NULL = unknown
  actual_bytes    INTEGER CHECK (actual_bytes IS NULL OR actual_bytes >= 0),
  sha256          TEXT,
  state           TEXT NOT NULL CHECK (state IN ('requested', 'downloading', 'complete', 'skipped', 'failed')),
  reason          TEXT CHECK (CASE state
                    WHEN 'skipped' THEN reason IS NOT NULL AND reason IN ('over_file_limit', 'over_run_limit')
                    WHEN 'failed' THEN reason IS NOT NULL AND reason IN ('reference_expired', 'message_gone',
                      'media_changed', 'flood_stop', 'request_failed', 'disk_full', 'storage_error',
                      'size_mismatch', 'path_conflict', 'stored_file_differs', 'interrupted')
                    ELSE reason IS NULL END),
  attempts        INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  source_run      TEXT,                         -- collection run of the history it was selected from
  first_run_uuid  TEXT NOT NULL REFERENCES media_runs (run_uuid),
  last_run_uuid   TEXT NOT NULL REFERENCES media_runs (run_uuid),
  profile_name    TEXT,                         -- the last run's profile
  profile_id      TEXT,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (channel_id, message_id, media_id),
  FOREIGN KEY (channel_id, sha256) REFERENCES blobs (channel_id, sha256),
  CHECK ((state = 'complete') = (sha256 IS NOT NULL)),
  CHECK ((state = 'complete') = (actual_bytes IS NOT NULL))
) WITHOUT ROWID;
CREATE INDEX items_by_state ON items (state);
CREATE INDEX items_by_media ON items (channel_id, media_kind, media_id);
CREATE INDEX items_by_blob ON items (channel_id, sha256) WHERE sha256 IS NOT NULL;

-- Download attempts: n counts an item's attempts across runs.
CREATE TABLE attempts (
  channel_id      INTEGER NOT NULL,
  message_id      INTEGER NOT NULL,
  media_id        INTEGER NOT NULL,
  n               INTEGER NOT NULL CHECK (n >= 1),
  run_uuid        TEXT NOT NULL REFERENCES media_runs (run_uuid),
  started_at      TEXT NOT NULL,
  ended_at        TEXT,
  outcome         TEXT CHECK (outcome IN ('complete', 'skipped', 'failed')),
  reason          TEXT,
  bytes_received  INTEGER NOT NULL DEFAULT 0 CHECK (bytes_received >= 0),
  app_requests    INTEGER NOT NULL DEFAULT 0 CHECK (app_requests >= 0),
  refreshed       INTEGER NOT NULL DEFAULT 0 CHECK (refreshed IN (0, 1)),
  PRIMARY KEY (channel_id, message_id, media_id, n),
  FOREIGN KEY (channel_id, message_id, media_id) REFERENCES items (channel_id, message_id, media_id),
  CHECK ((ended_at IS NULL) = (outcome IS NULL)),
  CHECK ((outcome = 'complete' OR outcome IS NULL) = (reason IS NULL))
) WITHOUT ROWID;
CREATE INDEX attempts_by_run ON attempts (run_uuid);

-- State machine of items.state; complete is final.
CREATE TRIGGER items_insert_requested BEFORE INSERT ON items
WHEN NEW.state <> 'requested'
BEGIN SELECT RAISE(ABORT, 'a media item starts as requested'); END;

CREATE TRIGGER items_complete_final BEFORE UPDATE ON items
WHEN OLD.state = 'complete'
BEGIN SELECT RAISE(ABORT, 'a complete media item never changes'); END;

CREATE TRIGGER items_state_transition BEFORE UPDATE OF state ON items
WHEN NOT (
  (OLD.state = 'requested' AND NEW.state IN ('requested', 'downloading', 'complete', 'skipped', 'failed'))
  OR (OLD.state = 'downloading' AND NEW.state IN ('complete', 'skipped', 'failed'))
  OR (OLD.state IN ('skipped', 'failed') AND NEW.state = 'requested'))
BEGIN SELECT RAISE(ABORT, 'invalid media item state transition'); END;

CREATE TRIGGER attempts_ended_final BEFORE UPDATE ON attempts
WHEN OLD.ended_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'an ended attempt never changes'); END;

CREATE TRIGGER ledger_meta_final BEFORE UPDATE ON ledger_meta
BEGIN SELECT RAISE(ABORT, 'ledger_meta never changes'); END;

CREATE TRIGGER blobs_final BEFORE UPDATE ON blobs
BEGIN SELECT RAISE(ABORT, 'a blob never changes'); END;

CREATE TRIGGER ledger_meta_kept BEFORE DELETE ON ledger_meta
BEGIN SELECT RAISE(ABORT, 'ledger rows are never deleted'); END;

CREATE TRIGGER media_runs_kept BEFORE DELETE ON media_runs
BEGIN SELECT RAISE(ABORT, 'ledger rows are never deleted'); END;

CREATE TRIGGER blobs_kept BEFORE DELETE ON blobs
BEGIN SELECT RAISE(ABORT, 'ledger rows are never deleted'); END;

CREATE TRIGGER items_kept BEFORE DELETE ON items
BEGIN SELECT RAISE(ABORT, 'ledger rows are never deleted'); END;

CREATE TRIGGER attempts_kept BEFORE DELETE ON attempts
BEGIN SELECT RAISE(ABORT, 'ledger rows are never deleted'); END;
