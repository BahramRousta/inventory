# Confirmation flow

```mermaid
sequenceDiagram
    participant C as Checkout caller
    participant API as Reservation API
    participant DB as PostgreSQL
    participant P as External provider

    C->>API: POST /reservations/{id}/confirm
    API->>DB: Verify owner and ACTIVE state
    DB-->>API: Reservation and HELD lines
    API->>DB: ACTIVE -> CONFIRMING (CAS, unexpired)
    API->>DB: Consume internal held stock
    API->>DB: Mark all lines CONFIRMED
    API->>DB: CONFIRMING -> CONFIRMED
    API->>DB: Create order with unique reservation_id
    DB-->>API: Commit
    API-->>C: CONFIRMED + order_id
```

External provider calls are not made during this confirmation transaction in
the current implementation. External lines already represent successful
provider holds; confirmation finalizes the local reservation and order.

