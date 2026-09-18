"""Unit tests for the WebSocket framing layer: python3 -m unittest discover tests"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wgws import ws  # noqa: E402


class MaskTest(unittest.TestCase):
    def test_mask_is_its_own_inverse(self):
        for size in (0, 1, 3, 4, 5, 125, 126, 1420, 65536):
            data = os.urandom(size)
            key = os.urandom(4)
            self.assertEqual(ws.apply_mask(ws.apply_mask(data, key), key), data)

    def test_mask_matches_naive_xor(self):
        data = os.urandom(133)
        key = os.urandom(4)
        naive = bytes(b ^ key[i % 4] for i, b in enumerate(data))
        self.assertEqual(ws.apply_mask(data, key), naive)

    def test_leading_zero_bytes_survive(self):
        data = b"\x00\x00\x00\x01wireguard"
        key = b"\x00\x00\x00\x00"
        self.assertEqual(ws.apply_mask(data, key), data)


class AcceptKeyTest(unittest.TestCase):
    def test_rfc6455_example(self):
        # The worked example from RFC 6455 section 1.3.
        self.assertEqual(
            ws.accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )


class FrameRoundTripTest(unittest.IsolatedAsyncioTestCase):
    async def _roundtrip(self, payload, mask):
        reader = asyncio.StreamReader()
        reader.feed_data(ws.build_frame(ws.OP_BIN, payload, mask))
        reader.feed_eof()
        conn = ws.WSConnection(reader, None, mask_out=False)
        fin, opcode, data = await conn._read_frame()
        self.assertTrue(fin)
        self.assertEqual(opcode, ws.OP_BIN)
        self.assertEqual(data, payload)

    async def test_all_length_classes(self):
        # 7-bit, 16-bit and 64-bit length encodings, masked and unmasked.
        for size in (0, 5, 125, 126, 1420, 70000):
            for mask in (False, True):
                await self._roundtrip(os.urandom(size), mask)

    async def test_recv_handles_ping_and_fragments(self):
        reader = asyncio.StreamReader()
        sent = []

        class FakeWriter:
            def write(self, data):
                sent.append(data)

            async def drain(self):
                pass

        reader.feed_data(ws.build_frame(ws.OP_PING, b"hi", True))
        reader.feed_data(ws.build_frame(ws.OP_BIN, b"wire", True))
        conn = ws.WSConnection(reader, FakeWriter(), mask_out=False)

        # A fragmented message: first BIN (fin=0) then CONT (fin=1).
        reader2 = asyncio.StreamReader()
        head = bytearray(ws.build_frame(ws.OP_BIN, b"wire", False))
        head[0] &= 0x7F  # clear FIN
        reader2.feed_data(bytes(head))
        cont = bytearray(ws.build_frame(ws.OP_CONT, b"guard", False))
        reader2.feed_data(bytes(cont))
        conn2 = ws.WSConnection(reader2, FakeWriter(), mask_out=False)
        self.assertEqual(await conn2.recv(), b"wireguard")

        # The ping above must be answered with a pong before the data message.
        self.assertEqual(await conn.recv(), b"wire")
        self.assertTrue(sent and sent[0][0] & 0x0F == ws.OP_PONG)

    async def test_oversized_message_is_rejected(self):
        reader = asyncio.StreamReader()
        reader.feed_data(ws.build_frame(ws.OP_BIN, b"x" * 300, False))
        conn = ws.WSConnection(reader, None, mask_out=False, max_message=100)
        with self.assertRaises(ws.WSError):
            await conn.recv()

    async def test_closed_stream_raises_wsclosed(self):
        reader = asyncio.StreamReader()
        reader.feed_eof()
        conn = ws.WSConnection(reader, None, mask_out=False)
        with self.assertRaises(ws.WSClosed):
            await conn.recv()


class HandshakeParsingTest(unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_detection(self):
        reader = asyncio.StreamReader()
        reader.feed_data(
            b"GET /ws HTTP/1.1\r\nHost: vpn.example\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
        )
        start, headers = await ws.read_http_head(reader)
        self.assertEqual(start, "GET /ws HTTP/1.1")
        self.assertTrue(ws.is_websocket_upgrade(headers))
        self.assertIn(b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", ws.server_accept_response(headers))

    async def test_plain_request_is_not_an_upgrade(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b"GET / HTTP/1.1\r\nHost: vpn.example\r\n\r\n")
        _, headers = await ws.read_http_head(reader)
        self.assertFalse(ws.is_websocket_upgrade(headers))


if __name__ == "__main__":
    unittest.main()
