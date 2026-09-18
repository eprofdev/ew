#!/usr/bin/env bash
# Set up WireGuard + the WSS tunnel on a fresh Debian/Ubuntu VPS.
#
#   sudo ./scripts/install-server.sh vpn.example.com
#
# With a domain name it asks certbot for a real certificate (port 80 must be
# free during issuance). Without one it falls back to a self-signed
# certificate, and the client then needs --insecure or --ca.
set -euo pipefail

DOMAIN="${1:-}"
WG_PORT="${WG_PORT:-51820}"
WG_NET="${WG_NET:-10.8.0.1/24}"
# Unique-local IPv6 prefix, randomly generated per install as RFC 4193 asks.
# Set WG_NET6="" to build an IPv4-only tunnel.
if [[ -z "${WG_NET6+x}" ]]; then
  rnd="$(openssl rand -hex 5)"
  WG_NET6="fd${rnd:0:2}:${rnd:2:4}:${rnd:6:4}::1/64"
fi
WSS_PORT="${WSS_PORT:-443}"
WS_PATH="${WS_PATH:-/ws}"
PREFIX="${PREFIX:-/opt/wg-wss}"
CONF=/etc/wireguard/wg0.conf
ENVFILE=/etc/wg-wss.env
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

echo "==> installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq wireguard-tools iptables python3 qrencode curl openssl
[[ -n "$DOMAIN" ]] && apt-get install -y -qq certbot || true

echo "==> installing wgws to $PREFIX"
install -d "$PREFIX"
cp -r "$SRC_DIR/wgws" "$PREFIX/"

echo "==> generating WireGuard keys"
umask 077
install -d /etc/wireguard
[[ -f /etc/wireguard/server.key ]] || wg genkey > /etc/wireguard/server.key
wg pubkey < /etc/wireguard/server.key > /etc/wireguard/server.pub

WAN_IF="$(ip route show default | awk '/default/ {print $5; exit}')"
echo "==> outbound interface: $WAN_IF"

# Not every kernel or container has ip6tables NAT. Check before committing to
# IPv6, so a missing feature downgrades the tunnel instead of failing install.
if [[ -n "$WG_NET6" ]] && ! ip6tables -t nat -L -n >/dev/null 2>&1; then
  echo "    ip6tables NAT unavailable on this host -> building an IPv4-only tunnel"
  WG_NET6=""
fi

ADDRESSES="$WG_NET"
[[ -n "$WG_NET6" ]] && ADDRESSES="$WG_NET, $WG_NET6"

if [[ ! -f "$CONF" ]]; then
  cat > "$CONF" <<EOF
[Interface]
Address = $ADDRESSES
ListenPort = $WG_PORT
PrivateKey = $(cat /etc/wireguard/server.key)
# Only the local WSS bridge talks to this port; block it from the internet.
PostUp   = iptables -I INPUT -p udp --dport $WG_PORT ! -i lo -j DROP
PostUp   = iptables -t nat -A POSTROUTING -o $WAN_IF -j MASQUERADE
PostUp   = iptables -I FORWARD -i wg0 -j ACCEPT
PostUp   = iptables -I FORWARD -o wg0 -j ACCEPT
${WG_NET6:+PostUp   = ip6tables -I INPUT -p udp --dport $WG_PORT ! -i lo -j DROP || true}
${WG_NET6:+PostUp   = ip6tables -t nat -A POSTROUTING -o $WAN_IF -j MASQUERADE || true}
${WG_NET6:+PostUp   = ip6tables -I FORWARD -i wg0 -j ACCEPT || true}
${WG_NET6:+PostUp   = ip6tables -I FORWARD -o wg0 -j ACCEPT || true}
PostDown = iptables -D INPUT -p udp --dport $WG_PORT ! -i lo -j DROP
PostDown = iptables -t nat -D POSTROUTING -o $WAN_IF -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT
PostDown = iptables -D FORWARD -o wg0 -j ACCEPT
${WG_NET6:+PostDown = ip6tables -D INPUT -p udp --dport $WG_PORT ! -i lo -j DROP || true}
${WG_NET6:+PostDown = ip6tables -t nat -D POSTROUTING -o $WAN_IF -j MASQUERADE || true}
${WG_NET6:+PostDown = ip6tables -D FORWARD -i wg0 -j ACCEPT || true}
${WG_NET6:+PostDown = ip6tables -D FORWARD -o wg0 -j ACCEPT || true}
EOF
  sed -i '/^$/{/./!d}' "$CONF"
  echo "==> wrote $CONF"
else
  echo "==> $CONF already exists, leaving it alone"
fi

echo "==> enabling IP forwarding"
{
  echo 'net.ipv4.ip_forward=1'
  [[ -n "$WG_NET6" ]] && echo 'net.ipv6.conf.all.forwarding=1'
} > /etc/sysctl.d/99-wg-wss.conf
sysctl -q -w net.ipv4.ip_forward=1
[[ -n "$WG_NET6" ]] && sysctl -q -w net.ipv6.conf.all.forwarding=1 2>/dev/null || true

echo "==> obtaining a TLS certificate"
if [[ -n "$DOMAIN" ]]; then
  if [[ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]]; then
    systemctl stop wg-wss 2>/dev/null || true
    certbot certonly --standalone --non-interactive --agree-tos \
      --register-unsafely-without-email -d "$DOMAIN"
  fi
  CERT="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
  KEY="/etc/letsencrypt/live/$DOMAIN/privkey.pem"
  install -d /etc/letsencrypt/renewal-hooks/deploy
  printf '#!/bin/sh\nsystemctl restart wg-wss\n' \
    > /etc/letsencrypt/renewal-hooks/deploy/wg-wss.sh
  chmod +x /etc/letsencrypt/renewal-hooks/deploy/wg-wss.sh
else
  echo "    no domain given -> self-signed certificate"
  "$SRC_DIR/scripts/gen-selfsigned.sh" /etc/wg-wss "$(curl -s4 --max-time 5 ifconfig.me || echo 127.0.0.1)"
  CERT=/etc/wg-wss/cert.pem
  KEY=/etc/wg-wss/key.pem
fi

TOKEN="${TOKEN:-$(openssl rand -hex 24)}"
cat > "$ENVFILE" <<EOF
WGWS_LISTEN=0.0.0.0:$WSS_PORT,[::]:$WSS_PORT
WGWS_WG=127.0.0.1:$WG_PORT
WGWS_PATH=$WS_PATH
WGWS_TOKEN=$TOKEN
WGWS_CERT=$CERT
WGWS_KEY=$KEY
EOF
chmod 600 "$ENVFILE"

echo "==> installing the systemd service"
sed -e "s|@PREFIX@|$PREFIX|g" "$SRC_DIR/systemd/wg-wss.service" \
  > /etc/systemd/system/wg-wss.service
systemctl daemon-reload
systemctl enable --now wg-quick@wg0
systemctl restart wg-wss
systemctl enable wg-wss

sleep 1
systemctl --no-pager --lines=5 status wg-wss || true

cat <<EOF

========================================================================
 Server is up.

   endpoint : wss://${DOMAIN:-YOUR_SERVER_IP}:$WSS_PORT$WS_PATH
   token    : $TOKEN
   pubkey   : $(cat /etc/wireguard/server.pub)

 Add a client:   sudo $SRC_DIR/scripts/add-peer.sh phone ${DOMAIN:-YOUR_SERVER_IP}
========================================================================
EOF
