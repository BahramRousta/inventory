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
- final order creation linked to the reservation;
- one provider port with a single `reserve(...)` operation; each provider
  hides whether it uses an availability query or a real reservation/HOLD API;
- simple configurable mock provider outcomes for interview/demo scenarios;
- independent hold, release, reconciliation and expiry worker processes.

Provider-specific mechanics are hidden behind `InventoryProvider.reserve()`.
A query-style provider can implement it with an availability check, while a
reservation-style provider can implement it with HOLD semantics.

## Run

```bash
docker compose up --build
```

Then seed products, sources, internal stock and the configured mock provider identity:

```bash
docker compose exec api python scripts/seed_demo.py
```

The API is on port 8000. Provider behavior is mocked in-process for this interview assignment.

The seeded external provider ID is:

```text
11111111-1111-1111-1111-111111111111
```

Compose configures every provider worker with that same provider ID. The factory selects the configured provider implementation for that ID.
Create flow only validates that the provider/source are enabled; the worker
loads the provider later and calls the same `reserve()` contract.

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

## Confirm a reservation

Payment processing is outside this service. After checkout/payment succeeds,
the caller confirms the reservation:

```bash
curl -i -X POST \
  -H 'X-User-Id: user-123' \
  http://localhost:8000/reservations/<reservation-id>/confirm
```

Confirmation consumes held internal inventory, marks reservation lines
confirmed, and creates the final order.

## Cancel

If checkout fails or is abandoned, the caller cancels the reservation:

```bash
curl -i -X POST \
  -H 'X-User-Id: user-123' \
  http://localhost:8000/reservations/<reservation-id>/cancel
```

Cancellation is asynchronous when external releases are required. Uncancelled
reservations are also released by TTL expiry.

## Mock provider scenarios

The assignment provider is intentionally simple. Set `MOCK_PROVIDER_MODE` to:

- `success`: HOLD and RELEASE succeed;
- `decline`: HOLD is definitively declined;
- `unknown`: HOLD/RELEASE/status lookup return ambiguous outcomes.

The PostgreSQL E2E tests also configure the mock object directly to demonstrate
reconciliation transitions without building a production HTTP integration.

## Processes

Compose runs:

- API;
- HOLD worker;
- RELEASE worker;
- reconciliation worker;
- expiry worker;
- one migration job;
- PostgreSQL.

Worker batch size, concurrency, lease length and polling interval are
configuration values. Provider selection for the demo uses `EXTERNAL_PROVIDER_ID` and
`MOCK_PROVIDER_MODE`. Real endpoint/authentication configuration is
deliberately outside this interview implementation. Defaults are documented in
`SCALABILITY.md`.

## Design documents

- `ARCHITECTURE.md` (submission deliverable)
- `docs/ARCHITECTURE.md` (same architecture baseline under docs)
- `docs/ASYNC_PROVIDER_STATE_MACHINE.md`
- `SCALABILITY.md`
- `REMEDIATION_WORKFLOW.md`

## Verification

The Step 9 suite is PostgreSQL-backed and asserts persisted database state in
every E2E/API scenario. Provider behavior is exercised through deterministic mock gateways while all
reservation and worker state is asserted against real PostgreSQL.

Start a disposable PostgreSQL instance (the Compose `db` service is enough),
then run:

```bash
export TEST_DATABASE_URL=postgresql+psycopg://reservation:reservation@127.0.0.1:5454/reservation
pytest -m postgres tests/e2e tests/integration
```

The E2E fixture creates and drops the schema for each test, so
`TEST_DATABASE_URL` **must point to a disposable test database**.

Coverage includes API create/read/confirm/cancel behavior,
idempotency and changed-body conflict, duplicate-line canonicalization,
insufficient stock rollback, owner checks, expiry, immutable reservation lines,
final-unit concurrency, provider HOLD success/decline/unknown outcomes,
reconciliation, provider RELEASE, mixed-source compensation, SKIP LOCKED work
claims, stale-lease recovery, and expiry/confirmation transition behavior.
