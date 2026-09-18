#!/usr/bin/env bash
# gen-selfsigned.sh <output-dir> [common-name]
# Creates cert.pem / key.pem for testing without a domain name.
set -euo pipefail
DIR="${1:-./certs}"
CN="${2:-localhost}"
mkdir -p "$DIR"
if [[ "$CN" =~ ^[0-9.]+$ ]]; then ALT="IP:$CN"; else ALT="DNS:$CN"; fi
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout "$DIR/key.pem" -out "$DIR/cert.pem" \
  -subj "/CN=$CN" -addext "subjectAltName=$ALT,DNS:localhost,IP:127.0.0.1" 2>/dev/null
chmod 600 "$DIR/key.pem"
echo "wrote $DIR/cert.pem and $DIR/key.pem (CN=$CN)"
echo "the client must use --insecure, or --ca $DIR/cert.pem"
