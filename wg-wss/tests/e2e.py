"""End-to-end check of the whole data path, no WireGuard or root needed.

Starts a fake "WireGuard" UDP echo service, the real wgws server (with a
self-signed certificate on a real TLS socket) and the real wgws client, then
pushes datagrams through and verifies they come back. Also checks the decoy
page, token rejection and automatic reconnection.
"""

import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
TOKEN = "e2e-secret-token"
PATH = "/tunnel-e2e"


GO_BINARY = ""


def log(msg):
    print("[e2e] %s" % msg, flush=True)


def host_has_ipv6():
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
        return True
    except OSError:
        return False


SKIP_EXIT = 77  # the automake convention, so runners can tell skip from pass

IPV6 = os.environ.get("WGWS_E2E_IPV6") == "1"
if IPV6 and not host_has_ipv6():
    print("[e2e] SKIP: this host has no IPv6 stack", flush=True)
    raise SystemExit(SKIP_EXIT)
LOOPBACK = "::1" if IPV6 else "127.0.0.1"


def hostport(host, port):
    return "[%s]:%d" % (host, port) if ":" in host else "%s:%d" % (host, port)


def free_port(kind=socket.SOCK_STREAM):
    family = socket.AF_INET6 if IPV6 else socket.AF_INET
    with socket.socket(family, kind) as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


def make_cert(tmp):
    cert = os.path.join(tmp, "cert.pem")
    key = os.path.join(tmp, "key.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", key, "-out", cert, "-days", "1", "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        check=True, capture_output=True,
    )
    return cert, key


class FakeWireGuard(threading.Thread):
    """Echoes every datagram back with a marker, like a peer that replies."""

    daemon = True

    def __init__(self, port):
        super().__init__()
        family = socket.AF_INET6 if IPV6 else socket.AF_INET
        self.sock = socket.socket(family, socket.SOCK_DGRAM)
        self.sock.bind((LOOPBACK, port))
        self.running = True
        self.seen = 0

    def run(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65535)
            except OSError:
                return
            self.seen += 1
            self.sock.sendto(b"ECHO:" + data, addr)

    def stop(self):
        self.running = False
        self.sock.close()


def wait_for_tcp(port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((LOOPBACK, port), 0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


GO_CLIENT = os.environ.get("WGWS_E2E_GO_CLIENT") == "1"


def build_go_client():
    """Compile the Go client once and return the path to the binary."""
    out = os.path.join(tempfile.gettempdir(), "wgws-client-e2e")
    subprocess.run(["go", "build", "-o", out, "."],
                   cwd=os.path.join(ROOT, "client-go"), check=True)
    return out


def to_go_args(args):
    """Translate the Python client's argv into the Go client's flags."""
    url = args[1]
    out = []
    i = 2
    while i < len(args):
        item = args[i]
        if item in ("--insecure", "-v"):
            out.append(item)
            i += 1
            continue
        value = args[i + 1]
        if item in ("--ping-interval", "--idle-timeout", "--connect-timeout"):
            value += "s"  # Go wants a duration
        out += [item, value]
        i += 2
    return out + [url]


def spawn(args, name):
    if GO_CLIENT and args[0] == "client":
        cmd = [GO_BINARY] + to_go_args(args)
    else:
        cmd = [PY, "-m", "wgws"] + args
    proc = subprocess.Popen(
        cmd, cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    def drain():
        for line in proc.stdout:
            print("  [%s] %s" % (name, line.rstrip()), flush=True)

    threading.Thread(target=drain, daemon=True).start()
    return proc


def roundtrip(sock, port, payload, timeout=6):
    sock.settimeout(timeout)
    deadline = time.time() + timeout
    sock.sendto(payload, (LOOPBACK, port))
    while time.time() < deadline:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            # WireGuard retransmits too; so do we.
            sock.sendto(payload, (LOOPBACK, port))
            continue
        if data == b"ECHO:" + payload:
            return True
    return False


def https_get(port, path, token=None):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((LOOPBACK, port), 5) as raw:
        with ctx.wrap_socket(raw, server_hostname="localhost") as tls:
            req = "GET %s HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n" % path
            if token:
                req += "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                req += "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                req += "Authorization: Bearer %s\r\n" % token
            tls.sendall((req + "\r\n").encode())
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 8192:
                try:
                    part = tls.recv(4096)
                except (socket.timeout, TimeoutError):
                    break
                if not part:
                    break
                buf += part
    return buf


def main():
    failures = []
    global GO_BINARY
    if GO_CLIENT:
        log("building the Go client")
        GO_BINARY = build_go_client()
    tmp = tempfile.mkdtemp(prefix="wgws-e2e-")
    cert, key = make_cert(tmp)
    wg_port = free_port(socket.SOCK_DGRAM)
    wss_port = free_port()
    local_port = free_port(socket.SOCK_DGRAM)

    wg = FakeWireGuard(wg_port)
    wg.start()
    log("%s client, %s: fake wireguard on udp/%d, wss on %d, local udp on %d"
        % ("Go" if GO_CLIENT else "Python", "IPv6" if IPV6 else "IPv4",
           wg_port, wss_port, local_port))

    server = spawn(
        ["server", "--listen", hostport(LOOPBACK, wss_port),
         "--wg", hostport(LOOPBACK, wg_port), "--cert", cert, "--key", key,
         "--path", PATH, "--token", TOKEN, "--ping-interval", "2", "-v"],
        "server",
    )
    client = spawn(
        ["client", "wss://%s%s" % (hostport(LOOPBACK, wss_port), PATH),
         "--listen", hostport(LOOPBACK, local_port), "--insecure",
         "--token", TOKEN, "--ping-interval", "2", "-v"],
        "client",
    )
    sock = socket.socket(
        socket.AF_INET6 if IPV6 else socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if not wait_for_tcp(wss_port):
            raise SystemExit("server never came up")
        time.sleep(1.0)

        # 1. a single datagram survives the round trip
        if roundtrip(sock, local_port, b"handshake-initiation"):
            log("PASS  single packet round trip")
        else:
            failures.append("single packet round trip")

        # 2. sustained traffic, including full-MTU packets
        ok = 0
        for i in range(50):
            payload = (b"pkt%03d-" % i) + os.urandom(1400 if i % 5 else 60)
            if roundtrip(sock, local_port, payload, timeout=4):
                ok += 1
        if ok == 50:
            log("PASS  50/50 packets (incl. 1400-byte MTU-sized) relayed")
        else:
            failures.append("bulk relay: only %d/50 packets" % ok)

        # 3. an unrelated request gets an ordinary-looking web page
        body = https_get(wss_port, "/")
        if b"200 OK" in body and b"nginx" in body:
            log("PASS  decoy page served on /")
        else:
            failures.append("decoy page: %r" % body[:120])

        # 4. right path, wrong token -> decoy, no upgrade
        body = https_get(wss_port, PATH, token="wrong-token")
        if b"101" not in body and b"200 OK" in body:
            log("PASS  wrong token rejected without revealing the tunnel")
        else:
            failures.append("token check: %r" % body[:120])

        # 5. correct token -> upgrade accepted
        body = https_get(wss_port, PATH, token=TOKEN)
        if b"101 Switching Protocols" in body:
            log("PASS  valid token upgrades to WebSocket")
        else:
            failures.append("valid upgrade: %r" % body[:120])

        # 6. server restart -> client reconnects on its own
        log("restarting the server to test reconnection ...")
        server.terminate()
        server.wait(10)
        time.sleep(1.0)
        server = spawn(
            ["server", "--listen", hostport(LOOPBACK, wss_port),
             "--wg", hostport(LOOPBACK, wg_port), "--cert", cert, "--key", key,
             "--path", PATH, "--token", TOKEN, "-v"],
            "server2",
        )
        if not wait_for_tcp(wss_port):
            raise SystemExit("server did not restart")
        if roundtrip(sock, local_port, b"after-restart", timeout=25):
            log("PASS  tunnel recovered after server restart")
        else:
            failures.append("reconnect after restart")

        # 7. dual-stack listen degrades gracefully on a single-stack host
        dual_port = free_port()
        dual = spawn(
            ["server", "--listen", "127.0.0.1:%d,[::1]:%d" % (dual_port, dual_port),
             "--wg", hostport(LOOPBACK, wg_port), "--cert", cert, "--key", key,
             "--path", PATH, "--token", TOKEN],
            "dual",
        )
        if wait_for_tcp(dual_port):
            log("PASS  dual-stack listen serves %s even when one family is missing"
                % ("IPv6" if IPV6 else "IPv4"))
        else:
            failures.append("dual-stack listen")
        dual.terminate()
        dual.wait(10)

        log("fake wireguard saw %d datagrams" % wg.seen)
    finally:
        sock.close()
        for proc in (server, client):
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        wg.stop()

    print()
    if failures:
        log("FAILED: " + "; ".join(failures))
        return 1
    log("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
