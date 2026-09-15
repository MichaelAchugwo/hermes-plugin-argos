from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_argos.authstore import AuthStore, DuplicateCredentialError, _FileLock
from hermes_argos.keepalive import run_keepalive
from hermes_argos.config import load_config

from hermes_argos.pool import choose_account, heal_premature_dead, health_for_usage, status, sync_usage_health
from hermes_argos.usage import parse_usage_payload


def entry(identifier: str, priority: int, refresh: str, access: str | None = None) -> dict:
    return {
        "id": identifier,
        "label": identifier,
        "priority": priority,
        "auth_type": "oauth",
        "source": "manual:device_code",
        "access_token": access or f"access-{identifier}",
        "refresh_token": refresh,
    }


def seed(path: Path, entries: list[dict]) -> None:
    path.write_text(json.dumps({
        "version": 2,
        "unrelated": {"keep": True},
        "providers": {"other": {"key": "untouched"}, "openai-codex": {"tokens": {}}},
        "credential_pool": {"other": [{"id": "x"}], "openai-codex": entries},
    }), encoding="utf-8")


def test_activate_is_atomic_scoped_and_backed_up(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    seed(auth, [entry("work", 0, "refresh-work"), entry("reserve", 1, "refresh-reserve")])
    seeded = json.loads(auth.read_text(encoding="utf-8"))
    seeded["providers"]["openai-codex"]["future_metadata"] = {"keep": True}
    auth.write_text(json.dumps(seeded), encoding="utf-8")
    store = AuthStore(tmp_path)

    selected = store.activate("reserve")

    data = json.loads(auth.read_text(encoding="utf-8"))
    assert selected["id"] == "reserve"
    assert data["unrelated"] == {"keep": True}
    assert data["providers"]["other"] == {"key": "untouched"}
    assert data["providers"]["openai-codex"]["future_metadata"] == {"keep": True}
    assert data["credential_pool"]["other"] == [{"id": "x"}]
    assert data["providers"]["openai-codex"]["tokens"]["access_token"] == "access-reserve"
    assert [(e["id"], e["priority"]) for e in data["credential_pool"]["openai-codex"]] == [
        ("reserve", 0), ("work", 1)
    ]
    backups = list((tmp_path / "backups" / "argos").glob("auth-*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8"))["unrelated"] == {"keep": True}


def test_selector_accepts_index_id_label_and_fingerprint_prefix(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    seed(auth, [entry("id-a", 0, "refresh-a"), {**entry("id-b", 1, "refresh-b"), "label": "Work"}])
    store = AuthStore(tmp_path)
    fp = store.public_entries()[1]["fingerprint"]

    assert store.resolve("2")["id"] == "id-b"
    assert store.resolve("id-b")["id"] == "id-b"
    assert store.resolve("work")["id"] == "id-b"
    assert store.resolve(fp[:8])["id"] == "id-b"


def test_duplicate_refresh_tokens_are_rejected(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    seed(auth, [entry("a", 0, "same"), entry("b", 1, "same")])
    with pytest.raises(DuplicateCredentialError):
        AuthStore(tmp_path).validate()


def test_mutation_outside_codex_sections_is_rejected_without_writing(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    seed(auth, [entry("a", 0, "r-a")])
    original = auth.read_text(encoding="utf-8")

    with pytest.raises(Exception, match="outside openai-codex"):
        AuthStore(tmp_path).mutate(lambda data: data["providers"]["other"].update({"key": "changed"}))

    assert auth.read_text(encoding="utf-8") == original


def test_backup_retention_keeps_twenty(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    seed(auth, [entry("a", 0, "r-a"), entry("b", 1, "r-b")])
    store = AuthStore(tmp_path)
    for i in range(24):
        store.activate("a" if i % 2 else "b")
    assert len(list((tmp_path / "backups" / "argos").glob("auth-*.json"))) == 20


def test_usage_parser_converts_used_to_remaining_and_resets() -> None:
    now = 1_700_000_000
    parsed = parse_usage_payload({
        "plan_type": "chatgpt_plus",
        "rate_limit": {
            "primary_window": {"used_percent": 77, "reset_after_seconds": 60},
            "secondary_window": {"used_percent": 12.5, "reset_at": now + 7200},
        },
        "rate_limit_reset_credits": {"available_count": 3, "expires_at": now + 86400},
    }, now=now)
    assert parsed["plan"] == "ChatGPT Plus"
    assert parsed["windows"]["five_hour"]["remaining_pct"] == 23.0
    assert parsed["windows"]["five_hour"]["reset_at"] == now + 60
    assert parsed["windows"]["weekly"]["remaining_pct"] == 87.5
    assert parsed["banked_resets"] == {"available_count": 3, "expires_at": now + 86400}


def test_health_rejects_empty_cooldown_and_reauth() -> None:
    usage = {"available": True, "windows": {"five_hour": {"remaining_pct": 1}, "weekly": {"remaining_pct": 80}}}
    assert health_for_usage({}, usage, threshold=1, now=100)["state"] == "limited"
    assert health_for_usage({"last_status": "dead"}, usage, threshold=1, now=100)["state"] == "reauth"
    assert health_for_usage({"last_status": "exhausted", "last_error_reset_at": 200}, usage, threshold=1, now=100)["state"] == "limited"


def test_health_reports_refresh_required_marker() -> None:
    usage = {"available": True, "windows": {"five_hour": {"remaining_pct": 80}}}
    state = health_for_usage({"refresh_required": True}, usage, threshold=1, now=100)
    assert state["healthy"] is True
    assert state["refresh_required"] is True
    assert "re-login" in str(state.get("reason"))


def test_usage_health_is_persisted_until_limiting_window_reset(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    seed(auth, [entry("a", 0, "r-a")])
    store = AuthStore(tmp_path)
    limited = {"a": {"available": True, "windows": {
        "five_hour": {"remaining_pct": 1, "reset_at": 150},
        "weekly": {"remaining_pct": 0, "reset_at": 900},
    }}}

    assert sync_usage_health(store, limited, threshold=1, now=100) is True
    marked = store.entries()[0]
    assert marked["last_status"] == "exhausted"
    assert marked["last_error_reason"] == "usage_limit_reached"
    assert marked["last_error_reset_at"] == 900

    restored = {"a": {"available": True, "windows": {
        "five_hour": {"remaining_pct": 100, "reset_at": 1000},
        "weekly": {"remaining_pct": 50, "reset_at": 2000},
    }}}
    assert sync_usage_health(store, restored, threshold=1, now=901) is True
    assert store.entries()[0]["last_status"] == "ok"


def test_least_weekly_remaining_spends_nearly_empty_healthy_first() -> None:
    accounts = [
        {"id": "full", "priority": 0, "health": {"healthy": True}, "usage": {"windows": {"weekly": {"remaining_pct": 90}}}},
        {"id": "near", "priority": 1, "health": {"healthy": True}, "usage": {"windows": {"weekly": {"remaining_pct": 8}}}},
        {"id": "dead", "priority": 2, "health": {"healthy": False}, "usage": {"windows": {"weekly": {"remaining_pct": 2}}}},
    ]
    assert choose_account(accounts, "least_weekly_remaining", current_id="full")["id"] == "near"
    assert choose_account(accounts, "fill_first", current_id="full")["id"] == "full"
    assert choose_account(accounts, "round_robin", current_id="full")["id"] == "near"


def test_keepalive_persists_rotated_tokens_and_updates_active_singleton(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    seed(auth, [entry("active", 0, "refresh-old", "access-old")])
    data = json.loads(auth.read_text(encoding="utf-8"))
    data["providers"]["openai-codex"]["tokens"] = {
        "access_token": "access-old", "refresh_token": "refresh-old"
    }
    auth.write_text(json.dumps(data), encoding="utf-8")

    report = run_keepalive(
        AuthStore(tmp_path), force=True,
        refresh_fn=lambda access, refresh, timeout_seconds=20: {
            "access_token": "access-new", "refresh_token": "refresh-new", "last_refresh": "2026-01-01T00:00:00Z"
        },
    )

    updated = json.loads(auth.read_text(encoding="utf-8"))
    assert report["refreshed"] == 1
    assert updated["credential_pool"]["openai-codex"][0]["refresh_token"] == "refresh-new"
    assert updated["providers"]["openai-codex"]["tokens"]["access_token"] == "access-new"


def test_config_allows_five_second_quota_polling(tmp_path: Path) -> None:
    (tmp_path / "argos.yaml").write_text(
        "argos:\n  usage_cache_seconds: 5\n  usage_poll_seconds: 5\n",
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config["usage_cache_seconds"] == 5
    assert config["usage_poll_seconds"] == 5


def test_weekly_only_plan_remaps_long_primary_window_to_weekly() -> None:
    now = 1_700_000_000
    parsed = parse_usage_payload({
        "plan_type": "chatgpt_plus",
        "rate_limit": {
            "primary_window": {"used_percent": 0, "reset_after_seconds": 586800},
        },
    }, now=now)
    assert parsed["windows"]["five_hour"] is None
    assert parsed["windows"]["weekly"]["remaining_pct"] == 100.0
    assert parsed["windows"]["weekly"]["reset_at"] == now + 586800


class ReusedRefreshError(Exception):
    code = "refresh_token_reused"


def jwt_with_exp(offset_seconds: float) -> str:
    import base64 as _b64
    import json as _json
    import time as _time
    payload = _b64.urlsafe_b64encode(
        _json.dumps({"exp": _time.time() + offset_seconds}).encode()
    ).rstrip(b"=").decode()
    return f"header.{payload}.sig"


def test_keepalive_degrades_to_refresh_required_when_access_token_still_usable(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    seed(auth, [entry("acct", 0, "refresh-stale", jwt_with_exp(9 * 24 * 3600))])
    store = AuthStore(tmp_path)

    def failing(access: str, refresh: str, *, timeout_seconds: float = 20.0):
        raise ReusedRefreshError("refresh_token_reused")

    report = run_keepalive(store, force=True, refresh_fn=failing)

    after = store.entries()[0]
    assert report["accounts"][0]["status"] == "refresh_required"
    assert after["last_status"] == "ok"
    assert after["refresh_required"] is True
    assert after["refresh_required_reason"] == "refresh_token_reused"


def test_keepalive_marks_dead_only_when_access_token_expired(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    seed(auth, [entry("acct", 0, "refresh-stale", jwt_with_exp(-3600))])
    store = AuthStore(tmp_path)

    def failing(access: str, refresh: str, *, timeout_seconds: float = 20.0):
        raise ReusedRefreshError("refresh_token_reused")

    report = run_keepalive(store, force=True, refresh_fn=failing)

    assert report["accounts"][0]["status"] == "reauth_required"
    assert store.entries()[0]["last_status"] == "dead"


def test_keepalive_skips_refresh_required_entries_without_force(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    seed(auth, [entry("acct", 0, "refresh-stale", "access-stale")])
    store = AuthStore(tmp_path)

    def mark(data: dict) -> None:
        entries = AuthStore._entries_in(data)
        entries[0]["refresh_required"] = True
        entries[0]["last_refresh"] = "2026-01-01T00:00:00Z"
        data.setdefault("credential_pool", {})["openai-codex"] = entries

    store.mutate(mark)
    calls: list[int] = []

    report = run_keepalive(store, force=False, refresh_fn=lambda *a, **k: calls.append(1) or {})

    assert calls == []
    assert report["accounts"][0]["status"] == "refresh_required"


def test_keepalive_adopts_peer_rotated_tokens_instead_of_marking_dead(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    seed(auth, [entry("acct", 0, "refresh-old", "access-old")])
    store = AuthStore(tmp_path)

    def racer(access: str, refresh: str, *, timeout_seconds: float = 20.0):
        # Simulate another process rotating the tokens between our read and our POST.
        store.update_tokens("acct", {
            "access_token": "access-peer", "refresh_token": "refresh-peer",
            "last_refresh": "2026-01-02T00:00:00Z",
        })
        raise ReusedRefreshError("refresh_token_reused")

    report = run_keepalive(store, force=True, refresh_fn=racer)

    after = store.entries()[0]
    assert after["refresh_token"] == "refresh-peer"
    assert after.get("last_status") != "dead"
    assert report["accounts"][0]["status"] == "adopted"
    assert report["refreshed"] == 0


def test_keepalive_success_clears_stale_error_fields(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    seed(auth, [entry("acct", 0, "refresh-old", "access-old")])
    store = AuthStore(tmp_path)

    def mark_dead(data: dict) -> None:
        entries = AuthStore._entries_in(data)
        entries[0].update({
            "last_status": "dead", "last_error_code": 401,
            "last_error_reason": "refresh_token_reused",
            "last_error_message": "OAuth refresh failed; reauthentication required",
            "refresh_required": True, "refresh_required_reason": "refresh_token_reused",
        })
        data.setdefault("credential_pool", {})["openai-codex"] = entries

    store.mutate(mark_dead)

    report = run_keepalive(
        store, force=True,
        refresh_fn=lambda access, refresh, timeout_seconds=20: {
            "access_token": "access-new", "refresh_token": "refresh-new",
            "last_refresh": "2026-01-03T00:00:00Z",
        },
    )

    after = store.entries()[0]
    assert report["refreshed"] == 1
    assert after["last_status"] == "ok"
    assert "last_error_reason" not in after
    assert "last_error_code" not in after
    assert "refresh_required" not in after
    assert "refresh_required_reason" not in after


def test_keepalive_skips_while_another_run_holds_the_lock(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    seed(auth, [entry("acct", 0, "refresh", "access")])
    store = AuthStore(tmp_path)
    (tmp_path / "argos-keepalive.lock").write_text("held", encoding="ascii")
    calls: list[int] = []

    report = run_keepalive(
        store, force=True,
        refresh_fn=lambda access, refresh, timeout_seconds=20: calls.append(1) or {},
    )

    assert report.get("skipped") == "another keepalive run is in progress"
    assert calls == []


def test_keepalive_takes_over_a_stale_lock(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    seed(auth, [entry("acct", 0, "refresh", "access")])
    store = AuthStore(tmp_path)
    lock = tmp_path / "argos-keepalive.lock"
    lock.write_text("stale", encoding="ascii")
    stale = time.time() - 3600
    os.utime(lock, (stale, stale))

    report = run_keepalive(
        store, force=True,
        refresh_fn=lambda access, refresh, timeout_seconds=20: {
            "access_token": "access-new", "refresh_token": "refresh-new",
            "last_refresh": "2026-01-04T00:00:00Z",
        },
    )

    assert report["refreshed"] == 1
    assert lock.exists() is False


def test_file_lock_waits_out_a_held_byte_range_instead_of_crashing(tmp_path: Path) -> None:
    msvcrt = pytest.importorskip("msvcrt")
    lock_path = tmp_path / "auth.lock"
    lock_path.write_bytes(b"0")
    holder = open(lock_path, "a+b")
    holder.seek(0)
    msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)
    try:
        lock = _FileLock(lock_path, timeout=0.3)
        with pytest.raises(TimeoutError):
            with lock:
                pass
    finally:
        holder.seek(0)
        msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, 1)
        holder.close()


def test_heal_premature_dead_restores_account_with_valid_access_token(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    seed(auth, [entry("acct", 0, "refresh-stale", jwt_with_exp(9 * 24 * 3600))])
    store = AuthStore(tmp_path)

    def mark(data: dict) -> None:
        entries = AuthStore._entries_in(data)
        entries[0].update({"last_status": "dead", "last_error_code": 401, "last_error_reason": "refresh_token_reused"})
        data.setdefault("credential_pool", {})["openai-codex"] = entries

    store.mutate(mark)

    assert heal_premature_dead(store) is True
    healed = store.entries()[0]
    assert healed["last_status"] == "ok"
    assert healed["refresh_required"] is True


def test_heal_premature_dead_keeps_account_without_valid_access(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    seed(auth, [entry("acct", 0, "refresh-stale", jwt_with_exp(-3600))])
    store = AuthStore(tmp_path)

    def mark(data: dict) -> None:
        entries = AuthStore._entries_in(data)
        entries[0].update({"last_status": "dead", "last_error_code": 401, "last_error_reason": "refresh_token_reused"})
        data.setdefault("credential_pool", {})["openai-codex"] = entries

    store.mutate(mark)

    assert heal_premature_dead(store) is False
    assert store.entries()[0]["last_status"] == "dead"


def test_status_heals_premature_dead_when_policy_runs(tmp_path: Path, monkeypatch) -> None:
    import hermes_argos.pool as pool_module

    monkeypatch.setattr(pool_module, "fetch_all_usage", lambda store, force=False: {})
    auth = tmp_path / "auth.json"
    seed(auth, [entry("acct", 0, "refresh-stale", jwt_with_exp(9 * 24 * 3600))])
    store = AuthStore(tmp_path)

    def mark(data: dict) -> None:
        entries = AuthStore._entries_in(data)
        entries[0].update({"last_status": "dead", "last_error_code": 401, "last_error_reason": "refresh_token_reused"})
        data.setdefault("credential_pool", {})["openai-codex"] = entries

    store.mutate(mark)

    status(store, apply_policy=True)

    assert store.entries()[0]["last_status"] == "ok"


def test_dashboard_api_imports_when_hermes_loads_it_by_file_path(tmp_path: Path) -> None:
    """Dashboard plugin APIs are loaded outside the plugin package's sys.path."""
    api = Path(__file__).parents[1] / "dashboard" / "plugin_api.py"
    code = (
        "import importlib.util\n"
        f"p = {str(api)!r}\n"
        "s = importlib.util.spec_from_file_location('dashboard_probe', p)\n"
        "m = importlib.util.module_from_spec(s)\n"
        "s.loader.exec_module(m)\n"
        "assert m.router is not None\n"
    )
    clean_env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=clean_env,
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr

