from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth as auth_api, product as product_api, review as review_api
from app.api.api import api_router
from app.api.dependencies import get_current_user
from app.core.exceptions import APIException, api_exception_handler
from app.core.security import create_access_token
from app.db.session import get_connection
from app.schemas.product import ProductRead
from app.schemas.review import ReviewResponse
from app.schemas.user import TokenData
from app.services.category import get_category_id_from_slug


@pytest.fixture
def storefront(monkeypatch):
    app = FastAPI()
    app.include_router(api_router)
    app.add_exception_handler(APIException, api_exception_handler)
    db = AsyncMock()

    @asynccontextmanager
    async def transaction():
        yield

    db.transaction = transaction
    db.fetchval.return_value = 0
    app.dependency_overrides[get_connection] = lambda: db
    user = {
        "id": uuid4(),
        "name": "Buyer",
        "email": "buyer@example.com",
        "role": "customer",
        "active": True,
        "address": None,
    }
    product = ProductRead(
        id=uuid4(),
        product_slug="lip-balm",
        category_id=uuid4(),
        currency="EGP",
        product_name="Lip Balm",
        description=None,
        price=Decimal("170.00"),
        stock_quantity=100,
        image_url="/uploads/lip.png",
    )
    monkeypatch.setattr(
        product_api, "get_all_products", AsyncMock(return_value=[product])
    )
    monkeypatch.setattr(
        product_api, "get_product_by_slug", AsyncMock(return_value=product)
    )
    remove = AsyncMock()
    monkeypatch.setattr(product_api, "delete_product", remove)
    monkeypatch.setattr(
        auth_api,
        "login_user",
        AsyncMock(return_value=TokenData(token="test-credential", user=user)),
    )
    with TestClient(app) as client:
        yield client, app, db, user, product, remove


@pytest.mark.parametrize("path", ["/api/products", "/api/products/lip-balm"])
def test_product_contract_exposes_identity_category_and_currency(storefront, path):
    client, _, _, _, product, _ = storefront
    response = client.get(path)
    assert response.status_code == 200
    data = response.json()["data"]
    if isinstance(data, list):
        data = data[0]
    assert data["id"] == str(product.id)
    assert data["categoryId"] == str(product.category_id)
    assert data["productSlug"] == "lip-balm"
    assert data["price"] == "170.00"
    assert data["currency"] == "EGP"
    assert data["imageUrl"] == "/uploads/lip.png"
    assert data["imageUrls"] == []


@pytest.mark.parametrize("field,count", [("imageFile", 1), ("imageFiles", 2)])
def test_admin_creates_gallery_with_legacy_or_multiple_uploads(
    storefront, monkeypatch, field, count
):
    from app.services import product as product_service

    client, app, _, user, product, _ = storefront
    app.dependency_overrides[get_current_user] = lambda: {**user, "role": "admin"}
    paths = [f"/photo-{i}.webp" for i in range(count)]
    monkeypatch.setattr(product_service, "upload_file", AsyncMock(side_effect=paths))
    monkeypatch.setattr(
        product_api,
        "get_category_id_from_slug",
        AsyncMock(return_value=product.category_id),
    )
    created = product.model_copy(update={"image_url": paths[0], "image_urls": paths})
    create = AsyncMock(return_value=created.model_dump())
    monkeypatch.setattr(product_api, "create_product", create)
    monkeypatch.setattr(product_api, "record_audit", AsyncMock())
    response = client.post(
        "/api/products",
        data={
            "productName": "Lip Balm",
            "price": "170.00",
            "stockQuantity": "2",
            "categorySlug": "lips",
        },
        files=[
            (field, (f"photo-{i}.webp", b"image", "image/webp")) for i in range(count)
        ],
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["imageUrls"] == paths
    assert response.json()["data"]["imageUrl"] == paths[0]
    assert create.await_args.args[2] == paths


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        (
            "post",
            "/api/products/lip-balm/images",
            {"files": {"imageFiles": ("x.webp", b"image", "image/webp")}},
        ),
        ("delete", "/api/products/lip-balm/images?imageUrl=x", {}),
        ("patch", "/api/products/lip-balm/images/cover?imageUrl=x", {}),
    ],
)
def test_product_gallery_mutations_require_admin(storefront, method, path, kwargs):
    client, app, _, user, _, _ = storefront
    assert getattr(client, method)(path, **kwargs).status_code in (401, 403)
    app.dependency_overrides[get_current_user] = lambda: user
    assert getattr(client, method)(path, **kwargs).status_code == 403


def test_audit_failure_rolls_back_storage_uploads(storefront, monkeypatch):
    from app.services import product as product_service

    client, app, _, user, product, _ = storefront
    app.dependency_overrides[get_current_user] = lambda: {**user, "role": "admin"}
    monkeypatch.setattr(
        product_service, "upload_file", AsyncMock(return_value="/new.webp")
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(product_service, "delete_uploaded_file", cleanup)
    monkeypatch.setattr(
        product_api, "change_product_images", AsyncMock(return_value=product)
    )
    monkeypatch.setattr(
        product_api, "record_audit", AsyncMock(side_effect=RuntimeError("audit failed"))
    )
    with pytest.raises(RuntimeError, match="audit failed"):
        client.post(
            "/api/products/lip-balm/images",
            files={"imageFiles": ("x.webp", b"image", "image/webp")},
        )
    cleanup.assert_awaited_once_with("/new.webp")


def test_only_admin_can_inspect_a_reported_review(storefront, monkeypatch):
    client, app, _, user, product, _ = storefront
    review = ReviewResponse(
        id=uuid4(),
        rating=2,
        comment="Packaging concern",
        username="Buyer",
        created_at=datetime.now(timezone.utc),
        product_id=product.id,
        user_id=user["id"],
    )
    lookup = AsyncMock(return_value=review)
    monkeypatch.setattr(review_api, "get_review", lookup)
    app.dependency_overrides[get_current_user] = lambda: user
    assert client.get(f"/api/reviews/{review.id}").status_code == 403
    app.dependency_overrides[get_current_user] = lambda: {**user, "role": "admin"}
    response = client.get(f"/api/reviews/{review.id}")
    assert response.status_code == 200
    assert response.json()["data"]["comment"] == "Packaging concern"
    lookup.assert_awaited_once()


def test_login_and_profile_include_user_identity_for_review_ownership(storefront):
    client, app, _, user, _, _ = storefront
    response = client.post(
        "/api/auth/login",
        json={
            "email": user["email"],
            "password": "example-password",
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["user"]["id"] == str(user["id"])
    app.dependency_overrides[get_current_user] = lambda: user
    assert client.get("/api/users/me").json()["data"]["id"] == str(user["id"])


def test_profile_verifies_bearer_identity_and_rejects_inactive_user(storefront):
    client, _, db, user, _, _ = storefront
    db.fetchrow.return_value = user
    token = create_access_token({"sub": str(user["id"]), "ver": 0})
    headers = {"Authorization": f"Bearer {token}"}
    response = client.get("/api/users/me", headers=headers)
    assert response.status_code == 200
    assert response.json()["data"]["id"] == str(user["id"])
    assert db.fetchrow.await_args.args[1] == user["id"]
    user["active"] = False
    assert client.get("/api/users/me", headers=headers).status_code == 401
    assert (
        client.get(
            "/api/users/me", headers={"Authorization": "Bearer invalid-credential"}
        ).status_code
        == 401
    )


def test_cors_allows_configured_origin_and_rejects_another_origin():
    from app.core.config import settings
    from app.main import app

    client = TestClient(app)
    headers = {
        "Origin": settings.FRONTEND_URL.rstrip("/"),
        "Access-Control-Request-Method": "PATCH",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    response = client.options("/api/users/update-profile", headers=headers)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == headers["Origin"]
    assert response.headers["access-control-allow-credentials"] == "true"
    headers["Origin"] = "https://unrelated.example"
    assert (
        client.options("/api/users/update-profile", headers=headers).status_code == 400
    )


def test_product_deletion_requires_admin_and_returns_empty_204(storefront):
    client, app, _, user, _, remove = storefront
    assert client.delete("/api/products/lip-balm").status_code in (401, 403)
    remove.assert_not_awaited()
    app.dependency_overrides[get_current_user] = lambda: user
    assert client.delete("/api/products/lip-balm").status_code == 403
    remove.assert_not_awaited()
    user["role"] = "admin"
    response = client.delete("/api/products/lip-balm")
    assert response.status_code == 204
    assert response.content == b""
    remove.assert_awaited_once()


def test_category_lookup_uses_indexed_slug():
    import asyncio

    category_id = uuid4()
    db = AsyncMock()
    db.fetchval.side_effect = [category_id, None]
    assert asyncio.run(get_category_id_from_slug(db, "lip-care")) == category_id
    assert asyncio.run(get_category_id_from_slug(db, "missing")) is None
    assert "WHERE slug = $1" in db.fetchval.await_args.args[0]
