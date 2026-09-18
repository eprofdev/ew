#!/usr/bin/env bash
# Runs every test in the project. No root, no VPS and no kernel WireGuard
# module needed. The Go parts are skipped when Go is not installed; the
# IPv6 run skips itself on a host without an IPv6 stack.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
rc=0
declare -a RESULTS

# Exit code 77 means the test skipped itself (a missing IPv6 stack, say);
# that is neither a pass nor a failure and must not be reported as one.
run() {
  local name="$1"; shift
  echo
  echo "=== $name ==="
  "$@"
  local status=$?
  case $status in
    0)  RESULTS+=("PASS  $name") ;;
    77) RESULTS+=("SKIP  $name") ;;
    *)  RESULTS+=("FAIL  $name"); rc=1 ;;
  esac
}

run "unit tests (WebSocket framing, address parsing)" \
    python3 -m unittest discover -s tests -v

run "tunnel over real TLS, Python client, IPv4" \
    python3 tests/e2e.py

run "tunnel over real TLS, Python client, IPv6" \
    env WGWS_E2E_IPV6=1 python3 tests/e2e.py

if command -v go >/dev/null; then
  run "go vet + build of the client" \
      sh -c 'cd client-go && go vet ./... && go build -o /dev/null .'
  run "tunnel over real TLS, Go client, IPv4" \
      env WGWS_E2E_GO_CLIENT=1 python3 tests/e2e.py
  run "tunnel over real TLS, Go client, IPv6" \
      env WGWS_E2E_GO_CLIENT=1 WGWS_E2E_IPV6=1 python3 tests/e2e.py
  run "real WireGuard session, Python client" \
      sh -c 'cd tests/wg_e2e && go run .'
  run "real WireGuard session, Go client" \
      sh -c 'cd tests/wg_e2e && WGWS_GO_CLIENT=1 go run .'
else
  echo
  echo "=== skipping the Go tests (go is not installed) ==="
fi

echo
echo "================ summary ================"
printf '%s\n' "${RESULTS[@]}"
echo "========================================="
[[ $rc -eq 0 ]] && echo "ALL TESTS PASSED" || echo "SOME TESTS FAILED"
exit $rc
