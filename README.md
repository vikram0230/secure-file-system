# Secure File System

A REST service for private file sharing. Users upload files, and the files are erasure-coded across a fleet of storage nodes: any 4 of 6 shards rebuild a file, so it survives the loss of 2 nodes. Owners can issue time-limited, HMAC-signed download links that keep working across service restarts. Every link issued and every download is recorded in an audit log.

- **Design and architecture diagrams:** [docs/DESIGN.md](docs/DESIGN.md)
- **DigitalOcean deployment:** [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
- **Build log:** [docs/PROGRESS.md](docs/PROGRESS.md)

```mermaid
flowchart LR
    C[Client] -->|HTTPS| API["API service (FastAPI)"]
    API <--> DB[(PostgreSQL)]
    W[Maintenance worker] <--> DB
    API <-->|"shards, private network"| N["Storage nodes ×6<br/>(4 data + 2 parity shards)"]
    W <-->|"health · GC · scrub · repair"| N
```

## Quick start (Docker)

Requirements: Docker with Compose v2.

```bash
cp .env.example .env
# Replace every <generate> with a fresh secret, and use the same POSTGRES_PASSWORD in SFS_DATABASE_URL:
for key in POSTGRES_PASSWORD SFS_SECRET_KEY SFS_NODE_TOKEN; do
  sed -i.bak "s|^$key=.*|$key=$(openssl rand -hex 32)|" .env
done
sed -i.bak "s|<POSTGRES_PASSWORD>|$(grep ^POSTGRES_PASSWORD= .env | cut -d= -f2)|" .env && rm .env.bak

docker compose up -d --build --wait
```

This starts Postgres, six storage nodes, a one-shot `migrate` job (runs the schema migrations and registers the nodes), the API on `localhost:8000`, and the maintenance worker.

Create a user. The API key is printed once and only its hash is stored:

```bash
docker compose exec api python scripts/create_user.py alice@example.com
# user_id: …
# api_key: sfs_…
export KEY=sfs_…
```

Upload a file, issue a link, and download through the link:

```bash
curl -s -H "Authorization: Bearer $KEY" -F "file=@README.md" localhost:8000/files
# {"id":"<file_id>", "size_bytes":…, "availability":{"state":"healthy","usable_shards":6,…}}

curl -s -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"ttl_seconds": 600}' localhost:8000/files/<file_id>/links
# {"url":"http://localhost:8000/download/<file_id>?exp=…&v=1&kid=k1&sig=…","expires_at":"…"}

curl -s -o downloaded.md "<url>"   # no API key needed; the signed URL is the credential
```

To see the fault tolerance, stop two nodes (`docker compose stop storage-node-1 storage-node-2`) and download again: it still succeeds. Stop a third and the download returns `503`. Within about 45 seconds the worker marks stopped nodes `unreachable`, and `GET /files/<file_id>` shows the file as `degraded` or `unavailable`.

Interactive API docs are at <http://localhost:8000/docs>. Tear everything down, including volumes, with `docker compose down -v`.

## API

| Method & path | Auth | Description |
|---|---|---|
| `POST /files` | API key | Multipart upload, field `file`. `201` returns metadata. |
| `GET /files?limit=&offset=` | API key | List your files (newest first; `limit` ≤ 100). |
| `GET /files/{id}` | API key | Metadata plus shard availability (`healthy` / `degraded` / `unavailable`). |
| `POST /files/{id}/links` | API key | `{"ttl_seconds": n}` (60 s – 7 days) → signed URL. Audited. |
| `DELETE /files/{id}` | API key | Deletes the file and revokes every link to it. |
| `GET /download/{id}?exp&v&kid&sig` | Signed URL | Public download; rate-limited per IP. Audited. |
| `GET /healthz` | — | Liveness check. |

Error codes:

| Code | Meaning |
|---|---|
| `401` | Missing or invalid API key |
| `403` | Bad, expired or revoked link |
| `404` | Not found, or not yours |
| `413` | File too large |
| `422` | Invalid input |
| `429` | Rate limited |
| `502` | A storage write failed; retry |
| `503` | Not enough healthy nodes, too few readable shards, server busy, or database down |

## Running tests

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest                      # unit tests only; database tests are skipped
```

The integration tests need a real Postgres. They run the Alembic migrations, spin up seven in-process storage nodes with fault injection, and cover upload, links, downloads under node outages and corruption, repair, and GC. The target database must be named `*_test`, because the suite truncates it; it's created automatically if missing.

```bash
docker compose up -d --wait postgres
set -a; . ./.env; set +a
export SFS_TEST_DATABASE_URL="postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@localhost:5432/sfs_test"
pytest
```

Lint and format checks: `ruff check src tests scripts migrations && ruff format --check src tests scripts migrations`.

CI (`.github/workflows/ci.yml`) runs lint, the full test suite against a Postgres service container, and both Docker image builds. In CI, database tests are required to run and are never skipped.

## Configuration

All settings are environment variables. Secrets and connection strings have no defaults, and the service refuses to start without them.

| Variable | Used by | Default | Notes |
|---|---|---|---|
| `SFS_DATABASE_URL` | API, worker, scripts | — | `postgresql+psycopg://user:pass@host:5432/db` |
| `SFS_SECRET_KEY` | API | — | Link-signing key, ≥ 32 chars |
| `SFS_SECRET_KEY_ID` | API | `k1` | Key ID (`kid`) embedded in links |
| `SFS_PREVIOUS_SECRET_KEYS` | API | *(empty)* | Retired keys still accepted for verification: `kid:key,…` |
| `SFS_NODE_TOKEN` | API, worker, nodes | — | Shared secret on every shard request, ≥ 32 chars |
| `SFS_NODE_SHARD_DIR` | nodes | — | Directory holding shards (never web-served) |
| `SFS_STORAGE_NODES` | seed script | — | `host:port,…` to register in `storage_nodes` |
| `SFS_SHARD_DATA_COUNT` / `SFS_SHARD_PARITY_COUNT` | API | `4` / `2` | Applies to new uploads; each file keeps the values it was stored with |
| `SFS_MAX_UPLOAD_BYTES` | API | 100 MB | Enforced as the body streams in |
| `SFS_MAX_CONCURRENT_TRANSFERS` | API | `4` | Per instance; beyond this, requests get `503` |
| `SFS_MIN_TTL_SECONDS` / `SFS_MAX_TTL_SECONDS` | API | `60` / 7 days | Bounds for link TTLs |
| `SFS_DOWNLOAD_RATE_PER_MINUTE` / `SFS_API_RATE_PER_MINUTE` | API | `60` / `120` | Per IP / per user, per instance |
| `SFS_WORKER_INTERVAL_SECONDS` | worker | `15` | Maintenance cycle length |
| `SFS_REPAIR_GRACE_SECONDS` | worker | `1800` | How long a node must be down before its shards are rebuilt |
| `FORWARDED_ALLOW_IPS` | API (uvicorn) | `127.0.0.1` | Proxies trusted for `X-Forwarded-For`; set to your load balancer |

## Operations

- **Rotating the signing key:** move the current key into `SFS_PREVIOUS_SECRET_KEYS` as `k1:<old>`, then set a new `SFS_SECRET_KEY` and `SFS_SECRET_KEY_ID=k2`. Links issued before the rotation keep working until they expire.
- **Rotating a user's API key:** `python scripts/create_user.py <email> --rotate`.
- **Adding a storage node:** start it with the same `SFS_NODE_TOKEN`, then register it with `SFS_STORAGE_NODES=<host:port> python scripts/seed_storage_nodes.py`. New uploads start using it immediately.
- **Retiring a storage node:** `UPDATE storage_nodes SET status = 'decommissioned' WHERE address = '…'`. The worker rebuilds that node's shards elsewhere on its next cycle.

## Project layout

```
src/api_service/        API service and maintenance worker
  main.py               app factory, middleware, error handling
  worker.py             worker entrypoint (single active instance via advisory lock)
  routers/              files (upload, list, links, delete), download
  services/             erasure coding, signing, auth, node client, file lifecycle, maintenance
  db/                   SQLAlchemy models and sessions
src/storage_node/       shard store: atomic durable writes, checksums, token auth
migrations/             Alembic
scripts/                seed_storage_nodes.py, create_user.py
tests/unit/             pure logic, no database
tests/integration/      real Postgres and an in-process 7-node fleet
```
