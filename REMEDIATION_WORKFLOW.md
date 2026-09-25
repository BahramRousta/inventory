# Reservation Assignment Remediation Workflow

## Implementation status

This branch implements Steps 1–8 and Step 10.

- Step 1: complete — lifecycle repository is wired through the unit of work.
- Step 2: complete — release reason determines EXPIRED vs CANCELLED.
- Step 3: complete — HTTP contract uses trusted user/idempotency headers,
  canonical request fingerprints, snapshot metadata, Location and retry hints.
- Step 4: complete — trusted, idempotent payment outcomes orchestrate success
  and failure transitions.
- Step 5: complete — the configured provider contract is explicitly
  **HOLD is final allocation** and is persisted as capability metadata.
- Step 6: complete — orders persist immutable order lines.
- Step 7: complete — provider capabilities and non-secret config references are
  persisted and checked for reservation eligibility.
- Step 8: complete — Compose runs a standalone fake HTTP provider and separate
  hold/release/reconciliation/expiry workers.
- Step 9: intentionally deferred — no new tests are added or run in this pass.
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
source-specific reservation, observe its state, submit an idempotent payment
outcome, and eventually get exactly one truthful terminal result:

- successful payment consumes internal stock, commits each required external
  allocation, and creates one immutable local order with lines;
- failed payment, explicit cancellation, or TTL expiry releases local and
  external holds exactly once where the provider outcome is known;
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
7. Require the trusted verified user identity for read, cancel, payment, and
   any direct confirmation operation; it must match the reservation owner.

**Assumption:** authentication remains out of scope. The trusted caller still
supplies verified user identity at this service boundary.

**Done when:** schemas, routes, application DTOs, and OpenAPI describe one
identical contract.

## Step 4 — Add trusted payment-outcome orchestration

**Goal:** replace direct checkout finalization as the primary flow with a
trusted payment result.

**Implementation:**

1. Add a payment outcome input with `event_id`, `reservation_id`, verified
   user, and `SUCCESS` or `FAILURE`.
2. Persist event identity/payload identity so duplicate delivery is harmless
   and a reused event ID with different content is a conflict.
3. On eligible `SUCCESS`, atomically claim `ACTIVE -> CONFIRMING` only before
   expiry according to database time.
4. On `FAILURE`, atomically claim `RESERVING|ACTIVE -> RELEASING`.
5. Keep late or contradictory events truthful: they must not resurrect an
   expired/released reservation or silently undo a confirmation.

**Assumption:** payment processing itself remains outside this service; this
endpoint/event consumes a trusted outcome only.

**Done when:** payment-vs-expiry has a single database transition winner and
the direct confirm endpoint is either removed, clearly administrative, or
delegates to the same application flow.

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

1. Add immutable order-line persistence for product, stock source, quantity,
   and any assignment-required source/provider reference.
2. Create the order header and all lines exactly once per reservation.
3. For internal lines, consume held stock and insert the local order in the
   same transaction.
4. Do not create the final order while any external required commit is
   unknown.

**Assumption:** one reservation creates at most one order, enforced by the
existing unique reservation reference plus idempotent creation behavior.

**Done when:** an order can independently explain every finalized reservation
line without consulting mutable reservation records.

## Step 7 — Persist provider capability and safe configuration metadata

**Goal:** distinguish provider behavior from a broad `EXTERNAL` type.

**Implementation:**

1. Persist/resolve whether a provider supports query, hold, release, status
   lookup, and either commit or final-allocation hold semantics.
2. Reject query-only and otherwise insufficient providers before creating a
   guaranteed checkout reservation.
3. Persist non-secret configuration and a credential reference only; do not
   put raw credentials or provider payloads in logs.
4. Keep provider-specific authentication, request shapes, and errors in
   infrastructure adapters.

**Assumption:** provider onboarding UI is out of scope; configuration may be
seeded or environment-backed for the assignment demo.

**Done when:** eligibility no longer means merely `ProviderKind.EXTERNAL` plus
an in-process gateway registration.

## Step 8 — Make the provider demo process-safe and deploy workers

**Goal:** demonstrate the async design using independently runnable processes.

**Implementation:**

1. Run a standalone fake HTTP provider for the assignment demo instead of
   process-local in-memory hold state.
2. Support at least a success scenario and a non-happy scenario such as
   timeout after the fake provider has accepted a hold.
3. Configure separate HOLD, RELEASE, reconciliation, and expiry worker
   services in Compose or documented process commands.
4. Keep batch claim size configurable (default 500), worker concurrency
   configurable (default 5), and all provider HTTP outside a DB transaction.

**Assumption:** PostgreSQL remains the sole shared authority for local state;
no distributed lock or message broker is introduced.

**Done when:** API, each worker, and the fake provider can run in separate
processes while reconciliation still observes the same provider state.

## Step 9 — Add authorized end-to-end verification

**Do not start this step until explicitly requested.**

**Required evidence when authorized:**

1. PostgreSQL-backed API-to-database tests for internal hold, confirm, cancel,
   expiry, idempotency, duplicate lines, and final order lines.
2. Concurrent final-unit test against PostgreSQL.
3. Actual fake-HTTP-provider tests for hold success, definitive decline,
   timeout-after-side-effect, release, and reconciliation.
4. Worker claim/lease tests proving `FOR UPDATE SKIP LOCKED`, stale-lease
   recovery, and no duplicate local state transition.
5. Payment-vs-expiry and duplicate/contradictory payment-event tests.

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
4 payment event
5 provider final commit decision + implementation
6 immutable orders
7 provider capabilities/config
8 process-safe demo and worker deployment
9 authorized E2E verification
10 truthful submission documentation
```

