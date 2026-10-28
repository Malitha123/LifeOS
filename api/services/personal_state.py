"""Small structured store for the PA's profile, preferences, goals and state.

This complements free-form memories.  Reads are local and safe; writes are only
reachable through a permission-gated tool in agent_tools.py.
"""
from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any



_DEFAULT = {
    "schema_version": 1,
    "profile": {},
    "preferences": {},
    "goals": {},
    "state": {},
}
_ALLOWED_SECTIONS = frozenset({"profile", "preferences", "goals", "state"})


class PersonalStateStore:
    def __init__(self, path: Path | None = None):
        if path is None:
            from config.settings import settings
            path = Path(settings.chroma_path).parent / "personal_state.json"
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read_locked(self) -> dict[str, Any]:
        if not self.path.exists():
            return deepcopy(_DEFAULT)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return deepcopy(_DEFAULT)
        if not isinstance(data, dict):
            return deepcopy(_DEFAULT)
        result = deepcopy(_DEFAULT)
        result["schema_version"] = int(data.get("schema_version") or 1)
        for section in _ALLOWED_SECTIONS:
            value = data.get(section, {})
            result[section] = value if isinstance(value, dict) else {}
        return result

    def read(self, section: str | None = None) -> dict[str, Any]:
        with self._lock:
            data = self._read_locked()
            if section:
                if section not in _ALLOWED_SECTIONS:
                    raise ValueError(f"Unknown personal-state section: {section}")
                return deepcopy(data[section])
            return deepcopy(data)

    def update(self, section: str, values: dict[str, Any], *, mode: str = "merge") -> dict[str, Any]:
        if section not in _ALLOWED_SECTIONS:
            raise ValueError(f"Unknown personal-state section: {section}")
        if not isinstance(values, dict):
            raise ValueError("values must be an object")
        if mode not in {"merge", "replace"}:
            raise ValueError("mode must be 'merge' or 'replace'")

        with self._lock:
            data = self._read_locked()
            if mode == "replace":
                data[section] = deepcopy(values)
            else:
                merged = dict(data[section])
                for key, value in values.items():
                    if value is None:
                        merged.pop(str(key), None)
                    else:
                        merged[str(key)] = value
                data[section] = merged
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temp, self.path)
            return deepcopy(data[section])


_STORE: PersonalStateStore | None = None


def get_personal_state_store() -> PersonalStateStore:
    global _STORE
    if _STORE is None:
        _STORE = PersonalStateStore()
    return _STORE


def render_personal_state_context(max_chars: int = 6000) -> str:
    """Return a compact prompt block when structured state contains data."""
    data = get_personal_state_store().read()
    payload = {k: data[k] for k in ("profile", "preferences", "goals", "state") if data.get(k)}
    if not payload:
        return ""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    if len(text) > max_chars:
        text = text[: max_chars - 40] + "\n... [structured personal state truncated]"
    return "## Structured personal context\n" + text
