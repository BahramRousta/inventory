# Reconciliation and compensation flow

```mermaid
flowchart TD
    A[Reconciliation worker poll] --> B[Recover expired provider leases]
    B --> C[Claim HOLD_UNKNOWN and RELEASE_UNKNOWN rows]
    C --> D{Work claimed?}
    D -- no --> E[Wait for next poll]
    D -- yes --> F[Call provider.get_reservation outside transaction]
    F --> G{Lookup result}

    G -- RESERVED for hold --> H[Line -> HELD]
    H --> I{Reservation already RELEASING?}
    I -- yes --> J[Line -> RELEASE_PENDING]
    I -- no --> K{All lines HELD?}
    K -- yes --> L[Reservation -> ACTIVE]
    K -- no --> M[Remain RESERVING]

    G -- NOT_RESERVED for hold --> N[Line -> FAILED]
    N --> O[Reservation -> RELEASING]
    G -- UNKNOWN --> P[Return line to UNKNOWN]
    P --> E

    G -- NOT_RESERVED for release --> Q[Line -> RELEASED]
    Q --> R{All lines resolved?}
    R -- yes --> S[Reservation -> CANCELLED or EXPIRED]
    R -- no --> E
    G -- RESERVED for release --> T[Return line to RELEASE_PENDING]
    T --> E
```

```mermaid
sequenceDiagram
    participant RW as Release worker
    participant DB as PostgreSQL
    participant P as Provider

    RW->>DB: Claim RELEASING reservation and lines
    DB-->>RW: Local lines released and external lines RELEASE_PENDING
    RW->>DB: Claim RELEASE_PENDING with token + lease
    DB-->>RW: RELEASE_IN_PROGRESS
    RW->>P: release(external_ref) outside transaction
    P-->>RW: RELEASED / UNKNOWN
    RW->>DB: Persist result with claim-token CAS
    alt UNKNOWN
        DB-->>RW: RELEASE_UNKNOWN
        Note over DB,RW: Reconciliation will inquire later
    else RELEASED
        DB-->>RW: RELEASED
        Note over DB,RW: Finalize CANCELLED or EXPIRED when all lines resolve
    end
```

Unknown outcomes are retried through polling and lease recovery. The current
implementation has no attempt counter or exponential backoff; it relies on
the configured polling interval and provider claim lease.
