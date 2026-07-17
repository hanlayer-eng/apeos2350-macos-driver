#!/bin/bash
# ================================================================
# FUJIFILM Apeos 2350 NDA — macOS Print Driver Uninstaller
# ================================================================

set -e

PRINTER_NAME="FUJIFILM_Apeos_2350"
PLIST="/Library/LaunchDaemons/com.apeos2350.proxy.plist"
PROXY="/usr/local/bin/apeos2350-proxy.py"
META_FILTER="/usr/libexec/cups/filter/apeos2350-meta"

echo "=== FUJIFILM Apeos 2350 NDA Driver Uninstaller ==="

# Stop proxy daemon
echo "[1/3] Stopping proxy daemon..."
launchctl unload "$PLIST" 2>/dev/null || true

# Remove files
echo "[2/3] Removing files..."
rm -f "$PLIST"
rm -f "$PROXY"
rm -f "$META_FILTER"
rm -f /usr/libexec/cups/filter/foo2hbpl2-cups 2>/dev/null || true
rm -f /tmp/apeos2350-proxy.log 2>/dev/null || true

# Remove CUPS printer queue
echo "[3/3] Removing printer queue..."
lpadmin -x "$PRINTER_NAME" 2>/dev/null || true

echo ""
echo "=== Uninstallation Complete! ==="
echo "Printer '$PRINTER_NAME' has been removed."
echo ""
echo "Note: foo2hbpl2 and Ghostscript are NOT removed (may be used by other tools)."
echo "To also remove them: sudo rm /usr/local/bin/foo2hbpl2 /usr/local/bin/foo2hbpl2-wrapper"
