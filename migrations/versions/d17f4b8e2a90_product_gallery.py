"""Store ordered product photos while preserving the existing cover URL.

Revision ID: d17f4b8e2a90
Revises: ca4e9d7b21f0
"""

from alembic import op

revision = "d17f4b8e2a90"
down_revision = "ca4e9d7b21f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE product ADD COLUMN image_urls TEXT[] NOT NULL DEFAULT '{}'
            CHECK (cardinality(image_urls) <= 10)
    """)
    op.execute("""
        UPDATE product SET image_urls = ARRAY[image_url]
        WHERE image_url IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE product DROP COLUMN image_urls")
