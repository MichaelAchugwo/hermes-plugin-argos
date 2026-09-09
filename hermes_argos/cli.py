from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .authstore import AuthStore, AuthStoreError, SelectorError
from .config import load_config, save_config
from .keepalive import run_keepalive
from .pool import status


def _fmt_pct(value: Any) -> str:
    return "—" if not isinstance(value, (int, float)) else f"{float(value):.0f}%"


def _fmt_reset(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%MZ")


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def _table(snapshot: dict[str, Any]) -> None:
    accounts = snapshot.get("accounts") or []
    if not accounts:
        print("No openai-codex OAuth accounts found. Add one with: hermes auth add openai-codex")
        return
    headers = ("#", "account", "plan", "5h left", "week left", "health", "active")
    rows = []
    for account in accounts:
        windows = account.get("usage", {}).get("windows", {}) or {}
        rows.append((
            str(account.get("index", "")), str(account.get("label") or account.get("id")),
            str(account.get("usage", {}).get("plan") or "—"),
            _fmt_pct((windows.get("five_hour") or {}).get("remaining_pct")),
            _fmt_pct((windows.get("weekly") or {}).get("remaining_pct")),
            str(account.get("health", {}).get("state") or "unknown"),
            "yes" if account.get("active") else "",
        ))
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    print("  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    print("  ".join("─" * width for width in widths))
    for row in rows:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(row))))
    mode = "on" if snapshot.get("auto_rotate") else "off"
    print(f"\nAuto-rotate: {mode} · strategy: {snapshot.get('strategy')} · threshold: {snapshot.get('threshold_pct')}%")
    if snapshot.get("all_unhealthy"):
        print("All Codex subscriptions are currently unhealthy; Hermes fallback_model remains authoritative.")


def _list(store: AuthStore, as_json: bool) -> int:
    entries = store.public_entries()
    if as_json:
        _print_json({"accounts": entries})
        return 0
    if not entries:
        print("No openai-codex OAuth accounts found. Add one with: hermes auth add openai-codex")
        return 1
    print("OpenAI Codex subscriptions (fingerprints are non-secret):")
    for entry in entries:
        marker = "*" if entry["active"] else " "
        print(f"{marker} {entry['index']}. {entry['label']}  id={entry['id']}  fp={entry['fingerprint']}  priority={entry['priority']}")
    return 0


def _use(store: AuthStore, selector: str, as_json: bool) -> int:
    selected = store.activate(selector)
    public = next(entry for entry in store.public_entries() if entry["id"] == selected["id"])
    if as_json:
        _print_json({"active": public})
    else:
        print(f"Active Codex subscription: {public['label']} ({public['fingerprint']})")
    return 0


def _next(store: AuthStore, as_json: bool) -> int:
    entries = store.public_entries()
    if not entries:
        print("No openai-codex OAuth accounts found.")
        return 1
    current = next((index for index, item in enumerate(entries) if item["active"]), -1)
    return _use(store, str((current + 1) % len(entries) + 1), as_json)


def _doctor(store: AuthStore, as_json: bool) -> int:
    issues: list[str] = []
    warnings: list[str] = []
    try:
        issues.extend(store.validate())
    except AuthStoreError as exc:
        issues.append(str(exc))
    entries = store.public_entries() if not issues else []
    if not entries:
        warnings.append("No pooled openai-codex credentials; run `hermes auth add openai-codex` for each subscription.")
    if len(entries) < 2:
        warnings.append("Automatic failover needs at least two accounts; one account is currently available.")
    root = store.home / "plugins" / "argos"
    for relative in ("plugin.yaml", "dashboard/manifest.json", "dashboard/plugin_api.py", "desktop/plugin.js"):
        if not (root / relative).exists():
            issues.append(f"Missing {relative}")
    payload = {
        "ok": not issues,
        "auth_path": str(store.path),
        "account_count": len(entries),
        "active_count": sum(bool(item["active"]) for item in entries),
        "issues": issues,
        "warnings": warnings,
        "native_same_turn_rotation": True,
        "note": "Hermes core handles usage_limit_reached immediately, generic 429 after one retry, and 401 refresh/rotation.",
    }
    if as_json:
        _print_json(payload)
    else:
        print("Codex subscriptions doctor:")
        print(f"  auth store: {payload['auth_path']}")
        print(f"  accounts: {payload['account_count']} · active: {payload['active_count']}")
        print(f"  core same-turn recovery: available")
        for warning in warnings:
            print(f"  warning: {warning}")
        for issue in issues:
            print(f"  error: {issue}")
        print("  result: OK" if not issues else "  result: FAILED")
    return 0 if not issues else 1


def register_cli(parser: argparse.ArgumentParser) -> None:
    parser.description = "Manage pooled ChatGPT/Codex OAuth subscriptions"
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    subs = parser.add_subparsers(dest="subs_action")
    subs.add_parser("list", help="List subscriptions without contacting OpenAI")
    subs.add_parser("current", help="Show the active subscription")
    use = subs.add_parser("use", help="Activate by index, id, label, or fingerprint prefix")
    use.add_argument("selector")
    subs.add_parser("next", help="Activate the next subscription")
    subs.add_parser("status", help="Show quota and health for every subscription")
    refresh = subs.add_parser("refresh", help="Force a live quota refresh")
    refresh.add_argument("--no-policy", action="store_true", help="Do not reorder/rotate after refresh")
    keepalive = subs.add_parser("keepalive", help="Refresh OAuth tokens conservatively")
    keepalive.add_argument("--once", action="store_true", required=True)
    subs.add_parser("doctor", help="Validate auth store and plugin surfaces")
    auto = subs.add_parser("auto", help="Enable or disable automatic policy")
    auto.add_argument("mode", choices=("on", "off"))
    parser.set_defaults(func=command)


def command(args: argparse.Namespace) -> int:
    store = AuthStore()
    action = getattr(args, "subs_action", None) or "status"
    as_json = bool(getattr(args, "json", False))
    try:
        if action == "list":
            return _list(store, as_json)
        if action == "current":
            current = next((entry for entry in store.public_entries() if entry["active"]), None)
            if as_json:
                _print_json({"active": current})
            else:
                print(current["label"] if current else "No active Codex subscription")
            return 0 if current else 1
        if action == "use":
            return _use(store, args.selector, as_json)
        if action == "next":
            return _next(store, as_json)
        if action in {"status", "refresh"}:
            snapshot = status(store, force=action == "refresh", apply_policy=not getattr(args, "no_policy", False))
            _print_json(snapshot) if as_json else _table(snapshot)
            return 2 if snapshot.get("all_unhealthy") else 0
        if action == "keepalive":
            report = run_keepalive(store)
            _print_json(report) if as_json else print(
                f"Keepalive checked {report['checked']} account(s); refreshed {report['refreshed']}."
            )
            return 0
        if action == "doctor":
            return _doctor(store, as_json)
        if action == "auto":
            config = load_config(store.home)
            config["auto_rotate"] = args.mode == "on"
            save_config(config, store.home)
            result = {"auto_rotate": config["auto_rotate"]}
            _print_json(result) if as_json else print(f"Auto-rotate {args.mode}.")
            return 0
    except (AuthStoreError, SelectorError, ValueError) as exc:
        if as_json:
            _print_json({"ok": False, "error": str(exc)})
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 2
