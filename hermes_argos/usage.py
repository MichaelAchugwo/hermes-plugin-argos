from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .authstore import AuthStore, fingerprint
from .config import home_path, load_config

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _reset_at(window: dict[str, Any], now: float) -> float | None:
    direct = _number(window.get("reset_at") if window.get("reset_at") is not None else window.get("expires_at"))
    if direct is not None:
        return direct / 1000.0 if direct > 1_000_000_000_000 else direct
    after = _number(window.get("reset_after_seconds"))
    return now + after if after is not None else None


def _window(raw: dict[str, Any], now: float) -> dict[str, Any] | None:
    used = _number(raw.get("used_percent"))
    if used is None:
        return None
    return {
        "used_pct": max(0.0, min(100.0, used)),
        "remaining_pct": max(0.0, min(100.0, 100.0 - used)),
        "reset_at": _reset_at(raw, now),
    }


def _plan_name(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).replace("-", " ").replace("_", " ").title()
    return text.replace("Chatgpt", "ChatGPT")


def parse_usage_payload(payload: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    timestamp = float(now if now is not None else time.time())
    rate = payload.get("rate_limit") if isinstance(payload.get("rate_limit"), dict) else {}
    primary = rate.get("primary_window") if isinstance(rate.get("primary_window"), dict) else {}
    secondary = rate.get("secondary_window") if isinstance(rate.get("secondary_window"), dict) else {}
    resets = payload.get("rate_limit_reset_credits") if isinstance(payload.get("rate_limit_reset_credits"), dict) else {}
    available_count = resets.get("available_count")
    result = {
        "available": True,
        "fetched_at": timestamp,
        "plan": _plan_name(payload.get("plan_type") or payload.get("plan")),
        "windows": {
            "five_hour": _window(primary, timestamp),
            "weekly": _window(secondary, timestamp),
        },
        "banked_resets": {
            "available_count": int(available_count) if isinstance(available_count, (int, float)) else 0,
            "expires_at": _reset_at(resets, timestamp),
        },
    }
    credits = payload.get("credits") if isinstance(payload.get("credits"), dict) else {}
    if credits:
        result["credits"] = {
            "has_credits": bool(credits.get("has_credits")),
            "unlimited": bool(credits.get("unlimited")),
            "balance": credits.get("balance") if isinstance(credits.get("balance"), (int, float)) else None,
        }
    return result


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        value = json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _account_id(token: str) -> str | None:
    auth = _jwt_claims(token).get("https://api.openai.com/auth")
    value = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    return str(value).strip() if value else None


def _safe_usage_url(url: str = USAGE_URL) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "chatgpt.com":
        raise ValueError("Codex OAuth tokens may only be sent to https://chatgpt.com")
    return url


def fetch_entry_usage(entry: dict[str, Any], *, timeout: float = 15.0, opener=urlopen) -> dict[str, Any]:
    token = str(entry.get("access_token") or "")
    if not token:
        return {"available": False, "reason": "missing access token", "fetched_at": time.time()}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "hermes-argos/1.0",
        "originator": "hermes",
    }
    account_id = _account_id(token)
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    request = Request(_safe_usage_url(), headers=headers, method="GET")
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("usage response was not an object")
        return parse_usage_payload(payload)
    except HTTPError as exc:
        return {"available": False, "reason": f"HTTP {exc.code}", "status_code": exc.code, "fetched_at": time.time()}
    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {"available": False, "reason": str(exc)[:240], "fetched_at": time.time()}


class UsageCache:
    def __init__(self, home: Path | str | None = None):
        self.path = home_path(home) / "cache" / "argos" / "usage.json"

    def read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="usage.json.tmp.", dir=str(self.path.parent))
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)


def fetch_all_usage(
    store: AuthStore | None = None, *, force: bool = False, fetcher=fetch_entry_usage,
) -> dict[str, dict[str, Any]]:
    auth = store or AuthStore()
    entries = auth.entries()
    cache = UsageCache(auth.home)
    cached = cache.read()
    max_age = int(load_config(auth.home)["usage_cache_seconds"])
    now = time.time()
    results: dict[str, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    for entry in entries:
        key = fingerprint(entry.get("refresh_token") or entry.get("access_token"))
        old = cached.get(key) if isinstance(cached.get(key), dict) else None
        fetched_at = _number(old.get("fetched_at")) if old else None
        if not force and fetched_at is not None and now - fetched_at < max_age:
            results[str(entry.get("id"))] = old
        else:
            pending.append(entry)
    if pending:
        with ThreadPoolExecutor(max_workers=min(4, len(pending))) as executor:
            jobs = {executor.submit(fetcher, entry): entry for entry in pending}
            for job in as_completed(jobs):
                entry = jobs[job]
                try:
                    result = job.result()
                except Exception as exc:
                    result = {"available": False, "reason": str(exc)[:240], "fetched_at": time.time()}
                results[str(entry.get("id"))] = result
    disk = dict(cached)
    for entry in entries:
        identifier = str(entry.get("id"))
        if identifier in results:
            disk[fingerprint(entry.get("refresh_token") or entry.get("access_token"))] = results[identifier]
    cache.write(disk)
    return results
