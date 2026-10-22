# URL occurrences (`build-urls`)

`build-urls` extracts URL occurrences from collected message text, hyperlink targets and stored previews, with parsed parts, registered domains and counts per domain and channel. Extraction follows the evidence rules below; unsupported forms such as URL buttons are omitted. It reads the same sources as `build-dataset` and `build-network`, with the same `--source` choice and the same checks (see the README, "Building the dataset and the network").

```
python main.py build-urls --data-path ./output/data
python main.py build-urls --data-path ./output/project-s --source sqlite
```

It works offline. No link is opened, resolved, redirected or expanded, and no preview is fetched. The public suffix list is the copy bundled with `tldextract` (pinned in `requirements.txt`); downloading a newer list and the library's cache file are both turned off, including when `TLDEXTRACT_PUBLIC_SUFFIX_LIST_URLS` is set. The builder does not need pandas, NetworkX, matplotlib or Telethon.

## Outputs

Every build replaces all three outputs. Nothing is appended, so building again gives the same rows.

| File | Contents |
|---|---|
| `urls_dataset.csv` | One row per occurrence (UTF-8, `\n` line ends; empty cell = null). |
| `urls.sqlite3` | The same rows in `occurrences`, plus `build`, `channels`, `domains` and `channel_domains`. It is a separate file: the collection database is only read. |
| `urls-build.json` | The build record: what was read (as in `dataset-build.json`), `build_uuid`, `normalization_version`, `suffix_list`, and the CSV's row count and SHA-256. |

The database is written under a temporary name and the CSV is written from it, so both hold the same rows. The three files are then replaced in the order above, with the record last. If a build stops between two replaces, the outputs come from different builds. The next build detects this by checking the record's `build_uuid` against `urls.sqlite3` and the CSV's SHA-256, says so on stderr (`Note: ... this build replaces all three outputs`), and replaces them. A failed read replaces nothing and leaves no temporary file.

**Locks.** A build holds `urls.sqlite3.lock` from before it reads until its outputs are replaced, so a second `build-urls` on the same folder stops at once (exit 1, `The URL outputs ... is in use by another process`). This holds whatever the source. From a database it also holds `telegram-tracker.sqlite3.lock` while it reads, like the other builders. While it does, a collection, an upgrade, `build-dataset` or `build-network` on that folder is refused, and `build-urls` is refused while one of those holds the database. Outputs are replaced after the database lock is released. A lock left by a killed build is reported, not removed: make sure no build is running, then delete it.

## What counts as an occurrence

An occurrence is one piece of evidence that a message carries a URL. `source` says which:

| `source` | Evidence |
|---|---|
| `url_entity` | A URL Telegram marked in the text (`MessageEntityUrl`); the URL is the marked text. |
| `text_link` | A hyperlink (`MessageEntityTextUrl`): `raw_url` is its hidden target and `label` is the text shown. |
| `plain_text` | A URL found in the text by pattern: `scheme://...` for any scheme, `www.` hosts, and `host.suffix/path` under a known public suffix. Trailing sentence punctuation and unbalanced closing brackets are left out. A bare `example.com` without a path is not detected this way (Telegram usually marks it as a `url_entity`). |
| `preview` | The message's link preview (`MessageMediaWebPage`) when it has a URL: `WebPage`, `WebPagePending` or `WebPageEmpty`, as collected. The title and description are the ones Telegram supplied. |

Rules for overlapping evidence:

- A pattern match that overlaps a `url_entity` is that entity: Telegram's own boundaries are kept and `evidence` gains `plain_text`.
- A pattern match inside a `text_link` label is merged only when it names the link's target. A label that shows one URL but links to another gives two occurrences, the `text_link` and a `plain_text` row for the URL shown.
- A preview is always its own occurrence. `preview_matches_text` is 1 when the same message has a text occurrence with the same `url_key`.
- A URL repeated in a message gives one occurrence per repetition.

Text offsets and lengths (`text_offset`, `text_length`) are UTF-16 code units, as Telegram counts them: an emoji outside the Basic Multilingual Plane counts 2. An entity whose span lies outside the text or splits a character keeps its offsets: a `url_entity` gets no `raw_url` (`url_kind` = `malformed`), and a `text_link` gets no `label`.

Not extracted: URL buttons under a post, other entity types (mentions, e-mail addresses, hashtags), and preview content beyond the fields listed. A URL that is not in the collected payload cannot be recovered.

## Parsing and normalization

`raw_url` is always the URL as found. `url_kind` classifies it:

| `url_kind` | Meaning |
|---|---|
| `web` | `http` or `https` with a host. |
| `schemeless` | No scheme (`www.example.com/a`). It is parsed as `//host/...` and is not assumed to be http. |
| `other_scheme` | Any other scheme. `ftp`, `ftps`, `sftp`, `ws` and `wss` URLs have a host and a domain; others (`mailto:`, `tg:`, ...) have none. |
| `malformed` | No URL text, a missing or invalid host (an empty label, a character a host name cannot hold, invalid punycode, unbalanced brackets), an invalid port, or a network scheme without `//`. |

Normalization (version 1, recorded as `normalization_version`) is conservative:

- the scheme and host are lowercased;
- the host is converted to ASCII label by label (Unicode NFC, then punycode for non-ASCII labels, without other character mapping), and `host_unicode` keeps the readable form;
- one trailing dot in the host is dropped;
- the default port is dropped for `http` (80) and `https` (443) but kept in `port`;
- an empty `http`/`https` path becomes `/`;
- userinfo, path, query and fragment are kept exactly as written: nothing is percent-decoded, resolved or stripped. An empty `?` or `#` is kept;
- a URL without a scheme normalizes to `//host/...`.

`url_key` is `normalized_url`, or `raw_url` when there is none. It is the URL identity used for counting.

**Registered domains.** The public suffix list includes its private section, so `foo.blogspot.com` and `user.github.io` are registered domains of their own. `registry_domain` gives the domain under the ICANN suffix only (`blogspot.com`, `github.io`). Hosts without a registered domain go into buckets:

| `host_type` | `domain_key` | Example |
|---|---|---|
| `domain` | the registered domain | `www.bbc.co.uk` → `bbc.co.uk` |
| `ipv4` | `[ipv4]` | `192.168.0.1` |
| `ipv6` | `[ipv6]` | `[::1]` |
| `local` | `[local]` | `localhost` (one label) |
| `unknown_suffix` | `[unknown-suffix]` | `site.unknowntld` |
| `public_suffix` | `[public-suffix]` | `github.io`, `co.uk` |
| (none) | `[no-host]` | `mailto:x@example.com`, `tg://resolve?domain=x` |
| (none) | `[malformed]` | `http://bad_host!/` |

## Counting rules

- `occurrences`: rows. `text_occurrences` counts the text sources, `preview_occurrences` the previews.
- `messages`: distinct (`channel`, `message_id`). A URL in the text and its preview are two occurrences in one message. To count posts that link to a domain, use `messages`.
- `urls`: distinct `url_key` (a `url_entity` without URL text has none and is not counted).
- `channels`: distinct `channel`. A channel is the history's collection name, as in `msgs_dataset.csv`.

From the files, a channel renamed between collections has a history under each name, and both are read. From the database only the newest name is read (the same difference as `build-dataset`).

## `urls_dataset.csv` and `occurrences`

| Column | Meaning |
|---|---|
| `occurrence_id` | Row number, in output order: histories in the builders' order, messages newest first, then occurrences in text order followed by the preview. |
| `channel` | The history's collection name. |
| `channel_id` | The message's channel ID. |
| `message_id` | The message ID. |
| `date` | The message date, as stored. |
| `msg_link` | `https://t.me/<channel>/<message_id>`. |
| `occurrence_index` | Position of the occurrence in its message, from 0. |
| `source` | `url_entity`, `text_link`, `plain_text` or `preview`. |
| `evidence` | Every kind merged into the row, joined by `+` (`url_entity+plain_text`). |
| `text_offset` | UTF-16 offset in the message text; null for a preview. |
| `text_length` | UTF-16 length; null for a preview. |
| `label` | The text shown for a `text_link`. |
| `raw_url` | The URL as found. |
| `url_kind` | `web`, `schemeless`, `other_scheme` or `malformed`. |
| `url_key` | `normalized_url`, or `raw_url` without one. |
| `normalized_url` | See normalization; null for `malformed`. |
| `scheme` | Lowercased scheme; null without one. |
| `netloc` | The authority as written (`user@Host:port`); null without a host. |
| `userinfo` | The `user:password` part before `@`, as written. |
| `host` | ASCII host (IPv6 without brackets). |
| `host_unicode` | Readable host. |
| `port` | Explicit port. |
| `host_type` | `domain`, `ipv4`, `ipv6`, `local`, `unknown_suffix` or `public_suffix`. |
| `subdomain` | Labels left of the registered domain. |
| `suffix` | Public suffix (ASCII). |
| `is_private_suffix` | 1 when the suffix comes from the list's private section. |
| `registered_domain` | Domain under the public suffix, private suffixes included. |
| `registry_domain` | Domain under the ICANN suffix. |
| `domain_key` | Registered domain or bucket; the key of the `domains` table. |
| `path` | Path as written. |
| `query` | Query as written, without `?`. |
| `fragment` | Fragment as written, without `#`. |
| `preview_type` | Webpage type of a preview (`WebPage`, `WebPagePending`, `WebPageEmpty`). |
| `preview_display_url` | Preview display URL. |
| `preview_site_name` | Preview site name. |
| `preview_title` | Preview title. |
| `preview_description` | Preview description. |
| `preview_matches_text` | For a preview: 1 when a text occurrence of the message has the same `url_key`, else 0. |
| `is_forward` | 1 for a forwarded message. |
| `fwd_from_type` | Origin of a forward: `channel`, `user` or `chat`; null when hidden or unknown. |
| `fwd_from_id` | Origin channel, user or chat ID. |
| `fwd_from_post` | Origin channel post ID. |
| `fwd_from_date` | Original date. |
| `fwd_from_name` | Name shown for a hidden origin. |
| `profile_name` | Local profile that last wrote the history (null = unknown). |
| `profile_id` | That profile's ID. |
| `history_run` | From the database: the run that last changed the history; null from the files. |

Origin names are not resolved; a forward whose origin was not collected stays unknown.

## Other tables in `urls.sqlite3`

`PRAGMA user_version` is the output format (1).

**`build`** (one row):

| Column | Meaning |
|---|---|
| `build_uuid` | Same as `urls-build.json`. |
| `format` | Output format, 1. |
| `normalization_version` | Normalization rules version. |
| `built_at` | Build time (UTC). |
| `source` | `files` or `sqlite`. |
| `storage` | The folder's storage mode. |
| `collection` | JSON: the database identity and last run, as in the build record. |
| `suffix_list` | JSON: library, version, `bundled snapshot`, online updates off, private suffixes included, suffix count and SHA-256 of the loaded list. |

**`channels`** (one row per channel name read, with or without URLs):

| Column | Meaning |
|---|---|
| `channel` | Collection name. |
| `channel_id` | Channel ID. |
| `histories` | Histories read under the name. |
| `messages` | Items in those histories, service messages included. |
| `occurrences` | Occurrences. |
| `text_occurrences` | Text occurrences. |
| `preview_occurrences` | Preview occurrences. |
| `messages_with_urls` | Distinct messages with an occurrence. |
| `urls` | Distinct `url_key`. |
| `domains` | Distinct `domain_key`. |

**`domains`** (one row per `domain_key`):

| Column | Meaning |
|---|---|
| `domain_key` | Registered domain or bucket. |
| `domain_kind` | `domain`, a bucket's host type, `no_host` or `malformed`. |
| `registered_domain` | Null for buckets. |
| `registry_domain` | Null for buckets. |
| `suffix` | Null for buckets. |
| `is_private_suffix` | Null for buckets. |
| `occurrences` | Occurrences. |
| `text_occurrences` | Text occurrences. |
| `preview_occurrences` | Preview occurrences. |
| `messages` | Distinct messages. |
| `urls` | Distinct `url_key`. |
| `channels` | Distinct channels. |
| `first_date` | Earliest message date. |
| `last_date` | Latest message date. |

**`channel_domains`** (one row per channel and `domain_key`):

| Column | Meaning |
|---|---|
| `channel` | Collection name. |
| `domain_key` | Registered domain or bucket. |
| `occurrences` | Occurrences. |
| `text_occurrences` | Text occurrences. |
| `preview_occurrences` | Preview occurrences. |
| `messages` | Distinct messages. |
| `urls` | Distinct `url_key`. |

## Example queries

Domains linked from the most posts:

```sql
SELECT domain_key, messages, channels, occurrences
FROM domains
ORDER BY messages DESC, domain_key
LIMIT 20;
```

Hidden links whose shown text is a different URL:

```sql
SELECT t.channel, t.message_id, t.label, t.raw_url
FROM occurrences t
JOIN occurrences p ON p.channel = t.channel AND p.message_id = t.message_id
  AND p.source = 'plain_text' AND p.text_offset = t.text_offset AND p.text_length = t.text_length
WHERE t.source = 'text_link';
```

Forwarded posts linking to a domain, by origin channel:

```sql
SELECT fwd_from_id, count(DISTINCT channel || '/' || message_id) AS posts
FROM occurrences
WHERE is_forward = 1 AND fwd_from_type = 'channel' AND registered_domain = 'example.com'
GROUP BY fwd_from_id;
```
