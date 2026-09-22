# Build Progress

Implementation plan for the FastAPI + erasure-coded distributed file service described in `docs/DESIGN.md`.

Legend: `[ ]` not started · `[~]` in progress · `[x]` done

## Stage 0 — Boilerplate & Project Scaffolding
- [x] Layered layout: `src/api_service/` (routers / db / schemas / services), `src/storage_node/`, `tests/unit/`, `tests/integration/`
- [x] `pyproject.toml` (Python 3.12, FastAPI, SQLAlchemy, Alembic, `zfec`, psycopg, httpx; pytest + ruff for dev)
- [x] Config via `pydantic-settings`: secrets and connection strings required, no hardcoded defaults
- [x] `docker-compose.yml`: 6 storage nodes + Postgres + API, secrets from git-ignored `.env`, each container gets only its own variables
- [x] Health check endpoints on both services

## Stage 1 — Metadata Layer
- [x] Models: `users`, `files`, `storage_nodes`, `shards`, `audit_events`, with TIMESTAMPTZ, CHECK constraints, unique constraints and indexes
- [x] k/m and `link_version` stored per file; API key hash on users; nullable audit `file_id`
- [x] Alembic initial migration (verified against Postgres, including constraint rejection)
- [x] Idempotent `scripts/seed_storage_nodes.py`; `storage_nodes` is the single source of truth for node addresses

## Stage 2 — Storage Node Service
- [x] `PUT/GET/DELETE /shards/{shard_id}`; canonical-UUID IDs only
- [x] Single-file format (digest + payload), atomic durable writes (temp file → fsync → link → dir fsync), immutable shards (409)
- [x] In-transit checksum header, at-rest digest verification, size cap (413), disk full (507), shared-token auth
- [x] Non-root multi-stage Docker images; verified under docker-compose

## Stage 3 — Erasure Coding Module
- [x] `zfec` encode/decode with padding, per-file k/m, single-block regeneration for repair (`services/erasure.py`)
- [x] Unit tests: round trip at edge sizes, all 15 four-of-six survivor sets, regeneration, other k/m schemes

## Stage 4 — Authentication & Upload
- [x] API-key auth (`Authorization: Bearer`, SHA-256 at rest), `scripts/create_user.py` with `--rotate`
- [x] `POST /files`: auth and rate limit run *before* the body is read; streamed size cap; filename/content-type sanitization; rows before bytes; parallel shard writes; rollback, or hand-off to GC if cleanup can't finish
- [x] Per-route body limits (upload cap on `POST /files`, 64 KiB elsewhere), per-instance transfer semaphore

## Stage 5 — Signed Links
- [x] `POST /files/{id}/links`: ownership check (404 for others' files), strict integer TTL bounds, versioned HMAC payload with `kid` and `link_version`, retired keys still verify
- [x] Audit `link_generated` with TTL and client IP

## Stage 6 — Public Download
- [x] `GET /download/{id}`: per-IP rate limit first, strict parse, constant-time verify, expiry, revocation check
- [x] Fetch k shards (healthy nodes and data shards first) with fallback on outage or corruption, decode, whole-file checksum, hardened headers (attachment, nosniff, CSP sandbox, no-store, no-referrer)
- [x] Audit `download_success` (committed before bytes are sent) and `download_denied`; 503 when the DB is down; signatures redacted from access logs

## Stage 7 — Metadata & Deletion
- [x] `GET /files` (paginated), `GET /files/{id}` with computed `healthy` / `degraded` / `unavailable` availability
- [x] `DELETE /files/{id}`: status and `link_version` bump, audit, best-effort shard removal (worker finishes the rest)

## Stage 8 — Maintenance Worker
- [x] Node health probing (threshold-based), stale-upload GC, deletion GC, scrub, repair onto spare healthy nodes after a grace period
- [x] Single active worker via a session-level advisory lock; exits if the lock connection drops

## Stage 9 — Testing
- [x] Unit: erasure coding, signing, filenames, rate limiter, config validation, middleware, storage node
- [x] Integration (real Postgres, migrated by Alembic, plus an in-process 7-node fleet with fault injection): upload → link → download, restart survival, key rotation, outages ≤ m and > m, corruption, revocation, ownership, rate limiting, DB outage, repair, scrub, GC — 160 tests, stable across repeated runs
- [x] Manual end-to-end run on the docker-compose stack: real node containers stopped and started, API restarted, worker detection and recovery observed

## Stage 10 — CI/CD & Documentation
- [x] GitHub Actions: lint + format check, full tests against a Postgres service container (DB tests may not skip in CI), both Docker image builds
- [x] README (quick start, API, tests, configuration, operations); `docs/DEPLOYMENT.md` aligned with the code; one-command `docker compose up` including migrations
