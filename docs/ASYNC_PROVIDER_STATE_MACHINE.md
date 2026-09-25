# Async Provider State Machine

## Contract

Internal-only reservations remain synchronous. External provider calls are
asynchronous and never run inside a PostgreSQL transaction.

Worker claims are durable reservation-line state. PostgreSQL row locks with
`SKIP LOCKED`, claim tokens and lease deadlines prevent duplicate local work;
provider idempotency keys remain the correctness boundary if a process dies
after an upstream side effect.

## Stable keys

```text
HOLD:    {reservation_id}:{stock_source_id}:HOLD
RELEASE: {reservation_id}:{stock_source_id}:RELEASE
```

## Reservation states

- `RESERVING`: external HOLD work is pending or unresolved; payment forbidden.
- `ACTIVE`: every line is held and TTL has not passed; payment allowed.
- `CONFIRMING`: a trusted payment success won the transition race and local
  finalization is in progress.
- `RELEASING`: failure, cancellation or expiry requires compensation.
- `CONFIRMED`: internal stock was consumed and one immutable order exists.
- `CANCELLED`: all releasable claims are proved released or never held.
- `EXPIRED`: the same resolved compensation state, but entered because TTL
  elapsed.

## External line states

HOLD flow:

```text
HOLD_PENDING -> HOLD_IN_PROGRESS -> HELD
                              \-> FAILED
                              \-> HOLD_UNKNOWN
```

RELEASE flow:

```text
HELD -> RELEASE_PENDING -> RELEASE_IN_PROGRESS -> RELEASED
                                            \-> RELEASE_UNKNOWN
```

The demo provider uses **HOLD is final allocation** semantics. Therefore after
trusted payment success a proven `HELD` external line can be marked locally
`CONFIRMED` without a second provider call, but only when the provider's
persisted capability metadata explicitly declares this guarantee.

## HOLD executor

1. In a short transaction, claim `HOLD_PENDING` rows with
   `FOR UPDATE SKIP LOCKED`, assign a claim token and lease, and commit.
2. Call the provider outside any transaction using the stable HOLD key.
3. In a new short transaction, compare-and-set the result only if the claim
   token still owns a live lease.
4. Normalize provider outcomes:
   - success -> `HELD` and persist `external_hold_ref`;
   - definitive decline -> `FAILED` and begin `RELEASING`;
   - timeout/ambiguous response -> `HOLD_UNKNOWN`.
5. Move `RESERVING -> ACTIVE` only if every line is `HELD` and TTL has not
   elapsed.

## RELEASE executor

1. Local compensation changes known external `HELD` lines to
   `RELEASE_PENDING`; internal held counters are decremented in the same local
   transaction.
2. A release worker claims pending rows using the same token/lease mechanism.
3. Provider RELEASE happens outside a transaction with the stable RELEASE key.
4. Proven release -> `RELEASED`; ambiguous result -> `RELEASE_UNKNOWN`.
5. The reservation reaches a terminal release state only when every line is
   `FAILED` or `RELEASED`.

## Reconciliation

A stale worker lease is never interpreted as provider failure. Expired
in-progress claims become an UNKNOWN state.

For `HOLD_UNKNOWN`, GET_HOLD determines whether the original stable HOLD key
created an allocation. A proven hold becomes `HELD` (or is queued for release
if the reservation is already releasing); proven absence becomes `FAILED`;
ambiguity remains unknown.

For `RELEASE_UNKNOWN`, the same provider lookup proves whether the original
hold still exists. Absence means release is complete; a still-existing hold is
returned to `RELEASE_PENDING`; ambiguity remains unknown.

## Expiry

The expiry worker atomically claims expired `RESERVING` or `ACTIVE`
reservations into `RELEASING` with `release_reason=EXPIRED`. Compensation
uses the normal release workflow. Once every line is resolved, the reservation
becomes `EXPIRED`, not `CANCELLED`.

## Fake provider scenarios

The standalone fake provider supports these modes:

- `success`: HOLD succeeds normally.
- `decline`: HOLD returns a definitive 409 decline.
- `timeout_after_side_effect`: a mutating provider call applies its HOLD or
  RELEASE side effect and then delays the response beyond the client timeout.
  Local state becomes `HOLD_UNKNOWN` or `RELEASE_UNKNOWN`; reconciliation
  later proves the durable upstream state before any terminal claim is made.

The mode can be changed through the fake provider's
`POST /admin/mode/{mode}` endpoint for demonstrations.
