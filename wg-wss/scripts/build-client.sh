#!/usr/bin/env bash
# Cross-compile the Go client to a single static binary per platform.
#   ./scripts/build-client.sh            # every target below
#   ./scripts/build-client.sh windows/amd64 android/arm64
# No modules are fetched: the client only uses the Go standard library.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../client-go"
OUT="${OUT:-../dist}"
mkdir -p "$OUT"

TARGETS=("$@")
if [[ ${#TARGETS[@]} -eq 0 ]]; then
  TARGETS=(
    linux/amd64 linux/arm64 linux/arm
    windows/amd64 windows/arm64
    darwin/amd64 darwin/arm64
    android/arm64
    freebsd/amd64
  )
fi

VERSION="$(grep -oP 'version = "\K[^"]+' main.go)"
for target in "${TARGETS[@]}"; do
  os="${target%%/*}"; arch="${target##*/}"
  name="wgws-client-$os-$arch"
  [[ $os == windows ]] && name="$name.exe"
  # -s -w strips symbols; CGO off keeps the binary static and portable.
  CGO_ENABLED=0 GOOS="$os" GOARCH="$arch" \
    go build -trimpath -ldflags "-s -w -X main.version=$VERSION" -o "$OUT/$name" .
  printf '  %-34s %s\n' "$name" "$(du -h "$OUT/$name" | cut -f1)"
done

echo
echo "binaries in $(cd "$OUT" && pwd)"
