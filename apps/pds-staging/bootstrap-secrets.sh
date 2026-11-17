#!/usr/bin/env bash
set -euo pipefail
umask 0077

readonly namespace=pds-staging
readonly secret_name=pds-staging-env

for command in kubectl openssl od; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    printf 'required command not found: %s\n' "${command}" >&2
    exit 1
  fi
done

kubectl apply -f "$(dirname -- "${BASH_SOURCE[0]}")/namespace.yaml" >/dev/null

if kubectl -n "${namespace}" get secret "${secret_name}" >/dev/null 2>&1; then
  printf '%s/%s already exists; refusing to rotate staging credentials\n' \
    "${namespace}" "${secret_name}" >&2
  exit 1
fi

jwt_secret="$(openssl rand -hex 16)"
admin_password="$(openssl rand -hex 16)"
plc_key="$(
  openssl ecparam --name secp256k1 --genkey --noout --outform DER \
    | tail --bytes=+8 \
    | head --bytes=32 \
    | od -An -tx1 -v \
    | tr -d ' \n'
)"

if [[ ! "${plc_key}" =~ ^[0-9a-fA-F]{64}$ ]]; then
  printf 'generated PLC key is not 64 hexadecimal characters\n' >&2
  exit 1
fi

env_file="$(mktemp)"
cleanup() {
  rm -f -- "${env_file}"
}
trap cleanup EXIT

cat >"${env_file}" <<EOF
PDS_HOSTNAME=pds-staging.mousses.xyz
PDS_PORT=3000
PDS_DATA_DIRECTORY=/pds
PDS_BLOBSTORE_DISK_LOCATION=/pds/blocks
PDS_BLOB_UPLOAD_LIMIT=52428800
PDS_DID_PLC_URL=https://plc.directory
PDS_BSKY_APP_VIEW_URL=https://api.bsky.app
PDS_BSKY_APP_VIEW_DID=did:web:api.bsky.app
PDS_REPORT_SERVICE_URL=https://mod.bsky.app
PDS_REPORT_SERVICE_DID=did:plc:ar7c4by46qjdydhdevvrndac
PDS_CRAWLERS=
PDS_SERVICE_HANDLE_DOMAINS=.pds-staging.mousses.xyz
PDS_INVITE_REQUIRED=false
PDS_RATE_LIMITS_ENABLED=false
# PDS requires both emailFromAddress and emailSmtpUrl, or neither.
# Staging intentionally configures neither.
LOG_ENABLED=true
PDS_JWT_SECRET=${jwt_secret}
PDS_ADMIN_PASSWORD=${admin_password}
PDS_PLC_ROTATION_KEY_K256_PRIVATE_KEY_HEX=${plc_key}
EOF

kubectl -n "${namespace}" create secret generic "${secret_name}" \
  --from-env-file="${env_file}" >/dev/null

printf 'created staging secret %s/%s\n' "${namespace}" "${secret_name}"
printf 'read the staging admin password with:\n'
printf "  kubectl -n %s get secret %s -o jsonpath='{.data.PDS_ADMIN_PASSWORD}' | base64 -d; echo\n" \
  "${namespace}" "${secret_name}"
