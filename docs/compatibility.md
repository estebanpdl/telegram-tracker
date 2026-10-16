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

Baseline behavior at commit `9b0ec71`, before the CLI phases. The canonical
`python main.py <subcommand>` forms are planned in Phase 4 and do not exist yet.

| Invocation | Inputs | Outputs | Planned canonical form |
|---|---|---|---|
| `python main.py --telegram-channel NAME` | one channel; `--limit-download-to-channel-metadata`, `--output/-o DIR` (default `./output/data`), `--min-id N` | `<DIR>/<NAME>/<NAME>.json`, `<NAME>_messages.json`, `chats.txt`, `collected_chats.csv`, `counter.csv`, `_exceptions-channels-not-found.txt`, `_exceptions-users-not-found.txt` | `python main.py collect --telegram-channel NAME` |
| `python main.py --batch-file FILE` | one target per line; same options | same, one folder per channel | `python main.py collect --batch-file FILE` |
| `python build-datasets.py [--data-path/-d DIR]` | `<DIR>/**/*_messages.json`, `<DIR>/collected_chats.csv` | `msgs_dataset.csv` (append), `collected_chats.xlsx` (replace) | `python main.py build-dataset --data-path DIR` |
| `python channels-to-network.py [--data-path/-d DIR]` | `<DIR>/collected_chats.csv` (`username`, `counter`, `source`) | `Graph.gexf`, `network.png` | `python main.py build-network --data-path DIR` |

Authentication: a session without authorization requests a login code, prompts for it,
and signs in. Phase 2 adds the Telegram cloud-password (two-step verification) step to
the same flow.

## Known limitations at baseline

| Area | Limitation | Resolution |
|---|---|---|
| Event loop | `main.py` and `utils/__init__.py` call `asyncio.get_event_loop()` at import. Python 3.14 raises `RuntimeError: There is no current event loop`, so the collector and both builders (which import `utils`) fail before doing any work. | Phase 2 (one `asyncio.run` per collection, no import-time loop) |
| Event loop | Telethon 1.45 binds a client to the loop that connects it and refuses use from another loop; creating a client outside a running loop makes Telethon create and set a global loop. | Phase 2 (create, connect, and disconnect inside one running loop) |
| Import side effects | `main.py` parses argv, reads `config/config.ini`, and connects at import. | Phase 2 (entry guard; services importable) |
| pandas 3 | `pd.to_datetime(..., infer_datetime_format=True)` raises `TypeError`. Used by `utils.get_forward_attrs` (dataset builder) and `utils.timestamp_attrs`. The flag has been a no-op since pandas 2.0, so dropping it preserves behavior. | Phase 3 |
| matplotlib 3.11 | `matplotlib.cm.get_cmap` no longer exists; `channels-to-network.py` uses it. `matplotlib.colormaps[name].resampled(n)` is the replacement. | Phase 3 |
| Graph edge cases | No-edge input fails in `pd.concat([])`; equal node degrees divide by zero in `normalize_values`. | Phase 3 |

The test suite pins the pandas and matplotlib removals in
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
- `tests/fixtures/expected/` records expectations from static inspection of the
  baseline code: `collection.json` (request sequence, files, counter, chats, JSON
  fields, and the intended-fix ledger F1–F6), `dataset.json`, and `network.json`
  (for Phase 3). Preserved behavior and intended fixes are listed separately there.
