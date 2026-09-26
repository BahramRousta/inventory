# Reservation flowcharts

These diagrams describe the implemented asynchronous reservation workflow.
PostgreSQL is the source of truth and also stores durable provider work. A
provider call is never made while a database transaction or row lock is held.

## Diagrams

- [Reservation lifecycle](./reservation-lifecycle.md) — state transitions for
  reservations and lines.
- [Create and hold flow](./reservation-create-and-hold.md) — HTTP creation,
  internal stock, and external provider holds.
- [Reconciliation and release flow](./reconciliation-and-release.md) —
  unknown outcomes, lease recovery, compensation, cancellation, and expiry.
- [Confirmation flow](./confirmation.md) — payment handoff, local consumption,
  and order creation.

## Common terms

| Term | Meaning |
|---|---|
| Claim | A short database transaction changes a work line to an in-progress state, assigns a token, and sets a lease. |
| Lease | The time window owned by a worker before reconciliation may recover the work. |
| Unknown | The provider outcome cannot safely be classified as success or failure. |
| Compensation | Releasing every known hold after cancellation, expiry, or a failed basket. |

