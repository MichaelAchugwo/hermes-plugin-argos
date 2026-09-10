from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "auto_rotate": True,
    "strategy": "least_weekly_remaining",
    "empty_threshold_pct": 1.0,
    "usage_cache_seconds": 300,
    "usage_poll_seconds": 300,
    "keepalive_enabled": True,
    "access_refresh_skew_minutes": 20,
    "keepalive_interval_hours": 6,
}
_ALLOWED_STRATEGIES = {"least_weekly_remaining", "fill_first", "round_robin"}


def home_path(home: Path | str | None = None) -> Path:
    return Path(home or os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()


def config_path(home: Path | str | None = None) -> Path:
    return home_path(home) / "argos.yaml"


def _parse_scalar(value: str) -> Any:
    raw = value.strip()
    if raw.lower() in {"true", "false"}:
        return raw.lower() == "true"
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return raw.strip("'\"")


def load_config(home: Path | str | None = None) -> dict[str, Any]:
    path = config_path(home)
    data: dict[str, Any] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped == "argos:":
                continue
            if ":" in stripped:
                key, value = stripped.split(":", 1)
                data[key.strip()] = _parse_scalar(value)
    merged = {**DEFAULTS, **data}
    if merged["strategy"] not in _ALLOWED_STRATEGIES:
        merged["strategy"] = DEFAULTS["strategy"]
    merged["empty_threshold_pct"] = max(0.0, min(100.0, float(merged["empty_threshold_pct"])))
    merged["usage_cache_seconds"] = max(5, int(merged["usage_cache_seconds"]))
    merged["usage_poll_seconds"] = max(5, int(merged["usage_poll_seconds"]))
    merged["access_refresh_skew_minutes"] = max(1, int(merged["access_refresh_skew_minutes"]))
    merged["keepalive_interval_hours"] = max(1, int(merged["keepalive_interval_hours"]))
    return merged


def save_config(config: dict[str, Any], home: Path | str | None = None) -> Path:
    merged = {**load_config(home), **config}
    if merged["strategy"] not in _ALLOWED_STRATEGIES:
        raise ValueError(f"Unsupported strategy: {merged['strategy']}")
    path = config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["argos:"] + [
        f"  {key}: {str(merged[key]).lower() if isinstance(merged[key], bool) else merged[key]}"
        for key in DEFAULTS
    ]
    fd, name = tempfile.mkstemp(prefix="argos.yaml.tmp.", dir=str(path.parent))
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path
