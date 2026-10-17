import asyncio
from io import BytesIO
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import UploadFile

from app.core.exceptions import APIException
from app.services import product as service


def product_row(images):
    return {
        "id": uuid4(),
        "product_slug": "balm",
        "category_id": uuid4(),
        "product_name": "Balm",
        "description": None,
        "price": "170.00",
        "stock_quantity": 3,
        "currency": "EGP",
        "image_url": images[0] if images else None,
        "image_urls": images,
    }


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"added": ["third"]}, ["first", "second", "third"]),
        ({"removed": "first"}, ["second"]),
        ({"cover": "second"}, ["second", "first"]),
    ],
)
def test_gallery_updates_keep_cover_and_order_consistent(change, expected):
    row = product_row(["first", "second"])
    db = AsyncMock()
    db.fetchrow.side_effect = [
        row,
        {**row, "image_url": expected[0], "image_urls": expected},
    ]
    result = asyncio.run(service.change_product_images(db, "balm", **change))
    assert "FOR UPDATE" in db.fetchrow.await_args_list[0].args[0]
    assert db.fetchrow.await_args.args[1:] == (expected, expected[0], row["id"])
    assert result.image_urls == expected
    assert result.image_url == expected[0]


@pytest.mark.parametrize(
    "images,change,status",
    [
        (["first"], {"removed": "first"}, 422),
        (["first"], {"removed": "unknown"}, 404),
        (["first"], {"cover": "unknown"}, 404),
        ([str(i) for i in range(10)], {"added": ["overflow"]}, 422),
    ],
)
def test_gallery_rejects_invalid_changes_without_writing(images, change, status):
    db = AsyncMock()
    db.fetchrow.return_value = product_row(images)
    with pytest.raises(APIException) as error:
        asyncio.run(service.change_product_images(db, "balm", **change))
    assert error.value.status_code == status
    assert db.fetchrow.await_count == 1


def test_adding_photo_preserves_cover_created_by_older_app_process():
    row = product_row(["cover"])
    row["image_urls"] = []
    db = AsyncMock()
    db.fetchrow.side_effect = [row, {**row, "image_urls": ["cover", "new"]}]
    result = asyncio.run(service.change_product_images(db, "balm", added=["new"]))
    assert result.image_urls == ["cover", "new"]
    assert db.fetchrow.await_args.args[1:3] == (["cover", "new"], "cover")


@pytest.mark.parametrize("failure", ["upload", "commit"])
def test_failed_upload_or_commit_cleans_up_all_new_photos(monkeypatch, failure):
    upload = AsyncMock(side_effect=["first", RuntimeError("invalid photo")])
    if failure == "commit":
        upload.side_effect = ["first", "second"]
    cleanup = AsyncMock()
    monkeypatch.setattr(service, "upload_file", upload)
    monkeypatch.setattr(service, "delete_uploaded_file", cleanup)
    files = [UploadFile(BytesIO(b"image"), size=5) for _ in range(2)]

    async def run():
        async with service.uploaded_product_images(files):
            raise RuntimeError("commit failed")

    with pytest.raises(RuntimeError):
        asyncio.run(run())
    assert [call.args[0] for call in cleanup.await_args_list] == (
        ["first"] if failure == "upload" else ["first", "second"]
    )


@pytest.mark.parametrize(
    "sizes,status", [([], 422), ([1] * 11, 422), ([3_000_000] * 2, 413)]
)
def test_upload_limits_are_checked_before_storage(monkeypatch, sizes, status):
    upload = AsyncMock()
    monkeypatch.setattr(service, "upload_file", upload)
    files = [UploadFile(BytesIO(), size=size) for size in sizes]

    async def run():
        async with service.uploaded_product_images(files):
            pass

    with pytest.raises(APIException) as error:
        asyncio.run(run())
    assert error.value.status_code == status
    upload.assert_not_awaited()
