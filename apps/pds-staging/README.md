# PDS staging

Temporary, non-federating PDS used to validate the Kubernetes deployment before
the production cutover.

This app intentionally does **not** have a public Cloudflare A record or a UDR7
port forward. cert-manager can still issue its wildcard certificate through the
Cloudflare DNS-01 issuer. Test it from the LAN with `--resolve` or a temporary
local DNS record.

## Start

Create the staging data directory on OMV:

```bash
sudo install -d -o 1000 -g 1000 -m 0700 /filesystem/k3s/data/pds-staging
```

Generate staging-only credentials and apply the app:

```bash
./apps/pds-staging/bootstrap-secrets.sh
kubectl apply -k apps/pds-staging
kubectl -n pds-staging rollout status deploy/pds-staging
kubectl -n pds-staging get certificate,pvc,deploy,pods
```

Get the staging admin password when needed:

```bash
kubectl -n pds-staging get secret pds-staging-env \
  -o jsonpath='{.data.PDS_ADMIN_PASSWORD}' | base64 -d; echo
```

## Verify from the LAN

Traefik is `10.9.20.14`:

```bash
curl --resolve pds-staging.mousses.xyz:443:10.9.20.14 \
  https://pds-staging.mousses.xyz/xrpc/_health

curl --resolve pds-staging.mousses.xyz:443:10.9.20.14 \
  https://pds-staging.mousses.xyz/xrpc/com.atproto.server.describeServer
```

Create a test account. `PDS_INVITE_REQUIRED=false`, so no invite code is needed.
Do **not** send the admin basic-auth credentials to `createAccount`; those are
only for `com.atproto.admin.*` and invite-code endpoints.

```bash
curl --resolve pds-staging.mousses.xyz:443:10.9.20.14 \
  -H 'Content-Type: application/json' \
  -d '{"email":"staging@mousses.xyz","handle":"test.pds-staging.mousses.xyz","password":"staging-test-password"}' \
  https://pds-staging.mousses.xyz/xrpc/com.atproto.server.createAccount
```

Verify wildcard routing:

```bash
curl --resolve test.pds-staging.mousses.xyz:443:10.9.20.14 \
  https://test.pds-staging.mousses.xyz/.well-known/atproto-did
```

For WebSocket testing, add a temporary local DNS record for
`pds-staging.mousses.xyz -> 10.9.20.14`, then use your normal `websocat`/`wscat`
command against `wss://pds-staging.mousses.xyz/xrpc/com.atproto.sync.subscribeRepos?cursor=0`.

Also restart the pod and confirm the staging account/data survives:

```bash
kubectl -n pds-staging delete pod -l app.kubernetes.io/name=pds-staging
kubectl -n pds-staging rollout status deploy/pds-staging
```

## Cleanup

The PV uses `Retain`, so deleting the namespace does not delete the staging
data. Remove the app first, then the PV/PVC/path explicitly when the test is
finished:

```bash
kubectl delete -k apps/pds-staging
kubectl delete pvc pds-staging-data -n pds-staging
kubectl delete pv pds-staging-data
sudo rm -rf /filesystem/k3s/data/pds-staging
```

Do not delete the namespace until the staging test is complete; the Secret and
account data are needed for the validation.
