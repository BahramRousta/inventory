"""Legacy E2E entry point.

Comprehensive PostgreSQL-backed API/database coverage now lives in
`test_reservation_api_postgres.py` and `test_external_provider_postgres.py`.
Keeping this module prevents stale SQLite-based E2E coverage from silently
testing a weaker persistence model than the assignment requires.
"""
