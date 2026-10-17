from decimal import Decimal

import asyncpg
from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status

from app.api.dependencies import is_admin
from app.core.exceptions import APIException
from app.db.session import get_connection
from app.schemas.product import ProductCreate, ProductRead, ProductUpdate
from app.schemas.response import APIResponse
from app.services.audit import record_audit
from app.services.category import get_category_id_from_slug
from app.services.product import (
    change_product_images,
    create_product,
    delete_product,
    get_all_products,
    get_product_by_slug,
    update_product,
    uploaded_product_images,
)
from app.services.upload import delete_uploaded_file

router = APIRouter(prefix="/products", tags=["products"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_product(
    product_name: str = Form(alias="productName"),
    price: Decimal = Form(),
    stock_quantity: int = Form(alias="stockQuantity", ge=0),
    category_slug: str = Form(alias="categorySlug"),
    description: str | None = Form(default=None),
    image_file: UploadFile | None = File(default=None, alias="imageFile"),
    image_files: list[UploadFile] | None = File(default=None, alias="imageFiles"),
    db: asyncpg.Connection = Depends(get_connection),
    admin: dict = Depends(is_admin),
) -> APIResponse[ProductRead]:

    category_id = await get_category_id_from_slug(db, category_slug)

    if category_id is None:
        raise APIException(
            status_code=status.HTTP_404_NOT_FOUND,
            message="Category not found",
        )

    product = ProductCreate(
        product_name=product_name,
        description=description,
        price=price,
        stock_quantity=stock_quantity,
        category_id=category_id,
    )

    files = ([image_file] if image_file is not None else []) + (image_files or [])
    async with uploaded_product_images(files) as paths:
        async with db.transaction():
            created_product = await create_product(db, product, paths)
            await record_audit(
                db, admin, "product.created", "product", created_product["id"]
            )

    return APIResponse(
        status_code=status.HTTP_201_CREATED,
        message="Product Created",
        data=ProductRead.model_validate(created_product),
    )


@router.get("", status_code=status.HTTP_200_OK)
async def get_products(
    category_slug: str | None = Query(default=None, alias="categorySlug"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    db: asyncpg.Connection = Depends(get_connection),
) -> APIResponse[list[ProductRead]]:
    products = await get_all_products(db, category_slug, limit, offset)

    return APIResponse(
        status_code=status.HTTP_200_OK,
        message="Products",
        data=products,
    )


@router.patch("/{product_slug}", status_code=status.HTTP_200_OK)
async def edit_product(
    product_slug: str,
    product_changes: ProductUpdate,
    db: asyncpg.Connection = Depends(get_connection),
    admin: dict = Depends(is_admin),
) -> APIResponse[ProductRead]:
    async with db.transaction():
        info = await update_product(db, product_slug, product_changes)
        await record_audit(db, admin, "product.updated", "product", info.id)
    return APIResponse(
        status_code=status.HTTP_200_OK, message="Product Updated", data=info
    )


@router.get("/{product_slug}", status_code=status.HTTP_200_OK)
async def get_product(
    product_slug: str,
    db: asyncpg.Connection = Depends(get_connection),
) -> APIResponse[ProductRead]:
    product = await get_product_by_slug(db, product_slug)
    return APIResponse(status_code=status.HTTP_200_OK, message="Product", data=product)


@router.delete("/{product_slug}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_product(
    product_slug: str,
    db: asyncpg.Connection = Depends(get_connection),
    admin: dict = Depends(is_admin),
) -> Response:
    async with db.transaction():
        await delete_product(db, product_slug)
        await record_audit(db, admin, "product.deactivated", "product", product_slug)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{product_slug}/images")
async def add_product_images(
    product_slug: str,
    image_files: list[UploadFile] = File(alias="imageFiles"),
    db: asyncpg.Connection = Depends(get_connection),
    admin: dict = Depends(is_admin),
) -> APIResponse[ProductRead]:
    await get_product_by_slug(db, product_slug)
    async with uploaded_product_images(image_files) as paths:
        async with db.transaction():
            product = await change_product_images(db, product_slug, added=paths)
            await record_audit(db, admin, "product.photos_added", "product", product.id)
    return APIResponse(status_code=200, message="Product photos added", data=product)


@router.delete("/{product_slug}/images")
async def remove_product_image(
    product_slug: str,
    image_url: str = Query(alias="imageUrl"),
    db: asyncpg.Connection = Depends(get_connection),
    admin: dict = Depends(is_admin),
) -> APIResponse[ProductRead]:
    async with db.transaction():
        product = await change_product_images(db, product_slug, removed=image_url)
        await record_audit(db, admin, "product.photo_removed", "product", product.id)
    await delete_uploaded_file(image_url)
    return APIResponse(status_code=200, message="Product photo removed", data=product)


@router.patch("/{product_slug}/images/cover")
async def set_product_cover(
    product_slug: str,
    image_url: str = Query(alias="imageUrl"),
    db: asyncpg.Connection = Depends(get_connection),
    admin: dict = Depends(is_admin),
) -> APIResponse[ProductRead]:
    async with db.transaction():
        product = await change_product_images(db, product_slug, cover=image_url)
        await record_audit(db, admin, "product.cover_changed", "product", product.id)
    return APIResponse(status_code=200, message="Product cover changed", data=product)
