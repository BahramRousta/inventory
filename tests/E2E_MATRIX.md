# E2E Verification Matrix

All tests in this matrix use a real PostgreSQL database through
`TEST_DATABASE_URL`. Every E2E/API scenario asserts persisted database state,
not only HTTP responses. Provider scenarios use configurable capability-specific mock gateways; all
reservation/worker persistence still uses real PostgreSQL.

| Area | Scenario | Main test file |
|---|---|---|
| Health | health endpoint does not mutate persistence | `test_reservation_api_postgres.py` |
| Create | internal hold succeeds and persists ACTIVE/HELD | `test_reservation_api_postgres.py` |
| Create | duplicate lines canonicalize before stock mutation | `test_reservation_api_postgres.py` |
| Create | insufficient stock rolls back reservation + line + hold | `test_reservation_api_postgres.py` |
| Create | product/source mismatch | `test_reservation_api_postgres.py` |
| Create | disabled source/provider | `test_reservation_api_postgres.py` |
| Create | required trusted-user/idempotency headers | `test_reservation_api_postgres.py` |
| Create | invalid quantity and aggregate overflow | `test_reservation_api_postgres.py` |
| Idempotency | identical settled replay returns same reservation without a second hold | `test_reservation_api_postgres.py` |
| Idempotency | changed body with same key conflicts | `test_reservation_api_postgres.py` |
| Idempotency | pending external replay stays 202 and creates one work item | `test_external_provider_postgres.py` |
| Provider eligibility | query-only provider rejected for reservation workflow | `test_reservation_api_postgres.py` |
| Provider eligibility | missing reservation gateway rejected | `test_reservation_api_postgres.py` |
| Read | owner sees persisted snapshot | `test_reservation_api_postgres.py` |
| Read | missing reservation / wrong owner returns 404 | `test_reservation_api_postgres.py` |
| Cancel | ACTIVE internal -> RELEASING -> CANCELLED and hold released once | `test_reservation_api_postgres.py` |
| Cancel | wrong owner cannot mutate reservation | `test_reservation_api_postgres.py` |
| Cancel | duplicate cancellation does not double-release | `test_reservation_api_postgres.py` |
| Cancel | confirmed reservation cannot be cancelled | `test_reservation_api_postgres.py` |
| Payment | SUCCESS consumes internal hold and creates exactly one order + lines | `test_reservation_api_postgres.py` |
| Payment | multi-item SUCCESS creates one order containing every immutable line | `test_reservation_api_postgres.py` |
| Payment | duplicate event ID/same payload is idempotent | `test_reservation_api_postgres.py` |
| Payment | reused event ID/different payload conflicts | `test_reservation_api_postgres.py` |
| Payment | separate contradictory FAILURE after success is rejected | `test_reservation_api_postgres.py` |
| Payment | FAILURE releases held internal inventory | `test_reservation_api_postgres.py` |
| Payment | wrong owner records no payment outcome | `test_reservation_api_postgres.py` |
| Expiry | expired internal reservation releases and finishes EXPIRED | `test_reservation_api_postgres.py` |
| Expiry | late success cannot resurrect EXPIRED reservation | `test_reservation_api_postgres.py` |
| Direct confirm | deprecated admin flow finalizes once and is owner-scoped | `test_reservation_api_postgres.py` |
| Direct confirm | expired reservation cannot be confirmed | `test_reservation_api_postgres.py` |
| External HOLD | mock reservation provider success -> ACTIVE + external ref | `test_external_provider_postgres.py` |
| External HOLD | definitive provider decline -> compensation -> CANCELLED | `test_external_provider_postgres.py` |
| External HOLD | mock ambiguous outcome -> HOLD_UNKNOWN | `test_external_provider_postgres.py` |
| Reconciliation | GET_HOLD resolves ambiguous hold to HELD/ACTIVE | `test_external_provider_postgres.py` |
| External RELEASE | cancel releases upstream hold and persists RELEASED | `test_external_provider_postgres.py` |
| External RELEASE | mock unknown release -> RELEASE_UNKNOWN -> reconciliation -> CANCELLED | `test_external_provider_postgres.py` |
| External finalization | successful HOLD permits final local order creation | `test_external_provider_postgres.py` |
| Pre-HOLD failure | payment failure before provider call terminates pending work without remote hold | `test_external_provider_postgres.py` |
| Concurrency | two API checkouts compete for last unit; exactly one wins | `test_postgres_concurrency.py` |
| Worker claims | concurrent SKIP LOCKED claimers get different lines for the same provider | `test_postgres_concurrency.py` |
| Lease recovery | stale claim becomes UNKNOWN exactly once | `test_postgres_concurrency.py` |
| Payment/expiry | concurrent transition attempts preserve one local outcome | `test_postgres_concurrency.py` |
| Idempotency race | concurrent same create key produces one reservation/hold | `test_postgres_concurrency.py` |
| Payment race | concurrent delivery of the same event ID creates one event/order | `test_postgres_concurrency.py` |
| Payment/expiry | expired database TTL wins and late success is rejected | `test_postgres_concurrency.py` |

Run:

```bash
export TEST_DATABASE_URL=postgresql+psycopg://reservation:reservation@127.0.0.1:5454/reservation
pytest -m postgres tests/e2e tests/integration
```

The database must be disposable because the fixture creates and drops the full
schema for isolation.
