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


class AddressParsingTest(unittest.TestCase):
    """IPv6 literals need brackets in URLs and Host headers, not in sockets."""

    def test_split_hostport(self):
        from wgws.__main__ import _split_hostport

        self.assertEqual(_split_hostport("0.0.0.0:443", 443), ("0.0.0.0", 443))
        self.assertEqual(_split_hostport("127.0.0.1", 51820), ("127.0.0.1", 51820))
        self.assertEqual(_split_hostport("[::]:443", 443), ("::", 443))
        self.assertEqual(_split_hostport("[2001:db8::1]:8443", 443),
                         ("2001:db8::1", 8443))
        self.assertEqual(_split_hostport("[::1]", 51820), ("::1", 51820))

    def test_host_header_brackets_ipv6_only(self):
        from wgws.client import format_host_header

        self.assertEqual(format_host_header("vpn.example.com", 443), "vpn.example.com")
        self.assertEqual(format_host_header("vpn.example.com", 8443),
                         "vpn.example.com:8443")
        self.assertEqual(format_host_header("2001:db8::1", 443), "[2001:db8::1]")
        self.assertEqual(format_host_header("2001:db8::1", 8443), "[2001:db8::1]:8443")

    def test_peer_formatting(self):
        from wgws.client import format_peer

        self.assertEqual(format_peer(("127.0.0.1", 51820)), "127.0.0.1:51820")
        # IPv6 peers arrive as 4-tuples with flowinfo and scope id.
        self.assertEqual(format_peer(("::1", 51820, 0, 0)), "[::1]:51820")

    def test_client_url_parsing(self):
        from wgws.__main__ import build_parser, prepare_client
        import socket as s

        args = prepare_client(build_parser().parse_args(
            ["client", "wss://[2001:db8::1]:8443/secret"]))
        self.assertEqual((args.host, args.port, args.path),
                         ("2001:db8::1", 8443, "/secret"))
        self.assertFalse(args.no_tls)
        self.assertEqual(args.family, 0)

        args = prepare_client(build_parser().parse_args(
            ["client", "ws://vpn.example.com/ws", "--family", "6"]))
        self.assertTrue(args.no_tls)
        self.assertEqual((args.port, args.family), (80, s.AF_INET6))

    def test_server_listen_list_and_family_filter(self):
        from wgws.__main__ import build_parser, prepare_server
        import socket as s

        base = ["server", "--no-tls", "--listen"]
        args = prepare_server(build_parser().parse_args(
            base + ["0.0.0.0:443,[::]:443"]))
        self.assertEqual(args.listen_hosts, ["0.0.0.0", "::"])
        self.assertEqual(args.listen_port, 443)

        args = prepare_server(build_parser().parse_args(
            base + ["0.0.0.0:443,[::]:443", "--family", "6"]))
        self.assertEqual(args.listen_hosts, ["::"])

        args = prepare_server(build_parser().parse_args(
            base + ["0.0.0.0:443,[::]:443", "--family", "4"]))
        self.assertEqual(args.listen_hosts, ["0.0.0.0"])
        self.assertEqual(args.family, s.AF_INET)

        with self.assertRaises(SystemExit):
            prepare_server(build_parser().parse_args(
                base + ["0.0.0.0:443,[::]:8443"]))  # mismatched ports


class FamilyHandlingTest(unittest.TestCase):
    """Regressions from the IPv6 review: each of these used to misbehave."""

    def test_unbracketed_ipv6_literal_has_no_port(self):
        from wgws.__main__ import _split_hostport

        self.assertEqual(_split_hostport("::", 443), ("::", 443))
        self.assertEqual(_split_hostport("2001:db8::1", 443), ("2001:db8::1", 443))

    def test_invalid_port_exits_cleanly(self):
        from wgws.__main__ import _split_hostport

        with self.assertRaises(SystemExit):
            _split_hostport("example.com:not-a-port", 443)

    def test_v6only_set_when_ipv4_is_bound_separately(self):
        import socket as s
        from wgws.server import needs_v6only

        self.assertTrue(needs_v6only(0, ["0.0.0.0", "::"]))
        # --family 6 means IPv6 alone, so IPv4-mapped clients must be refused.
        self.assertTrue(needs_v6only(s.AF_INET6, ["::"]))
        # "::" on its own is the dual-stack case and must accept both.
        self.assertFalse(needs_v6only(0, ["::"]))

    def test_unresolvable_listen_host_does_not_raise(self):
        from wgws.server import _family_of

        self.assertIsNone(_family_of("no-such-host.invalid"))

    def test_family_filter_resolves_names_instead_of_guessing(self):
        import socket as s
        from wgws.__main__ import _usable_in_family

        self.assertTrue(_usable_in_family("0.0.0.0", s.AF_INET))
        self.assertFalse(_usable_in_family("0.0.0.0", s.AF_INET6))
        self.assertTrue(_usable_in_family("::", s.AF_INET6))
        self.assertFalse(_usable_in_family("::", s.AF_INET))
        # A name is decided by resolution, never by looking for a colon.
        self.assertTrue(_usable_in_family("localhost", s.AF_INET))
        self.assertTrue(_usable_in_family("anything", 0))


if __name__ == "__main__":
    unittest.main()
