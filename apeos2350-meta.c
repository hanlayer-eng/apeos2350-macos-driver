/*
 * apeos2350-meta.c — CUPS filter for FUJIFILM Apeos 2350 NDA
 *
 * This filter runs INSIDE the CUPS sandbox. It does NOT fork/exec any
 * child processes — it only manipulates memory buffers and writes to
 * stdout, which the sandbox allows.
 *
 * Purpose: Read CUPS print-job options (duplex, paper size, resolution,
 * copies, input slot) from argv, encode them into a compact metadata
 * header, prepend that header to the original PDF/PS data, and output
 * the combined stream to stdout.
 *
 * The downstream socket:// backend delivers the stream to our proxy
 * daemon (apeos2350-proxy.py) which parses the header and uses the
 * options when converting PDF → gs → foo2hbpl2 → HBPL-II.
 *
 * Metadata header format (one line, terminated by '\n'):
 *   APEOS_META:d=2;paper=a4;res=600x600;n=1;source=7\n
 *   <raw PDF/PS data follows immediately after the newline>
 *
 * Fields:
 *   d      — duplex code: 1=off, 2=longedge, 3=shortedge
 *   paper  — paper size name: a4, letter, legal, a5, b5
 *   res    — resolution: 600x600 or 300x300
 *   n      — number of copies
 *   source — input slot code: 1=tray1, 2=tray2, 4=manual, 7=auto
 *
 * Build:
 *   cc -O2 -o apeos2350-meta apeos2350-meta.c
 *
 * Install:
 *   cp apeos2350-meta /usr/libexec/cups/filter/apeos2350-meta
 *   chmod 755 /usr/libexec/cups/filter/apeos2350-meta
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ── Paper-code mapping (must match foo2hbpl2 -p codes) ──────── */

static int paper_code(const char *name)
{
    if (strcmp(name, "a4")     == 0) return 1;
    if (strcmp(name, "b5")     == 0) return 2;
    if (strcmp(name, "a5")     == 0) return 3;
    if (strcmp(name, "letter") == 0) return 4;
    if (strcmp(name, "legal")  == 0) return 7;
    return 1;  /* default A4 */
}

/* ── Parse CUPS options string (argv[5]) ────────────────────── */

static void parse_options(const char *opts,
                          int *duplex, int *copies,
                          char *paper, char *res, int *source)
{
    /* options format: "Key=Value Key=Value ..."
     * e.g. "Duplex=LongEdge PageSize=A4 Resolution=600x600dpi" */

    if (!opts) return;

    char *buf = strdup(opts);
    if (!buf) return;

    char *saveptr = NULL;
    char *token   = strtok_r(buf, " ", &saveptr);

    while (token) {
        /* Duplex (PPD option) */
        if (strncmp(token, "Duplex=", 7) == 0) {
            const char *val = token + 7;
            if      (strcmp(val, "LongEdge")  == 0) *duplex = 2;
            else if (strcmp(val, "ShortEdge") == 0) *duplex = 3;
            else                                     *duplex = 1;
        }
        /* sides (IPP attribute — macOS applications often use this) */
        else if (strncmp(token, "sides=", 6) == 0) {
            const char *val = token + 6;
            if      (strcmp(val, "two-sided-long-edge")  == 0) *duplex = 2;
            else if (strcmp(val, "two-sided-short-edge") == 0) *duplex = 3;
            else if (strcmp(val, "one-sided")            == 0) *duplex = 1;
        }
        /* cupsDuplex (another variant) */
        else if (strncmp(token, "cupsDuplex=", 11) == 0) {
            const char *val = token + 11;
            if      (strcmp(val, "LongEdge")  == 0) *duplex = 2;
            else if (strcmp(val, "ShortEdge") == 0) *duplex = 3;
            else                                     *duplex = 1;
        }
        /* PageSize */
        else if (strncmp(token, "PageSize=", 9) == 0) {
            const char *val = token + 9;
            if      (strcmp(val, "A4")     == 0) strcpy(paper, "a4");
            else if (strcmp(val, "Letter") == 0) strcpy(paper, "letter");
            else if (strcmp(val, "Legal")  == 0) strcpy(paper, "legal");
            else if (strcmp(val, "A5")     == 0) strcpy(paper, "a5");
            else if (strcmp(val, "B5")     == 0) strcpy(paper, "b5");
        }
        /* Resolution — CUPS format "600x600dpi", strip "dpi" */
        else if (strncmp(token, "Resolution=", 11) == 0) {
            const char *val = token + 11;
            size_t len = strlen(val);
            if (len > 3 && strcmp(val + len - 3, "dpi") == 0) {
                strncpy(res, val, len - 3);
                res[len - 3] = '\0';
            } else {
                strncpy(res, val, 15);
                res[15] = '\0';
            }
        }
        /* InputSlot */
        else if (strncmp(token, "InputSlot=", 10) == 0) {
            const char *val = token + 10;
            if      (strcmp(val, "Auto")    == 0) *source = 7;
            else if (strcmp(val, "Tray1")   == 0) *source = 1;
            else if (strcmp(val, "Tray2")   == 0) *source = 2;
            else if (strcmp(val, "Manual")  == 0) *source = 4;
        }
        /* copies (fallback if argv[4] missing) */
        else if (strncmp(token, "copies=", 7) == 0) {
            int c = atoi(token + 7);
            if (c > 0) *copies = c;
        }

        token = strtok_r(NULL, " ", &saveptr);
    }

    free(buf);
}

/* ── Main ────────────────────────────────────────────────────── */

int main(int argc, char *argv[])
{
    /* CUPS filter arguments:
     *   argv[1] = job-id
     *   argv[2] = user
     *   argv[3] = title
     *   argv[4] = copies
     *   argv[5] = options   (PPD options, "Key=Value ..." format)
     *   argv[6] = filename  (input file path, or "-" for stdin)
     */

    int   duplex   = 1;           /* 1=off (default) */
    int   copies   = 1;
    char  paper[16]  = "a4";
    char  res[16]    = "600x600";
    int   source   = 7;           /* 7=auto (default) */

    /* copies from argv[4] */
    if (argc > 4 && argv[4]) {
        int c = atoi(argv[4]);
        if (c > 0) copies = c;
    }

    /* options from argv[5] */
    if (argc > 5 && argv[5]) {
        fprintf(stderr, "apeos2350-meta: argv[5] options = \"%s\"\n", argv[5]);
        parse_options(argv[5], &duplex, &copies, paper, res, &source);
    } else {
        fprintf(stderr, "apeos2350-meta: no options (argv[5] missing)\n");
    }

    fprintf(stderr, "apeos2350-meta: duplex=%d paper=%s res=%s copies=%d source=%d\n",
            duplex, paper, res, copies, source);

    /* ── Output metadata header ─────────────────────────────── */
    int pcode = paper_code(paper);

    fprintf(stdout,
            "APEOS_META:d=%d;paper=%s;res=%s;n=%d;source=%d\n",
            duplex, paper, res, copies, source);
    fflush(stdout);

    /* ── Copy input data to stdout ──────────────────────────── */

    FILE *input = NULL;

    if (argc > 6 && argv[6] && strcmp(argv[6], "-") != 0) {
        input = fopen(argv[6], "rb");
    } else {
        /* Mark stdin as binary on macOS */
        input = stdin;
    }

    if (!input) {
        fprintf(stderr, "apeos2350-meta: ERROR cannot open input\n");
        return 1;
    }

    char buf[65536];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), input)) > 0) {
        if (fwrite(buf, 1, n, stdout) != n) {
            fprintf(stderr, "apeos2350-meta: ERROR write failed\n");
            return 1;
        }
    }

    fflush(stdout);

    if (input != stdin)
        fclose(input);

    return 0;
}
