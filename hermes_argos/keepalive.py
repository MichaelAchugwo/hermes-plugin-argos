from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .authstore import AuthStore
from .config import load_config

_KEEPALIVE_LOCK_NAME = "argos-keepalive.lock"
# A refresh cycle may POST once per account with ~20s timeouts; anything older
# than this is a crashed run, not an active one.
_KEEPALIVE_LOCK_STALE_SECONDS = 900.0


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


def _acquire_refresh_lock(home: Path, *, stale_after: float = _KEEPALIVE_LOCK_STALE_SECONDS) -> Path | None:
    """Single-flight guard: at most one process may POST refresh tokens.

    Codex refresh tokens are single-use. Two processes replaying the same token
    trip OpenAI's reuse detection, which invalidates the entire token family —
    a failure the account cannot recover from without a fresh login. Concurrent
    refreshes are therefore prevented, not merely tolerated.
    """
    path = Path(home) / _KEEPALIVE_LOCK_NAME
    for attempt in (0, 1):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if attempt:
                return None
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                return None
            if age <= stale_after:
                return None
            try:
                path.unlink()
            except OSError:
                return None
        except OSError:
            return None
        else:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(str(os.getpid()))
            return path
    return None


def _release_refresh_lock(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _peer_rotated(auth: AuthStore, identifier: str, attempted_refresh: str) -> bool:
    """Detect a refresh race: the store now holds tokens another process rotated."""
    try:
        current = auth.read()
    except Exception:
        return False
    entry = next((e for e in AuthStore._entries_in(current) if str(e.get("id")) == identifier), None)
    stored = str((entry or {}).get("refresh_token") or "")
    return bool(stored) and stored != attempted_refresh


def run_keepalive(
    store: AuthStore | None = None, *, force: bool = False,
    refresh_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    auth = store or AuthStore()
    config = load_config(auth.home)
    report: dict[str, Any] = {"enabled": bool(config["keepalive_enabled"]), "checked": 0, "refreshed": 0, "accounts": []}
    if not report["enabled"] and not force:
        return report
    lock = _acquire_refresh_lock(auth.home)
    if lock is None:
        report["skipped"] = "another keepalive run is in progress"
        return report
    try:
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
                if terminal and _peer_rotated(auth, identifier, refresh):
                    # Another process consumed the single-use token and persisted
                    # the successor pair; adopt instead of condemning the account.
                    report["accounts"].append({
                        "id": identifier, "label": label, "status": "adopted",
                        "reason": "another process rotated the token family first",
                    })
                    continue
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
    finally:
        _release_refresh_lock(lock)
