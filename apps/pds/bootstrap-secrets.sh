#!/usr/bin/env bash
set -euo pipefail
umask 0077

readonly namespace=pds
readonly secret_name=pds-env
readonly expected_hostname=bluesky.mousses.xyz
env_file="${PDS_ENV_FILE:-/pds/pds.env}"

if ! command -v kubectl >/dev/null 2>&1; then
  printf 'required command not found: kubectl\n' >&2
  exit 1
fi

if [[ ! -r "${env_file}" ]]; then
  printf 'PDS env file not readable: %s\n' "${env_file}" >&2
  printf 'Set PDS_ENV_FILE to a protected copy of the PDS /pds/pds.env file.\n' >&2
  exit 1
fi

kubectl apply -f "$(dirname -- "${BASH_SOURCE[0]}")/namespace.yaml" >/dev/null

if kubectl -n "${namespace}" get secret "${secret_name}" >/dev/null 2>&1; then
  printf '%s/%s already exists; refusing to overwrite production credentials\n' \
    "${namespace}" "${secret_name}" >&2
  exit 1
fi

normalized="$(mktemp)"
secret_file="$(mktemp)"
cleanup() {
  rm -f -- "${normalized}" "${secret_file}"
}
trap cleanup EXIT

# Preserve the existing PDS configuration exactly, including non-secret values.
# Keeping the whole env in one Secret avoids importing a partial configuration
# during the VM -> Kubernetes migration.
sed -E 's/^[[:space:]]*(export[[:space:]]+)?//' "${env_file}" \
  | grep -E '^[A-Z0-9_]+=' >"${normalized}" || true

for key in \
  PDS_HOSTNAME \
  PDS_JWT_SECRET \
  PDS_ADMIN_PASSWORD \
  PDS_PLC_ROTATION_KEY_K256_PRIVATE_KEY_HEX \
  PDS_DATA_DIRECTORY \
  PDS_BLOBSTORE_DISK_LOCATION; do
  if ! grep -q "^${key}=" "${normalized}"; then
    printf 'missing required key in %s: %s\n' "${env_file}" "${key}" >&2
    exit 1
  fi
done

get_value() {
  awk -v key="$1" -F= '$1 == key { sub(/^[^=]*=/, ""); print; exit }' "${normalized}"
}

hostname="$(get_value PDS_HOSTNAME)"
if [[ "${hostname}" != "${expected_hostname}" ]]; then
  printf 'PDS_HOSTNAME is %q, expected %q; refusing to change the PDS identity\n' \
    "${hostname}" "${expected_hostname}" >&2
  exit 1
fi

data_dir="$(get_value PDS_DATA_DIRECTORY)"
if [[ "${data_dir}" != "/pds" ]]; then
  printf 'PDS_DATA_DIRECTORY is %q, expected /pds\n' "${data_dir}" >&2
  exit 1
fi

blob_dir="$(get_value PDS_BLOBSTORE_DISK_LOCATION)"
if [[ "${blob_dir}" != "/pds/blocks" ]]; then
  printf 'PDS_BLOBSTORE_DISK_LOCATION is %q, expected /pds/blocks\n' "${blob_dir}" >&2
  exit 1
fi

plc_key="$(get_value PDS_PLC_ROTATION_KEY_K256_PRIVATE_KEY_HEX)"
if [[ ! "${plc_key}" =~ ^[0-9a-fA-F]{64}$ ]]; then
  printf 'PDS_PLC_ROTATION_KEY_K256_PRIVATE_KEY_HEX must be 64 hexadecimal characters\n' >&2
  exit 1
fi

for key in PDS_JWT_SECRET PDS_ADMIN_PASSWORD; do
  value="$(get_value "${key}")"
  if [[ -z "${value}" ]]; then
    printf '%s must not be empty\n' "${key}" >&2
    exit 1
  fi
done

# The Deployment's Service is also named pds. Force the application port so a
# future service-link change cannot make the PDS bind to an unexpected port.
grep -v '^PDS_PORT=' "${normalized}" >"${secret_file}" || true
printf 'PDS_PORT=3000\n' >>"${secret_file}"

kubectl -n "${namespace}" create secret generic "${secret_name}" \
  --from-env-file="${secret_file}" >/dev/null

key_count="$(awk 'END { print NR }' "${secret_file}")"
printf 'created secret %s/%s with %s keys; values were not displayed\n' \
  "${namespace}" "${secret_name}" "${key_count}"
