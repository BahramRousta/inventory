# Reservation Assignment Remediation Workflow

## Implementation status

This branch implements Steps 1–8 and Step 10.

- Step 1: complete — lifecycle repository is wired through the unit of work.
- Step 2: complete — release reason determines EXPIRED vs CANCELLED.
- Step 3: complete — HTTP contract uses trusted user/idempotency headers,
  canonical request fingerprints, snapshot metadata, Location and retry hints.
- Step 4: simplified — payment processing remains outside this inventory
  service. Successful checkout calls `confirm`; failed/abandoned checkout uses
  `cancel` or TTL expiry.
- Step 5: complete — reservation-capable providers explicitly expose
  **HOLD is final allocation** through the reservation provider interface.
- Step 6: complete — orders persist immutable reservation lines.
- Step 7: complete — provider-specific mechanics are hidden behind one
  `InventoryProvider.reserve(...)` contract; query-style and hold-style
  providers are selected by the factory but processed identically by the
  application worker.
- Step 8: simplified for interview scope — Compose runs separate workers while
  provider behavior is represented by deterministic mock gateways rather than a
  production-style HTTP provider implementation.
- Step 9: test suite added — PostgreSQL-backed API/database, provider,
  concurrency, claim/lease, reconciliation, and payment-race scenarios are
  implemented. Execution still requires a disposable `TEST_DATABASE_URL`.
- Step 10: complete — README, architecture, async-state-machine and scalability
  documentation describe the implemented behavior.

**Purpose:** close the remaining gaps in the inventory-reservation assignment
without changing unrelated models or combining independent milestones.

**Working rule:** implement exactly one numbered step at a time. After each
step, explain the completed behavior, the assumption it relies on, and the
next step. Do not write or run tests unless explicitly requested.

**Design baseline:** `docs/ARCHITECTURE.md` and
`docs/ASYNC_PROVIDER_STATE_MACHINE.md`. Where they conflict with the current
code, this workflow states the intended implementation decision before code is
changed.

## Completion definition

The assignment is complete only when a trusted caller can create a
source-specific reservation, observe its state, confirm it after successful
checkout, or cancel/expire it when checkout does not complete:

- confirmation consumes internal stock, finalizes reservation state, and
  creates one local order;
- explicit cancellation or TTL expiry releases local and external holds exactly
  once where the provider outcome is known;
- unknown provider outcomes remain visible and reconcilable rather than being
  reported as success or release;
- provider calls happen outside database transactions and worker claims use
  database row locks with leases.

## Step 1 — Repair lifecycle repository wiring

**Goal:** make the existing read, confirm, cancel, expiry, and worker services
call the repository implementation that actually supplies their methods.

**Current problem:** `SqlAlchemyUnitOfWork` constructs
`SqlAlchemyReservationRepository`, while several lifecycle methods are only
defined by `SqlAlchemyLifecycleReservationRepository`. API lifecycle requests
can therefore fail with a missing-method error.

**Implementation:**

1. Choose one concrete reservation repository for the unit of work.
2. Preserve all existing provider-work claim methods and lifecycle methods on
   that concrete repository.
3. Remove duplicate or dead repository variants only when doing so does not
   alter the schema or public behavior.
4. Keep repositories returning DTOs/primitives, never ORM models.

**Assumption:** this is a wiring repair only; no state-machine transition or
database model changes are introduced.

**Done when:** every method declared by the reservation repository port exists
on the unit-of-work repository instance.

## Step 2 — Correct release terminal-state semantics

**Goal:** preserve why a reservation entered `RELEASING` and end it truthfully.

**Implementation:**

1. A reservation released because TTL elapsed ends as `EXPIRED`.
2. A reservation released because creation failed, payment failed, or the user
   cancelled ends as `CANCELLED`.
3. Do not overwrite `release_reason` during finalization.
4. Keep `RELEASING` while any external release result is unresolved.

**Assumption:** no new status is needed; existing `release_reason` and
`ReservationStatus.EXPIRED` are sufficient.

**Done when:** an expiry worker cannot turn an expired reservation into a
`CANCELLED`/`CREATE_FAILED` reservation.

## Step 3 — Freeze and align the reservation HTTP contract

**Goal:** make API behavior agree with the checked-in contract and business
assumptions.

**Implementation:**

1. Take the idempotency key only from required `Idempotency-Key` header.
2. Canonicalize duplicate `(product_id, stock_source_id)` items before stock
   mutation; reject invalid totals and overflow deterministically.
3. Store or derive a request fingerprint so the same key with a changed body
   returns `IDEMPOTENCY_CONFLICT`.
4. Return the documented snapshot fields: `created_at`, `expires_at`,
   `payment_allowed`, `requires_attention`, and line states.
5. Return `201` for a new immediately active internal reservation, `202` for
   pending work, and `200` for settled idempotent replay. Include `Location`;
   include a retry hint for pending work.
6. Use the documented error envelope consistently.
7. Require the trusted verified user identity for read, cancel, and confirm;
   it must match the reservation owner.

**Assumption:** authentication remains out of scope. The trusted caller still
supplies verified user identity at this service boundary.

**Done when:** schemas, routes, application DTOs, and OpenAPI describe one
identical contract.

## Step 4 — Keep payment outside the inventory service

**Goal:** keep the service boundary focused on reservation lifecycle.

**Implementation:**

1. Do not expose a payment-processing or payment-outcome endpoint.
2. After payment succeeds outside this service, the caller invokes
   `POST /reservations/{id}/confirm`.
3. If checkout fails or is abandoned, the caller invokes cancel or the
   reservation expires through TTL.
4. Keep confirmation idempotent and race-safe with expiration.

**Assumption:** payment systems and checkout orchestration are outside this
service boundary.

**Done when:** the public lifecycle is create, get, confirm, cancel, plus
automatic expiration.

## Step 5 — Define the external final-commit contract

**Goal:** make external confirmation truthful and durable.

**Decision required before implementation:** select one of these explicit
provider contracts per provider:

1. **Hold is final allocation:** the provider contract guarantees that a
   successful hold is the post-payment allocation; no remote confirm exists.
2. **Hold requires commit:** provider supports idempotent `CONFIRM` and status
   lookup; a reservation cannot become `CONFIRMED` until it succeeds.

**Implementation if contract 2 is selected:**

1. Use the existing `CONFIRM_PENDING` and `CONFIRM_UNKNOWN` line states.
2. Add batch claim/lease/compare-and-set processing for confirms, equivalent
   to HOLD and RELEASE.
3. Call provider confirm outside the transaction using a stable key derived
   from reservation and line identity.
4. Reconcile timeout/unknown outcomes with provider status lookup.

**Implementation if contract 1 is selected:**

1. Record this provider capability explicitly in configuration/metadata.
2. Permit local finalization only for providers with that documented guarantee.
3. Do not represent such a provider as supporting a missing `CONFIRM` call.

**Done when:** no code marks an external line `CONFIRMED` based on an
undocumented assumption.

## Step 6 — Complete immutable order persistence

**Goal:** preserve what was successfully purchased, not merely that an order
header exists.

**Implementation:**

1. Create one final order header exactly once per reservation.
2. For internal lines, consume held stock and create the local order in the
   same transaction.
3. Do not create the final order while any external required commit is
   unknown.
4. Keep detailed item/source information on reservation lines rather than
   duplicating it into a second reservation-line model for this assignment.

**Assumption:** one reservation creates at most one order, enforced by the
existing unique reservation reference plus idempotent creation behavior.

**Done when:** a confirmed reservation creates at most one order and the reservation lines
remain the item/source detail for the assignment.

## Step 7 — Persist provider capability and safe configuration metadata

**Goal:** distinguish provider behavior from a broad `EXTERNAL` type.

**Implementation:**

1. Keep provider-specific query/HOLD behavior behind the provider adapter.
2. Create flow validates enabled provider/source state only; provider execution
   later calls the common `reserve(...)` operation.
3. Keep provider selection/configuration in the factory; do not persist
   provider behavior flags in PostgreSQL.
4. A production HTTP/authentication adapter is intentionally outside this
   interview implementation.

**Assumption:** provider onboarding UI is out of scope; configuration may be
seeded or environment-backed for the assignment demo.

**Done when:** eligibility no longer means merely `ProviderKind.EXTERNAL`;
the configured provider factory must resolve an adapter whose declared
capabilities satisfy the workflow.

## Step 8 — Make the provider demo process-safe and deploy workers

**Goal:** demonstrate the async design using independently runnable processes.

**Implementation:**

1. Use a simple configurable provider mock at the provider port boundary.
2. Support success, definitive decline, and ambiguous/unknown outcomes.
3. Configure separate HOLD, RELEASE, reconciliation, and expiry worker
   services in Compose or documented process commands.
4. Keep batch claim size configurable (default 500), worker concurrency
   configurable (default 5), and provider work outside DB transactions.

**Assumption:** PostgreSQL remains the sole shared authority for local state;
no distributed lock or message broker is introduced.

**Done when:** API/workers exercise the complete reservation state machine and
tests can deterministically drive success, decline, and reconciliation paths.

## Step 9 — Add authorized end-to-end verification

**Do not start this step until explicitly requested.**

**Required evidence when authorized:**

1. PostgreSQL-backed API-to-database tests for internal hold, confirm, cancel,
   expiry, idempotency, duplicate lines, and final reservation lines.
2. Concurrent final-unit test against PostgreSQL.
3. Provider-boundary tests for hold success, definitive decline, unknown
   outcome, release, and reconciliation using configurable mocks.
4. Worker claim/lease tests proving `FOR UPDATE SKIP LOCKED`, stale-lease
   recovery, and no duplicate local state transition.
5. Confirmation-vs-expiry and duplicate-confirmation tests.

Mock only the provider boundary where an actual fake HTTP provider is not the
specific behavior under demonstration. Database assertions must use real
PostgreSQL.

## Step 10 — Make documentation truthful for submission

**Goal:** ensure the deliverable accurately describes verified behavior.

**Implementation:**

1. Update `README.md` run instructions, API examples, workers, seed data, and
   provider demo.
2. Update architecture/spec documents to the final chosen confirm contract and
   implemented API behavior.
3. Complete `SCALABILITY.md`: bottlenecks, batch/worker limits, database
   indexes, provider-rate limits, and the next scale-out choices.
4. State known non-atomic external-provider limitations and operational
   recovery behavior plainly.

**Done when:** no documentation claims an unimplemented behavior and no
implemented public behavior is missing from the submission guide.

## Suggested execution order

```text
1 wiring
2 terminal semantics
3 HTTP contract
4 inventory-service boundary
5 provider final commit decision + implementation
6 immutable orders
7 provider capabilities/config
8 process-safe demo and worker deployment
9 authorized E2E verification
10 truthful submission documentation
```

