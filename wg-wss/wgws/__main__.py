"""Command line entry point: `python3 -m wgws server|client`."""

import argparse
import asyncio
import logging
import os
import socket
import sys
from urllib.parse import urlsplit

from . import __version__, client, server


def _env(name, default=None):
    return os.environ.get("WGWS_" + name.upper(), default)


def _add_common(parser):
    parser.add_argument("--path", default=_env("path", "/ws"),
                        help="secret WebSocket path (default: /ws)")
    parser.add_argument("--token", default=_env("token", ""),
                        help="shared secret sent as an Authorization header")
    parser.add_argument("--family", choices=("auto", "4", "6"), default="auto",
                        help="restrict to IPv4 or IPv6 (default: auto)")
    parser.add_argument("--ping-interval", type=float, default=25.0,
                        help="seconds between keepalive pings (default: 25)")
    parser.add_argument("--idle-timeout", type=float, default=600.0,
                        help="drop a tunnel after this many idle seconds")
    parser.add_argument("--queue-size", type=int, default=2048,
                        help="packets buffered per direction (default: 2048)")
    parser.add_argument("-v", "--verbose", action="store_true")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="wgws",
        description="Tunnel WireGuard over a TLS WebSocket on port 443.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="mode", required=True)

    srv = sub.add_parser("server", help="run on the VPS, next to WireGuard")
    srv.add_argument("--listen", default=_env("listen", "0.0.0.0:443,[::]:443"),
                     help="comma-separated addresses to listen on "
                          "(default: 0.0.0.0:443,[::]:443 - both families)")
    srv.add_argument("--wg", default=_env("wg", "127.0.0.1:51820"),
                     help="local WireGuard UDP endpoint (default: 127.0.0.1:51820)")
    srv.add_argument("--cert", default=_env("cert"), help="TLS certificate chain (PEM)")
    srv.add_argument("--key", default=_env("key"), help="TLS private key (PEM)")
    srv.add_argument("--no-tls", action="store_true",
                     help="serve plain HTTP (only behind nginx/Caddy on localhost)")
    srv.add_argument("--decoy-file",
                     help="HTML file served to anything that is not a valid tunnel request")
    srv.add_argument("--handshake-timeout", type=float, default=15.0)
    _add_common(srv)

    cli = sub.add_parser("client", help="run on your device, in front of WireGuard")
    cli.add_argument("url", help="wss://host[:port]/path of the server")
    cli.add_argument("--listen", default=_env("client_listen", "127.0.0.1:51820"),
                     help="local UDP address for WireGuard (default: 127.0.0.1:51820)")
    cli.add_argument("--ca", default=_env("ca"),
                     help="CA bundle to verify the server certificate")
    cli.add_argument("--insecure", action="store_true",
                     help="skip certificate verification (self-signed testing only)")
    cli.add_argument("--sni", default=_env("sni"), help="TLS server name to present")
    cli.add_argument("--host-header", default=_env("host_header"),
                     help="HTTP Host header to send")
    cli.add_argument("--connect-timeout", type=float, default=20.0)
    cli.add_argument("--backoff-min", type=float, default=1.0)
    cli.add_argument("--backoff-max", type=float, default=30.0)
    _add_common(cli)
    return parser


FAMILIES = {"auto": 0, "4": socket.AF_INET, "6": socket.AF_INET6}


def _usable_in_family(host, family):
    """Whether a listen address can be bound in the requested family.

    Resolution decides it, so a DNS name with only AAAA records is kept under
    --family 6 and a literal of the wrong family is dropped.
    """
    if family == 0:
        return True
    try:
        socket.getaddrinfo(host, None, family=family, flags=socket.AI_PASSIVE)
        return True
    except OSError:
        return False


def _split_hostport(value, default_port):
    if value.startswith("["):  # [::1]:443
        host, _, rest = value[1:].partition("]")
        port = int(rest.lstrip(":")) if rest.lstrip(":") else default_port
        return host, port
    if value.count(":") > 1:
        # An unbracketed IPv6 literal such as "::" or "2001:db8::1" carries no
        # port; only the bracketed form can.
        return value, default_port
    host, _, port = value.partition(":")
    try:
        return host, int(port) if port else default_port
    except ValueError:
        raise SystemExit("error: %r is not a valid address" % value)


def prepare_server(args):
    args.family = FAMILIES[args.family]
    args.listen_hosts = []
    ports = set()
    for item in args.listen.split(","):
        item = item.strip()
        if not item:
            continue
        host, port = _split_hostport(item, 443)
        if not _usable_in_family(host, args.family):
            continue
        args.listen_hosts.append(host)
        ports.add(port)
    if not args.listen_hosts:
        raise SystemExit("error: --listen has no address for the chosen --family")
    if len(ports) > 1:
        raise SystemExit("error: every --listen address must use the same port")
    args.listen_port = ports.pop()
    args.listen_host = args.listen_hosts[0]
    args.wg_host, args.wg_port = _split_hostport(args.wg, 51820)
    if not args.no_tls:
        if not args.cert or not args.key:
            raise SystemExit(
                "error: --cert and --key are required unless --no-tls is used"
            )
        for path in (args.cert, args.key):
            if not os.path.exists(path):
                raise SystemExit("error: no such file: %s" % path)
    if args.decoy_file:
        with open(args.decoy_file, "rb") as handle:
            args.decoy_body = handle.read()
    else:
        args.decoy_body = server.DECOY_PAGE.encode("utf-8")
    return args


def prepare_client(args):
    parts = urlsplit(args.url)
    if parts.scheme not in ("wss", "ws", "https", "http"):
        raise SystemExit("error: url must start with wss:// (or ws:// behind a proxy)")
    args.no_tls = parts.scheme in ("ws", "http")
    args.host = parts.hostname
    if not args.host:
        raise SystemExit("error: could not parse a host out of %s" % args.url)
    args.port = parts.port or (80 if args.no_tls else 443)
    if parts.path and parts.path != "/":
        args.path = parts.path
    args.family = FAMILIES[args.family]
    args.listen_host, args.listen_port = _split_hostport(args.listen, 51820)
    return args


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if args.mode == "server":
        coro = server.run(prepare_server(args))
    else:
        coro = client.run(prepare_client(args))
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
