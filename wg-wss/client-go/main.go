// wgws-client - the local half of the WireGuard-over-WSS tunnel.
//
// Listens on a local UDP port that WireGuard points its Endpoint at, and
// carries every datagram to the server as a binary WebSocket frame over TLS.
// One static binary, no dependencies outside the Go standard library, so it
// cross-compiles to Windows, macOS, Linux and Android without a toolchain.
package main

import (
	"crypto/tls"
	"crypto/x509"
	"flag"
	"fmt"
	"log"
	"net"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

var version = "1.0.0"

// A connection that lasted at least this long is treated as having worked.
const stableAfter = 30 * time.Second

type config struct {
	url          string
	host         string
	port         int
	path         string
	plain        bool // ws:// instead of wss://, for use behind a reverse proxy
	listen       string
	token        string
	ca           string
	insecure     bool
	sni          string
	hostHeader   string
	family       string
	pingInterval time.Duration
	idleTimeout  time.Duration
	connTimeout  time.Duration
	backoffMin   time.Duration
	backoffMax   time.Duration
	queueSize    int
	verbose      bool
}

func envOr(name, fallback string) string {
	if v := os.Getenv("WGWS_" + name); v != "" {
		return v
	}
	return fallback
}

func main() {
	cfg := &config{}
	showVersion := flag.Bool("version", false, "print the version and exit")
	flag.StringVar(&cfg.listen, "listen", envOr("CLIENT_LISTEN", "127.0.0.1:51820"),
		"local UDP address WireGuard sends to")
	flag.StringVar(&cfg.token, "token", envOr("TOKEN", ""), "shared secret")
	flag.StringVar(&cfg.path, "path", envOr("PATH", "/ws"),
		"secret WebSocket path (overridden by the path in the url)")
	flag.StringVar(&cfg.ca, "ca", envOr("CA", ""), "CA bundle for the server certificate")
	flag.BoolVar(&cfg.insecure, "insecure", false,
		"skip certificate verification (self-signed testing only)")
	flag.StringVar(&cfg.sni, "sni", envOr("SNI", ""), "TLS server name to present")
	flag.StringVar(&cfg.hostHeader, "host-header", envOr("HOST_HEADER", ""),
		"HTTP Host header to send")
	flag.StringVar(&cfg.family, "family", "auto", "address family: auto, 4 or 6")
	flag.DurationVar(&cfg.pingInterval, "ping-interval", 25*time.Second,
		"keepalive ping interval")
	flag.DurationVar(&cfg.idleTimeout, "idle-timeout", 10*time.Minute,
		"drop a session after this long without traffic")
	flag.DurationVar(&cfg.connTimeout, "connect-timeout", 20*time.Second, "dial timeout")
	flag.DurationVar(&cfg.backoffMin, "backoff-min", time.Second, "first retry delay")
	flag.DurationVar(&cfg.backoffMax, "backoff-max", 30*time.Second, "longest retry delay")
	flag.IntVar(&cfg.queueSize, "queue-size", 2048, "packets buffered per direction")
	flag.BoolVar(&cfg.verbose, "v", false, "verbose logging")
	flag.Usage = func() {
		fmt.Fprintf(os.Stderr,
			"usage: %s [options] wss://host[:port]/path\n\noptions:\n", os.Args[0])
		flag.PrintDefaults()
	}
	flag.Parse()

	if *showVersion {
		fmt.Println("wgws-client", version)
		return
	}
	if flag.NArg() != 1 {
		flag.Usage()
		os.Exit(2)
	}
	switch cfg.family {
	case "auto", "4", "6":
	default:
		fmt.Fprintf(os.Stderr, "error: -family must be auto, 4 or 6\n")
		os.Exit(2)
	}
	if err := parseURL(cfg, flag.Arg(0)); err != nil {
		log.Fatalf("error: %v", err)
	}

	log.SetFlags(log.Ldate | log.Ltime)
	if err := run(cfg); err != nil {
		log.Fatalf("error: %v", err)
	}
}

func parseURL(cfg *config, raw string) error {
	parsed, err := url.Parse(raw)
	if err != nil {
		return err
	}
	switch parsed.Scheme {
	case "wss", "https":
		cfg.plain = false
	case "ws", "http":
		cfg.plain = true
	default:
		return fmt.Errorf("url must start with wss:// (or ws:// behind a proxy)")
	}
	cfg.host = parsed.Hostname()
	if cfg.host == "" {
		return fmt.Errorf("could not parse a host out of %q", raw)
	}
	cfg.port = 443
	if cfg.plain {
		cfg.port = 80
	}
	if p := parsed.Port(); p != "" {
		if cfg.port, err = strconv.Atoi(p); err != nil {
			return err
		}
	}
	if escaped := parsed.EscapedPath(); escaped != "" && escaped != "/" {
		cfg.path = escaped
	}
	if parsed.RawQuery != "" {
		cfg.path += "?" + parsed.RawQuery
	}
	cfg.url = raw
	return nil
}

// hostHeaderFor brackets IPv6 literals and omits the default ports.
func hostHeaderFor(host string, port int, plain bool) string {
	literal := host
	if strings.Contains(host, ":") {
		literal = "[" + host + "]"
	}
	if (plain && port == 80) || (!plain && port == 443) {
		return literal
	}
	return fmt.Sprintf("%s:%d", literal, port)
}

func (c *config) network() string {
	switch c.family {
	case "4":
		return "4"
	case "6":
		return "6"
	}
	return ""
}

func (c *config) tlsConfig() (*tls.Config, error) {
	if c.plain {
		return nil, nil
	}
	conf := &tls.Config{
		MinVersion: tls.VersionTLS12,
		ServerName: c.host,
		NextProtos: []string{"http/1.1"},
	}
	if c.sni != "" {
		conf.ServerName = c.sni
	}
	if c.insecure {
		conf.InsecureSkipVerify = true
	} else if c.ca != "" {
		pem, err := os.ReadFile(c.ca)
		if err != nil {
			return nil, err
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("no certificate found in %s", c.ca)
		}
		conf.RootCAs = pool
	}
	return conf, nil
}

// session is one local UDP peer, normally a single WireGuard instance.
type session struct {
	addr     *net.UDPAddr
	cfg      *config
	local    *net.UDPConn
	out      chan []byte
	done     chan struct{}
	lastSeen atomic64
	closeOne sync.Once
}

func (s *session) close() { s.closeOne.Do(func() { close(s.done) }) }

func (s *session) feed(pkt []byte) {
	s.lastSeen.set(time.Now().UnixNano())
	select {
	case s.out <- pkt:
	default:
		if s.cfg.verbose {
			log.Printf("upstream queue full, dropping %d bytes", len(pkt))
		}
	}
}

func (s *session) run() {
	backoff := s.cfg.backoffMin
	for {
		select {
		case <-s.done:
			return
		default:
		}
		conn, err := s.dial()
		if err != nil {
			log.Printf("connect to %s failed: %v", s.cfg.url, err)
			select {
			case <-time.After(backoff):
			case <-s.done:
				return
			}
			if backoff *= 2; backoff > s.cfg.backoffMax {
				backoff = s.cfg.backoffMax
			}
			continue
		}
		log.Printf("tunnel up for %s", s.addr)
		started := time.Now()
		s.pump(conn)
		conn.Close()
		log.Printf("tunnel down, reconnecting")
		// A tunnel that stayed up is healthy, so the next dial starts from the
		// shortest delay. One that died on arrival backs off like a refused
		// connection, or a server that accepts and drops spins the handshake.
		if time.Since(started) >= stableAfter {
			backoff = s.cfg.backoffMin
			continue
		}
		select {
		case <-time.After(backoff):
		case <-s.done:
			return
		}
		if backoff *= 2; backoff > s.cfg.backoffMax {
			backoff = s.cfg.backoffMax
		}
	}
}

func (s *session) dial() (*wsConn, error) {
	dialer := &net.Dialer{Timeout: s.cfg.connTimeout}
	network := "tcp" + s.cfg.network()
	address := net.JoinHostPort(s.cfg.host, strconv.Itoa(s.cfg.port))

	var conn net.Conn
	var err error
	tlsConf, err := s.cfg.tlsConfig()
	if err != nil {
		return nil, err
	}
	if tlsConf == nil {
		conn, err = dialer.Dial(network, address)
	} else {
		conn, err = tls.DialWithDialer(dialer, network, address, tlsConf)
	}
	if err != nil {
		return nil, err
	}
	if tcp, ok := underlyingTCP(conn); ok {
		_ = tcp.SetNoDelay(true)
	}

	headers := map[string]string{}
	if s.cfg.token != "" {
		headers["Authorization"] = "Bearer " + s.cfg.token
	}
	host := s.cfg.hostHeader
	if host == "" {
		host = hostHeaderFor(s.cfg.host, s.cfg.port, s.cfg.plain)
	}
	ws, err := wsDial(conn, host, s.cfg.path, headers)
	if err != nil {
		conn.Close()
		return nil, err
	}
	return ws, nil
}

func underlyingTCP(conn net.Conn) (*net.TCPConn, bool) {
	switch c := conn.(type) {
	case *net.TCPConn:
		return c, true
	case *tls.Conn:
		tcp, ok := c.NetConn().(*net.TCPConn)
		return tcp, ok
	}
	return nil, false
}

// pump runs until either direction fails.
func (s *session) pump(conn *wsConn) {
	stop := make(chan struct{})
	var once sync.Once
	fail := func() { once.Do(func() { close(stop) }) }

	go func() { // WebSocket -> local UDP
		defer fail()
		for {
			pkt, err := conn.Recv()
			if err != nil {
				if s.cfg.verbose {
					log.Printf("read: %v", err)
				}
				return
			}
			s.lastSeen.set(time.Now().UnixNano())
			if _, err := s.local.WriteToUDP(pkt, s.addr); err != nil {
				log.Printf("local write: %v", err)
				return
			}
		}
	}()

	ticker := time.NewTicker(s.cfg.pingInterval)
	defer ticker.Stop()
	for { // local UDP -> WebSocket
		select {
		case pkt := <-s.out:
			if err := conn.Send(pkt); err != nil {
				if s.cfg.verbose {
					log.Printf("write: %v", err)
				}
				fail()
				return
			}
		case <-ticker.C:
			if err := conn.Ping(); err != nil {
				fail()
				return
			}
		case <-stop:
			return
		case <-s.done:
			fail()
			return
		}
	}
}

// atomic64 keeps the last-seen timestamp without pulling in sync/atomic types
// that differ between Go versions.
type atomic64 struct {
	mu sync.Mutex
	v  int64
}

func (a *atomic64) set(v int64) { a.mu.Lock(); a.v = v; a.mu.Unlock() }
func (a *atomic64) get() int64  { a.mu.Lock(); defer a.mu.Unlock(); return a.v }

func run(cfg *config) error {
	// The local listener follows the address given to -listen; -family only
	// governs how this client reaches the server.
	addr, err := net.ResolveUDPAddr("udp", cfg.listen)
	if err != nil {
		return err
	}
	local, err := net.ListenUDP("udp", addr)
	if err != nil {
		return err
	}
	defer local.Close()

	scheme := "wss"
	if cfg.plain {
		scheme = "ws"
	}
	log.Printf("listening on udp %s -> %s://%s%s%s", local.LocalAddr(), scheme,
		hostHeaderFor(cfg.host, cfg.port, cfg.plain), cfg.path,
		map[bool]string{true: " (TLS verification disabled)"}[cfg.insecure])
	log.Printf("point your WireGuard client at  Endpoint = %s", local.LocalAddr())

	var mu sync.Mutex
	sessions := map[string]*session{}

	go func() { // reap idle sessions
		for range time.Tick(cfg.idleTimeout / 2) {
			cutoff := time.Now().Add(-cfg.idleTimeout).UnixNano()
			mu.Lock()
			for key, sess := range sessions {
				if sess.lastSeen.get() < cutoff {
					log.Printf("idle session %s closed", sess.addr)
					sess.close()
					delete(sessions, key)
				}
			}
			mu.Unlock()
		}
	}()

	signals := make(chan os.Signal, 1)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-signals
		log.Printf("shutting down")
		local.Close()
	}()

	buf := make([]byte, 65535)
	for {
		n, peer, err := local.ReadFromUDP(buf)
		if err != nil {
			if strings.Contains(err.Error(), "use of closed network connection") {
				return nil
			}
			return err
		}
		key := peer.String()
		mu.Lock()
		sess, ok := sessions[key]
		if !ok {
			log.Printf("new local peer %s", peer)
			sess = &session{
				addr: peer, cfg: cfg, local: local,
				out:  make(chan []byte, cfg.queueSize),
				done: make(chan struct{}),
			}
			sess.lastSeen.set(time.Now().UnixNano())
			sessions[key] = sess
			go sess.run()
		}
		mu.Unlock()
		pkt := make([]byte, n)
		copy(pkt, buf[:n])
		sess.feed(pkt)
	}
}
