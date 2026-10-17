# Vee e-commerce API

FastAPI with asyncpg services and explicit PostgreSQL migrations. Run the API
with `uv run python main.py`; interactive API documentation is at
[localhost:8084/docs](http://localhost:8084/docs).

## Sign-in sessions

`POST /api/auth/login` returns the existing 15-minute bearer access token and
sets a 14-day `vee-refresh` cookie. The cookie is HttpOnly, scoped to
`/api/auth`, and contains an opaque random token. PostgreSQL stores only its
SHA-256 hash. The frontend keeps the access token in memory and calls
`POST /api/auth/refresh` with the cookie on reload or when the access token
expires. Each refresh creates a one-time successor in the same token family
and returns a new access token. Reuse of an older token revokes the entire
family and increments the account token version. The frontend must serialize
refresh calls in one tab to avoid treating concurrent refreshes as theft.
`POST /api/auth/logout` revokes that refresh family and clears
the cookie. A password change or reset invalidates all existing access and
refresh tokens through `token_version`.

Apply all migrations before deploying these endpoints. In production,
serve the API over HTTPS. `REFRESH_COOKIE_SECURE` defaults to true except for
loopback development; `REFRESH_COOKIE_SAMESITE` defaults to `lax`. If the
storefront and API are on different sites, set `REFRESH_COOKIE_SAMESITE=none`
and `REFRESH_COOKIE_SECURE=true`, and configure exact `FRONTEND_URL` and
`BACKEND_URL` origins. CORS permits credentials only from `FRONTEND_URL`.
Browser requests to login, refresh, and logout with an `Origin` header are
also checked against those configured origins. Keep access tokens out of
persistent browser storage. Logout ends refresh capability immediately; an
already-issued access token remains valid until its 15-minute expiry unless
the account's token version changes.

## Orders, reviews, and reports

All bodies accept camelCase and snake_case fields. Responses use the existing
`{status_code, message, data}` envelope and camelCase resource fields. Protected
routes require `Authorization: Bearer <access-token>`. List routes default to
`limit=50&offset=0`, with a maximum limit of 100.

| Method | Endpoint | Access and behavior |
| --- | --- | --- |
| POST | `/api/orders/quote` | Public; price supplied guest bag items without placing an order |
| POST | `/api/orders/cart-quote` | Authenticated; price the saved cart without placing an order |
| POST | `/api/orders/checkout` | Authenticated user's cart; creates a pending order |
| POST | `/api/orders/guest-checkout` | Public; creates a pending order from supplied items and contact details |
| GET | `/api/orders/receipt/{order_number}` | Public with a `Receipt-Token` header; limited confirmation details for 30 days |
| POST | `/api/coupons` | Admin; create a written or generated coupon |
| GET | `/api/coupons` | Admin; list coupons |
| GET | `/api/orders` | Customers see owned orders; delivery sees assigned orders; admins see all |
| GET | `/api/orders/{order_number}` | Same ownership and assignment rule as the list |
| PATCH | `/api/orders/{order_number}/delivery-assignment` | Admin assigns or unassigns one active delivery account |
| PATCH | `/api/orders/{order_number}/status` | Status transitions governed by role |
| GET | `/api/reviews?productId={uuid}` | Public, paginated product reviews |
| POST | `/api/reviews` | Delivered purchase required; one review per user/product |
| PATCH | `/api/reviews/{review_id}` | Owner may change rating, comment, or both |
| DELETE | `/api/reviews/{review_id}` | Owner or admin; returns an empty 204 response |
| POST | `/api/reports` | Report exactly one product or review |
| GET | `/api/reports` | User's reports; admin sees all; optional `status` filter |
| GET | `/api/reports/{report_id}` | Reporter or admin |
| PATCH | `/api/reports/{report_id}` | Admin resolves or dismisses an open report |
| GET | `/api/reports/sales` | Admin sales and order analytics |

Order identifiers in paths are the positive human-readable order number.
Review and report identifiers are UUIDs returned by their APIs. Order items also
include product UUIDs, which clients can use when submitting reviews or reports.

Checkout accepts:

```json
{"name":"Buyer","phone":"01012345678","shippingAddress":"12 Example Street, Cairo","couponCode":"WEEKEND10"}
```

The server calculates totals from current catalog prices, snapshots names and
prices into order items, reserves stock, and clears the cart in one transaction.
It locks the user, cart, and products to serialize conflicting operations.
Insufficient stock returns 409 and preserves the cart; an empty cart returns 400.
The same cart cannot be checked out twice concurrently. A fresh authenticated
cart may create another order; callers should prevent duplicate submissions.

Before checkout, send `{ "couponCode": "WEEKEND10" }` to the authenticated
`/api/orders/cart-quote`, or send a guest bag to `/api/orders/quote`:

```json
{"items":[{"productId":"<product UUID>","quantity":1}],"couponCode":"WEEKEND10"}
```

Both return current `items`, `subtotalPrice`, `shippingFee`,
`discountAmount`, `totalPrice`, `couponCode`, and `currency`. The public route
is limited to 60 requests per IP per minute. Quotes do not create orders,
reserve stock, or consume coupon uses. Their totals can change before checkout;
checkout verifies stock and coupon validity again inside its transaction.

Guest checkout takes the same `shippingAddress` and optional `couponCode`, plus
`name`, `email`, `phone`, and a nonempty `items` array:

```json
{"name":"Guest Buyer","email":"guest@example.com","phone":"01012345678","shippingAddress":"Cairo","items":[{"productId":"<product UUID>","quantity":1}]}
```

It does not create an account or cart. Send a unique `Idempotency-Key` header
(8-128 characters) and reuse it on retries. A retry returns the same order;
reusing the key with a different body returns 409. Staff can find guest orders
through the normal order list. Guests receive an email confirmation but do not
get full order lookup or cancellation access. The API limits guest checkout
to 10 requests per IP per hour; an edge limit is useful as well. Pending orders
expire after 24 hours, which releases reserved stock and cancels pending cash
payments unless staff have started processing them.

Both checkout responses include a `receiptToken`. The frontend keeps that
unguessable token and the order number in tab storage so a confirmation page can
reload without persisting the customer's contact details. Send the token in a
`Receipt-Token` header to `/api/orders/receipt/{order_number}`. That endpoint
returns only the amount, currency, recipient, address, phone, and delivery area;
it does not grant order management access. Receipts expire after 30 days, are
limited to 30 requests per IP per minute, and use `Cache-Control: no-store`.
Keep the token out of URLs, logs, and analytics.

New orders add a fixed 50 EGP shipping fee, configurable with `SHIPPING_FEE`.
If the store changes `PAYMENT_CURRENCY`, set `SHIPPING_FEE` in that currency too.
The response snapshots `subtotalPrice`, `shippingFee`, `discountAmount`, and
`totalPrice`. A percentage coupon discounts products only; a free-shipping
coupon discounts the full shipping fee. Both produce a single pending cash
payment for the final total.

Admins create a percentage coupon with `kind: "percent"` and a
`discountPercent` greater than 0 and below 100, or a shipping coupon with
`kind: "free_shipping"`. Omit `code` to generate a random one; otherwise supply
4–64 letters, digits, hyphens, or underscores. Codes are case insensitive.
`startsAt` is inclusive and `expiresAt` is exclusive. Set at least one of
`startsAt`, `expiresAt`, `assignedUserId`, or `maxUses`. A user-assigned
coupon works only at authenticated checkout for that registered customer.
`maxUses` is a shared limit across all customers and guests, from 1 to
2,147,483,647. A code without `assignedUserId` can be used by anyone who knows
it. To give one friend a single-use discount, create:

```json
{"code":"FRIEND10","kind":"percent","discountPercent":10,"maxUses":1}
```

For a group of three, use `"maxUses": 3` with a shared code. A written code can
be omitted to generate a random one. Admin coupon responses show `usesCount`
and `remainingUses`. A pending or fulfilled order occupies one use. Cancelling
an order releases its use; refunding a delivered order does not. A guest retry
with the same `Idempotency-Key` returns the original order and does not consume
a second use. Checkouts lock the coupon and count its non-cancelled orders
within the checkout transaction, so simultaneous requests cannot exceed the
limit.

Coupon validation, stock reservation, order creation, and payment insertion
share a database transaction. A rejected coupon leaves inventory unchanged.

Order status requests accept:

```json
{"status": "processing"}
```

Admin transitions are `pending -> processing -> shipped -> delivered`;
`pending` and `processing` may also transition to `cancelled`. Customers may
cancel only their own pending orders. Delivery staff may advance only their
assigned processing orders to shipped, then assigned shipped orders to delivered
after cash is recorded as collected. Completed and cancelled
orders are terminal. Repeating the current status is idempotent within these
role permissions. Invalid transitions return 409; forbidden actions return 403.
Cancellation restores reserved stock exactly once and cancels pending payments.
An order with a completed payment requires a recorded refund first. Order
routes do not collect or refund cash; use the staff payment endpoints below.

Confirmation, status, and reset emails enter a transactional database outbox.
A worker retries failed sends. SMTP failures cannot undo checkout. Delivery can
still duplicate a message if SMTP accepts it just before the worker crashes;
consumers should treat these emails as notifications, not payment receipts.

Review creation accepts:

```json
{"productId": "<product UUID>", "rating": 5, "comment": "Great product"}
```

Ratings must be 1-5. Reviews require at least one delivered order containing the
product. Duplicate reviews return 409. Updating or deleting another user's
review returns 404, except admins may delete reviews. Creating/deleting reviews
also updates the user's order-item `isReviewed` flags. Reviews are editable with
partial PATCH bodies; empty bodies and explicit nulls are rejected.

Customer reports accept exactly one target:

```json
{"reviewId": "<review UUID>", "reason": "Spam or misleading content"}
```

Use `productId` instead to report a product. One open report per reporter/target
is allowed; duplicates return 409. Admin moderation accepts:

```json
{"status": "resolved", "resolution": "Removed the reported spam review"}
```

Use `dismissed` to reject a report. Moderation requires a written resolution.
Closing a report does not automatically remove the target; review deletion is a
separate admin action. Closed reports cannot be changed; repeating the same
resolution is idempotent. Reports survive review deletion with `reviewId=null`
and `targetType=review`, preserving the reason and moderation history.

The sales endpoint accepts optional timezone-aware `start` and `end` timestamps:

```text
/api/reports/sales?start=2026-09-01T00:00:00Z&end=2026-10-01T00:00:00Z
```

The range includes `start` and excludes `end`, filtering by order creation time.
It returns total orders, counts and order values for all five statuses, delivered
sales, average delivered order value, and the top 10 products by delivered sales.
A repeatable-read snapshot keeps its sections consistent. Delivered sales are
order values, not collected payments or net revenue after refunds. The catalog
currently has no currency field; analytics assume a single catalog currency.

Public registration creates customer accounts only. Administrator and delivery
accounts must be provisioned through a trusted administrative process.

## Cash-on-delivery payments

Cash payments require no external payment provider, API key, hosted checkout,
or webhook. Checkout creates one pending cash payment in the same transaction
as the order. The amount is the order total calculated by the server. Order
responses include a `payment` resource; older orders without a payment return
`payment=null` until staff explicitly initialize their cash payment.

| Method | Endpoint | Access and behavior |
| --- | --- | --- |
| GET | `/api/payments/{order_number}` | Order owner, admin, or assigned delivery staff |
| POST | `/api/payments/{order_number}` | Admin/assigned delivery; initialize a legacy cash payment |
| POST | `/api/payments/{order_number}/collect` | Admin/assigned delivery; record full cash collection |
| POST | `/api/payments/{order_number}/refund` | Admin; record a full cash refund |

These paths use the order number, not the payment UUID. Initialization and
collection accept no payment fields; clients cannot choose the amount, currency,
provider, or payment status. The store currency defaults to `EGP` and can be set
with `PAYMENT_CURRENCY` (`EGP`, `SAR`, or `AED`). Each payment snapshots its
currency. Products and orders also snapshot currency. Keep one consistent catalog
currency; changing the store currency requires a catalog/data migration. Sales
reports require a currency filter when the requested period has mixed currencies
and expose refunded and net sales separately.

Initialize is an idempotent action for orders created before this integration.
It derives the amount from the order total and rejects cancelled orders. It
never overwrites an existing payment or infers historical cash collection.

Collection is allowed when an order is shipped or already delivered. Staff must
confirm that the full amount has actually been received, then call:

```text
POST /api/payments/123/collect
```

The payment changes from `pending` to `completed`, with `collectedAt` and
`collectedBy`. Retrying returns the same receipt, retaining the original staff
member and timestamp. Delivery cannot be recorded until the cash payment is
completed. For normal delivery, record cash collection first, then advance the
order to delivered. Customers can read their own payment but cannot record
collection or refunds.

For a cash refund, an admin must physically return the cash first, then record:

```text
POST /api/payments/123/refund
```

```json
{"reason": "Returned item; full cash amount handed back to customer"}
```

Only a completed payment on a delivered order can become `refunded`. The record retains its
collection audit and adds `refundedAt`, `refundedBy`, and `refundReason`.
Repeating the same request is idempotent; a different second refund returns 409.
Partial refunds are not supported. This endpoint records cash already returned;
it does not transfer money, change order status, or restock inventory. Product
returns/restocking remain a separate workflow. Refunded cash cannot be collected
again through this payment record.

Cancellation and payment actions lock the order before the payment. Cancellation
marks pending payments cancelled, and cancelled payments cannot be collected.
All writes run in a database transaction. A failure to create the payment rolls
back checkout, including inventory changes and cart clearing.

The cash migration enforces one positive-amount cash payment per order and adds
audit fields. Duplicate records, missing/nonpositive amounts, or old non-cash
transactions must be reconciled before upgrading; no financial history is
silently deleted or relabelled. The provider reference column is now optional
and retained only to preserve old references. Downgrading removes cash audit
columns but keeps payment records and nullable provider references.

## Landing image administration

The admin **Landing images** screen manages the fixed Hero and Ritual slots.
`GET /api/landing-images` is public. `PATCH /api/landing-images/{slot}`
requires an administrator and accepts multipart `imageFile` (optional),
`altText`, `caption`, `focalX`, and `focalY`. Saving publishes immediately
and records an audit event. The storefront keeps bundled artwork as a fallback
when the API or an image URL is unavailable.

Uploads must be PNG, JPEG, or WebP, at most 5 MiB and 25 megapixels, with
both dimensions at least 640 pixels. The backend creates 1024px and 640px
WebP variants, strips source metadata, and stores them at unique paths.
The configured Supabase bucket must allow public reads; only the backend
service role writes. `SUPABASE_SITE_BUCKET` defaults to the existing
`product-images` bucket and can point to a dedicated public bucket. Deploy
the database migration before the new API and frontend. Old versioned
objects remain in storage so pages already open during a publish keep
working; review their retention as part of storage operations.

## Product photos

Apply migration `d17f4b8e2a90` before deploying the gallery API. It backfills
each existing `image_url` into an ordered `image_urls` array. Product responses
expose `imageUrls` alongside `imageUrl`, which remains the first photo/cover
for collection cards and bags. Downgrading keeps the current cover.

Product creation accepts repeated multipart `imageFiles` fields (the legacy
single `imageFile` field still works). Each product supports up to ten photos;
each upload batch can total up to 5 MB. The existing PNG/JPEG/WebP validation
and 6 MiB request-body ceiling still apply.

These additional routes require an administrator and record audit events:

| Method | Route | Behavior |
| --- | --- | --- |
| POST | `/api/products/{slug}/images` | Append multipart `imageFiles` |
| PATCH | `/api/products/{slug}/images/cover?imageUrl={url}` | Move an existing photo to the cover position |
| DELETE | `/api/products/{slug}/images?imageUrl={url}` | Remove an existing photo; at least one must remain |

URL-encode the `imageUrl` query value. All three return the updated product.
Updates lock the product row to serialize concurrent edits and keep the cover
and gallery consistent. Failed uploads or database transactions clean up newly
uploaded files; successful removals delete the removed storage object after
the database commit.

## Database upgrades and validation

Apply migrations before using the new routes:

```powershell
uv run alembic upgrade head
```

The new migrations backfill purchased product names from the current catalog,
add review uniqueness/rating constraints and indexes, and create customer
reports. Historical product names cannot be recovered if they were already
changed before this upgrade. Existing duplicate reviews or ratings outside 1-5
must be corrected before upgrading; the migration deliberately preserves data
and fails rather than deleting feedback. Test upgrades on a disposable database.

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Database tests skip unless `TEST_DATABASE_URL` is set to a disposable PostgreSQL
DSN. They apply the migration SQL in randomly named temporary schemas and remove
those schemas afterward. They cover checkout rollback, stock races, repeated
cancellation, ownership, review races, reporting, and migration downgrade/upgrade.
HTTP and email tests use mocked integrations and never send real email.

## Production operations

Apply migrations on a backup copy of the database first. The historical slug
migration now backfills existing products; the hardening migration backfills
category slugs and order currencies and checks for case-insensitive duplicate
emails. The coupon usage migration counts existing non-cancelled coupon orders
from order history. Its downgrade rejects coupons that have only a usage limit,
since the prior schema cannot represent them. Resolve duplicate emails before upgrade. The migration also removes
the unique user-name constraint. Existing pending orders have no automatic
expiry; review and resolve these manually. Existing JWTs are invalidated when this
version is first deployed because tokens now carry a version, and password
changes invalidate all earlier tokens.

The app starts only after a database connection succeeds. `GET /api/health`
is a process liveness check; `GET /api/ready` checks the database. The
maintenance worker scans expired pending orders every 30 seconds and sends
outbox email with retries. Monitor readiness, worker errors, old unsent rows
in `email_outbox`, SMTP delivery failures, and the number of stale pending
orders. Request logs include an `X-Request-ID` response header. Do not log
reset links, request bodies, JWTs, or SMTP credentials.

Set `ENVIRONMENT=production`. Startup then fails unless frontend/backend URLs
use HTTPS, secure refresh cookies are enabled, `DATABASE_SSLMODE` requires TLS,
and an independent `CHECKOUT_HMAC_KEY` is present.
Set a long, random `CHECKOUT_HMAC_KEY` independently of `SECRET_JWT_KEY`
so rotating JWT credentials does not invalidate guest checkout retry keys or
active receipt tokens. Rotating the checkout key invalidates existing receipt
tokens, so plan that rotation around the 30-day receipt window.
Keep all three values out of version control. Set database connect, command,
statement, and pool-acquisition timeouts for the deployment. API documentation
is disabled in production, request bodies default to a 6 MiB ceiling, public
catalog reads are rate limited, and list offsets are capped. Enforce a tighter
body limit and connection/request timeouts at the reverse proxy too. Configure
the proxy to replace forwarded headers and allow Uvicorn to trust forwarded
headers only from that proxy's IP range.

Use a persistent, backed-up volume for PostgreSQL. The Docker Compose file is
local development infrastructure and pins PostgreSQL 18.4; changing a running
major version requires a planned database upgrade. Product images are stored
in the public Supabase bucket configured by `SUPABASE_PRODUCT_BUCKET`.
Keep the Supabase service-role key server-side. Include image storage and the database in
backup and restore drills. Use TLS at the reverse proxy and restrict database
and SMTP access to the app network. Run multiple Uvicorn worker processes under
the platform's process manager; `main.py` enables reload only in development.
Retain and monitor the append-only `audit_event` table for staff, order,
payment, moderation, and catalog changes according to the audit policy.

Run CI lint, a real Alembic upgrade, and the complete test suite before
deployment. Deploy the migration before starting new app processes. Confirm
`/api/ready`, then exercise a guest checkout retry, cancellation, coupon,
password reset email, cash collection, refund, and sales report with test data.
