from __future__ import annotations

import threading
import time

try:
    from .hermes_argos.cli import command, register_cli
    from .hermes_argos.config import load_config
    from .hermes_argos.keepalive import run_keepalive
    from .hermes_argos.pool import status
except ImportError:  # direct-source test collection has no plugin namespace
    from hermes_argos.cli import command, register_cli
    from hermes_argos.config import load_config
    from hermes_argos.keepalive import run_keepalive
    from hermes_argos.pool import status

_last_policy_run = 0.0
_last_keepalive_run = 0.0
_policy_lock = threading.Lock()
_scheduler_started = False
# Short-lived CLI processes also load this plugin. Refresh tokens are
# single-use, so keepalive attempts must stay rare per process; the pool store
# (not this loop) is the source of truth for when a refresh is actually due.
_KEEPALIVE_MIN_INTERVAL_SECONDS = 300.0


def _refresh_policy() -> None:
    status(force=False, apply_policy=True)


def _run_keepalive_throttled() -> None:
    global _last_keepalive_run
    now = time.monotonic()
    with _policy_lock:
        if now - _last_keepalive_run < _KEEPALIVE_MIN_INTERVAL_SECONDS:
            return
        _last_keepalive_run = now
    run_keepalive()


def _apply_policy_on_session_start(**_kwargs) -> None:
    """Fetch official Codex quota snapshots and apply ARGOS pool policy."""
    global _last_policy_run
    # Lazy start: registration must stay side-effect free so validation contexts
    # (plugin doctor) never spawn a background writer thread.
    _start_quota_scheduler()
    now = time.monotonic()
    minimum_interval = int(load_config()["usage_poll_seconds"])
    with _policy_lock:
        if now - _last_policy_run < minimum_interval:
            return
        _last_policy_run = now

    def worker() -> None:
        try:
            _refresh_policy()
            _run_keepalive_throttled()
        except Exception:
            # Policy is advisory; Hermes core credential-pool recovery remains authoritative.
            return

    threading.Thread(target=worker, name="argos-policy", daemon=True).start()


def _start_quota_scheduler() -> None:
    """Run quota policy while Hermes is open, not only when a chat begins.

    Deliberately policy-only: OAuth keepalive is throttled to session starts so
    many live processes cannot each POST the same single-use refresh token.
    """
    global _scheduler_started
    with _policy_lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    def scheduler() -> None:
        while True:
            try:
                _refresh_policy()
            except Exception:
                pass
            time.sleep(int(load_config()["usage_poll_seconds"]))

    threading.Thread(target=scheduler, name="argos-quota-scheduler", daemon=True).start()


def register(ctx) -> None:
    ctx.register_cli_command(
        name="subs",
        help="Manage pooled ChatGPT/Codex OAuth subscriptions",
        setup_fn=register_cli,
        handler_fn=command,
        description="Quota status, safe manual switching, keepalive, and automatic pool policy.",
    )
    ctx.register_hook("on_session_start", _apply_policy_on_session_start)
