# Compatibility and offline validation

## Tested environment

| Item | Value |
|---|---|
| Interpreter | Python 3.14.7 (conda-forge), conda env `telegram-tracker` |
| Run tests | `conda run -n telegram-tracker python -m unittest discover -s tests -v` (from the repo root) |
| Dependency check | `conda run -n telegram-tracker python -m pip check` |
| Installed command | 2026-10-18: wheel built with build 1.6.1 and setuptools 84.0.0, installed with pip 26.2.1 into a fresh Python 3.14.7 virtual environment with the pinned requirements. From an empty directory, `tgtracker` and `python -m telegram_tracker` matched `python main.py` in help, argument errors, exit codes, and builder outputs. |

Direct dependencies are pinned in `requirements.txt`:

| Package | Version | Notes |
|---|---|---|
| telethon | 1.45.0 | Uses the 1.x client and two-step verification interfaces. |
| pandas | 3.0.6 | Date helpers omit the removed `infer_datetime_format` argument. |
| openpyxl | 3.1.5 | XLSX export. |
| tqdm | 4.70.1 | Progress display. |
| networkx | 3.7 | Graph construction and GEXF export. |
| matplotlib | 3.11.2 | Uses `matplotlib.colormaps`, not the removed `cm.get_cmap`. |
| python-louvain | 0.16 | Imported as `community`. |

Transitive set resolved on 2026-10-16 (`pip freeze`): colorama 0.4.6, contourpy 1.4.0,
cycler 0.12.1, et_xmlfile 2.0.0, fonttools 4.67.0, kiwisolver 1.5.1, numpy 2.5.4,
packaging 26.3, pillow 12.3.0, pyaes 1.6.1, pyasn1 0.6.4, pyparsing 3.3.3,
python-dateutil 2.9.0.post0, rsa 4.9.1, six 1.17.0, tzdata 2026.5.

The tests establish synthetic offline contracts, not live Telegram compatibility
or an account-specific safe request rate. The README contains option examples.

## CLI contract

`python main.py <command>` (the `telegram_tracker.cli` package) is the canonical
interface. The installed `tgtracker` command and `python -m telegram_tracker` parse the
same arguments and reach the same handlers; only the program name in help and errors
differs. The repository scripts import the package from `src/` without installation.
Legacy forms reach the same handlers:

| Legacy invocation | Inputs | Outputs | Canonical form |
|---|---|---|---|
| `python main.py --telegram-channel NAME` | One target; metadata-only, `--output/-o DIR` (default `./output/data`), `--min-id N` | Channel JSON, messages JSON unless metadata-only, `chats.txt`, `collected_chats.csv`, `counter.csv`, `collection-status.json`; exception logs when needed | `python main.py collect --telegram-channel NAME` |
| `python main.py --batch-file FILE` | One target per line; same options | Same, one folder per collected channel | `python main.py collect --batch-file FILE` |
| `python build-datasets.py [--data-path/-d DIR]` | `<DIR>/**/*_messages.json`, `<DIR>/collected_chats.csv` | `msgs_dataset.csv` (append), `collected_chats.xlsx` (replace) | `python main.py build-dataset --data-path DIR` |
| `python channels-to-network.py [--data-path/-d DIR]` | `<DIR>/collected_chats.csv` (`username`, `counter`, `source`) | `Graph.gexf`, `network.png` | `python main.py build-network --data-path DIR` |

- A first token that is an option other than `-h`/`--help` selects the legacy
  collection parser. It accepts only selectors, metadata-only, output, and `--min-id`.
  Both collection parsers list `--min-id` in a "history window" help group.
- No arguments prints the overview to stderr and exits 2. Help exits 0 without
  credentials, sessions, Telethon, pandas, or plotting libraries.
- Validation precedes credential reads: selectors, batch file and target forms,
  numeric bounds, dates, incompatible options, and flags for the wrong command.
- Exit codes: 0 success; 1 target, login, input, storage, lease, or dependency
  failure; 2 argument errors and missing/invalid app credentials; 130 interrupted.
  Missing dependencies are named in the error.

### Sessions and app credentials

`auth login|status|logout|clear-credentials` (canonical form only) and `collect`
share one resolution (`telegram_tracker.credentials`); the legacy form uses the same
defaults without the `--session`/`--config` options.

- Application home: `~/.telegram_tracker`, or the absolute `TELEGRAM_TRACKER_HOME`.
  Resolved only by `collect` and `auth`, never by help, builders, or imports, and
  created (owner-only) only when a session or credentials are written there.
- Session: `--session` > `TELEGRAM_TRACKER_SESSION` > existing home session > existing
  `./session_file.session` (with a notice) > home session created by the first login.
  Paths resolve symlinks/junctions and use Telethon's exact lowercase `.session`
  suffix; resolved filenames without that suffix are refused. Hard links are
  not identified as aliases. No automatic move or copy.
  One `<session>.lock` lease is taken before the client exists and released after
  its one disconnect. The session must be outside `--output`; `--output` must
  neither be inside the application home nor contain it.
- App credentials: environment variables per value > `--config` INI > home
  `credentials.json` > `./config/config.ini` > `auth login` prompts. One file source,
  used whole; template placeholders count as missing; invalid values are errors.
- `credentials.json`: `schema_version` 1, `api_id` (positive integer), `api_hash`,
  `phone`; plaintext with owner-only permissions (POSIX 0700/0600, Windows protected
  owner-only DACL), staged privately and replaced atomically under a
  `credentials.json.lock` lease. Never a code, password, or session key.
- Saving: typed values after a successful login unless `--no-save`; environment/INI
  values and replacements only on consent or `--save`; never after a failed login.
- `auth status` prints source labels only and works offline; `--check` connects an
  existing session without requesting a code. `auth logout` revokes the session and
  keeps saved credentials; `auth clear-credentials` deletes only `credentials.json`.
- Earlier versions read `./config/config.ini` unconditionally (a missing file was a
  traceback) and created `./session_file.session` in the working directory.

Live limits: login, checked status, and logout were validated offline with a fake
client only. Telegram's responses to `log_out` and `is_user_authorized` on real
sessions, and Telethon's session-file updates during a check, are unverified.

### Account profiles

- Selection: `--profile NAME` (canonical `collect` and `auth` actions) >
  `TELEGRAM_TRACKER_PROFILE` (also the legacy form) > `default`. Names match
  `[a-z][a-z0-9_-]{0,31}`; Windows device names, link/junction profile folders, and
  case-variant folders are refused (exit 2). Nothing is saved as an active profile.
- `default` is the application home itself, with the session and credential rules
  above unchanged. A named profile is `<home>/profiles/<name>/` with its own
  `credentials.json` (same v1 format), `session_file.session`, and `profile.json`;
  it refuses `--session` and `TELEGRAM_TRACKER_SESSION` and never reads
  `./config/config.ini`, `./session_file.session`, or another profile's files.
- `profile.json`: `schema_version` 1, `name`, `profile_id` (random UUID, immutable),
  `account_id` (nullable; private). Owner-only, atomically replaced under a
  `profile.json.lock` lease. Created by a successful `auth login` (named profiles
  exist only once it is there; the default profile gets one at its next login) and
  kept by `clear-credentials`. An account is bound once, only through the profile's
  own session, and never replaced; a session of another account stops login and
  collection with exit 1. `auth login` also refuses a session whose account phone
  number differs from the one in use.
- Earlier versions ignore `profiles/`, `profile.json`, and `account-leases/`; the
  default `credentials.json` and session stay readable by them. There is no
  format conversion and no backup to restore.
- `collect` sends one `get_me` after login (recorded as `account_checks`, outside
  the request controls) and holds `<home>/account-leases/<hash>.lock` until the run
  ends, before the output lock. Lease order: session, account, output folder. The
  same account in a second collection is refused; different accounts may run at the
  same time into different folders, each with its own request controls. Every
  collection needs a writable application home for the lease. Coordination is per
  application home. Stale leases are reported, never removed.
- Provenance: `collection-status.json` gains `profile_name`, `profile_id` (null when
  unknown; absent in sidecars of earlier versions), and `account_checks`; target
  entries of replaced foreign histories gain `takeover`, `previous_profile_name`,
  and `previous_profile_id`. Each written history gets
  `<channel>/<channel>_provenance.json` (format 1). `--update` follows the matching
  rules in the README (ID, then name, then default-only for histories without a
  record) and refuses before any request for that channel. This provenance does
  not add a private account ID, phone number, API credential, session path, or
  lease name. Collected payloads retain user IDs and other users' phone numbers;
  only `User.phone` on objects marked `is_self` is redacted when written.
- Not implemented: profile rename/delete/copy, session migration, OS keyring,
  coordination across application homes, multi-profile batches.

Platform limits: owner-only protection of the new files was exercised on Windows
(protected DACL) only; the POSIX 0700/0600 branch is unexercised on this host. The
two-process account-lease test runs on the local file system; network file systems
are not covered. Profiles were not tested against live Telegram accounts.

### History selection and updates

`collect` supports exclusive ID bounds, inclusive `--since`, exclusive `--until`,
and `--max-messages` (newest first). Dates without offsets are UTC. Bounds combine
as AND. The server receives ID bounds on every page and `--until` on the first;
returned messages are also filtered locally. Paging stops at the cap or below
`--since`/`--min-id`. The date stop assumes dates follow ID order. Boundary-page
chats/users may refer to excluded messages. A filtered run replaces saved history
and warns before credentials when matching target files exist.

`--update` derives each target's minimum ID from its saved history, fetches newer
posts, and merges by ID with fetched copies winning. It is exclusive with every
window flag and metadata-only, and unavailable in the legacy collection form.
No new posts leaves the messages file byte-identical; absent history causes full
collection. Malformed saved history fails that target without replacement.
Updates do not refresh earlier edited or deleted Telegram posts.

## Collection and persistence

One event loop owns one client/session, including creation, connection, login,
and disconnect. Imports do not parse arguments, read credentials, connect, or
create a loop. `collect_channel` returns a `storage.records.ChannelResult` without writing.
A shared `MetadataCache` coalesces full-channel requests. Successes and permanent
access errors are cached per run; other failures are not. An inaccessible linked
chat ends its parent's enrichment as not found, retaining preceding linked-chat
observations and logging the parent ID.

`storage.writer.OutputWriter` writes completed results and replays their chat observations
in input order for deterministic aggregates:

- Channel JSON/history and the history's provenance are written after collection
  completes. Request failures preserve earlier files. A storage failure between
  replacements can leave files from different runs; replacement is atomic per
  file, not per channel.
- `chats.txt` retains earlier IDs and adds new IDs in first-occurrence order.
  `counter.csv` describes this run. `collected_chats.csv` preserves earlier rows
  and appends new rows with this run's counts, without `_x`/`_y` columns.
- JSON, CSV, and `chats.txt` use temporary files and `os.replace`. Exception logs
  (`_exceptions-channels-not-found.txt`, `_exceptions-users-not-found.txt`) append
  directly. There is no transaction spanning all outputs.
- A folder lock is checked before connecting and acquired after login. Stale
  `.collection.lock` files are reported with recovery steps, never removed automatically.
- `collection-status.json` records the profile, parameters, outcomes, request
  statistics, and `running`, `complete`, `partial`, or `interrupted` state without
  credentials, account IDs, or message text. Metadata-only marks histories retained from earlier runs as stale.
- Channel files keep collected users unfiltered, except that a `User` with `is_self`
  (the collecting account) is written with `phone` set to null.
- Cancellation/fatal errors retain completed channels and finalize their aggregates
  with interrupted status when storage remains writable.

Completed channel observations remain in memory until finalization. Each active
channel holds its history; updates also read earlier history.

The collector reaches storage only through a writer (`run_collection(...,
writer_factory=...)`) and its history source (`storage.histories`). The default is
the file writer above. `storage.sqlite.writer.SQLiteWriter` stores the same results in
the database described in [sqlite-schema.md](sqlite-schema.md), one transaction
per channel, applying the same `is_self` phone rule; `collect --storage` selects it
(see below). Its `--update` lookup is by resolved channel ID, after resolution and
before the first history request.

### Storage modes

`collect --storage files|sqlite|both` (canonical form only; the three entry points
agree) selects the writer through `storage.destination`:

- A folder without `collection-storage.json` is a `files` folder: files mode writes no
  manifest, so its outputs and sidecar keep their earlier shape. `sqlite` and `both`
  write the manifest (format 1: storage, database name, dataset UUID) right after
  creating `<output>/telegram-tracker.sqlite3`, which records the canonical folder in
  `schema_meta.output_binding`. There is no option for another database path.
- Without `--storage`, the recorded mode is used. Another explicit mode, or `sqlite`/
  `both` for a folder that holds other entries and no manifest, exits 2 before any
  client exists. The legacy collection form refuses `sqlite`/`both` folders with exit 2.
- Before credentials, read only: a recorded database must exist, classify as current,
  match the manifest's dataset UUID and the folder binding, and have no
  `destination_pending` row; otherwise exit 1. The selected session path is passed as
  a protected path and is never opened as the database.
- Lock order: `.collection.lock`, then `telegram-tracker.sqlite3.lock` (beside the
  canonical database path). Both are checked before connecting, each with its own
  message, and taken after login. Under the database lease the checks run again before
  the first write; `storage.sqlite.migrations.migrate` takes the same lease. Runs still
  `running` when the lease is free are recorded as `interrupted` (`stop_reason`
  `stale: ...`, `finished_at` null) in the transaction that records the new run.
- `both` (`storage.dual.DualWriter`): per channel, stage the files as files mode writes
  them, commit the database transaction with `files_state = 'pending'`, replace the
  files, set `written`. Aggregates: stage, finish the run in the database with
  `aggregate_files = 'pending'`, replace, append the exception logs (text mode, platform
  newlines), set `written`. A staging or database failure changes neither sink. A
  publish failure records the target as failed (best effort), keeps `pending` and stops
  the run with a storage error; after the run's end state was stored, it is set to
  `interrupted`. Any `pending` row blocks collection into the folder in every mode until
  a fresh folder is used; lock deletion does not clear it. A crash between replace and
  mark blocks too (a safe false positive). On Windows, `both` retries a `PermissionError`
  from `os.replace` up to 5 attempts (waits 0.05, 0.1, 0.2, 0.4 s); files mode keeps one
  attempt.
- `both --update` compares the files' and the database's newest ID and message-ID set
  after resolution, before the first history request; a difference, or a history in one
  sink only, fails that channel (`SinksDisagree`). The files get files mode's merged
  history; the database upserts the fetched messages.
- The sidecar gains, for database modes only, `storage`, `database` (file, schema
  version, dataset UUID, run UUID) and, for `both`, `publication` (pending targets,
  aggregate state). Its format stays 1.
- Builders read `collection-storage.json` only: a `sqlite` folder is refused, `both`
  passes to the usual sidecar gates. No builder opens the database.

### Scheduling and request policy

Batch collection defaults to 3 workers (`--concurrency` 1–10); one channel uses 1.
Pages remain sequential per channel. A bounded result queue feeds one writer.
Targets normalize public links and `@username`, skip blanks, and deduplicate
case-insensitively. Resolved aliases are skipped in input order.

Channel collection RPCs use `request_policy.RequestPolicy`: at most N in flight,
at least `--request-interval` (default 0.25 s, range 0.1–60) between admissions,
and a 60 s timeout per attempt. Flood waits pause admissions across workers.
Waits above `--max-flood-wait` (default 60 s, range 0–3600) or exhausted flood
retries stop the account's run with interrupted status and exit 1.

Server errors, timeouts, and dropped connections use exponential backoff
(1, 2, 4 ... s, capped at 30), sharing `--max-retries` (default 3, range 0–10)
with flood retries. Exhausted transient retries fail the channel; permanent
access failures are not retried. Rejected authorization stops the run.

Telethon uses `flood_sleep_threshold=0`, `request_retries=1`,
`raise_last_call_error=True`, and `receive_updates=False`. The library retry
permits DC migration and can retry a server error inside a policy attempt.
Statistics count policy attempts by `resolve`, `channel`, `history`, and
`enrichment`; a shared cache response belongs to its first requesting class.
Login and the subsequent account check precede this policy and its workers;
neither is paced or retried by it. All workers finish before disconnect.

**Fake-latency experiment** (`python tests/scheduler_experiment.py`, virtual clock):
10 generated channels × 3 pages plus an alias, a missing and a private target;
each fake request takes 1.0 s, with a 0.25 s request interval.

| Concurrency | Peak in flight | Virtual seconds | Requests | Succeeded / failed / duplicate |
|---|---|---|---|---|
| 1 | 1 | 66.00 | 66 | 10 / 2 / 1 |
| 3 | 3 | 26.00 | 66 | 10 / 2 / 1 |
| 10 | 5 | 18.75 | 66 | 10 / 2 / 1 |

Output files are byte-identical across these runs (status excluded). At 10,
the request gap limits overlap. These are simulated results, not Telegram timings.

### Login

An authorized `./session_file.session` skips prompts. Otherwise login requests a
code and, when needed, a two-step verification password through no-echo `getpass`.
It refuses echo fallback. Each has at most 3 attempts; expired codes and login
flood waits stop immediately. EOF and Telegram refusals exit 1; Ctrl+C exits 130.
Messages name the failing step without exposing entered values. Passwords are
never CLI arguments or written to output. Prompts need an interactive terminal;
run inside an activated environment. `conda run` does not attach terminal input
by default.

## Offline builders

`datasets.build_dataset` and `network.build_network` read local files without
Telethon, credentials, or sessions. Plotting loads only when rendering. Builders
reject running/interrupted status, note partial runs, and accept older folders
with no sidecar. They refuse a `--storage sqlite` folder (from its manifest, without
opening the database).

- `msgs_dataset.csv` appends: rebuilding duplicates rows, including after `--update`.
  `collected_chats.xlsx` is replaced. Message files use sorted paths.
- The graph is directed and unweighted. Each username's first row supplies its
  sources; duplicate edges collapse. Source order keeps first occurrence.
- Malformed input is rejected before output writes. Empty inputs produce header-only
  datasets or an empty GEXF and blank PNG. Equal-degree nodes use size 275.
- Outputs are staged and replaced separately; there is no multi-file transaction.

## Offline test harness

- `tests/offline_guard.py` blocks and records network/DNS connections, real client
  construction/authentication, and config/session file opens. It permits asyncio's
  Windows loopback `socketpair()`. Base cases fail recorded violations even if caught.
  It also refuses any file system call on the real `~/.telegram_tracker` tree,
  including relative, case, `..`, 8.3, `file:` URI, and symlink/junction aliases
  (resolved component by component without entering the tree), including its
  `profiles/` and `account-leases/` folders, gives every test process a temporary
  `TELEGRAM_TRACKER_HOME` and `TELEGRAM_TRACKER_SESSION`, and removes
  `TELEGRAM_TRACKER_PROFILE`.
- `tests/fakes.py` supplies synthetic responses, pagination, scripted errors/delays,
  a fake client, and scripted prompts. `tests/fixtures/telegram_world.json` covers
  shared chats, empty/missing/private channels, Unicode, forwards, replies, service
  messages, and web page, document, poll, geo, contact, and venue media.
- `tests/fixtures/expected/` retains static baseline contracts and regression evidence.
  Collection tests adapt pre-cache request traces to the current cache. No fixture
  implies live collection.
- `tests/builder_data.py` prepares fake collection output and runs wrappers in guarded
  interpreters. `tests/virtual_time.py` jumps to timers, avoiding real pacing waits.
- `tests/test_environment.py` checks pinned interfaces and prevents reintroduction
  of removed pandas and matplotlib calls.
