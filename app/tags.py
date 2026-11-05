"""Tags: turning what people type into tidy tag names, and saving them.

People type tags as one comma-separated field: "DNS, On Call, networking".
That becomes ["dns", "on-call", "networking"]: lowercase, spaces turned into
hyphens, duplicates removed, order kept.
"""

import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Tag

TAG_MAX = 30
MAX_TAGS = 10
# Letters, digits, and - _ . (not at the start), e.g. "dns", "k8s", "on-call", "v1.2".
_VALID = re.compile(r"[a-z0-9][a-z0-9._-]*")


def normalise_tag(name: str) -> str:
    """'  On Call ' -> 'on-call'."""
    return re.sub(r"\s+", "-", name.strip().lower())


def parse_tags(text: str) -> tuple[list[str], list[str]]:
    """Split a comma-separated field into tag names. Returns (names, errors)."""
    names: list[str] = []
    errors: list[str] = []
    for part in text.split(","):
        name = normalise_tag(part)
        if not name or name in names:
            continue  # skip blanks ("dns,,") and repeats
        if len(name) > TAG_MAX:
            errors.append(f"Tag “{name[:TAG_MAX]}…” is too long (max {TAG_MAX} characters).")
        elif not _VALID.fullmatch(name):
            errors.append(f"Tag “{name}” can only use letters, digits, - _ and .")
        else:
            names.append(name)
    if len(names) > MAX_TAGS:
        errors.append(f"Use at most {MAX_TAGS} tags.")
    return names, errors


def get_or_create_tags(db: Session, names: list[str]) -> list[Tag]:
    """The Tag rows for these names, creating any that don't exist yet.

    INSERT ... ON CONFLICT DO NOTHING (a Postgres feature) means two people
    adding the same new tag at the same moment can't cause an error: the
    second insert is simply skipped.
    """
    if not names:
        return []
    db.execute(insert(Tag).values([{"name": n} for n in names]).on_conflict_do_nothing())
    by_name = {t.name: t for t in db.scalars(select(Tag).where(Tag.name.in_(names)))}
    return [by_name[n] for n in names]
