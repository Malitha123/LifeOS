import json

import pytest

from api.services.personal_state import PersonalStateStore

pytestmark = pytest.mark.unit


def test_empty_store_has_expected_sections(tmp_path):
    store = PersonalStateStore(tmp_path / "state.json")
    data = store.read()
    assert data["schema_version"] == 1
    assert data["profile"] == {}
    assert data["preferences"] == {}
    assert data["goals"] == {}
    assert data["state"] == {}


def test_merge_and_remove_values(tmp_path):
    store = PersonalStateStore(tmp_path / "state.json")
    assert store.update("preferences", {"housing": "furnished", "parking": False}) == {
        "housing": "furnished",
        "parking": False,
    }
    assert store.update("preferences", {"parking": None, "studio": "avoid"}) == {
        "housing": "furnished",
        "studio": "avoid",
    }
    on_disk = json.loads((tmp_path / "state.json").read_text())
    assert on_disk["preferences"]["studio"] == "avoid"


def test_replace_section(tmp_path):
    store = PersonalStateStore(tmp_path / "state.json")
    store.update("goals", {"old": 1})
    assert store.update("goals", {"career": "AI role"}, mode="replace") == {
        "career": "AI role"
    }


def test_unknown_section_fails(tmp_path):
    store = PersonalStateStore(tmp_path / "state.json")
    with pytest.raises(ValueError):
        store.read("secrets")
    with pytest.raises(ValueError):
        store.update("secrets", {"x": 1})
