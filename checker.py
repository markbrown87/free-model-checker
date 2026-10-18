#!/usr/bin/env python3
"""free-model-checker.

A single, dependency-free Python 3.12 process that polls the OpenRouter models
API on an interval, computes the current set of models that cost $0 to use,
diffs it against persisted state, and announces additions/removals to a Discord
webhook using user-editable templates.

Standard library only. See SPEC.md for the full behavior specification.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import signal
import socket
import string
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

APP_DIR = "/app"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = 1
USER_AGENT = "FreeModelChecker/1.0"
MODEL_URL_PREFIX = "https://openrouter.ai/"
RETRY_AFTER_ATTEMPTS = 5
# Default spacing between consecutive Discord sends; also the granularity at
# which long sleeps are broken up so a stop request is honoured promptly.
MESSAGE_GAP_SECONDS = 1.0
SUMMARY_LIMIT = 280
ERROR_MESSAGE_LIMIT = 500
EMBED_DESCRIPTION_LIMIT = 4096
PLAIN_CONTENT_LIMIT = 2000
TITLE_LIMIT = 256

# Baked-in default templates. These MUST be byte-identical to the files in
# templates/ so the container still works when the bind mount is absent.
DEFAULT_TEMPLATES = {
    "new_model.txt": "`${id}`\n\n${summary}\n",
    "removed_model.txt": "`${id}`\n\n${reason}\n",
    "error.txt": (
        "OpenRouter models API error: ${error_type} ${error_code}\n\n"
        "${error_message}\n"
    ),
    "recovery.txt": (
        "The OpenRouter models API is responding again.\n\n"
        "Downtime: ${downtime}\n"
        "Failed cycles: ${failed_cycles}\n"
    ),
}

DEFAULT_CONFIG = {
    "api": {
        "url": "https://openrouter.ai/api/v1/models",
        "timeout_seconds": 30,
    },
    "schedule": {
        "interval": "1h",
        "run_on_start": True,
        "retry_delay": "5m",
    },
    "detection": {
        "mode": "zero_price",
        "text_only": True,
        "blacklist": ["openrouter/*"],
        "sanity_min_fraction": 0.5,
    },
    "discord": {
        "message_format": "embed",
        "username": "Free Model Checker",
        "error_notify": "once",
        "notify_recovery": True,
        "notify_on_first_run": True,
        "message_gap_seconds": MESSAGE_GAP_SECONDS,
    },
    "icons": {
        "new": "\U0001f195",
        "removed": "\u274c",
        "error": "\u26a0\ufe0f",
        "recovery": "\u2705",
    },
    "colors": {
        "new": "#2ECC71",
        "removed": "#E74C3C",
        "error": "#F1C40F",
    },
}

VALID_MODES = {"zero_price", "suffix", "either"}
VALID_MESSAGE_FORMATS = {"embed", "plain"}
VALID_ERROR_NOTIFY = {"once", "every"}

logger = logging.getLogger("free-model-checker")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ConfigError(Exception):
    """Raised for missing or invalid configuration; leads to a fast exit."""


class StateError(Exception):
    """Raised when persisted state cannot be read and must be repaired."""


class FetchError(Exception):
    """A classified models-API fetch failure."""

    def __init__(self, error_type: str, error_code: str, error_message: str):
        super().__init__(f"{error_type} {error_code}: {error_message}")
        self.error_type = error_type
        self.error_code = error_code
        self.error_message = error_message


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


class _BelowWarning(logging.Filter):
    """Only let records below WARNING through (so errors are not duplicated)."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        return record.levelno < logging.WARNING


def setup_logging() -> None:
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.addFilter(_BelowWarning())
    stdout_handler.setFormatter(formatter)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(formatter)

    logger.addHandler(stdout_handler)
    logger.addHandler(stderr_handler)


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #


def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with a trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def parse_interval(value) -> int:
    """Parse "<n>[smhd]" or bare seconds into an integer number of seconds."""
    if isinstance(value, bool):
        raise ConfigError(f"invalid interval: {value!r}")
    if isinstance(value, (int, float)):
        seconds = int(value)
    elif isinstance(value, str):
        text = value.strip().lower()
        if not text:
            raise ConfigError("interval must not be empty")
        unit = "s"
        if text[-1] in "smhd":
            unit = text[-1]
            text = text[:-1]
        text = text.strip()
        if not text.isdigit():
            raise ConfigError(
                f"invalid interval {value!r}: expected '<n>[smhd]' or seconds"
            )
        seconds = int(text) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    else:
        raise ConfigError(f"invalid interval type: {type(value).__name__}")
    if seconds <= 0:
        raise ConfigError(f"interval must be positive, got {value!r}")
    return seconds


def humanize_duration(seconds: float) -> str:
    """Humanize a duration, e.g. 3720 -> '1h 2m'."""
    total = int(max(0, round(seconds)))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def parse_color(value) -> int:
    """Parse a '#RRGGBB' string into a Discord decimal color int."""
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) == 6:
            try:
                return int(text, 16)
            except ValueError:
                pass
    raise ConfigError(f"invalid color value: {value!r} (expected '#RRGGBB')")


def escape_backticks(text: str) -> str:
    return text.replace("`", "\\`")


def truncate_words(text: str, limit: int = SUMMARY_LIMIT) -> str:
    """Truncate on a word boundary and append an ellipsis."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip() + "\u2026"


def truncate_hard(text: str, limit: int) -> str:
    """Hard-truncate and append an ellipsis when over the limit."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "\u2026"


def format_int(value) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{int(value):,}"
    return ""


def format_modalities(architecture) -> str:
    """Format 'text+image->text' as 'text + image \u2192 text'."""
    if not isinstance(architecture, dict):
        return ""
    modality = architecture.get("modality")
    if isinstance(modality, str) and modality:
        return modality.replace("->", " \u2192 ").replace("+", " + ")
    inputs = architecture.get("input_modalities") or []
    outputs = architecture.get("output_modalities") or []
    if inputs or outputs:
        left = " + ".join(str(item) for item in inputs)
        right = " + ".join(str(item) for item in outputs)
        return f"{left} \u2192 {right}"
    return ""


def render_template(template_text: str, context: dict) -> str:
    return string.Template(template_text).safe_substitute(context)


# --------------------------------------------------------------------------- #
# Configuration loading
# --------------------------------------------------------------------------- #


def find_base_dir() -> str:
    """Directory holding config.toml (next to the script, else /app)."""
    if os.path.exists(os.path.join(SCRIPT_DIR, "config.toml")):
        return SCRIPT_DIR
    if os.path.exists(os.path.join(APP_DIR, "config.toml")):
        return APP_DIR
    return SCRIPT_DIR


def load_env_file(path: str) -> dict:
    """Minimal KEY=VALUE .env parser (no python-dotenv)."""
    env: dict[str, str] = {}
    if not path or not os.path.exists(path):
        return env
    try:
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if key:
                    env[key] = value
    except OSError as exc:
        logger.warning("Could not read .env (%s): %s", path, exc)
    return env


def _merge_section(defaults: dict, raw: dict, name: str) -> dict:
    merged = dict(defaults)
    supplied = raw.get(name)
    if supplied is None:
        return merged
    if not isinstance(supplied, dict):
        raise ConfigError(f"[{name}] must be a table")
    merged.update(supplied)
    return merged


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        raise ConfigError(f"config file not found: {path}")
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} did not parse to a table")

    cfg = {
        section: _merge_section(defaults, raw, section)
        for section, defaults in DEFAULT_CONFIG.items()
    }

    _validate_config(cfg)
    return cfg


def _validate_config(cfg: dict) -> None:
    api = cfg["api"]
    if not isinstance(api.get("url"), str) or not api["url"].strip():
        raise ConfigError("[api].url must be a non-empty string")
    timeout = api.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError("[api].timeout_seconds must be a positive number")
    api["timeout_seconds"] = float(timeout)

    schedule = cfg["schedule"]
    schedule["_interval_seconds"] = parse_interval(schedule.get("interval"))
    schedule["_retry_seconds"] = parse_interval(schedule.get("retry_delay"))
    if not isinstance(schedule.get("run_on_start"), bool):
        raise ConfigError("[schedule].run_on_start must be a boolean")

    detection = cfg["detection"]
    if detection.get("mode") not in VALID_MODES:
        raise ConfigError(
            f"[detection].mode must be one of {sorted(VALID_MODES)}, "
            f"got {detection.get('mode')!r}"
        )
    if not isinstance(detection.get("text_only"), bool):
        raise ConfigError("[detection].text_only must be a boolean")
    blacklist = detection.get("blacklist")
    if not isinstance(blacklist, list) or not all(
        isinstance(item, str) for item in blacklist
    ):
        raise ConfigError("[detection].blacklist must be a list of strings")
    fraction = detection.get("sanity_min_fraction")
    if (
        isinstance(fraction, bool)
        or not isinstance(fraction, (int, float))
        or fraction < 0
    ):
        raise ConfigError("[detection].sanity_min_fraction must be a number >= 0")
    detection["sanity_min_fraction"] = float(fraction)

    discord = cfg["discord"]
    if discord.get("message_format") not in VALID_MESSAGE_FORMATS:
        raise ConfigError(
            f"[discord].message_format must be one of "
            f"{sorted(VALID_MESSAGE_FORMATS)}, got {discord.get('message_format')!r}"
        )
    if not isinstance(discord.get("username"), str):
        raise ConfigError("[discord].username must be a string")
    if discord.get("error_notify") not in VALID_ERROR_NOTIFY:
        raise ConfigError(
            f"[discord].error_notify must be one of {sorted(VALID_ERROR_NOTIFY)}, "
            f"got {discord.get('error_notify')!r}"
        )
    for key in ("notify_recovery", "notify_on_first_run"):
        if not isinstance(discord.get(key), bool):
            raise ConfigError(f"[discord].{key} must be a boolean")
    gap = discord.get("message_gap_seconds")
    if isinstance(gap, bool) or not isinstance(gap, (int, float)) or gap < 0:
        raise ConfigError("[discord].message_gap_seconds must be a number >= 0")
    discord["message_gap_seconds"] = float(gap)

    icons = cfg["icons"]
    for key in ("new", "removed", "error", "recovery"):
        if not isinstance(icons.get(key), str):
            raise ConfigError(f"[icons].{key} must be a string")

    colors = cfg["colors"]
    for key in ("new", "removed", "error"):
        colors[key + "_int"] = parse_color(colors.get(key))


def load_templates(templates_dir: str) -> dict:
    """Read templates from disk, falling back to baked-in defaults."""
    templates = {}
    for name, default in DEFAULT_TEMPLATES.items():
        path = os.path.join(templates_dir, name)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    templates[name] = handle.read()
                continue
            except OSError as exc:
                logger.warning("Could not read template %s: %s; using default", path, exc)
        templates[name] = default
    return templates


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def is_zero_priced(pricing) -> bool:
    """True when both prompt and completion prices are exactly the string "0"."""
    if not isinstance(pricing, dict):
        return False
    return pricing.get("prompt") == "0" and pricing.get("completion") == "0"


def is_free_model(model: dict, mode: str) -> bool:
    model_id = model.get("id")
    suffix = isinstance(model_id, str) and model_id.endswith(":free")
    if mode == "suffix":
        return suffix
    zero_price = is_zero_priced(model.get("pricing"))
    if mode == "zero_price":
        return zero_price
    return zero_price or suffix


def compute_free_set(data: list, cfg: dict) -> dict:
    detection = cfg["detection"]
    mode = detection["mode"]
    text_only = detection["text_only"]
    blacklist = detection["blacklist"]
    free: dict[str, dict] = {}
    for model in data:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        if not is_free_model(model, mode):
            continue
        if any(fnmatch.fnmatch(model_id, pattern) for pattern in blacklist):
            continue
        if text_only:
            architecture = model.get("architecture")
            outputs = []
            if isinstance(architecture, dict):
                outputs = architecture.get("output_modalities") or []
            # Keep only models that output text and nothing else. NOTE: the SPEC
            # phrased this as "lacks 'text'", but the live catalog gives the
            # zero-priced Google Lyria pair output_modalities ["text","audio"],
            # which that literal test would keep. The SPEC's own "Verified API
            # facts" and the default-configuration contract require the pipeline
            # to reproduce the 16 :free IDs (Lyria pair excluded), so text_only
            # means "text-only output".
            if not outputs or any(item != "text" for item in outputs):
                continue
        free[model_id] = model
    return free


def removal_reason(model_id: str, all_by_id: dict) -> str:
    """Why an id left the free set; three-valued.

    - "no longer listed" - the id is absent from the API response.
    - "now has pricing"  - the id is still listed but is no longer zero-priced.
    - "no longer free"   - the id is still zero-priced but filtered out of the
      free set (blacklist glob or the text_only rule).
    """
    model = all_by_id.get(model_id)
    if model is None:
        return "no longer listed"
    if not is_zero_priced(model.get("pricing")):
        return "now has pricing"
    return "no longer free"


# --------------------------------------------------------------------------- #
# State / history persistence
# --------------------------------------------------------------------------- #


def new_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "models": {},
        "last_success": None,
        "failure": {
            "consecutive_failures": 0,
            "streak_started": None,
            "error_notified": False,
        },
    }


def atomic_write_json(path: str, obj) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle_fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def snapshot_fields(model: dict) -> dict:
    """The API fields copied into the stored snapshot for a free model."""
    return {
        "id": model.get("id"),
        "name": model.get("name"),
        "description": model.get("description"),
        "context_length": model.get("context_length"),
        "architecture": model.get("architecture"),
        "expiration_date": model.get("expiration_date"),
    }


def make_snapshot(model: dict, first_seen: str, notified_at: str | None) -> dict:
    snapshot = snapshot_fields(model)
    snapshot["first_seen"] = first_seen
    snapshot["notified_at"] = notified_at
    snapshot["pending_removal"] = None
    return snapshot


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def fetch_models(cfg: dict, api_key: str) -> tuple[dict, list]:
    url = cfg["api"]["url"]
    timeout = cfg["api"]["timeout_seconds"]
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive
            pass
        raise FetchError("http", str(exc.code), truncate_hard(detail, ERROR_MESSAGE_LIMIT))
    except socket.timeout as exc:
        raise FetchError(
            "timeout", "", truncate_hard(str(exc) or "request timed out", ERROR_MESSAGE_LIMIT)
        )
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise FetchError(
                "timeout", "", truncate_hard(str(reason), ERROR_MESSAGE_LIMIT)
            ) from exc
        raise FetchError(
            "http", "", truncate_hard(str(reason), ERROR_MESSAGE_LIMIT)
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive
        raise FetchError(
            "http", "", truncate_hard(f"{type(exc).__name__}: {exc}", ERROR_MESSAGE_LIMIT)
        ) from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise FetchError("parse", "", truncate_hard(body, ERROR_MESSAGE_LIMIT)) from exc
    if not isinstance(payload, dict):
        raise FetchError("parse", "", "response is not a JSON object")
    data = payload.get("data")
    if not isinstance(data, list):
        raise FetchError("parse", "", "response has no 'data' array")
    if len(data) == 0:
        raise FetchError("empty", "", "response 'data' array is empty")
    return payload, data


# --------------------------------------------------------------------------- #
# Checker
# --------------------------------------------------------------------------- #


class Checker:
    def __init__(
        self,
        cfg: dict,
        base_dir: str,
        webhook_url: str,
        api_key: str,
        stop_flag: dict,
    ):
        self.cfg = cfg
        self.base_dir = base_dir
        self.webhook_url = webhook_url
        self.api_key = api_key
        self.stop_flag = stop_flag
        self.state_path = os.path.join(base_dir, "data", "state.json")
        self.history_path = os.path.join(base_dir, "data", "history.jsonl")
        self.templates = load_templates(os.path.join(base_dir, "templates"))
        self.state: dict = {}
        self.first_run = False
        self.cycle_ts = utc_now_iso()

    # -- setup ------------------------------------------------------------- #


    def load_state(self) -> dict:
        if not os.path.exists(self.state_path):
            return new_state()
        try:
            if os.path.getsize(self.state_path) == 0:
                return new_state()
        except OSError:
            return new_state()
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(
                f"{self.state_path} is not readable JSON ({exc}); "
                "delete it to re-seed"
            ) from exc
        if not isinstance(raw, dict):
            raise StateError(f"{self.state_path} is not a JSON object; delete it to re-seed")
        raw.setdefault("schema_version", SCHEMA_VERSION)
        if not isinstance(raw.get("models"), dict):
            raw["models"] = {}
        raw.setdefault("last_success", None)
        failure = raw.get("failure")
        if not isinstance(failure, dict):
            failure = {}
        failure.setdefault("consecutive_failures", 0)
        failure.setdefault("streak_started", None)
        failure.setdefault("error_notified", False)
        raw["failure"] = failure
        return raw

    def save_state(self) -> None:
        try:
            atomic_write_json(self.state_path, self.state)
        except OSError as exc:
            logger.error("Failed to write state file %s: %s", self.state_path, exc)

    def append_history(
        self,
        event: str,
        model_id: str | None = None,
        reason: str | None = None,
        detail: str | None = None,
    ) -> None:
        record = {"ts": utc_now_iso(), "event": event}
        if model_id is not None:
            record["id"] = model_id
        if reason is not None:
            record["reason"] = reason
        if detail is not None:
            record["detail"] = detail
        try:
            os.makedirs(os.path.dirname(self.history_path) or ".", exist_ok=True)
            with open(self.history_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.error("Failed to append history: %s", exc)

    # -- main loop --------------------------------------------------------- #


    def run(self) -> None:
        self.state = self.load_state()
        self.first_run = not self.state.get("models")
        interval = self.cfg["schedule"]["_interval_seconds"]
        delay = 0.0 if self.cfg["schedule"]["run_on_start"] else float(interval)
        logger.info(
            "free-model-checker started (interval=%ss, run_on_start=%s, mode=%s)",
            interval,
            self.cfg["schedule"]["run_on_start"],
            self.cfg["detection"]["mode"],
        )
        while not self.stop_flag["flag"]:
            if delay > 0:
                if self._sleep(delay):
                    break
            if self.stop_flag["flag"]:
                break
            delay = float(self.cycle())
        logger.info("free-model-checker stopped")

    def _sleep(self, seconds: float) -> bool:
        """Sleep, waking early if a stop is requested. Returns True if stopped."""
        deadline = time.monotonic() + max(0.0, seconds)
        while not self.stop_flag["flag"]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(remaining, MESSAGE_GAP_SECONDS))
        return True

    def cycle(self) -> int:
        """Run one cycle; returns the number of seconds to sleep afterwards."""
        self.cycle_ts = utc_now_iso()
        interval = self.cfg["schedule"]["_interval_seconds"]

        try:
            payload, data = fetch_models(self.cfg, self.api_key)
        except FetchError as exc:
            return self._handle_failure(exc)

        total_count = payload.get("total_count")
        if not isinstance(total_count, int):
            total_count = len(data)

        if self._sanity_gate(len(data), total_count):
            return interval

        failure = self.state["failure"]
        if failure["consecutive_failures"] > 0:
            # Only clear the streak once recovery is actually delivered (or was
            # disabled); a failed recovery POST leaves the streak intact so the
            # next successful cycle retries it.
            if self._send_recovery():
                failure["consecutive_failures"] = 0
                failure["streak_started"] = None
                failure["error_notified"] = False

        free = compute_free_set(data, self.cfg)
        all_by_id = {
            model["id"]: model
            for model in data
            if isinstance(model, dict) and isinstance(model.get("id"), str)
        }

        if self.first_run:
            self._seed(free)
            self.first_run = False
        else:
            self._diff_and_notify(free, all_by_id)

        self.state["last_success"] = {
            "timestamp": self.cycle_ts,
            "data_len": len(data),
            "total_count": total_count,
        }
        self.save_state()
        logger.info(
            "cycle complete: %d models, %d free, %d in state",
            len(data),
            len(free),
            len(self.state["models"]),
        )
        return interval

    # -- guards ------------------------------------------------------------ #


    def _sanity_gate(self, data_len: int, total_count: int) -> bool:
        fraction = self.cfg["detection"]["sanity_min_fraction"]
        if fraction <= 0:
            return False
        last = self.state.get("last_success")
        if not isinstance(last, dict):
            return False
        prev_len = last.get("data_len")
        prev_total = last.get("total_count")
        if not isinstance(prev_len, int) or not isinstance(prev_total, int):
            return False
        if data_len < fraction * prev_len or total_count < fraction * prev_total:
            logger.warning(
                "Sanity gate tripped: got %d models (total_count=%d) vs previous "
                "%d (total_count=%d); skipping cycle, no messages sent",
                data_len,
                total_count,
                prev_len,
                prev_total,
            )
            self.append_history(
                "cycle_skipped",
                detail=f"data_len={data_len} total_count={total_count} "
                f"previous_data_len={prev_len} previous_total_count={prev_total}",
            )
            self.save_state()
            return True
        return False

    def _handle_failure(self, error: FetchError) -> int:
        failure = self.state["failure"]
        if failure["consecutive_failures"] == 0:
            failure["streak_started"] = self.cycle_ts
        failure["consecutive_failures"] += 1
        logger.warning(
            "models API failure #%d (%s %s): %s",
            failure["consecutive_failures"],
            error.error_type,
            error.error_code,
            error.error_message,
        )
        if self.cfg["discord"]["error_notify"] == "every":
            self._send_error(error)
        elif not failure["error_notified"]:
            # "once": notify on the first delivered warning of the streak. Only
            # mark it sent after a 2xx, so a failed warning is retried next cycle.
            if self._send_error(error):
                failure["error_notified"] = True
        self.append_history(
            "error",
            detail=f"{error.error_type} {error.error_code}".strip(),
        )
        self.save_state()
        return self.cfg["schedule"]["_retry_seconds"]

    # -- first run --------------------------------------------------------- #


    def _seed(self, free: dict) -> None:
        now = self.cycle_ts
        notify = self.cfg["discord"]["notify_on_first_run"]
        for model_id in sorted(free):
            self.state["models"][model_id] = make_snapshot(
                free[model_id], now, None if notify else now
            )
        self.append_history("seed", detail=f"{len(free)} models")
        logger.info(
            "first run: seeded %d free models (notify_on_first_run=%s)",
            len(free),
            notify,
        )
        if notify:
            self._send_batch(
                [("new", model_id, free[model_id]) for model_id in sorted(free)]
            )
        else:
            self.save_state()

    # -- diff -------------------------------------------------------------- #


    def _diff_and_notify(self, free: dict, all_by_id: dict) -> None:
        free_ids = set(free)
        new_items = []
        for model_id in sorted(free_ids):
            entry = self.state["models"].get(model_id)
            if entry is None or entry.get("notified_at") is None:
                new_items.append((model_id, free[model_id]))
            elif entry.get("pending_removal"):
                entry["pending_removal"] = None

        removed_items = []
        for model_id in sorted(self.state["models"]):
            if model_id in free_ids:
                continue
            entry = self.state["models"][model_id]
            reason = removal_reason(model_id, all_by_id)
            entry["pending_removal"] = reason
            removed_items.append((model_id, entry, reason))

        if removed_items:
            self.save_state()

        # One ordered batch: all new models first, then all removals, each group
        # alphabetical by id. The inter-message gap applies across the group
        # boundary too, so the whole run is spaced uniformly.
        batch = [("new", model_id, model) for model_id, model in new_items]
        batch += [
            ("removed", model_id, entry, reason)
            for model_id, entry, reason in removed_items
        ]
        self._send_batch(batch)

    # -- notification batches --------------------------------------------- #


    def _send_batch(self, items: list) -> None:
        """Send an ordered list of messages, spaced, stop-aware.

        Each item is ``("new", model_id, model)`` or
        ``("removed", model_id, entry, reason)``. The gap is slept between every
        pair of consecutive messages, including across the new/removed boundary.
        The gap is ``[discord].message_gap_seconds`` (default 1.0; 0 disables it).
        """
        gap = self.cfg["discord"]["message_gap_seconds"]
        for index, item in enumerate(items):
            if self.stop_flag["flag"]:
                return
            if item[0] == "new":
                self._send_new(item[1], item[2])
            else:
                self._send_removed(item[1], item[2], item[3])
            if index < len(items) - 1 and gap > 0:
                if self._sleep(gap):
                    return

    def _send_new(self, model_id: str, model: dict) -> bool:
        icon = self.cfg["icons"]["new"]
        context = self._model_context(model, icon)
        body = render_template(self.templates["new_model.txt"], context)
        payload = self._build_model_payload(
            model,
            icon,
            context,
            body,
            self.cfg["colors"]["new_int"],
        )
        if not self._post(payload):
            return False
        now = utc_now_iso()
        entry = self.state["models"].get(model_id)
        if entry is None:
            self.state["models"][model_id] = make_snapshot(model, now, now)
        else:
            entry.update(snapshot_fields(model))
            entry["notified_at"] = now
            entry["pending_removal"] = None
            entry.setdefault("first_seen", now)
        self.append_history("added", model_id=model_id)
        self.save_state()
        logger.info("notified: new free model %s", model_id)
        return True

    def _send_removed(self, model_id: str, entry: dict, reason: str) -> bool:
        icon = self.cfg["icons"]["removed"]
        context = self._model_context(entry, icon, reason=reason)
        body = render_template(self.templates["removed_model.txt"], context)
        payload = self._build_model_payload(
            entry,
            icon,
            context,
            body,
            self.cfg["colors"]["removed_int"],
        )
        if not self._post(payload):
            return False
        self.state["models"].pop(model_id, None)
        self.append_history("removed", model_id=model_id, reason=reason)
        self.save_state()
        logger.info("notified: %s left the free set (%s)", model_id, reason)
        return True

    # -- payload construction --------------------------------------------- #


    def _base_context(self, icon: str) -> dict:
        """The full 13-key template context, all values empty except ``icon``.

        Every send helper starts from this so all placeholders are always
        defined (``string.Template`` is rendered without strict mode); each
        caller overrides only the keys it actually uses.
        """
        return {
            "icon": icon,
            "id": "",
            "name": "",
            "context_length": "",
            "modalities": "",
            "summary": "",
            "url": "",
            "reason": "",
            "error_type": "",
            "error_code": "",
            "error_message": "",
            "downtime": "",
            "failed_cycles": "",
        }

    def _model_context(self, model: dict, icon: str, reason: str = "") -> dict:
        model_id = model.get("id") or ""
        description = model.get("description")
        if not isinstance(description, str):
            description = ""
        name = model.get("name")
        if not isinstance(name, str):
            name = ""
        context = self._base_context(icon)
        context.update(
            {
                "id": str(model_id),
                "name": escape_backticks(name),
                "context_length": format_int(model.get("context_length")),
                "modalities": format_modalities(model.get("architecture")),
                "summary": escape_backticks(
                    truncate_words(description, SUMMARY_LIMIT)
                ),
                "url": f"{MODEL_URL_PREFIX}{model_id}",
                "reason": reason,
            }
        )
        return context

    def _model_fields(self, model: dict) -> list:
        fields = []
        context_length = model.get("context_length")
        if isinstance(context_length, (int, float)) and not isinstance(context_length, bool):
            fields.append(
                {
                    "name": "Context length",
                    "value": f"{int(context_length):,} tokens",
                    "inline": True,
                }
            )
        modalities = format_modalities(model.get("architecture"))
        if modalities:
            fields.append({"name": "Modalities", "value": modalities, "inline": True})
        expiration = model.get("expiration_date")
        if expiration:
            fields.append({"name": "Free until", "value": str(expiration), "inline": True})
        return fields

    def _build_model_payload(
        self, model: dict, icon: str, context: dict, body: str, color: int
    ) -> dict:
        username = self.cfg["discord"]["username"]
        if self.cfg["discord"]["message_format"] == "plain":
            content = truncate_hard(f"{icon} {body}", PLAIN_CONTENT_LIMIT)
            return {"username": username, "content": content}
        display_name = context["name"] or context["id"]
        embed = {
            "title": truncate_hard(f"{icon} {display_name}", TITLE_LIMIT),
            "url": context["url"],
            "description": truncate_hard(body, EMBED_DESCRIPTION_LIMIT),
            "color": color,
            "timestamp": self.cycle_ts,
        }
        fields = self._model_fields(model)
        if fields:
            embed["fields"] = fields
        return {"username": username, "embeds": [embed]}

    # -- error / recovery messages ---------------------------------------- #


    def _send_error(self, error: FetchError) -> bool:
        icon = self.cfg["icons"]["error"]
        context = self._base_context(icon)
        context["error_type"] = error.error_type
        context["error_code"] = error.error_code
        context["error_message"] = error.error_message
        body = render_template(self.templates["error.txt"], context)
        payload = self._build_status_payload(
            icon,
            "OpenRouter models API error",
            body,
            self.cfg["colors"]["error_int"],
            field_value=f"{error.error_type} {error.error_code}".strip(),
        )
        return self._post(payload)

    def _send_recovery(self) -> bool:
        icon = self.cfg["icons"]["recovery"]
        failure = self.state["failure"]
        failed_cycles = failure.get("consecutive_failures", 0)
        started = parse_iso(failure.get("streak_started")) if failure.get("streak_started") else None
        if started is not None:
            now = parse_iso(self.cycle_ts) or datetime.now(timezone.utc)
            downtime = humanize_duration((now - started).total_seconds())
        else:
            downtime = humanize_duration(0)
        context = self._base_context(icon)
        context["downtime"] = downtime
        context["failed_cycles"] = str(failed_cycles)
        detail = f"downtime={downtime} failed_cycles={failed_cycles}"
        if not self.cfg["discord"]["notify_recovery"]:
            # Nothing to deliver; the streak may be closed.
            self.append_history("recovery", detail=detail)
            return True
        body = render_template(self.templates["recovery.txt"], context)
        payload = self._build_status_payload(
            icon,
            "OpenRouter models API recovered",
            body,
            self.cfg["colors"]["new_int"],
        )
        if not self._post(payload):
            return False
        self.append_history("recovery", detail=detail)
        return True

    def _build_status_payload(
        self,
        icon: str,
        title_text: str,
        body: str,
        color: int,
        field_value: str = "",
    ) -> dict:
        username = self.cfg["discord"]["username"]
        if self.cfg["discord"]["message_format"] == "plain":
            content = truncate_hard(f"{icon} {body}", PLAIN_CONTENT_LIMIT)
            return {"username": username, "content": content}
        embed = {
            "title": truncate_hard(f"{icon} {title_text}", TITLE_LIMIT),
            "description": truncate_hard(body, EMBED_DESCRIPTION_LIMIT),
            "color": color,
            "timestamp": self.cycle_ts,
        }
        if field_value:
            embed["fields"] = [{"name": "Error", "value": field_value, "inline": True}]
        return {"username": username, "embeds": [embed]}

    # -- webhook delivery -------------------------------------------------- #


    def _post(self, payload: dict) -> bool:
        for _ in range(RETRY_AFTER_ATTEMPTS):
            result = self._post_once(payload)
            if result["ok"]:
                return True
            if result["status"] == 429:
                wait = result["retry_after"]
                if wait is None:
                    wait = self.cfg["discord"]["message_gap_seconds"]
                logger.info(
                    "Discord rate limit (429); waiting %.1fs before retrying", wait
                )
                if self._sleep(wait):
                    return False
                continue
            logger.error(
                "Discord send failed (status=%s): %s", result["status"], result["error"]
            )
            return False
        logger.error(
            "Discord send failed: still HTTP 429 after %d attempts", RETRY_AFTER_ATTEMPTS
        )
        return False

    def _post_once(self, payload: dict) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.webhook_url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.cfg["api"]["timeout_seconds"]
            ) as response:
                status = getattr(response, "status", 200) or 200
                response.read()
                return {"ok": 200 <= status < 300, "status": status, "retry_after": None, "error": None}
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:  # pragma: no cover - defensive
                pass
            header = exc.headers.get("Retry-After") if exc.headers else None
            return {
                "ok": False,
                "status": exc.code,
                "retry_after": self._parse_retry_after(header, body),
                "error": body or f"HTTP {exc.code}",
            }
        except socket.timeout as exc:
            return {"ok": False, "status": None, "retry_after": None, "error": f"timeout: {exc}"}
        except urllib.error.URLError as exc:
            return {"ok": False, "status": None, "retry_after": None, "error": str(exc)}
        except Exception as exc:  # pragma: no cover - defensive
            return {
                "ok": False,
                "status": None,
                "retry_after": None,
                "error": f"{type(exc).__name__}: {exc}",
            }

    @staticmethod
    def _parse_retry_after(header, body) -> float | None:
        if header:
            try:
                return max(0.0, float(str(header).strip()))
            except ValueError:
                pass
        if body:
            try:
                parsed = json.loads(body)
                value = parsed.get("retry_after") if isinstance(parsed, dict) else None
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return max(0.0, float(value))
            except (json.JSONDecodeError, AttributeError):
                pass
        return None


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _handle_signal(signum, _frame):
    logger.info("Received signal %s; shutting down", signum)
    raise SystemExit(0)


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)


def main() -> int:
    setup_logging()
    base_dir = find_base_dir()

    env_path = os.path.join(base_dir, ".env")
    if not os.path.exists(env_path) and os.path.exists(os.path.join(APP_DIR, ".env")):
        env_path = os.path.join(APP_DIR, ".env")
    file_env = load_env_file(env_path)
    for key, value in file_env.items():
        os.environ.setdefault(key, value)
    if os.environ.get("TZ"):
        try:
            time.tzset()
        except AttributeError:  # pragma: no cover - non-POSIX
            pass

    try:
        cfg = load_config(os.path.join(base_dir, "config.toml"))
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 2

    webhook_url = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    if not webhook_url:
        logger.error(
            "DISCORD_WEBHOOK_URL is not set; add it to %s or the environment",
            env_path,
        )
        return 2
    api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()

    install_signal_handlers()
    stop_flag = {"flag": False}
    checker = Checker(cfg, base_dir, webhook_url, api_key, stop_flag)
    try:
        checker.run()
    except StateError as exc:
        logger.error("State error: %s", exc)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
