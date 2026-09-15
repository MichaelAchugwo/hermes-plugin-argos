from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

PROVIDER = "openai-codex"
_SECRET_KEYS = {"access_token", "refresh_token", "id_token", "token", "api_key"}


class AuthStoreError(RuntimeError):
    pass


class DuplicateCredentialError(AuthStoreError):
    pass


class SelectorError(AuthStoreError):
    pass


def fingerprint(value: Any) -> str:
    text = str(value or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12] if text else "none"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


class _FileLock:
    _thread_lock = threading.RLock()

    def __init__(self, path: Path, timeout: float = 30.0):
        self.path = path
        self.timeout = timeout
        self.handle = None

    def __enter__(self):
        self._thread_lock.acquire()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        try:
            self.handle.seek(0)
            if self.handle.read(1) == b"":
                self.handle.write(b"0")
                self.handle.flush()
        except OSError:
            # Another process holds the byte-range lock; byte 0 exists by its hand.
            pass
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    self._thread_lock.release()
                    raise TimeoutError(f"Timed out locking {self.path}")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.handle is not None:
                if os.name == "nt":
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                self.handle.close()
        finally:
            self.handle = None
            self._thread_lock.release()


class AuthStore:
    def __init__(self, hermes_home: Path | str | None = None):
        raw_home = hermes_home or os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
        self.home = Path(raw_home).expanduser()
        self.path = self.home / "auth.json"
        self.backup_dir = self.home / "backups" / "argos"
        # Hermes core uses the same sibling lock file; sharing it prevents
        # plugin writes from racing `hermes auth` or runtime token refreshes.
        self.lock_path = self.path.with_suffix(".lock")

    @contextmanager
    def locked(self) -> Iterator[None]:
        with _FileLock(self.lock_path):
            yield

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 2, "providers": {}, "credential_pool": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthStoreError(f"Cannot safely read {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise AuthStoreError(f"Auth store root is not an object: {self.path}")
        return data

    def read(self) -> dict[str, Any]:
        with self.locked():
            return self._read_unlocked()

    @staticmethod
    def _entries_in(data: dict[str, Any]) -> list[dict[str, Any]]:
        pools = data.get("credential_pool")
        entries = pools.get(PROVIDER) if isinstance(pools, dict) else None
        return [dict(e) for e in entries] if isinstance(entries, list) else []

    def entries(self) -> list[dict[str, Any]]:
        return sorted(self._entries_in(self.read()), key=lambda e: (int(e.get("priority", 999999)), str(e.get("id", ""))))

    def validate(self) -> list[str]:
        entries = self.entries()
        return self._validate_entries(entries)

    @staticmethod
    def _validate_entries(entries: list[dict[str, Any]]) -> list[str]:
        seen: dict[str, str] = {}
        problems: list[str] = []
        for entry in entries:
            refresh = str(entry.get("refresh_token") or "")
            if refresh:
                fp = fingerprint(refresh)
                if fp in seen:
                    raise DuplicateCredentialError(
                        f"Duplicate refresh-token fingerprint {fp} on {seen[fp]} and {entry.get('id', '?')}"
                    )
                seen[fp] = str(entry.get("id") or "?")
            if not entry.get("access_token"):
                problems.append(f"{entry.get('label') or entry.get('id') or '?'} has no access token")
        return problems

    def _is_active(self, data: dict[str, Any], entry: dict[str, Any]) -> bool:
        providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
        state = providers.get(PROVIDER) if isinstance(providers, dict) else {}
        tokens = state.get("tokens") if isinstance(state, dict) else {}
        return bool(tokens and tokens.get("access_token") == entry.get("access_token"))

    def public_entries(self) -> list[dict[str, Any]]:
        data = self.read()
        entries = sorted(self._entries_in(data), key=lambda e: (int(e.get("priority", 999999)), str(e.get("id", ""))))
        public: list[dict[str, Any]] = []
        for index, entry in enumerate(entries, 1):
            public.append({
                "index": index,
                "id": str(entry.get("id") or ""),
                "label": str(entry.get("label") or entry.get("id") or f"account-{index}"),
                "priority": int(entry.get("priority", index - 1)),
                "source": str(entry.get("source") or ""),
                "auth_type": str(entry.get("auth_type") or ""),
                "fingerprint": fingerprint(entry.get("refresh_token") or entry.get("access_token")),
                "active": self._is_active(data, entry),
                "last_status": entry.get("last_status"),
                "last_status_at": entry.get("last_status_at"),
                "last_error_code": entry.get("last_error_code"),
                "last_error_reason": entry.get("last_error_reason"),
                "last_error_reset_at": entry.get("last_error_reset_at"),
                "last_refresh": entry.get("last_refresh"),
                "expires_at": entry.get("expires_at"),
                "expires_at_ms": entry.get("expires_at_ms"),
                "refresh_required": bool(entry.get("refresh_required")),
                "refresh_required_reason": entry.get("refresh_required_reason"),
            })
        return public

    def resolve(self, selector: str, *, data: dict[str, Any] | None = None) -> dict[str, Any]:
        entries = sorted(self._entries_in(data or self.read()), key=lambda e: (int(e.get("priority", 999999)), str(e.get("id", ""))))
        raw = str(selector or "").strip()
        if not raw:
            raise SelectorError("Selector is required")
        if raw.isdigit() and 1 <= int(raw) <= len(entries):
            return entries[int(raw) - 1]
        lowered = raw.casefold()
        matches = [e for e in entries if str(e.get("id") or "").casefold() == lowered or str(e.get("label") or "").casefold() == lowered]
        if not matches and len(raw) >= 4:
            matches = [e for e in entries if fingerprint(e.get("refresh_token") or e.get("access_token")).startswith(lowered)]
        if len(matches) != 1:
            detail = "ambiguous" if matches else "not found"
            raise SelectorError(f"Credential selector {raw!r} {detail}")
        return matches[0]

    def _backup_unlocked(self) -> Path | None:
        if not self.path.exists():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        target = self.backup_dir / f"auth-{_utc_stamp()}-{uuid.uuid4().hex[:6]}.json"
        shutil.copy2(self.path, target)
        backups = sorted(self.backup_dir.glob("auth-*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for stale in backups[20:]:
            stale.unlink(missing_ok=True)
        return target

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix="auth.json.tmp.", dir=str(self.path.parent))
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def mutate(self, mutator: Callable[[dict[str, Any]], Any]) -> Any:
        with self.locked():
            data = self._read_unlocked()
            before_other = copy.deepcopy({k: data.get(k) for k in data if k not in {"providers", "credential_pool", "updated_at", "version"}})
            providers_before = copy.deepcopy(dict(data.get("providers") or {}))
            pools_before = copy.deepcopy(dict(data.get("credential_pool") or {}))
            result = mutator(data)
            after_other = {k: data.get(k) for k in data if k not in {"providers", "credential_pool", "updated_at", "version"}}
            if before_other != after_other:
                raise AuthStoreError("Mutation attempted outside allowed auth-store sections")
            providers_after = dict(data.get("providers") or {})
            pools_after = dict(data.get("credential_pool") or {})
            providers_before.pop(PROVIDER, None)
            providers_after.pop(PROVIDER, None)
            pools_before.pop(PROVIDER, None)
            pools_after.pop(PROVIDER, None)
            if providers_before != providers_after or pools_before != pools_after:
                raise AuthStoreError("Mutation attempted outside openai-codex auth sections")
            self._validate_entries(self._entries_in(data))
            self._backup_unlocked()
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._write_unlocked(data)
            return result

    @staticmethod
    def _provider_state_for(entry: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
        tokens = {key: entry[key] for key in ("access_token", "refresh_token", "id_token", "account_id") if entry.get(key)}
        return {
            **(existing or {}),
            "tokens": tokens,
            "last_refresh": entry.get("last_refresh") or datetime.now(timezone.utc).isoformat(),
            "auth_mode": "device_code" if "device_code" in str(entry.get("source") or "") else "oauth",
        }

    def activate(self, selector: str, *, clear_if_healthy: bool = False) -> dict[str, Any]:
        def apply(data: dict[str, Any]) -> dict[str, Any]:
            selected = self.resolve(selector, data=data)
            entries = sorted(self._entries_in(data), key=lambda e: (int(e.get("priority", 999999)), str(e.get("id", ""))))
            ordered = [e for e in entries if e.get("id") == selected.get("id")] + [e for e in entries if e.get("id") != selected.get("id")]
            for priority, item in enumerate(ordered):
                item["priority"] = priority
            chosen = ordered[0]
            if clear_if_healthy:
                for key in ("last_error_code", "last_error_reason", "last_error_message", "last_error_reset_at", "failure_reason"):
                    chosen.pop(key, None)
                chosen["last_status"] = "ok"
                chosen["last_status_at"] = time.time()
            pools = data.setdefault("credential_pool", {})
            pools[PROVIDER] = ordered
            providers = data.setdefault("providers", {})
            existing = providers.get(PROVIDER) if isinstance(providers.get(PROVIDER), dict) else {}
            providers[PROVIDER] = self._provider_state_for(chosen, existing)
            return dict(chosen)
        return self.mutate(apply)

    def reorder(self, ids: list[str]) -> None:
        def apply(data: dict[str, Any]) -> None:
            entries = self._entries_in(data)
            by_id = {str(e.get("id")): e for e in entries}
            ordered = [by_id.pop(identifier) for identifier in ids if identifier in by_id]
            ordered.extend(sorted(by_id.values(), key=lambda e: int(e.get("priority", 999999))))
            for priority, entry in enumerate(ordered):
                entry["priority"] = priority
            data.setdefault("credential_pool", {})[PROVIDER] = ordered
        self.mutate(apply)

    def update_tokens(self, identifier: str, tokens: dict[str, Any], *, status: str | None = None) -> None:
        def apply(data: dict[str, Any]) -> None:
            entries = self._entries_in(data)
            target = next((e for e in entries if str(e.get("id")) == identifier), None)
            if target is None:
                raise SelectorError(f"Credential id {identifier!r} not found")
            was_active = self._is_active(data, target)
            for key in ("access_token", "refresh_token", "last_refresh"):
                if tokens.get(key):
                    target[key] = tokens[key]
            if status:
                target["last_status"] = status
                target["last_status_at"] = time.time()
            if status == "ok":
                # A successful refresh proves the credential is alive; stale
                # rate-limit/terminal markers must not keep it in "reauth".
                for key in (
                    "last_error_code", "last_error_reason", "last_error_message",
                    "last_error_reset_at", "failure_reason",
                    "refresh_required", "refresh_required_reason", "refresh_required_at",
                ):
                    target.pop(key, None)
            data.setdefault("credential_pool", {})[PROVIDER] = entries
            if was_active:
                providers = data.setdefault("providers", {})
                existing = providers.get(PROVIDER) if isinstance(providers.get(PROVIDER), dict) else {}
                providers[PROVIDER] = self._provider_state_for(target, existing)
        self.mutate(apply)
