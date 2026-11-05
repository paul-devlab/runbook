"""Password hashing with argon2.

We never store passwords, only a *hash*: a one-way scramble that can be checked
against a typed password but not turned back into it. argon2 is deliberately
slow and memory-hungry, so guessing passwords from a leaked hash is expensive.

The hash string records its own settings, e.g. "$argon2id$v=19$m=65536,t=3,p=4$...",
so we can raise the settings later and old hashes still verify.
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# argon2-cffi's defaults follow the RFC 9106 "low memory" recommendation
# (argon2id, 64 MiB, 3 passes). One instance can be shared.
_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256  # long enough for any passphrase; stops absurd inputs


def hash_password(password: str) -> str:
    """Return an argon2id hash of `password`, safe to store in the database."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """True if `password` matches `password_hash`. Never raises for a wrong password."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def password_problem(password: str) -> str | None:
    """Return why `password` isn't acceptable, or None if it's fine."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
    return None
