"""Minimal RFC 6455 WebSocket implementation on top of asyncio streams.

Only what the tunnel needs: binary messages, ping/pong keepalive, close
handshake, client-side masking. No third-party dependencies.
"""

import asyncio
import base64
import hashlib
import os
import struct

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

CONTROL_OPS = (OP_CLOSE, OP_PING, OP_PONG)

# A WireGuard datagram never exceeds the interface MTU by much; anything
# larger than this is either a bug or someone poking at the port.
MAX_MESSAGE = 128 * 1024


class WSError(Exception):
    """Protocol violation."""


class WSClosed(Exception):
    """Peer closed the connection (cleanly or not)."""


def accept_key(key):
    """Sec-WebSocket-Accept value for a given Sec-WebSocket-Key."""
    digest = hashlib.sha1(key.encode("ascii", "strict") + GUID).digest()
    return base64.b64encode(digest).decode("ascii")


def apply_mask(data, mask):
    """XOR `data` with the 4-byte `mask` (same operation both directions)."""
    n = len(data)
    if n == 0:
        return data
    key = int.from_bytes(mask * ((n + 3) // 4), "big") >> (8 * ((-n) % 4))
    return (int.from_bytes(data, "big") ^ key).to_bytes(n, "big")


def build_frame(opcode, payload=b"", mask=False):
    """Serialize a single final frame."""
    b1 = 0x80 | opcode
    n = len(payload)
    flag = 0x80 if mask else 0x00
    if n < 126:
        head = struct.pack("!BB", b1, flag | n)
    elif n < 65536:
        head = struct.pack("!BBH", b1, flag | 126, n)
    else:
        head = struct.pack("!BBQ", b1, flag | 127, n)
    if not mask:
        return head + payload
    key = os.urandom(4)
    return head + key + apply_mask(payload, key)


class WSConnection:
    """A WebSocket connection over an established asyncio stream pair."""

    def __init__(self, reader, writer, mask_out, max_message=MAX_MESSAGE):
        self.reader = reader
        self.writer = writer
        self.mask_out = mask_out
        self.max_message = max_message
        self.closed = False
        self.bytes_in = 0
        self.bytes_out = 0
        self._send_lock = asyncio.Lock()

    # -- sending ---------------------------------------------------------
    async def _send_frame(self, opcode, payload=b""):
        if self.closed:
            raise WSClosed("connection is closed")
        frame = build_frame(opcode, payload, self.mask_out)
        async with self._send_lock:
            self.writer.write(frame)
            await self.writer.drain()
        self.bytes_out += len(payload)

    async def send(self, data):
        await self._send_frame(OP_BIN, data)

    async def ping(self, data=b""):
        await self._send_frame(OP_PING, data)

    # -- receiving -------------------------------------------------------
    async def _read_frame(self):
        head = await self.reader.readexactly(2)
        b1, b2 = head[0], head[1]
        if b1 & 0x70:
            raise WSError("reserved bits set")
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", await self.reader.readexactly(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", await self.reader.readexactly(8))[0]
        if opcode in CONTROL_OPS:
            if not fin:
                raise WSError("fragmented control frame")
            if n > 125:
                raise WSError("oversized control frame")
        if n > self.max_message:
            raise WSError("message of %d bytes exceeds limit" % n)
        key = await self.reader.readexactly(4) if masked else None
        payload = await self.reader.readexactly(n) if n else b""
        if key is not None:
            payload = apply_mask(payload, key)
        return fin, opcode, payload

    async def recv(self):
        """Return the next binary message; control frames are handled here."""
        buf = b""
        frag_op = None
        while True:
            try:
                fin, opcode, payload = await self._read_frame()
            except (asyncio.IncompleteReadError, ConnectionError) as exc:
                self.closed = True
                raise WSClosed(str(exc) or "stream ended")
            if opcode == OP_CLOSE:
                self.closed = True
                raise WSClosed("peer sent close")
            if opcode == OP_PING:
                await self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CONT:
                if frag_op is None:
                    raise WSError("continuation without start frame")
            elif opcode in (OP_BIN, OP_TEXT):
                if frag_op is not None:
                    raise WSError("new message before previous one finished")
                frag_op = opcode
            else:
                raise WSError("unknown opcode 0x%x" % opcode)
            buf += payload
            if len(buf) > self.max_message:
                raise WSError("fragmented message exceeds limit")
            if fin:
                self.bytes_in += len(buf)
                return buf

    # -- teardown --------------------------------------------------------
    async def close(self, code=1000):
        if not self.closed:
            self.closed = True
            try:
                await asyncio.wait_for(
                    self._force_frame(OP_CLOSE, struct.pack("!H", code)), 2
                )
            except (OSError, asyncio.TimeoutError, ConnectionError):
                pass
        try:
            self.writer.close()
            await asyncio.wait_for(self.writer.wait_closed(), 2)
        except (OSError, asyncio.TimeoutError, ConnectionError):
            pass

    async def _force_frame(self, opcode, payload):
        frame = build_frame(opcode, payload, self.mask_out)
        async with self._send_lock:
            self.writer.write(frame)
            await self.writer.drain()


async def read_http_head(reader, limit=16384, timeout=15):
    """Read an HTTP request/response head and return (start_line, headers)."""
    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    if len(raw) > limit:
        raise WSError("HTTP head too large")
    lines = raw.decode("latin-1").split("\r\n")
    start = lines[0]
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return start, headers


async def client_handshake(reader, writer, host, path, extra_headers=None):
    """Perform the client side of the opening handshake."""
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    lines = [
        "GET %s HTTP/1.1" % path,
        "Host: %s" % host,
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: %s" % key,
        "Sec-WebSocket-Version: 13",
        "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    ]
    for name, value in (extra_headers or {}).items():
        lines.append("%s: %s" % (name, value))
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    await writer.drain()

    start, headers = await read_http_head(reader)
    parts = start.split(" ", 2)
    if len(parts) < 2 or parts[1] != "101":
        raise WSError("handshake rejected: %s" % start)
    if headers.get("upgrade", "").lower() != "websocket":
        raise WSError("missing Upgrade header in response")
    if headers.get("sec-websocket-accept", "") != accept_key(key):
        raise WSError("bad Sec-WebSocket-Accept")
    return WSConnection(reader, writer, mask_out=True)


def is_websocket_upgrade(headers):
    return (
        headers.get("upgrade", "").lower() == "websocket"
        and "upgrade" in headers.get("connection", "").lower()
        and bool(headers.get("sec-websocket-key"))
    )


def server_accept_response(headers):
    """101 response bytes for a validated upgrade request."""
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Accept: %s\r\n\r\n" % accept_key(headers["sec-websocket-key"])
    ).encode("latin-1")
