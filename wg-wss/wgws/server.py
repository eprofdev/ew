"""WSS -> WireGuard bridge.

Terminates TLS on 443 (or runs plain behind nginx/Caddy), accepts WebSocket
connections on a secret path, and relays every binary message to the local
WireGuard UDP port. Each WebSocket connection gets its own UDP socket, so
WireGuard's replies find their way back to the right client.
"""

import asyncio
import hmac
import logging
import socket
import ssl
import time

from . import ws

log = logging.getLogger("wgws.server")

DECOY_PAGE = (
    "<!doctype html><html><head><title>Welcome to nginx!</title></head>"
    "<body><h1>Welcome to nginx!</h1>"
    "<p>If you see this page, the nginx web server is successfully installed "
    "and working. Further configuration is required.</p></body></html>"
)


class _UDPRelay(asyncio.DatagramProtocol):
    """Receives WireGuard's replies and pushes them into a queue."""

    def __init__(self, queue):
        self.queue = queue
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            log.debug("downstream queue full, dropping %d bytes", len(data))

    def error_received(self, exc):
        # ICMP port-unreachable arrives here when WireGuard is not listening.
        log.debug("udp error: %s", exc)


class Server:
    def __init__(self, config):
        self.cfg = config
        self.sessions = 0

    # -- helpers ---------------------------------------------------------
    def _authorized(self, target, headers):
        if target.split("?", 1)[0] != self.cfg.path:
            return False
        if not self.cfg.token:
            return True
        sent = headers.get("authorization", "")
        if sent.lower().startswith("bearer "):
            sent = sent[7:]
        else:
            sent = headers.get("x-auth-token", "")
        return hmac.compare_digest(sent, self.cfg.token)

    async def _serve_decoy(self, writer, status="200 OK"):
        body = self.cfg.decoy_body
        head = (
            "HTTP/1.1 %s\r\n"
            "Server: nginx\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n\r\n" % (status, len(body))
        )
        try:
            writer.write(head.encode("latin-1") + body)
            await writer.drain()
        except (OSError, ConnectionError):
            pass
        finally:
            writer.close()

    # -- connection handling ---------------------------------------------
    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername")
        peer_repr = "%s:%s" % peer[:2] if peer else "?"
        try:
            start, headers = await ws.read_http_head(
                reader, timeout=self.cfg.handshake_timeout
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError,
                asyncio.LimitOverrunError, ws.WSError, UnicodeDecodeError):
            writer.close()
            return

        parts = start.split(" ")
        target = parts[1] if len(parts) > 1 else "/"
        if not ws.is_websocket_upgrade(headers) or not self._authorized(target, headers):
            log.info("decoy response to %s for %s", peer_repr, target)
            await self._serve_decoy(writer)
            return

        try:
            writer.write(ws.server_accept_response(headers))
            await writer.drain()
        except (OSError, ConnectionError):
            writer.close()
            return

        conn = ws.WSConnection(reader, writer, mask_out=False)
        self.sessions += 1
        log.info("tunnel open from %s (%d active)", peer_repr, self.sessions)
        started = time.monotonic()
        try:
            await self._pump(conn)
        except (ws.WSClosed, ws.WSError, ConnectionError, OSError) as exc:
            log.debug("tunnel from %s ended: %s", peer_repr, exc)
        finally:
            self.sessions -= 1
            log.info(
                "tunnel closed from %s after %.0fs (up %d B / down %d B)",
                peer_repr, time.monotonic() - started, conn.bytes_in, conn.bytes_out,
            )
            await conn.close()

    async def _pump(self, conn):
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=self.cfg.queue_size)
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _UDPRelay(queue),
            remote_addr=(self.cfg.wg_host, self.cfg.wg_port),
            family=socket.AF_INET,
        )
        try:
            up = asyncio.create_task(self._upstream(conn, transport))
            down = asyncio.create_task(self._downstream(conn, queue))
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
        finally:
            transport.close()

    async def _upstream(self, conn, transport):
        """WebSocket -> WireGuard."""
        while True:
            try:
                packet = await asyncio.wait_for(conn.recv(), self.cfg.idle_timeout)
            except asyncio.TimeoutError:
                log.debug("idle timeout")
                return
            if packet:
                transport.sendto(packet)

    async def _downstream(self, conn, queue):
        """WireGuard -> WebSocket, with periodic pings as keepalive."""
        while True:
            try:
                packet = await asyncio.wait_for(queue.get(), self.cfg.ping_interval)
            except asyncio.TimeoutError:
                await conn.ping()
                continue
            await conn.send(packet)


def build_ssl_context(cfg):
    if cfg.no_tls:
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cfg.cert, cfg.key)
    try:
        ctx.set_alpn_protocols(["http/1.1"])
    except NotImplementedError:
        pass
    return ctx


async def run(cfg):
    server = Server(cfg)
    ssl_ctx = build_ssl_context(cfg)
    srv = await asyncio.start_server(
        server.handle, cfg.listen_host, cfg.listen_port, ssl=ssl_ctx,
        reuse_address=True, backlog=256,
    )
    where = ", ".join(str(s.getsockname()) for s in srv.sockets)
    log.info(
        "listening on %s (%s) -> wireguard %s:%d, path %s, auth %s",
        where, "plain HTTP behind a proxy" if cfg.no_tls else "TLS",
        cfg.wg_host, cfg.wg_port, cfg.path, "on" if cfg.token else "off",
    )
    async with srv:
        await srv.serve_forever()
