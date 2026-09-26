# Inventory Reservation Service

FastAPI reservation service for source-specific inventory. PostgreSQL is the
authoritative store for products, stock sources, reservations, reservation
lines, provider work, and orders.

The repository contains one API process and four independent workers:

- `hold-worker` processes pending external holds;
- `release-worker` releases local and external holds;
- `reconciliation-worker` resolves unknown provider outcomes and stale leases;
- `expiry-worker` moves expired reservations into the release workflow.

## Requirements

- Docker and Docker Compose; or
- Python 3.12+ with the dependencies installed from `pyproject.toml`;
- PostgreSQL 16 for local execution and integration tests.

## Run everything with Compose

Start PostgreSQL, apply migrations, start the API, and start all workers:

```bash
docker compose up --build
```

The services are:

| Service | Purpose | Address/command |
|---|---|---|
| `db` | PostgreSQL 16 | host port `5454` |
| `migrate` | Runs `alembic upgrade head` once | migration job |
| `api` | FastAPI application | `http://localhost:8000` |
| `hold-worker` | External HOLD processing | continuous worker |
| `release-worker` | Internal/external release processing | continuous worker |
| `reconciliation-worker` | Unknown outcome and lease recovery | continuous worker |
| `expiry-worker` | TTL expiry processing | continuous worker |

Check the API:

```bash
curl http://localhost:8000/health
```

To stop the stack:

```bash
docker compose down
```

The PostgreSQL volume is named `reservation_pg`. To remove the disposable
database as well:

```bash
docker compose down -v
```

## Apply migrations manually

Compose runs migrations automatically through the `migrate` service. If the
database is already running and you need to apply migrations again:

```bash
docker compose run --rm migrate
```

For a host Python environment, use the default local database URL:

```bash
alembic upgrade head
```

## Seed demo data

The seed script creates products, an internal provider, the configured fake
external provider, source-specific inventory, and internal stock rows. It is
safe to run repeatedly for the same seeded records.

With the Compose API container:

```bash
docker compose exec api python -m  scripts.seed_demo
```

Or from the host environment after PostgreSQL is available:

```bash
python scripts/seed_demo.py
```

The script prints each `product_id`, provider ID, and `stock_source_id`. The
reservation request must use the product/source pair from the same printed
row. The demo external provider ID is:

```text
11111111-1111-1111-1111-111111111111
```

The demo query-only provider ID is:

```text
22222222-2222-2222-2222-222222222222
```

Each seeded product has an internal source, a reservable external source, and
a query-only external source. Use the printed `stock_source_id` that matches
the provider behavior you want to demonstrate.

## Reservation API

The caller supplies a verified user identity through `X-User-Id`. Payment
processing is outside this service; after successful payment the caller uses
the confirmation endpoint.

### Create

```bash
curl -i -X POST http://localhost:8000/reservations \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: user-123' \
  -H 'Idempotency-Key: checkout-001' \
  -d '{
    "items": [
      {
        "product_id": "<product-id>",
        "stock_source_id": "<stock-source-id>",
        "quantity": 1
      }
    ]
  }'
```

Internal-only reservations can return `201 ACTIVE`. Reservations containing
external work return `202 RESERVING`; the response includes a `Location` and a
`Retry-After` header. The caller must not start payment until the reservation
is `ACTIVE` and `payment_allowed` is true.

### Read status

```bash
curl -i http://localhost:8000/reservations/<reservation-id> \
  -H 'X-User-Id: user-123'
```

### Confirm after trusted payment success

```bash
curl -i -X POST \
  http://localhost:8000/reservations/<reservation-id>/confirm \
  -H 'X-User-Id: user-123'
```

Confirmation consumes internal held stock and creates one order for the
reservation. Repeating confirmation is idempotent for an already confirmed
reservation.

### Cancel

```bash
curl -i -X POST \
  http://localhost:8000/reservations/<reservation-id>/cancel \
  -H 'X-User-Id: user-123'
```

Cancellation may return `202 RELEASING` while workers release external holds.
Reservations also enter the release workflow when their TTL expires.

## Run workers separately

`docker compose up --build` starts every worker. To run only selected
processes:

```bash
docker compose up --build db migrate api hold-worker
docker compose up --build release-worker
docker compose up --build reconciliation-worker
docker compose up --build expiry-worker
```

Each worker also supports a single iteration, which is useful for local
inspection:

```bash
docker compose run --rm hold-worker python -m app.workers.hold_worker
docker compose run --rm release-worker python -m app.workers.release_worker
docker compose run --rm reconciliation-worker python -m app.workers.reconciliation_worker
docker compose run --rm expiry-worker python -m app.workers.expiry_worker
```

The continuous mode is selected by `--forever`; it polls until stopped:

```bash
docker compose run --rm hold-worker python -m app.workers.hold_worker --forever
```

Do not run multiple copies of the same worker against a database unless you
intend to scale it. Row claims use PostgreSQL `FOR UPDATE SKIP LOCKED`, claim
tokens, and leases so separate worker processes can safely share the queue.

## Configuration

Settings are read from environment variables or `.env`:

| Variable | Default | Meaning |
|---|---:|---|
| `DATABASE_URL` | `postgresql+psycopg://reservation:reservation@127.0.0.1:5454/reservation` | Async PostgreSQL URL |
| `RESERVATION_TTL_SECONDS` | `900` | Reservation lifetime |
| `EXTERNAL_PROVIDER_ID` | unset | Provider ID used by the demo provider factory |
| `QUERY_ONLY_PROVIDER_ID` | unset | Optional query-style provider ID |
| `MOCK_PROVIDER_MODE` | `success` | `success`, `decline`, or `unknown` |
| `MOCK_PROVIDER_AVAILABLE_QUANTITY` | `100` | Query-provider availability |
| `PROVIDER_WORKER_BATCH_SIZE` | `500` | Maximum claimed items per iteration |
| `PROVIDER_WORKER_CONCURRENCY` | `5` | Concurrent provider calls per worker |
| `PROVIDER_WORKER_LEASE_SECONDS` | `60` | Claim lease duration |
| `PROVIDER_WORKER_POLL_INTERVAL_SECONDS` | `1.0` | Continuous-worker polling interval |

The included provider implementation is an in-process deterministic mock for
the assignment demo. It is not a production provider adapter and does not
represent a separate external HTTP service.

## Integration tests

Tests require a disposable PostgreSQL database. Start only the database:

```bash
docker compose up -d db
```

Then run the PostgreSQL BDD-style integration suite from the project virtual
environment:

```bash
export TEST_DATABASE_URL='postgresql+psycopg://reservation:reservation@127.0.0.1:5454/reservation'
.venv/bin/pytest -m postgres tests/e2e tests/integration
```

The fixtures in `tests/conftest.py` create and drop the schema for each
scenario. Tests assert persisted PostgreSQL rows, not only HTTP responses.
Do not point `TEST_DATABASE_URL` at a database containing data you need to
keep.

## Architecture references

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/SCALABILITY.md](docs/SCALABILITY.md)
