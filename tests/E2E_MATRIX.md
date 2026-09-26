# PostgreSQL BDD Integration Matrix

All tests in this matrix are PostgreSQL-backed integration scenarios through
`TEST_DATABASE_URL`. Each scenario follows **Given / When / Then** phases:
seed state through `tests/conftest.py`, call the HTTP boundary or worker use
case, then assert persisted database rows. There are no SQLite, unit-only, or
API-only test suites.

Shared data builders, HTTP setup, and database lifecycle are owned by
`tests/conftest.py`; scenario files must not construct catalog/provider/stock
records directly.

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
| Provider execution | query-style provider reserves through availability semantics | `test_external_provider_postgres.py` |
| Create | enabled external source is accepted before provider processing | `test_reservation_api_postgres.py` |
| Read | owner sees persisted snapshot | `test_reservation_api_postgres.py` |
| Read | missing reservation / wrong owner returns 404 | `test_reservation_api_postgres.py` |
| Cancel | ACTIVE internal -> RELEASING -> CANCELLED and hold released once | `test_reservation_api_postgres.py` |
| Cancel | wrong owner cannot mutate reservation | `test_reservation_api_postgres.py` |
| Cancel | duplicate cancellation does not double-release | `test_reservation_api_postgres.py` |
| Cancel | confirmed reservation cannot be cancelled | `test_reservation_api_postgres.py` |
| Expiry | expired internal reservation releases and finishes EXPIRED | `test_reservation_api_postgres.py` |
| Expiry | late success cannot resurrect EXPIRED reservation | `test_reservation_api_postgres.py` |
| Confirm | confirmation finalizes once and is owner-scoped | `test_reservation_api_postgres.py` |
| Confirm | expired reservation cannot be confirmed | `test_reservation_api_postgres.py` |
| External HOLD | mock reservation provider success -> ACTIVE + external ref | `test_external_provider_postgres.py` |
| External HOLD | definitive provider decline -> compensation -> CANCELLED | `test_external_provider_postgres.py` |
| External HOLD | mock ambiguous outcome -> HOLD_UNKNOWN | `test_external_provider_postgres.py` |
| Reconciliation | GET_HOLD resolves ambiguous hold to HELD/ACTIVE | `test_external_provider_postgres.py` |
| External RELEASE | cancel releases upstream hold and persists RELEASED | `test_external_provider_postgres.py` |
| External RELEASE | mock unknown release -> RELEASE_UNKNOWN -> reconciliation -> CANCELLED | `test_external_provider_postgres.py` |
| External finalization | successful HOLD permits final local order creation | `test_external_provider_postgres.py` |
| Concurrency | two API checkouts compete for last unit; exactly one wins | `test_postgres_concurrency.py` |
| Worker claims | concurrent SKIP LOCKED claimers get different lines for the same provider | `test_postgres_concurrency.py` |
| Lease recovery | stale claim becomes UNKNOWN exactly once | `test_postgres_concurrency.py` |
| Idempotency race | concurrent same create key produces one reservation/hold | `test_postgres_concurrency.py` |

Run:

```bash
export TEST_DATABASE_URL=postgresql+psycopg://reservation:reservation@127.0.0.1:5454/reservation
pytest -m postgres tests/e2e tests/integration
```

The database must be disposable because the fixture creates and drops the full
schema for isolation.
