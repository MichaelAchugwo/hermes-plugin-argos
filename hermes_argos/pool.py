from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from .authstore import AuthStore
from .config import load_config
from .keepalive import _access_usable
from .usage import fetch_all_usage


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if float(value) > 1_000_000_000_000 else float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    return None


def heal_premature_dead(store: AuthStore, *, now: float | None = None) -> bool:
    """Restore accounts whose refresh chain died but whose access token still works.

    Stale processes or core rewrites can mark such an account ``dead``; dead
    excludes a credential from rotation, so a usable account must be restored to
    ``ok`` (with the refresh_required marker intact) instead of being lost.
    """
    timestamp = float(now if now is not None else time.time())
    target_ids = [
        str(entry.get("id"))
        for entry in store.entries()
        if str(entry.get("last_status") or "").lower() == "dead"
        and _access_usable(str(entry.get("access_token") or ""), now=timestamp)
    ]
    if not target_ids:
        return False

    def apply(data: dict[str, Any]) -> None:
        entries = AuthStore._entries_in(data)
        for entry in entries:
            if str(entry.get("id")) not in target_ids:
                continue
            reason = str(entry.get("last_error_reason") or "refresh_token_invalidated")
            for key in ("last_error_code", "last_error_reason", "last_error_message", "last_error_reset_at", "failure_reason"):
                entry.pop(key, None)
            entry.update({
                "last_status": "ok", "last_status_at": timestamp,
                "refresh_required": True, "refresh_required_reason": reason,
                "refresh_required_at": timestamp,
                "last_refresh": datetime.now(timezone.utc).isoformat(),
            })
        data.setdefault("credential_pool", {})["openai-codex"] = entries

    store.mutate(apply)
    return True


def _with_refresh_marker(entry: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Surface a dead refresh chain without blocking an otherwise usable account."""
    if entry.get("refresh_required"):
        state["refresh_required"] = True
        if state.get("reason") is None:
            state["reason"] = "refresh chain requires re-login; account remains usable until access token expiry"
    return state


def health_for_usage(entry: dict[str, Any], usage: dict[str, Any], *, threshold: float, now: float | None = None) -> dict[str, Any]:
    timestamp = float(now if now is not None else time.time())
    status = str(entry.get("last_status") or "").lower()
    reason = str(entry.get("last_error_reason") or "").lower()
    if status == "dead" or "refresh_token_invalidated" in reason or "invalid_grant" in reason:
        return _with_refresh_marker(entry, {"state": "reauth", "healthy": False, "reason": "OAuth reauthentication required"})
    reset = _epoch(entry.get("last_error_reset_at"))
    if status == "exhausted" and (reset is None or reset > timestamp):
        return _with_refresh_marker(entry, {"state": "limited", "healthy": False, "reason": "cooldown active", "reset_at": reset})
    if not usage.get("available"):
        return _with_refresh_marker(entry, {"state": "unavailable", "healthy": False, "reason": usage.get("reason") or "usage unavailable"})
    windows = usage.get("windows") if isinstance(usage.get("windows"), dict) else {}
    remaining = [
        float(window["remaining_pct"])
        for window in windows.values()
        if isinstance(window, dict) and isinstance(window.get("remaining_pct"), (int, float))
    ]
    if remaining and min(remaining) <= float(threshold):
        reset_times = [window.get("reset_at") for window in windows.values() if isinstance(window, dict) and window.get("reset_at")]
        return _with_refresh_marker(entry, {
            "state": "limited", "healthy": False,
            "reason": f"remaining capacity at or below {threshold:g}%",
            "reset_at": min(reset_times) if reset_times else None,
        })
    return _with_refresh_marker(entry, {"state": "ok", "healthy": True, "reason": None})


def _weekly_remaining(account: dict[str, Any]) -> float:
    usage = account.get("usage") if isinstance(account.get("usage"), dict) else {}
    windows = usage.get("windows") if isinstance(usage.get("windows"), dict) else {}
    weekly = windows.get("weekly") if isinstance(windows.get("weekly"), dict) else {}
    value = weekly.get("remaining_pct")
    return float(value) if isinstance(value, (int, float)) else 101.0


def choose_account(accounts: list[dict[str, Any]], strategy: str, *, current_id: str | None = None) -> dict[str, Any] | None:
    healthy = [account for account in accounts if (account.get("health") or {}).get("healthy")]
    if not healthy:
        return None
    ordered = sorted(healthy, key=lambda account: (int(account.get("priority", 999999)), str(account.get("id", ""))))
    if strategy == "least_weekly_remaining":
        return min(ordered, key=lambda account: (_weekly_remaining(account), int(account.get("priority", 999999))))
    if strategy == "round_robin" and len(ordered) > 1:
        ids = [str(account.get("id")) for account in ordered]
        if current_id in ids:
            return ordered[(ids.index(str(current_id)) + 1) % len(ordered)]
    return ordered[0]


def _capacity(usage: dict[str, Any], threshold: float) -> tuple[bool | None, float | None]:
    if not usage.get("available"):
        return None, None
    windows = usage.get("windows") if isinstance(usage.get("windows"), dict) else {}
    depleted: list[dict[str, Any]] = []
    for window in windows.values():
        if isinstance(window, dict) and isinstance(window.get("remaining_pct"), (int, float)):
            if float(window["remaining_pct"]) <= threshold:
                depleted.append(window)
    resets = [float(window["reset_at"]) for window in depleted if isinstance(window.get("reset_at"), (int, float))]
    return bool(depleted), max(resets) if resets else None


def sync_usage_health(
    store: AuthStore, usage_by_id: dict[str, dict[str, Any]], *, threshold: float, now: float | None = None,
) -> bool:
    """Persist quota exhaustion from authoritative usage snapshots.

    A restored snapshot clears rate-limit exhaustion, but never a terminal
    OAuth (`dead`) state. The latest reset among depleted windows is used so an
    account with both windows empty cannot re-enter after only the first resets.
    """
    timestamp = float(now if now is not None else time.time())
    current = store.entries()
    desired: dict[str, tuple[str, float | None] | None] = {}
    changed = False
    for entry in current:
        identifier = str(entry.get("id") or "")
        depleted, reset = _capacity(usage_by_id.get(identifier, {}), float(threshold))
        if depleted is None or str(entry.get("last_status") or "").lower() == "dead":
            continue
        if depleted:
            desired[identifier] = ("exhausted", reset)
            changed = changed or not (
                entry.get("last_status") == "exhausted"
                and entry.get("last_error_reason") == "usage_limit_reached"
                and _epoch(entry.get("last_error_reset_at")) == reset
            )
        elif entry.get("last_status") == "exhausted":
            desired[identifier] = ("ok", None)
            changed = True
    if not changed:
        return False

    def apply(data: dict[str, Any]) -> None:
        entries = AuthStore._entries_in(data)
        for entry in entries:
            state = desired.get(str(entry.get("id") or ""))
            if state is None or str(entry.get("last_status") or "").lower() == "dead":
                continue
            status_name, reset = state
            if status_name == "exhausted":
                entry.update({
                    "last_status": "exhausted", "last_status_at": timestamp,
                    "last_error_code": 429, "last_error_reason": "usage_limit_reached",
                    "last_error_message": "Codex usage window is at or below the configured threshold",
                    "last_error_reset_at": reset,
                })
            else:
                for key in ("last_error_code", "last_error_reason", "last_error_message", "last_error_reset_at", "failure_reason"):
                    entry.pop(key, None)
                entry.update({"last_status": "ok", "last_status_at": timestamp})
        data.setdefault("credential_pool", {})["openai-codex"] = entries

    store.mutate(apply)
    return True


def status(store: AuthStore | None = None, *, force: bool = False, apply_policy: bool = True) -> dict[str, Any]:
    auth = store or AuthStore()
    config = load_config(auth.home)
    if apply_policy:
        heal_premature_dead(auth)
    public = auth.public_entries()
    raw_by_id = {str(entry.get("id")): entry for entry in auth.entries()}
    usage_by_id = fetch_all_usage(auth, force=force)
    if apply_policy and sync_usage_health(
        auth, usage_by_id, threshold=float(config["empty_threshold_pct"]),
    ):
        public = auth.public_entries()
        raw_by_id = {str(entry.get("id")): entry for entry in auth.entries()}
    accounts: list[dict[str, Any]] = []
    for entry in public:
        identifier = str(entry["id"])
        usage = usage_by_id.get(identifier, {"available": False, "reason": "not fetched"})
        health = health_for_usage(raw_by_id.get(identifier, {}), usage, threshold=float(config["empty_threshold_pct"]))
        accounts.append({**entry, "usage": usage, "health": health})
    current = next((account for account in accounts if account.get("active")), None)
    recommended = choose_account(accounts, str(config["strategy"]), current_id=str(current.get("id")) if current else None)
    rotated = False
    if apply_policy and config["auto_rotate"] and recommended and (
        current is None
        or not current["health"]["healthy"]
        or (str(config["strategy"]) == "least_weekly_remaining" and current.get("id") != recommended.get("id"))
    ):
        auth.activate(str(recommended["id"]), clear_if_healthy=True)
        rotated = True
        for account in accounts:
            account["active"] = account["id"] == recommended["id"]
        current = recommended
    if apply_policy and config["auto_rotate"] and str(config["strategy"]) == "least_weekly_remaining":
        ranked = sorted(
            [account for account in accounts if account["health"]["healthy"]],
            key=lambda account: (_weekly_remaining(account), int(account.get("priority", 999999))),
        )
        unhealthy = [account for account in accounts if not account["health"]["healthy"]]
        wanted = [str(account["id"]) for account in ranked + unhealthy]
        if wanted and wanted != [str(account["id"]) for account in sorted(accounts, key=lambda a: int(a.get("priority", 999999)))]:
            auth.reorder(wanted)
            for priority, identifier in enumerate(wanted):
                next(account for account in accounts if str(account["id"]) == identifier)["priority"] = priority
    worst = None
    if current:
        values = [
            window.get("remaining_pct")
            for window in (current.get("usage", {}).get("windows", {}) or {}).values()
            if isinstance(window, dict) and isinstance(window.get("remaining_pct"), (int, float))
        ]
        worst = min(values) if values else None
    return {
        "provider": "openai-codex",
        "auto_rotate": bool(config["auto_rotate"]),
        "strategy": config["strategy"],
        "threshold_pct": config["empty_threshold_pct"],
        "account_count": len(accounts),
        "active_id": current.get("id") if current else None,
        "active_worst_remaining_pct": worst,
        "recommended_id": recommended.get("id") if recommended else None,
        "rotated": rotated,
        "all_unhealthy": bool(accounts) and not any(account["health"]["healthy"] for account in accounts),
        "accounts": accounts,
    }
