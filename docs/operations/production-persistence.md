# Production persistence boundary

MarketPilot's default runtime remains the local append-only SQLite safety store. The
production persistence package adds a synchronous PostgreSQL adapter and an explicit
encrypted landing contract without silently changing the running API.

## PostgreSQL audit store

1. Provision PostgreSQL with encrypted storage, TLS, backups, point-in-time recovery,
   least-privilege roles, and credentials delivered through a secret manager.
2. Apply `migrations/postgresql/0001_audit.sql` using a migration identity. The runtime
   identity must receive `SELECT` and `INSERT` only; it must not own the tables or the
   append-only trigger function.
3. Install `marketpilot[postgres]` in the deployment image. Construct
   `PostgreSQLAuditRepository(psycopg_connection_factory(secret_dsn))`. Resolve
   `secret_dsn` from a secret manager; do not place it in Git, command output, logs, or
   API responses.
4. Call `verify_schema()` before accepting traffic. Inject the repository through the
   existing decision and operations service constructors. Keep SQLite configured until
   a real PostgreSQL restore drill and parity run have passed.

The adapter provides idempotent inserts, immutable-content conflict detection,
transaction commit/rollback, point-in-time metadata, replay manifests, and append-only
recovery checkpoints. `integrity_check()` proves schema/readability and foreign-key
enforcement expectations; production backup validity still requires an actual restore
into an isolated PostgreSQL instance.

`PostgreSQLStreamAttributionStore` is the interface-compatible durable backend for SSE
alert projections, delivery-attempt evidence, reverse-attribution tasks, and reviews.
Its event sequence supports `Last-Event-ID`; reconnects may observe normal PostgreSQL
identity gaps but never reuse or reorder committed identifiers. Call
`verify_append_only_triggers()` during readiness checks.

`PostgreSQLFrozenChampionRegistry(PostgreSQLGovernanceStore(...))` can be injected into
`GovernanceService`. It persists registered versions, approvals and governance events,
serializes promotion/rollback per model with transaction-scoped advisory locks, and
persists session freezes. Approval and event writes commit atomically, approval reuse is
rejected, and a session freeze is serialized against champion changes. Validation result
artifacts themselves remain immutable external artifacts addressed by their hashes.
The decision path calls `freeze_session` for every run: LIVE session IDs come only from
the server clock in the configured New York/XNYS namespace, while caller-provided,
explicit scenario IDs are isolated under `SCENARIO:`. The runtime requires both governed
model version and declared artifact hash to match the loaded descriptor; either mismatch
produces `MODEL_VERSION_NOT_LOADED` and `NO_TRADE` without model output.

The declared artifact hash is a deterministic executable-contract identity retained for
append-only baseline compatibility. It is not source-tree or binary attestation. A
production deployment must additionally pin `MARKETPILOT_CODE_VERSION`, verify a signed
build manifest, and create a new model version plus explicit approval whenever model
implementation or behavior-changing parameters change. Never rewrite an existing
append-only model row to make a new build appear compatible.

## Licensed raw landing

`LicensedPayloadLandingService` is an internal ingestion/authorized-research boundary,
not an HTTP router or response schema. Mount production implementations for:

- `LandingAuthorizer`: workload identity plus dataset/purpose policy;
- `PayloadCipher`: authenticated envelope encryption backed by KMS/HSM;
- `EncryptedObjectStore`: private immutable object storage with retention controls;
- `LandingMetadataSink`: safe append-only receipts, optionally the PostgreSQL adapter.

Wrap input in `SensitivePayload`; its representation is redacted. The service authorizes
before encryption or storage, binds ciphertext to landing identity with associated data,
checks plaintext hashes on authorized reads, and returns only safe receipt metadata.
PostgreSQL stores no raw plaintext, ciphertext, nonce, wrapped key, or canonical licensed
content. Raw bytes must never be emitted through application logs, metrics, traces,
exception messages, or HTTP APIs.

## Recovery evidence

After each verified backup, append a `RecoveryCheckpoint` containing the backup
reference, PostgreSQL LSN, code/schema version, and replay manifest hash. A restore drill
must prove:

1. migration/schema version equality;
2. append-only triggers still reject updates and deletes;
3. audit/replay hashes match the checkpoint;
4. referenced encrypted objects are accessible only to authorized identities;
5. replay uses `first_seen_at <= virtual_clock` and produces the expected manifest.

This repository contains contract/fake tests only unless a PostgreSQL service is
explicitly provisioned. Passing them is not evidence of TLS, KMS, object-store IAM,
backup retention, or a successful real restore.

## Local PostgreSQL integration overlay

`compose.postgres.yaml` is a local migration/adapter integration harness. It requires
both passwords/DSNs from the shell and does not contain a committed credential:

```bash
export MARKETPILOT_POSTGRES_PASSWORD='<local-only-secret>'
export MARKETPILOT_POSTGRES_DSN='<local-secret-dsn-using-host-postgres>'
docker compose -f compose.yaml -f compose.postgres.yaml up --build
```

The overlay sets `MARKETPILOT_AUDIT_BACKEND=postgresql`; startup refuses traffic if the
DSN is absent, the migration is missing, an append-only trigger is disabled, or a
foreign-key constraint is unvalidated. This harness deliberately uses one local
database identity and therefore is not production role-separation evidence. Destroying
its named volume is a destructive action and is not part of normal `make down`.

### Local acceptance evidence (2026-08-16)

The explicit local overlay was exercised against `postgres:17-alpine`, not a fake
connection. The migration produced the `marketpilot` schema with 16 tables total
(one schema marker plus 15 audited tables) and 30
non-internal UPDATE/DELETE denial triggers. A direct UPDATE against a persisted decision
was rejected with `append-only audit table`. A decision and a promoted local champion
were both recovered after API restarts; promoting an artifact not loaded by the decision
runner produced `MODEL_VERSION_NOT_LOADED` and `NO_TRADE`, and an explicit rollback
restored the loaded baseline to non-executable `WAIT`.

This is local adapter/migration/restart evidence only. It does not certify production
TLS, split migration/runtime roles, KMS/HSM encryption, immutable object-store controls,
backup retention, disaster recovery, or any Webull/data entitlement.

## Isolated local backup/restore drill

`make postgres-restore-drill` performs a real `pg_dump`/`pg_restore` exercise against
two disposable `postgres:17-alpine` instances. It does not connect to the Compose
database, read a DSN, publish a port, or mount the `marketpilot-postgres` volume. Both
containers and both uniquely named volumes carry the same random
`marketpilot.restore-drill` label. Cleanup verifies that label before removing only
those temporary resources, including after a failed check.

Inspect the no-op plan first:

```bash
make postgres-restore-drill-plan
```

Run the drill (Docker must be available; the image may be pulled if absent):

```bash
make postgres-restore-drill
```

The source fixture includes a `NO_TRADE` decision, replay manifest, recovery checkpoint,
alert, champion/challenger lineage, promotion evidence, and a frozen session. The drill
backs it up, restores it into a fresh PostgreSQL data volume, and requires all of the
following before reporting `PASS`:

- schema version `1`, 16 tables total (15 audited), and 30 enabled append-only triggers;
- 8 validated foreign keys plus a deliberately rejected invalid FK insert;
- deliberately rejected UPDATE and DELETE operations;
- exact SHA-256 equality for decision, replay-manifest, checkpoint, and aggregate
  governance rows between source and restored databases;
- a checkpoint whose manifest and schema references resolve in the restored database.

The machine-readable result is written atomically to
`output/postgres-restore-drill/latest.json`. It contains no DSN, credential, raw licensed
payload, or provider secret. This fixture drill proves the migration and logical backup
path; it still does not certify a production backup service, retention policy, encrypted
object recovery, RPO/RTO, production IAM, TLS, or KMS/HSM controls.
