#!/usr/bin/env bash
# add-peer.sh <name> [server-host]
# Adds a WireGuard peer and prints the client config plus the command that
# starts the local WSS bridge on the client device.
set -euo pipefail

NAME="${1:?usage: add-peer.sh <name> [server-host]}"
HOST="${2:-}"
CONF=/etc/wireguard/wg0.conf
ENVFILE=/etc/wg-wss.env
OUT_DIR="${OUT_DIR:-/etc/wireguard/clients}"
DNS="${DNS:-1.1.1.1, 9.9.9.9}"
# 1280 leaves room for IP + TCP + TLS + WebSocket headers inside a 1500 MTU.
MTU="${MTU:-1280}"
LOCAL_UDP="${LOCAL_UDP:-127.0.0.1:51820}"

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -f $CONF ]] || { echo "$CONF not found - run install-server.sh first" >&2; exit 1; }
# shellcheck disable=SC1090
[[ -f $ENVFILE ]] && source "$ENVFILE"

umask 077
mkdir -p "$OUT_DIR"
KEY="$(wg genkey)"
PUB="$(wg pubkey <<<"$KEY")"
PSK="$(wg genpsk)"

# Pick the next free host number, in both families the interface carries.
ADDR_LINE="$(grep -m1 '^Address' "$CONF")"
BASE="$(grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' <<<"$ADDR_LINE" | head -1 | cut -d. -f1-3)"
[[ -n "$BASE" ]] || { echo "no IPv4 address found in $CONF" >&2; exit 1; }
USED="$(grep -oE "$BASE\.[0-9]+" "$CONF" | cut -d. -f4 | sort -n | tail -1)"
NEXT=$(( ${USED:-1} + 1 ))
[[ $NEXT -lt 255 ]] || { echo "subnet is full" >&2; exit 1; }
ADDR="$BASE.$NEXT"

# The IPv6 half is optional: only if the server interface has a prefix. Any
# notation works - ipaddress does the arithmetic, not a regular expression.
IFACE6="$(tr ',' '\n' <<<"$ADDR_LINE" | grep -oE '[0-9a-fA-F:]+:[0-9a-fA-F:]*/[0-9]+' | head -1)"
if [[ -n "$IFACE6" ]]; then
  ADDR6="$(python3 - "$IFACE6" "$NEXT" <<'PY'
import ipaddress, sys
network = ipaddress.ip_interface(sys.argv[1]).network
print(network.network_address + int(sys.argv[2]))
PY
)"
  CLIENT_ADDRS="$ADDR/24, $ADDR6/64"
  PEER_IPS="$ADDR/32, $ADDR6/128"
  ALLOWED="0.0.0.0/0, ::/0"
else
  CLIENT_ADDRS="$ADDR/24"
  PEER_IPS="$ADDR/32"
  ALLOWED="0.0.0.0/0"
fi

cat >> "$CONF" <<EOF

# $NAME  (added $(date -Is))
[Peer]
PublicKey = $PUB
PresharedKey = $PSK
AllowedIPs = $PEER_IPS
EOF

wg addconf wg0 <(printf '[Peer]\nPublicKey = %s\nPresharedKey = %s\nAllowedIPs = %s\n' \
  "$PUB" "$PSK" "$PEER_IPS") 2>/dev/null || systemctl restart wg-quick@wg0

CLIENT="$OUT_DIR/$NAME.conf"
cat > "$CLIENT" <<EOF
[Interface]
PrivateKey = $KEY
Address = $CLIENT_ADDRS
DNS = $DNS
MTU = $MTU

[Peer]
PublicKey = $(cat /etc/wireguard/server.pub)
PresharedKey = $PSK
# Not the real server: this is the local WSS bridge on this device.
Endpoint = $LOCAL_UDP
AllowedIPs = $ALLOWED
PersistentKeepalive = 25
EOF

echo "=== $CLIENT ==="
cat "$CLIENT"
command -v qrencode >/dev/null && { echo; qrencode -t ansiutf8 < "$CLIENT"; }

cat <<EOF

On the client device, start the bridge first, then bring WireGuard up:

  python3 -m wgws client wss://${HOST:-YOUR_SERVER}:${WGWS_LISTEN##*:}${WGWS_PATH:-/ws} \\
      --token ${WGWS_TOKEN:-<token>} --listen $LOCAL_UDP
  sudo wg-quick up $NAME

Note: exclude the server's IP from the tunnel, or add a host route to it via
your normal gateway, so the WebSocket itself does not go through the VPN.
EOF
