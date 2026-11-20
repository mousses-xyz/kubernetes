# PDS

Bluesky Personal Data Server, migrated from Proxmox VM `104` to k3s on
`openmediavault`.

## Status

Production is live in Kubernetes and verified externally. Health,
`describeServer`, `listRepos`, and federation pass. The old VM is stopped and
retained for rollback. `replicas` is `1`, so re-applying the app will not scale
it back down.

The isolated staging app (`apps/pds-staging`) was removed after validation.

Current production facts:

- PDS runtime `0.4.5051` (`ghcr.io/bluesky-social/pds:0.4`)
- service DID `did:web:bluesky.mousses.xyz`
- one hosted account, `did:plc:csv4htneuv5pmbep5ivobilv`
- handle domain `.bluesky.mousses.xyz`
- public ingress via the `homelab` Cloudflare Tunnel; handle
  `petmouse.bluesky.mousses.xyz` resolves with an `_atproto` TXT record
- blob upload limit 50 MB
- PDS data lives under `/pds`: SQLite databases, `actors/`, and `blocks/`
- current `/pds` size is about **171 MB**; the 64 GiB PV is headroom for future
  blobs and repository growth

## Architecture

```text
Internet
        |
        v
Cloudflare edge (proxied CNAME bluesky.mousses.xyz)
        |
        v
cloudflared (namespace cloudflare)
        |
        v
Traefik websecure :443 (SNI bluesky.mousses.xyz)
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

Public ingress uses the existing `homelab` Cloudflare Tunnel. The tunnel
ingress targets Traefik's in-cluster Service, not the PDS Service, so the PDS
NetworkPolicy still admits only the ingress controller. The PDS route
deliberately has **no** Cloudflare Access application because federation must
stay public.

Public DNS has no `A`/`AAAA` records for `bluesky` or `*.bluesky`.
`bluesky.mousses.xyz` is a proxied CNAME to the tunnel. The handle
`petmouse.bluesky.mousses.xyz` resolves through the preferred `_atproto` DNS
TXT record (`did=did:plc:csv4htneuv5pmbep5ivobilv`), which avoids Cloudflare's
second-level wildcard certificate limit. Local split-horizon UniFi DNS still
returns `10.9.20.14` for `bluesky` and `*.bluesky`, so LAN clients bypass the
tunnel.

There is no WAN port forward for the PDS. The public IPv4 `443` no longer
reaches Traefik; the UDR7 answers there with its own UniFi OS page. The
Cloudflare Tunnel is the only public PDS path.

TLS at the origin is still cert-manager's `pds-tls`. Cloudflared verifies it
with SNI `bluesky.mousses.xyz`; Cloudflare terminates public TLS at the edge.
The wildcard certificate remains installed for local use and any future
wildcard ingress.

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
- `deploy/pds` is `1/1`

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

On the PDS VM, stop Watchtower first so it cannot update the PDS container
during the migration, then stop the PDS and Caddy:

```bash
sudo docker stop watchtower
sudo docker compose -f /pds/compose.yaml stop pds caddy
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

These are the original direct-IPv4 cutover steps, kept for rollback. Public
ingress has since moved to the Cloudflare Tunnel; see **Architecture** above.
The Bluesky port forward has been removed. The historical steps were:

- Move the Bluesky forward from `10.9.20.12` to Traefik's LAN address
  (normally `10.9.20.14`).
- Forward `443` to Traefik `443`. Forward `80` to Traefik's HTTP entrypoint
  (`8080` in the current Traefik config), or leave `80` disabled until a
  redirect route is added.
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

Confirm the request actually used the tunnel. On the LAN, normal DNS returns
Traefik's private address, so force a public Cloudflare edge address:

```bash
# Get the public edge address without the local split-horizon answer.
curl -fsS -H 'accept: application/dns-json' \
  'https://cloudflare-dns.com/dns-query?name=bluesky.mousses.xyz&type=A' |
  jq -r '.Answer[] | select(.type == 1) | .data' | head -1

curl --resolve bluesky.mousses.xyz:443:<edge-ip> \
  https://bluesky.mousses.xyz/xrpc/_health
```

Check the handle TXT record:

```bash
dig +short TXT _atproto.petmouse.bluesky.mousses.xyz
# "did=did:plc:csv4htneuv5pmbep5ivobilv"
```

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
# Handle resolution: preferred TXT method, plus PDS-side resolution
dig +short TXT _atproto.petmouse.bluesky.mousses.xyz
curl 'https://bluesky.mousses.xyz/xrpc/com.atproto.identity.resolveHandle?handle=petmouse.bluesky.mousses.xyz'

# WebSocket firehose (use websocat/wscat)
websocat 'wss://bluesky.mousses.xyz/xrpc/com.atproto.sync.subscribeRepos?cursor=0'
```

## Rollback

1. `kubectl -n pds scale deploy/pds --replicas=0`
2. Revert the Cloudflare tunnel/DNS change and apply it, restoring the public
   `bluesky` and `*.bluesky` `A` records and removing the tunnel CNAME.
3. Recreate the UDR7 Bluesky forward (`WAN TCP/UDP 443 -> 10.9.20.12:443`) and
   point local DNS back to `10.9.20.12`.
4. Start the VM PDS.
5. If the Kubernetes PDS accepted new writes, copy its `/pds` back to the VM
   before starting the old PDS.

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
- **Cloudflare now proxies public PDS traffic.** If federation stalls, check
  Bot Fight Mode, WAF challenges, and security-level rules for
  `bluesky.mousses.xyz`. The relay and AppView crawlers must not be challenged.
- **No WAN port forward remains.** The public IPv4 `443` is the UDR7's own
  UniFi OS page, not the PDS. Test the PDS through `bluesky.mousses.xyz` or the
  tunnel.
- **The public wildcard was removed.** Handle resolution is TXT-only for
  `petmouse.bluesky.mousses.xyz`. Creating another account under
  `*.bluesky.mousses.xyz` requires its own `_atproto.<handle>` TXT record, or a
  manual Advanced Certificate plus a wildcard tunnel route.
- **Cloudflare's proxy has a 100-second HTTP timeout.** Long-lived
  `subscribeRepos` streams reconnect; this is expected and does not affect the
  repository cursor.

## Backups

The PDS backup is implemented in `apps/pds/backups/` and
`scripts/backups/{prepare,backup,restore,daily,recovery,install-pds-schedule}-pds.py`.
It follows the namespace backup pattern: an in-cluster HTTPS Restic REST
server, a local VolSync staging PVC, verified snapshots with `7d` retention, and
an age-encrypted recovery export kept off OMV.

### Consistency model

A live copy of SQLite is not a consistent backup. Each run:

1. captures the PDS service identity, health, and repository list;
2. stores a quiescence journal and scales `deployment/pds` to `0`;
3. archives `/filesystem/k3s/data/pds` (SQLite databases, `actors/`, `blocks/`),
   recording per-file SHA-256, numeric ownership, and SQLite
   `PRAGMA integrity_check` results;
4. restarts the PDS, waits for readiness, and verifies the writer resumed;
5. publishes and uploads the immutable artifact set through VolSync, then
   verifies the snapshot and every checksum from a separate Job.

Artifacts are `data.tar`, `contents.json`, `application.json`,
`application-config.json`, `application-secret.json`, `sqlite-integrity.json`,
`metadata.json`, `backup-id`, and `SHA256SUMS`. The daily run is at
**06:00 Arizona / 13:00 UTC**, after the other namespace backups. Downtime is
one PDS writer stop/start per run.

### Storage layout

- Restic repository: `/filesystem/k3s/data/pds-backups/backups`
- VolSync staging: `/filesystem/k3s/data/pds-backups/backup-staging/pds-data`
- Isolated restore scratch:
  `/filesystem/k3s/data/pds-backups/restore-validation/pds-data/initial`
- Private recovery evidence: `/root/k3s-backup-recovery/pds`

The live path is `/filesystem/k3s/data/pds`; backup paths are siblings, so a
Restic repository never nests inside the live data directory.

### One-time setup

Run on OMV from a verified checkout of this repository at a full commit hash:

```bash
sudo python3 scripts/backups/prepare-pds.py
```

This creates the array directories, Restic TLS/auth credentials,
`restic-pds-data`, the `restic-repositories` and `data-backup-staging` PVCs,
applies the backup kustomization, and verifies DNS, TLS, and authentication with
a Job. Credentials are written only under `/root/k3s-backup-recovery/pds`.

### Initial backup and restore test

The initial backup performs a full quiesced capture and upload. Its verification
Job checks the snapshot, every artifact checksum, and exercises `--keep-within 7d`
pruning in a scratch repository:

```bash
sudo python3 scripts/backups/backup-pds.py
```

Do **not** decommission VM `104` until the restore test passes. The restore test
restores the initial snapshot into isolated scratch PVCs, never the live claim.
It validates SQLite integrity, blob checksums, and captured ownership, then
starts an isolated no-network PDS and compares health, service DID, and the
repository list:

```bash
sudo python3 scripts/backups/restore-pds.py
```

The validation Job runs as root only because `data.tar` preserves UID/GID
`1000:1000`; it has no network access and writes only to isolated scratch paths.
The smoke Deployment runs as `1000:1000`, forces `PDS_CRAWLERS` empty, requests
less CPU/memory than production so it can schedule beside the live PDS, and is
scaled back to zero when the test finishes. The `pds` namespace Pod Security
labels are audit/warn only, so the root Job is admitted; do not add enforce
labels without revisiting this design.

### Daily schedule

After the initial backup and restore test pass:

```bash
sudo python3 scripts/backups/install-pds-schedule.py --revision <full-40-char-commit>
```

The installer verifies the saved evidence, installs an immutable runtime under
`/usr/local/lib/k3s-backups/pds/<commit>`, installs
`k3s-pds-backup.timer` and `k3s-pds-backup-recovery.service`, runs the first
daily sequence and retention proof, and only then enables the timer. The
recovery unit resumes a PDS writer left scaled down by an interrupted run.

```bash
systemctl list-timers k3s-pds-backup.timer --all
journalctl -u k3s-pds-backup.service -n 100 --no-pager
```

### Off-OMV recovery copy

Export the latest verified generation through age and keep the encrypted file
independent of OMV, matching the other namespace recovery exports. Keep the age
private key off OMV and off the cluster:

```bash
umask 077
sudo sh -c 'python3 scripts/backups/recovery-pds.py export --recipient age1... \
  | age --encrypt --recipient age1... --output /root/k3s-backup-recovery/pds/recovery.tar.age'
sha256sum /root/k3s-backup-recovery/pds/recovery.tar.age
```

Copy the encrypted file and its SHA-256 to the recovery host. Verify by
streaming the decrypted archive; the verifier writes no decrypted files:

```bash
python3 scripts/backups/recovery-pds.py verify \
  --archive recovery.tar.age \
  --identity ~/.config/age/pds-recovery.key \
  --expected-sha256 <sha256-from-OMV> \
  --receipt recovery-receipt.json
```

The export contains the full latest verified artifact set, including
`data.tar`, plus the repository credentials, CA, initial upload/restore/smoke
evidence, daily receipts, schedule proof, and retention proof.

### Production restore outline

`restore-pds.py` validates into scratch storage; it does not overwrite the live
claim. For a real recovery, keep VM `104` and the existing `/pds` untouched,
scale `deployment/pds` to `0`, restore the newest verified snapshot with the
same Restic credentials and CA, verify `SHA256SUMS` and SQLite integrity, then
stage the restored data at a new local path owned by `1000:1000` with directories
`0700` and files `0600`. Point the `pds-data` PV/PVC at the staged data, scale the
Deployment to `1`, and verify health, service DID, and the repository listing
before deleting any prior copy.

Keep the backup repository off the OMV host as well as on it.
