"""Persistent JSON cache for slow / rate-limited passive site signals.

Stored between CI runs with `actions/cache` so each weekly run only re-queries expired keys.
Layout: `{"version": 1, "namespaces": {"<ns>": {"<key>": {"checked_at": iso, "data": {...}}}}}`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

CACHE_VERSION = 1


class SignalCache:
    def __init__(self, path: Path | None = None, *, now: datetime | None = None) -> None:
        self.path = path
        self.now = now or datetime.now(UTC)
        self._ns: dict[str, dict[str, dict[str, Any]]] = {}
        if path is not None and path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("version") == CACHE_VERSION:
                    ns = raw.get("namespaces")
                    if isinstance(ns, dict):
                        self._ns = {k: v for k, v in ns.items() if isinstance(v, dict)}
            except (OSError, ValueError):
                self._ns = {}

    def get(self, namespace: str, key: str, *, ttl: timedelta) -> Any | None:
        """Return cached `data` for `key` if it was checked within `ttl`, else `None`."""
        row = self._ns.get(namespace, {}).get(key)
        if not isinstance(row, dict):
            return None
        checked = row.get("checked_at")
        try:
            checked_dt = datetime.fromisoformat(str(checked))
        except ValueError:
            return None
        if checked_dt.tzinfo is None:
            checked_dt = checked_dt.replace(tzinfo=UTC)
        if self.now - checked_dt > ttl:
            return None
        return row.get("data")

    def get_any_age(self, namespace: str, key: str) -> tuple[Any, datetime] | None:
        """Return `(data, checked_at)` regardless of age, for keys a budgeted run could not refresh."""
        row = self._ns.get(namespace, {}).get(key)
        if not isinstance(row, dict):
            return None
        try:
            checked_dt = datetime.fromisoformat(str(row.get("checked_at")))
        except ValueError:
            return None
        if checked_dt.tzinfo is None:
            checked_dt = checked_dt.replace(tzinfo=UTC)
        return row.get("data"), checked_dt

    def put(self, namespace: str, key: str, data: Any) -> None:
        self._ns.setdefault(namespace, {})[key] = {"checked_at": self.now.isoformat(), "data": data}

    def stale_first(self, namespace: str, keys: list[str]) -> list[str]:
        """Order `keys` never-checked first, then oldest `checked_at` first (for rotating budgets)."""

        def _age(k: str) -> datetime:
            hit = self.get_any_age(namespace, k)
            return hit[1] if hit else datetime.min.replace(tzinfo=UTC)

        return sorted(keys, key=_age)

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"version": CACHE_VERSION, "namespaces": self._ns}, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp.replace(self.path)
