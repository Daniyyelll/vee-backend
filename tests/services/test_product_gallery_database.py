import asyncio
import importlib
import os
from uuid import uuid4

import asyncpg
import pytest

from app.services.product import change_product_images, get_product_by_slug


def test_migration_preserves_covers_and_gallery_edits_persist(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL database")
    migration = importlib.import_module(
        "migrations.versions.d17f4b8e2a90_product_gallery"
    )
    queries = []
    monkeypatch.setattr(migration.op, "execute", queries.append)

    async def run():
        db = await asyncpg.connect(url)
        schema = f"test_gallery_{uuid4().hex}"
        try:
            await db.execute(f'CREATE SCHEMA "{schema}"')
            await db.execute(f'SET search_path TO "{schema}"')
            await db.execute("""
                CREATE TABLE product (
                    id UUID PRIMARY KEY, product_slug TEXT UNIQUE,
                    category_id UUID, product_name TEXT, description TEXT,
                    price NUMERIC(6,2), stock_quantity INTEGER,
                    image_url TEXT, active BOOLEAN DEFAULT TRUE,
                    currency TEXT DEFAULT 'EGP'
                )
            """)
            await db.execute(
                "INSERT INTO product (id, product_slug, category_id, product_name, "
                "price, stock_quantity, image_url) "
                "VALUES ($1, 'balm', $2, 'Balm', 170, 3, '/cover.webp'), "
                "($3, 'no-photo', $2, 'No photo', 170, 3, NULL)",
                uuid4(),
                uuid4(),
                uuid4(),
            )
            migration.upgrade()
            async with db.transaction():
                for query in queries:
                    await db.execute(query)
            assert (await get_product_by_slug(db, "balm")).image_urls == ["/cover.webp"]
            assert (await get_product_by_slug(db, "no-photo")).image_urls == []
            async with db.transaction():
                await change_product_images(db, "balm", added=["/side.webp"])
            async with db.transaction():
                await change_product_images(db, "balm", cover="/side.webp")
            result = await get_product_by_slug(db, "balm")
            assert result.image_url == "/side.webp"
            assert result.image_urls == ["/side.webp", "/cover.webp"]
            async with db.transaction():
                await change_product_images(db, "balm", removed="/side.webp")
            assert (await get_product_by_slug(db, "balm")).image_url == "/cover.webp"
            with pytest.raises(asyncpg.CheckViolationError):
                await db.execute("UPDATE product SET image_urls = $1", ["photo"] * 11)
            queries.clear()
            migration.downgrade()
            for query in queries:
                await db.execute(query)
            assert (
                await db.fetchval(
                    "SELECT image_url FROM product WHERE product_slug = 'balm'"
                )
                == "/cover.webp"
            )
        finally:
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')
            await db.close()

    asyncio.run(run())
