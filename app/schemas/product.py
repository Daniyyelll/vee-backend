from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from app.domain.enums import Currency
from app.schemas.review import ReviewResponse


class ProductCreate(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        str_strip_whitespace=True,
    )

    product_name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    price: Decimal = Field(gt=0, max_digits=6, decimal_places=2)
    stock_quantity: int = Field(ge=0, le=2147483647)
    category_id: UUID


class ProductRead(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
    )

    id: UUID
    product_slug: str
    category_id: UUID
    currency: Currency
    product_name: str
    description: str | None
    price: Decimal
    stock_quantity: int = Field(ge=0)
    image_url: str | None
    image_urls: list[str] = Field(default_factory=list)
    active: bool = True


class ProductDetails(ProductRead):
    reviews: list[ReviewResponse] = Field(default_factory=list)


class ProductUpdate(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    product_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    price: Decimal | None = Field(default=None, gt=0, max_digits=6, decimal_places=2)
    stock_quantity: int | None = Field(default=None, ge=0, le=2147483647)
    category_id: UUID | None = None

    @model_validator(mode="after")
    def require_valid_change(self):
        changes = self.model_dump(exclude_unset=True)
        if not changes or any(
            value is None for key, value in changes.items() if key != "description"
        ):
            raise ValueError("Provide a valid product change")
        return self
