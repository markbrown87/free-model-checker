# free-model-checker

A self-running Docker Compose service that watches the [OpenRouter](https://openrouter.ai) models catalog and pings a private Discord channel whenever the set of free models changes. On a configurable interval a single dependency-free Python process fetches the models API, computes the current free set (models whose input and output price are both `$0`), and diffs it against state persisted on a bind-mounted volume. New free models get a 🆕 embed with the ID, name, context length, modalities and summary so you can evaluate them without opening the docs; models leaving the free set get a ❌ embed with the reason (`now has pricing` or `no longer listed`), rendered from the stored snapshot so the details survive even after the model disappears. API failures get a ⚠️ embed (first of a streak by default) and recovery gets a ✅ embed, so you are never surprised by a silent outage or a promo quietly turning into a paid model. Message wording lives in bind-mounted template files and all non-secret settings live in a single `config.toml`, so you can re-skin or re-tune without rebuilding the image. The service runs as a non-root user with `restart: unless-stopped` and never needs Python installed on the host.

## Prerequisites

- **Docker Engine** with the **Compose v2 plugin** (`docker compose`, not the legacy `docker-compose`).
- A machine that can reach `https://openrouter.ai` (or whatever `[api].url` you configure).
- A Discord account with permission to create a server and a webhook (any account can create a private server).
- Outbound HTTPS access from the container to `discord.com` (webhook delivery).

No Python, pip, or other dependencies are needed on the host — everything is built and run inside the container.

## Discord setup

Notifications are delivered to a channel webhook on a **private** server only you can see. Do this once:

1. **Create a private server.** In the Discord client, click the **`+`** button at the bottom of the server list (left sidebar) → **Create My Own** → choose **For me and my friends** → give it a name (e.g. `Alerts`) → **Create**. Do not invite anyone; the server stays private to you.
2. **Create a channel.** In that server, click the **`+`** next to the channel list heading (or the server name → **Create Channel**) → choose **Text Channel** → name it `model-alerts` → **Create Channel**. Leave it so that only you can view it.
3. **Open Server Settings.** Click the server name at the top of the channel list → **Server Settings** (in the menu).
4. **Go to Integrations.** In the left sidebar of Server Settings, select **Integrations**.
5. **Open Webhooks.** Click **Webhooks** (or **View Webhooks** / **Create Webhook** depending on the client version).
6. **Create a webhook.** Click **New Webhook**. A new webhook appears in the list, named something like `Captain Hook`.
7. **Point it at your channel.** Click the webhook → set **Name** to `Free Model Checker` (the config also overrides the display name at send time, but this keeps the server tidy) → click the channel dropdown (**Select a channel…**) and choose `model-alerts`.
8. **Copy the URL.** Click **Copy Webhook URL**. It looks like `https://discord.com/api/webhooks/<id>/<token>`. Treat it as a secret.

> The webhook URL is a bearer credential for that channel — anyone with it can post. Keep it in `.env`, never in version control.

## Setup

1. **Create your `.env` from the template.**
   ```sh
   cp .env.example .env
   ```

2. **Paste the webhook URL.** Open `.env` and set `DISCORD_WEBHOOK_URL` to the URL you copied above. Optionally set `OPENROUTER_API_KEY` (sent as `Authorization: Bearer` only when non-empty) and `TZ` so log timestamps match your local clock.
   ```dotenv
   # Required: private Discord channel webhook URL.
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/<id>/<token>
   # Optional: OpenRouter API key (sent as Authorization: Bearer when set).
   OPENROUTER_API_KEY=
   # Optional: timezone for the process/log timestamps.
   TZ=UTC
   ```

3. **Build and start the service** (detached):
   ```sh
   docker compose up -d --build
   ```
   The first cycle runs immediately at container start (`run_on_start = true`), so you should see messages in the channel within a few seconds.

4. **Follow the logs:**
   ```sh
   docker compose logs -f
   ```
   Each event is logged as one plain line on stdout; errors go to stderr.

5. **Stop the service:**
   ```sh
   docker compose down
   ```
   Your state in `data/` is a bind mount, so it survives `down`, `up`, rebuilds, and restarts.

## Configuration reference

All non-secret settings live in `config.toml`, which is copied into the image at build time. Secrets live only in `.env`. Edit `config.toml`, then rebuild and restart to apply:

```sh
docker compose up -d --build
```

(Template prose is the part you can change with only a restart — see [Templates](#templates).)

### `[api]`

| Key | Default | Meaning |
|---|---|---|
| `url` | `"https://openrouter.ai/api/v1/models"` | Models endpoint to fetch. Point it at a local mock server or mirror to test scenarios. |
| `timeout_seconds` | `30` | Per-request HTTP timeout in seconds before the cycle is treated as a `timeout` failure. |

### `[schedule]`

| Key | Default | Meaning |
|---|---|---|
| `interval` | `"1h"` | Poll interval between successful cycles. Accepts `<n>s`, `<n>m`, `<n>h`, `<n>d`, or a bare number of seconds (see [Interval syntax](#interval-syntax)). |
| `run_on_start` | `true` | Run a cycle immediately at startup instead of waiting one interval. |
| `retry_delay` | `"5m"` | Delay after a failed cycle (fetch/HTTP/parse/empty) before retrying, instead of the full `interval`. |

### `[detection]`

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"zero_price"` | How "free" is defined: `zero_price` (`pricing.prompt == "0"` and `pricing.completion == "0"`), `suffix` (`id` ends with `:free`), or `either` (union of both). |
| `text_only` | `true` | Keep only models whose `architecture.output_modalities` is exactly `["text"]`. Mixed-modality zero-priced models such as the Lyria pair (`text`+`audio`) never notify you. |
| `blacklist` | `["openrouter/*"]` | fnmatch globs matched against the model `id`; matches are ignored entirely. The default hides OpenRouter's `openrouter/free` auto-router, which is not a model. |
| `sanity_min_fraction` | `0.5` | Skip a cycle (log a warning, send no messages) when the model count or `total_count` drops below this fraction of the last successful cycle. `0` disables the guard. |

### `[discord]`

| Key | Default | Meaning |
|---|---|---|
| `message_format` | `"embed"` | `embed` sends a Discord embed; `plain` sends the same rendered body as markdown `content` with no embed. |
| `username` | `"Free Model Checker"` | Webhook display name used on every message. |
| `error_notify` | `"once"` | `once` notifies on the first failure of a streak only; `every` notifies on every failed cycle (useful while debugging). |
| `notify_recovery` | `true` | Send a ✅ message once the API works again, reporting the outage duration and failed-cycle count. |
| `notify_on_first_run` | `true` | On an empty/missing state file, announce every current free model as 🆕. Set `false` to seed silently (e.g. after wiping the volume). |

### `[icons]`

| Key | Default | Meaning |
|---|---|---|
| `new` | `"🆕"` | Icon prefixed to new-model titles/messages. |
| `removed` | `"❌"` | Icon for models leaving the free set. |
| `error` | `"⚠️"` | Icon for API-failure messages. |
| `recovery` | `"✅"` | Icon for recovery messages. |

### `[colors]`

| Key | Default | Meaning |
|---|---|---|
| `new` | `"#2ECC71"` | Embed colour (green) for new-model messages. |
| `removed` | `"#E74C3C"` | Embed colour (red) for removal messages. |
| `error` | `"#F1C40F"` | Embed colour (yellow) for error messages. |

### Secrets (`.env` only)

| Variable | Required | Meaning |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | Yes | Webhook URL to POST messages to. Missing/empty fails fast at startup with a non-zero exit. |
| `OPENROUTER_API_KEY` | No | When set, sent as `Authorization: Bearer` on models requests to raise rate limits. |
| `TZ` | No | Timezone for process/log timestamps (also passed through to the container). |

## Interval syntax

`interval` and `retry_delay` accept a human-readable duration:

| Value | Meaning |
|---|---|
| `30m` | 30 minutes |
| `1h` | 1 hour |
| `24h` | 24 hours |
| `1d` | 1 day |
| `90s` | 90 seconds |
| `300` | Bare number = 300 seconds (5 minutes) |

Supported units are `s` (seconds), `m` (minutes), `h` (hours), `d` (days). A bare number is interpreted as seconds. The delay runs *after* the work of a cycle completes, and a cycle can never overlap itself.

## Templates

Message prose lives in template files bind-mounted read-only at `/app/templates`. Edit a file on the host and restart to apply — **no rebuild required**:

```sh
# edit ./templates/new_model.txt (or any template)
docker compose restart
```

Available templates: `new_model.txt`, `removed_model.txt`, `error.txt`, `recovery.txt`. They are rendered with Python's [`string.Template`](https://docs.python.org/3/library/string.html#template-strings) syntax, so use `$name` or `${name}` for placeholders. If the mount is absent, baked-in defaults identical to the shipped files are used, so the container still works with volumes removed.

Placeholders available to every template:

| Placeholder | Value |
|---|---|
| `${icon}` | Event icon from `[icons]`. |
| `${id}` | Model ID (raw). |
| `${name}` | Model name (Discord-escaped). |
| `${context_length}` | Comma-formatted digits, e.g. `262,144`. |
| `${modalities}` | Formatted modality, e.g. `text + image → text`. |
| `${summary}` | `description` truncated at ~280 characters on a word boundary + `…` (Discord-escaped). |
| `${url}` | `https://openrouter.ai/<id>`. |
| `${reason}` | `now has pricing` or `no longer listed` (removal only). |
| `${error_type}` | `http`, `timeout`, `parse`, or `empty` (error only). |
| `${error_code}` | HTTP status string, else empty (error only). |
| `${error_message}` | Response body / exception text, truncated to ~500 characters (error only). |
| `${downtime}` | Humanized wall time, e.g. `1h 3m 12s` (recovery only). |
| `${failed_cycles}` | Number of consecutive failed cycles (recovery only). |

Default template bodies:

```text
# new_model.txt
`${id}`

${summary}
```

```text
# removed_model.txt
`${id}`

${reason}
```

```text
# error.txt
OpenRouter models API error: ${error_type} ${error_code}

${error_message}
```

```text
# recovery.txt
The OpenRouter models API is responding again.

Downtime: ${downtime}
Failed cycles: ${failed_cycles}
```

## Message appearance

Each event type has its own icon and colour, and the model ID links to its OpenRouter page.

| Event | Icon | Colour | Contents |
|---|---|---|---|
| New free model | 🆕 | Green (`#2ECC71`) | Title `${icon} ${name}`; body rendered from `new_model.txt`; inline fields **Context length** (`<digits> tokens`), **Modalities** (`text + image → text`), and **Free until** (only when the API reports `expiration_date`). |
| Model leaves free set | ❌ | Red (`#E74C3C`) | Title `${icon} ${name}`; body rendered from `removed_model.txt` (snapshot fields, so details survive after the model is gone). |
| API error | ⚠️ | Yellow (`#F1C40F`) | Title `${icon} OpenRouter models API error`; body rendered from `error.txt` with error type, code, and detail. No URL. |
| Recovery | ✅ | Green (`#2ECC71`) | Title `${icon} OpenRouter models API recovered`; body rendered from `recovery.txt` with downtime and failed-cycle count. No URL. |

Every embed carries a detection timestamp (UTC in the payload, localized by Discord) and the webhook display name from `[discord].username`.

With `message_format = "plain"`, the same rendered body is posted as markdown `content` (prefixed with the event icon) and no embed is created — a fallback so you can abandon embeds without touching code.

## Persistence

All runtime state lives in the bind-mounted `./data` directory and survives restarts, rebuilds, and `docker compose down`:

- **`data/state.json`** — human-readable JSON. Holds the current free set keyed by model ID with a snapshot of each model's API fields (`id`, `name`, `description`, `context_length`, `architecture`, `expiration_date`), plus `first_seen` and `notified_at` bookkeeping; `last_success` for the sanity gate; and failure-streak counters. Writes are atomic (temp file + rename), so an interrupted write never corrupts it. Because it is plain JSON, you can inspect or repair it by hand.
- **`data/history.jsonl`** — append-only change log, one JSON object per line (`{"ts", "event", "id"?, "reason"?, "detail"?}`). Events: `seed`, `added`, `removed`, `error`, `recovery`, `cycle_skipped`. Use it to audit what happened without a database.

### Re-seeding

To make the service re-announce the current inventory from scratch (a fresh baseline), stop the service and delete the state file:

```sh
docker compose down
rm data/state.json
docker compose up -d
```

On the next start the state is empty, so the checker seeds the free set and appends a `seed` history event. If `notify_on_first_run = true` it sends a 🆕 message for every currently free model; set `notify_on_first_run = false` first if you want to re-seed silently. `data/history.jsonl` is not touched by re-seeding (it only ever grows).

## First-run behavior

When `data/state.json` is missing or empty, the service seeds the current free set, appends one `seed` event to `history.jsonl`, and — when `notify_on_first_run = true` (the shipped default) — sends a 🆕 message for every current free model. No removal messages are ever sent on the first run.

On subsequent starts with a warm `state.json`, the diff is against the persisted free set, so a simple restart produces no messages at all unless the catalog actually changed. A message is marked notified in state only after Discord returns a 2xx, so transient delivery failures are retried on the next cycle rather than silently lost — no duplicates, no gaps.

## Troubleshooting

**Watch the logs.** Live out-of-container view:

```sh
docker compose logs -f          # stdout (one line per event) + stderr
docker compose logs -f free-model-checker
```

Errors (fetch failures, Discord send failures, guard warnings) are written to **stderr**; normal cycle events are on **stdout**. A `docker compose logs` line carries a stream tag you can use to tell them apart when reading a file.

**Non-zero exit on bad configuration.** If `config.toml` is missing or invalid, or `DISCORD_WEBHOOK_URL` is unset/empty, the process logs a clear line and exits non-zero instead of running blind. With `restart: unless-stopped` Compose will keep restarting it — check the logs, fix `.env`/`config.toml`, then `docker compose up -d` again.

**Discord 429 (rate limited).** If the webhook returns HTTP 429, the checker reads `retry_after` from the response (the `Retry-After` header is preferred, falling back to the JSON body's `retry_after` field), sleeps that long, and retries the *same* message. Messages in a batch are also spaced 1 second apart. A burst of changes is therefore throttled, not lost.

**Other Discord send failures.** A non-429 send failure is logged to stderr and the message is *not* marked notified, so the next cycle retries it. Fix the webhook URL and the backlog clears itself.

**Sanity-gate warnings.** A log line about the model count dropping below the sanity fraction means the cycle was **skipped**: a fetch succeeded but the response looked truncated (fewer models, or a lower `total_count`, than half of the last successful cycle). No Discord message is sent and the free set is left untouched, so a partial response can never be mistaken for mass removal. If you legitimately expect the catalog to shrink that much (or you are testing against a tiny mock), set `sanity_min_fraction = 0` in `config.toml` to disable the guard, then `docker compose up -d --build`.

**Empty or failed responses.** An empty `data` array, a non-2xx status, a timeout, or unparseable JSON all abort the cycle without removals. Failures increment the streak counter and retry after `retry_delay` (not the full interval) so recovery is quick. With `error_notify = "once"` you get one ⚠️ per streak; the following successful cycle sends one ✅ with downtime and the failed-cycle count.

**`data/` permission errors.** The `data` directory is bind-mounted from the host, and the container runs as a non-root user with uid/gid **1000**. If the host directory is owned by a different user, the container cannot write `state.json` and you will see permission errors in the logs. Fix the ownership on the host:

```sh
sudo chown -R 1000:1000 data
```

The same applies if you run the container with a different UID via an override.

**Malicious or malformed data / edge cases.** Model names, descriptions, and IDs come from a third-party API. The checker escapes backticks before inserting them into embeds, truncates the summary at ~280 characters (error messages at ~500), and caps embed descriptions at 4096 and plain content at 2000 characters, so a hostile or malformed field cannot break the message or exceed Discord's limits. A description containing `${...}`-looking text does not affect rendering because `string.Template` renders placeholders in the template file only, not in substituted values; if you want to change wording, edit the template rather than model data.

## Verification (manual end-to-end)

There is no test framework; behavior is verified by running the stack against a real webhook. To exercise the whole service, run these scenarios in order. The seams you need are all config or files: `[api].url` (point it at a mock JSON server), `DISCORD_WEBHOOK_URL` (use a broken URL to force failures), and `data/state.json` (hand-edit to inject scenarios).

1. **Fresh volume, `notify_on_first_run = true`** → every current free model arrives as a 🆕 embed, and `data/history.jsonl` contains a `seed` event.
2. **Restart with a warm volume** → no duplicate messages on the next cycle.
3. **Delete one entry from `data/state.json`** → exactly one 🆕 message for that model on the next cycle.
4. **Add a fake entry to `data/state.json`** → exactly one ❌ message, rendered from the stored snapshot (reason `no longer listed`).
5. **Set `[api].url` to an unreachable host** → one ⚠️ per streak (with `error_notify = "once"`); revert the URL → one ✅ carrying the downtime.
6. **Have a mock return an empty `data` array** → the cycle is skipped, no ❌ flood, and a warning appears in the logs.
7. **Break the webhook URL** → a stderr line and no crash; fix the URL and the message is retried on the next cycle.
8. **Set `message_format = "plain"`** → the same content arrives as markdown text with no embed.

Only externally observable behavior counts as evidence: channel messages, stdout/stderr, and the contents of `data/`.
