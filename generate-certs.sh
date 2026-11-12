#!/usr/bin/env bash
#
# Generate a self-signed CA and per-node TLS certificates for fibre_exporter.
#
# Usage:
#   ./generate-certs.sh <node-address> [node-address ...]
#
# Each address is an IP or a host name, the one Prometheus scrapes the node
# at. The Docker stack reaches an exporter on its own machine as
# host.docker.internal.
#
# Example:
#   ./generate-certs.sh 192.168.1.10 192.168.1.11 host.docker.internal
#
# Output structure:
#   certs/
#     ca.crt              <- give this to Prometheus (tls_config.ca_file)
#     ca.key              <- keep safe, only needed to sign new node certs
#     <address>/
#       node.crt          <- copy to that node
#       node.key          <- copy to that node
#
set -euo pipefail

# ── defaults ────────────────────────────────────────────────────────────────
CA_DAYS=3650          # 10 years
NODE_DAYS=3650        # 10 years
KEY_BITS=2048
OUT_DIR="certs"
CA_SUBJECT="/CN=FIBRE Exporter CA"
# ────────────────────────────────────────────────────────────────────────────

if [ $# -eq 0 ]; then
    echo "Usage: $0 <node-address> [node-address ...]"
    echo ""
    echo "Each address is an IP or a host name, the one Prometheus scrapes the node at."
    echo ""
    echo "Example:"
    echo "  $0 192.168.1.10 192.168.1.11 host.docker.internal"
    exit 1
fi

command -v openssl >/dev/null 2>&1 || { echo "Error: openssl is required but not installed"; exit 1; }

mkdir -p "$OUT_DIR"

# ── generate CA (only if it doesn't already exist) ──────────────────────────
if [ -f "$OUT_DIR/ca.crt" ] && [ -f "$OUT_DIR/ca.key" ]; then
    echo "Using existing CA: $OUT_DIR/ca.crt"
else
    echo "Generating CA certificate..."
    openssl genrsa -out "$OUT_DIR/ca.key" "$KEY_BITS" 2>/dev/null
    openssl req -new -x509 \
        -key "$OUT_DIR/ca.key" \
        -out "$OUT_DIR/ca.crt" \
        -days "$CA_DAYS" \
        -subj "$CA_SUBJECT" \
        -nodes 2>/dev/null
    chmod 600 "$OUT_DIR/ca.key"
    echo "  Created: $OUT_DIR/ca.crt (give this to Prometheus)"
    echo "  Created: $OUT_DIR/ca.key (keep safe)"
fi

# ── generate per-node certs ─────────────────────────────────────────────────
for NODE in "$@"; do
    NODE_DIR="$OUT_DIR/$NODE"
    mkdir -p "$NODE_DIR"

    if [ -f "$NODE_DIR/node.crt" ] && [ -f "$NODE_DIR/node.key" ]; then
        echo "Skipping $NODE (cert already exists)"
        continue
    fi

    echo "Generating cert for $NODE..."

    # The certificate must hold the address Prometheus connects to: an IP
    # (IPv4, or IPv6 with colons) as an IP SAN, anything else as a DNS SAN.
    if [[ "$NODE" =~ ^[0-9]+(\.[0-9]+){3}$ || "$NODE" == *:* ]]; then
        NODE_SAN="IP:$NODE"
    else
        NODE_SAN="DNS:$NODE"
    fi
    EXT_FILE=$(mktemp)
    cat > "$EXT_FILE" <<EXTEOF
[v3_ext]
subjectAltName = $NODE_SAN,IP:127.0.0.1
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
EXTEOF

    # Generate key + CSR
    openssl genrsa -out "$NODE_DIR/node.key" "$KEY_BITS" 2>/dev/null
    openssl req -new \
        -key "$NODE_DIR/node.key" \
        -out "$NODE_DIR/node.csr" \
        -subj "/CN=$NODE" \
        -nodes 2>/dev/null

    # Sign with CA
    openssl x509 -req \
        -in "$NODE_DIR/node.csr" \
        -CA "$OUT_DIR/ca.crt" \
        -CAkey "$OUT_DIR/ca.key" \
        -CAcreateserial \
        -out "$NODE_DIR/node.crt" \
        -days "$NODE_DAYS" \
        -extfile "$EXT_FILE" \
        -extensions v3_ext 2>/dev/null

    # Cleanup
    rm -f "$NODE_DIR/node.csr" "$EXT_FILE"
    chmod 600 "$NODE_DIR/node.key"

    echo "  Created: $NODE_DIR/node.crt"
    echo "  Created: $NODE_DIR/node.key"
done

echo ""
echo "Done. Next steps:"
echo ""
echo "  1. Copy certs to each node:"
echo "     scp $OUT_DIR/<address>/node.{crt,key} <user>@<node>:/etc/fibre-exporter/"
echo ""
echo "  2. Start the exporter with TLS and a password:"
echo "     fibre_exporter.py --bitcoind /path/to/bitcoind \\"
echo "       --tls-cert /etc/fibre-exporter/node.crt \\"
echo "       --tls-key  /etc/fibre-exporter/node.key \\"
echo "       --metrics-auth-username prometheus \\"
echo "       --metrics-auth-password <password>"
echo ""
echo "  3. Give the CA and the password to the Docker stack's Prometheus:"
echo "     cp $OUT_DIR/ca.crt docker/secrets/exporter-ca.crt"
echo "     (see docker/secrets/README.md)"
echo ""
echo "  4. List each node at the address its certificate holds,"
echo "     in docker/prometheus/targets/nodes.yml:"
for NODE in "$@"; do
    if [[ "$NODE" == *:* ]]; then TARGET="[$NODE]:9435"; else TARGET="$NODE:9435"; fi
    echo "     - targets: ['$TARGET']"
    echo "       labels:"
    echo "         node: '<name>'"
done
echo ""
echo "     The Nix collector takes the CA as services.fibre-collector.tls.caFile."
