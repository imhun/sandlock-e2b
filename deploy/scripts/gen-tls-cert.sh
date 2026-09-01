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
chmod 600 "$key"

echo "wrote $cert and $key"
echo "SAN: $(IFS=,; echo "${sans[*]}")"
