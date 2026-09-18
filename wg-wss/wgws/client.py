"""Local UDP -> WSS bridge.

Listens on a local UDP port that the WireGuard client points its Endpoint at,
and forwards every datagram over a TLS WebSocket to the server on 443.
One WebSocket connection per local UDP source address, re-established
automatically with backoff whenever it drops.
"""

import asyncio
import logging
import ssl
import time

from . import ws

log = logging.getLogger("wgws.client")

# A connection that lasted at least this long is treated as having worked.
STABLE_AFTER = 30.0


class Session:
    """One local UDP peer (normally a single WireGuard instance)."""

    def __init__(self, addr, cfg, transport, on_done):
        self.addr = addr
        self.cfg = cfg
        self.transport = transport
        self.on_done = on_done
        self.queue = asyncio.Queue(maxsize=cfg.queue_size)
        self.last_seen = time.monotonic()
        self.connected = False
        self.task = asyncio.create_task(self._run())

    def feed(self, data):
        self.last_seen = time.monotonic()
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            log.debug("upstream queue full, dropping %d bytes", len(data))

    def close(self):
        self.task.cancel()

    async def _run(self):
        backoff = self.cfg.backoff_min
        try:
            while True:
                try:
                    conn = await asyncio.wait_for(
                        self._connect(), self.cfg.connect_timeout
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - report and retry
                    log.warning("connect to %s failed: %s", self.cfg.url, exc)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self.cfg.backoff_max)
                    continue
                self.connected = True
                started = time.monotonic()
                log.info("tunnel up for %s", format_peer(self.addr))
                try:
                    await self._pump(conn)
                except (ws.WSClosed, ws.WSError, ConnectionError, OSError) as exc:
                    log.info("tunnel down (%s), reconnecting", exc)
                finally:
                    self.connected = False
                    await conn.close()
                # A tunnel that stayed up is healthy, so the next dial starts
                # from the shortest delay. One that died on arrival must back
                # off exactly like a refused connection, or a server that
                # accepts and immediately drops turns into a TLS handshake spin.
                if time.monotonic() - started >= STABLE_AFTER:
                    backoff = self.cfg.backoff_min
                else:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self.cfg.backoff_max)
        except asyncio.CancelledError:
            pass
        finally:
            self.on_done(self.addr)

    async def _connect(self):
        cfg = self.cfg
        ctx = build_ssl_context(cfg)
        reader, writer = await asyncio.open_connection(
            cfg.host, cfg.port, ssl=ctx, family=cfg.family,
            server_hostname=(cfg.sni or cfg.host) if ctx else None,
        )
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                import socket as _s

                sock.setsockopt(_s.IPPROTO_TCP, _s.TCP_NODELAY, 1)
            except OSError:
                pass
        headers = {}
        if cfg.token:
            headers["Authorization"] = "Bearer %s" % cfg.token
        host_header = cfg.host_header or format_host_header(cfg.host, cfg.port)
        return await ws.client_handshake(reader, writer, host_header, cfg.path, headers)

    async def _pump(self, conn):
        up = asyncio.create_task(self._upstream(conn))
        down = asyncio.create_task(self._downstream(conn))
        done, pending = await asyncio.wait(
            [up, down], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc:
                raise exc

    async def _upstream(self, conn):
        """Local UDP -> WebSocket, pinging when there is nothing to send."""
        while True:
            try:
                packet = await asyncio.wait_for(
                    self.queue.get(), self.cfg.ping_interval
                )
            except asyncio.TimeoutError:
                await conn.ping()
                continue
            await conn.send(packet)

    async def _downstream(self, conn):
        """WebSocket -> local UDP."""
        while True:
            packet = await conn.recv()
            if packet:
                self.last_seen = time.monotonic()
                self.transport.sendto(packet, self.addr)


class LocalUDP(asyncio.DatagramProtocol):
    def __init__(self, cfg):
        self.cfg = cfg
        self.sessions = {}
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        session = self.sessions.get(addr)
        if session is None:
            log.info("new local peer %s", format_peer(addr))
            session = Session(addr, self.cfg, self.transport, self._drop)
            self.sessions[addr] = session
        session.feed(data)

    def _drop(self, addr):
        self.sessions.pop(addr, None)

    async def reap(self):
        """Close sessions that have seen no traffic for a while."""
        while True:
            await asyncio.sleep(self.cfg.idle_timeout / 2)
            now = time.monotonic()
            for addr, session in list(self.sessions.items()):
                if now - session.last_seen > self.cfg.idle_timeout:
                    log.info("idle session %s closed", format_peer(addr))
                    session.close()


def format_peer(addr):
    """Readable form of a UDP peer address, IPv4 or IPv6."""
    host, port = addr[0], addr[1]
    return "[%s]:%d" % (host, port) if ":" in host else "%s:%d" % (host, port)


def format_host_header(host, port):
    """Host header value, with IPv6 literals in brackets as RFC 3986 requires."""
    literal = "[%s]" % host if ":" in host else host
    return literal if port in (80, 443) else "%s:%d" % (literal, port)


def build_ssl_context(cfg):
    if cfg.no_tls:
        return None
    if cfg.insecure:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ctx = ssl.create_default_context(cafile=cfg.ca)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


async def run(cfg):
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(
        lambda: LocalUDP(cfg), local_addr=(cfg.listen_host, cfg.listen_port)
    )
    local = format_peer((cfg.listen_host, cfg.listen_port))
    log.info(
        "listening on udp %s -> %s://%s%s%s",
        local, "ws" if cfg.no_tls else "wss",
        format_host_header(cfg.host, cfg.port), cfg.path,
        " (TLS verification disabled)" if cfg.insecure else "",
    )
    log.info("point your WireGuard client at  Endpoint = %s", local)
    reaper = asyncio.create_task(proto.reap())
    try:
        await asyncio.Future()
    finally:
        reaper.cancel()
        for session in list(proto.sessions.values()):
            session.close()
        transport.close()
