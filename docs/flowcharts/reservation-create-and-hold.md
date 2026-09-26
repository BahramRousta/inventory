# Create and provider-hold flow

```mermaid
flowchart TD
    A[POST /reservations] --> B[Validate user headers, idempotency key, and items]
    B --> C{Existing idempotency record?}
    C -- yes --> D[Return existing reservation]
    C -- no --> E[Begin PostgreSQL transaction]
    E --> F[Validate product/source ownership and enabled state]
    F --> G[Create reservation as RESERVING]
    G --> H{Source kind}
    H -- INTERNAL --> I[Guarded UPDATE internal_stock<br/>held += quantity if available]
    I -- insufficient --> X[Rollback entire create transaction]
    I -- success --> J[Create line as HELD]
    H -- EXTERNAL --> K[Create line as HOLD_PENDING]
    J --> L{Any external lines?}
    K --> L
    L -- no --> M[Set reservation ACTIVE]
    L -- yes --> N[Keep reservation RESERVING]
    M --> O[Commit transaction]
    N --> O
    O --> P[Return 201 or 202 + Location]

    N --> Q[Hold worker claims pending lines]
    Q --> R[Transaction: HOLD_PENDING -> HOLD_IN_PROGRESS<br/>assign token and lease]
    R --> S[Commit claim transaction]
    S --> T[Call provider.reserve outside transaction]
    T --> U{Provider result}
    U -- RESERVED --> V[Transaction: line -> HELD]
    V --> W{All lines HELD?}
    W -- yes --> Y[Reservation -> ACTIVE]
    W -- no --> Z[Remain RESERVING]
    U -- DECLINED --> AA[Line -> FAILED<br/>reservation -> RELEASING]
    U -- UNKNOWN/exception --> AB[Line -> HOLD_UNKNOWN]
```

The initial transaction is atomic for local state. Provider effects are not
part of that transaction. A result is persisted in a second transaction after
the provider call; if that write fails, lease recovery and reconciliation
investigate the provider instead of assuming failure.
