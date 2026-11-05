"""Tests for turning typed tags into tag names (app/tags.py). No database needed."""

import pytest

from app.tags import MAX_TAGS, TAG_MAX, normalise_tag, parse_tags


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("dns", "dns"), ("  DNS ", "dns"), ("On Call", "on-call"), ("on   call", "on-call")],
)
def test_normalise(typed, expected) -> None:
    assert normalise_tag(typed) == expected


def test_parse_splits_tidies_and_dedupes_keeping_order() -> None:
    assert parse_tags("Networking, dns,, DNS , on call, k8s, v1.2") == (
        ["networking", "dns", "on-call", "k8s", "v1.2"],
        [],
    )


def test_empty_field_means_no_tags() -> None:
    assert parse_tags("") == ([], [])
    assert parse_tags(" , ,") == ([], [])


@pytest.mark.parametrize("bad", ["-dns", "dns!", "<b>", "naïve", "a/b"])
def test_odd_characters_are_rejected(bad) -> None:
    names, errors = parse_tags(f"ok, {bad}")

    assert names == ["ok"]
    assert len(errors) == 1


def test_overlong_tag_is_rejected() -> None:
    names, errors = parse_tags("x" * (TAG_MAX + 1))

    assert names == []
    assert "too long" in errors[0]


def test_too_many_tags_is_rejected() -> None:
    _, errors = parse_tags(",".join(f"t{i}" for i in range(MAX_TAGS + 1)))

    assert errors == [f"Use at most {MAX_TAGS} tags."]
