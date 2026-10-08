"""Resolve a Claude model family name to the newest model in that family.

Claude model settings and constants name a family (`haiku`, `sonnet`,
`opus`) rather than a release, and the Anthropic call boundary
(`AnthropicLLMClient`, the Managed Agents executor) passes whatever model it
was given through `resolve_claude_model` before sending a request. A family
name becomes the newest model in that family; a full model id
(`claude-sonnet-4-6`, a dated snapshot) is returned unchanged, so an
operator pins a release by naming it.

"Newest" comes from Anthropic's models list (`models.list()`), parsed with
the same `claude-<family>-<version>` convention the board's model catalog
uses. Only the three families above are ever chosen — a Fable, Mythos or
any other model is ignored — and an undated alias (`claude-sonnet-5-5`)
wins over a dated snapshot of the same version.

The list is cached on disk (`data/claude_models.json`, shared by every
LifeOS process) and in memory for `settings.anthropic_models_ttl_seconds`
(default 24h). At most one refresh — a short blocking call bounded by
`_FETCH_TIMEOUT_SECONDS` — runs per TTL; a process lock and a file lock
serialize it across threads and processes. A failed refresh keeps the last
good list (with one logged warning) and is not retried until the TTL passes
again. With no list at all — no API key, or no successful fetch yet —
families resolve from `LATEST_KNOWN`. Resolution never raises.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

FAMILIES = ("haiku", "sonnet", "opus")

# The newest model per family as of this module's last edit; used when the
# models list is unavailable.
LATEST_KNOWN = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}

# A trailing dated-snapshot suffix on an Anthropic model id, e.g.
# "claude-sonnet-4-5-20250929" -> "claude-sonnet-4-5".
_DATED_SNAPSHOT_SUFFIX = re.compile(r"-\d{8}$")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_PATH = _REPO_ROOT / "data" / "claude_models.json"
_FETCH_TIMEOUT_SECONDS = 10.0

_lock = threading.Lock()
_memo: Optional[dict] = None
_warned = False


def _parse_version(segments: list[str]) -> tuple[int, ...] | None:
    """Flatten dash/dot-separated numeric segments into a version tuple,
    or None if any segment isn't a plain integer (e.g. a non-numeric id
    that doesn't fit the family/version convention at all)."""
    version: list[int] = []
    for segment in segments:
        for piece in segment.split("."):
            if not piece.isdigit():
                return None
            version.append(int(piece))
    return tuple(version)


def _parse_claude_family_version(model_id: str) -> tuple[str, tuple[int, ...]] | None:
    """`claude-<family>-<version...>`, dated snapshot suffix stripped first
    (`claude-opus-4-5-20251101` -> family "opus", version (4, 5))."""
    stripped = _DATED_SNAPSHOT_SUFFIX.sub("", model_id)
    parts = stripped.split("-")
    if len(parts) < 3 or parts[0] != "claude":
        return None
    version = _parse_version(parts[2:])
    if version is None:
        return None
    return parts[1], version


def family_of(model: str) -> Optional[str]:
    """The family (`haiku`/`sonnet`/`opus`) a bare family name or a
    `claude-<family>-<version>` id belongs to, or None for anything else."""
    name = (model or "").strip().lower()
    if name in FAMILIES:
        return name
    parsed = _parse_claude_family_version(name)
    if parsed and parsed[0] in FAMILIES:
        return parsed[0]
    return None


def newest_in_family(model_ids: list[str], family: str) -> Optional[str]:
    """The newest id in `family` by parsed version; at an equal version an
    undated alias beats a dated snapshot, then list order breaks ties.
    None when `family` isn't one of FAMILIES or nothing in the list matches."""
    if family not in FAMILIES:
        return None
    best_id: Optional[str] = None
    best_key: tuple | None = None
    for model_id in model_ids:
        if not isinstance(model_id, str):
            continue
        parsed = _parse_claude_family_version(model_id)
        if parsed is None or parsed[0] != family:
            continue
        key = (parsed[1], not _DATED_SNAPSHOT_SUFFIX.search(model_id))
        if best_key is None or key > best_key:
            best_id, best_key = model_id, key
    return best_id


def _fetch_model_ids() -> Optional[list[str]]:
    """Every model id Anthropic's models list returns, or None when no API
    key is configured. Raises on a failed request."""
    from config.settings import settings

    api_key = settings.anthropic_api_key
    if not api_key:
        return None
    import anthropic

    client = anthropic.Anthropic(
        api_key=api_key, timeout=_FETCH_TIMEOUT_SECONDS, max_retries=0,
    )
    return [m.id for m in client.models.list(limit=1000)]


def _read_cache() -> Optional[dict]:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("models"), list)
        or not isinstance(data.get("checked_at"), (int, float))
    ):
        return None
    return data


def _write_cache(entry: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_name(f"{CACHE_PATH.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except OSError as exc:
        logger.warning("could not write the Claude models cache %s: %s", CACHE_PATH, exc)


class _FileLock:
    """Advisory lock on `<cache>.lock` so concurrent processes don't both
    refresh. Best effort: where locking isn't possible it does nothing."""

    def __enter__(self):
        self._fd = None
        try:
            import fcntl

            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(f"{CACHE_PATH}.lock", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except (ImportError, OSError):
            if self._fd is not None:
                os.close(self._fd)
            self._fd = None
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            os.close(self._fd)  # closing the descriptor releases the lock
        return False


def _is_fresh(entry: Optional[dict], now: float, ttl: float) -> bool:
    return entry is not None and (now - entry["checked_at"]) < ttl


def _model_ids() -> list[str]:
    """The cached models list, refreshed when older than the TTL. Empty
    when no list has ever been fetched."""
    global _memo, _warned
    from config.settings import settings

    ttl = float(settings.anthropic_models_ttl_seconds)
    with _lock:
        now = time.time()
        if _is_fresh(_memo, now, ttl):
            return _memo["models"]
        with _FileLock():
            disk = _read_cache()
            if _is_fresh(disk, now, ttl):
                _memo = disk
                return disk["models"]
            last_good = (disk or _memo or {}).get("models") or []
            try:
                fetched = _fetch_model_ids()
            except Exception as exc:  # noqa: BLE001 — never fail a request over the list
                if not _warned:
                    logger.warning(
                        "Anthropic models list refresh failed (%s); resolving Claude "
                        "families from the %s", type(exc).__name__,
                        "last good list" if last_good else "built-in table",
                    )
                    _warned = True
                # Record the attempt so the next one waits a full TTL.
                _memo = {"checked_at": now, "models": last_good}
                _write_cache(_memo)
                return last_good
            if fetched is None:
                # No API key: nothing to fetch. Memoized in this process only,
                # so the disk cache is left for a process that has a key.
                _memo = {"checked_at": now, "models": last_good}
                return last_good
            _memo = {"checked_at": now, "models": fetched}
            _write_cache(_memo)
            _warned = False
            return fetched


def resolve_claude_model(value: str) -> str:
    """The newest model in `value`'s family when `value` is a bare family
    name (`haiku`/`sonnet`/`opus`, any case, surrounding whitespace
    ignored); any other value unchanged. Never raises."""
    if not isinstance(value, str):
        return value
    family = value.strip().lower()
    if family not in FAMILIES:
        return value
    try:
        return newest_in_family(_model_ids(), family) or LATEST_KNOWN[family]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Claude model resolution failed for %r: %s", family, exc)
        return LATEST_KNOWN[family]


def reset_cache() -> None:
    """Forget the in-process memo and warning state (tests)."""
    global _memo, _warned
    with _lock:
        _memo = None
        _warned = False
