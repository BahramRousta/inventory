# SCALABILITY

## 1. Current deployment model

The implementation is intentionally small:

```text
API processes
    |
    v
PostgreSQL
    ^
    |
    +-- hold workers
    +-- release workers
    +-- reconciliation workers
    +-- expiry workers
```

PostgreSQL is the only durable infrastructure component.

The API processes are stateless and can be replicated. Workers are also
replicable because claims are coordinated through database row locks, claim
tokens, and leases.

Default worker configuration:

```text
reservation TTL       = 900 seconds
poll interval         = 1 second
claim batch size      = 500
worker concurrency    = 5
claim lease           = 60 seconds
```

These values are configuration defaults, not throughput guarantees.

## 2. Expected first bottleneck: hot inventory rows

For internal inventory, reservation uses a guarded atomic update of one
`internal_stock` row.

That is correct but means a very popular stock source becomes a serialization
point:

```text
many checkout requests
        |
        v
same internal_stock row
        |
        v
row-update contention
```

This is the first database bottleneck I would expect for flash-sale style
traffic.

I would keep this design until contention is measured because it gives a simple
and strong no-oversell invariant.

Signals that justify changing it:

- materially increasing lock wait time on `internal_stock`;
- checkout latency dominated by inventory-row updates;
- high abort/retry rate for a small number of hot SKUs;
- database CPU is not saturated but transactions are waiting on the same rows.

Possible next designs, in increasing complexity:

1. split one logical stock source into physical inventory buckets where that
   matches the warehouse model;
2. pre-allocate inventory pools to partitions/regions;
3. serialize extremely hot SKU mutations through a dedicated queue;
4. shard inventory ownership only when the operational model requires it.

I would not introduce sharding preemptively because cross-shard reservation and
rebalancing are significantly harder than a single guarded SQL update.

## 3. External providers are usually the first end-to-end throughput limit

Remote providers have independent latency, availability, and rate limits.

The current design keeps those calls outside database transactions, so a slow
provider does not hold SQL locks. However, provider latency still determines
how quickly external reservations leave `RESERVING`.

Approximate worker capacity for one process is bounded by:

```text
concurrency / average provider latency
```

For example, with concurrency 5, increasing the batch size above 500 does not
itself increase provider-call throughput.

The metrics I would watch per provider are:

- reserve latency p50/p95/p99;
- release latency;
- error/timeout rate;
- decline rate;
- number and age of `HOLD_PENDING`;
- number and age of `HOLD_UNKNOWN`;
- number and age of `RELEASE_UNKNOWN`;
- reconciliation success rate.

Scale worker concurrency only within the provider's allowed rate limit.

## 4. Provider isolation

Today a worker uses one global concurrency setting.

At moderate scale that is sufficient. At higher provider volume, one slow or
unhealthy provider can consume worker slots and increase latency for healthy
providers.

A concrete trigger for per-provider isolation is:

> one provider's queue age grows or its latency/errors materially affect
> processing latency for unrelated providers.

At that point I would introduce:

- per-provider concurrency limits;
- per-provider rate limiting;
- circuit breakers/backoff;
- optionally separate worker pools by provider.

This change should happen before adding a general-purpose broker.

## 5. PostgreSQL work-queue scaling

Workers select work using statuses and
`FOR UPDATE SKIP LOCKED`.

Relevant indexes are:

```text
reservations(status, expires_at)
reservation_lines(status, provider_lease_until)
```

`SKIP LOCKED` lets multiple workers claim independent rows without waiting on
each other.

The likely database problems at larger history sizes are:

- scanning too many terminal rows;
- index growth;
- vacuum pressure from frequent state updates;
- increased contention between API transactions and worker scans.

Signals that justify work-queue changes:

- work-claim queries become a meaningful fraction of database load;
- p95 claim latency grows as terminal history grows;
- workers cannot keep up even though provider capacity is available;
- autovacuum/index maintenance becomes operationally significant.

Before introducing a broker I would:

1. add/verify partial indexes targeting only active work states;
2. archive old terminal reservations;
3. keep claim batches bounded;
4. tune polling intervals;
5. separate API and worker database connection budgets.

## 6. Why no message broker yet

A broker is not automatically more scalable.

Introducing Kafka/RabbitMQ/SQS would create two durability domains:

```text
PostgreSQL transaction
+
message publication
```

Correct publication would then require an outbox/inbox design.

For the assignment, the work already exists durably in PostgreSQL and workers
can recover it after crashes, so polling is a simpler design.

I would introduce a broker when at least one of these becomes true:

- PostgreSQL polling measurably competes with checkout traffic;
- provider work needs much higher independent throughput;
- multiple other services need reservation events;
- retention/replay requirements exceed what the operational reservation tables
  should provide;
- queue age cannot be controlled economically by database workers.

The migration path would be:

```text
reservation transaction
    |
    +-- write outbox row atomically
    |
outbox publisher
    |
broker
    |
provider workers / other consumers
```

I would not publish directly to a broker inside the reservation transaction.

## 7. Expiration scanning

Expiry workers search reservations by:

```text
(status, expires_at)
```

The existing index supports this access pattern.

The scan remains inexpensive while the active reservation set is bounded.

Signals that require a different strategy:

- expiry scans consume noticeable database IO;
- millions of simultaneously active reservations;
- expiration precision requirements become tighter than the polling interval;
- worker lag causes reservations to remain held materially past their TTL.

Possible next steps:

1. tune batch size and poll interval;
2. use partial indexes for expirable states;
3. partition/archive old reservations;
4. only then consider a dedicated delayed-queue/timer system.

## 8. Database connection pressure

Horizontal scaling of API and worker processes multiplies connection pools.

The database connection budget should be treated as:

```text
API replicas * API pool size
+
worker replicas * worker pool size
+
migration/admin connections
<= PostgreSQL connection budget
```

A large number of stateless replicas is not useful if they exhaust PostgreSQL
connections.

A connection pooler such as PgBouncer becomes useful before very large replica
counts.

## 9. Confirmation contention

Confirmation uses an atomic state transition:

```text
ACTIVE -> CONFIRMING
```

followed by local inventory consumption, line confirmation, and order creation
in one transaction.

The unique order constraint:

```text
orders.reservation_id UNIQUE
```

protects against duplicate order creation.

The main scalability property is that confirmation locks only the reservation
and inventory rows involved in that checkout; there is no global lock.

For large carts, transaction duration grows with line count. A practical limit
on reservation line count would therefore be appropriate in production.

## 10. Non-atomic external-provider boundary

No SQL transaction can atomically include a remote provider operation.

The current pattern is:

```text
claim in DB
commit
call provider
persist result in DB
```

This means a worker may crash after the provider acts but before the local
result is stored.

The design handles this with:

- stable reservation keys;
- `UNKNOWN` states;
- lease recovery;
- provider status lookup/reconciliation.

This recovery model scales better than holding a database transaction open
during remote calls.

The operational tradeoff is temporary uncertainty: during provider outages,
some inventory may remain unavailable while reconciliation is pending.

## 11. Query-only provider limitation at scale

A query-only provider does not provide globally exclusive stock ownership.

That is not primarily a throughput problem; it is a correctness boundary.

At low traffic a best-effort availability check may appear sufficient. Under
higher contention the probability of external oversell increases because other
clients of that provider can consume the same inventory after the check.

Therefore the trigger is not database load. The trigger is the business
requirement:

> if checkout must guarantee external inventory, query-only providers cannot be
> treated as equivalent to a real HOLD provider.

At that point the options are:

- reject that provider for guaranteed checkout;
- require the provider to expose an allocation/HOLD API;
- accept and explicitly expose a weaker best-effort guarantee.

No amount of local scaling can create a lock in another system.

## 12. Data growth

The largest growing tables are expected to be:

- reservations;
- reservation lines.

Most operational worker queries care only about non-terminal states.

When terminal history becomes large, I would:

1. archive old confirmed/cancelled/expired reservations to a history store or
   partition;
2. keep active-work indexes small with partial indexes;
3. preserve order/reservation identifiers needed for audit;
4. avoid deleting unresolved provider work until reconciliation policy allows
   it.

A change is justified when operational query latency or maintenance cost is
measurably affected by historical data.

## 13. Caching

Redis is not required for reservation correctness.

Caching product or read-only reservation views may reduce read load, but writes
must continue to use PostgreSQL as the authority.

I would not cache available internal inventory as the source of truth because
that would create another consistency problem around stock mutation.

A cache becomes useful only if read traffic is demonstrated to dominate
database load.

## 14. Observability required before scaling

Scaling decisions should be based on measured bottlenecks.

Minimum production metrics:

### API

- request rate;
- p50/p95/p99 latency;
- error rate by endpoint.

### Database

- transaction latency;
- lock wait time;
- deadlocks;
- connection usage;
- slow work-claim queries;
- vacuum/index growth.

### Reservation lifecycle

- count by reservation status;
- age of oldest `RESERVING`;
- age of oldest `RELEASING`;
- expiry lag.

### Provider workflow

- reserve/release/lookup latency;
- provider errors and timeouts;
- pending/unknown backlog by provider;
- lease recoveries;
- reconciliation age.

These measurements determine whether the next investment belongs in the
database, worker pool, provider isolation, or message infrastructure.

## 15. Scale-out order

I would evolve the system in this order:

1. measure latency, locks, provider queue age, and connection pressure;
2. tune SQL indexes, transaction duration, and connection pools;
3. tune worker batch size/concurrency per provider;
4. add provider-specific rate limiting and circuit breaking;
5. archive terminal data and add partial active-work indexes;
6. add a transactional outbox and broker only when database polling is a
   measured bottleneck or events need multiple consumers;
7. partition/shard inventory only when hot-row contention or data volume proves
   it is necessary.

This order preserves the current correctness model and adds complexity only
when there is evidence that the simpler design is no longer sufficient.
