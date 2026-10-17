# Compatibility and offline test contracts

Working reference for the CLI unification and concurrency initiative. This file tracks
the current command contract, the tested dependency set, known limitations, and the
offline test harness. README stays the user guide.

## Tested environment

| Item | Value |
|---|---|
| Interpreter | Python 3.14.7 (conda-forge), conda env `telegram-tracker` |
| Run tests | `conda run -n telegram-tracker python -m unittest discover -s tests -v` (from the repo root) |
| Dependency check | `conda run -n telegram-tracker python -m pip check` (no broken requirements) |

Direct dependencies are pinned in `requirements.txt`:

| Package | Version | Notes |
|---|---|---|
| telethon | 1.45.0 | Stay on 1.x. Provides `SessionPasswordNeededError`, `PasswordHashInvalidError`, and `sign_in(password=...)` for two-step verification. |
| pandas | 3.0.6 | `to_datetime(infer_datetime_format=...)` was removed (see limitations). |
| openpyxl | 3.1.5 | |
| tqdm | 4.70.1 | |
| networkx | 3.7 | |
| matplotlib | 3.11.2 | `matplotlib.cm.get_cmap` was removed (see limitations). |
| python-louvain | 0.16 | Imported as `community`. |

Transitive set resolved on 2026-10-16 (`pip freeze`): colorama 0.4.6, contourpy 1.4.0,
cycler 0.12.1, et_xmlfile 2.0.0, fonttools 4.67.0, kiwisolver 1.5.1, numpy 2.5.4,
packaging 26.3, pillow 12.3.0, pyaes 1.6.1, pyasn1 0.6.4, pyparsing 3.3.3,
python-dateutil 2.9.0.post0, rsa 4.9.1, six 1.17.0, tzdata 2026.5.

## Current CLI contract

Since Phase 4, `python main.py <command>` (`cli.py`) is the canonical interface, and the
legacy forms below reach the same handlers. Outputs are unchanged from commit `9b0ec71`
except for the corrections listed per phase. The README has the migration table.

| Legacy invocation | Inputs | Outputs | Canonical form |
|---|---|---|---|
| `python main.py --telegram-channel NAME` | one channel; `--limit-download-to-channel-metadata`, `--output/-o DIR` (default `./output/data`), `--min-id N` | `<DIR>/<NAME>/<NAME>.json`, `<NAME>_messages.json`, `chats.txt`, `collected_chats.csv`, `counter.csv`, `_exceptions-channels-not-found.txt`, `_exceptions-users-not-found.txt` | `python main.py collect --telegram-channel NAME` |
| `python main.py --batch-file FILE` | one target per line; same options | same, one folder per channel | `python main.py collect --batch-file FILE` |
| `python build-datasets.py [--data-path/-d DIR]` | `<DIR>/**/*_messages.json`, `<DIR>/collected_chats.csv` | `msgs_dataset.csv` (append), `collected_chats.xlsx` (replace) | `python main.py build-dataset --data-path DIR` |
| `python channels-to-network.py [--data-path/-d DIR]` | `<DIR>/collected_chats.csv` (`username`, `counter`, `source`) | `Graph.gexf`, `network.png` | `python main.py build-network --data-path DIR` |

### Phase 4: unified CLI

- **Dispatch.** A first token that is an option other than `-h`/`--help` selects the
  legacy collection parser. That parser is frozen to the baseline flags: the selectors,
  `--limit-download-to-channel-metadata`, `--output/-o`, and `--min-id`. Anything else
  goes to the canonical parser.
- **Help.** `python main.py` with no arguments prints the overview to stderr and exits 2.
  `--help` exits 0.
- **Builder wrappers.** Each keeps its own `--data-path/-d` parser (`cli.builder_main`).
- **Validation before credentials or service imports** (exit 2):
  - selectors and the batch file;
  - control bounds (`--concurrency` 1–10, `--request-interval` finite 0.1–60,
    `--max-flood-wait` 0–3600, `--max-retries` 0–10);
  - window values (dates parse, `--since` < `--until`, `--max-id` > `--min-id` + 1,
    `--min-id` ≥ 0, `--max-messages` ≥ 1);
  - window options with `--limit-download-to-channel-metadata` (rejected in `collect`;
    the legacy form keeps accepting `--min-id` with it, as before);
  - flags given to the wrong command.
- **Request controls** were parsed and validated but inert in Phase 4; Phase 6 applies them.
- **History window** (`collector.HistoryWindow`, `collect` only):
  - The server receives `min_id`/`max_id` on every page and `--until` as `offset_date`
    on the first page.
  - Every bound is also applied client-side.
  - `--max-messages` shrinks the page `limit` and sends no request once reached.
  - Paging stops at the first page reaching below `--since` or `--min-id`.
  - Unfiltered and legacy `--min-id` requests are identical to Phase 2.
- **Replace warning.** A filtered run warns on stderr, before credentials, when a target
  already has `<channel>_messages.json` in the output folder (count plus the first 10
  names). The run header prints the active window.
- **Missing service dependencies** give exit 1 with `missing dependency '<name>'`.
  Help works without Telethon, pandas, NetworkX, matplotlib, python-louvain, tqdm,
  or openpyxl.

### Phase 2 changes to the collection command

| Behavior | Baseline | Now |
|---|---|---|
| Target selectors | `required=` computed from `sys.argv`; the "both given" check never fired | Required mutually exclusive group: exactly one of `--telegram-channel` / `--batch-file`; `--flag=value` works |
| Unreadable `--batch-file` | Traceback after connecting | Exit 2 before credentials are read |
| Missing/private/invalid target | Missing: logged; private: crash | Both logged to `_exceptions-channels-not-found.txt`; the run continues |
| Exit code | 0 unless a traceback | 0 all targets collected; 1 any target failed or login failed; 130 interrupted; 2 argument errors |
| Empty history, empty batch, all targets failed | Crash | Empty `<channel>_messages.json`; header-correct `counter.csv` / `collected_chats.csv` |
| Batch pause | `time.sleep(2)` blocking the loop | `await asyncio.sleep(2)` between resolved channels; removed in Phase 6 (the request policy spaces requests) |

### Phase 3 changes to the builders

`build-datasets.py` and `channels-to-network.py` are thin wrappers around `datasets.build_dataset`
and `network.build_network`. Outputs, columns, and graph meaning are unchanged: `msgs_dataset.csv`
is still appended to, so a rerun duplicates rows; `collected_chats.xlsx` is replaced; `Graph.gexf`
is a directed, unweighted graph whose repeated observations collapse into one edge.

| Behavior | Baseline | Now |
|---|---|---|
| Python 3.14 / pandas 3 / matplotlib 3.11 | Crash (import-time loop, `infer_datetime_format`, `cm.get_cmap`) | Runs; `colormaps['viridis'].resampled(n)` replaces `get_cmap` |
| Imports | `utils` pulled in Telethon and a loop | No Telethon, config, or session access; plotting loads only when the image is drawn |
| Missing or malformed input (CSV, JSON, `source` literal, `counter`, missing columns or message fields) | Traceback, sometimes after a partial write | Exit 1 with `> build-dataset: ...` / `> build-network: ...` before anything is written |
| Partial writes | Rows appended one by one into `msgs_dataset.csv` | Rows are appended to a copy that replaces the file (`os.replace`), and so are the XLSX, GEXF, and PNG |
| No message files | No `msgs_dataset.csv` | Header-only `msgs_dataset.csv` |
| No edges | Crash in `pd.concat([])` | Empty `Graph.gexf` and a blank `network.png` |
| Equal node degrees | Division by zero | Every node is drawn at size 275 (the middle of 50–500) |
| Messages file order | Filesystem glob order | Sorted paths |
| Source order within a chat | `set` order (varies per process) | First occurrence; the edge set is unchanged |

A row whose `counter` is empty (for example a chat appended by an earlier run that this run's
counter did not cover) is rejected with its line number. The rerun merge itself is fixed in Phase 5.

### Phase 5: one owner for output

`collect_channel` returns a `storage.ChannelResult` (identity, channel JSON, history payload,
ordered chat observations) and writes nothing. `storage.OutputWriter` alone writes files: each
channel's JSON when the channel is complete, and the aggregates at the end by replaying the
committed results in input order. Every chat a channel sees directly is enriched the first
time that channel sees it, through a per-run `collector.MetadataCache`, so the replay always
has the outcome it needs and the aggregates do not depend on completion order.

| Behavior | Phase 4 | Now |
|---|---|---|
| Full-channel requests | One per role: requested channel, newly observed chat, linked chat of every parent (fixture batch: 101, 102, 103 twice; 106 twice) | One per chat per run while it succeeds, shared by concurrent callers; a missing/private chat is remembered, any other failure is not (fix F7) |
| Channel files | `<channel>.json` written before the history was fetched | Both JSON files written after the channel completes, each through a temporary file and `os.replace`; a failed channel keeps its earlier files |
| Aggregates | Appended during the run, merged at the end | Written once at the end, each through a temporary file; exception logs appended at the end |
| Rerun into a folder | `collected_chats.csv` gets `counter_x/_y`, `source_x/_y` | Earlier rows kept as written, this run's rows appended with this run's counts, legacy column names (fix F8). A run that adds no rows leaves the file untouched |
| `collection-status.json` | None | `running` → `complete` / `partial` / `interrupted`; parameters (mode, window bounds); per target: status, reason, channel id, history mode (`replaced`, `merged`, `unchanged`, `created`, `not_collected`), message count, derived `min_id` (`--update`), `stale_history` (metadata only with an earlier messages file). No credentials or message text |
| Output lock | None | `.collection.lock` (O_EXCL) for the run; checked before connecting and taken after login. A stale lock is reported with recovery steps, never removed |
| Builders | Read any folder | Refuse `running` / `interrupted` (exit 1, recovery hint); note a `partial` run, a window, or `--update` on stderr; folders without a status file pass |
| Cancellation / fatal error | Disconnect; aggregates not finalized | Completed channels stay; aggregates finalized from them; status `interrupted` with the reason; lock removed; disconnect once |
| Storage error | Traceback | `> cannot write ...`, exit 1; the channel is `failed`, never reported as collected |
| `--update` (`collect` only) | Not available | See README; exclusive with every window flag and metadata-only (exit 2) |

The legacy form does not get `--update`: its parser stays frozen to the baseline flags, as
decided in Phase 4. Enrichment outcomes are unchanged, including the baseline quirk that an
inaccessible linked chat ends its parent's enrichment as not found (the parent is logged).

Memory: a channel's history is held until its result is written, and the replay keeps every
committed channel's observations (chat ids and usernames, not messages) until the end.
Updates hold the parsed earlier history of a channel while it is collected.

### Phase 6: bounded workers and one request policy

| Behavior | Phase 5 | Now |
|---|---|---|
| Scheduling | One channel at a time, 2 s pause after each | `--concurrency` workers (batch default 3, single channel 1) take targets in input order; pages of a channel stay sequential; results go through a queue of at most N to the one writer. No pause |
| Requests | Sent directly | Every RPC (`get_entity`, both full-channel uses, history) through `request_policy.RequestPolicy`: at most N in flight, `--request-interval` spacing (slots reserved in call order, re-checked after the permit), shared flood cooldown, 60 s per-attempt timeout |
| Flood wait | Telethon slept up to 60 s per request, unseen | Telethon `flood_sleep_threshold=0`; the policy pauses all admissions for the server's seconds and retries; over `--max-flood-wait` or out of retries → account stop: run ends with completed channels saved, status `interrupted`, exit 1 |
| Transient errors | Telethon retried 5 times; then the run aborted | Telethon `request_retries=1` (keeps one DC migration) and `raise_last_call_error=True` (the real error, not a ValueError that would read as "not found"); the policy retries ServerError/TimedOutError/ConnectionError/timeouts with 1, 2, 4 ... s backoff (cap 30) within `--max-retries`, then fails that channel |
| Permanent errors | — | Not retried. Channel access errors fail the channel; `UnauthorizedError`/`AuthKeyError` stop the run (`session rejected`) |
| Discovered private chat | Run aborted (`ChannelPrivateError`) | Logged as `ID - <id>` in `_exceptions-users-not-found.txt`, like a missing chat (fix F9) |
| Targets | Raw lines | Normalized (`@name`, t.me links → `name`), blank lines skipped, case-insensitive duplicates dropped, unsupported forms rejected with the line number (exit 2); targets resolving to an earlier target's channel are skipped as `duplicate` (decided in input order) |
| Statistics | — | Per method class (`resolve`, `channel`, `history`, `enrichment`): requests, retries, flood waits, flood-wait seconds, in the summary and `collection-status.json` (D2). A full-channel response shared by the cache is counted under the class that requested it first |
| Updates | Telethon default | `receive_updates=False` |

One process, one client, one session: login runs once, before any worker starts. Every worker
is awaited before the single disconnect.

**Fake-latency experiment** (`python tests/scheduler_experiment.py`, virtual clock): 10 generated
channels × 3 pages plus an alias, a missing and a private target; every fake request takes
1.0 s, `--request-interval 0.25`.

| `--concurrency` | Peak in flight | Virtual seconds | Requests | Succeeded / failed / duplicate |
|---|---|---|---|---|
| 1 | 1 | 66.00 | 66 | 10 / 2 / 1 |
| 3 | 3 | 26.00 | 66 | 10 / 2 / 1 |
| 10 | 5 | 18.75 | 66 | 10 / 2 / 1 |

Output files are byte-identical across the three runs (status file excluded). At 10, the
0.25 s gap, not the worker count, limits overlap (1.0 s / 0.25 s). This shows scheduler behavior
under these assumptions only, not Telegram latency, throughput, or safe rates.

Tests run collections on `tests/virtual_time.VirtualTimeLoop`, a selector loop whose clock jumps
to the next timer, so spacing and cooldowns cost no wall time.

### Login

An authorized session (`./session_file.session`) skips every prompt. Otherwise the
collector requests a login code, prompts `Enter the code: `, and signs in. When the
account has two-step verification, Telegram answers with `SessionPasswordNeededError`
and the collector prompts `Enter your two-step verification password: ` without echo
(`getpass`; it refuses to fall back to an echoing prompt), then signs in on the same
client, which creates the session.

- At most 3 attempts each for the code and the password; an expired code stops at once.
- A rate limit (`FloodWaitError`) during the code request or sign-in stops the run. The
  login temporarily sets Telethon's `flood_sleep_threshold` to 0 so nothing retries
  silently, then restores it for collection.
- Unavailable input (EOF) and Telegram refusals exit 1 with a message that names the
  failing step only. Ctrl+C at a prompt exits 130. The client disconnects once in every
  case.
- The password is never a CLI argument and is never written, logged, or kept beyond
  the `sign_in` call.
- Prompts need a real terminal: run `python main.py ...` inside an activated
  `telegram-tracker` environment. `conda run` does not attach the terminal's input by
  default.

## Known limitations at baseline

| Area | Limitation | Resolution |
|---|---|---|
| Event loop | `main.py` and `utils/__init__.py` call `asyncio.get_event_loop()` at import. Python 3.14 raises `RuntimeError: There is no current event loop`, so the collector and both builders (which import `utils`) fail before doing any work. | Resolved in Phase 2: one `asyncio.run` per collection; no import-time loop |
| Event loop | Telethon 1.45 binds a client to the loop that connects it and refuses use from another loop; creating a client outside a running loop makes Telethon create and set a global loop. | Resolved in Phase 2: the client is created, connected, and disconnected inside one running loop |
| Import side effects | `main.py` parses argv, reads `config/config.ini`, and connects at import. | Resolved in Phase 2: `main()` entry guard; `utils` no longer imports Telethon |
| pandas 3 | `pd.to_datetime(..., infer_datetime_format=True)` raises `TypeError`. Used by `utils.get_forward_attrs` (dataset builder) and `utils.timestamp_attrs`. The flag has been a no-op since pandas 2.0, so dropping it preserves behavior. | Resolved in Phase 3 |
| matplotlib 3.11 | `matplotlib.cm.get_cmap` no longer exists; `channels-to-network.py` uses it. `matplotlib.colormaps[name].resampled(n)` is the replacement. | Resolved in Phase 3 |
| Graph edge cases | No-edge input fails in `pd.concat([])`; equal node degrees divide by zero in `normalize_values`. | Resolved in Phase 3 |

The test suite pins the pandas and matplotlib removals and checks that project code no longer calls them in
`tests/test_environment.py::KnownLibraryRemovalTests`, so a dependency change is visible.

## Offline test harness

- `tests/offline_guard.py` is installed by every test module. It blocks socket connects
  (except the loopback `socketpair()` asyncio needs on Windows), DNS, asyncio
  connections, real `TelegramClient` construction and auth methods, and any open of
  `config.ini` or `.session` files. Every attempt is recorded, and the base test cases
  fail a test that triggered one, even if the code swallowed the exception.
- `tests/fakes.py` provides `FixtureWorld` (synthetic Telegram data with server-style
  pagination), `FakeTelegramClient` (connect, authorization check, code request,
  sign-in with an optional password challenge, entity lookup, RPC call, disconnect,
  with scripted exceptions and delays), and `ScriptedPrompt` (code/password prompts).
- `tests/fixtures/telegram_world.json` holds synthetic channels (`*_fx`): two requested
  channels sharing a discovered chat, an empty channel, missing and private targets,
  Unicode text, forwards, replies, service messages, and web page, document, poll,
  geo, contact, and venue media.
- Collection modules: `collector.py` (async run, workers, pagination, enrichment cache,
  channel results), `request_policy.py` (admission, cooldown, retries, statistics), `storage.py` (counting replay, every output file, lock, status sidecar, history
  merge), `api/__init__.py` (Telethon request helpers and client factory), `api/auth.py`
  (login), `utils/__init__.py` (pure helpers). Fakes replace the client and the
  prompts; no test constructs a real client.
- Builder modules: `datasets.py` and `network.py`. `tests/builder_data.py` collects the
  batch scenario once with the fake client and copies it into each test's temporary
  directory, and runs the wrapper scripts in a fresh guarded interpreter that reports
  whether Telethon, the collector, or matplotlib were loaded.
- `tests/fixtures/expected/` records expectations from static inspection of the
  baseline code: `collection.json` (request sequence, files, counter, chats, JSON
  fields, and the intended-fix ledger F1–F6), `dataset.json`, and `network.json`
  (checked by the Phase 3 builder tests). Preserved behavior and intended fixes are listed separately there.
