# Reservation lifecycle

```mermaid
stateDiagram-v2
    [*] --> RESERVING: external work exists
    [*] --> ACTIVE: internal-only create

    RESERVING --> ACTIVE: every line HELD
    RESERVING --> RELEASING: definitive hold failure
    RESERVING --> RELEASING: TTL expiry
    ACTIVE --> CONFIRMING: confirm request
    ACTIVE --> RELEASING: cancel request
    ACTIVE --> RELEASING: TTL expiry

    CONFIRMING --> CONFIRMED: all lines confirmed + order created
    RELEASING --> CANCELLED: all lines FAILED or RELEASED
    RELEASING --> EXPIRED: release reason is EXPIRED

    CONFIRMED --> [*]
    CANCELLED --> [*]
    EXPIRED --> [*]
```

```mermaid
stateDiagram-v2
    [*] --> HOLD_PENDING: external line created
    HOLD_PENDING --> HOLD_IN_PROGRESS: hold worker claim
    HOLD_IN_PROGRESS --> HELD: provider RESERVED
    HOLD_IN_PROGRESS --> FAILED: provider DECLINED
    HOLD_IN_PROGRESS --> HOLD_UNKNOWN: timeout/exception
    HOLD_UNKNOWN --> HOLD_IN_PROGRESS: reconciliation claim

    HELD --> CONFIRMED: local confirmation
    HELD --> RELEASE_PENDING: compensation claim
    RELEASE_PENDING --> RELEASE_IN_PROGRESS: release worker claim
    RELEASE_IN_PROGRESS --> RELEASED: provider released
    RELEASE_IN_PROGRESS --> RELEASE_UNKNOWN: ambiguous release
    RELEASE_UNKNOWN --> RELEASE_IN_PROGRESS: reconciliation claim
    RELEASE_UNKNOWN --> RELEASE_PENDING: provider still RESERVED
```

There is no reservation-level `FAILED` state. A definitive line failure moves
the reservation to `RELEASING`; compensation then ends it as `CANCELLED` (or
`EXPIRED` when the reason is TTL expiry).

