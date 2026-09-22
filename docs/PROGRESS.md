# Build Progress

Implementation plan for the FastAPI + erasure-coded distributed file service described in `docs/DESIGN.md`. Work proceeds stage by stage; each stage is implemented, then paused for approval before starting the next.

Legend: `[ ]` not started · `[~]` in progress · `[x]` done, approved

## Stage 0 — Boilerplate & Project Scaffolding
- [x] Repo layout: `src/api_service/` (API service, layered: routers/db/schemas/services), `src/storage_node/` (shard-store service), `tests/unit/`, `tests/integration/`
- [x] `pyproject.toml`, dependency set (FastAPI, uvicorn, SQLAlchemy, Alembic, `reedsolo`, `psycopg`, pytest, httpx)
- [x] Config loading (`pydantic-settings`; `SECRET_KEY`/DB URL/storage node list are required, no hardcoded defaults)
- [x] `docker-compose.yml` simulating the 6-node fleet + Postgres locally, secrets/endpoints via git-ignored `.env.docker` (not inlined in the compose file)
- [x] Base FastAPI app skeleton with health check endpoint — verified both services boot and respond to `/healthz`

## Stage 1 — Metadata Layer
- [x] SQLAlchemy models: `users`, `files`, `storage_nodes`, `shards`, `audit_events`
- [x] Alembic migration setup + initial migration — applied against Postgres in docker-compose and verified all 5 tables + FKs created correctly
- [x] DB session/connection management (`db/session.py`), seed script for local storage nodes (`scripts/seed_storage_nodes.py`) — verified it populates `storage_nodes` from `SFS_STORAGE_NODES`

## Stage 2 — Storage Node Service
- [ ] Minimal FastAPI app exposing `PUT/GET/DELETE /shards/{shard_id}`
- [ ] Writes to a non-public directory on local disk, UUID-named files
- [ ] Per-shard checksum verification on write and read

## Stage 3 — Erasure Coding Module
- [ ] Encode: split bytes into 4 data + 2 parity shards (Reed-Solomon 4+2, `reedsolo`)
- [ ] Decode: reconstruct original bytes from any 4 of 6 shards
- [ ] Unit tests for encode/decode round-trip, including simulated shard loss

## Stage 4 — Upload Endpoint
- [ ] `POST /files` — auth, validation (size/content-type), encode, parallel shard writes across nodes
- [ ] Metadata + audit rows written; partial-failure cleanup path
- [ ] Input validation & error handling per `DESIGN.md` §5

## Stage 5 — Signed URL Generation
- [ ] `POST /files/{file_id}/sign` — ownership check, stateless HMAC token, TTL bounds
- [ ] Audit event on every link issuance

## Stage 6 — Download / Retrieval Endpoint
- [ ] `GET /download` — signature + expiry validation (constant-time compare)
- [ ] Fetch any 4 of 6 shards, decode, stream response
- [ ] Audit event on success/denial; graceful handling of node outages

## Stage 7 — Metadata Query Endpoints
- [ ] `GET /files` / `GET /files/{file_id}` — owner-scoped file status, size, upload date, shard health summary

## Stage 8 — Testing
- [ ] Unit tests: erasure coding, HMAC signing/verification, validation logic
- [ ] Integration tests: full upload → sign → download flow, including simulated node failure (≤2 nodes down)
- [ ] Edge-case tests from `DESIGN.md` §5 (expired link, tampered signature, wrong owner, oversized file, etc.)

## Stage 9 — CI/CD & Documentation
- [ ] GitHub Actions workflow: lint, run tests, build check
- [ ] README: setup, running locally (docker-compose), running tests, example requests

---

Next: **Stage 0 — Boilerplate & Project Scaffolding**, pending your go-ahead.
