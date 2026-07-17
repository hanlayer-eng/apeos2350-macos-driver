# Apeos 2350 NDA macOS Print Driver

Unofficial macOS print driver for **FUJIFILM Apeos 2350 NDA** and other HBPL-II based printers.

This driver enables any macOS application to print to HBPL-II printers that have no official macOS driver support. It uses a proxy daemon architecture to bypass macOS CUPS sandbox restrictions.

## Supported Printers

Any printer that uses the **HBPL-II** (Host Based Print Language II) protocol, including:

- FUJIFILM Apeos 2350 NDA
- Fuji Xerox DocuPrint CM205 / CM215 / CP205 / CP215
- Fuji Xerox DocuPrint M205 / M215
- Xerox Phaser 6000 / 6020 / 6500
- Other HBPL-II compatible models

## Features

- **Full macOS application support** — Print from Word, Excel, PowerPoint, Safari, Preview, WPS, etc.
- **Portrait & landscape** — Automatic orientation detection and rotation
- **Multi-page documents** — Unlimited page count
- **Auto duplex printing** — Long-edge (book) and short-edge (calendar) binding
- **Print options** — Paper size, resolution, input tray, copies
- **One-command install/uninstall**

## Architecture

```
macOS App → CUPS → apeos2350-meta filter → PDF + APEOS_META header
                                              ↓
                                    localhost:9101 (TCP)
                                              ↓
                              Proxy Daemon (LaunchDaemon)
                              ├── Parse APEOS_META header
                              ├── Detect orientation (portrait/landscape)
                              ├── Ghostscript: PDF → PBM (pbmraw)
                              ├── Split multi-page PBM
                              ├── Landscape: rotate PBM 90° CCW
                              ├── Duplex long-edge: rotate even pages 180°
                              ├── foo2hbpl2: PBM → HBPL-II
                              ├── Fix PJL duplex header
                              └── Merge pages into single PJL job
                                              ↓
                              192.168.x.x:9100 (Raw TCP) → Printer
```

### Why a proxy daemon?

macOS (since Mojave) runs CUPS filters inside a **sandbox** that prohibits `fork()`, `exec()`, and `posix_spawn()`. Traditional CUPS drivers that call external tools (like `gs` and `foo2hbpl2`) cannot work. This driver uses:

1. **A lightweight C CUPS filter** (`apeos2350-meta`) — runs inside the sandbox, only does string concatenation (no subprocess calls), attaches print options as a metadata header to the PDF stream.
2. **A proxy daemon** (`apeos2350-proxy.py`) — runs as a LaunchDaemon outside the sandbox, receives the PDF + metadata, performs the full rendering pipeline, and sends HBPL-II data to the printer.

## Prerequisites

### 1. Ghostscript

```bash
brew install ghostscript
```

Or download from [ghostscript.com](https://ghostscript.com/releases/gsdnld.html).

### 2. foo2hbpl2

Compile from the foo2zjs project:

```bash
git clone https://github.com/ValdikSS/foo2zjs.git
cd foo2zjs
make
sudo make install
```

> **China users** can use the Gitee mirror:
> ```bash
> git clone https://gitee.com/mirrors/foo2zjs.git
> ```

Verify installation:
```bash
which foo2hbpl2        # should be /usr/local/bin/foo2hbpl2
foo2hbpl2 -h           # should show help with -d duplex option
```

### 3. C Compiler

Xcode Command Line Tools (for compiling the CUPS filter):

```bash
xcode-select --install
```

## Installation

```bash
sudo bash install.sh [PRINTER_IP]
```

- `PRINTER_IP` defaults to `192.168.1.219` — replace with your printer's IP address.
- The printer must be reachable on **TCP port 9100** (raw socket printing).

### Verify installation

```bash
# Check printer queue
lpstat -p FUJIFILM_Apeos_2350

# Check proxy daemon
lsof -i :9101

# Print a test page
lp -d FUJIFILM_Apeos_2350 /path/to/test.pdf
```

## Usage

### From macOS applications

1. Open any application (Word, Safari, Preview, etc.)
2. File → Print (⌘P)
3. Select **FUJIFILM_Apeos_2350**
4. Choose options as needed:
   - **Duplex**: None / Long Edge / Short Edge
   - **Paper Size**: A4 / Letter / Legal / A5 / B5
   - **Resolution**: 300×300 dpi / 600×600 dpi
   - **Input Tray**: Auto / Tray1 / Tray2 / Manual
5. Click Print

### From command line

```bash
# Simplex (single-sided)
lp -d FUJIFILM_Apeos_2350 document.pdf

# Duplex - long edge (book binding)
lp -d FUJIFILM_Apeos_2350 -o Duplex=LongEdge document.pdf

# Duplex - short edge (calendar binding)
lp -d FUJIFILM_Apeos_2350 -o Duplex=ShortEdge document.pdf

# Multiple copies
lp -d FUJIFILM_Apeos_2350 -o copies=3 document.pdf
```

## Uninstallation

```bash
sudo bash uninstall.sh
```

## File Structure

```
Apeos2350_NDA_Driver/
├── install.sh                  # One-command installer
├── uninstall.sh                # One-command uninstaller
├── apeos2350-meta.c            # CUPS metadata filter source (C)
├── apeos2350-proxy.py          # Proxy daemon (Python 3)
├── Apeos2350_NDA.ppd           # CUPS PPD file
└── com.apeos2350.proxy.plist   # LaunchDaemon configuration
```

## How It Works

### Print flow

1. macOS application generates a PDF and sends it to CUPS
2. CUPS invokes the `apeos2350-meta` filter, which reads print options (duplex, paper size, resolution, copies, input tray) from CUPS arguments and prepends an `APEOS_META:` header to the PDF data
3. CUPS sends the combined data via socket to `localhost:9101` where the proxy daemon is listening
4. The proxy daemon:
   - Parses the `APEOS_META:` header to extract print options
   - Detects PDF orientation (portrait/landscape) from the MediaBox
   - Uses Ghostscript to render PDF → PBM (1-bit bitmap, pbmraw format)
   - Splits multi-page PBM into individual pages
   - For landscape pages: rotates PBM 90° CCW (HBPL-II only supports portrait geometry)
   - For long-edge duplex: rotates even pages 180°
   - Converts each PBM page to HBPL-II using `foo2hbpl2`
   - Fixes the PJL header (`DUPLEX=OFF` → `DUPLEX=ON` / `BINDING=SHORTEDGE`)
   - Merges all pages into a single PJL job (for duplex)
   - Sends the final HBPL-II data to the printer via TCP port 9100

### Key technical details

- **CUPS sandbox bypass**: The C filter (`apeos2350-meta`) only does string concatenation — no `fork`/`exec` calls, so it passes sandbox restrictions. The proxy daemon runs outside the sandbox as a LaunchDaemon.
- **PDF passthrough**: The PPD uses `cupsFilter` directives to pass PDF directly to the proxy, bypassing CUPS's built-in `cgpdftops` which produces empty PostScript on some macOS versions.
- **HBPL-II portrait-only**: HBPL-II only supports portrait page dimensions. Landscape PDFs are rendered with landscape geometry, then the PBM bitmap is rotated 90° to fit portrait dimensions.
- **Duplex PJL fix**: `foo2hbpl2` hardcodes `DUPLEX=OFF` in the PJL header regardless of the `-d` parameter. The proxy fixes this to `DUPLEX=ON` (long-edge) or `DUPLEX=ON` + `BINDING=SHORTEDGE` (short-edge).
- **Single PJL job merge**: For duplex printing, all pages must be in a single PJL job. The proxy merges individual page outputs by extracting page data (from `ESC PS<` markers) and combining them under one PJL job header.

## Troubleshooting

### Printer not printing

1. Check proxy daemon is running:
   ```bash
   lsof -i :9101
   ```
   If not running, restart:
   ```bash
   sudo launchctl unload /Library/LaunchDaemons/com.apeos2350.proxy.plist
   sudo launchctl load -w /Library/LaunchDaemons/com.apeos2350.proxy.plist
   ```

2. Check printer connectivity:
   ```bash
   nc -z <PRINTER_IP> 9100 && echo "Printer reachable" || echo "Printer unreachable"
   ```

3. Check proxy log:
   ```bash
   tail -30 /tmp/apeos2350-proxy.log
   ```

### Blank pages

- Ensure Ghostscript is installed at `/usr/local/bin/gs`
- Check the PPD is correctly installed: `lpoptions -p FUJIFILM_Apeos_2350 -l`

### Duplex not working

- Verify the printer has a hardware duplexer (Apeos 2350 NDA includes one by default)
- Check the proxy log shows `DUPLEX=ON` or `BINDING=SHORTEDGE`
- For long-edge: verify even pages are rotated 180° (check log for "rotated 180°")

### Printer error after duplex print

- Clear the error on the printer panel (press OK/Cancel button)
- Restart the printer if the error persists
- This can happen if invalid PJL commands are sent — ensure you're running the latest version

### Wrong page orientation

- Portrait documents should print correctly by default
- Landscape documents are auto-detected and rotated — check the proxy log for "landscape" detection

## Limitations

- Requires network connection to printer (TCP port 9100)
- Printer must support HBPL-II protocol
- `foo2hbpl2` and `gs` must be pre-installed
- Color printing not supported (HBPL-II driver is monochrome only)

## Credits

- **foo2zjs/foo2hbpl2** — HBPL-II encoder by Rick Richardson & contributors ([GitHub](https://github.com/ValdikSS/foo2zjs))
- **Ghostscript** — PDF/PS rendering engine
- **CUPS** — Apple's printing system

## License

GPL-2.0-or-later — same as foo2zjs.
