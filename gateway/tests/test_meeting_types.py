# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for connectors.meeting_types — memaix-src card d0a1f633."""

from __future__ import annotations

import pytest

from memaix_gateway.connectors.meeting_types import MeetingTypesStore, validate_types


@pytest.fixture()
def acl_with_vault(tmp_path):
    from memaix_gateway.acl import Acl

    return Acl(
        users={"alice": {"grants": {"proj": "owner"}}},
        projects={"proj": {"vault": str(tmp_path), "calendar": {"type": "caldav"}}},
    )


def _type(slug="quick", name="Quick sync", duration_min=30, **extra):
    return {"slug": slug, "name": name, "duration_min": duration_min, **extra}


def test_store_get_defaults_to_empty_when_never_set(acl_with_vault):
    store = MeetingTypesStore(acl_with_vault, "proj", "alice")
    assert store.get() == []


def test_store_set_then_get_roundtrips(acl_with_vault):
    store = MeetingTypesStore(acl_with_vault, "proj", "alice")
    store.set([_type()])
    result = store.get()
    assert result == [
        {"slug": "quick", "name": "Quick sync", "duration_min": 30, "interval_min": 30, "default": True}
    ]


def test_interval_min_defaults_to_duration_min(acl_with_vault):
    store = MeetingTypesStore(acl_with_vault, "proj", "alice")
    store.set([_type(duration_min=45)])
    assert store.get()[0]["interval_min"] == 45


def test_interval_min_can_differ_from_duration(acl_with_vault):
    store = MeetingTypesStore(acl_with_vault, "proj", "alice")
    store.set([_type(interval_min=60)])
    assert store.get()[0]["interval_min"] == 60


def test_first_type_auto_promoted_default_when_none_marked(acl_with_vault):
    store = MeetingTypesStore(acl_with_vault, "proj", "alice")
    store.set([_type(slug="a", name="A"), _type(slug="b", name="B")])
    result = store.get()
    assert result[0]["default"] is True
    assert result[1]["default"] is False


def test_explicit_default_is_respected(acl_with_vault):
    store = MeetingTypesStore(acl_with_vault, "proj", "alice")
    store.set([_type(slug="a", name="A"), _type(slug="b", name="B", default=True)])
    result = store.get()
    assert result[0]["default"] is False
    assert result[1]["default"] is True


def test_store_is_scoped_per_user(acl_with_vault):
    alice = MeetingTypesStore(acl_with_vault, "proj", "alice")
    bob = MeetingTypesStore(acl_with_vault, "proj", "bob")
    alice.set([_type()])
    assert bob.get() == []


def test_delete_removes_one_type(acl_with_vault):
    store = MeetingTypesStore(acl_with_vault, "proj", "alice")
    store.set([_type(slug="a", name="A"), _type(slug="b", name="B")])
    store.delete("a")
    slugs = [t["slug"] for t in store.get()]
    assert slugs == ["b"]


def test_delete_missing_slug_is_a_noop(acl_with_vault):
    store = MeetingTypesStore(acl_with_vault, "proj", "alice")
    store.set([_type()])
    store.delete("does-not-exist")
    assert len(store.get()) == 1


def test_delete_last_type_leaves_empty_list(acl_with_vault):
    store = MeetingTypesStore(acl_with_vault, "proj", "alice")
    store.set([_type()])
    store.delete("quick")
    assert store.get() == []


def test_delete_default_type_promotes_another(acl_with_vault):
    store = MeetingTypesStore(acl_with_vault, "proj", "alice")
    store.set([_type(slug="a", name="A"), _type(slug="b", name="B")])
    store.delete("a")
    result = store.get()
    assert result == [
        {"slug": "b", "name": "B", "duration_min": 30, "interval_min": 30, "default": True}
    ]


def test_validate_rejects_invalid_slug():
    with pytest.raises(ValueError, match="invalid slug"):
        validate_types([_type(slug="Not Valid")])


def test_validate_rejects_duplicate_slug():
    with pytest.raises(ValueError, match="duplicate slug"):
        validate_types([_type(slug="a", name="A"), _type(slug="a", name="B")])


def test_validate_rejects_missing_name():
    with pytest.raises(ValueError, match="name is required"):
        validate_types([{"slug": "a", "duration_min": 30}])


def test_validate_rejects_duration_min_out_of_range():
    with pytest.raises(ValueError, match="duration_min"):
        validate_types([_type(duration_min=0)])
    with pytest.raises(ValueError, match="duration_min"):
        validate_types([_type(duration_min=43201)])


def test_validate_rejects_non_int_duration_min():
    with pytest.raises(ValueError, match="duration_min"):
        validate_types([_type(duration_min="30")])


def test_validate_rejects_multiple_defaults():
    with pytest.raises(ValueError, match="at most one"):
        validate_types([
            _type(slug="a", name="A", default=True),
            _type(slug="b", name="B", default=True),
        ])


def test_validate_accepts_max_duration():
    result = validate_types([_type(duration_min=43200)])
    assert result[0]["duration_min"] == 43200


def test_validate_empty_list_is_valid():
    assert validate_types([]) == []


def test_validate_rejects_non_dict_item():
    with pytest.raises(ValueError, match="must be an object"):
        validate_types(["not-a-dict"])


# ---------------------------------------------------------------------------
# Karakteriseringstester (Sonar S3776-saneringen av validate_types): exakta
# meddelanden, kontrollordning och normalisering.
# ---------------------------------------------------------------------------

def _err(types):
    with pytest.raises(ValueError) as exc:
        validate_types(types)
    return str(exc.value)


def test_validate_error_messages_are_exact():
    assert _err(["x"]) == "each meeting type must be an object, got 'x'"
    assert _err([_type(slug="Bad_Slug")]) == "invalid slug 'Bad_Slug': must be lowercase alphanumeric/hyphen"
    assert _err([{"name": "N", "duration_min": 5}]) == "invalid slug '': must be lowercase alphanumeric/hyphen"
    assert _err([_type(slug="a"), _type(slug="a")]) == "duplicate slug 'a'"
    assert _err([{"slug": "a", "duration_min": 5}]) == "'a': name is required"
    assert _err([_type(slug="a", duration_min=0)]) == "'a': duration_min must be an int in 1..43200"
    assert _err([_type(slug="a", duration_min=None)]) == "'a': duration_min must be an int in 1..43200"
    assert _err([_type(slug="a", duration_min=2.5)]) == "'a': duration_min must be an int in 1..43200"
    assert _err([_type(slug="a", interval_min=0)]) == "'a': interval_min must be an int in 1..43200"
    assert _err([_type(slug="a", interval_min=43201)]) == "'a': interval_min must be an int in 1..43200"
    assert _err([_type(slug="a", interval_min="15")]) == "'a': interval_min must be an int in 1..43200"
    assert _err([_type(slug="a", interval_min=None)]) == "'a': interval_min must be an int in 1..43200"
    assert _err([_type(slug="a", default=True), _type(slug="b", default=True)]) == (
        "at most one meeting type may be marked default"
    )


def test_validate_slug_pattern_boundaries():
    for ok in ("a", "a1", "a-b", "9", "ab-cd-ef"):
        assert validate_types([_type(slug=ok)])[0]["slug"] == ok
    for bad in ("", "-a", "a-", "A", "a_b", "a b", "å"):
        assert "invalid slug" in _err([_type(slug=bad)])


def test_validate_slug_with_trailing_newline_is_accepted_today():
    """Kvirk som låses (ändra inte): `$` i slug-regexen matchar före ett avslutande
    radbrytningstecken, så "a\\n" passerar."""
    assert validate_types([_type(slug="a\n")])[0]["slug"] == "a\n"


def test_validate_non_string_slug_raises_typeerror_today():
    """Kvirk som låses (ändra inte): slug som inte är sträng ger TypeError från
    regex-matchningen, inte ValueError."""
    with pytest.raises(TypeError):
        validate_types([_type(slug=None)])
    with pytest.raises(TypeError):
        validate_types([_type(slug=5)])


def test_validate_bool_duration_is_accepted_today():
    """Kvirk som låses (ändra inte): bool är int i Python, så True passerar som 1."""
    result = validate_types([_type(duration_min=True)])
    assert result[0]["duration_min"] is True
    assert result[0]["interval_min"] is True


def test_validate_check_order_slug_then_duplicate_then_name_then_duration_then_interval():
    assert "invalid slug" in _err([{"slug": "Bad", "duration_min": "x"}])
    assert "duplicate slug" in _err([_type(slug="a"), {"slug": "a", "duration_min": "x"}])
    assert "name is required" in _err([{"slug": "a", "duration_min": "x"}])
    assert "duration_min" in _err([_type(slug="a", duration_min="x", interval_min="y")])
    assert "interval_min" in _err([_type(slug="a", interval_min="y")])


def test_validate_item_error_wins_over_default_error_and_later_items_are_checked():
    assert "name is required" in _err([
        _type(slug="a", default=True), _type(slug="b", default=True), {"slug": "c", "duration_min": 5}
    ])
    assert "must be an object" in _err([_type(slug="a"), 3])


def test_validate_bounds_inclusive_and_interval_independent():
    result = validate_types([_type(slug="a", duration_min=1, interval_min=43200)])
    assert result == [{"slug": "a", "name": "Quick sync", "duration_min": 1, "interval_min": 43200, "default": True}]


def test_validate_normalizes_output_and_does_not_mutate_input():
    src = [
        {"slug": "a", "name": "A", "duration_min": 30, "extra": "dropped", "default": 0},
        {"slug": "b", "name": "B", "duration_min": 60, "interval_min": 15, "default": "yes"},
    ]
    snapshot = [dict(x) for x in src]
    result = validate_types(src)
    assert result == [
        {"slug": "a", "name": "A", "duration_min": 30, "interval_min": 30, "default": False},
        {"slug": "b", "name": "B", "duration_min": 60, "interval_min": 15, "default": True},
    ]
    assert src == snapshot


def test_validate_promotes_first_only_when_no_default():
    result = validate_types([_type(slug="a"), _type(slug="b"), _type(slug="c")])
    assert [t["default"] for t in result] == [True, False, False]
    result = validate_types([_type(slug="a"), _type(slug="b", default=True)])
    assert [t["default"] for t in result] == [False, True]
