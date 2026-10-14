# Unified CLI and concurrent batch collection — Implementation plan

**Project:** Telegram Tracker — `projects/telegram-tracker/`

**Planning status:** Draft for Maya and Esteban; application implementation is not authorized by this document.

**Prepared:** 2026-10-14 by Prime; intended implementer: Logan.

**Task:** `cc81a8a0-8c9e-4a42-828e-32f40f337f9b`

**Baseline:** `main` at `d194eab65dcabf3a066e3cca0bab9bd0f6cbd692`; clean before planning.

**Plan branch:** `plan/cli-unification-concurrency`.

## 1. Objective and scope

**Problem or opportunity:** Collection, dataset construction, and network construction have three separate script interfaces. Batch collection serializes all channels. Simply adding threads would leave nested event-loop calls, shared counters, output files, and a shared Telethon session exposed to races.

**Desired outcome:** One documented CLI supports `collect`, `build-dataset`, and `build-network`. Batch collection overlaps independent channel requests with a bounded worker count, one client/session owner, shared pacing, and predictable partial-failure behavior. Existing commands and downstream file formats remain usable.

**In scope**

- Import-safe services, an argparse dispatcher, compatibility adapters for all three scripts, and Python 3.14 compatibility in affected paths.
- Configurable asyncio batch workers; account-wide request admission and flood-wait handling, including metadata-enrichment requests.
- Serialized aggregate persistence, duplicate-target protection, orderly cancellation, synthetic offline tests, and README/operator guidance.
- Narrow defects that prevent these paths from operating: empty history, all-failed batches, inconsistent counter keys, empty/equal-degree graph rendering, and dependency compatibility established in Phase 1. Record each intentional correction against a fixture.

**Out of scope**

- Package publishing, a new installed console command, UI, automatic collect→dataset→network chaining, multi-account collection, or collection in threads/processes.
- Changes to graph meaning/weights, dataset columns, general data-quality cleanup, a new database, durable page-level resume, or merging old and new message histories. `--min-id` retains its current fetch-filter meaning.
- Live Telegram requests, login/code requests, reading credential values or session files during this planning task. No dependencies are installed by this task.

**Success criteria:** All three canonical commands and their legacy equivalents pass an offline compatibility matrix. Instrumented fake requests prove actual overlap with `--concurrency > 1`, never exceed the bound, and pause together after a flood wait. Concurrent and serial runs produce equivalent normalized outputs for the same fixtures, including shared discovered chats. Errors preserve completed outputs and yield documented nonzero exits. Approval and offline evidence precede any merge or live validation.

## 2. Verified baseline and constraints

Line references below describe the baseline commit, not proposed files.

| Verified finding | Evidence and implication |
|---|---|
| Collection executes at import; no functions or entry guard | `main.py:29-79`, `110-125`, `162-404`: parses args/config, obtains a loop/client, and drives each request with `run_until_complete`. Do not import/run it to inspect help. |
| There is a second, hidden event-loop owner | `utils/__init__.py:19-42`, `81-91`, `95-215`: imports the API, creates a loop at import, and performs enrichment requests inside the output helper. Both offline builders import this module. |
| Authentication can issue a real code request | `api/__init__.py:21-37`: `get_connection` connects, checks authorization, then calls `send_code_request` and `sign_in`. Tests must replace this boundary; no real client/session is needed. |
| Pagination requests 100 messages per page | `api/__init__.py:116-137`; `main.py:237-309` accumulates messages/chats/users in memory. Initial empty messages cause `min([])` before the loop condition. First-page chat observations also differ from subsequent-page observations; characterize before changing counts. |
| Shared output is intertwined with API enrichment | `main.py:220-233`, `287-295`, `350-396`; `utils/__init__.py:115-215`: appends chat IDs and metadata, updates a shared counter, then rewrites/merges CSVs. The counter uses both `channel_req_targeted_by` and `channel_request_targeted_by`; a broad `KeyError` handler can hide this. |
| Actual exception filenames differ from README | `main.py:339`: `_exceptions-channels-not-found.txt`; `utils/__init__.py:197`: `_exceptions-users-not-found.txt`. README lists `user_exceptions.txt`. Preserve actual filenames and correct documentation. |
| Dataset script is offline logic with import-time execution | `build-datasets.py:27-63`, `75-79`, `319-344`: `--data-path/-d`, default `./output/data`, recursive message JSON discovery, append-mode `msgs_dataset.csv`, and `collected_chats.xlsx`. It does not write enriched statistics back to `collected_chats.csv`. |
| Network consumes collection CSV directly | `channels-to-network.py:29-47`, `57-119`, `151-152`: reads `username`, `source`, `counter`; writes `Graph.gexf` (case-sensitive name) and `network.png` in the data directory. The exported graph is a directed graph without edge weights; repeated edges collapse. Dataset building is not a prerequisite. |
| Graph boundaries need handling | `channels-to-network.py:92-101`, `119-125`; `utils/__init__.py:578-589`: no-edge input can reach empty concat/partition; equal degree causes division by zero in normalization. |
| No tracked test suite or package configuration | `git ls-files` lists the three scripts, `api/__init__.py`, `utils/__init__.py`, README, and requirements; no tests, pyproject, setup configuration, or environment manifest. `requirements.txt:1-7` has seven unpinned dependencies. |
| Registered environment is empty of project packages | Safe metadata-only check via `conda run -n telegram-tracker python <agent scratch script>`: Python **3.14.7**, executable `D:/miniforge3/envs/telegram-tracker/python.exe`; Telethon, pandas, openpyxl, tqdm, NetworkX, matplotlib, python-louvain, and pytest all reported **not installed**. No project module was imported. |
| Fresh-process loop creation is incompatible with 3.14 | A stdlib-only `asyncio.get_event_loop()` probe raised `RuntimeError: There is no current event loop in thread 'MainThread'.` This confirms the runtime behavior; actual script execution was not attempted because dependencies are missing. |

**Relevant existing components:** Reuse the small coroutine API helpers in `api/__init__.py`, serialization/column/extraction helpers in `utils/__init__.py`, and the existing dataset and graph algorithms. Extract responsibilities rather than rewriting analytical behavior.

**Constraints and invariants**

- Work in a feature branch. This task delivers only this plan; no push, merge, code edits, dependency installation, or real-data validation.
- Use `conda run -n telegram-tracker ...` for project Python, tests, and installation once implementation is approved. Verify interpreter first. Never install into Instinkt's runtime.
- Never read, print, copy, move, or commit `.session` files or real `config/config.ini` values. Help, parsing, builders, and tests must not access them.
- One client and one event loop own one session for a collection run. Do not run a second collector using that session, even with a different output directory.
- Preserve output names, encoding, JSON structure, column names/order, numeric IDs, and `source` as a list serialized in CSV so `ast.literal_eval` remains compatible. CSV row ordering may become deterministic; consumers should not rely on legacy set iteration order.
- Default data paths remain relative to the invocation working directory, as today. Do not silently relocate credentials, sessions, or output paths when extracting modules.

**Unknowns and assumptions**

- **Unknown:** Compatible dependency versions for this Python 3.14 environment. Resolve in Phase 1 with import/fixture checks; no installed-version or live Telethon compatibility claim is made here.
- **Assumption:** Target Telethon's stable **1.x** API, matching existing `TelegramClient` helpers. Pin a tested version below 2; any major-version migration needs a separate decision.
- **Unknown:** A safe account-specific Telegram request rate or expected speedup. Defaults below are proposed operating limits, not a Telegram allowance or a performance guarantee. No account/session inspection is needed to resolve the architecture.

## 3. Proposed technical design

### CLI and service boundaries

Keep argparse and use `python main.py <subcommand>` as the canonical interface. Do not add Typer/Click or package installation for this initiative.

| Canonical command | Inputs and behavior | Compatibility |
|---|---|---|
| `python main.py collect ...` | Exactly one of `--telegram-channel` or `--batch-file`; preserve `--limit-download-to-channel-metadata`, `--output/-o`, `--min-id` and add the controls below | Legacy `python main.py --telegram-channel ...` and `--batch-file ...` normalize to `collect` |
| `python main.py build-dataset --data-path PATH` | Preserve `--data-path/-d`, default `./output/data`; operate on local files | `build-datasets.py` becomes a thin adapter to the same handler |
| `python main.py build-network --data-path PATH` | Preserve `--data-path/-d`, default `./output/data`; requires collection CSV, not a dataset run | `channels-to-network.py` becomes a thin adapter to the same handler |

`main(argv=None) -> int` builds the parser, validates, and dispatches; `if __name__ == '__main__': raise SystemExit(main())` is the only executable entry. No args and top-level `--help` display the command overview without credentials. Subcommand help is similarly offline. Use a required mutually exclusive group for collection inputs instead of inspecting `sys.argv`. Retain valid legacy flags, including `--flag=value` forms; rejecting both target selectors is a deliberate correction of current unreliable validation.

**Proposed new modules** (names are design proposals, not existing code):

- `cli.py`: parser, legacy argument normalization, lazy command dispatch, exit mapping.
- `collector.py`: async run/channel orchestration and pagination; injectable client factory, request policy, and output sink.
- `storage.py`: aggregate reducer and output writer; no network calls.
- `datasets.py`, `network.py`: callable offline operations, reusing the existing transformations.
- `request_policy.py`: shared request admission, retry/cooldown state, injected monotonic clock/sleeper.
- Keep `api/__init__.py` as the narrow Telethon adapter; split or lazily import the network-dependent helpers currently in `utils`. Pure helpers must not create a loop or import Telethon as a side effect.

Dispatch imports only the selected service: collecting does not load plotting libraries; builders do not load Telethon/config/authentication. Dataset/network services return a small output summary rather than parsing argv. The wrappers contain no copied business logic. Canonical `--help` should work even before optional service dependencies are installed; service invocation reports a useful missing-dependency error.

### Concurrency choice and request controls

**Recommendation:** One process, one `asyncio.run(run_collection(...))`, one Telethon client created and disconnected inside that coroutine, and a fixed pool of channel worker tasks. Pages within one channel remain sequential. This fits the existing coroutine API and avoids sharing a SQLite-backed session across worker clients.

| Alternative | Assessment |
|---|---|
| Threads | Network I/O could overlap, but each thread needs correct loop ownership or cross-thread scheduling. Sharing the current client/counters is unsafe; separate clients reuse the SQLite session. Adds coordination without solving account-wide rate limits. |
| Processes | Useful for independent CPU work, but Windows process startup, client/session serialization, multiple session writers, memory copies, and aggregate IPC make it a poor collection model. More processes do not grant more account quota. |
| Asyncio tasks | Reuses native awaitables and one session owner; enables bounded overlap and one shared cooldown. Blocking serialization/pandas work can still stall the loop, so keep writes bounded and measure before considering a separate I/O executor. |

Telethon's [asyncio guide](https://docs.telethon.dev/en/stable/concepts/asyncio.html) supports tasks for concurrent I/O and explains thread/loop coordination. Its [FAQ](https://docs.telethon.dev/en/stable/quick-references/faq.html) documents session lock conflicts with multiple clients and the need to disconnect. These support the recommendation; they do not establish an account-specific safe rate. Python's [3.14 event-loop documentation](https://docs.python.org/3.14/library/asyncio-eventloop.html) documents the fresh-loop error and recommends high-level runners.

Proposed collection controls, subject to decisions D1–D3:

| Argument | Default | Validation and meaning |
|---|---|---|
| `--concurrency N` | **3** for batch, effective **1** for a single channel | Integer 1–10; fail outside bounds. Same defaults for canonical and legacy collection. `1` provides serial scheduling for diagnosis/rollback. Report requested/effective counts. |
| `--request-interval SECONDS` | **1.0** | Finite float 0.1–60 inclusive; minimum spacing between application RPC admission times across the account, including retries and enrichment. Independent of channel count. |
| `--max-flood-wait SECONDS` | **60** | Integer 0–3600; maximum automatic wait for an individual server flood response. `0` means stop on any positive flood wait. Never shorten a server wait. |
| `--max-retries N` | **3** | Integer 0–10; additional attempts per logical RPC shared by flood and transient failures; 0 means one attempt. No retry for permanent access/auth/input/output errors. |
| `--interactive-login` | **False** (proposed behavior change; D3) | Collection only. Allows the existing interactive login workflow when the operator deliberately opts in. Without it, an unauthorized client fails without sending a code. |

All options are collection-local; builders reject them. Validate input paths, bounds (including NaN/infinity), and readable batch contents before config/client creation. Retain bare usernames and numeric IDs; normalize `@name` and equivalent public `t.me/name` forms to one target. Skip blank lines, deduplicate canonical identities preserving first occurrence, and reject unsafe/unsupported path-like targets with line numbers. After entity resolution, coalesce distinct aliases with the same channel ID before history collection/output. Do not join channels or expand target scope. Plan fixtures must cover case variants, duplicates, and Windows path traversal.

**Request policy:** Every application RPC, including `get_entity` resolution and discovered-chat/participant enrichment, passes through one shared admission helper. A run-wide semaphore bounds actual in-flight application calls to `N`, including shared enrichment tasks; do not hold its permits while sleeping between attempts. Use a separate lock only to reserve the next request slot against a monotonic clock; release it before awaits. Waiting tasks recheck spacing and cooldown after acquiring a permit and just before dispatch, so an already reserved slot cannot bypass a later flood response. Client/session operations remain on the same event loop. Disable update reception for the collector if supported by the selected 1.x version.

On `FloodWaitError`, read the server's seconds, extend a shared deadline to `max(existing_deadline, now + seconds)`, and pause admission for every worker. Already in-flight requests may finish; concurrency cannot recall them. If within the cap and retries remain, asynchronously wait and retry the same RPC; add no rapid retry loop. If over the cap or retries exhausted, stop the whole account's batch, cancel pending/in-flight work cooperatively, persist completed results, and report the server delay. Never let other channels continue submitting requests after account-wide refusal.

Configure Telethon `flood_sleep_threshold=0` and finite retry/reconnect settings so application cooldown and retry ownership are observable. Select settings in Phase 1 that prevent hidden transient/flood retry multiplication; retain essential library protocol handling such as DC migration. For retryable transport timeout/server failures, use bounded exponential backoff (1, 2, 4 seconds, capped at 30), then pass admission again. Apply a 60-second per-attempt application timeout. Invalid/private/deleted channels fail only that channel; invalid/revoked authorization and session/storage corruption abort the run. Never turn broad RPC errors into unlimited retries. Verify exact error classes against the chosen 1.x version, using the [RPC error reference](https://docs.telethon.dev/en/stable/concepts/errors.html) and [client retry settings](https://docs.telethon.dev/en/stable/modules/client.html).

### Output ownership, compatibility, and recovery

Workers own only channel-local pagination state. They produce `ChannelResult` records containing resolved identity, metadata/history payloads, ordered chat-observation deltas, and structured failures. Use a bounded result queue (at most `N` waiting results) and a single writer/reducer. Avoid an unbounded task per input or a `gather` holding every channel's complete history. The current per-channel message accumulation remains for this release; memory can still grow with the largest `N` channels and must be disclosed.

The reducer alone owns counters, discovered-chat rows, source lists, exception logs, and public output files. Separate async enrichment from reduction: use a per-run ID→in-flight-task cache so a discovered chat is fetched once even when several workers encounter it. Cache successful metadata, account for all observation events, and do not treat a transient failure as permanent cache success. No lock is held while requesting enrichment. Bound enrichment calls within the same worker/request limits; do not spawn unlimited enrichment tasks. Preserve source/counter semantics using the Phase 1 fixtures, including repeated observations within one source. Ordering of successful metadata selection must be explicit (earliest input/page occurrence), not completion timing.

Keep `<channel>/<channel>.json`, `<channel>/<channel>_messages.json`, `chats.txt`, `collected_chats.csv`, `counter.csv`, and actual exception filenames. Replace successful JSON and final aggregate snapshots through same-directory temporary files plus `os.replace`; one writer opens handles with context managers. Initialize header-correct empty aggregates for a fresh empty/all-failed result instead of relying on `chats_file` or CSVs existing. A metadata-only run does not create a new message-history file. Preserve an earlier complete history if a subsequent history request fails; explicitly report any metadata/history generation mismatch.

Preserve current cross-run semantics: collection replaces a requested channel's JSON (including the filtered history from `--min-id`), while discovered-chat metadata/exception logs append and counters describe the current invocation. Do not introduce automatic historical message merging, deduplicate historical rows, or reinterpret existing source columns under this refactor. Test rerunning into an existing output directory: prevent accidental `_x/_y` counter/source columns when merging fresh counters and preserve unrelated channel files. Dataset building continues append-mode message CSV behavior; warn that rerunning can duplicate rows. An overwrite/idempotence feature is deferred to separate approval.

For cancellation or a channel failure, commit only fully collected channel results; finalize aggregates from completed channels, close all files, and disconnect once in `finally`. Save a small `collection-status.json` sidecar with overall state and per-request input/identity/status, without credentials or message text. Failed and cancelled channels remain distinguishable from successful empty histories. Builders reject a sidecar marked `running`/`interrupted` and explain recovery; a finalized partial run is allowed with a warning about excluded failures. Existing directories without a sidecar stay supported.

Atomic replacement is **per file**, not a transaction across every output. A hard process/OS crash may leave mixed aggregate generations; the sidecar must remain `running` until finalization completes. Do not claim crash-safe resume. Recover an interrupted directory by keeping it for inspection and rerunning collection into a fresh output directory; completed individual JSON files remain readable. No new builder silently deletes or repairs existing files. Document that builders must run after collection finishes, and that separate collector processes must not share the account/session or output directory. Add an exclusive output-directory lock to reject accidental same-directory runs; never lock by opening/reading the real `.session` file. A stale lock requires explicit recovery instructions, not automatic PID guessing.

**Exit contract:** `0` for successful requested work (including valid empty data); `2` for parsing/input errors before authentication; `1` for channel partial failures, account/auth/dependency/storage failures, or retry exhaustion; `130` for Ctrl+C. Summaries show requested, deduplicated, succeeded, failed, and cancelled counts plus output locations, with bounded diagnostic text and no credential/message dumps. These exits become shared by legacy adapters; document the tightened error signaling.

## 4. Phase map and execution strategy

| Phase | Outcome / deliverable | Prerequisites | Relative effort / risk | Verification gate |
|---|---|---|---|---|
| 1 | Reproducible environment and offline contracts | Plan approval; D1–D3 recorded | Medium: empty env and no test baseline | Dependency/import evidence and synthetic fixture review |
| 2 | Import-safe, sequential async collector | 1 | High: nested loop and auth lifecycle | Fake-client pagination/lifecycle tests |
| 3 | Callable offline dataset and network builders | 1; pure-helper boundary agreed with 2 | Medium: preserve analytical meaning | Fixture outputs, graph readback, no auth/API imports |
| 4 | Unified CLI and legacy adapters | 2, 3 | Medium: dispatch and compatibility | Canonical/legacy parsing and output matrix |
| 5 | One owner for persistence and aggregate state | 2, 4 | High: counter/source semantics and partial writes | Shared-chat, rerun, failure, and cancellation fixtures |
| 6 | Bounded concurrent batch with account policy | 5 | High: admission/flood/cancellation interleavings | Deterministic overlap and virtual-clock failure tests |
| 7 | Operator documentation and release evidence | 4–6 | Medium: integration review and operational clarity | Full offline matrix, README examples, approval handoff |

**Implementation order:** 1→2→4→5→6→7 is the collection critical path; Phase 3 can proceed independently after Phase 1 once pure-helper boundaries are agreed. This describes code independence, not an instruction to enroll or assign more agents. Maya coordinates ownership; Logan implements after approval. Add focused tests and relevant doc notes with each phase so Phase 7 is integration and consolidation, not a testing backlog.

**Phase-design check:** Async lifecycle, builder extraction, CLI integration, persistence, and concurrency each have a distinct gate. Phase 6 has seven steps because policy and scheduler interleavings must be validated together; it assumes already working serialization, CLI, and fake fixtures rather than absorbing them.

## 5. Phased implementation

### Phase 1 — Establish environment readiness and offline contracts

**Status:** Pending

**Objective:** Give Logan reproducible dependencies and independent expected behavior before refactoring.

**Prerequisites:** Maya/Esteban approve the plan and record D1–D3; no live authorization is implied.

**Implementation steps**

- [ ] **1.1** Verify `conda run -n telegram-tracker python ...` interpreter, then resolve/install dependencies only in that environment. Select a tested Telethon 1.x set compatible with Python 3.14; record/pin reproducible versions in the project requirements/environment documentation. Escalate any need to change Python or use Telethon 2.
- [ ] **1.2** Add a stdlib `unittest` harness using `IsolatedAsyncioTestCase`, `AsyncMock`, temporary directories, and synthetic fixtures. Block real socket creation/client authentication and assert zero config/session access. Tests must fail if the network guard is bypassed.
- [ ] **1.3** Create hand-reviewed metadata, multipage history, and collected-chat fixtures with two channels sharing a discovered chat. Cover an empty channel, missing/private channel, duplicate targets, Unicode, forwards/replies/media/service messages, no-edge and equal-degree graphs. Include every field required by the existing selected column helpers.
- [ ] **1.4** Record expected filenames, CSV headers/values, source/counter observations, JSON fields, directed unweighted graph edges, and dataset append behavior from static inspection. Do not generate expectations by importing the current entry scripts. Separate intended fixes from preserved successful behavior.
- [ ] **1.5** Add the initial CLI compatibility table and document current dependency/event-loop limitations. Define fake client methods (`connect`, authorization, entity lookup, RPC call, disconnect) and scripted exceptions/delays without constructing a real client.

**Acceptance:** Dependencies import in the registered environment; fixtures contain no real targets or personal data. The harness blocks network/authentication and supplies explicit expectations for later extraction. Existing defects are labeled, not enshrined as correct output.

**Validation plan:** Record interpreter and resolved package versions; run `conda run -n telegram-tracker python -m unittest discover -s tests -v`. Verify the guard with a test that intentionally attempts a socket and expects rejection. Do not run the legacy collector, even with `--help`.

**Likely affected components:** `requirements.txt`, README; proposed `tests/` and fixtures.

**Delivery evidence:** Pending implementation; no tests/dependencies were added by the planning task.

**Unfinished / deferred:** Account rate calibration and real Telegram compatibility remain outside this phase.

### Phase 2 — Make collection import-safe and asynchronous

**Status:** Pending

**Objective:** A sequential collector operates through one async lifecycle and can be tested without credentials.

**Prerequisites:** Phase 1.

**Implementation steps**

- [ ] **2.1** Extract command-independent collection logic into `run_collection`/`collect_channel` services. Remove global loop creation from `main.py` and `utils`; keep imports free of argv parsing, config reads, connection creation, and writes.
- [ ] **2.2** Replace nested `run_until_complete` calls in collection, `write_collected_chats`, and participant enrichment with awaited helpers. Split pure observations/transforms from network enrichment while retaining the Phase 1 count contract.
- [ ] **2.3** Inject the client factory and create/connect/disconnect it within one running loop. Implement D3's auth decision explicitly; no implicit code request when interactive login is disabled. Ensure disconnect happens once after success, failure, and cancellation.
- [ ] **2.4** Preserve 100-message pagination, `min_id` and offsets; handle an empty first/final page, deduplicate repeated message/chat/user identities, and stop with a diagnostic if offsets fail to advance. Keep metadata-only behavior independent of history calls.
- [ ] **2.5** Fix only the counter-key inconsistency and empty/all-failed finalization issues necessary for a sound sequential baseline. Characterize first-page observations and preserve their current count policy unless Esteban approves an explicit correction.
- [ ] **2.6** Add sequential fake-client tests for lifecycle, page requests, metadata-only, `--min-id`, unauthorized sessions, failures, and empty batches. Update internal docstrings to describe the new boundaries.

**Acceptance:** Importing services is inert; no global event loop exists. A fake sequential run produces expected payloads, requests every page once, handles no results, and always disconnects. Production tests never call `send_code_request`, `sign_in`, or real `get_connection`.

**Validation plan:** Focused lifecycle/pagination tests plus the full offline suite; fresh subprocess imports with invalid argv, missing credentials, and no current loop. Validate request arguments, not just method invocation counts.

**Likely affected components:** `main.py`, `api/__init__.py`, `utils/__init__.py`; proposed `collector.py` and tests.

**Delivery evidence:** Pending.

**Unfinished / deferred:** Scheduling remains sequential until Phases 5–6; no speedup claim.

### Phase 3 — Extract offline builders without changing analytical outputs

**Status:** Pending

**Objective:** Dataset and network generation are importable local-file services.

**Prerequisites:** Phase 1; coordinate the pure-helper split with Phase 2.

**Implementation steps**

- [ ] **3.1** Extract the dataset loop into a callable service accepting a data path and returning output locations/counts. Preserve message transformations, selected columns, append CSV behavior, and XLSX enrichment.
- [ ] **3.2** Extract graph construction/rendering into a callable service. Preserve the current directed, unweighted GEXF topology and the undirected graph used for Louvain; do not silently convert observation counts to edge weights.
- [ ] **3.3** Separate pure helpers from API/config/asyncio imports and lazy-load plotting dependencies. Use a headless backend for tests and close figures; check Matplotlib/Louvain/NetworkX APIs against Phase 1 versions.
- [ ] **3.4** Validate missing files, malformed JSON/CSV or `source` literals, and required columns before writing. Empty valid input produces header-correct datasets/empty graph outputs; equal-degree graphs use a finite constant node size. Malformed input fails clearly without corrupting prior outputs.
- [ ] **3.5** Add semantic fixture assertions for CSV/XLSX content and GEXF readback; verify PNG exists and is readable without pixel-golden assertions. Document intentional empty-input corrections alongside builder behavior.

**Acceptance:** Builders run on fixtures with no Telethon/config/session import or access. Output paths/schemas match the baseline; directed edge sets and no-edge behavior are explicit. A second dataset run demonstrates the documented append behavior.

**Validation plan:** Focused builder tests and full suite. Use temporary fixture copies, XLSX readback, GEXF node/edge assertions, and headless image inspection. Do not compare random layout pixels or community numeric labels.

**Likely affected components:** `build-datasets.py`, `channels-to-network.py`, `utils/__init__.py`; proposed `datasets.py`, `network.py`, builder tests.

**Delivery evidence:** Pending.

**Unfinished / deferred:** Weighted graphs, analytical cleanup, and idempotent dataset rebuilds require separate scope.

### Phase 4 — Expose one CLI and preserve legacy invocation

**Status:** Pending

**Objective:** Canonical subcommands and compatibility adapters reach the same tested services.

**Prerequisites:** Phases 2–3.

**Implementation steps**

- [ ] **4.1** Implement argparse subcommands and `main(argv=None)`/entry guard. Add collection controls from Section 3; parsing validates the mutually exclusive target selectors, paths, and bounds before loading credentials.
- [ ] **4.2** Normalize legacy collection arguments only when no recognized subcommand is present. Keep each old builder filename as a thin adapter with `--data-path/-d`; route all handlers through shared services.
- [ ] **4.3** Make top-level and all subcommand/wrapper help offline. Keep errors/deprecation notices on stderr, outcomes on stdout, and implement the common exit contract. Preserve working-directory defaults and argument aliases.
- [ ] **4.4** Add tests for every README invocation form and its canonical equivalent, `--flag=value`, invalid/both/missing selectors, invalid controls, missing input, and incorrect cross-command flags. Assert rejected commands never create a client or read credentials.
- [ ] **4.5** Add a migration table and command-help excerpts to README as the interface lands. Retain wrappers throughout the first release; any removal requires a dated, separately approved change.

**Acceptance:** The same input reaches the same handler via canonical and legacy forms. CLI help works with no credentials/session and without service dependencies. Invalid input fails with exit 2 before connection; builders are fully offline.

**Validation plan:** Parser tests and subprocess help/legacy-builder fixture tests in the registered environment. Collection subprocess tests use fake dispatch or a test-only injected client boundary; never execute production collection with a real client.

**Likely affected components:** All three entry scripts, README; proposed `cli.py`, CLI tests.

**Delivery evidence:** Pending.

**Unfinished / deferred:** Installed command/package distribution remains outside scope.

### Phase 5 — Give aggregate state and files one owner

**Status:** Pending

**Objective:** Persistence is correct before multiple channel workers can complete out of order.

**Prerequisites:** Phases 2 and 4.

**Implementation steps**

- [ ] **5.1** Define channel-result and chat-observation records with source input index/page ordinal. Move all shared counters/source sets, metadata selection, and public file writes to one reducer/writer.
- [ ] **5.2** Add per-run shared metadata lookup caching with in-flight request sharing and deterministic merge rules. Keep network work outside writer state mutation; retain every required observation count while avoiding duplicate enrichment for the same discovered chat.
- [ ] **5.3** Write completed channel JSON and aggregate snapshots through temporary files/atomic replacement. Support fresh empty/all-failed runs, partial success, existing-directory reruns, and stable legacy column names without merge suffixes.
- [ ] **5.4** Add status-sidecar lifecycle, directory exclusion lock, and compatibility checks in builders as described in Section 3. Preserve earlier complete history on failure and mark metadata/history mismatch; keep exception filenames unchanged.
- [ ] **5.5** Implement graceful writer drain/finalization and cancellation cleanup. Propagate storage errors as run-fatal, cancel producers, and never report a failed write as a successful channel.
- [ ] **5.6** Test shared chats, repeated observations, reversed completion order, failed enrichment, failure between file replacements, no successful channels, existing outputs, and Ctrl+C-equivalent task cancellation. Document per-file atomicity and fresh-directory crash recovery.

**Acceptance:** No worker mutates shared output; reducer output is independent of completion order. No lost counter increments, duplicated CSV headers, `_x/_y` columns, half JSON, or uninitialized file references occur. Successful empty, failed, cancelled, and interrupted outcomes remain distinct. Builders reject known incomplete generations.

**Validation plan:** Reducer fixtures, injected I/O failure and cancellation tests, CSV/JSON/schema readback, and full offline suite. Use synthetic directories only; test lock collisions using synthetic output paths, never real sessions.

**Likely affected components:** Collection/output code extracted from `main.py` and `utils/__init__.py`, builders; proposed `storage.py`, result records, persistence tests.

**Delivery evidence:** Pending.

**Unfinished / deferred:** Cross-file crash transactions and page-level resume are explicitly unsupported.

### Phase 6 — Add bounded workers and one account request policy

**Status:** Pending

**Objective:** Batch channels overlap under configurable bounds without bypassing rate limits or corrupting output.

**Prerequisites:** Phase 5; D1–D3 remain recorded in the approved plan.

**Implementation steps**

- [ ] **6.1** Parse/normalize/deduplicate batch targets before authentication; coalesce resolved aliases by channel ID. Start at most `N` channel workers and a bounded result queue, with sequential pages per channel and no unbounded enrichment tasks.
- [ ] **6.2** Add one injectable request policy used by every collection/enrichment RPC. Implement a global in-flight semaphore, monotonic admission spacing, a shared cooldown, rechecks immediately before dispatch, and the 60-second per-attempt timeout; no concurrency permit spans backoff/cooldown sleeps.
- [ ] **6.3** Configure Telethon's finite retry behavior and disable positive automatic flood sleeps. Implement server-seconds handling, retry limits/backoff, and a run-fatal account stop for over-cap/exhausted flood waits. Count retries explicitly and expose waits without secrets.
- [ ] **6.4** Classify channel-local access errors, run-fatal auth/session/storage errors, and transient failures. Handle worker exceptions without accidental whole-batch cancellation for a missing channel; propagate cancellation and unexpected faults, with all workers awaited before disconnect.
- [ ] **6.5** Wire the CLI controls, requested/effective worker summary, completed/failed/cancelled counts, and exit codes. Remove the blocking inter-channel `time.sleep(2)`; do not retain it as an additional hidden throttle.
- [ ] **6.6** Add virtual-clock policy tests: one flood pauses all admissions, later floods extend the deadline, reserved slots recheck it, too-long waits stop the account, retries respect limits, and permanent errors never retry. Assert no write handles or state locks span network waits.
- [ ] **6.7** Add scheduler integration tests and a repeatable fake-latency experiment for serial versus concurrent runs. Assert actual overlapping requests, `peak_active <= N`, bounded producer/result queues, equivalent normalized files, one connect/disconnect, and surviving completed output after mixed failures/cancellation. Update concurrency docs with measured fake evidence and its limits.

**Acceptance:** With virtual pacing held constant, fixtures demonstrate actual overlap at `N=3` and serial execution at `N=1`. No more than `N` application RPCs are in flight, including enrichment. All workers share the flood deadline; an account stop prevents new admissions. File/schema/counter equivalence holds regardless of completion order. Client/session is never shared with threads or other processes.

**Validation plan:** Deterministic async tests with injected clock/sleep and event barriers rather than fragile wall-clock speed assertions; focused retry/flood tests plus full suite. Record peak active requests, virtual elapsed time, attempts/waits, successful channels, and output comparisons for `N=1,3,10`. Fake latency demonstrates scheduler behavior only, not Telegram throughput or safe quotas.

**Likely affected components:** `api/__init__.py` request boundaries, proposed collector/policy/storage/CLI modules and async tests, README.

**Delivery evidence:** Pending.

**Unfinished / deferred:** Live rate calibration, large-history memory optimization, multiple accounts, and thread/process execution are separate work.

### Phase 7 — Complete operator guidance and integration evidence

**Status:** Pending

**Objective:** Logan hands Maya a usable, documented, offline-validated change with explicit limits.

**Prerequisites:** Phases 4–6.

**Implementation steps**

- [ ] **7.1** Rewrite affected README usage around the canonical commands and migration table; fix repository/title/clone drift and stray backticks. Document Python/environment setup, exact output names, independent builder prerequisites, append/overwrite semantics, partial exits, concurrency defaults/ranges, flood stop behavior, and `--concurrency 1` fallback.
- [ ] **7.2** Document interactive auth policy, single-session/process ownership, stale output-lock handling, interrupted-sidecar recovery into a fresh directory, and why the configurable upper bound is not a Telegram quota. Use synthetic target placeholders; no real account examples or captured message text.
- [ ] **7.3** Run the complete offline matrix from the documented environment and a temporary working/data directory: canonical/legacy commands, all help, collection fake scenarios, dataset/graph readbacks, and dependency checks. Record exact commands/results and limitations in this plan; keep implementation and validation status distinct.
- [ ] **7.4** Review the diff for secrets/schema drift and scope, preserve all earlier evidence, and hand the branch to Maya for review. Resolve D1–D3 and any discovered compatibility exceptions before requesting merge authorization. Do not push, merge, publish, or collect live under this plan alone.

**Acceptance:** Every documented offline example is reproducible on fixtures; legacy invocation remains usable and output changes are explicit. Maya receives test evidence, unresolved limits, and a narrow approval request. No phase is marked complete without its gate evidence.

**Validation plan:** From repository root, `conda run -n telegram-tracker python -m unittest discover -s tests -v`; `conda run -n telegram-tracker python -m pip check`; inspect all command help and the compatibility table. Record test counts and package versions at execution time, not in advance. Live smoke tests, if later requested, require a separate documented purpose/authorization and operator-present login.

**Likely affected components:** README, requirements/environment documentation, this plan, integration tests and evidence.

**Delivery evidence:** Pending.

**Unfinished / deferred:** Merge/release and live validation require separate authorization.

## 6. Cross-phase acceptance and release gates

- [ ] Canonical/legacy dispatch and every existing documented option have offline evidence; invalid inputs fail before auth.
- [ ] Serial/concurrent fixtures preserve JSON/CSV/XLSX schemas and GEXF semantics, with each intentional defect correction documented.
- [ ] Fake-client tests prove overlap, bounded queues/in-flight RPCs, a shared cooldown, finite retries, and no double session owner.
- [ ] Empty, missing/private, duplicate/aliased, metadata-only, incremental-filter, partial-failure, cancellation, and storage-failure cases pass.
- [ ] Completed files survive failures; interrupted generations are detectable; crash/retry limits are accurately documented.
- [ ] Network/auth guards and lazy imports establish zero live calls, code requests, credential reads, or session access during offline validation.
- [ ] Dependencies work in the registered Python environment; README and migration guidance match actual help and outputs.
- [ ] Each phase has separate implementation/validation evidence; Maya reviews and Esteban approves behavior decisions and any merge/release.

**Release / merge authorization:** Maya reviews the design and implementation; Esteban approves D1–D3 and merging/releasing. This planning branch may be committed locally. Neither this plan nor passing fixtures authorizes pushing, merging, live collection, authentication, or paid/external actions.

## 7. Risks, dependencies, and unresolved decisions

| Item | Impact if unresolved | Mitigation / investigation | Owner or decision gate |
|---|---|---|---|
| Missing dependencies; unpinned requirements | Current tools cannot run in the registered env; dependency APIs may differ | Phase 1 tested version set, import checks, headless builder fixtures; no installs during planning | Logan; Maya reviews changes |
| One account's flood limits | More workers may bring earlier waits without improving throughput | Global pacing/cooldown, low proposed default, finite attempts; no quota promises | Logan; D1/D2 |
| Hidden enrichment RPCs and nested loops | Rate limit bypass, loop errors, duplicate metadata | Async extraction plus single-flight metadata lookup, complete adapter coverage | Phases 2, 5, 6 |
| Existing counters/data quirks | A refactor could silently change analytical meaning | Hand-reviewed fixtures; intentional fix ledger; defer weighting/idempotence cleanup | Logan; Esteban for semantic changes |
| Multiple processes reuse the session | SQLite contention and account/session errors | One client; operational prohibition and clear diagnostics; output lock is not an account lock | Logan; Phase 7 documentation |
| Large histories and blocking JSON/CSV work | Memory grows with N; serialization may reduce overlap | Bounded workers/results, disclose current accumulation; measure with synthetic stress data before expanding scope | Logan; no promised speedup |
| Per-file atomicity and existing directories | Hard crash or rerun can mix generations or duplicate legacy rows | Status-sidecar refusal/recovery, fresh-dir crash recovery, existing-output tests; no false transaction/resume claim | Phases 5, 7 |
| Authentication default change | Existing first-use commands may stop instead of requesting a code | Make D3 explicit and document an operator-present opt-in; fixtures only | Esteban before implementation |

**Open decisions needing Esteban's call**

- **D1 — Batch activation:** Approve default `--concurrency 3` (range 1–10) for both canonical and legacy batch commands, with `1` as the serial fallback; or choose opt-in concurrency (`1` by default). Recommendation: 3, behind the shared policy, to deliver concurrent batch behavior by default. Single-channel effective concurrency remains 1.
- **D2 — Operational limits and compatibility window:** Approve the proposed 1-second request interval, 60-second automatic flood cap, three retries, and retention of all legacy adapters through the first release (no removal date yet). These are conservative project defaults, not validated Telegram limits. Recommendation: accept and adjust only with separately authorized evidence.
- **D3 — Authentication:** Approve default fail-fast for an unauthorized session with explicit `--interactive-login` for operator-present onboarding, or require preservation of implicit interactive login for legacy collection. Recommendation: explicit opt-in across both CLI forms; treat the onboarding change as a documented behavior change.

**Blocking questions:** D1–D3 block implementing their dependent behavior, not reviewing this planning deliverable. Maya can seek one grouped operator decision. No decision is presumed approved; Logan records the chosen values here before implementation. Output schema/graph semantics changes or a Telethon major upgrade discovered during implementation require a plan amendment, not an inferred approval.

## 8. Change control and implementation record

Keep this plan as the authoritative record for this initiative. Update phase status and add commands/artifacts as gates pass; do not check boxes based on code existence alone. Record any change of CLI behavior, count semantics, retry policy, dependencies, or recovery guarantees before expanding work. Split a phase or propose follow-up scope when discoveries warrant it.

| Date | Change / discovery | Affected phase(s) | Decision / authorization | Effect on scope and validation |
|---|---|---|---|---|
| 2026-10-14 | Initial repo-grounded plan; verified empty project env and Python 3.14 fresh-loop error | 1–7 | Planning task authorized; implementation approval pending | Adds explicit environment gate and import/lifecycle tests; no application changes |

**Follow-up initiatives:** None opened by this planning task.

**Deferred:** Durable page resume/history merging, idempotent dataset rebuilds, weighted networks, package publishing, multi-account support, and large-history streaming. Maya/Esteban prioritize separately after this scope is reviewed; no competing implementation plan is created here.

## 9. Completion and validation summary

**Overall status:** Planning deliverable ready for review; application implementation pending approval.

**Completed phases:** None; all implementation steps intentionally unchecked.

**Pending or deferred phases:** 1–7 pending; deferred initiatives listed above.

**Planning evidence:** Read all three entry scripts, API/util helpers, README, requirements and ignore rules at the baseline commit; confirmed clean `main`, no existing tracked plan/tests/package configuration; created `plan/cli-unification-concurrency`. Executed `conda run -n telegram-tracker python D:/instinkt/workspaces/osint-lab/agents/prime-bb39bc14/scratch/inspect_tracker_runtime.py` (2026-10-14, exit 0): metadata-only dependency checks and a fresh-process stdlib event-loop probe, with results in Section 2. Checked official Telethon/Python guidance linked in Section 3.

**Tests and evidence:** No application tests executed: no suite exists and application dependencies are absent. No baseline help/entry script was run. Planning validation checks the template sections, seven complete phase structures, unchecked implementation state, source references, and a plan-only repository diff.

**Live or paid validation:** Not performed. No Telegram connection, login/code request, credential-value read, or session-file access.

**Known limitations:** Runtime library compatibility and real-account throughput are unverified; offline simulated concurrency cannot establish either. Cross-file crash transactions and durable resume are outside scope.

**Operator decisions required:** D1–D3.

**Handoff to Maya:** Review this plan and the defaults/behavior decisions with Esteban. After approval, Logan implements phase by phase in a feature branch, updating this record with focused and full offline evidence. This task requests plan review only.
