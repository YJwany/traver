"""User-supplied content ingestions.

Adds the table holding text a traveller supplied from a page they were already
reading — a 小红书 note, a 大众点评 review, a 携程 攻略. None of those platforms
offers a public API, so the text is supplied by the traveller rather than fetched,
and this table is where it lives.

Two columns are worth explaining, because both encode a refusal to guess:

``content_text`` keeps the supplied text verbatim. The extracted facts are only
checkable against it, and an extraction nobody can check is an assertion rather
than a citation.

``published_at`` is nullable, and the null is the point: it records that the page
did not state when the advice was written. Defaulting it to the ingestion time
would have made every note look current, which is the exact error the freshness
work removed from the provider path — here it would misrepresent the traveller's
own material.

``facts_json`` is JSON rather than a child table because a note's facts are always
read whole, never queried individually, and never joined against.
"""

import sqlalchemy as sa
from alembic import op

revision = "a1f7c93e5b20"
down_revision = "e7c4b1a90d52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_ingestions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.String(length=120), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("facts_json", sa.JSON(), nullable=False),
        sa.Column("notes_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # Owner scoping is the hot path: a note belongs to the person who saved it, and
    # every read filters on this column.
    op.create_index("ix_content_ingestions_owner_id", "content_ingestions", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_content_ingestions_owner_id", table_name="content_ingestions")
    op.drop_table("content_ingestions")
