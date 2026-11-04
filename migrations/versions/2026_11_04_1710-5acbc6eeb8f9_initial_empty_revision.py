"""initial empty revision

Revision ID: 5acbc6eeb8f9
Revises:
Create Date: 2026-11-04 17:10:19.974157

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "5acbc6eeb8f9"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
