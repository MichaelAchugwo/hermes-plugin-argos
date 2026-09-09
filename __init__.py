from __future__ import annotations

import threading
import time

try:
    from .hermes_argos.cli import command, register_cli
    from .hermes_argos.keepalive import run_keepalive
    from .hermes_argos.pool import status
except ImportError:  # direct-source test collection has no plugin namespace
    from hermes_argos.cli import command, register_cli
    from hermes_argos.keepalive import run_keepalive
    from hermes_argos.pool import status

_last_policy_run = 0.0
_policy_lock = threading.Lock()


def _apply_policy_on_session_start(**_kwargs) -> None:
    global _last_policy_run
    now = time.monotonic()
    with _policy_lock:
        if now - _last_policy_run < 60:
            return
        _last_policy_run = now

    def worker() -> None:
        try:
            status(force=False, apply_policy=True)
            run_keepalive()
        except Exception:
            # Policy is advisory; Hermes core credential-pool recovery remains authoritative.
            return

    threading.Thread(target=worker, name="argos-policy", daemon=True).start()


def register(ctx) -> None:
    ctx.register_cli_command(
        name="subs",
        help="Manage pooled ChatGPT/Codex OAuth subscriptions",
        setup_fn=register_cli,
        handler_fn=command,
        description="Quota status, safe manual switching, keepalive, and automatic pool policy.",
    )
    ctx.register_hook("on_session_start", _apply_policy_on_session_start)
