#!/usr/bin/env bash
# Setup only: download the self-contained Bazel release (includes its JDK).
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION=8.4.2
# Published at releases.bazel.build/8.4.2/release/bazel-8.4.2-linux-x86_64.sha256.
SHA256=4dc8e99dfa802e252dac176d08201fd15c542ae78c448c8a89974b6f387c282c
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || {
  echo 'The pinned Bazel smoke executable requires Linux x86_64.' >&2; exit 1;
}
for tool in curl sha256sum; do command -v "$tool" >/dev/null; done
mkdir -p "$ROOT/.tools/downloads"
ASSET="$ROOT/.tools/downloads/bazel-$VERSION-linux-x86_64"
BASE="https://releases.bazel.build/$VERSION/release/bazel-$VERSION-linux-x86_64"
if [[ ! -f "$ASSET" ]]; then
  # Cross-check the published checksum with our committed pin before download.
  PUBLISHED="$(curl --fail --location --retry 3 --connect-timeout 30 "$BASE.sha256")"
  [[ "${PUBLISHED%% *}" == "$SHA256" ]] || { echo 'Published Bazel checksum differs from pin' >&2; exit 1; }
  curl --fail --location --retry 3 --connect-timeout 30 "$BASE" --output "$ASSET.partial"
  printf '%s  %s\n' "$SHA256" "$ASSET.partial" | sha256sum --check -
  mv -- "$ASSET.partial" "$ASSET"
fi
printf '%s  %s\n' "$SHA256" "$ASSET" | sha256sum --check -
BIN="$ROOT/.tools/bazel-$VERSION"
if [[ ! -f "$BIN" ]] || [[ "$(sha256sum "$BIN" | cut -d ' ' -f 1)" != "$SHA256" ]]; then
  install -m 0755 "$ASSET" "$BIN.partial"
  mv -- "$BIN.partial" "$BIN"
fi
# Extract the embedded JDK in setup, not in the timed smoke run. --batch prevents
# a persistent Bazel server. Runtime still uses fresh output_user_root values.
"$BIN" --batch --output_user_root="$ROOT/.tools/bazel-setup-root" \
  --install_base="$ROOT/.tools/bazel-install-$VERSION" version
printf '\nBazel ready: %s\n' "$BIN"
