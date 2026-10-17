import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

import asyncpg
from fastapi import UploadFile, status

from app.core.config import settings
from app.core.exceptions import APIException
from app.schemas.product import ProductCreate, ProductRead, ProductUpdate
from app.services.category import get_category_id_from_slug
from app.services.upload import MAX_IMAGE_SIZE, delete_uploaded_file, upload_file
from app.utils.slugify import slugify

product_cols = """
    id, product_slug, category_id, product_name, description,
    price, stock_quantity, image_url, image_urls, active, currency::text AS currency
"""

MAX_PRODUCT_IMAGES = 10


@asynccontextmanager
async def uploaded_product_images(files: list[UploadFile]):
    """Compensate storage uploads if validation, DB writes or commit fail."""
    if not 1 <= len(files) <= MAX_PRODUCT_IMAGES:
        raise APIException(
            "Choose between 1 and 10 photos.", status.HTTP_422_UNPROCESSABLE_ENTITY
        )
    if sum(file.size or 0 for file in files) > MAX_IMAGE_SIZE:
        raise APIException(
            "Photos must total 5 MB or smaller per upload.",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )
    paths: list[str] = []
    try:
        for file in files:
            paths.append(await upload_file(file))
        yield paths
    except BaseException:
        for path in paths:
            await delete_uploaded_file(path)
        raise


# helpers
async def get_product_id_by_slug(db: asyncpg.Connection, product_slug: str) -> UUID:
    product_id = await db.fetchval(
        "SELECT id FROM product WHERE product_slug = $1",
        product_slug,
    )
    if product_id is None:
        raise APIException("Product not found.", status.HTTP_404_NOT_FOUND)
    return product_id


async def create_product(
    db: asyncpg.Connection, product: ProductCreate, image_paths: list[str]
):
    try:
        row = await db.fetchrow(
            """
            INSERT INTO product (
                id, product_name, description, price, stock_quantity,
                image_url, created_at, category_id, product_slug, currency, image_urls
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING id, product_name, description, price, stock_quantity,
                      image_url, created_at, category_id, product_slug,
                      image_urls, active, currency::text AS currency
    """,
            uuid.uuid4(),
            product.product_name,
            product.description,
            product.price,
            product.stock_quantity,
            image_paths[0],
            datetime.now(timezone.utc),
            product.category_id,
            slugify(product.product_name),
            settings.payment_currency.value,
            image_paths,
        )

    except asyncpg.ForeignKeyViolationError as exc:
        raise APIException(
            "The selected category does not exist.",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        ) from exc
    except asyncpg.UniqueViolationError as exc:
        raise APIException(
            "A product with this name already exists.", status.HTTP_409_CONFLICT
        ) from exc

    return dict(row)


async def change_product_images(
    db: asyncpg.Connection,
    product_slug: str,
    *,
    added: list[str] | None = None,
    removed: str | None = None,
    cover: str | None = None,
) -> ProductRead:
    # The caller holds a transaction through the audit write and commit.
    row = await db.fetchrow(
        f"SELECT {product_cols} FROM product "
        "WHERE product_slug = $1 AND active = TRUE FOR UPDATE",
        product_slug,
    )
    if row is None:
        raise APIException("Product not found.", status.HTTP_404_NOT_FOUND)
    # Older app processes may still create a single-photo product during rollout.
    images = list(row["image_urls"]) or ([row["image_url"]] if row["image_url"] else [])
    if added:
        images.extend(added)
    if len(images) > MAX_PRODUCT_IMAGES:
        raise APIException(
            "A product can have at most 10 photos.",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    selected = removed if removed is not None else cover
    if selected is not None:
        if selected not in images:
            raise APIException("Product photo not found.", status.HTTP_404_NOT_FOUND)
        if removed is not None and len(images) == 1:
            raise APIException(
                "Keep at least one product photo.", status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        images.remove(selected)
        if cover is not None:
            images.insert(0, selected)
    updated = await db.fetchrow(
        "UPDATE product SET image_urls = $1, image_url = $2 "
        f"WHERE id = $3 RETURNING {product_cols}",
        images,
        images[0] if images else None,
        row["id"],
    )
    return ProductRead.model_validate(dict(updated))


async def get_all_products(
    db: asyncpg.Connection,
    category_slug: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ProductRead]:
    if category_slug is not None:
        category_id = await get_category_id_from_slug(db, category_slug)

        rows = await db.fetch(
            f"""
            SELECT {product_cols} FROM product
            WHERE category_id = $1 AND active = TRUE
            ORDER BY created_at DESC, id DESC LIMIT $2 OFFSET $3
            """,
            category_id,
            limit,
            offset,
        )

    else:
        rows = await db.fetch(
            f"""
            SELECT {product_cols} FROM product WHERE active = TRUE
            ORDER BY created_at DESC, id DESC LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )

    return [ProductRead.model_validate(dict(product)) for product in rows]


async def get_product_by_slug(db: asyncpg.Connection, product_slug: str) -> ProductRead:
    product_id = await get_product_id_by_slug(db, product_slug)

    product = await db.fetchrow(
        f"""
        SELECT {product_cols}
        FROM product
        WHERE id = $1 AND active = TRUE;
        """,
        product_id,
    )

    if product is None:
        raise APIException("Product not found.", status.HTTP_404_NOT_FOUND)
    return ProductRead.model_validate(dict(product))


async def update_product(
    db: asyncpg.Connection, product_slug: str, product: ProductUpdate
):
    product_name: bool = False
    changes = product.model_dump(exclude_unset=True)

    assignments: list[str] = []
    values: list[object] = []

    for field, value in changes.items():
        if field == "product_name":
            product_name = True
        values.append(value)
        assignments.append(f"{field} = ${len(values)}")

    if not assignments:
        raise APIException(
            "Provide at least one product field.", status.HTTP_400_BAD_REQUEST
        )

    if product_name:
        # Maintain Slug when updating Name
        values.append(slugify(product.product_name))
        assignments.append(f"product_slug = ${len(values)}")

    product_id = await get_product_id_by_slug(db, product_slug)
    values.append(product_id)

    try:
        row = await db.fetchrow(
            f"""
            UPDATE product
            SET {", ".join(assignments)}
            WHERE id = ${len(values)}
            RETURNING {product_cols}
            """,
            *values,
        )
    except asyncpg.UniqueViolationError as exc:
        raise APIException(
            "Product name or slug already exists.", status.HTTP_409_CONFLICT
        ) from exc
    except asyncpg.ForeignKeyViolationError as exc:
        raise APIException(
            "The selected category does not exist.",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        ) from exc
    if row is None:
        raise APIException("Product not found.", status.HTTP_404_NOT_FOUND)

    return ProductRead.model_validate(dict(row))


async def delete_product(db: asyncpg.Connection, product_slug: str):
    product_id = await get_product_id_by_slug(db, product_slug)

    info = await db.execute(
        """
        UPDATE product SET active = FALSE WHERE id = $1;""",
        product_id,
    )
    return info
