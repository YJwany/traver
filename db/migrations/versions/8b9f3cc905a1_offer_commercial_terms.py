"""persist hotel offer commercial terms

Revision ID: 8b9f3cc905a1
Revises: 591c65cd5fef
"""

import sqlalchemy as sa
from alembic import op

revision = "8b9f3cc905a1"
down_revision = "591c65cd5fef"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hotel_offers", sa.Column("meal_plan", sa.String(length=120), nullable=True))
    op.add_column("hotel_offers", sa.Column("cancellation_policy", sa.Text(), nullable=True))
    op.add_column("hotel_offers", sa.Column("payment_policy", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("hotel_offers") as batch_op:
        batch_op.drop_column("payment_policy")
        batch_op.drop_column("cancellation_policy")
        batch_op.drop_column("meal_plan")
