# PDS

Bluesky Personal Data Server, migrated from Proxmox VM `104` to k3s on
`openmediavault`.

## Status

Phase 1: manifests only. The production Deployment is intentionally created
with `replicas: 0` so an empty PDS cannot start before the VM data has been
copied into the PVC.

Current production facts:

- PDS runtime `0.4.5051` (`ghcr.io/bluesky-social/pds:0.4`)
- service DID `did:web:bluesky.mousses.xyz`
- one hosted account, `did:plc:csv4htneuv5pmbep5ivobilv`
- handle domain `.bluesky.mousses.xyz`
- blob upload limit 50 MB
- PDS data lives under `/pds`: SQLite databases, `actors/`, and `blocks/`
- current `/pds` size is about **171 MB**; the 64 GiB PV is headroom for future
  blobs and repository growth

## Architecture

```text
Cox IPv4 / optional Cloudflare Tunnel
        |
        v
UDR7 80/443 -> Traefik on openmediavault
        |
        v
Service pds:3000
        |
        v
Deployment pds (1 replica, openmediavault)
        |
        v
PVC pds-data -> /filesystem/k3s/data/pds
```

TLS is issued by cert-manager through the existing
`letsencrypt-production-cloudflare` ClusterIssuer. The certificate covers both
`bluesky.mousses.xyz` and `*.bluesky.mousses.xyz`.

Traefik routes the root hostname and all first-level subdomains to the PDS.
There is deliberately no Authentik middleware: PDS federation must remain
publicly reachable.

## Prerequisites

- The PV directory must exist on `openmediavault` and be writable by UID/GID
  `1000`.
- `/pds/pds.env` must be available as a protected file on the machine running
  `kubectl`. Never commit it.
- The storage, certificate, and service manifests assume the single k3s node is
  named `openmediavault`.

Create the PV directory on OMV:

```bash
sudo install -d -o 1000 -g 1000 -m 0700 /filesystem/k3s/data/pds
```

## Source VM layout

On the Proxmox VM (`pds@10.9.20.12`):

- data and config: `/pds`
- env file: `/pds/pds.env`
- compose file: `/pds/compose.yaml`
- containers: `pds`, `caddy`, `watchtower`
- there is no `pds.service` systemd unit; the stack is Docker Compose

Do **not** import `/pds/pds.env` directly. The raw file contains a Compose
interpolation such as `PDS_EMAIL_FROM_ADDRESS=$GMAIL_USERNAME@gmail.com`; the
running container has the resolved value. Capture the effective environment
from the container instead:

```bash
umask 077
ssh pds@10.9.20.12 \
  "docker exec pds env | grep -E '^(PDS_|LOG_ENABLED=|TZ=)'" \
  > ~/pds.env
chmod 600 ~/pds.env
```

This writes the secrets only to the machine that has `kubectl`. Do not paste the
file contents into chat or commit it.

## Install the paused production app

Create the Secret from the effective env file:

```bash
PDS_ENV_FILE=~/pds.env ./apps/pds/bootstrap-secrets.sh
rm -f ~/pds.env
```

The script imports the effective env as `pds/pds-env`. It refuses to overwrite
an existing Secret, validates the PDS hostname, data paths, PLC key format, and
rejects unresolved `$VAR` values, then forces `PDS_PORT=3000`.

Then apply the app:

```bash
kubectl apply -k apps/pds
kubectl -n pds get pvc pds-data
kubectl -n pds get certificate pds pds-staging
kubectl -n pds get deploy pds
```

Expected:

- `pds-data` is `Bound`
- both Certificates become `Ready=True`
- `deploy/pds` is `0/0`

The staging certificate is unused by the IngressRoute; it validates wildcard
DNS-01 issuance before the production certificate is relied on.

## Production data cutover

Use a maintenance window. Keep VM `104` untouched for rollback.

### 1. Pre-copy while the VM PDS is still running

From a host with SSH access to the PDS VM and OMV, seed the PVC:

```bash
rsync -aHAX --numeric-ids --delete \
  --exclude caddy \
  --exclude pds.env \
  --exclude temp \
  pds@10.9.20.12:/pds/ /filesystem/k3s/data/pds/
```

The first copy is not the consistent copy. It only reduces the final delta.

### 2. Stop the old PDS

On the PDS VM:

```bash
sudo systemctl stop pds
# or: sudo docker compose stop
```

Confirm nothing is listening on 3000 or 443.

### 3. Final consistent copy

Run the same rsync again after the PDS is stopped. Then fix ownership:

```bash
sudo chown -R 1000:1000 /filesystem/k3s/data/pds
sudo find /filesystem/k3s/data/pds -type d -exec chmod 0700 {} \;
sudo find /filesystem/k3s/data/pds -type f -exec chmod 0600 {} \;
```

Do not delete the VM data.

### 4. Start the Kubernetes PDS

```bash
kubectl -n pds scale deploy/pds --replicas=1
kubectl -n pds rollout status deploy/pds
kubectl -n pds logs -f deploy/pds
```

### 5. Switch ingress

- Change the unmanaged **Bluesky** port forward on the UDR7 from
  `10.9.20.12` to Traefik's LAN address (normally `10.9.20.14`).
- Forward 443 to Traefik `443`. Forward 80 to Traefik's HTTP entrypoint
  (`8080` in the current Traefik config), or leave 80 disabled until a redirect
  route is added.
- Keep the Cloudflare `A` records unchanged while the public IPv4 is unchanged.
- Remove the public `AAAA` records for `bluesky` and `*.bluesky` unless Traefik
  has a verified public IPv6 path.
- Update the local UniFi DNS A records for `bluesky` and `*.bluesky` to the
  Traefik LAN address. Remove or update their AAAA records at the same time.

## Verification

From an external network:

```bash
curl https://bluesky.mousses.xyz/xrpc/_health
curl https://bluesky.mousses.xyz/xrpc/com.atproto.server.describeServer
curl https://bluesky.mousses.xyz/xrpc/com.atproto.sync.listRepos?limit=100
```

Expected: runtime `0.4.5051`, the same service DID, the same one repo, the same
handle domain, and the same 50 MB blob limit.

Check the relay:

```bash
kubectl -n pds exec deploy/pds -- \
  goat relay host status bluesky.mousses.xyz --json
```

Then log into Bluesky, create a post, and verify federation. If the relay did
not observe the post, request a crawl:

```bash
curl -X POST \
  -H 'Content-Type: application/json' \
  -d '{"hostname":"bluesky.mousses.xyz"}' \
  https://bsky.network/xrpc/com.atproto.sync.requestCrawl
```

If the relay is desynced, the PDS README documents a `sequencer.sqlite`
`repo_seq` cursor fix. Back up `sequencer.sqlite` first and run the update from
a debug pod with `sqlite3` and the PVC mounted.

Also verify:

```bash
# Wildcard handle routing
curl https://<handle>.bluesky.mousses.xyz/.well-known/atproto-did

# WebSocket firehose (use websocat/wscat)
websocat 'wss://bluesky.mousses.xyz/xrpc/com.atproto.sync.subscribeRepos?cursor=0'
```

## Rollback

1. `kubectl -n pds scale deploy/pds --replicas=0`
2. Point the UDR7 forward and local DNS back to `10.9.20.12`.
3. Start the VM PDS.
4. If the Kubernetes PDS accepted new writes, copy its `/pds` back to the VM
   before starting the old PDS.

## Phase 2 staging

Create a temporary staging app with the same manifests but independent identity
and storage. Do not reuse production secrets or the production PVC.

Suggested staging values:

```text
PDS_HOSTNAME=pds-staging.mousses.xyz
PDS_SERVICE_HANDLE_DOMAINS=.pds-staging.mousses.xyz
PDS_CRAWLERS=
PDS_INVITE_REQUIRED=false
PDS_DATA_DIRECTORY=/pds
PDS_BLOBSTORE_DISK_LOCATION=/pds/blocks
```

Generate new `PDS_JWT_SECRET`, `PDS_ADMIN_PASSWORD`, and PLC key for staging.
Use a separate PV at `/filesystem/k3s/data/pds-staging`, a 5 GiB PVC, a
`pds-staging` namespace, and certificates for
`pds-staging.mousses.xyz` plus `*.pds-staging.mousses.xyz`. The staging route
must use the same Traefik/cert-manager path as production.

Validate health, wildcard TLS, invite/account creation, a post, a pod restart,
and a WebSocket firehose connection before touching production.

## Known gotchas

- **`enableServiceLinks: false` is mandatory.** The Service is named `pds`;
  without this, Kubernetes injects `PDS_PORT=tcp://...` and the PDS binds to
  the wrong port.
- **SQLite stays on local disk.** Do not move the PVC to NFS or a replicated
  network filesystem.
- **Traefik's `websecure` read timeout is 60 seconds.** Verify the PDS
  `subscribeRepos` WebSocket stays connected; if it does not, add a dedicated
  entrypoint or adjust the timeout.
- **The PLC rotation key is identity-critical.** Back it up offline separately
  from the cluster and do not rotate it during the migration.
- **Do not change `PDS_HOSTNAME`.** The service DID is `did:web`; changing the
  hostname changes the service identity.
- **SMTP egress** is allowed on ports 25/465/587. If the current SMTP endpoint
  is on the LAN, add a specific LAN egress rule; the current policy only allows
  public destinations.

## Backups

Not implemented in Phase 1. The PDS data is SQLite plus content-addressed
blobs, so a raw live copy is not a consistent backup. Phase 6 should add either:

- a quiesced PVC snapshot (scale to 0, VolSync/Restic, scale back to 1), or
- online `sqlite3 .backup` for `account.sqlite`, `sequencer.sqlite`,
  `did_cache.sqlite`, and `actors/` plus a blob sync.

Keep the backup repository off the OMV host as well as on it.
