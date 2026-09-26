# ARCHITECTURE

## 1. Scope

This service owns inventory reservation during checkout.

It is responsible for:

- creating a reservation for one or more source-specific items;
- preventing oversell for inventory controlled by this service;
- coordinating reservation attempts against external inventory providers;
- exposing reservation state;
- confirming a valid reservation and creating one final order;
- cancelling or expiring a reservation and releasing held inventory;
- recovering ambiguous external-provider outcomes.

Authentication, payment processing, cart management, pricing, shipping, seller
selection, and provider-onboarding UI are outside this service.

The caller supplies an already-verified `X-User-Id`. Payment is handled
outside this service. After successful payment the caller invokes
`POST /reservations/{id}/confirm`; when checkout fails or is abandoned it
invokes cancel, or the reservation expires by TTL.

This is an explicit boundary decision for this implementation: the assignment
requires confirmation after successful payment, but this inventory service does
not model payment itself.

## 2. Public API

The implemented reservation API is intentionally small:

```text
POST /reservations
GET  /reservations/{reservation_id}
POST /reservations/{reservation_id}/confirm
POST /reservations/{reservation_id}/cancel
```

`POST /reservations` requires:

- `X-User-Id`
- `Idempotency-Key`
- one or more items containing `product_id`, `stock_source_id`, and quantity.

An internal-only reservation can become `ACTIVE` synchronously. A reservation
with external lines is created as `RESERVING` and external work is completed
asynchronously.

### Asynchronous reservation response contract

`POST /reservations` always returns a `Location` header pointing to the
reservation resource:

```text
Location: /reservations/{reservation_id}
```

Clients use that URI to retrieve the latest reservation state with
`GET /reservations/{reservation_id}`.

The response status communicates whether the reservation is complete at the
time of creation:

```text
201 Created
```

is returned when all required work completed during the request, such as an
internal-only reservation that is already `ACTIVE`.

```text
202 Accepted
Location: /reservations/{reservation_id}
Retry-After: {worker pickup interval in seconds}
```

is returned when the reservation was persisted but external or compensation
work is still pending. The reservation is normally `RESERVING` or
`RELEASING`. `Retry-After` is based on the worker polling/pickup interval, not
on a fixed one-second assumption. If the deployment's worker interval is five
minutes, the value should be at least `300` seconds. This gives the worker a
reasonable opportunity to claim and process the work before the client polls
again.

`202 Accepted` does not mean that the reservation succeeded. The client must
follow the resource until it reaches a truthful state such as `ACTIVE`,
`CANCELLED`, or `EXPIRED`. `Location` and `Retry-After` exist because provider
calls run asynchronously outside the request transaction; they give the
client a stable resource URI and a bounded polling hint while workers finish
the durable workflow. The value is a minimum polling recommendation, not a
guarantee that processing completes within that interval; queue load and
provider latency can make the actual completion time longer.

## 3. Architectural style

The implementation uses ports and adapters.

```text
FastAPI
  |
  v
Application services
  |
  +---- Repository ports --------> SQLAlchemy / PostgreSQL
  |
  +---- InventoryProvider port --> provider implementations
```

The application layer is organized by business flow:

```text
app/application/services/
├── create/
├── inquiry/
├── confirm/
├── cancel/
├── expiry/
└── reconciliation/
```

The application layer depends on repository/provider protocols, not on
SQLAlchemy or provider-specific implementations.

PostgreSQL is both:

1. the transactional source of truth for local reservation state; and
2. the durable work queue for external provider work.

This avoids introducing a broker for the assignment while still allowing
multiple worker processes to coordinate safely.

## 4. Domain model

### Product

`Product` represents the catalog item.

### InventoryProvider

`InventoryProvider` identifies the system or owner that controls a stock
source. The database stores only identity/business state:

- id;
- name;
- kind: `INTERNAL` or `EXTERNAL`;
- enabled flag.

Provider implementation details are kept outside the database.

### StockSource

A product can have inventory from different sources. `StockSource` connects a
product to an inventory provider and optional provider SKU.

The reservation request selects a concrete stock source. This service does not
choose the seller/provider automatically.

### InternalStock

For locally controlled inventory:

```text
available = on_hand - held
```

The database enforces:

```text
on_hand >= 0
held >= 0
held <= on_hand
```

### Reservation

A reservation has:

- owner `user_id`;
- idempotency key;
- lifecycle status;
- `expires_at`;
- optional release reason;
- timestamps.

Reservation statuses are:

```text
RESERVING
ACTIVE
CONFIRMING
RELEASING
CONFIRMED
CANCELLED
EXPIRED
```

### ReservationLine

Each line identifies one stock source and quantity. Line states capture the
provider/local workflow, including:

```text
HOLD_PENDING
HOLD_IN_PROGRESS
HOLD_UNKNOWN
HELD

RELEASE_PENDING
RELEASE_IN_PROGRESS
RELEASE_UNKNOWN
RELEASED

CONFIRMED
FAILED
```

A reservation is not considered `ACTIVE` until all required lines are in
`HELD`.

### Order

The assignment requires creation of a final order after successful
confirmation. This implementation stores one order header per reservation:

```text
orders.reservation_id UNIQUE
```

Detailed purchased item/source information remains available from the
reservation lines. A full OMS is deliberately outside scope.

## 5. Internal inventory correctness

Local stock reservation uses a single guarded SQL `UPDATE`:

```text
UPDATE internal_stock
SET held = held + quantity
WHERE stock_source_id = ?
  AND on_hand - held >= quantity
```

The update succeeds for only one transaction when two concurrent requests
compete for the last units.

This is the main local inventory invariant:

> a reservation may only increase `held` when enough unheld `on_hand`
> inventory exists.

On cancellation:

```text
held = held - quantity
```

On confirmation:

```text
held    = held - quantity
on_hand = on_hand - quantity
```

Both operations are guarded so quantities cannot become invalid.

## 6. Reservation creation

### Internal source

For an internal source the create transaction:

1. validates source/product ownership and enabled state;
2. creates the reservation in `RESERVING`;
3. atomically holds internal inventory;
4. creates the reservation line as `HELD`;
5. moves the reservation to `ACTIVE` when no external work remains;
6. commits.

If the stock hold fails, the transaction is rolled back.

### External source

For an external source, create does not call a provider while holding the
database transaction.

Instead it stores:

```text
Reservation     -> RESERVING
ReservationLine -> HOLD_PENDING
```

and returns a pending reservation.

A worker later claims the line and performs provider work.

This separation is deliberate: remote latency or failure must not keep a
database transaction open.

## 7. Provider abstraction

The application sees one interface:

```python
class InventoryProvider(Protocol):
    async def reserve(...): ...
    async def release(...): ...
    async def get_reservation(...): ...
```

The worker does not inspect provider type or capabilities. It resolves the
provider by `provider_id` and calls:

```python
provider.reserve(...)
```

The concrete provider decides how that reservation attempt is implemented.

For this interview implementation two simple provider styles are demonstrated:

- a reservation-style provider can model a real upstream HOLD;
- a query-style provider can model a stock-availability API.

The common result is one of:

```text
RESERVED
DECLINED
UNKNOWN
```

### Important guarantee for query-only providers

A stock query is not a real upstream lock.

Therefore a query-style provider is treated as a **best-effort reservation
mode** in this demo. If availability is sufficient, the line may become
`HELD` locally, but the service cannot prevent another customer of that
external provider from buying the same units.

That limitation is intentional and documented rather than pretending a query
API provides exclusivity.

In a production checkout requiring guaranteed inventory, I would either reject
query-only providers for the guaranteed path or require a stronger contractual
allocation mechanism.

## 8. Provider failure handling

Provider calls happen outside database transactions.

Before calling a provider, a worker durably claims work using:

- line status;
- a random claim token;
- a lease deadline.

The claim is created under PostgreSQL locking with `FOR UPDATE SKIP LOCKED`.

This permits multiple workers to process different rows without processing the
same claim concurrently.

A provider result is mapped as follows:

```text
RESERVED -> HELD
DECLINED -> FAILED
UNKNOWN  -> HOLD_UNKNOWN
```

An exception during `reserve()` is treated as `UNKNOWN`, not as a
definitive failure, because the remote side effect may have happened before the
response was lost.

### Mixed-source atomicity and compensation

A reservation containing multiple products or stock sources is all-or-none at
the business level, but it is not one distributed transaction across
PostgreSQL and external providers.

The workflow is:

```text
create transaction
  internal holds + reservation lines
  -> commit as RESERVING

provider workers
  all lines HELD       -> ACTIVE
  any line DECLINED    -> line FAILED, reservation RELEASING

compensation workers
  release every known HELD line
  -> reservation CANCELLED when all lines are FAILED or RELEASED
```

The initial database transaction is atomic. If an internal hold fails, the
reservation and all earlier internal holds from that request are rolled back.
External calls happen only after that transaction commits, so their effects
cannot be rolled back by PostgreSQL. When one external line is definitively
declined, already-held internal and external lines are therefore released by
the compensation workflow before the reservation reaches its terminal state.

The reservation state model does not contain a `FAILED` reservation status.
Failure is represented by a `FAILED` line and the reservation transition
`RESERVING -> RELEASING -> CANCELLED`. A provider `UNKNOWN` result remains
`HOLD_UNKNOWN` and is reconciled; it is never treated as a definitive failure
or released blindly.

This provides eventual all-or-none behavior with durable recovery, rather than
instantaneous global atomicity across provider systems.

## 9. Reconciliation

Ambiguous remote outcomes remain durable.

```text
HOLD_UNKNOWN
RELEASE_UNKNOWN
```

The reconciliation worker claims unknown work and calls:

```python
provider.get_reservation(reservation_key=...)
```

Possible lookup results:

```text
RESERVED
NOT_RESERVED
UNKNOWN
```

The line is only moved to a truthful terminal/intermediate state when the
provider result is known.

### Reconciliation retry policy

The current implementation retries both reservation and release
reconciliation, but the retry policy is intentionally simple:

- `HOLD_UNKNOWN` and `RELEASE_UNKNOWN` are eligible on the next reconciliation
  worker poll;
- each worker claim gets a lease, so a crashed or stuck worker is recovered
  after the lease expires and the line returns to its corresponding `UNKNOWN`
  state;
- an `UNKNOWN` provider lookup returns the line to `HOLD_UNKNOWN` or
  `RELEASE_UNKNOWN`, so it can be attempted again later;
- a release lookup that still reports `RESERVED` returns the line to
  `RELEASE_PENDING` for another release attempt;
- there is currently no attempt counter, exponential backoff, maximum retry
  limit, or dead-letter/manual-review state.

This policy retries only ambiguous work. A definitive hold decline becomes
`FAILED`, and a definitive release result becomes `RELEASED`; neither is
repeated as if it were an unknown outcome. The stable reservation/source
operation key makes repeated provider inquiries and release attempts
correlatable.

The polling interval and lease provide crash recovery and limit concurrent
duplicate processing, but they are not a full provider retry policy. In
production, repeated `UNKNOWN` outcomes should gain bounded exponential
backoff, an attempt limit, provider-specific retry classification, and a
manual-reconciliation state so an unavailable provider cannot cause an
unbounded hot retry loop.

Expired worker leases are also recovered into an unknown state so a crashed
worker does not silently lose remote work.

## 10. Confirmation

Only an `ACTIVE` reservation can be confirmed.

Confirmation starts with an atomic compare-and-set:

```text
ACTIVE -> CONFIRMING
```

and the SQL condition also requires:

```text
expires_at > database_now
```

This prevents confirmation after the reservation TTL has already elapsed and
protects against races with the expiry worker.

During confirmation:

1. every line must already be `HELD`;
2. internal holds are consumed;
3. lines become `CONFIRMED`;
4. the reservation becomes `CONFIRMED`;
5. one order is created.

All local confirmation changes run in one database transaction.

Repeated confirmation of an already confirmed reservation returns the existing
order rather than creating another one.

## 11. Cancellation and expiration

Cancellation moves an eligible reservation to:

```text
RELEASING
```

The release worker then:

- marks unprocessed pending external holds as failed;
- releases internal held inventory transactionally;
- moves known external holds to `RELEASE_PENDING`;
- calls external provider `release()` outside the transaction;
- reconciles ambiguous releases when required.

When every line is resolved, the reservation becomes:

```text
CANCELLED
```

For TTL expiry, the same compensation path is used but the release reason is
`EXPIRED`, so the final state becomes:

```text
EXPIRED
```

This preserves why the inventory was released.

## 12. Transaction boundaries

The main rule is:

> database state changes are transactional; remote provider calls are not made
> inside the transaction.

Examples:

### Create internal reservation

One transaction:

```text
reservation row
+ reservation lines
+ internal hold
+ ACTIVE transition
```

### External reserve

```text
TX 1: claim work + commit
remote provider call
TX 2: persist result + reservation transition + commit
```

### Confirm

One local transaction:

```text
ACTIVE -> CONFIRMING
consume internal holds
mark lines CONFIRMED
reservation -> CONFIRMED
create order
```

No distributed transaction is attempted across PostgreSQL and an external
provider.

## 13. Idempotency and concurrency

### Create

The database has a uniqueness constraint on:

```text
(user_id, idempotency_key)
```

A retry returns the existing reservation rather than creating a second one.

### Confirm

The reservation state transition and the unique
`orders.reservation_id` constraint prevent duplicate final orders.

### Workers

Worker claim tokens and leases make stale or duplicate worker results fail the
compare-and-set update instead of overwriting newer state.

## 14. Indexes used by the workflow

The schema currently includes:

```text
reservations(status, expires_at)
reservation_lines(status, provider_lease_until)
```

These support:

- expiration scans;
- provider work claims;
- stale-lease recovery.

## 15. Why PostgreSQL is also the work queue

For this assignment, keeping workflow state and work claims in PostgreSQL has
several advantages:

- one durable system;
- transactional creation of work with reservation state;
- no outbox needed;
- easy recovery after worker crash;
- `SKIP LOCKED` enables multiple workers.

A broker would add another durable subsystem and failure boundary without being
necessary for the demonstrated workload.

## 16. Assumptions and deliberate simplifications

The following are explicit implementation assumptions:

1. The caller has already authenticated the user.
2. Payment happens outside this service.
3. A successful external reservation/HOLD does not require a second remote
   confirmation call in this demo.
4. Query-only providers are best-effort and do not provide a globally exclusive
   reservation.
5. The request selects a stock source; this service does not rank providers or
   substitute another seller.
6. Reservation TTL defaults to 900 seconds.
7. Provider integrations are deterministic mocks for the assignment; real HTTP
   authentication and transport code are intentionally not implemented.
8. Provider secrets/credential storage is not implemented in this demo.
   In production I would inject secret material from a secret manager or
   deployment environment and keep only non-secret identity/configuration in
   application configuration.
9. PostgreSQL is the only durable infrastructure dependency.
10. The order model is intentionally minimal.

## 17. Failure scenarios demonstrated

The assignment asks for at least two provider-call scenarios, including a
non-happy path. The implementation supports deterministic provider behavior for:

### Scenario A: successful reservation

```text
HOLD_PENDING
-> HOLD_IN_PROGRESS
-> provider.reserve()
-> RESERVED
-> HELD
-> reservation ACTIVE
```

This demonstrates the normal external reservation flow.

### Scenario B: definitive decline

```text
provider.reserve()
-> DECLINED
-> line FAILED
-> reservation RELEASING
-> compensation
-> CANCELLED
```

This demonstrates a business failure.

### Scenario C: ambiguous result

```text
provider.reserve()
-> UNKNOWN
-> HOLD_UNKNOWN
-> reconciliation
-> RESERVED / NOT_RESERVED / UNKNOWN
```

This demonstrates the more important distributed-systems failure: the caller
does not know whether the remote side effect happened.

These scenarios were chosen because they cover success, deterministic failure,
and ambiguous failure without building unnecessary production provider
infrastructure.

## 18. What I would change for production

Given more time and production requirements, I would add only when justified by
measured needs:

- real provider adapters with authentication, timeouts, retries, and
  provider-specific rate limits;
- secret-manager integration for provider credentials;
- stronger semantics for query-only providers;
- structured metrics/tracing and manual-reconciliation tooling;
- request-body fingerprinting for stronger idempotency-key misuse detection;
- per-provider worker isolation when one provider can starve others;
- transactional outbox + broker if database polling becomes a measured
  bottleneck or other services need reservation events;
- archival/partitioning when terminal reservation history materially affects
  operational queries.

The central correctness choices would remain the same: guarded local inventory
updates, explicit reservation states, short database transactions, durable
provider work, and truthful handling of ambiguous external outcomes.
