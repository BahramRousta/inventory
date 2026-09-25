from typing import AsyncContextManager, Protocol


class ProviderWorkLock(Protocol):
    """Cross-worker duplicate-work guard for one provider operation key."""

    def try_acquire(self, operation_key: str) -> AsyncContextManager[bool]: ...
