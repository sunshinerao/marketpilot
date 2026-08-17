# Production authentication and authorization

MarketPilot has two explicit ingress modes. `local` preserves the loopback-only demo
stack without credentials. `shared` and `production` enable fail-closed bearer-token
authentication and must be used before any API is reachable outside one operator's
machine. Authentication changes access to decision support only; it does not add an
order-submission interface or authorize automated execution.

## Startup configuration

Set `MARKETPILOT_ENV=production` and `MARKETPILOT_AUTH_MODE=production` for a production
runtime. A protected runtime refuses to start unless all three independent credentials
are present, unique, free of surrounding whitespace, and at least 32 characters:

- `MARKETPILOT_READ_ONLY_TOKEN`
- `MARKETPILOT_OPERATOR_TOKEN`
- `MARKETPILOT_REVIEWER_TOKEN`

Generate credentials with a cryptographically secure random generator and deliver them
through the platform secret manager. Do not put a value in `.env.example`, Compose
files, Git, image layers, command output, tickets, or logs. Rotate by updating the
secret-manager versions and restarting the API; the current static-token boundary does
not provide overlapping-key rotation. Use short rotation windows and revoke the old
deployment immediately after health and role checks pass.

If `MARKETPILOT_AUTH_MODE` is omitted, local/development/test environments select
`local`, `production` selects `production`, and every other environment selects
`shared`. Explicit `local` mode is rejected when `MARKETPILOT_ENV` is not a local,
development, or test environment. This prevents a production label from silently
starting with authentication bypassed.

## Role matrix

| Request | read-only | operator | reviewer |
| --- | ---: | ---: | ---: |
| Read any `/v1` resource | yes | yes | yes |
| Non-safe method outside `/v1/governance` | no | yes | no |
| Challenger registration, promotion, rollback | no | no | yes |
| `/health` | anonymous | anonymous | anonymous |

The operator and reviewer roles are intentionally separate. A reviewer credential
cannot run decisions, write feedback, start collectors, create attribution records, or
perform other operational writes. An operator credential cannot change model
governance. Use the standard header `Authorization: Bearer <role-token>`. Missing or
invalid credentials return `401`; a valid credential with the wrong role returns `403`.
Secret verification hashes the supplied value and compares every configured role digest
with constant-time comparisons, without logging or returning the token.

All `/v1` reads are authenticated in protected modes because decision history, alerts,
provider state, and governance metadata can be sensitive. `HEAD` and `OPTIONS` follow
the read policy. Every other HTTP method follows the write policy, so newly added write
routes are protected without relying on a developer to attach a per-route dependency.

## Health, API contract, and web proxy

`GET /health` stays anonymous for container and load-balancer liveness checks and returns
only status and service name. It must not grow database, provider, account, model, or
credential details. In shared/production mode `/docs`, `/redoc`, and `/openapi.json` are
not registered. Publish a reviewed API contract as a separate protected artifact when
operators need one.

The checked-in Compose stack explicitly uses `local` mode and publishes API and web only
on `127.0.0.1`; it is a developer harness, not a production ingress. The Next.js proxy
routes forward only the caller's `Authorization` header and preserve upstream `401/403`
responses. They do not contain a service token and cannot turn an anonymous browser into
an authenticated operator.

For a shared browser workbench, put both Next.js and FastAPI behind a TLS-terminating,
identity-aware gateway. The gateway must authenticate the human, map group membership to
exactly one MarketPilot role, inject the corresponding bearer credential on trusted
upstream connections, strip any client-supplied identity/authorization headers before
injection, enforce CSRF protection for browser writes, rate-limit failures, and emit
redacted access audit events. Do not expose the included local Compose ports or place a
role token in client-side JavaScript, `NEXT_PUBLIC_*`, cookies readable by JavaScript, or
repository configuration.

## Acceptance checks

Perform these checks through the actual production gateway before enabling shared
shadow use:

1. unauthenticated `/health` returns only the minimal payload;
2. unauthenticated and invalid-token `/v1` requests return `401`;
3. read-only can read but all writes return `403`;
4. operator can perform non-governance writes but governance writes return `403`;
5. reviewer can read and perform governance writes but other writes return `403`;
6. docs and OpenAPI return `404`;
7. tokens never appear in application, proxy, trace, or exception logs;
8. TLS, rate limiting, CSRF controls, credential rotation, revocation, and gateway audit
   retention are exercised and evidenced.

These application tests prove the in-process role boundary only. They do not certify an
external identity provider, TLS, WAF/rate limits, CSRF configuration, secret-manager
delivery, or production audit retention.

## Deployment identity

Shared and production startup also require `MARKETPILOT_CODE_VERSION` to identify
the pinned deployed artifact. The placeholder `development-unpinned` is rejected.
Shadow-readiness evaluation only counts sessions recorded with that exact runtime
identity.
