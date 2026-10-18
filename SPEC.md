# free-model-checker — Spec

Status: ready for implementation
Date: 2026-10-18
Source: planning conversation (grilling rounds 1–3), including live inspection of `https://openrouter.ai/api/v1/models`.

This project introduces its own small vocabulary; the spec uses it consistently:

- **free set** — the models the current detection rule classifies as costing $0 to use (`prompt == "0" && completion == "0"` for the default mode).
- **cycle** — one fetch → guard → diff → notify → persist pass.
- **streak** — consecutive failed cycles; reset by the first successful cycle.
- **snapshot** — the stored copy of a model's API fields at the time it was last seen free.
- **guard** — a rule that aborts a cycle whose data is untrustworthy.

---

## Problem Statement

OpenRouter's catalog contains free/preview/promo models (input and output price both $0) that appear and disappear without notice. Today the operator has to remember to check the models API by hand; finding a new free model is manual drudgery, and a promo ending is discovered by surprise when usage suddenly costs money. There is no built-in alerting. The operator wants a set-and-forget service on his own hardware that watches the catalog and pings a private Discord channel when the free set changes, with enough detail (ID, context length, modalities, summary) to decide whether to use a model without opening the docs.

## Solution

A self-running Docker Compose service containing a single dependency-free Python process. On a configurable interval it fetches the OpenRouter models API, computes the current free set, and diffs it against state persisted in a bind-mounted volume. Changes are announced as templated Discord embeds posted to a webhook in a channel on a private server the operator creates (the README walks through server → channel → webhook). New free models get a 🆕 message, models leaving the free set get a ❌ message with the reason, API failures get a ⚠️ message (first of a streak only, by default), and recovery gets a ✅ message. Non-secret settings live in `config.toml`; secrets live in `.env`. Message wording lives in bind-mounted template files so it can be edited with a restart instead of a rebuild. There is no AI anywhere in the product.

Verified API facts that shaped this design (live, 2026-10-18):

- 471 models total; 16 IDs end in `:free`; 19 are zero-priced `prompt`/`completion`; the difference is `google/lyria-3-pro-preview`, `google/lyria-3-clip-preview` (audio) and `openrouter/free` (an auto-router, not a model).
- `pricing` values are strings (`"0"` is a valid free value).
- Each model carries `id`, `name`, `description`, `context_length`, `architecture` (`modality`, `input_modalities`, `output_modalities`), and some carry `expiration_date`.

## User Stories

### Deployment and configuration

1. As an operator, I want to deploy the whole service with `docker compose up -d --build`, so that I never install Python or dependencies on the host.
2. As an operator, I want a numbered README walkthrough to create a private Discord server, channel, and webhook, so that notifications land somewhere only I can see them.
3. As an operator, I want to supply the webhook URL as a secret in `.env`, so that it never enters version control.
4. As an operator, I want to point the service at a different models endpoint via `config.toml`, so that I can test against a mock server or a mirror.
5. As an operator, I want to set the polling interval with human-readable values (`"30m"`, `"1h"`, `"24h"`, or bare seconds), so that I can dial frequency without arithmetic.
6. As an operator, I want the first cycle to run immediately at container start (`run_on_start`), so that a restart doesn't make me wait a full interval.
7. As an operator, I want the schedule to live inside the long-running process (no host cron, no in-container cron daemon), so that deployment has exactly one moving part.
8. As an operator, I want `config.toml` to hold all non-secret settings and `.env` to hold only secrets, so that the repository is safe to share.
9. As an operator, I want to tune the definition of "free" (`mode = "zero_price" | "suffix" | "either"`), so that I can adapt if OpenRouter changes conventions.
10. As an operator, I want a blacklist of glob patterns (default `["openrouter/*"]`), so that the auto-router and any other non-model entries never notify me.
11. As an operator, I want a text-only toggle (`text_only = true`) that drops models whose `architecture.output_modalities` lacks `"text"`, so that audio-only models like the Lyria pair never notify me even when zero-priced.
12. As an operator, I want every rule above changeable through configuration, so that tuning never requires editing code.
13. As an operator, I want `TZ` passed through from `.env`, so that log timestamps match my local clock.

### Notifications: new free models

14. As an operator, I want a Discord message the first cycle a model joins the free set, so that I learn about free capacity immediately.
15. As an operator, I want each new model in its own message, so that I can act on each without parsing a digest.
16. As an operator, I want the new-model message to contain the model ID, name, context length, modalities, and summary sourced from the models API, so that I can evaluate it without opening the docs.
17. As an operator, I want the summary to be the API `description` truncated on a word boundary at ~280 characters with an ellipsis, so that long descriptions never break the embed.
18. As an operator, I want the model ID to link to its OpenRouter page, so that one click opens the detail view.
19. As an operator, I want a "Free until" field shown only when the API reports `expiration_date`, so that I know when a promo or preview ends.
20. As an operator, I want a 🆕 icon and green color on new-model messages, so that "new" is obvious at a glance.
21. As an operator, I want a detection timestamp on the embed (UTC in the payload, localized by Discord), so that I know when the model was found.

### Notifications: models leaving the free set

22. As an operator, I want a ❌ message the cycle a model leaves the free set, so that I stop relying on it before it costs money.
23. As an operator, I want the removal reason distinguished — `now has pricing` when the model is still in the API response with non-zero price, `no longer listed` when it is absent — so that I know whether the model still exists.
24. As an operator, I want removal messages rendered from the stored snapshot (not the current API response), so that context length, modalities, and summary survive even after the model disappears entirely.
25. As an operator, I want ❌ messages in red, so that removals never read as good news.

### Failure handling

26. As an operator, I want a ⚠️ message when the models API call fails, containing error type (`http` / `timeout` / `parse`), error code (e.g. `503`), and error message details (response body or exception text, truncated to ~500 characters), so that I know why notifications stopped.
27. As an operator, I want only the first failure of a streak to notify by default (`error_notify = "once"`), so that a day-long outage doesn't ping me every hour.
28. As an operator, I want an `error_notify = "every"` option, so that I can see each failure while debugging.
29. As an operator, I want a ✅ recovery message once the API works again, reporting the outage duration and failed-cycle count, so that I know the gap is closed.
30. As an operator, I want a failed or empty API response to never be interpreted as mass removal, so that a glitch can't produce a burst of false ❌ alerts.
31. As an operator, I want a sanity gate that skips the cycle (logging a warning, sending no messages) when the model count drops below 50% of the last successful cycle, disable-able via config, so that partial responses are ignored.
32. As an operator, I want failed cycles to retry after a short delay (`retry_delay = "5m"`) rather than the full interval, so that recovery is quick.
33. As an operator, I want Discord send failures logged to stderr and retried on the next cycle, so that nothing is silently lost.
34. As an operator, I want an optional `OPENROUTER_API_KEY` that is sent as `Authorization: Bearer` only when set, so that I can raise rate limits later without code changes.

### State and persistence

35. As an operator, I want state and history to persist in a bind-mounted `data` directory, so that restarts and rebuilds never re-announce the world.
36. As an operator, I want `state.json` to be human-readable JSON, so that I can inspect or repair it by hand.
37. As an operator, I want an append-only `history.jsonl` change log, so that I can audit what happened without running a database.
38. As an operator, I want a Discord message marked notified only after the webhook returns 2xx, so that transient Discord failures retry instead of vanishing (no duplicates, no losses).
39. As an operator, I want first-run behavior controlled by `notify_on_first_run` (shipping `true`), so that first boot announces the current full inventory but a wiped volume can be silenced.
40. As an operator, I want the tool to record a "seed" event in history on first run, so that I can tell a fresh state apart from an established one.

### Templating and appearance

41. As an operator, I want message prose to live in template files bind-mounted into the container, so that rewording requires only a restart.
42. As an operator, I want icons and colors as `config.toml` keys (`🆕`/`❌`/`⚠️`/`✅`, `#2ECC71`/`#E74C3C`/`#F1C40F`), so that re-skinning never touches templates or code.
43. As an operator, I want embed titles of the form `${icon} ${name}` and a webhook display name of "Free Model Checker", so that messages look intentional in the channel.
44. As an operator, I want a `message_format = "plain"` fallback that wraps the same rendered body as plain markdown content, so that I can abandon embeds without code changes.
45. As an operator, I want baked-in default templates used when the template mount is absent, so that the container still works with volumes removed.

### Operations

46. As an operator, I want one plain log line per event on stdout, readable with `docker compose logs -f`, so that I can watch cycles without tooling.
47. As an operator, I want the container to run as a non-root user with `restart: unless-stopped`, so that the service is safe and self-healing.
48. As an operator, I want the service to honor HTTP 429 with `retry_after` when sending Discord messages, interspersed with a 1-second gap between messages in a batch, so that a burst of changes never gets rate-limited into silent loss.
49. As a maintainer, I want zero third-party Python dependencies, so that image updates are trivial and the build is fast.
50. As a maintainer, I want a README covering build, compose usage, Discord webhook setup, the full config reference, persistence, template editing, interval syntax, first-run behavior, and troubleshooting, so that the stack can be redeployed on a new machine from scratch.

## Implementation Decisions

- **Shape**: one long-running Python 3.12 process, standard library only (`urllib.request`, `json`, `tomllib`, `fnmatch`, `string.Template`, `logging`). Base image `python:3.12-slim`, non-root user, `WORKDIR /app`.
- **Deliverables (top level)**: `checker.py`, `config.toml`, `.env.example`, `templates/`, `data/` (runtime, gitignored), `Dockerfile`, `compose.yaml`, `README.md`, `.gitignore`, `.dockerignore`.
- **Templates** (user-facing contract, bind-mounted at `/app/templates`): `new_model.txt`, `removed_model.txt`, `error.txt`, `recovery.txt`, rendered with `string.Template`. Baked-in defaults exist in the image for when the mount is missing. Placeholder vocabulary: `${icon}`, `${id}`, `${name}`, `${context_length}`, `${modalities}`, `${summary}`, `${url}`, `${reason}`, `${error_type}`, `${error_code}`, `${error_message}`, `${downtime}`. Default `new_model.txt` = backticked ID line + truncated summary.
- **Config contract** (`config.toml`, tomllib):

  ```toml
  [api]       url = "https://openrouter.ai/api/v1/models"; timeout_seconds = 30
  [schedule]  interval = "1h"; run_on_start = true; retry_delay = "5m"
  [detection] mode = "zero_price"        # zero_price | suffix | either
              text_only = true           # drop models whose output modalities lack "text"
              blacklist = ["openrouter/*"]  # fnmatch globs
              sanity_min_fraction = 0.5  # 0 disables the drop guard
  [discord]   message_format = "embed"   # embed | plain
              username = "Free Model Checker"
              error_notify = "once"      # once | every
              notify_recovery = true
              notify_on_first_run = true
  [icons]     new = "🆕"; removed = "❌"; error = "⚠️"; recovery = "✅"
  [colors]    new = "#2ECC71"; removed = "#E74C3C"; error = "#F1C40F"
  ```

  Secrets in `.env` only: `DISCORD_WEBHOOK_URL` (required), `OPENROUTER_API_KEY` (optional), `TZ` (optional). Missing/invalid config or missing webhook URL fails fast at startup with a clear log line and non-zero exit.
- **Detection rule**: `zero_price` = `pricing.prompt == "0" && pricing.completion == "0"`; `suffix` = `id.endswith(":free")`; `either` = union. Then apply blacklist globs (fnmatch) and, when `text_only`, require `"text" in architecture.output_modalities`.
- **Loop**: `fetch → guards → diff → notify → persist → sleep(interval)`. Interval parser accepts `<n><m|h|d>` and bare seconds. Runs once immediately when `run_on_start`. A cycle cannot overlap itself; sleep runs after the work completes.
- **Guards** (all abort the cycle, send no removals): fetch exception/timeout; non-2xx HTTP; unparseable JSON; empty `data`; and when the sanity gate is enabled, `len(data)` or `total_count` below `sanity_min_fraction` of the last successful cycle's values (log warning only, no Discord message for this guard).
- **Error streak state machine**: failures increment `consecutive_failures`; the first failure of a streak sends ⚠️ when `error_notify = "once"` (every failure sends when `"every"`); any successful cycle with `consecutive_failures > 0` sends ✅ (when `notify_recovery`) carrying `${downtime}` (humanized wall time) and the failed-cycle count, then resets the counter. Failed cycles sleep `retry_delay` instead of `interval`.
- **Notification pipeline**: build message list as new models first, then removals, each alphabetical by ID; one message per model; 1-second spacing between sends; on HTTP 429 sleep `retry_after` from the response body and retry that message; a message's `notified_at` in state is written only after a 2xx; on other send failures log to stderr and leave `notified_at` unset so the next cycle retries.
- **State schema** (`data/state.json`): `schema_version`, `models` keyed by ID with snapshot fields (`id`, `name`, `description`, `context_length`, `architecture`, `expiration_date`), `first_seen`, `notified_at`; plus `last_success` (`timestamp`, `data_len`, `total_count`) for the sanity gate and `failure` streak bookkeeping (`consecutive_failures`, `streak_started`). Writes are atomic (temp file + rename). `data/history.jsonl` appends one JSON object per event: `{"ts", "event", "id"?, "reason"?, "detail"?}`.
- **Embed contract**: `title` = `${icon} ${name}` (with icon+color per event type); `url` = `https://openrouter.ai/<id>`; `description` = rendered template body; inline fields = Context length (comma-separated digits + " tokens"), Modalities (formatted `text + image → text` from `architecture.modality`), Free until (only when `expiration_date` exists); `timestamp` = cycle time in UTC ISO-8601; `username` override on the webhook payload. Removal embeds reuse snapshot fields. Error/recovery embeds use their own templates with ${error_*} / ${downtime} placeholders.
- **Truncation**: summary at ~280 chars on a word boundary + `…`; error message ~500 chars; final embed description capped at 4096 and plain content at 2000 characters as a safety net. Descriptions may contain backticks, so escaping is applied before embedding.
- **Plain mode**: `message_format = "plain"` posts `{"content": rendered_body}` (including icon) with no embed, same pipeline otherwise.
- **First run**: empty/missing state file → seed the free set, append a `seed` history event, and, when `notify_on_first_run = true`, send 🆕 messages for every current free model.
- **HTTP client**: plain `urllib.request` with timeout from config; sets `Authorization` only when the API key env var is present; a `User-Agent` identifying the checker.
- **Compose/manifest**: `compose.yaml` service `free-model-checker` with `restart: unless-stopped`, build from the local Dockerfile, `env_file: .env`, bind mounts `./data:/app/data` and `./templates:/app/templates:ro`, `TZ` passthrough; data and templates paths are container-internal defaults with no config keys needed.
- **README content**: Discord server/channel/webhook walkthrough (numbered, with the exact UI path), config reference table (every key, default, meaning), build/run/log/stop commands, interval syntax, template editing flow, persistence explanation, first-run behavior, troubleshooting (stderr logs, 429s, sanity-gate warnings, how to re-seed by deleting `state.json`).
- **Repository state**: the working directory is not yet a git repository; the first implementation step is `git init` followed by a single initial commit. No remote, no push.

## Testing Decisions

- The operator explicitly declined unit tests and implementation tests; they will test by running the stack. The testing approach is therefore **manual end-to-end verification against a real Discord webhook**, with no test framework, no CI, and no assertions on internals. Only externally observable behavior (channel messages, stdout/stderr, `data/` contents) counts as evidence.
- The three existing seams double as the manual-test hooks, and no new seams are introduced:
  1. **`[api] url`** — point at a local mock JSON server or a copy of the response to simulate additions, removals, empty payloads, partial payloads, and HTTP errors without touching code.
  2. **`DISCORD_WEBHOOK_URL`** — a deliberately broken URL exercises the stderr-log-and-retry path; a real webhook exercises delivery.
  3. **`data/state.json`** — hand-editing it injects scenarios: deleting an entry makes the model re-announce as 🆕; adding a fake entry makes the next cycle emit ❌ (snapshot-rendered, `no longer listed`).
- Manual scenario checklist the operator will run (documented in the README's troubleshooting section where applicable):
  1. Fresh volume + `notify_on_first_run = true` → inventory of 🆕 embeds; history shows a seed event.
  2. Restart with warm volume → no duplicate messages.
  3. Remove one entry from `state.json` → exactly one 🆕 message on the next cycle.
  4. Add a fake entry to `state.json` → exactly one ❌ message rendered from the snapshot.
  5. Set `[api] url` to an unreachable host → one ⚠️ per streak (with `once`); revert → one ✅ with downtime.
  6. Return an empty `data` array from a mock → cycle skipped, no ❌ flood, warning in logs.
  7. Break the webhook URL → stderr line, no crash, retry next cycle after fixing.
  8. Set `message_format = "plain"` → same content as markdown text, no embed.

## Out of Scope

- Any AI/LLM usage in the product, by explicit operator requirement.
- Unit tests, integration tests, and CI pipelines.
- Pushing to a remote git repository (local `git init` + one initial commit only).
- Discord bot tokens and DM delivery; delivery is webhook-to-channel only.
- Cron-expression scheduling; only interval strings are supported.
- `--once` / `--dry-run` CLI flags (offered during planning; not requested, excluded from scope).
- Multiple webhooks/channels, per-model subscriptions, or any fan-out.
- Editing or deleting already-posted Discord messages.
- Tracking paid pricing, price history, benchmarks, or any model attribute beyond the fields listed in the embed contract.
- Backfilling history for models that were free before the first run; only the state at first boot is captured.
- Web UI, API of its own, metrics endpoints, or database backends.

## Further Notes

- Discord embed limits respected by the truncation rules: title 256, description 4096, field value 1024, ≤25 fields, total embed 6000, plain content 2000.
- The default blacklist exists because `openrouter/free` is OpenRouter's auto-router, not a model, and it is zero-priced.
- `pricing` values arrive as strings; comparisons must be string/exact against `"0"` rather than float coercion.
- `:free` IDs and zero-priced non-suffix models overlap but are not identical; `mode` exists so the operator can switch without recalling this spec.
- This spec was written to a file (`SPEC.md`) in the project directory at the operator's instruction rather than published to an issue tracker; no tracker is configured for this directory, and the usual triage label (`ready-for-agent`) therefore does not apply. If a tracker is set up later, this document is written to be pasted in as the issue body.
- Implementation must not begin until the operator says so; the deliverable of this document is the spec alone.
