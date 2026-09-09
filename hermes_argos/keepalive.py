from __future__ import annotations

import base64
import json
import time
from datetime import datetime
from typing import Any, Callable

from .authstore import AuthStore
from .config import load_config


def _jwt_exp(token: str) -> float | None:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
        return float(claims["exp"]) if isinstance(claims, dict) and isinstance(claims.get("exp"), (int, float)) else None
    except Exception:
        return None


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _default_refresh(access: str, refresh: str, *, timeout_seconds: float = 20.0) -> dict[str, Any]:
    from hermes_cli.auth_codex import refresh_codex_oauth_pure
    return refresh_codex_oauth_pure(access, refresh, timeout_seconds=timeout_seconds)


def _due(entry: dict[str, Any], config: dict[str, Any], now: float) -> bool:
    expiry = _jwt_exp(str(entry.get("access_token") or ""))
    skew = int(config["access_refresh_skew_minutes"]) * 60
    if expiry is not None and expiry <= now + skew:
        return True
    last = _timestamp(entry.get("last_refresh"))
    return last is None or now - last >= int(config["keepalive_interval_hours"]) * 3600


def run_keepalive(
    store: AuthStore | None = None, *, force: bool = False,
    refresh_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    auth = store or AuthStore()
    config = load_config(auth.home)
    report: dict[str, Any] = {"enabled": bool(config["keepalive_enabled"]), "checked": 0, "refreshed": 0, "accounts": []}
    if not report["enabled"] and not force:
        return report
    refresher = refresh_fn or _default_refresh
    now = time.time()
    # Refresh serially: Codex refresh tokens are single-use and writes must land before the next account.
    for entry in auth.entries():
        identifier = str(entry.get("id") or "")
        label = str(entry.get("label") or identifier)
        report["checked"] += 1
        if not force and not _due(entry, config, now):
            report["accounts"].append({"id": identifier, "label": label, "status": "not_due"})
            continue
        access = str(entry.get("access_token") or "")
        refresh = str(entry.get("refresh_token") or "")
        if not refresh:
            report["accounts"].append({"id": identifier, "label": label, "status": "reauth_required", "reason": "missing refresh token"})
            continue
        try:
            updated = refresher(access, refresh, timeout_seconds=20.0)
            updated = {**updated, "old_access_token": access}
            auth.update_tokens(identifier, updated, status="ok")
            report["refreshed"] += 1
            report["accounts"].append({"id": identifier, "label": label, "status": "refreshed"})
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "").lower()
            message = str(exc).lower()
            terminal = bool(getattr(exc, "relogin_required", False)) or any(
                token in code or token in message
                for token in ("refresh_token_invalidated", "refresh_token_reused", "invalid_grant")
            )
            if terminal:
                def mark(data: dict[str, Any], account_id: str = identifier, reason: str = code or "refresh_token_invalidated") -> None:
                    entries = AuthStore._entries_in(data)
                    target = next((item for item in entries if str(item.get("id")) == account_id), None)
                    if target:
                        target.update({
                            "last_status": "dead", "last_status_at": time.time(),
                            "last_error_code": 401, "last_error_reason": reason,
                            "last_error_message": "OAuth refresh failed; reauthentication required",
                        })
                    data.setdefault("credential_pool", {})["openai-codex"] = entries
                auth.mutate(mark)
            report["accounts"].append({
                "id": identifier, "label": label,
                "status": "reauth_required" if terminal else "unavailable",
                "reason": (code or type(exc).__name__)[:120],
            })
    return report
