# DigitalOcean Deployment Guide

Step-by-step setup of the architecture in `docs/DESIGN.md` §10, mostly through the DigitalOcean control panel (cloud.digitalocean.com). Order matters: the VPC comes first, because every other resource must join it.

**What you'll end up with:**
- a private VPC
- DO Managed PostgreSQL
- 6 storage-node Droplets, each with an attached Volume and no public IP
- the API service and the maintenance worker, both inside the VPC

Only the API is reachable from the internet.

> Variable names below match the code (`src/api_service/config.py`, `src/storage_node/config.py`). Generate every secret with `openssl rand -hex 32`, and never reuse the values from your local `.env`.

## 0. Prerequisites

- A DigitalOcean account, with [`doctl`](https://docs.digitalocean.com/reference/doctl/how-to/install/) installed and authenticated (`doctl auth init`).
- Docker with `buildx`, to build `linux/amd64` images.
- Three secrets, generated now and kept in a password manager:
  - `SFS_SECRET_KEY`: signs download links
  - `SFS_NODE_TOKEN`: shared between the API, the worker and every node
  - the database password (DigitalOcean generates this for you in step 3)

## 1. Create a project

1. **Projects → New Project**. Name it `secure-file-system`, environment "Production".
2. Assign every resource below to this project as you create it.

## 2. Create the private VPC

1. **Networking → VPC → Create VPC Network**.
2. Choose one region, for example `nyc3`. Every resource must be in this region.
3. Name it `sfs-vpc` and note its IP range, for example `10.10.0.0/16`. The firewall rules below use it.

## 3. Create the managed PostgreSQL database

1. **Databases → Create Database Cluster** → PostgreSQL 16, in the same region, VPC `sfs-vpc`. Name it `sfs-db`.
2. When it's ready, go to **Users & Databases**: create a database `sfs` and a user `sfs_app`. This user owns the schema, so it can run the migrations.
3. Go to **Connection Details** and choose **VPC network**, not public, for `sfs_app` / `sfs`. Copy the connection string.
4. Convert it into `SFS_DATABASE_URL` by changing the scheme and keeping the TLS requirement:
   ```
   postgresql://sfs_app:…@private-sfs-db-….db.ondigitalocean.com:25060/sfs?sslmode=require
   → postgresql+psycopg://sfs_app:…@private-sfs-db-….db.ondigitalocean.com:25060/sfs?sslmode=require
   ```
5. Under **Settings → Trusted Sources**, allow only the API/worker app (or their Droplets) once they exist.

## 4. Build and push the images

1. **Container Registry → Create**, for example `sfs-registry`.
2. From the repo root:
   ```bash
   doctl registry login
   REG=registry.digitalocean.com/sfs-registry
   TAG=$(git rev-parse --short HEAD)
   docker buildx build --platform linux/amd64 -f Dockerfile.api          -t $REG/sfs-api:$TAG          --push .
   docker buildx build --platform linux/amd64 -f Dockerfile.storage_node -t $REG/sfs-storage-node:$TAG --push .
   ```

## 5. Create the storage-node Droplets

Create 6 Droplets. You can do them all at once with the Quantity selector.

1. **Droplets → Create Droplet**. Choose the same region, Ubuntu LTS, and a plan sized for your expected shard volume.
2. **VPC:** `sfs-vpc`. **Leave public IPv4 off.** For management, use the web console in the control panel or a bastion host.
3. **Volumes:** add one Volume per Droplet, for example 100 GB, *automatically formatted and mounted*. It mounts at `/mnt/<volume_name>`.
4. **Hostnames:** `sfs-node-1` … `sfs-node-6`.
5. On each node, open the Droplet's **Console** and run:
   ```bash
   apt-get update && apt-get install -y docker.io
   VOL=/mnt/<volume_name>
   chown 10001:10001 "$VOL"            # the container runs as non-root uid 10001

   install -d -m 700 /etc/sfs
   install -m 600 /dev/null /etc/sfs/node.env
   cat > /etc/sfs/node.env <<'EOF'
   SFS_NODE_SHARD_DIR=/data
   SFS_NODE_TOKEN=<the shared node token>
   EOF

   PRIVATE_IP=$(curl -s http://169.254.169.254/metadata/v1/interfaces/private/0/ipv4/address)
   # Read-only registry token via stdin, so it never lands in shell history:
   read -rs DOCR_TOKEN && echo "$DOCR_TOKEN" | docker login registry.digitalocean.com -u "$DOCR_TOKEN" --password-stdin
   docker run -d --name storage-node --restart unless-stopped \
     --env-file /etc/sfs/node.env \
     -v "$VOL":/data \
     -p "$PRIVATE_IP":8000:8000 \
     registry.digitalocean.com/sfs-registry/sfs-storage-node:<TAG>
   curl -s "http://$PRIVATE_IP:8000/healthz"   # {"status":"ok"}
   ```
   Binding to the private IP gives a second layer of protection behind the firewall in step 6.
6. Write down all 6 private IPs. They're registered in step 8.

## 6. Firewall the storage nodes

1. **Networking → Firewalls → Create Firewall**, named `sfs-nodes`.
2. **Inbound:** allow TCP `8000` from the VPC range only (for example `10.10.0.0/16`). Remove every other inbound rule, or allow SSH only from a bastion host.
3. Apply it to all six storage-node Droplets. Tagging them `sfs-node` and applying the firewall to the tag means future nodes are covered automatically.

## 7. Deploy the API and the worker

The storage nodes have no public IP, so the API must run **inside `sfs-vpc`**.

**Option A: App Platform** (use this if App Platform VPC networking is available in your region; check the app's *Networking* settings).

1. **Apps → Create App → Container Image →** `sfs-registry/sfs-api:<TAG>`.
2. **Web service component:**
   - HTTP port `8000`, health check path `/healthz`, 2 or more instances.
   - The image's default command already runs `uvicorn --factory api_service.main:create_app … --no-access-log`.
3. **Add a Worker component** from the same image, with run command `python -m api_service.worker`, **1 instance**. A second instance would only sit as a hot standby, because of the advisory lock.
4. **Add a Job component**, kind **Pre-deploy**, with run command `alembic upgrade head`. Migrations then run before every release.
5. **Networking:** attach the app to `sfs-vpc`.
6. **Environment variables** (app level, all marked *Encrypted*):
   - `SFS_DATABASE_URL`: from step 3
   - `SFS_SECRET_KEY`: the link-signing key
   - `SFS_NODE_TOKEN`: the same value as on the nodes
   - `FORWARDED_ALLOW_IPS=*`: this is safe only because App Platform's ingress is the sole route to the container. It makes client IPs in audit logs and rate limits accurate.
7. **Create Resources.** App Platform provisions HTTPS automatically.

**Option B: Droplets behind a DO Load Balancer.**

1. Create 2 or more Droplets in `sfs-vpc` and run the `sfs-api` image with the environment variables above.
2. Set `FORWARDED_ALLOW_IPS` to the load balancer's private address.
3. Run the worker on one of these Droplets with `docker run … sfs-api:<TAG> python -m api_service.worker`.
4. Create a **Load Balancer** in `sfs-vpc`: HTTPS 443 → HTTP 8000, health check `/healthz`, with a managed Let's Encrypt certificate.
5. Run `alembic upgrade head` once from an API Droplet before sending traffic.

## 8. Register storage nodes and create users

Run these one-off commands from an API instance. On App Platform, use the component's **Console** tab; on Droplets, use `docker exec`.

```bash
SFS_STORAGE_NODES=10.10.0.5:8000,10.10.0.6:8000,10.10.0.7:8000,10.10.0.8:8000,10.10.0.9:8000,10.10.0.10:8000 \
  python scripts/seed_storage_nodes.py
# registered 6 new storage node(s); 6 configured

python scripts/create_user.py alice@example.com
# api_key: sfs_…   ← shown once; deliver it to the user securely
```

## 9. DNS (optional)

**Networking → Domains**: add your domain and point a CNAME at the App Platform hostname, or an A record at the load balancer. For App Platform, also add the domain under the app's **Settings → Domains** so the certificate covers it.

## 10. Verify end to end

1. `curl https://<host>/healthz` returns `{"status":"ok"}`.
2. Upload: `curl -H "Authorization: Bearer $KEY" -F "file=@test.bin" https://<host>/files`. The response's `availability.usable_shards` should be `6`.
3. Issue a link with `POST /files/<id>/links {"ttl_seconds": 600}` and download it without an API key. The bytes should match the file you uploaded.
4. **Fault tolerance:** power off two storage Droplets. The download still succeeds. Within about 45 s the worker marks those nodes `unreachable` and `GET /files/<id>` reports `degraded`. Power them back on and they return to `healthy`.
5. **Audit trail:** from a trusted-source client, run `SELECT event_type, count(*) FROM audit_events GROUP BY 1;`. You should see `file_uploaded`, `link_generated` and `download_success` rows.

## 11. Operations

- **Monitoring:** on every Droplet, set DO Monitoring alerts for disk use above 80% and for CPU. Set App Platform alerts on error rate and latency. The API writes one JSON access-log line per request, with link signatures redacted; forward the logs to your log sink.
- **Backups:** Managed PostgreSQL backups are automatic. For shard Volumes, erasure coding already provides redundancy; snapshots are optional extra protection.
- **Adding capacity:** create a node as in step 5, then register it with `SFS_STORAGE_NODES=<ip>:8000 python scripts/seed_storage_nodes.py`. New uploads start placing shards on it right away. Existing shards aren't rebalanced (`docs/DESIGN.md` §12).
- **Retiring a node:** run `UPDATE storage_nodes SET status='decommissioned' WHERE address='<ip>:8000';`. The worker rebuilds its shards on other nodes, which requires a spare node that doesn't already hold a shard of each affected file.
- **Key rotation:** follow the steps in `README.md` → *Operations*.
