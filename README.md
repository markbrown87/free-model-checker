# free-model-checker

A self-running Docker Compose service that watches the free-model inventories of multiple **providers** — [OpenRouter](https://openrouter.ai) and [OpenCode](https://opencode.ai) — and pings a private Discord channel whenever one of their free sets changes. On a configurable interval a single dependency-free Python process iterates the enabled providers **independently**: for each one it fetches the models catalog, guards the result, diffs it against persisted per-provider state, and announces changes to the same webhook — so one provider's outage never blocks or delays the other's notifications. Every message names its provider, so OpenRouter and OpenCode announcements are distinguishable at a glance.

New free models get a 🆕 embed with the ID, name and summary (plus context length and modalities when the provider publishes them) so you can evaluate them without opening the docs; models leaving a free set get a ❌ embed with the reason (`now has pricing`, `no longer listed`, or `no longer free`), rendered from the stored snapshot so the details survive after the model disappears. Per-provider API failures get a ⚠️ embed (first of a streak by default) and recovery gets a ✅ embed, so you are never surprised by a silent outage or a promo quietly turning into a paid model. Message wording lives in bind-mounted template files and all non-secret settings live in a single bind-mounted `config.toml`, so you can re-tune providers or re-skin messages with a restart, not a rebuild. The service runs as a non-root user with `restart: unless-stopped` and never needs Python installed on the host.

## Vocabulary

| Term | Meaning |
|---|---|
| **provider** | A first-class, code-supported source of a models catalog (OpenRouter, OpenCode). You **enable** providers by name; you do not define them. |
| **free set** | Per provider, the models that provider's detection rule classifies as costing `$0` to use. |
| **cycle** | One pass over all enabled providers: fetch → guard → diff → notify → persist, performed independently per provider. |
| **streak** | Per provider, the number of consecutive failed cycles; reset by that provider's first successful cycle. |
| **snapshot** | The stored copy of a model's fields at the time it was last seen free, used to render removal messages after the model disappears. |
| **guard** | A rule that aborts a provider's cycle when its data is untrustworthy (currently the per-provider sanity gate). |
| **catalog** | OpenCode only: the [models.dev](https://models.dev) registry, used as the pricing source of truth. |

## Prerequisites

- **Docker Engine** with the **Compose v2 plugin** (`docker compose`, not the legacy `docker-compose`).
- A machine that can reach the models endpoints of the providers you enable (OpenRouter's `https://openrouter.ai/api/v1/models`, OpenCode's `https://opencode.ai/inference/v1/models`) and, for OpenCode, the pricing catalog at `https://models.dev/api.json`.
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

2. **Paste the webhook URL.** Open `.env` and set `DISCORD_WEBHOOK_URL` to the URL you copied above. Optionally set `OPENROUTER_API_KEY` and `OPENCODE_API_KEY` (each is sent as `Authorization: Bearer` only when non-empty) and `TZ` so log timestamps match your local clock.
   ```dotenv
   # Required: private Discord channel webhook URL.
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/<id>/<token>
   # Optional: OpenRouter API key (sent as Authorization: Bearer when set).
   OPENROUTER_API_KEY=
   # Optional: OpenCode API key (sent as Authorization: Bearer when set).
   OPENCODE_API_KEY=
   # Optional: timezone for the process/log timestamps.
   TZ=UTC
   ```

3. **Build and start the service** (detached):
   ```sh
   docker compose up -d
   ```
   The first cycle runs immediately at container start (`run_on_start = true`), so you should see messages in the channel within a few seconds. Both providers are enabled by default, so a fresh deployment watches OpenRouter and OpenCode immediately.

4. **Follow the logs:**
   ```sh
   docker compose logs -f
   ```
   Each event is logged as one plain line per provider on stdout; errors go to stderr.

5. **Stop the service:**
   ```sh
   docker compose down
   ```
   Your state in `data/` is a bind mount, so it survives `down`, `up`, rebuilds, and restarts.

### `config.toml` can be bind-mounted

`config.toml` can be mounted read-only at `/app/config.toml`, so **changing providers or knobs requires only a restart** — never a rebuild:

```sh
# edit ./config.toml
docker compose restart
```

The copy baked into the image is used as a fallback when the mount is absent, so the container still starts with working defaults.

> **Caveat — the config file must exist before `docker compose up` if mount is uncommented in compose file.** The compose file bind-mounts `./config.toml` as a *file*. If that host path does not exist when you start the stack, Docker creates a **directory** at the mount point instead, and config loading fails. The repository ships `config.toml`, so this only bites if you delete it or start from a bare checkout; if needed, restore it (`git checkout config.toml` or create the file) before running `docker compose up`.

## Container flows

The compose service keeps its local build context **and** references the published image name, `ghcr.io/markbrown87/free-model-checker:latest`, so both workflows work from the same file.

### Build and push to GHCR

Single-arch (build for the current machine, tag, push):

```sh
docker build -t ghcr.io/markbrown87/free-model-checker:latest .
docker push ghcr.io/markbrown87/free-model-checker:latest
```

Multi-arch (build for `amd64` and `arm64` and push in one step):

```sh
docker buildx build --platform linux/amd64,linux/arm64 \
  --build-arg GIT_REVISION=$(git rev-parse --short HEAD) \
  -t ghcr.io/markbrown87/free-model-checker:latest --push .
```

The image has **no build-time `RUN` steps**, so a multi-platform build just copies files into each platform's layer — no QEMU/binfmt emulation is needed on the build host, and the default `docker` builder works. Two notes on the command:

- A multi-platform result cannot be `--load`ed into the local image store (Docker only loads single-platform images). Either push it (as above) or export it to a file for inspection: `docker buildx build --platform linux/amd64,linux/arm64 --output type=oci,dest=image.tar -t ghcr.io/markbrown87/free-model-checker:latest .`
- If you *do* add a `RUN` step later (e.g. installing a package), the `arm64` build will need emulation; register it once with `docker run --privileged --rm tonistiigi/binfmt --install all` and use a `docker-container` buildx builder.

Before pushing you must be authenticated to GHCR (`docker login ghcr.io` with a GitHub personal access token that has `write:packages`).

> **The first push creates a private package.** GHCR packages are private by default, so a target machine cannot pull `ghcr.io/markbrown87/free-model-checker:latest` until you make it public. In GitHub, open your profile (or the repository) → **Packages** → `free-model-checker` → **Package settings** → **Danger Zone** → **Change visibility** → **Public**. (Authenticating the target machine to GHCR is the alternative if you prefer to keep the package private.)

### Run the published image without building

On a target machine, pull the image and start the stack — no build context or toolchain required:

```sh
docker compose pull && docker compose up -d
```

### Build locally and run

To build the image on the current machine and start the stack in one step (as in [Setup](#setup)):

```sh
docker compose up -d --build
```

## Configuration reference

All non-secret settings live in `config.toml` (bind-mounted read-only at `/app/config.toml`); secrets live only in `.env`. Edit `config.toml` and run `docker compose restart` to apply — no rebuild needed. If the mount is absent, the baked-in copy identical to the shipped file is used.

### Providers

Providers are code-level integrations. Each is enabled in its own table:

- **Shipped default:** both `[providers.openrouter]` and `[providers.opencode]` are present and enabled, so a fresh deployment watches both.
- **Disable cleanly:** an **absent** provider table disables that provider, and `enabled = false` inside a present table does the same. You never need to delete a whole section to turn a provider off.
- **Unknown names fail fast:** any name other than `openrouter` or `opencode` under `[providers]` raises a config error at startup that lists the supported providers, so a typo can never silently disable monitoring.
- **Every key is optional** and falls back to the code default shown below; `enabled` defaults to `true` when the table is present.

The old global `[detection]` section is gone; its knobs now live under `[providers.openrouter]`. The old `[api].url` is similarly gone — each provider has its own `url` (and OpenCode an additional `catalog_url`).

### `[api]`

| Key | Default | Meaning |
|---|---|---|
| `timeout_seconds` | `30` | Per-request HTTP timeout in seconds, shared by every provider request. A timeout counts as a failed cycle for the provider that hit it. |

### `[schedule]`

| Key | Default | Meaning |
|---|---|---|
| `interval` | `"1h"` | Poll interval between successful cycles. Accepts `<n>s`, `<n>m`, `<n>h`, `<n>d`, or a bare number of seconds (see [Interval syntax](#interval-syntax)). |
| `run_on_start` | `true` | Run a cycle immediately at startup instead of waiting one interval. |
| `retry_delay` | `"5m"` | Delay before the next cycle when **any** provider's fetch fails this cycle, instead of the full `interval`, so recovery is quick. A failing provider never delays the other provider's notifications within the cycle. |

### `[discord]`

| Key | Default | Meaning |
|---|---|---|
| `message_format` | `"embed"` | `embed` sends a Discord embed; `plain` sends the same rendered body as markdown `content` with no embed. |
| `username` | `"Free Model Checker"` | Webhook display name used on every message. |
| `error_notify` | `"once"` | Per provider: `once` notifies on the first failure of a streak only; `every` notifies on every failed cycle (useful while debugging). |
| `notify_recovery` | `true` | Send a ✅ message once a provider works again, reporting the outage duration and failed-cycle count. |
| `notify_on_first_run` | `true` | Applied per provider: when a provider has no state section, announce every current free model as 🆕. Set `false` to seed silently (e.g. after wiping the volume). |
| `message_gap_seconds` | `1.0` | Seconds to wait between consecutive Discord sends in a batch. The gap applies across the new→removed boundary too. Raise it to stay clear of webhook rate limits; `0` disables the gap (a burst of changes then goes out back-to-back, which is what gets webhooks throttled or blocked). |

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

### `[providers.openrouter]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Enable or disable this provider. An absent table also disables it. |
| `mode` | `"zero_price"` | How "free" is defined: `zero_price` (`pricing.prompt == "0"` and `pricing.completion == "0"`), `suffix` (`id` ends with `suffix`), or `either` (union of both). |
| `suffix` | `":free"` | The `id` suffix that marks a free model when `mode` includes `suffix`. |
| `text_only` | `true` | Keep only models whose `architecture.output_modalities` is exactly `["text"]`. Mixed-modality zero-priced models such as the Lyria pair (`text`+`audio`) never notify you. |
| `blacklist` | `["openrouter/*"]` | fnmatch globs matched against the model `id`; matches are ignored entirely. The default hides OpenRouter's `openrouter/free` auto-router, which is not a model. |
| `api_key_env` | `"OPENROUTER_API_KEY"` | Name of the environment variable holding this provider's API key. When unset/empty, no `Authorization` header is sent. |
| `model_url` | `"https://openrouter.ai/{id}"` | Per-model URL template; `{id}` is substituted with the model ID and used as the embed link. An empty string disables the link. |
| `url` | `"https://openrouter.ai/api/v1/models"` | Models endpoint to fetch. Point it at a local mock server to test scenarios. |
| `sanity_min_fraction` | `0.5` | Skip this provider's cycle (log a warning, send no messages) when the model count or `total_count` drops below this fraction of the provider's last successful cycle. `0` disables the guard. |

### `[providers.opencode]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Enable or disable this provider. An absent table also disables it. |
| `url` | `"https://opencode.ai/inference/v1/models"` | OpenCode models endpoint (live availability). Point it at a local mock server to test scenarios. |
| `catalog_url` | `"https://models.dev/api.json"` | The models.dev registry used for pricing truth. Point it at a local mock registry to test scenarios. |
| `api_key_env` | `"OPENCODE_API_KEY"` | Name of the environment variable holding this provider's API key. When unset/empty, no `Authorization` header is sent. |
| `model_url` | `""` | Per-model URL template. Empty by default because OpenCode has no per-model page, so **no embed link is created** for OpenCode messages. |
| `sanity_min_fraction` | `0.5` | Skip this provider's cycle (log a warning, send no messages) when the model count or `total_count` drops below this fraction of the provider's last successful cycle. `0` disables the guard. |

OpenCode exposes no `mode`/`suffix`/`text_only`/`blacklist` knobs — its detection is automatic and list-free (see [Detection semantics](#detection-semantics)), so those knobs do not appear where they would do nothing.

### Secrets (`.env` only)

| Variable | Required | Meaning |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | Yes | Webhook URL to POST messages to. Missing/empty fails fast at startup with a non-zero exit. |
| `OPENROUTER_API_KEY` | No | When set, sent as `Authorization: Bearer` on OpenRouter requests to raise rate limits. |
| `OPENCODE_API_KEY` | No | When set, sent as `Authorization: Bearer` on OpenCode requests to raise rate limits. |
| `TZ` | No | Timezone for process/log timestamps (also passed through to the container). |

Each provider reads the environment variable named by its `api_key_env`; when that variable is unset or empty, no `Authorization` header is sent at all.

## Detection semantics

### OpenRouter (unchanged)

Free detection is exactly as before and is fully configurable via `[providers.openrouter]`:

- **`mode`** — `zero_price` (both `pricing.prompt` and `pricing.completion` are `"0"`), `suffix` (`id` ends with `suffix`), or `either` (union).
- **`text_only`** — keep only outputs that are exactly text.
- **`blacklist`** — fnmatch globs (default `["openrouter/*"]`) matched against the `id`; matches are ignored entirely, so the `openrouter/free` auto-router never notifies.

### OpenCode (automatic, list-free)

OpenCode's models endpoint publishes only `id`/`object`/`created`/`owned_by` — **no pricing, name, description, or architecture** — and there is no per-model detail endpoint. To keep detection fully automatic with no hand-maintained list, the checker combines two sources:

- **Availability** comes from the OpenCode models endpoint (`[providers.opencode].url`). This live list is authoritative: models that exist only in the registry but are not currently served never produce false alarms.
- **Pricing truth** comes from the **models.dev** registry (`[providers.opencode].catalog_url`), under `api.json["opencode"]["models"]`. models.dev is the registry OpenCode itself uses to populate its model picker, making it the most trustworthy pricing source available.

A model is free when it is **currently served** *and* either its `id` ends in `-free` **or** the registry reports zero input **and** output cost:

```text
free(id) = id ∈ live AND ( id ends with "-free" OR ( catalog[id].cost.input == 0 AND catalog[id].cost.output == 0 ) )
```

There is deliberately **no suffix-only fallback** — if a model is served but the registry is missing or does not price it, treating it as free would later report a false removal. Suffix-named promos (`jev-1.13-free`) and unsuffixed stealth models the registry already prices at zero (`big-pickle`) are both caught; the junk `test` entry is excluded automatically because it is neither `-free` nor zero-cost. There is **no `blacklist`** for OpenCode because detection already excludes everything non-free.

**Catalog fetching.** While the provider is enabled, the catalog is fetched once per cycle with a conditional GET (`If-None-Match` with the cached ETag). A `304 Not Modified` reuses the in-memory cache; a `200` replaces it. The first cycle full-fetches (a restart refetches once). The cache is process-local, so repeated cycles do not re-download the multi-megabyte registry.

**Failure is a failed cycle.** If either the live list or the catalog fetch fails, that is **that provider's failed cycle**: no diff, no messages, retried after `retry_delay`. A degraded definition can therefore never emit false removals.

**Residual gap.** A brand-new stealth model that is served but has neither a `-free` suffix nor a models.dev entry yet is not detected until the registry catches up. This is the accepted cost of never maintaining a list.

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
| `${source}` | Provider display name (`OpenRouter` or `OpenCode`). Added to all four templates. |
| `${id}` | Model ID (raw). |
| `${name}` | Model name (Discord-escaped); falls back to the ID when the provider supplies no name (typical for OpenCode). |
| `${context_length}` | Comma-formatted digits, e.g. `262,144`. |
| `${modalities}` | Formatted modality, e.g. `text + image → text`. |
| `${summary}` | `description` truncated at ~280 characters on a word boundary + `…` (Discord-escaped). |
| `${url}` | The provider's `model_url` formatted with `{id}`, e.g. `https://openrouter.ai/<id>`. **An empty string when the provider has no model URL (OpenCode)**, in which case no embed link is created. |
| `${reason}` | Removal reason (removal only): `now has pricing` (still listed, but no longer zero-priced), `no longer listed` (absent from the API response), or `no longer free` (still listed and still zero-priced, but no longer classified as free — e.g. filtered out by the blacklist or the `text_only` rule, or no longer matching the configured rule). |
| `${error_type}` | `http`, `timeout`, `parse`, or `empty` (error only). |
| `${error_code}` | HTTP status string, else empty (error only). |
| `${error_message}` | Response body / exception text, truncated to ~500 characters (error only). |
| `${downtime}` | Humanized wall time, e.g. `1h 3m 12s` (recovery only). |
| `${failed_cycles}` | Number of consecutive failed cycles (recovery only). |

Default template bodies:

```text
# new_model.txt
`${id}`

Source: ${source}

${summary}
```

```text
# removed_model.txt
`${id}`

Source: ${source}

${reason}
```

```text
# error.txt
${source} models API error: ${error_type} ${error_code}

${error_message}
```

```text
# recovery.txt
The ${source} models API is responding again.

Downtime: ${downtime}
Failed cycles: ${failed_cycles}
```

## Message appearance

Each event type has its own icon and colour. New and removal embeds are titled `${icon} [${source}] ${name-or-id}`, and the provider is also named in the body via `Source: ${source}`, so every message says which provider it concerns. The model ID links to the provider's model page **only when the provider configures a `model_url`** — OpenRouter does, OpenCode does not, so OpenCode messages have no embed link.

| Event | Icon | Colour | Contents |
|---|---|---|---|
| New free model | 🆕 | Green (`#2ECC71`) | Title `${icon} [${source}] ${name-or-id}`; body rendered from `new_model.txt`; inline fields **Context length** (`<digits> tokens`) and **Modalities** (`text + image → text`) only when the provider supplies them, and **Free until** only when the API reports `expiration_date`. |
| Model leaves free set | ❌ | Red (`#E74C3C`) | Title `${icon} [${source}] ${name-or-id}`; body rendered from `removed_model.txt` (snapshot fields, so details survive after the model is gone). |
| API error | ⚠️ | Yellow (`#F1C40F`) | Title `${icon} ${source} models API error`; body rendered from `error.txt` with error type, code, and detail; inline **Error** field (`<type> <code>`). No URL. |
| Recovery | ✅ | Green (`#2ECC71`) | Title `${icon} ${source} models API recovered`; body rendered from `recovery.txt` with downtime and failed-cycle count. No URL. |

Because OpenCode's payload is sparse (id only), its messages are **identifier-first**: the title falls back to the ID and the context-length/modality fields are omitted, yielding a clean message with the `Source:` line.

Every embed carries a detection timestamp (UTC in the payload, localized by Discord) and the webhook display name from `[discord].username`.

With `message_format = "plain"`, the same rendered body (including the `Source:` line) is posted as markdown `content` prefixed with the event icon, and no embed is created — a fallback so you can abandon embeds without touching code.

## Persistence

All runtime state lives in the bind-mounted `./data` directory and survives restarts, rebuilds, and `docker compose down`:

- **`data/state.json`** — human-readable JSON, **schema v2**, with one section per provider keyed by display name. Each section holds `models` (the current free set keyed by model ID, each with a snapshot of the provider's fields — `id`, `name`, `description`, `context_length`, `architecture`, `expiration_date` — plus `first_seen`, `notified_at`, and `pending_removal` bookkeeping), `last_success` (for the sanity gate), and `failure` (streak counters). Writes are atomic (temp file + rename), so an interrupted write never corrupts it.

  ```json
  {
    "schema_version": 2,
    "sources": {
      "OpenRouter": {
        "models": { "<id>": { "...snapshot...", "first_seen": "...", "notified_at": "...|null", "pending_removal": "..." } },
        "last_success": { "timestamp": "...", "data_len": 0, "total_count": 0 },
        "failure": { "consecutive_failures": 0, "streak_started": null, "error_notified": false }
      },
      "OpenCode": { "...": "..." }
    }
  }
  ```

- **`data/history.jsonl`** — append-only change log, one JSON object per line. Each record carries a **`source`** field (the provider display name) alongside `ts`, `event`, and event-specific fields (`id`, `reason`, `detail`). Events: `seed`, `added`, `removed`, `error`, `recovery`, `cycle_skipped`. Use it to audit what happened per provider without a database.

State is keyed by provider, so it is easy to inspect or repair one provider by hand without touching the other.

### Legacy state and re-seeding

**No migration.** A legacy (schema v1) or otherwise unknown-shaped state file is ignored, and every enabled provider seeds fresh from its API on the next cycle. This means an upgrade is effectively a first run per provider.

To make the service re-announce inventories from scratch (a fresh baseline), stop the service and delete the state file:

```sh
docker compose down
rm data/state.json
docker compose up -d
```

On the next start each provider's state section is empty, so the checker seeds that provider's free set and appends a `seed` history event with its `source`. If `notify_on_first_run = true` it sends a 🆕 message for every currently free model; set it to `false` first if you want to re-seed silently. Seeding is per provider and independent, so a newly enabled provider seeds without re-announcing providers whose state is already warm. `data/history.jsonl` is not touched by re-seeding (it only ever grows).

## First-run behavior

When a provider has no section in `data/state.json` (missing or empty file, a newly enabled provider, or a v1 file that is ignored), the service seeds that provider's current free set, appends one `seed` event carrying the source, and — when `notify_on_first_run = true` (the shipped default) — sends a 🆕 message for every current free model **for that provider only**. No removal messages are ever sent on a first run.

On subsequent starts with warm state, the diff is against the persisted per-provider free sets, so a simple restart produces no messages at all unless a catalog actually changed. A message is marked notified in state only after Discord returns a 2xx, so transient delivery failures are retried on the next cycle rather than silently lost — no duplicates, no gaps.

## Failure handling

Each provider has its own fetch → guard → diff → notify → persist pipeline and its own failure state:

- **Per-provider isolation.** One provider's outage never blocks the other provider's diff or delays its notifications.
- **Per-provider streaks.** Failures increment only that provider's `consecutive_failures`; the provider's first successful cycle resets the streak.
- **`error_notify`.** With `once` (default) you get a single ⚠️ per streak per provider; with `every` you get one per failed cycle. Error messages name the provider and carry type/code/message details.
- **Recovery.** When a provider responds again, `notify_recovery = true` sends one ✅ naming the provider and carrying the downtime and failed-cycle count.
- **Sanity gate.** Each provider's sanity gate compares against **its own** last successful cycle and skips only that provider.
- **No false removals.** Failed or empty provider responses are never interpreted as removals — the cycle aborts with no diff and no messages.
- **Retry cadence.** Failed cycles retry after `retry_delay`, not the full interval, so recovery is quick.

## Troubleshooting

**Watch the logs.** Live out-of-container view:

```sh
docker compose logs -f          # stdout (one line per event per provider) + stderr
docker compose logs -f free-model-checker
```

Errors (fetch failures, Discord send failures, guard warnings) are written to **stderr**; normal cycle events are on **stdout**. A `docker compose logs` line carries a stream tag you can use to tell them apart when reading a file.

**Non-zero exit on bad configuration.** If `config.toml` is missing or invalid, a provider name is unknown, or `DISCORD_WEBHOOK_URL` is unset/empty, the process logs a clear line and exits non-zero instead of running blind. An unknown provider name lists the supported providers so you can fix the typo. With `restart: unless-stopped` Compose will keep restarting it — check the logs, fix `.env`/`config.toml`, then `docker compose up -d` again.

**Per-provider streaks.** Failure state is per provider, so read the streak counters per source. A ⚠️ names the provider whose streak began; the matching ✅ names the same provider when it recovers. A failure that appears in only one provider's log lines does **not** affect the other, which keeps diffing on schedule. With `error_notify = "once"` you get one ⚠️ per streak per provider; the following successful cycle sends one ✅ with downtime and the failed-cycle count.

**Discord 429 (rate limited).** If the webhook returns HTTP 429, the checker reads `retry_after` from the response (the `Retry-After` header is preferred, falling back to the JSON body's `retry_after` field), sleeps that long, and retries the *same* message. Messages in a batch are also spaced by `[discord].message_gap_seconds` (1 second by default). A burst of changes is therefore throttled, not lost.

**Other Discord send failures.** A non-429 send failure is logged to stderr and the message is *not* marked notified, so the next cycle retries it. Fix the webhook URL and the backlog clears itself.

**Sanity-gate warnings (per provider).** A log line about the model count dropping below the sanity fraction means **that provider's** cycle was **skipped**: its fetch succeeded but the response looked truncated (fewer models, or a lower `total_count`, than half of that provider's last successful cycle). No Discord message is sent and that provider's free set is left untouched, so a partial response can never be mistaken for mass removal. The other provider is unaffected. If you legitimately expect the catalog to shrink that much (or you are testing against a tiny mock), set `sanity_min_fraction = 0` for that provider in `config.toml` to disable the guard, then `docker compose restart`.

**Empty or failed responses.** An empty `data` array, a non-2xx status, a timeout, or unparseable JSON all abort **that provider's** cycle without removals. Failures increment that provider's streak and retry after `retry_delay` (not the full interval) so recovery is quick. With `error_notify = "once"` you get one ⚠️ per streak per provider; the following successful cycle sends one ✅ with downtime and the failed-cycle count.

**models.dev failure (OpenCode).** OpenCode's cycle needs two fetches: its live models endpoint and the models.dev catalog. If **either** fails, the whole cycle for OpenCode fails — no diff, no messages, and no removals (so a missing catalog can never make every model look removed). The cycle is retried after `retry_delay`. The live-availability list stays authoritative, so a catalog that is temporarily behind or missing only delays detection; it never produces false alarms. OpenRouter is unaffected.

**`data/` permission errors.** The `data` directory is bind-mounted from the host, and the container runs as a non-root user with uid/gid **1000**. If the host directory is owned by a different user, the container cannot write `state.json` and you will see permission errors in the logs. Fix the ownership on the host:

```sh
sudo chown -R 1000:1000 data
```

The same applies if you run the container with a different UID via an override.

**Malicious or malformed data / edge cases.** Model names, descriptions, and IDs come from third-party APIs. The checker escapes backticks before inserting them into embeds, truncates the summary at ~280 characters (error messages at ~500), and caps embed descriptions at 4096 and plain content at 2000 characters, so a hostile or malformed field cannot break the message or exceed Discord's limits. A description containing `${...}`-looking text does not affect rendering because `string.Template` renders placeholders in the template file only, not in substituted values; if you want to change wording, edit the template rather than model data.

## Verification (manual end-to-end)

There is **no unit-test framework and no CI**; behavior is verified end-to-end by running the stack and observing externally observable evidence only — Discord payloads captured by the fake webhook, stdout/stderr, and the contents of `data/`. The repository ships a stdlib fake-webhook tool, mock fixtures, and a compose test overlay so you can exercise the whole service on one machine, in containers, without a real Discord channel and without `host.docker.internal`.

### Shipped test tooling

```text
tools/fake_webhook.py        # stdlib; prints each POST payload, responds 204 (GET -> 200); --host/--port flags; binds 0.0.0.0; --log PATH appends JSONL payloads
tools/fixtures/
  openrouter/models.json     # {"data":[...], "total_count": N}
  openrouter/empty.json      # {"data":[], "total_count": 0}
  opencode/models.json       # {"object":"list","data":[{"id","object","created","owned_by"}]}
  models.dev/api.json        # {"opencode":{"models":{"<id>":{...,"cost":{"input","output"}}}}}
tools/test.config.toml       # points provider urls + catalog_url at the mock service, webhook at the fake webhook
compose.test.yaml            # overlay: fake-webhook service + mock static-file service + test config mount + env
```

`compose.test.yaml` is an **overlay** on `compose.yaml`. It adds a fake-webhook service and a mock-models service (a static file server, `python -m http.server`, serving `tools/fixtures`), sets `DISCORD_WEBHOOK_URL=http://fake-webhook:9090/hook`, and mounts `./tools/test.config.toml` over `/app/config.toml` — that mounted config is what points the checker's provider URLs and `catalog_url` at the mock (`http://mock:8080/<path>`). Because everything runs on the compose network in containers, verification works even where host networking is firewalled.

The mock server re-reads fixture files on every request, so **editing a file under `tools/fixtures/` takes effect on the next cycle** — no restart needed. Config edits (for example `message_format = "plain"`) go in `tools/test.config.toml` and need `docker compose -f compose.yaml -f compose.test.yaml restart free-model-checker` to be picked up. To capture payloads to a file for scripted assertions, add `--log` to the fake-webhook command in `compose.test.yaml` (or run `python tools/fake_webhook.py --log /tmp/hooks.jsonl` on the host).

### Running the scenarios

Start the overlay stack and follow the captured payloads:

```sh
docker compose -f compose.yaml -f compose.test.yaml up -d
docker compose -f compose.yaml -f compose.test.yaml logs -f fake-webhook
```

> The base `compose.yaml` uses `env_file: .env`, so a `.env` must exist even for the overlay (`cp .env.example .env` is enough — the overlay's `DISCORD_WEBHOOK_URL` overrides whatever is in it).

The seams you use are all config or files: the per-provider `url` override, OpenCode's `catalog_url` override (`tools/test.config.toml`), the `DISCORD_WEBHOOK_URL` env var (`compose.test.yaml`), hand-editing `data/state.json`, and editing the fixture files. Walk the scenarios in order:

1. **Fresh state** → both providers announce their current free sets, each message labeled with its provider, and `data/history.jsonl` shows `seed` events carrying the right `source`.
2. **Warm restart** → no duplicate messages on the next cycle.
3. **Delete one model entry** from a provider's section in `data/state.json` → exactly one 🆕 for that provider.
4. **Add a fake entry** to a provider's section → exactly one ❌, rendered from the stored snapshot with the right reason (`no longer listed`).
5. **Point one provider's `url` at an unreachable host** (e.g. `http://mock:9999/openrouter/models.json`, a dead port) in `tools/test.config.toml` and restart the checker → one ⚠️ for that provider only while the other keeps diffing; revert → one ✅ with downtime, labeled with the provider.
6. **Make a mock return an empty `data` array** (point a provider's `url` at `http://mock:8080/openrouter/empty.json`) → that provider's cycle is skipped, a warning is logged, and there is no ❌ flood.
7. **Edit the fixtures to add or remove a zero-cost model** in the mock catalog (`tools/fixtures/models.dev/api.json`) and/or the OpenCode fixture (`tools/fixtures/opencode/models.json`) → OpenCode 🆕/❌ without touching real models.dev.
8. **Break the webhook URL** (point the overlay's `DISCORD_WEBHOOK_URL` at a bad host in `compose.test.yaml` and restart) → a stderr line and no crash; fix it and the message is retried on the next cycle.
9. **Set `message_format = "plain"`** in `tools/test.config.toml` and restart the checker → the same bodies (including the `Source:` line) arrive as markdown with no embeds.
10. **Real-API sanity run.** With the fake webhook still attached but the providers pointed back at their real endpoints (`https://openrouter.ai/api/v1/models`, `https://opencode.ai/inference/v1/models`, and `https://models.dev/api.json`), the captured payload set matches the expected free sets for both providers.

Only externally observable behavior counts as evidence: captured Discord payloads, stdout/stderr, and the contents of `data/`.
