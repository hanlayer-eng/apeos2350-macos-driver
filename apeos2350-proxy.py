#!/usr/bin/env python3
"""
apeos2350-proxy.py — Print proxy daemon for FUJIFILM Apeos 2350 NDA

Runs as a LaunchDaemon OUTSIDE the CUPS sandbox.
Listens on localhost:9101, receives PDF or PostScript from CUPS
(with optional APEOS_META header carrying print options),
converts to HBPL-II via gs + foo2hbpl2, and sends to
the real printer via raw TCP (Port 9100).

HBPL-II always uses PORTRAIT page geometry.  Landscape PDFs are rendered
in their natural landscape orientation and the bitmap is rotated 90° CCW
so the content fills the portrait page.

Multi-page documents are split into individual PBM pages, each converted
to HBPL-II separately.

Duplex printing: foo2hbpl2 hardcodes DUPLEX=OFF in its PJL header, so
the proxy fixes the PJL header to DUPLEX=ON and merges all pages into
a single PJL job so the printer's hardware duplexer is engaged.

Usage:
  python3 apeos2350-proxy.py [--port PORT] [--printer HOST:PORT]
"""

import socket
import subprocess
import sys
import os
import tempfile
import argparse
import signal
import logging
import re

# ── Configuration ──────────────────────────────────────────────

DEFAULT_PROXY_PORT = 9101
DEFAULT_PRINTER_HOST = "192.168.1.219"
DEFAULT_PRINTER_PORT = 9100
GS_PATH = "/usr/local/bin/gs"
FOO2_PATH = "/usr/local/bin/foo2hbpl2"

# Paper sizes at 600x600dpi — ALWAYS portrait geometry (xpix × ypix)
PAPERS = {
    "a4":     {"xpix": 4960, "ypix": 7016, "code": 1},
    "letter": {"xpix": 5100, "ypix": 6600, "code": 4},
    "legal":  {"xpix": 5100, "ypix": 8400, "code": 7},
    "a5":     {"xpix": 3496, "ypix": 4960, "code": 3},
    "b5":     {"xpix": 4298, "ypix": 6070, "code": 2},
}

DEFAULT_PAPER = "a4"
DEFAULT_RESOLUTION = "600x600"
DEFAULT_DUPLEX = 1       # 1=off, 2=longedge, 3=shortedge
DEFAULT_SOURCE = 7       # 1=tray1, 2=tray2, 4=manual, 7=auto

# ── Logging ────────────────────────────────────────────────────

log_handlers = [logging.StreamHandler(sys.stdout)]
try:
    log_handlers.append(logging.FileHandler('/tmp/apeos2350-proxy.log', mode='a'))
except (PermissionError, OSError):
    pass  # Non-critical: log to stdout only

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [apeos2350-proxy] %(message)s',
    handlers=log_handlers
)
log = logging.getLogger(__name__)


# ── Metadata Header Parsing ────────────────────────────────────

def parse_meta_header(data):
    """Parse APEOS_META header prepended by the CUPS filter.

    The header is one line in the format:
      APEOS_META:d=2;paper=a4;res=600x600;n=1;source=7\n

    Returns (options_dict, remaining_data) where options_dict contains
    parsed key-value pairs and remaining_data is the raw PDF/PS data
    after the header.  If no header is found, returns (None, data).
    """
    HEADER_PREFIX = b"APEOS_META:"
    if not data.startswith(HEADER_PREFIX):
        return None, data

    # Find the newline that terminates the header
    nl_pos = data.find(b'\n', len(HEADER_PREFIX))
    if nl_pos < 0:
        log.warning("APEOS_META header has no terminating newline, "
                     "treating entire data as headerless")
        return None, data

    header_str = data[len(HEADER_PREFIX):nl_pos].decode('ascii', errors='replace')
    payload = data[nl_pos + 1:]   # skip the newline byte

    # Parse "key=value;key=value;..."
    options = {}
    for pair in header_str.split(';'):
        if '=' not in pair:
            continue
        key, val = pair.split('=', 1)
        options[key.strip()] = val.strip()

    log.info(f"Parsed APEOS_META header: {options}")
    return options, payload


# ── Format Detection ──────────────────────────────────────────

def detect_format(data):
    """Detect whether incoming data is PDF or PostScript."""
    if data.startswith(b'%PDF'):
        return 'pdf'
    if data.startswith(b'%!PS') or data.startswith(b'%!PS-Adobe'):
        return 'ps'
    if b'%PDF' in data[:1024]:
        return 'pdf'
    return 'ps'


def detect_pdf_orientation(data):
    """Detect orientation of the first page in a PDF.

    Returns 'portrait' or 'landscape'.
    Checks MediaBox dimensions and Rotate attribute of the first page.
    """
    if not data.startswith(b'%PDF'):
        return 'portrait'

    text = data.decode('latin-1', errors='replace')

    # Find first Page object
    page_match = re.search(r'/Type\s*/Page[^s]', text)
    if page_match:
        page_text = text[page_match.start():page_match.start()+500]

        # Check Rotate attribute (90 or 270 = landscape)
        rotate_match = re.search(r'/Rotate\s+(\d+)', page_text)
        if rotate_match:
            rotate_val = int(rotate_match.group(1))
            if rotate_val in (90, 270):
                log.info(f"PDF Rotate={rotate_val} -> landscape")
                return 'landscape'

        # Check MediaBox dimensions
        media_match = re.search(
            r'/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\]',
            page_text)
        if media_match:
            width = float(media_match.group(3)) - float(media_match.group(1))
            height = float(media_match.group(4)) - float(media_match.group(2))
            if width > height:
                log.info(f"PDF MediaBox landscape (w={width}, h={height})")
                return 'landscape'
            log.info(f"PDF MediaBox portrait (w={width}, h={height})")
            return 'portrait'

    # Fallback: scan entire PDF for MediaBox patterns
    media_matches = re.findall(
        r'/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\]',
        text[:50000])
    for m in media_matches:
        width = float(m[2]) - float(m[0])
        height = float(m[3]) - float(m[1])
        if width > height:
            log.info(f"PDF landscape detected via fallback MediaBox (w={width}, h={height})")
            return 'landscape'

    # Check Rotate in any Page object
    rotate_matches = re.findall(r'/Rotate\s+(\d+)', text[:50000])
    for r_val in rotate_matches:
        if int(r_val) in (90, 270):
            log.info(f"PDF landscape detected via Rotate={r_val}")
            return 'landscape'

    log.info("PDF orientation: portrait (default)")
    return 'portrait'


# ── PBM Manipulation ──────────────────────────────────────────

def split_pbm_pages(pbmraw_data):
    """Split a multi-page P4 pbmraw stream into individual page buffers.

    gs with -sOutputFile=- concatenates all pages into a single stream.
    Each page starts with "P4\\n[comments]\\n<width> <height>\\n<binary data>".
    foo2hbpl2 only processes one page, so we need to split and convert each
    page separately.

    Returns a list of byte buffers, one per page.
    """
    pages = []
    pos = 0

    while pos < len(pbmraw_data) - 2:
        if pbmraw_data[pos:pos+2] != b'P4':
            break

        p = pos + 3  # skip "P4\\n"

        # Skip comment lines
        while p < len(pbmraw_data) and pbmraw_data[p] == ord('#'):
            while p < len(pbmraw_data) and pbmraw_data[p] != ord('\n'):
                p += 1
            p += 1

        # Read width and height
        start = p
        while p < len(pbmraw_data) and pbmraw_data[p] != ord('\n'):
            p += 1
        dim_line = pbmraw_data[start:p].decode('ascii')
        parts = dim_line.split()
        width = int(parts[0])
        height = int(parts[1])
        p += 1  # skip newline after dimensions

        # Calculate binary data size
        row_bytes = (width + 7) // 8
        page_data_size = row_bytes * height

        # Extract this page
        page_end = p + page_data_size
        if page_end > len(pbmraw_data):
            log.warning(f"Page data truncated: expected {page_data_size}B, "
                        f"have {len(pbmraw_data) - p}B")
            page_end = len(pbmraw_data)

        pages.append(pbmraw_data[pos:page_end])
        pos = page_end

    return pages


def rotate_pbm90_ccw(pbmraw_data):
    """Rotate a P4 (pbmraw) binary bitmap 90 degrees counter-clockwise.

    Input:  P4 PBM with width W, height H
    Output: P4 PBM with width H, height W

    Rotation formula: output[r][c] = input[W-1-r][c]
    """
    pos = 0
    if pbmraw_data[0:2] != b'P4':
        raise ValueError("Not P4 PBM")
    pos = 3  # skip 'P4\\n'

    # Skip comments
    while pbmraw_data[pos] == ord('#'):
        while pbmraw_data[pos] != ord('\n'):
            pos += 1
        pos += 1

    # Read dimensions
    start = pos
    while pbmraw_data[pos] != ord('\n'):
        pos += 1
    dim_line = pbmraw_data[start:pos].decode('ascii')
    parts = dim_line.split()
    width = int(parts[0])
    height = int(parts[1])
    pos += 1  # skip newline

    header_len = pos
    input_row_bytes = (width + 7) // 8
    output_width = height
    output_height = width
    output_row_bytes = (output_width + 7) // 8

    output = bytearray(output_row_bytes * output_height)

    for r in range(output_height):
        for c in range(output_width):
            in_x = width - 1 - r
            in_y = c
            in_byte_idx = in_y * input_row_bytes + (in_x // 8)
            in_bit = 7 - (in_x % 8)
            in_val = (pbmraw_data[header_len + in_byte_idx] >> in_bit) & 1

            if in_val:
                out_byte_idx = r * output_row_bytes + (c // 8)
                out_bit = 7 - (c % 8)
                output[out_byte_idx] |= (1 << out_bit)

    header = f"P4\n{output_width} {output_height}\n".encode()
    return header + bytes(output)


def rotate_pbm180(pbmraw_data):
    """Rotate a P4 (pbmraw) binary bitmap 180 degrees.

    Input:  P4 PBM with width W, height H
    Output: P4 PBM with width W, height H (same dimensions, rotated 180°)

    For duplex printing: the back page must be rotated 180° so that
    when the paper is flipped along the long edge, the content reads
    correctly (like turning a book page).

    Uses a fast byte-level approach when width is a multiple of 8
    (true for all standard paper sizes at 600dpi).
    """
    pos = 0
    if pbmraw_data[0:2] != b'P4':
        raise ValueError("Not P4 PBM")
    pos = 3  # skip 'P4\n'

    # Skip comments
    while pbmraw_data[pos] == ord('#'):
        while pbmraw_data[pos] != ord('\n'):
            pos += 1
        pos += 1

    # Read dimensions
    start = pos
    while pbmraw_data[pos] != ord('\n'):
        pos += 1
    dim_line = pbmraw_data[start:pos].decode('ascii')
    parts = dim_line.split()
    width = int(parts[0])
    height = int(parts[1])
    pos += 1  # skip newline

    header = f"P4\n{width} {height}\n".encode()
    pixel_data = pbmraw_data[pos:]
    row_bytes = (width + 7) // 8

    # Bit reversal lookup table (reverse bit order in a byte)
    _BIT_REV = bytes([
        0x00, 0x80, 0x40, 0xC0, 0x20, 0xA0, 0x60, 0xE0,
        0x10, 0x90, 0x50, 0xD0, 0x30, 0xB0, 0x70, 0xF0,
        0x08, 0x88, 0x48, 0xC8, 0x28, 0xA8, 0x68, 0xE8,
        0x18, 0x98, 0x58, 0xD8, 0x38, 0xB8, 0x78, 0xF8,
        0x04, 0x84, 0x44, 0xC4, 0x24, 0xA4, 0x64, 0xE4,
        0x14, 0x94, 0x54, 0xD4, 0x34, 0xB4, 0x74, 0xF4,
        0x0C, 0x8C, 0x4C, 0xCC, 0x2C, 0xAC, 0x6C, 0xEC,
        0x1C, 0x9C, 0x5C, 0xDC, 0x3C, 0xBC, 0x7C, 0xFC,
        0x02, 0x82, 0x42, 0xC2, 0x22, 0xA2, 0x62, 0xE2,
        0x12, 0x92, 0x52, 0xD2, 0x32, 0xB2, 0x72, 0xF2,
        0x0A, 0x8A, 0x4A, 0xCA, 0x2A, 0xAA, 0x6A, 0xEA,
        0x1A, 0x9A, 0x5A, 0xDA, 0x3A, 0xBA, 0x7A, 0xFA,
        0x06, 0x86, 0x46, 0xC6, 0x26, 0xA6, 0x66, 0xE6,
        0x16, 0x96, 0x56, 0xD6, 0x36, 0xB6, 0x76, 0xF6,
        0x0E, 0x8E, 0x4E, 0xCE, 0x2E, 0xAE, 0x6E, 0xEE,
        0x1E, 0x9E, 0x5E, 0xDE, 0x3E, 0xBE, 0x7E, 0xFE,
        0x01, 0x81, 0x41, 0xC1, 0x21, 0xA1, 0x61, 0xE1,
        0x11, 0x91, 0x51, 0xD1, 0x31, 0xB1, 0x71, 0xF1,
        0x09, 0x89, 0x49, 0xC9, 0x29, 0xA9, 0x69, 0xE9,
        0x19, 0x99, 0x59, 0xD9, 0x39, 0xB9, 0x79, 0xF9,
        0x05, 0x85, 0x45, 0xC5, 0x25, 0xA5, 0x65, 0xE5,
        0x15, 0x95, 0x55, 0xD5, 0x35, 0xB5, 0x75, 0xF5,
        0x0D, 0x8D, 0x4D, 0xCD, 0x2D, 0xAD, 0x6D, 0xED,
        0x1D, 0x9D, 0x5D, 0xDD, 0x3D, 0xBD, 0x7D, 0xFD,
        0x03, 0x83, 0x43, 0xC3, 0x23, 0xA3, 0x63, 0xE3,
        0x13, 0x93, 0x53, 0xD3, 0x33, 0xB3, 0x73, 0xF3,
        0x0B, 0x8B, 0x4B, 0xCB, 0x2B, 0xAB, 0x6B, 0xEB,
        0x1B, 0x9B, 0x5B, 0xDB, 0x3B, 0xBB, 0x7B, 0xFB,
        0x07, 0x87, 0x47, 0xC7, 0x27, 0xA7, 0x67, 0xE7,
        0x17, 0x97, 0x57, 0xD7, 0x37, 0xB7, 0x77, 0xF7,
        0x0F, 0x8F, 0x4F, 0xCF, 0x2F, 0xAF, 0x6F, 0xEF,
        0x1F, 0x9F, 0x5F, 0xDF, 0x3F, 0xBF, 0x7F, 0xFF,
    ])

    if width % 8 == 0:
        # Fast path: reverse byte array + reverse bits in each byte
        reversed_data = bytearray(pixel_data[::-1])
        for i in range(len(reversed_data)):
            reversed_data[i] = _BIT_REV[reversed_data[i]]
        return header + bytes(reversed_data)
    else:
        # Slow path: handle padding bits per row
        output = bytearray(len(pixel_data))
        for r in range(height):
            src_start = r * row_bytes
            dst_start = (height - 1 - r) * row_bytes
            for b in range(row_bytes):
                output[dst_start + row_bytes - 1 - b] = \
                    _BIT_REV[pixel_data[src_start + b]]
        return header + bytes(output)


# ── Conversion Pipeline ────────────────────────────────────────

def convert_to_hbpl2_pages(data, paper=DEFAULT_PAPER,
                           resolution=DEFAULT_RESOLUTION,
                           copies=1, duplex=DEFAULT_DUPLEX,
                           source=DEFAULT_SOURCE):
    """Convert PDF or PostScript data to a list of HBPL-II page buffers.

    Each page is converted separately because foo2hbpl2 only processes
    one PBM page at a time.  The duplex flag is included in every
    foo2hbpl2 call so the printer knows whether to flip pages.

    Key insight from foo2hbpl2-wrapper: HBPL-II always uses PORTRAIT
    page geometry.  For landscape PDFs, we render the PDF at its natural
    landscape size, then rotate the bitmap 90° CCW to fit the portrait page.

    Returns a list of HBPL-II byte buffers (one per page), or None on failure.
    """
    fmt = detect_format(data)
    pi = PAPERS.get(paper.lower(), PAPERS[DEFAULT_PAPER])

    # Detect orientation for PDF files
    orientation = 'portrait'
    if fmt == 'pdf':
        orientation = detect_pdf_orientation(data)

    # Portrait dimensions (always used for final HBPL-II output)
    pt_xpix = pi['xpix']
    pt_ypix = pi['ypix']

    duplex_name = {1: "off", 2: "longedge", 3: "shortedge"}.get(duplex, "unknown")
    log.info(f"Format: {fmt.upper()}, Paper: {paper}, "
             f"Orientation: {orientation}, "
             f"Duplex: {duplex_name}, Copies: {copies}, Source: {source}")

    # Write input to temp file
    with tempfile.NamedTemporaryFile(suffix=f'.{fmt}', delete=False) as tmp_in:
        tmp_in.write(data)
        tmp_in_path = tmp_in.name

    try:
        if orientation == 'landscape':
            # Step 1: Render PDF at its natural landscape size
            ls_xpix = pi['ypix']  # long side = landscape width
            ls_ypix = pi['xpix']  # short side = landscape height

            gs_cmd = [
                GS_PATH,
                "-q", "-dBATCH", "-dQUIET", "-dNOPAUSE",
                "-dNOINTERPOLATE",
                f"-sPAPERSIZE={paper}",
                f"-g{ls_xpix}x{ls_ypix}",
                f"-r{resolution}",
                "-sDEVICE=pbmraw",
                "-sOutputFile=-",
                tmp_in_path
            ]
            log.info(f"Running gs (landscape): {' '.join(gs_cmd)}")
            gs_proc = subprocess.run(gs_cmd, capture_output=True, timeout=120)

            if gs_proc.returncode != 0:
                log.error(f"gs failed (rc={gs_proc.returncode}): "
                          f"{gs_proc.stderr.decode(errors='replace')[:500]}")
                return None

            if gs_proc.stderr:
                gs_err = gs_proc.stderr.decode(errors='replace')
                if gs_err.strip():
                    log.warning(f"gs stderr: {gs_err[:500]}")

            pbmraw_ls = gs_proc.stdout
            if len(pbmraw_ls) == 0:
                log.error("gs produced no output")
                return None

            log.info(f"gs produced {len(pbmraw_ls)}B pbmraw (landscape)")

            # Step 2: Split into individual PBM pages
            ls_pages = split_pbm_pages(pbmraw_ls)
            log.info(f"Split landscape pbmraw into {len(ls_pages)} pages")

            # Step 3: Rotate each page 90° CCW to portrait
            pbm_pages = []
            for i, page in enumerate(ls_pages):
                rotated = rotate_pbm90_ccw(page)
                pbm_pages.append(rotated)
                log.info(f"  Page {i+1}: rotated {len(page)}B -> {len(rotated)}B")

        else:
            # Portrait: render directly to portrait pbmraw
            gs_cmd = [
                GS_PATH,
                "-q", "-dBATCH", "-dQUIET", "-dNOPAUSE",
                "-dNOINTERPOLATE",
                f"-sPAPERSIZE={paper}",
                f"-g{pt_xpix}x{pt_ypix}",
                f"-r{resolution}",
                "-sDEVICE=pbmraw",
                "-sOutputFile=-",
                tmp_in_path
            ]
            log.info(f"Running gs: {' '.join(gs_cmd)}")
            gs_proc = subprocess.run(gs_cmd, capture_output=True, timeout=120)

            if gs_proc.returncode != 0:
                log.error(f"gs failed (rc={gs_proc.returncode}): "
                          f"{gs_proc.stderr.decode(errors='replace')[:500]}")
                return None

            if gs_proc.stderr:
                gs_err = gs_proc.stderr.decode(errors='replace')
                if gs_err.strip():
                    log.warning(f"gs stderr: {gs_err[:500]}")

            pbmraw_data = gs_proc.stdout
            if len(pbmraw_data) == 0:
                log.error("gs produced no output")
                return None

            log.info(f"gs produced {len(pbmraw_data)}B pbmraw (portrait)")

            # Split into individual PBM pages
            pbm_pages = split_pbm_pages(pbmraw_data)
            log.info(f"Split portrait pbmraw into {len(pbm_pages)} pages")

        # Step 4: Convert each PBM page to HBPL-II separately
        # For duplex long-edge: rotate even pages (2nd, 4th, ...) 180°
        # so that when the paper is flipped along the long edge, the
        # back side reads correctly (like turning a book page).
        hbpl2_pages = []
        for i, page_pbm in enumerate(pbm_pages):
            # Rotate back pages for long-edge duplex (page index 0-based:
            # even index = odd page number = front; odd index = even page
            # number = back)
            if duplex == 2 and i % 2 == 1:
                page_pbm = rotate_pbm180(page_pbm)
                log.info(f"  Page {i+1}: rotated 180° for duplex longedge")

            foo2_cmd = [
                FOO2_PATH,
                f"-r{resolution}",
                f"-g{pt_xpix}x{pt_ypix}",
                f"-p{pi['code']}",
                f"-n{copies}",
                f"-d{duplex}",     # duplex code: 1=off, 2=longedge, 3=shortedge
                f"-s{source}",     # input slot: 1=tray1, 2=tray2, 4=manual, 7=auto
            ]
            log.info(f"Running foo2hbpl2 for page {i+1}/{len(pbm_pages)}: "
                      f"{' '.join(foo2_cmd)}")
            foo2_proc = subprocess.run(
                foo2_cmd, input=page_pbm, capture_output=True, timeout=30)

            if foo2_proc.returncode != 0:
                log.error(f"foo2hbpl2 failed on page {i+1} "
                          f"(rc={foo2_proc.returncode}): "
                          f"{foo2_proc.stderr.decode(errors='replace')[:200]}")
                return None

            hbpl2_page = foo2_proc.stdout
            if len(hbpl2_page) == 0:
                log.error(f"foo2hbpl2 produced no output for page {i+1}")
                return None

            hbpl2_pages.append(hbpl2_page)
            log.info(f"  Page {i+1}: {len(page_pbm)}B pbm -> "
                      f"{len(hbpl2_page)}B HBPL-II (duplex={duplex_name})")

        total_hbpl2 = sum(len(p) for p in hbpl2_pages)
        log.info(f"Conversion complete: {len(data)}B {fmt.upper()} "
                 f"({orientation}) -> {len(pbm_pages)} pages -> "
                 f"{total_hbpl2}B HBPL-II total, duplex={duplex_name}")
        return hbpl2_pages

    finally:
        os.unlink(tmp_in_path)


# ── PJL / HBPL-II Manipulation ─────────────────────────────────

# Binary markers in foo2hbpl2 output
UEL         = b'\x1b%-12345X'         # Universal Exit Language
HBPL_START  = b'\x1bJP<'              # HBPL-II job start (one per job)
PAGE_START  = b'\x1bPS<'              # HBPL-II page start (one per page)
PJL_EOJ     = b'\x1b%-12345X@PJL EOJ\r\n'

def fix_pjl_duplex(hbpl2_output, duplex):
    """Fix the DUPLEX setting in a foo2hbpl2 PJL header.

    foo2hbpl2 hardcodes @PJL SET DUPLEX=OFF regardless of the -d
    parameter.  This function replaces it with the correct value
    so the printer's hardware duplexer is engaged.
    """
    if duplex <= 1:
        return hbpl2_output  # OFF is correct for simplex

    if duplex == 2:
        duplex_val = b'ON'
    elif duplex == 3:
        # Short-edge: PJL uses DUPLEX=ON + BINDING=SHORTEDGE
        # (DUPLEX=SHORTEDGE is not a valid PJL value and causes printer error)
        old = b'@PJL SET DUPLEX=OFF\r\n'
        new = b'@PJL SET DUPLEX=ON\r\n@PJL SET BINDING=SHORTEDGE\r\n'
        if old in hbpl2_output:
            result = hbpl2_output.replace(old, new, 1)
            log.info("  Fixed PJL DUPLEX: OFF -> ON + BINDING=SHORTEDGE")
            return result
        return hbpl2_output
    else:
        return hbpl2_output

    old = b'@PJL SET DUPLEX=OFF\r\n'
    new = b'@PJL SET DUPLEX=' + duplex_val + b'\r\n'
    if old in hbpl2_output:
        result = hbpl2_output.replace(old, new, 1)
        log.info(f"  Fixed PJL DUPLEX: OFF -> {duplex_val.decode()}")
        return result
    return hbpl2_output


def merge_duplex_job(hbpl2_pages, duplex):
    """Merge multiple foo2hbpl2 outputs into a single PJL job.

    Each foo2hbpl2 invocation produces a complete PJL job:
      [UEL + PJL JOB header + PJL SET commands + ENTER LANGUAGE=HBPL]
      [ESC JP< + job metadata]            ← job start (one per job)
      [ESC PS< + page data + ESC PE<]     ← page data (one per page)
      [UEL + PJL EOJ]

    For duplex printing, all pages must be in a SINGLE PJL job with
    a SINGLE job-start marker so the printer's hardware duplexer
    prints front+back on the same sheet.

    Merge strategy:
      Page 1:  [Fixed PJL header] + [ESC JP< + job meta] + [ESC PS< ... ESC PE< + trailer]
               (keep everything except trailing UEL+EOJ)
      Page 2+: [ESC PS< ... ESC PE< + trailer]  (page data only, NO ESC JP<)
      End:     [UEL + PJL EOJ]
    """
    if len(hbpl2_pages) == 1:
        return fix_pjl_duplex(hbpl2_pages[0], duplex)

    # Process first page: fix DUPLEX, remove trailing EOJ
    first = fix_pjl_duplex(hbpl2_pages[0], duplex)
    eoj_pos = first.rfind(PJL_EOJ)
    if eoj_pos >= 0:
        first = first[:eoj_pos]
    else:
        # Fallback: find UEL before EOJ text
        uel_pos = first.rfind(UEL)
        if uel_pos > first.find(HBPL_START):
            first = first[:uel_pos]

    merged = first

    # Process subsequent pages: extract PAGE data only (from ESC PS< to UEL)
    # Do NOT include ESC JP< (job start) — there must be only one per job
    for i, page in enumerate(hbpl2_pages[1:], 2):
        ps_pos = page.find(PAGE_START)
        if ps_pos < 0:
            log.warning(f"  Page {i}: no page-start marker (ESC PS<), skipping")
            continue

        # Find end of page data (before trailing UEL+EOJ)
        uel_pos = page.rfind(UEL)
        if uel_pos < 0 or uel_pos < ps_pos:
            hbpl_data = page[ps_pos:]
        else:
            hbpl_data = page[ps_pos:uel_pos]

        merged += hbpl_data
        log.info(f"  Merged page {i}: {len(hbpl_data)}B page data "
                 f"(ESC PS< to UEL)")

    # Append trailing EOJ
    merged += PJL_EOJ

    return merged


# ── Network ────────────────────────────────────────────────────

def send_to_printer(hbpl2_pages, host, port, duplex=DEFAULT_DUPLEX):
    """Send HBPL-II page buffers to printer via raw TCP socket.

    For simplex (duplex=1): each page is sent in a separate TCP
    connection, matching the original behavior.

    For duplex (duplex=2 or 3): all pages are merged into a single
    PJL job (with DUPLEX=ON in the header) and sent in a single
    TCP connection so the printer's hardware duplexer is engaged.
    """
    if duplex > 1 and len(hbpl2_pages) > 1:
        # Duplex: merge pages into single PJL job with correct DUPLEX setting
        merged_data = merge_duplex_job(hbpl2_pages, duplex)
        log.info(f"Duplex mode: merged {len(hbpl2_pages)} pages into "
                 f"single PJL job ({len(merged_data)}B), "
                 f"{'LONGEDGE' if duplex == 2 else 'SHORTEDGE'}")

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(30)
            sock.connect((host, port))
            sock.sendall(merged_data)
            sock.close()
            log.info(f"Duplex job sent: {len(merged_data)}B to {host}:{port}")
            return True
        except Exception as e:
            log.error(f"Failed duplex send to printer: {e}")
            return False
    elif duplex > 1 and len(hbpl2_pages) == 1:
        # Single page duplex: just fix the PJL header
        fixed = fix_pjl_duplex(hbpl2_pages[0], duplex)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((host, port))
            sock.sendall(fixed)
            sock.close()
            log.info(f"Single-page duplex sent: {len(fixed)}B to {host}:{port}")
            return True
        except Exception as e:
            log.error(f"Failed to send page to printer: {e}")
            return False
    else:
        # Simplex: send each page in separate connection (original behavior)
        total_sent = 0
        for i, page_data in enumerate(hbpl2_pages):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(10)
                sock.connect((host, port))
                sock.sendall(page_data)
                sock.close()
                total_sent += len(page_data)
                log.info(f"Sent page {i+1}/{len(hbpl2_pages)}: "
                          f"{len(page_data)}B to printer {host}:{port}")
            except Exception as e:
                log.error(f"Failed to send page {i+1} to printer: {e}")
                return False

        log.info(f"All {len(hbpl2_pages)} pages sent: "
                  f"{total_sent}B total to printer {host}:{port}")
        return True


# ── Server ─────────────────────────────────────────────────────

def handle_client(conn, addr, printer_host, printer_port):
    """Handle a CUPS connection: receive data, parse options, convert, send."""
    log.info(f"Connection from {addr}")

    data = b""
    conn.settimeout(60)
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        if len(data) == 0:
            log.warning(f"Timeout with no data from {addr}")
            conn.close()
            return

    log.info(f"Received {len(data)}B from CUPS")

    if len(data) == 0:
        log.warning("No data received, closing")
        conn.close()
        return

    # ── Parse metadata header (if present) ────────────────────
    meta_options, payload = parse_meta_header(data)

    # Extract options with defaults
    paper      = DEFAULT_PAPER
    resolution = DEFAULT_RESOLUTION
    copies     = 1
    duplex     = DEFAULT_DUPLEX
    source     = DEFAULT_SOURCE

    if meta_options:
        paper      = meta_options.get('paper', DEFAULT_PAPER)
        resolution = meta_options.get('res', DEFAULT_RESOLUTION)
        copies     = int(meta_options.get('n', '1'))
        duplex     = int(meta_options.get('d', '1'))
        source     = int(meta_options.get('source', '7'))
        log.info(f"Options from CUPS: paper={paper}, res={resolution}, "
                  f"copies={copies}, duplex={duplex}, source={source}")
    else:
        log.info("No APEOS_META header, using defaults")

    # ── Convert and send ──────────────────────────────────────
    hbpl2_pages = convert_to_hbpl2_pages(
        payload, paper=paper, resolution=resolution,
        copies=copies, duplex=duplex, source=source)

    if hbpl2_pages is None:
        log.error("Conversion failed, cannot print")
        conn.close()
        return

    success = send_to_printer(hbpl2_pages, printer_host, printer_port,
                              duplex=duplex)
    conn.close()
    log.info(f"Job complete (success={success}, pages={len(hbpl2_pages)}, "
             f"duplex={duplex})")


def run_server(port, printer_host, printer_port):
    """Run the proxy daemon listening on localhost:port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(5)
    log.info(f"apeos2350-proxy listening on 127.0.0.1:{port}")
    log.info(f"Printer target: {printer_host}:{printer_port}")
    log.info("Supports APEOS_META header for duplex/paper/copies options")
    log.info("Landscape PDFs are auto-rotated to portrait page geometry")
    log.info("Multi-page documents: each page converted separately")
    log.info("Duplex: pages sent in single TCP connection for duplex jobs")

    def shutdown(signum, frame):
        log.info("Shutting down...")
        sock.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while True:
        try:
            conn, addr = sock.accept()
            handle_client(conn, addr, printer_host, printer_port)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Accept error: {e}")


# ── Entry Point ────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Apeos 2350 NDA print proxy daemon")
    parser.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT,
                        help=f"Local proxy port (default: {DEFAULT_PROXY_PORT})")
    parser.add_argument("--printer", type=str,
                        default=f"{DEFAULT_PRINTER_HOST}:{DEFAULT_PRINTER_PORT}",
                        help=f"Printer host:port "
                             f"(default: {DEFAULT_PRINTER_HOST}:{DEFAULT_PRINTER_PORT})")
    args = parser.parse_args()

    phost, pport = args.printer.rsplit(":", 1)
    pport = int(pport)

    run_server(args.port, phost, pport)
