#!/usr/bin/env bash
# Generate a self-signed TLS certificate for the control plane API
# (local verification / non-production deployments).
#
# Usage:
#   ./deploy/scripts/gen-tls-cert.sh [OUT_DIR] [SAN...]
#
# OUT_DIR defaults to ./tls; SAN defaults to localhost + 127.0.0.1 + ::1.
# Extra names are added as DNS: unless they look like an IP address (IP:).
# Examples:
#   ./deploy/scripts/gen-tls-cert.sh deploy/stack/tls
#   ./deploy/scripts/gen-tls-cert.sh deploy/compose/tls control-plane 10.0.0.5
#
# Then point the stack at the certs (paths are container-side):
#   E2B_TLS_CERT=/tls/tls.crt
#   E2B_TLS_KEY=/tls/tls.key
set -euo pipefail

out_dir="${1:-./tls}"
shift || true

sans=(DNS:localhost IP:127.0.0.1 IP:::1)
for name in "$@"; do
    if [[ "$name" =~ ^[0-9.]+$ || "$name" =~ ^[0-9a-fA-F:]+$ ]]; then
        sans+=("IP:$name")
    else
        sans+=("DNS:$name")
    fi
done

mkdir -p "$out_dir"
cert="$out_dir/tls.crt"
key="$out_dir/tls.key"

openssl req -x509 -newkey rsa:2048 -sha256 -nodes \
    -keyout "$key" \
    -out "$cert" \
    -days 365 \
    -subj "/CN=localhost" \
    -addext "subjectAltName=$(IFS=,; echo "${sans[*]}")" \
    -addext "extendedKeyUsage=serverAuth"

# C3 Task 5: the control plane serves this pair, and it now runs as **uid
# 65534** in every lane (the k8s Deployment's `runAsUser`, and `user:
# "65534:65534"` on the compose stacks' `control-plane`). The pair is mounted
# read-only (`./tls:/tls:ro`) and owned by whoever ran *this* script -- not by
# 65534 -- so a `0600` key (and a `0600` cert, which is what `umask 077` gives
# you) is unreadable to it and uvicorn dies at startup with
# `PermissionError: [Errno 13] ... /tls/tls.key`. Measured 2026-09-30 on the
# `deploy/stack` shape; both arms are in `docs/deploy-clusters.md` §7.12.
#
# Fixed by mode rather than by owner/group on purpose: this recipe is the
# **local-verification, self-signed** one (see the header), a non-root operator
# cannot `chown 65534` the key, and the k8s sibling delivers the same pair
# through `kubectl create secret tls`, whose in-pod default mode is `0644`.
# Production does not use this script: it terminates TLS at the ingress, or
# ships a key whose owner/group the control plane actually carries.
chmod 644 "$cert" "$key"

echo "wrote $cert and $key"
echo "SAN: $(IFS=,; echo "${sans[*]}")"
