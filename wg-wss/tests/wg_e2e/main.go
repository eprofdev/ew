// A real WireGuard handshake through the WSS tunnel, entirely in userspace.
//
//	go run ./tests/wg_e2e
//
// Two userspace WireGuard devices (wireguard-go's netstack TUN) stand in for
// the VPS and the phone. The client's Endpoint points at the local wgws
// bridge instead of the server, so every encrypted WireGuard packet travels
// as a WebSocket binary frame over TLS. If the HTTP request at the end
// succeeds, a genuine Noise handshake and real IP traffic went through the
// tunnel. Needs no root, no TUN device and no kernel WireGuard module.
package main

import (
	"context"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun/netstack"
)

const (
	serverIP  = "10.9.0.1"
	clientIP  = "10.9.0.2"
	wgPort    = 51900 // "WireGuard" on the fake VPS
	wssPort   = 8443  // the TLS WebSocket listener
	localUDP  = 51901 // what the client's Endpoint points at
	token     = "real-wg-test-token"
	tunnelURI = "/wg"
)

func keypair() (privHex, pubHex string) {
	priv, err := exec.Command("wg", "genkey").Output()
	must(err)
	cmd := exec.Command("wg", "pubkey")
	cmd.Stdin = strings.NewReader(string(priv))
	pub, err := cmd.Output()
	must(err)
	return toHex(string(priv)), toHex(string(pub))
}

func toHex(b64 string) string {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(b64))
	must(err)
	return hex.EncodeToString(raw)
}

func must(err error) {
	if err != nil {
		fmt.Println("[wg-e2e] FATAL:", err)
		os.Exit(1)
	}
}

// findRoot walks up from the working directory until it finds the wgws package.
func findRoot() string {
	dir, err := filepath.Abs(".")
	must(err)
	for i := 0; i < 6; i++ {
		if _, err := os.Stat(filepath.Join(dir, "wgws", "__main__.py")); err == nil {
			return dir
		}
		dir = filepath.Dir(dir)
	}
	must(fmt.Errorf("could not find the wgws package above the working directory"))
	return ""
}

func step(format string, args ...any) {
	fmt.Printf("[wg-e2e] "+format+"\n", args...)
}

func main() {
	root := findRoot()
	tmp, err := os.MkdirTemp("", "wg-e2e-")
	must(err)
	defer os.RemoveAll(tmp)

	step("generating a self-signed certificate")
	cert := filepath.Join(tmp, "cert.pem")
	key := filepath.Join(tmp, "key.pem")
	out, err := exec.Command("openssl", "req", "-x509", "-newkey", "rsa:2048",
		"-nodes", "-keyout", key, "-out", cert, "-days", "1",
		"-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1").CombinedOutput()
	if err != nil {
		fmt.Println(string(out))
		must(err)
	}

	srvPriv, srvPub := keypair()
	cliPriv, cliPub := keypair()
	step("server key %s..., client key %s...", srvPub[:16], cliPub[:16])

	// -- the "VPS": a WireGuard device listening on UDP, serving HTTP inside.
	srvTun, srvNet, err := netstack.CreateNetTUN(
		[]netip.Addr{netip.MustParseAddr(serverIP)},
		[]netip.Addr{netip.MustParseAddr("1.1.1.1")}, 1280)
	must(err)
	srvDev := device.NewDevice(srvTun, conn.NewDefaultBind(),
		device.NewLogger(device.LogLevelError, "wg-server "))
	must(srvDev.IpcSet(fmt.Sprintf(
		"private_key=%s\nlisten_port=%d\npublic_key=%s\nallowed_ip=%s/32\n",
		srvPriv, wgPort, cliPub, clientIP)))
	must(srvDev.Up())
	defer srvDev.Close()

	listener, err := srvNet.ListenTCP(&net.TCPAddr{Port: 80})
	must(err)
	go http.Serve(listener, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "hello from %s over wireguard-in-websocket\n", serverIP)
	}))
	step("wireguard server up on udp/%d, http inside the tunnel on %s:80", wgPort, serverIP)

	// -- the tunnel: python wgws server and client.
	srv := exec.Command("python3", "-m", "wgws", "server",
		"--listen", fmt.Sprintf("127.0.0.1:%d", wssPort),
		"--wg", fmt.Sprintf("127.0.0.1:%d", wgPort),
		"--cert", cert, "--key", key, "--path", tunnelURI, "--token", token)
	srv.Dir, srv.Stdout, srv.Stderr = root, os.Stdout, os.Stderr
	must(srv.Start())
	defer srv.Process.Kill()

	cli := exec.Command("python3", "-m", "wgws", "client",
		fmt.Sprintf("wss://127.0.0.1:%d%s", wssPort, tunnelURI),
		"--listen", fmt.Sprintf("127.0.0.1:%d", localUDP),
		"--insecure", "--token", token)
	cli.Dir, cli.Stdout, cli.Stderr = root, os.Stdout, os.Stderr
	must(cli.Start())
	defer cli.Process.Kill()
	time.Sleep(2 * time.Second)

	// -- the "phone": Endpoint points at the local bridge, not the server.
	cliTun, cliNet, err := netstack.CreateNetTUN(
		[]netip.Addr{netip.MustParseAddr(clientIP)},
		[]netip.Addr{netip.MustParseAddr("1.1.1.1")}, 1280)
	must(err)
	cliDev := device.NewDevice(cliTun, conn.NewDefaultBind(),
		device.NewLogger(device.LogLevelError, "wg-client "))
	must(cliDev.IpcSet(fmt.Sprintf(
		"private_key=%s\npublic_key=%s\nendpoint=127.0.0.1:%d\n"+
			"allowed_ip=0.0.0.0/0\npersistent_keepalive_interval=5\n",
		cliPriv, srvPub, localUDP)))
	must(cliDev.Up())
	defer cliDev.Close()
	step("wireguard client up, Endpoint = 127.0.0.1:%d (the WSS bridge)", localUDP)

	// -- real traffic across the tunnel.
	client := &http.Client{
		Transport: &http.Transport{DialContext: cliNet.DialContext},
		Timeout:   10 * time.Second,
	}
	var body string
	deadline := time.Now().Add(45 * time.Second)
	for time.Now().Before(deadline) {
		ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
		req, _ := http.NewRequestWithContext(ctx, "GET", "http://"+serverIP+"/", nil)
		resp, err := client.Do(req)
		if err == nil {
			data, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			body = strings.TrimSpace(string(data))
			cancel()
			break
		}
		cancel()
		step("waiting for the wireguard handshake ... (%v)", err)
		time.Sleep(2 * time.Second)
	}
	if body == "" {
		step("FAILED: no reply came back through the tunnel")
		os.Exit(1)
	}
	step("PASS  http reply through the tunnel: %q", body)

	// Throughput over the tunnel, to show it carries more than a handshake.
	start := time.Now()
	var total int
	for i := 0; i < 20; i++ {
		resp, err := client.Get("http://" + serverIP + "/")
		if err != nil {
			step("FAILED on request %d: %v", i, err)
			os.Exit(1)
		}
		n, _ := io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		total += int(n)
	}
	step("PASS  20 more requests (%d bytes) in %v", total, time.Since(start).Round(time.Millisecond))

	ipc, err := srvDev.IpcGet()
	must(err)
	for _, line := range strings.Split(ipc, "\n") {
		if strings.HasPrefix(line, "rx_bytes") || strings.HasPrefix(line, "tx_bytes") ||
			strings.HasPrefix(line, "last_handshake_time_sec") {
			step("server sees %s", line)
		}
	}
	step("ALL CHECKS PASSED - a real WireGuard session ran over WSS")
}
