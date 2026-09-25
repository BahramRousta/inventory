# Inventory Reservation Service

Interview assignment implementation for source-specific inventory reservations
during checkout.

## What is implemented

- internal and external inventory sources;
- atomic local stock holds that prevent overselling;
- asynchronous external HOLD/RELEASE with durable PostgreSQL claims and leases;
- reconciliation of ambiguous provider outcomes;
- TTL expiry and truthful `EXPIRED` vs `CANCELLED` terminal semantics;
- required `Idempotency-Key` request header with body fingerprint checking;
- trusted payment-outcome orchestration;
- immutable orders with order lines;
- persisted provider capability/configuration metadata;
- standalone fake HTTP provider with success, decline and
  timeout-after-side-effect scenarios;
- independent hold, release, reconciliation and expiry worker processes.

The configured demo provider uses **HOLD is final allocation** semantics. There
is no remote provider-confirm call.

## Run

```bash
docker compose up --build
```

Then seed products, sources, internal stock and the configured fake provider:

```bash
docker compose exec api python scripts/seed_demo.py
```

The API is on port 8000 and the fake provider is on port 9000.

The seeded external provider ID is:

```text
11111111-1111-1111-1111-111111111111
```

Compose configures every provider worker with that same ID and with
`http://fake-provider:9000`.

## Create a reservation

Use the source IDs printed by the seed command.

```bash
curl -i -X POST http://localhost:8000/reservations \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: user-123' \
  -H 'Idempotency-Key: checkout-001' \
  -d '{
    "items": [
      {
        "product_id": "<product-id>",
        "stock_source_id": "<source-id>",
        "quantity": 1
      }
    ]
  }'
```

A new internal-only reservation returns `201`. A reservation containing
external work returns `202` and a `Retry-After` hint. A settled idempotent
replay returns `200`. Reusing the same idempotency key with a changed
canonical request body returns `IDEMPOTENCY_CONFLICT`.

## Read reservation state

```bash
curl -H 'X-User-Id: user-123' \
  http://localhost:8000/reservations/<reservation-id>
```

The snapshot includes `created_at`, `expires_at`, `payment_allowed`,
`requires_attention`, and every line state.

## Submit payment outcome

Payment processing is outside this service. Submit only the trusted outcome:

```bash
curl -i -X POST \
  http://localhost:8000/reservations/<reservation-id>/payment-outcome \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: user-123' \
  -d '{
    "event_id": "<unique-event-uuid>",
    "outcome": "SUCCESS"
  }'
```

`SUCCESS` conditionally claims `ACTIVE -> CONFIRMING` before expiry,
consumes internal held stock, finalizes eligible external allocations, and
creates one immutable order with lines.

`FAILURE` moves an eligible reservation into `RELEASING`; compensation is
completed by the release/reconciliation workers.

The event ID is idempotent. Reusing the event ID with different content is a
conflict.

## Cancel

```bash
curl -i -X POST \
  -H 'X-User-Id: user-123' \
  http://localhost:8000/reservations/<reservation-id>/cancel
```

Cancellation is asynchronous when external releases are required.

## Administrative direct confirm

`POST /reservations/{id}/confirm` remains only as a deprecated administrative
compatibility endpoint. Checkout should use the payment-outcome endpoint.

## Fake provider scenarios

The fake provider begins in `success` mode.

Definitive decline:

```bash
curl -X POST http://localhost:9000/admin/mode/decline
```

Timeout after the provider has already created the hold:

```bash
curl -X POST http://localhost:9000/admin/mode/timeout_after_side_effect
```

The latter intentionally creates an ambiguous local result; the reconciliation
worker later discovers the provider-side hold through GET_HOLD.

Restore normal behavior:

```bash
curl -X POST http://localhost:9000/admin/mode/success
```

## Processes

Compose runs:

- API;
- fake HTTP provider;
- HOLD worker;
- RELEASE worker;
- reconciliation worker;
- expiry worker;
- one migration job;
- PostgreSQL.

Worker batch size, concurrency, lease length and polling interval are
configuration values. Defaults are documented in `SCALABILITY.md`.

## Design documents

- `docs/ARCHITECTURE.md`
- `docs/ASYNC_PROVIDER_STATE_MACHINE.md`
- `SCALABILITY.md`
- `REMEDIATION_WORKFLOW.md`

## Verification status

Automated end-to-end verification described in remediation Step 9 is
intentionally **not added or run in this branch**, per the explicit instruction
for this implementation pass.
