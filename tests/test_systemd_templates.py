"""Every placeholder in the systemd unit templates is substituted by setup-systemd.sh."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*__")


def test_setup_systemd_substitutes_every_system_unit_placeholder():
    setup = (REPO_ROOT / "scripts" / "setup-systemd.sh").read_text()
    substituted = set(re.findall(r'-e "s\|(__[A-Z][A-Z0-9_]*__)\|', setup))
    for unit in sorted((REPO_ROOT / "config" / "systemd").glob("*.[st]*")):
        if not unit.is_file():
            continue
        missing = set(PLACEHOLDER.findall(unit.read_text())) - substituted
        assert not missing, f"{unit.name}: {sorted(missing)} never substituted"


def test_llm_context_size_is_a_setting_with_the_current_default():
    unit = (REPO_ROOT / "config" / "systemd" / "lifeos-llm.service").read_text()
    setup = (REPO_ROOT / "scripts" / "setup-systemd.sh").read_text()
    assert "-c __LLM_CONTEXT_SIZE__" in unit
    assert '_read_env "LIFEOS_LLM_CONTEXT_SIZE" "32768"' in setup
