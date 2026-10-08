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
(default 24h). Resolution never waits on the network: it serves the cached
list and, when that list is stale or absent, starts one background refresh
thread (single-flight per process; a file lock keeps concurrent processes
from fetching twice) whose result the next resolution sees. A failed
refresh keeps the last good list (with one logged warning) and is not
retried until the TTL passes again. With no list at all — no API key, or
no fetch finished yet — families resolve from `LATEST_KNOWN`. Resolution
never raises.
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

# `_lock` guards only in-memory state and is never held across I/O.
_lock = threading.Lock()
_memo: Optional[dict] = None
_refreshing = False
_refresh_thread: Optional[threading.Thread] = None
_warned = False
_clock = time.time


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


def _read_cache(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("models"), list)
        or not isinstance(data.get("checked_at"), (int, float))
    ):
        return None
    return data


def _write_cache(path: Path, entry: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("could not write the Claude models cache %s: %s", path, exc)


class _FileLock:
    """Advisory lock on `<cache>.lock` so concurrent processes don't both
    refresh. Taken only by the background refresh thread. Best effort:
    where locking isn't possible it does nothing."""

    def __init__(self, path: Path):
        self._path = path
        self._fd = None

    def __enter__(self):
        try:
            import fcntl

            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(f"{self._path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
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


def _set_memo(entry: dict) -> None:
    global _memo
    with _lock:
        _memo = entry


def _refresh(path: Path, ttl: float) -> None:
    """Background refresh: re-check the disk cache under the file lock (another
    process may have just refreshed it), else fetch the list and record it.
    A failure keeps the last good list and records the attempt, so the next
    one waits a full TTL."""
    global _refreshing, _warned
    try:
        with _FileLock(path):
            now = _clock()
            disk = _read_cache(path)
            if _is_fresh(disk, now, ttl):
                _set_memo(disk)
                return
            with _lock:
                last_good = (disk or _memo or {}).get("models") or []
            try:
                fetched = _fetch_model_ids()
            except Exception as exc:  # noqa: BLE001 — never fail resolution over the list
                if not _warned:
                    logger.warning(
                        "Anthropic models list refresh failed (%s); resolving Claude "
                        "families from the %s", type(exc).__name__,
                        "last good list" if last_good else "built-in table",
                    )
                    _warned = True
                entry = {"checked_at": now, "models": last_good}
                _write_cache(path, entry)
                _set_memo(entry)
                return
            if fetched is None:
                # No API key: nothing to fetch. Memoized in this process only,
                # so the disk cache is left for a process that has a key.
                _set_memo({"checked_at": now, "models": last_good})
                return
            entry = {"checked_at": now, "models": fetched}
            _write_cache(path, entry)
            _set_memo(entry)
            _warned = False
    except Exception as exc:  # noqa: BLE001
        logger.warning("Claude models cache refresh failed: %s", exc)
    finally:
        with _lock:
            _refreshing = False


def _model_ids() -> list[str]:
    """The cached models list, or empty when none has been loaded yet.
    Never waits on the network or another thread's refresh: when the list is
    stale (or absent), one background refresh is started and the current list
    is served meanwhile."""
    global _memo, _refreshing, _refresh_thread
    from config.settings import settings

    ttl = float(settings.anthropic_models_ttl_seconds)
    path = CACHE_PATH
    if _memo is None:
        # First use in this process: adopt whatever another process cached.
        disk = _read_cache(path)
        if disk is not None:
            with _lock:
                if _memo is None:
                    _memo = disk
    with _lock:
        entry = _memo
        start = not _refreshing and not _is_fresh(entry, _clock(), ttl)
        if start:
            _refreshing = True
    if start:
        thread = threading.Thread(
            target=_refresh, args=(path, ttl), name="claude-models-refresh", daemon=True,
        )
        _refresh_thread = thread
        thread.start()
    return entry["models"] if entry else []


def resolve_claude_model(value: str) -> str:
    """The newest model in `value`'s family when `value` is a bare family
    name (`haiku`/`sonnet`/`opus`, any case, surrounding whitespace
    ignored); any other value unchanged. Never raises or blocks on the
    network."""
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


def wait_for_refresh(timeout: float = 5.0) -> None:
    """Block until an in-flight background refresh finishes (tests)."""
    thread = _refresh_thread
    if thread is not None:
        thread.join(timeout)


def reset_cache() -> None:
    """Wait out any refresh, then forget the in-process memo and warning
    state (tests)."""
    global _memo, _warned
    wait_for_refresh()
    with _lock:
        _memo = None
        _warned = False
