// Client side of RFC 6455, standard library only, so the binary
// cross-compiles anywhere Go runs without fetching a single module.
package main

import (
	"bufio"
	"crypto/rand"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"strings"
	"sync"
)

const (
	opCont  = 0x0
	opText  = 0x1
	opBin   = 0x2
	opClose = 0x8
	opPing  = 0x9
	opPong  = 0xA

	wsGUID     = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
	maxMessage = 128 * 1024
)

// wsConn is a WebSocket connection with independent read and write locks, so
// a keepalive ping never interleaves with a packet being written.
type wsConn struct {
	conn net.Conn
	br   *bufio.Reader
	mu   sync.Mutex
}

func acceptKey(key string) string {
	sum := sha1.Sum([]byte(key + wsGUID))
	return base64.StdEncoding.EncodeToString(sum[:])
}

// wsDial performs the opening handshake over an already-connected net.Conn.
func wsDial(conn net.Conn, hostHeader, path string, headers map[string]string) (*wsConn, error) {
	nonce := make([]byte, 16)
	if _, err := rand.Read(nonce); err != nil {
		return nil, err
	}
	key := base64.StdEncoding.EncodeToString(nonce)

	var req strings.Builder
	fmt.Fprintf(&req, "GET %s HTTP/1.1\r\n", path)
	fmt.Fprintf(&req, "Host: %s\r\n", hostHeader)
	req.WriteString("Upgrade: websocket\r\n")
	req.WriteString("Connection: Upgrade\r\n")
	fmt.Fprintf(&req, "Sec-WebSocket-Key: %s\r\n", key)
	req.WriteString("Sec-WebSocket-Version: 13\r\n")
	req.WriteString("User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 " +
		"(KHTML, like Gecko) Chrome/125.0 Safari/537.36\r\n")
	for name, value := range headers {
		fmt.Fprintf(&req, "%s: %s\r\n", name, value)
	}
	req.WriteString("\r\n")
	if _, err := io.WriteString(conn, req.String()); err != nil {
		return nil, err
	}

	br := bufio.NewReaderSize(conn, 4096)
	status, err := br.ReadString('\n')
	if err != nil {
		return nil, err
	}
	if !strings.Contains(status, " 101") {
		return nil, fmt.Errorf("handshake rejected: %s", strings.TrimSpace(status))
	}
	var gotAccept string
	for {
		line, err := br.ReadString('\n')
		if err != nil {
			return nil, err
		}
		line = strings.TrimRight(line, "\r\n")
		if line == "" {
			break
		}
		name, value, _ := strings.Cut(line, ":")
		if strings.EqualFold(strings.TrimSpace(name), "Sec-WebSocket-Accept") {
			gotAccept = strings.TrimSpace(value)
		}
	}
	if gotAccept != acceptKey(key) {
		return nil, fmt.Errorf("bad Sec-WebSocket-Accept")
	}
	return &wsConn{conn: conn, br: br}, nil
}

func (c *wsConn) writeFrame(opcode byte, payload []byte) error {
	n := len(payload)
	head := make([]byte, 0, 14)
	head = append(head, 0x80|opcode)
	switch {
	case n < 126:
		head = append(head, 0x80|byte(n))
	case n < 65536:
		head = append(head, 0x80|126, byte(n>>8), byte(n))
	default:
		head = append(head, 0x80|127)
		var ext [8]byte
		binary.BigEndian.PutUint64(ext[:], uint64(n))
		head = append(head, ext[:]...)
	}
	var mask [4]byte
	if _, err := rand.Read(mask[:]); err != nil {
		return err
	}
	head = append(head, mask[:]...)

	// Clients must mask; do it into a scratch buffer, never in place.
	body := make([]byte, n)
	for i := 0; i < n; i++ {
		body[i] = payload[i] ^ mask[i&3]
	}

	c.mu.Lock()
	defer c.mu.Unlock()
	if _, err := c.conn.Write(append(head, body...)); err != nil {
		return err
	}
	return nil
}

func (c *wsConn) Send(data []byte) error { return c.writeFrame(opBin, data) }
func (c *wsConn) Ping() error            { return c.writeFrame(opPing, nil) }

// Recv returns the next binary message, answering control frames itself.
func (c *wsConn) Recv() ([]byte, error) {
	var buf []byte
	for {
		var head [2]byte
		if _, err := io.ReadFull(c.br, head[:]); err != nil {
			return nil, err
		}
		if head[0]&0x70 != 0 {
			return nil, fmt.Errorf("reserved bits set")
		}
		fin := head[0]&0x80 != 0
		opcode := head[0] & 0x0F
		masked := head[1]&0x80 != 0
		n := int(head[1] & 0x7F)
		switch n {
		case 126:
			var ext [2]byte
			if _, err := io.ReadFull(c.br, ext[:]); err != nil {
				return nil, err
			}
			n = int(binary.BigEndian.Uint16(ext[:]))
		case 127:
			var ext [8]byte
			if _, err := io.ReadFull(c.br, ext[:]); err != nil {
				return nil, err
			}
			size := binary.BigEndian.Uint64(ext[:])
			if size > maxMessage {
				return nil, fmt.Errorf("message of %d bytes exceeds limit", size)
			}
			n = int(size)
		}
		if n > maxMessage {
			return nil, fmt.Errorf("message of %d bytes exceeds limit", n)
		}
		var mask [4]byte
		if masked {
			if _, err := io.ReadFull(c.br, mask[:]); err != nil {
				return nil, err
			}
		}
		payload := make([]byte, n)
		if _, err := io.ReadFull(c.br, payload); err != nil {
			return nil, err
		}
		if masked {
			for i := range payload {
				payload[i] ^= mask[i&3]
			}
		}

		switch opcode {
		case opClose:
			return nil, io.EOF
		case opPing:
			if err := c.writeFrame(opPong, payload); err != nil {
				return nil, err
			}
			continue
		case opPong:
			continue
		case opBin, opText, opCont:
			buf = append(buf, payload...)
			if len(buf) > maxMessage {
				return nil, fmt.Errorf("fragmented message exceeds limit")
			}
			if fin {
				return buf, nil
			}
		default:
			return nil, fmt.Errorf("unknown opcode 0x%x", opcode)
		}
	}
}

func (c *wsConn) Close() error {
	_ = c.writeFrame(opClose, []byte{0x03, 0xE8}) // 1000, normal closure
	return c.conn.Close()
}
