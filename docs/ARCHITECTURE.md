# Architecture

## Scope

This service coordinates inventory reservations during checkout. It owns local
reservation state, internal inventory holds, the local order record, and durable
workflow state for external inventory providers.

Authentication and payment processing are outside the service. A trusted caller
supplies the verified user identity through `X-User-Id`, and payment systems
submit only the final trusted payment outcome.

## Architectural style

The code uses ports and adapters around application services:

- FastAPI routes translate HTTP requests into application commands.
- Application services own reservation workflows and state transitions.
- Repository and provider protocols are application-owned ports.
- SQLAlchemy/PostgreSQL and HTTP provider clients are infrastructure adapters.
- A unit of work gives one transaction boundary for local state changes.
- Independent workers claim external work from PostgreSQL using row locks and
  leases.

Provider HTTP is never performed while a database transaction is held.

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

There is no remote CONFIRM operation. The HTTP provider adapter declares this
contract through `ProviderCapabilities.hold_is_final_allocation`, and the
application checks the configured adapter before an external source can
participate in a guaranteed reservation or be finalized after payment.

Provider operation capabilities belong to adapter code rather than provider
rows in PostgreSQL. The configured gateway declares support for HOLD, RELEASE,
GET_HOLD, and final-allocation semantics. A query-only or otherwise
insufficient adapter is rejected before a reservation is created.

Provider runtime configuration is environment-backed and assembled by
`ProviderGatewayFactory`. Provider ID, base URL, timeout, and optional API key
come from deployment settings/CI secret injection. Provider-specific
authentication, request shapes, capabilities, and errors stay in
infrastructure adapters; raw credentials are not persisted in this service's
database.

## Payment and confirmation

Checkout finalization is driven by
`POST /reservations/{id}/payment-outcome`.

A payment outcome contains a globally unique `event_id` and either `SUCCESS`
or `FAILURE`. Its payload hash is stored so duplicate delivery is harmless,
while reuse of the event ID with different content is a conflict.

On success, PostgreSQL conditionally transitions `ACTIVE -> CONFIRMING` only
when the reservation has not expired. This database transition races safely
with expiry. Internal held stock is consumed, external lines are accepted only
for providers whose HOLD is declared final allocation, every line is marked
`CONFIRMED`, and one final order is inserted in the same
local transaction.

On payment failure, `RESERVING|ACTIVE -> RELEASING` is claimed atomically and
the normal compensation workflow is used.

The legacy direct confirm endpoint remains only as a deprecated administrative
compatibility endpoint. It requires the same verified user identity and uses
the same finalization helper as payment success.

## Compensation and terminal truth

A reservation enters `RELEASING` for creation failure, payment failure, user
cancellation or TTL expiry.

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
- Only one configured external HTTP provider is used in the runnable demo.
- Provider HOLD is the final external allocation, avoiding an undocumented
  remote CONFIRM protocol.
- The final order is linked one-to-one with its confirmed reservation; detailed
  item/source state remains on the reservation lines for this assignment scope.
- Authentication and payment processing remain outside the service boundary.

## What would change with more time

The next production steps would be stronger operational metrics, per-provider
rate limiting and circuit breaking, explicit secret-manager integration,
partitioned worker queues for very high provider volume, and broader
PostgreSQL-backed end-to-end verification.
