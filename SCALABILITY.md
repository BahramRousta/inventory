# Scalability

## Current shape

The service deliberately uses PostgreSQL as both the transactional authority and
the durable provider-work queue. This keeps the assignment operationally small
while preserving correctness under concurrent reservation requests and worker
processes.

The default worker settings are:

- batch size: 500 claimed rows;
- concurrency: 5 provider calls per worker process;
- lease: 60 seconds;
- poll interval: 1 second.

These are configuration defaults, not throughput guarantees.

## First bottlenecks

### Provider latency and rate limits

External calls will normally become the first throughput constraint. Increasing
worker concurrency blindly can amplify provider failures or trigger upstream
rate limits. Concurrency should therefore be tuned per provider, with metrics
for latency, timeout rate, decline rate and reconciliation backlog.

At larger scale I would add provider-specific concurrency/rate-limit controls
before adding a message broker.

### PostgreSQL work scanning

Workers repeatedly query reservation lines by status and lease deadline. The
current `ix_reservation_line_work_claim(status, provider_lease_until)` index
supports recovery and work selection, while
`ix_reservation_status_expiry(status, expires_at)` supports TTL scanning.

At high row counts, the next improvement would be partial indexes for active
work states and bounded archival of terminal reservation history. Work should
continue to use `FOR UPDATE SKIP LOCKED` so competing workers claim different
rows instead of blocking.

### Hot internal inventory rows

Internal stock reservation is a guarded atomic UPDATE. This prevents oversell,
but a very popular SKU can become a single-row serialization point. That is a
correctness-preserving bottleneck. Possible later options are inventory
sharding by physical bucket, pre-allocation pools, or queueing extremely hot
SKU mutations, but those add operational and reconciliation complexity.

## Why there is no queue yet

A broker would improve decoupling and reduce polling at sufficient volume, but
it would also introduce a second durable system and require an outbox/inbox
protocol. For the assignment's scale, PostgreSQL already contains the exact
durable state required to recover work, and `SKIP LOCKED` supports horizontal
worker scaling.

A broker becomes justified when database work polling materially competes with
checkout traffic, when provider workloads need independent retention/replay, or
when cross-service consumers need the same events. At that point I would add a
transactional outbox rather than publish directly from reservation
transactions.

## Horizontal scaling

API instances are stateless and can scale horizontally behind a load balancer.
Workers can also scale horizontally because claims are database coordinated.
The standalone fake provider must remain a single demo process because its
state is intentionally in memory; a real provider owns its own durable state.

For production provider adapters, no correctness assumption depends on a worker
staying alive. Claim leases recover crashed local workers and provider
idempotency keys/reconciliation recover ambiguous remote side effects.

## Database pool and connection pressure

Every API instance and worker process has its own SQLAlchemy pool. Increasing
process counts therefore multiplies PostgreSQL connections. Pool sizes should
be set from the database connection budget rather than copied unchanged across
replicas. A connection pooler such as PgBouncer becomes useful before very
large replica counts.

## Non-atomic external boundary

No local database transaction can atomically commit an HTTP provider side
effect. The design explicitly accepts this and records UNKNOWN states when the
outcome cannot be proven. Reconciliation, stable idempotency keys, and
non-terminal UNKNOWN states are the recovery mechanism.

This means availability may be temporarily reduced during provider outages:
ambiguous inventory is intentionally not released for payment or reported as
success until truth is established.

## Provider contract growth

The demo uses "HOLD is final allocation". Providers that require a post-payment
commit would need a separate persisted capability and a CONFIRM worker using
the same claim/lease/idempotency/reconciliation pattern. The existing
`CONFIRM_PENDING` and `CONFIRM_UNKNOWN` line states leave room for that
without pretending the current HTTP adapter supports it.

## Observability and operations

Before production scale-out I would add:

- metrics by reservation/line state and age;
- oldest UNKNOWN and oldest leased-work gauges;
- provider latency/error/reconciliation metrics;
- alerts for growing `RELEASING`, `HOLD_UNKNOWN`, and
  `RELEASE_UNKNOWN` backlogs;
- structured logs containing reservation/provider IDs and operation keys but
  never provider credentials or secret payloads;
- dead-letter/manual-review tooling only after an operational timeout policy is
  defined.

## Next scale-out order

1. Tune database indexes, pool budgets and per-provider worker concurrency.
2. Add provider-specific rate limiting/circuit breaking.
3. Archive terminal workflow rows and use partial work indexes.
4. Add transactional outbox + broker only when PostgreSQL polling becomes a
   measured bottleneck or other services need the event stream.
5. Partition/shard inventory only for demonstrated hot-row or data-volume
   pressure.
