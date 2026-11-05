"""Tests for password hashing and password rules."""

from app.security import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    hash_password,
    password_problem,
    verify_password,
)

PASSWORD = "correct horse battery staple"  # noqa: S105  (fake test value)


def test_hash_is_not_the_password_and_uses_argon2id() -> None:
    password_hash = hash_password(PASSWORD)

    assert PASSWORD not in password_hash
    assert password_hash.startswith("$argon2id$")


def test_same_password_hashes_differently_each_time() -> None:
    # A random "salt" is mixed in, so equal passwords don't give equal hashes.
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_verify_accepts_right_password_and_rejects_wrong_one() -> None:
    password_hash = hash_password(PASSWORD)

    assert verify_password(password_hash, PASSWORD) is True
    assert verify_password(password_hash, PASSWORD + "!") is False


def test_verify_returns_false_for_a_garbage_hash() -> None:
    assert verify_password("not-a-hash", PASSWORD) is False


def test_password_rules() -> None:
    assert password_problem("x" * MIN_PASSWORD_LENGTH) is None
    assert password_problem("x" * (MIN_PASSWORD_LENGTH - 1)) is not None
    assert password_problem("x" * MAX_PASSWORD_LENGTH) is None
    assert password_problem("x" * (MAX_PASSWORD_LENGTH + 1)) is not None
