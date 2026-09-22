# DigitalOcean Deployment Guide (Console Walkthrough)

Step-by-step setup of the architecture described in `docs/DESIGN.md` using the DigitalOcean web console (cloud.digitalocean.com). Order matters — the VPC and database are created before anything that needs to join them.

## 0. Prerequisites
- A DigitalOcean account with billing enabled.
- This repo's code pushed to a Git provider (GitHub/GitLab) that App Platform can connect to.
- The container images for `app/` (API) and `storage_node/` build successfully locally (`docker build`).

## 1. Create a Project
1. Console → **Projects** → **New Project**.
2. Name it (e.g. `secure-file-system`), purpose "Web Application", environment "Production".
3. All resources below get assigned to this project as you create them, so everything stays grouped in the sidebar.

## 2. Create the Private VPC
1. Console → **Networking** → **VPC Network** → **Create VPC Network**.
2. Choose the region you'll deploy everything into (e.g. `nyc3`) — every resource must share one region to join this VPC.
3. Name it `secure-file-system-vpc`, accept the default IP range (or set one, e.g. `10.10.0.0/16`).
4. Create. This VPC is selected for every Droplet/database/App Platform resource created below so storage nodes and the DB are never exposed publicly.

## 3. Create the Managed Postgres Database
1. Console → **Databases** → **Create Database Cluster**.
2. Engine: PostgreSQL (latest stable). Plan: smallest tier is fine to start.
3. Region: same region as the VPC. VPC network: select `secure-file-system-vpc`.
4. Name it `sfs-metadata-db`. Create cluster (takes a few minutes).
5. Once ready, open the cluster → **Connection Details** → copy the private connection string (use the **VPC** connection, not the public one). Save it — it becomes the API service's `DATABASE_URL`.
6. Under **Users & Databases**, create a database named `sfs` (or use the default) and an app-specific user if you want to avoid using the admin credentials.

## 4. Create the Storage Node Droplets
Repeat this 6 times (once per shard slot), or use the "Create multiple Droplets" flow to do it in one pass.

1. Console → **Droplets** → **Create Droplet**.
2. Choose an image: Ubuntu (latest LTS) — you'll run the `storage_node` container via Docker, or deploy the app directly if you prefer a non-Docker setup.
3. Plan: Basic, size based on expected shard volume (start small, resize later).
4. **Datacenter region**: same as the VPC.
5. **VPC network**: select `secure-file-system-vpc` — do **not** enable a public IPv4 unless you need SSH access directly (prefer connecting through the DO web console's built-in Droplet console, or a bastion, to keep nodes fully private).
6. **Additional storage — Volumes**: attach a new DO Volume sized for shard storage (e.g. 50GB to start). This is where shard files are written, mounted at e.g. `/mnt/sfs-shards`.
7. Authentication: SSH key (recommended) or password.
8. Hostname: `sfs-node-1` through `sfs-node-6` so they're identifiable later.
9. Create Droplet. Repeat for nodes 2–6, incrementing the hostname.
10. On each node once it boots: install Docker, then run the `storage_node` container with the volume mounted:
    ```
    docker run -d --name storage-node \
      -v /mnt/sfs-shards:/data \
      -e SHARD_DIR=/data \
      -p 8080:8080 \
      <your-registry>/sfs-storage-node:latest
    ```
    Port 8080 is only reachable inside the VPC since no public IP was assigned.
11. Note each node's **private IP** (Droplet page → Networking tab) — these go into the API service's `STORAGE_NODES` config (Stage 4 of `docs/PROGRESS.md`).

## 5. Create a Firewall for the Storage Nodes
1. Console → **Networking** → **Firewalls** → **Create Firewall**.
2. Name: `sfs-storage-nodes-fw`.
3. Inbound rules: allow TCP `8080` **only** from the VPC's IP range (e.g. `10.10.0.0/16`) — not from `0.0.0.0/0`.
4. Allow SSH (22) only from your own IP or a bastion, if you enabled a public IP for management.
5. Apply this firewall to all 6 storage-node Droplets.

## 6. Deploy the API Service on App Platform
1. Console → **Apps** → **Create App**.
2. Source: connect your GitHub/GitLab repo, select the branch, and set the source directory to `app/` (or wherever the FastAPI service's Dockerfile lives).
3. App Platform detects the Dockerfile (or choose "Web Service" + build command/run command if not using Docker).
4. **Region**: same as the VPC. Under the resource's settings, confirm/select the VPC (`secure-file-system-vpc`) so it can reach the DB and storage nodes privately.
5. HTTP port: whatever the FastAPI app listens on (e.g. 8000). Health check path: `/healthz`.
6. **Environment Variables** (App-level, encrypted):
   - `DATABASE_URL` → the private Postgres connection string from step 3.
   - `SECRET_KEY` → a long random value (`openssl rand -hex 32`), marked as an **encrypted** secret.
   - `STORAGE_NODES` → comma-separated private IPs/ports from step 4 (e.g. `10.10.0.5:8080,10.10.0.6:8080,...`).
   - `SHARD_K` / `SHARD_M` → `4` / `2`.
7. Plan: choose instance size and instance count ≥ 2 for basic HA (App Platform load-balances across instances automatically).
8. Review and **Create Resources**. App Platform builds the image, provisions a public HTTPS endpoint (TLS included automatically), and deploys.
9. Once live, run the Alembic migration against the DB (either as a one-off App Platform "Job" component, or manually via `doctl` / a temporary console session) to create the schema from `docs/PROGRESS.md` Stage 1.

## 7. Wire Up DNS (optional)
1. Console → **Networking** → **Domains** → add your domain if not already present.
2. Create a CNAME/A record pointing at the App Platform-provided endpoint (App Platform shows the exact target under the app's **Settings → Domains** tab).

## 8. Verify End-to-End
1. `curl https://<your-app-url>/healthz` → expect `200`.
2. Upload a small test file via `POST /files`, confirm shards land on all 6 nodes (check each node's `/mnt/sfs-shards` directory or a debug endpoint).
3. Generate a signed link via `POST /files/{id}/sign`, then `GET` it — confirm the file downloads correctly.
4. Stop one storage-node Droplet (Console → Droplets → Power Off) and repeat the download — it should still succeed, demonstrating the erasure-coding tolerance. Power the node back on afterward.
5. Check `audit_events` in the database for the corresponding `link_generated` / `download_success` rows.

## 9. Ongoing Operations
- **Monitoring**: enable DO Monitoring/alerts on each Droplet (disk usage, CPU) and on the App Platform service (response time, error rate) from each resource's **Monitoring** tab.
- **Backups**: enable automated backups on the Managed Database (on by default) and consider periodic DO Volume snapshots for the storage nodes.
- **Scaling storage nodes**: adding a 7th+ node for spare capacity means updating the API's `STORAGE_NODES` env var and letting the repair job (Stage 8) rebalance onto it — no code change needed.
