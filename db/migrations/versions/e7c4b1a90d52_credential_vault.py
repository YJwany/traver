"""Credential vault storage.

Adds the table that holds a user's own third-party API keys. Unlike ``api_keys``
— Traver's bearer credentials, stored as a one-way hash because they are only
ever verified — these must be replayed upstream, so the column holds AES-256-GCM
ciphertext (``nonce || ciphertext+tag``) sealed under a master key that is not in
this database.

The unique constraint is on ``(owner_id, provider_id, credential_key)`` because
that triple is exactly what the ciphertext is bound to as associated data, and it
is also the identity a re-save should update rather than duplicate.
"""

import sqlalchemy as sa
from alembic import op

revision = "e7c4b1a90d52"
down_revision = "d4f2a7c81b03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.String(length=120), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("credential_key", sa.String(length=64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id",
            "provider_id",
            "credential_key",
            name="uq_credentials_owner_provider_key",
        ),
    )
    # Every read filters by owner, so this is the hot lookup rather than a nicety.
    op.create_index("ix_credentials_owner_id", "credentials", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_credentials_owner_id", table_name="credentials")
    op.drop_table("credentials")
