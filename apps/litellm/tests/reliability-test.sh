#!/usr/bin/env bash
set -euo pipefail

readonly app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly runtime="${CONTAINER_RUNTIME:-docker}"
readonly image='ghcr.io/berriai/litellm@sha256:c560d317136880ea92d62334d4367ab10955b1af189dd3ea24f6551290b1d6c1'

# All inference runs against loopback fixtures in the pinned deployment image.
# No credentials, cluster, host networking, or external providers are used.
exec "${runtime}" run --rm --network none \
  --env LITELLM_LOCAL_MODEL_COST_MAP=True \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --volume "${app_root}:/contract:ro" --workdir /contract \
  --entrypoint python "${image}" \
  -m unittest discover -s tests -p test_reliability.py -q
