# Alert delivery outbox safety harness

`POST /v1/scenario/alert-delivery/run` exercises alert delivery policy without granting
network authority. The route requires `run_mode=SCENARIO` and `scope=LOCAL`; every
response is `UNVERIFIED`, `execution_enabled=false`, and `NO_TRADE`.

Safety boundaries:

- all WEBHOOK, EMAIL, and MOBILE channels default to disabled and require explicit
  opt-in plus an opaque authorization reference;
- the default `NullDeliverySender` performs no I/O and records a disabled attempt;
- the router supports only the null sender and a deterministic local scripted sender;
- no HTTP, SMTP, push, DNS, URL resolution, or provider SDK implementation exists;
- destination values are opaque allowlisted identifiers. URLs, email addresses, paths,
  whitespace, and arbitrary endpoint strings are rejected, preventing SSRF through this
  interface;
- the service has a second `network_authorized` gate for any future injected
  network-capable sender. The scenario router fixes that gate to `false`.

The outbox message, state-transition events, and delivery attempts are separate
append-only SQLite records. Database triggers reject updates and deletes. Messages are
HMAC-SHA256 signed over canonical content before enqueue; the scenario key is an
explicitly non-secret fixture and must never be reused for production.

The coordinator implements fingerprint/destination/channel deduplication, cooldown,
exponential retry with a bounded attempt budget, permanent-failure and retry-exhaustion
dead letters, acknowledgments, and bounded unacknowledged-alert escalation. Sender
exceptions are converted to a sanitized reason code; exception text is not persisted.

Run the focused suite with:

```bash
.venv/bin/pytest tests/test_alert_delivery_outbox.py -q
```

Production delivery remains intentionally unimplemented. A future adapter must resolve
opaque destination references from an authorized allowlist, inject the sender outside
the scenario router, provide a real secret through a secret manager, and satisfy both
the channel policy and runtime network-authorization gates.
