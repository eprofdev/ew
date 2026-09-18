#!/usr/bin/env bash
# Runs every test in the project. No root, no VPS, no WireGuard needed
# (the Go test needs Go and internet access to fetch wireguard-go once).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
rc=0

echo "=== unit tests (WebSocket framing) ==="
python3 -m unittest discover -s tests -v || rc=1

echo
echo "=== end-to-end tunnel over real TLS ==="
python3 tests/e2e.py || rc=1

if command -v go >/dev/null; then
  echo
  echo "=== real WireGuard session over the tunnel ==="
  (cd tests/wg_e2e && go run .) || rc=1
else
  echo
  echo "=== skipping the real WireGuard test (go not installed) ==="
fi

echo
[[ $rc -eq 0 ]] && echo "ALL TESTS PASSED" || echo "SOME TESTS FAILED"
exit $rc
