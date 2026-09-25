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

There is no remote CONFIRM operation. The application asks the registry for a
`ReservationProviderGateway`. Query-only providers implement the separate
`AvailabilityProviderGateway` interface and therefore cannot be selected for
reservation work.

The reservation gateway contract consists of HOLD, RELEASE, and GET_HOLD plus
the explicit `hold_is_final_allocation` semantic. The interview implementation
uses a configurable mock gateway that returns deterministic results; a real
HTTP adapter is intentionally not implemented.

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
