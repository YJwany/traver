"""API keys and trip ownership.

Adds the credential store and the owner column that scopes trips to a principal.

``owner_id`` is nullable on purpose: trips created before authentication existed
must remain readable and become owned on the first authenticated write, rather
than being orphaned or deleted by the migration.
"""

import sqlalchemy as sa
from alembic import op

revision = "d4f2a7c81b03"
down_revision = "c3a1d2e4f607"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("key_prefix", sa.String(length=24), nullable=False),
        sa.Column("owner_id", sa.String(length=120), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # The model declares ``unique=True, index=True``, which SQLAlchemy renders as
    # a single *unique index*. Emitting a separate UniqueConstraint plus a
    # non-unique index instead leaves a schema that autogenerate reports as
    # drifted — a unique constraint and a unique index are not interchangeable.
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)
    # Indexed because every trip list and ownership check filters on it.
    op.create_index("ix_api_keys_owner_id", "api_keys", ["owner_id"])

    op.add_column("trips", sa.Column("owner_id", sa.String(length=120), nullable=True))
    op.create_index("ix_trips_owner_id", "trips", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_trips_owner_id", table_name="trips")
    op.drop_column("trips", "owner_id")
    op.drop_index("ix_api_keys_owner_id", table_name="api_keys")
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_table("api_keys")
