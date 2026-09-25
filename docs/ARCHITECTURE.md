# Architecture

## Scope

This service coordinates inventory reservations during checkout. It owns local
reservation state, internal inventory holds, the local order record, and durable
workflow state for external inventory providers.

Authentication and payment processing are outside the service. A trusted caller
supplies the verified user identity through `X-User-Id`. After payment succeeds
outside this service, the caller invokes the reservation confirm endpoint; on
failure or abandonment it invokes cancel or lets the reservation expire.

## Architectural style

The code uses ports and adapters around application services:

- FastAPI routes translate HTTP requests into application commands.
- Application services own reservation workflows and state transitions.
- Repository and provider protocols are application-owned ports.
- SQLAlchemy/PostgreSQL and provider gateways are infrastructure adapters.
- A unit of work gives one transaction boundary for local state changes.
- Independent workers claim external work from PostgreSQL using row locks and
  leases.

Provider work is invoked outside database transactions; the interview implementation uses deterministic mock gateways at that boundary.

## Reservation creation

Internal-only reservations are synchronous. One transaction validates the
source, creates the reservation and lines, performs guarded local holds, and
moves the reservation to `ACTIVE`.

Mixed or external reservations create durable `HOLD_PENDING` lines and return
`202 Accepted`. HOLD calls are performed later by the hold worker.

Duplicate request items with the same product/source pair are canonicalized
before stock mutation. The canonical body is hashed and stored as
`request_fingerprint`. A replay with the same user and `Idempotency-Key`
returns the existing snapshot; a changed body raises `IDEMPOTENCY_CONFLICT`.

## External-provider contract

The assignment demo chooses this explicit provider contract:

**A successful HOLD is the final external allocation.**

The application owns one provider port: `InventoryProvider`. The worker loads
a provider by ID and calls `reserve(...)`; the concrete provider decides how
that operation is implemented. A query-style provider may check availability,
while a reservation-style provider may perform a HOLD. The application service
does not branch on those provider details.

The same provider abstraction also exposes release/status operations needed by
compensation and reconciliation. The interview implementation uses simple mock
providers rather than real HTTP integrations.

## Confirmation

Checkout/payment handling is outside this service. The reservation API exposes
`POST /reservations/{id}/confirm` as the success transition.

Confirmation conditionally transitions `ACTIVE -> CONFIRMING` before expiry,
consumes internal held stock, marks all reservation lines `CONFIRMED`, and
creates exactly one local order.

Failure/abandonment is represented through the existing cancel and TTL-expiry
paths; there is no payment-specific API or payment domain model.

## Compensation and terminal truth

A reservation enters `RELEASING` for creation failure, user cancellation, or TTL expiry.

Internal holds are released transactionally. Known external holds become
`RELEASE_PENDING` and are released asynchronously. Ambiguous external
outcomes remain `HOLD_UNKNOWN` or `RELEASE_UNKNOWN` until reconciliation
proves the provider state.

The terminal status preserves the reason:

- `release_reason=EXPIRED` -> `EXPIRED`
- other resolved release reasons -> `CANCELLED`

Finalization never overwrites `release_reason`.

## Worker model

PostgreSQL is the durable work queue. Workers use
`FOR UPDATE SKIP LOCKED` plus a claim token and lease expiry. This allows
multiple worker processes to claim different rows without an external broker.
Expired claims are recovered into an unknown state and reconciled before any
terminal claim is made.

The Compose deployment runs independent hold, release, reconciliation and
expiry worker processes.

## Deliberate simplicity

For this interview assignment:

- PostgreSQL is the only local source of truth; no Kafka, RabbitMQ, Celery or
  Redis is introduced.
- Only simple mock provider gateways are used in the runnable demo; real provider HTTP/auth details are deliberately out of scope.
- Provider HOLD is the final external allocation, avoiding an undocumented
  remote CONFIRM protocol.
- The final order is linked one-to-one with its confirmed reservation; detailed
  item/source state remains on the reservation lines for this assignment scope.
- Authentication and payment processing remain outside the service boundary.

## What would change with more time

The next production steps would be stronger operational metrics, per-provider
rate limiting and circuit breaking, a real provider adapter/authentication integration,
partitioned worker queues for very high provider volume, and broader
PostgreSQL-backed end-to-end verification.
