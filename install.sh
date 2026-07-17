#!/bin/bash
# ================================================================
# FUJIFILM Apeos 2350 NDA — macOS Print Driver Installer
# ================================================================
# This script installs the complete print driver package:
#   1. Checks prerequisites (Ghostscript, foo2hbpl2, C compiler)
#   2. Compiles and installs CUPS metadata filter (apeos2350-meta)
#   3. Installs proxy daemon to /usr/local/bin/
#   4. Installs LaunchDaemon for auto-start
#   5. Configures CUPS printer queue
#   6. Starts proxy daemon
#
# Requires: sudo
# Printer IP default: 192.168.1.219
# ================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PRINTER_IP="${1:-192.168.1.219}"
PRINTER_PORT=9100
PROXY_PORT=9101
PRINTER_NAME="FUJIFILM_Apeos_2350"

echo "=== FUJIFILM Apeos 2350 NDA Driver Installer ==="
echo "Printer: $PRINTER_IP:$PRINTER_PORT"
echo ""

# ── Step 1: Check Prerequisites ────────────────────────────────

echo "[1/7] Checking prerequisites..."

if ! command -v /usr/local/bin/gs &> /dev/null; then
    echo "  ERROR: Ghostscript not found at /usr/local/bin/gs"
    echo "  Please install Ghostscript first:"
    echo "    brew install ghostscript"
    echo "  or download from: https://ghostscript.com/releases/gsdnld.html"
    exit 1
fi
echo "  Ghostscript: OK ($( /usr/local/bin/gs --version 2>/dev/null || echo 'unknown' ))"

if ! command -v /usr/local/bin/foo2hbpl2 &> /dev/null; then
    echo "  ERROR: foo2hbpl2 not found at /usr/local/bin/foo2hbpl2"
    echo "  Please compile and install foo2hbpl2 first:"
    echo "    git clone https://github.com/ValdikSS/foo2zjs.git"
    echo "    cd foo2zjs && make && sudo make install"
    echo "  Or from Gitee mirror (China):"
    echo "    git clone https://gitee.com/mirrors/foo2zjs.git"
    exit 1
fi
echo "  foo2hbpl2: OK"

if ! command -v cc &> /dev/null; then
    echo "  ERROR: C compiler (cc) not found"
    echo "  Please install Xcode command-line tools:"
    echo "    xcode-select --install"
    exit 1
fi
echo "  C compiler: OK ($( cc --version 2>&1 | head -1 ))"

# ── Step 2: Compile and Install CUPS Metadata Filter ───────────

echo "[2/7] Compiling CUPS metadata filter..."

cc -O2 -o "$SCRIPT_DIR/apeos2350-meta" "$SCRIPT_DIR/apeos2350-meta.c"
echo "  Compiled: apeos2350-meta"

cp "$SCRIPT_DIR/apeos2350-meta" /usr/libexec/cups/filter/apeos2350-meta
chmod 755 /usr/libexec/cups/filter/apeos2350-meta
chown root:wheel /usr/libexec/cups/filter/apeos2350-meta
echo "  Installed: /usr/libexec/cups/filter/apeos2350-meta"

# ── Step 3: Install Proxy Daemon ───────────────────────────────

echo "[3/7] Installing proxy daemon..."

cp "$SCRIPT_DIR/apeos2350-proxy.py" /usr/local/bin/apeos2350-proxy.py
chmod 755 /usr/local/bin/apeos2350-proxy.py
echo "  Proxy script: /usr/local/bin/apeos2350-proxy.py"

# ── Step 4: Install LaunchDaemon ───────────────────────────────

echo "[4/7] Installing LaunchDaemon..."

# Update plist with correct printer IP
PLIST_SRC="$SCRIPT_DIR/com.apeos2350.proxy.plist"
PLIST_DST="/Library/LaunchDaemons/com.apeos2350.proxy.plist"

# Unload old daemon if present
launchctl unload "$PLIST_DST" 2>/dev/null || true

# Replace placeholder with actual printer IP
sed "s/__PRINTER_IP__/$PRINTER_IP/g" "$PLIST_SRC" > "$PLIST_DST"
chmod 644 "$PLIST_DST"
chown root:wheel "$PLIST_DST"
echo "  LaunchDaemon: $PLIST_DST (printer: $PRINTER_IP)"

# ── Step 5: Remove obsolete CUPS filter (if present) ───────────

echo "[5/7] Cleaning up obsolete files..."

rm -f /usr/libexec/cups/filter/foo2hbpl2-cups 2>/dev/null || true
echo "  Old CUPS filter removed (if existed)"

# ── Step 6: Configure CUPS Printer ─────────────────────────────

echo "[6/7] Configuring CUPS printer..."

# Remove old printer queue if it exists
lpadmin -x "$PRINTER_NAME" 2>/dev/null || true

# Add new printer queue pointing to local proxy
lpadmin -p "$PRINTER_NAME" \
    -v "socket://127.0.0.1:$PROXY_PORT" \
    -P "$SCRIPT_DIR/Apeos2350_NDA.ppd" \
    -o auth-info-required=none \
    -o printer-is-shared=false \
    -E

echo "  Printer queue: $PRINTER_NAME -> socket://127.0.0.1:$PROXY_PORT"

# ── Step 7: Start Proxy Daemon ─────────────────────────────────

echo "[7/7] Starting proxy daemon..."

launchctl load -w "$PLIST_DST"
sleep 2

# Verify proxy is running
if python3 -c "import socket; s=socket.socket(); s.settimeout(3); s.connect(('127.0.0.1',$PROXY_PORT)); s.close()" 2>/dev/null; then
    echo "  Proxy daemon: RUNNING on 127.0.0.1:$PROXY_PORT"
else
    echo "  WARNING: Proxy daemon not responding yet"
    echo "  Check: tail /tmp/apeos2350-proxy.log"
fi

# ── Done ────────────────────────────────────────────────────────

echo ""
echo "=== Installation Complete! ==="
echo ""
echo "Printer: $PRINTER_NAME"
echo "Status:  $( lpstat -p "$PRINTER_NAME" 2>/dev/null | head -1 || echo 'unknown' )"
echo "Proxy:   127.0.0.1:$PROXY_PORT -> $PRINTER_IP:$PRINTER_PORT"
echo "Log:     /tmp/apeos2350-proxy.log"
echo ""
echo "Supported options:"
echo "  - Duplex (None/Long Edge/Short Edge) — auto double-sided printing"
echo "  - PageSize (A4/Letter/Legal/A5/B5)"
echo "  - Resolution (300x300dpi/600x600dpi)"
echo "  - InputSlot (Auto/Tray1/Tray2/Manual)"
echo "  - Copies (1-50)"
echo ""
echo "Usage:"
echo "  Print from any macOS app -> select '$PRINTER_NAME'"
echo "  Select 'Long Edge' duplex in print dialog for double-sided"
echo "  Test:  lp -d $PRINTER_NAME -o Duplex=LongEdge -o PageSize=A4 /path/to/file.pdf"
echo ""
echo "To uninstall:"
echo "  sudo bash $SCRIPT_DIR/uninstall.sh"
