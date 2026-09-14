#!/usr/bin/env bash
# Reproducible Linux amd64 bootstrap; no checkout of BuildBuddy is required.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION=v2.303.0
BINARY_SHA256=1ea34ea814bd4a21021f4b0698cad6d726cce7e1843c88bda921c16a6ab73fd3
# SHA256 above is the GitHub release API's sha256 digest, also independently
# checked against the downloaded release asset. Never execute an unchecked asset.
SKIP_BINARY=0
SKIP_BROWSER=0
WITH_DEPS=0
BINARY="${BUILDBUDDY_BINARY:-}"
usage() {
  cat <<'EOF'
Usage: scripts/setup.sh [--binary PATH | --skip-binary] [--skip-browser] [--with-deps]

Creates .venv/, generated/, and .tools/buildbuddy-enterprise. By default downloads
and SHA256-verifies BuildBuddy enterprise v2.303.0 for Linux amd64, generates
bindings from pinned source archives, and installs and launches Chromium.

  --binary PATH    Use an existing executable (also: BUILDBUDDY_BINARY=PATH).
                   Does not copy or overwrite it; pass the same path to the runner.
                   Protocols still use the pinned v2.303.0 source.
  --skip-binary    Skip executable download (supply one to the runner at runtime).
  --skip-browser   Skip Chromium install/launch, useful for protocol-only setup.
  --with-deps      Have Playwright install OS packages (requires root/sudo).

Requires Python 3.10+ with venv/pip, curl, sha256sum. On Debian/Ubuntu, e.g.:
  sudo apt-get update && sudo apt-get install -y python3-venv curl
For missing Chromium libraries, run setup with --with-deps, or separately:
  sudo .venv/bin/python -m playwright install-deps chromium
Normal setup and smoke tests do not require root. Browser downloads use
Playwright's standard per-user cache (honors PLAYWRIGHT_BROWSERS_PATH).
EOF
}
while (($#)); do
  case "$1" in
    --binary) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; BINARY="$2"; shift 2 ;;
    --skip-binary) SKIP_BINARY=1; shift ;;
    --skip-browser) SKIP_BROWSER=1; shift ;;
    --with-deps) WITH_DEPS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
for tool in "${PYTHON:-python3}" curl sha256sum; do
  command -v "$tool" >/dev/null || { echo "Missing prerequisite: $tool" >&2; exit 1; }
done
"${PYTHON:-python3}" -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  "${PYTHON:-python3}" -m venv "$ROOT/.venv" || {
    echo 'Failed creating venv. Install python3-venv (may require sudo), then retry.' >&2
    exit 1
  }
fi
PY="$ROOT/.venv/bin/python"
"$PY" -m pip install --disable-pip-version-check --requirement "$ROOT/requirements.txt"
"$PY" -m pip check
mkdir -p "$ROOT/.tools/downloads"
if [[ -n "$BINARY" ]]; then
  [[ -f "$BINARY" && -x "$BINARY" ]] || { echo "Not an executable file: $BINARY" >&2; exit 1; }
  BINARY="$("$PY" -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$BINARY")"
  echo "Using supplied binary (not release-verified): $BINARY"
elif (( ! SKIP_BINARY )); then
  [[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || {
    echo 'Default binary is Linux amd64 only. Supply --binary PATH or --skip-binary.' >&2
    exit 1
  }
  BINARY="$ROOT/.tools/buildbuddy-enterprise"
  ASSET="$ROOT/.tools/downloads/buildbuddy-enterprise-$VERSION-linux-amd64"
  if [[ ! -f "$ASSET" ]]; then
    curl --fail --location --retry 3 --connect-timeout 30 \
      "https://github.com/buildbuddy-io/buildbuddy/releases/download/$VERSION/buildbuddy-enterprise-linux-amd64" \
      --output "$ASSET.partial"
    printf '%s  %s\n' "$BINARY_SHA256" "$ASSET.partial" | sha256sum --check -
    mv -- "$ASSET.partial" "$ASSET"
  fi
  printf '%s  %s\n' "$BINARY_SHA256" "$ASSET" | sha256sum --check -
  # Keep a versioned cache; install only after verification, including on reruns.
  # Avoid rewriting a running executable on repeated setup invocations.
  if [[ ! -f "$BINARY" ]] || [[ "$(sha256sum "$BINARY" | cut -d ' ' -f 1)" != "$BINARY_SHA256" ]]; then
    install -m 0755 "$ASSET" "$BINARY.partial"
    mv -- "$BINARY.partial" "$BINARY"
  fi
  printf '%s\n' "$VERSION $BINARY_SHA256" > "$ROOT/.tools/buildbuddy-enterprise.version"
fi
"$PY" "$ROOT/scripts/generate_protos.py"
if (( ! SKIP_BROWSER )); then
  if (( WITH_DEPS )); then
    "$PY" -m playwright install --with-deps chromium
  else
    "$PY" -m playwright install chromium
  fi
  "$PY" - <<'PY'
from playwright.sync_api import sync_playwright
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<title>bootstrap-ok</title>')
        assert page.title() == 'bootstrap-ok'
        print(f'Chromium launch verified: {browser.version}')
        browser.close()
except Exception:
    print('Chromium launch failed. For missing system libraries, rerun with --with-deps,')
    print('or run: sudo .venv/bin/python -m playwright install-deps chromium')
    raise
PY
fi
printf '\nSetup complete. Python: %s\nBindings: %s\n' "$PY" "$ROOT/generated"
if [[ -n "$BINARY" ]]; then printf 'Binary: %s\n' "$BINARY"; fi
