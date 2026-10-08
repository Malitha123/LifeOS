#!/usr/bin/env python3
"""Probe every configured remote-provider model id and report the ones that fail.

A provider can retire a model id at any time; every LifeOS surface (and any
other agent configured with the same provider) then fails each call. This
collects model ids from LifeOS's own settings (`LIFEOS_REMOTE_LLM_MODEL`,
`LIFEOS_REMOTE_LLM_MODEL_OPTIONS`) and from the files named in
`LIFEOS_MODEL_PROBE_SOURCES` (a local path, or `host:path` read over ssh),
then sends each id one tiny completion on the remote provider.

Output: one line per problem worth alerting on, `<key>\\t<message>`, for the
infra watchdog to forward. A retired model (404) or a rejected key (401/403)
is reported at once; a transient failure (timeout, 5xx, other 4xx) and an
unreadable source are reported only after `STRIKES` consecutive runs. State
lives in `$LIFEOS_INFRA_STATE_DIR/model-probe.json`. Always exits 0 unless
the provider itself is unconfigured (exit 2).

Usage:
    ~/.venvs/lifeos/bin/python scripts/check_remote_models.py [--verbose]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
os.chdir(PROJECT_DIR)

from config.settings import settings  # noqa: E402

MODEL_ID_RE = re.compile(r"accounts/[A-Za-z0-9_-]+/models/[A-Za-z0-9._-]+")
# A `#` at the start of a line or after whitespace starts a comment in both
# .env and YAML; a commented-out model id is not configured.
COMMENT_RE = re.compile(r"(^|\s)#.*$", re.MULTILINE)
STRIKES = 3
# Probes and source reads run in parallel under these bounds, so a run
# finishes well inside the watchdog's own time limit.
TIMEOUT_SECONDS = 20
SSH_TIMEOUT_SECONDS = 20


def _split(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def read_source(source: str) -> str:
    """Contents of a local path, or of `host:path` over ssh."""
    host, sep, path = source.partition(":")
    if sep and "/" not in host and not Path(source).expanduser().exists():
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, f"cat {path}"],
            capture_output=True, text=True, timeout=SSH_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise OSError(f"ssh {host} exited {result.returncode}")
        return result.stdout
    return Path(source).expanduser().read_text()


def collect(sources: list[str]) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Model id -> where it is configured, plus source -> read error."""
    where: dict[str, list[str]] = {}
    for model in [settings.remote_llm_model, *_split(settings.remote_llm_model_options)]:
        if model:
            where.setdefault(model, []).append("LifeOS settings")
    errors: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {source: pool.submit(read_source, source) for source in sources}
    for source, future in futures.items():
        try:
            text = future.result()
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
            errors[source] = str(exc) or type(exc).__name__
            continue
        for model in sorted(set(MODEL_ID_RE.findall(COMMENT_RE.sub("", text)))):
            where.setdefault(model, []).append(source)
    return where, errors


def probe(client: httpx.Client, base_url: str, model: str) -> tuple[str, str]:
    """('ok' | 'retired' | 'auth' | 'transient', detail) for one model id."""
    try:
        response = client.post(
            f"{base_url}/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": "ok"}], "max_tokens": 16},
        )
    except httpx.HTTPError as exc:
        return "transient", type(exc).__name__
    if response.status_code == 200:
        return "ok", ""
    if response.status_code == 404:
        return "retired", "404 model not found"
    if response.status_code in (401, 403):
        return "auth", f"HTTP {response.status_code}"
    return "transient", f"HTTP {response.status_code}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verbose", action="store_true", help="print every result to stderr")
    args = parser.parse_args()

    base_url = (settings.remote_llm_base_url or "").rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[: -len("/v1")]
    if not (base_url and settings.remote_llm_api_key):
        print("remote provider not configured", file=sys.stderr)
        return 2

    state_dir = Path(os.environ.get("LIFEOS_INFRA_STATE_DIR", PROJECT_DIR / "logs"))
    state_path = state_dir / "model-probe.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    strikes: dict[str, int] = state.get("strikes", {})
    next_strikes: dict[str, int] = {}

    where, read_errors = collect(_split(settings.model_probe_sources))
    problems: list[tuple[str, str]] = []

    for source, error in read_errors.items():
        key = f"source:{source}"
        next_strikes[key] = strikes.get(key, 0) + 1
        if args.verbose:
            print(f"source {source}: unreadable ({error})", file=sys.stderr)
        if next_strikes[key] >= STRIKES:
            problems.append((key, f"The model probe could not read {source} for {next_strikes[key]} runs ({error}), so model ids configured there are not being checked."))

    headers = {"Authorization": f"Bearer {settings.remote_llm_api_key}"}
    results: dict[str, str] = {}
    with httpx.Client(timeout=TIMEOUT_SECONDS, headers=headers) as client:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = {model: pool.submit(probe, client, base_url, model) for model in where}
        for model, places in sorted(where.items()):
            status, detail = outcomes[model].result()
            results[model] = status
            if args.verbose:
                print(f"{model}: {status} {detail}".rstrip(), file=sys.stderr)
            used_in = ", ".join(places)
            if status == "retired":
                problems.append((f"model:{model}", f"Remote model {model} is no longer served ({detail}). Every call configured with it fails. Configured in: {used_in}."))
            elif status == "auth":
                problems.append((f"model:{model}", f"Remote provider rejected the key for {model} ({detail}). Configured in: {used_in}."))
            elif status == "transient":
                key = f"model:{model}"
                next_strikes[key] = strikes.get(key, 0) + 1
                if next_strikes[key] >= STRIKES:
                    problems.append((key, f"Remote model {model} has failed {next_strikes[key]} probes in a row ({detail}). Configured in: {used_in}."))

    state_dir.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"strikes": next_strikes, "results": results}, indent=1))
    for key, message in problems:
        print(f"{key}\t{message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
