#!/usr/bin/env python3
"""Cross-platform two-pass PDF translation helper for pdf2zh_next + Gemini 4 Hydrogen.

Subcommands:
  extract <input.pdf> <workdir> [--pages SPEC] [--chunk-size N]
          [--no-split-short-lines] [--short-line-split-factor F]
          [--no-protect-code] [--formular-font-pattern REGEX]
          [--ocr-workaround]
      Runs pdf2zh_next layout/formula extraction (zero network), aligns segments
      to true PDF reading order using PyMuPDF, detects & protects code blocks
      verbatim (preventing BabelDOC from collapsing indentation/line breaks),
      detects cross-column/cross-box split sentences, and writes translatable
      chunks into <workdir>/chunks/chunk_XXX.json.

  validate <workdir>
      Merges <workdir>/translated/chunk_XXX.json plus verbatim-protected code
      blocks into <workdir>/translations.json and verifies 100% coverage + {vN}
      placeholder preservation.

  render <input.pdf> <workdir> <output_dir> [chinese_basename] [--pages SPEC]
      Replays <workdir>/translations.json through pdf2zh_next using the exact
      layout flags saved during extract, renames output to clean Chinese title,
      and removes intermediate files.

  verify <pdf_path> <out_dir> [--pages 1,7]
      Renders sample pages of a PDF to PNG using PyMuPDF for visual inspection.
"""
import argparse
import bisect
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(SCRIPT_DIR, "bridge.py")
PLACEHOLDER_RE = re.compile(r"\{v\d+\}")

DEFAULT_SPLIT_SHORT_LINES = True
DEFAULT_SHORT_LINE_SPLIT_FACTOR = 0.65
DEFAULT_DEDUP_FONT_XREFS = True

MONO_FONT_RE = re.compile(
    r"(courier|mono|monl|cmtt|lmtt|txtt|pxtt|beramono|consolas|menlo|monaco|"
    r"inconsolata|typewriter|sourcecode|firacode|jetbrains|iosevka|ptmono|"
    r"sfmono|pcrr|lucidaconsole|andale|anonymouspro|dejavusansmono|"
    r"liberationmono|ubuntumono|notosansmono|robotomono|droidsansmono|"
    r"ocr-?[ab]|\bhack\b|fixedsys|teletype)",
    re.IGNORECASE,
)

PROSE_STOPWORDS = frozenset({
    "the", "that", "this", "these", "those", "with", "from", "have", "has",
    "had", "which", "were", "was", "are", "been", "being", "their", "they",
    "we", "our", "can", "could", "would", "should", "may", "might", "because",
    "however", "therefore", "although", "between", "through", "into", "about",
    "also", "more", "most", "than", "such", "when", "where", "only", "each",
    "other", "some", "many", "very", "used", "using", "paper", "section",
    "figure", "table",
})

CODE_DECL_START_RE = re.compile(
    r"^(?:\};?\s*)?(?:struct|enum|union|typedef|class|interface|trait|impl|"
    r"def|fn|func|pub|let|const|static|extern|import|from|package|#include|"
    r"#define|return|if|else|elif|for|while|switch|case|break|continue|"
    r"int|void|char|short|long|float|double|bool|unsigned|signed|"
    r"u_short|u_long|u_int|uint\d+_t|int\d+_t|size_t|ssize_t|caddr_t|dev_t|fsid_t)\b"
)

CODE_BLOCK_HEADER_RE = re.compile(
    r"^(?:\};?\s*)?(?:struct|enum|union|class|interface|trait|impl)\s+\w+[^.]*\{\s*$"
)

CODE_TYPE_KW_RE = re.compile(
    r"\b(?:struct|enum|union|typedef|class|interface|trait|impl|def|fn|func|"
    r"int|void|char|short|long|u_short|u_long|u_int|uint\d+_t|int\d+_t|"
    r"size_t|ssize_t|caddr_t|dev_t|fsid_t|bool|float|double|const|static|"
    r"extern|return|pub|let|mut|var)\b"
)

CODE_OP_RE = re.compile(r"(\(\*\w+\)\(\)|/\*|\*/|//|->|=>|:=|::|==|!=|<=|>=|<<|>>|&&|\|\|)")
CLI_PROMPT_RE = re.compile(r"^[$#>%]\s+[a-zA-Z0-9_./~-]+(?:\s|$)")
FUNC_SIG_ONLY_RE = re.compile(r"^[a-zA-Z_]\w*\s*\([a-zA-Z0-9_,\s*\[\].]*\)\s*;?$")
PSEUDOCODE_KW_RE = re.compile(
    r"(:=|←|\bif\b.{1,60}\bthen\b|\bfor\s+(?:each|all)?\b.{1,60}\bdo\b|"
    r"\bwhile\b.{1,60}\bdo\b|\bend\s*(?:if|for|while|procedure|function)\b|"
    r"\b(?:procedure|algorithm|function)\s+\w+\s*\(|\breturn\b)",
    re.IGNORECASE,
)


def _find_pdf2zh():
    found = shutil.which("pdf2zh_next")
    if found:
        return found
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".local", "bin", "pdf2zh_next"),
        os.path.join(home, ".local", "bin", "pdf2zh_next.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    print(
        "ERROR: pdf2zh_next not found. Install via:\n"
        "  uv tool install --python 3.12 pdf2zh-next",
        file=sys.stderr,
    )
    sys.exit(1)


def _find_pymupdf_python():
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".local", "share", "uv", "tools", "pdf2zh-next", "bin", "python"),
        os.path.join(home, "AppData", "Roaming", "uv", "tools", "pdf2zh-next", "Scripts", "python.exe"),
        os.path.join(home, "Library", "Application Support", "uv", "tools", "pdf2zh-next", "bin", "python"),
    ]
    uv_bin = shutil.which("uv") or os.path.join(home, ".local", "bin", "uv")
    if uv_bin and os.path.exists(uv_bin):
        try:
            res = subprocess.run([uv_bin, "tool", "dir"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                tdir = res.stdout.strip()
                candidates.insert(0, os.path.join(tdir, "pdf2zh-next", "bin", "python"))
                candidates.insert(1, os.path.join(tdir, "pdf2zh-next", "Scripts", "python.exe"))
        except Exception:
            pass
    for c in candidates:
        if os.path.exists(c):
            return c
    return sys.executable


def _prepare_pdf_for_babeldoc(input_pdf, workdir, dedup_font_xrefs=True):
    """Work around BabelDOC's cross-page font-cache bug that zeroes font descent on page 2+.

    In BabelDOC 0.7.x's Rust PDF parser, reusing a font object's xref_id across pages drops
    its cached `descent` to 0.0 on subsequent pages, shifting space visual_bbox upward so it
    overlaps descenders on the line above and collapses multi-line blocks (e.g. TOC entries)
    into a single PdfLine. Cloning only the lightweight `/Type /Font` dictionary xrefs per
    page (keeping all font streams/descriptors shared by reference) gives every page accurate
    font descent metrics with negligible size/time overhead.
    """
    if not dedup_font_xrefs:
        return input_pdf

    prep_dir = os.path.join(workdir, "prepared_input")
    out_pdf = os.path.join(prep_dir, os.path.basename(input_pdf))
    if os.path.exists(out_pdf):
        return out_pdf

    py = _find_pymupdf_python()
    code = (
        "import os, re, sys, pymupdf\n"
        "src, dst = sys.argv[1], sys.argv[2]\n"
        "doc = pymupdf.open(src)\n"
        "if len(doc) <= 1:\n"
        "    print('CLONED:0')\n"
        "    sys.exit(0)\n"
        "def find_resources(page_xref):\n"
        "    cur = page_xref\n"
        "    visited = set()\n"
        "    while cur and cur not in visited:\n"
        "        visited.add(cur)\n"
        "        rt, rv = doc.xref_get_key(cur, 'Resources')\n"
        "        if rt != 'null':\n"
        "            return cur, rt, rv\n"
        "        pt, pv = doc.xref_get_key(cur, 'Parent')\n"
        "        if pt == 'xref':\n"
        "            cur = int(pv.split()[0])\n"
        "        else:\n"
        "            break\n"
        "    return page_xref, 'null', 'null'\n"
        "def rewrite_top_level_font_refs(dict_str, seen, counter):\n"
        "    out = []\n"
        "    i = 0\n"
        "    n = len(dict_str)\n"
        "    depth = 0\n"
        "    changed = False\n"
        "    ref_re = re.compile(r'(/[^\\s/<>\\[\\]()%]+)\\s+(\\d+)\\s+0\\s+R')\n"
        "    while i < n:\n"
        "        if dict_str.startswith('<<', i):\n"
        "            depth += 1\n"
        "            out.append('<<')\n"
        "            i += 2\n"
        "            continue\n"
        "        if dict_str.startswith('>>', i):\n"
        "            depth -= 1\n"
        "            out.append('>>')\n"
        "            i += 2\n"
        "            continue\n"
        "        if depth == 1 and dict_str[i] == '/':\n"
        "            m = ref_re.match(dict_str, i)\n"
        "            if m:\n"
        "                fname, xid = m.group(1), int(m.group(2))\n"
        "                if xid in seen and not doc.xref_is_stream(xid):\n"
        "                    new_x = doc.get_new_xref()\n"
        "                    doc.update_object(new_x, doc.xref_object(xid))\n"
        "                    out.append(f'{fname} {new_x} 0 R')\n"
        "                    counter[0] += 1\n"
        "                    changed = True\n"
        "                else:\n"
        "                    seen.add(xid)\n"
        "                    out.append(m.group(0))\n"
        "                i = m.end()\n"
        "                continue\n"
        "        out.append(dict_str[i])\n"
        "        i += 1\n"
        "    return ''.join(out), changed\n"
        "seen = set()\n"
        "counter = [0]\n"
        "for page in doc:\n"
        "    owner, rt, rv = find_resources(page.xref)\n"
        "    if rt == 'null':\n"
        "        continue\n"
        "    if rt == 'xref':\n"
        "        res_xref = int(rv.split()[0])\n"
        "        ft, fv = doc.xref_get_key(res_xref, 'Font')\n"
        "    else:\n"
        "        res_xref = None\n"
        "        ft, fv = doc.xref_get_key(owner, 'Resources/Font')\n"
        "    if ft == 'xref':\n"
        "        fd_xref = int(fv.split()[0])\n"
        "        fdict_str = doc.xref_object(fd_xref)\n"
        "        new_fdict_str, changed = rewrite_top_level_font_refs(fdict_str, seen, counter)\n"
        "        if changed:\n"
        "            new_fd = doc.get_new_xref()\n"
        "            doc.update_object(new_fd, new_fdict_str)\n"
        "            if res_xref is not None:\n"
        "                new_res = doc.get_new_xref()\n"
        "                doc.update_object(new_res, doc.xref_object(res_xref))\n"
        "                doc.xref_set_key(new_res, 'Font', f'{new_fd} 0 R')\n"
        "                doc.xref_set_key(page.xref, 'Resources', f'{new_res} 0 R')\n"
        "            else:\n"
        "                if owner != page.xref:\n"
        "                    doc.xref_set_key(page.xref, 'Resources', rv)\n"
        "                doc.xref_set_key(page.xref, 'Resources/Font', f'{new_fd} 0 R')\n"
        "    elif ft == 'dict':\n"
        "        new_fv, changed = rewrite_top_level_font_refs(fv, seen, counter)\n"
        "        if changed:\n"
        "            if res_xref is not None:\n"
        "                new_res = doc.get_new_xref()\n"
        "                doc.update_object(new_res, doc.xref_object(res_xref))\n"
        "                doc.xref_set_key(new_res, 'Font', new_fv)\n"
        "                doc.xref_set_key(page.xref, 'Resources', f'{new_res} 0 R')\n"
        "            else:\n"
        "                if owner != page.xref:\n"
        "                    doc.xref_set_key(page.xref, 'Resources', rv)\n"
        "                doc.xref_set_key(page.xref, 'Resources/Font', new_fv)\n"
        "if counter[0] > 0:\n"
        "    os.makedirs(os.path.dirname(dst), exist_ok=True)\n"
        "    doc.save(dst)\n"
        "print(f'CLONED:{counter[0]}')\n"
    )
    res = subprocess.run(
        [py, "-c", code, input_pdf, out_pdf],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if res.returncode == 0 and os.path.exists(out_pdf):
        return out_pdf
    return input_pdf


def _build_bridge_cmd(mode, data_path):
    parts = [sys.executable, BRIDGE, mode, data_path]
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return " ".join(shlex.quote(p) for p in parts)


def run_pdf2zh(
    input_pdf,
    output_dir,
    bridge_cmd,
    pages=None,
    workers=8,
    split_short_lines=DEFAULT_SPLIT_SHORT_LINES,
    short_line_split_factor=DEFAULT_SHORT_LINE_SPLIT_FACTOR,
    formular_font_pattern=None,
    ocr_workaround=False,
):
    pdf2zh = _find_pdf2zh()
    cmd = [
        pdf2zh,
        input_pdf,
        "--lang-in", "en",
        "--lang-out", "zh-CN",
        "--output", output_dir,
        "--no-dual",
        "--ignore-cache",
        "--no-auto-extract-glossary",
        "--qps", "200",
        "--pool-max-workers", str(workers),
        "--clitranslator",
        "--clitranslator-command", bridge_cmd,
    ]
    if split_short_lines:
        cmd.append("--split-short-lines")
        if short_line_split_factor is not None:
            cmd.extend(["--short-line-split-factor", str(short_line_split_factor)])
    if formular_font_pattern:
        cmd.extend(["--formular-font-pattern", formular_font_pattern])
    if ocr_workaround:
        cmd.extend(["--ocr-workaround", "--skip-scanned-detection"])
    if pages:
        cmd.extend(["--pages", pages])
    env = os.environ.copy()
    local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin")
    env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
    res = subprocess.run(cmd, env=env)
    if res.returncode != 0:
        sys.exit(res.returncode)


def _norm(s):
    s = PLACEHOLDER_RE.sub("", s)
    s = re.sub(r"-\s+", "", s)
    s = re.sub(r"[^a-zA-Z0-9]+", "", s)
    return s.lower()


def _extract_pdf_layout_index(input_pdf):
    """Extract normalized reading-order text plus monospace/indentation index via PyMuPDF."""
    py = _find_pymupdf_python()
    code = (
        "import json, re, sys, pymupdf\n"
        "mono_re = re.compile(sys.argv[2], re.IGNORECASE)\n"
        "alnum_re = re.compile(r'[^a-zA-Z0-9]+')\n"
        "doc = pymupdf.open(sys.argv[1])\n"
        "chunks = []\n"
        "spans = []\n"
        "line_map = {}\n"
        "cur = 0\n"
        "lid = 0\n"
        "for page in doc:\n"
        "    for b in page.get_text('dict')['blocks']:\n"
        "        if b.get('type') != 0:\n"
        "            continue\n"
        "        for l in b.get('lines', []):\n"
        "            lid += 1\n"
        "            lx0 = round(float(l['bbox'][0]), 1)\n"
        "            line_norm_parts = []\n"
        "            line_mono = 0\n"
        "            line_total = 0\n"
        "            for s in l.get('spans', []):\n"
        "                sn = alnum_re.sub('', s.get('text', '')).lower()\n"
        "                if not sn:\n"
        "                    continue\n"
        "                fn = s.get('font', '').split('+')[-1]\n"
        "                is_mono = 1 if ((s.get('flags', 0) & 8) or mono_re.search(fn)) else 0\n"
        "                nlen = len(sn)\n"
        "                chunks.append(sn)\n"
        "                line_norm_parts.append(sn)\n"
        "                line_total += nlen\n"
        "                if is_mono:\n"
        "                    line_mono += nlen\n"
        "                if spans and spans[-1][3] == lid and spans[-1][2] == is_mono:\n"
        "                    spans[-1][1] = cur + nlen\n"
        "                else:\n"
        "                    spans.append([cur, cur + nlen, is_mono, lid, lx0])\n"
        "                cur += nlen\n"
        "            if line_total >= 4:\n"
        "                ln = ''.join(line_norm_parts)\n"
        "                if ln not in line_map:\n"
        "                    line_map[ln] = [line_mono, line_total]\n"
        "                else:\n"
        "                    line_map[ln][0] += line_mono\n"
        "                    line_map[ln][1] += line_total\n"
        "sys.stdout.write(json.dumps({'full_norm': ''.join(chunks), 'spans': spans, 'line_map': line_map}))\n"
    )
    res = subprocess.run(
        [py, "-c", code, input_pdf, MONO_FONT_RE.pattern],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if res.returncode != 0 or not res.stdout.strip():
        return {"full_norm": "", "spans": [], "starts": [], "line_map": {}}
    try:
        data = json.loads(res.stdout)
    except Exception:
        return {"full_norm": "", "spans": [], "starts": [], "line_map": {}}
    spans = data.get("spans", [])
    data["starts"] = [sp[0] for sp in spans]
    return data


def _lookup_pdf_metrics(norm_text, layout_idx, pos_range):
    spans = layout_idx.get("spans") or []
    starts = layout_idx.get("starts") or []
    if pos_range and pos_range[0] < 10**12 and spans:
        start, end = pos_range
        idx = max(0, bisect.bisect_right(starts, start) - 1)
        total = 0
        mono = 0
        lines = set()
        x_buckets = set()
        while idx < len(spans):
            s0, s1, is_mono, lid, lx0 = spans[idx]
            if s0 >= end:
                break
            ov = min(s1, end) - max(s0, start)
            if ov > 0:
                total += ov
                if is_mono:
                    mono += ov
                lines.add(lid)
                x_buckets.add(round(lx0 / 4.0))
            idx += 1
        if total > 0:
            return {
                "mono_ratio": mono / total,
                "line_count": len(lines),
                "indent_levels": len(x_buckets),
            }
    line_map = layout_idx.get("line_map") or {}
    if len(norm_text) >= 4 and norm_text in line_map:
        mc, tc = line_map[norm_text]
        if tc > 0:
            return {"mono_ratio": mc / tc, "line_count": 1, "indent_levels": 1}
    return {"mono_ratio": None, "line_count": 0, "indent_levels": 0}


def _prose_stopword_ratio(s):
    words = re.findall(r"[a-zA-Z0-9_]+", s.lower())
    words = [w for w in words if len(w) >= 2 and any(c.isalpha() for c in w)]
    if not words:
        return 0.0
    return sum(1 for w in words if w in PROSE_STOPWORDS) / len(words)


def _detect_code_block(text, metrics):
    """Return (is_code_block, reason) for segments that must stay verbatim to preserve layout."""
    raw = text.strip()
    if len(raw) < 4 or raw == "Hello":
        return False, ""
    if re.match(r"^\[\d+\]\s+", raw):
        return False, ""
    if (
        re.match(r"^(?:Figure|Table|Section|Fig\.)\s+\d+[\s.:]", raw, re.IGNORECASE)
        and ";" not in raw
        and "{" not in raw
    ):
        return False, ""

    clean = PLACEHOLDER_RE.sub("_", raw).strip()
    no_comments = re.sub(r"/\*.*?\*/", " ", clean, flags=re.DOTALL)
    no_comments = re.sub(r"//.*", " ", no_comments)
    nc_stripped = no_comments.strip()
    pr = _prose_stopword_ratio(no_comments)

    if CODE_BLOCK_HEADER_RE.match(nc_stripped):
        return True, "code_header"

    mono_ratio = metrics.get("mono_ratio")
    line_count = metrics.get("line_count", 0)
    indent_levels = metrics.get("indent_levels", 0)

    if FUNC_SIG_ONLY_RE.match(clean):
        if "_" in clean or "," in clean or clean.endswith(";") or (mono_ratio is not None and mono_ratio >= 0.80):
            return True, "func_signature"

    semi_count = no_comments.count(";")
    has_braces = ("{" in no_comments) or ("}" in no_comments)
    has_type_kw = bool(CODE_TYPE_KW_RE.search(no_comments))
    has_code_op = bool(CODE_OP_RE.search(clean))
    is_cli = bool(CLI_PROMPT_RE.match(clean))

    # Rule 1: Monospace-dominant block (covers Courier, CourierNewPS, CMTT, NimbusMonL, etc.)
    if mono_ratio is not None and mono_ratio >= 0.80:
        has_snake = bool(re.search(r"\b[a-zA-Z]\w*_[a-zA-Z0-9_]+\b", clean))
        is_decl = bool(CODE_DECL_START_RE.match(nc_stripped))
        is_addr = bool(re.match(r"^(\S+@\S+|https?://\S+)$", clean, re.IGNORECASE))
        if is_addr:
            return True, f"mono_addr(mono={mono_ratio:.2f})"
        if (has_code_op or has_braces or semi_count >= 1 or is_cli or is_decl or has_snake):
            if pr < 0.22 or semi_count >= 2 or has_braces or is_cli:
                return True, f"mono_code(mono={mono_ratio:.2f})"
        if line_count >= 2 and indent_levels >= 2 and pr < 0.15:
            return True, f"mono_indented(mono={mono_ratio:.2f},indents={indent_levels})"

    # Rule 2: Font-independent code / CLI / indented pseudocode detection
    prose_sentences = len(re.findall(r"\.\s+[A-Z][a-z]+", no_comments))
    if semi_count >= 2 and (has_braces or has_code_op or (has_type_kw and prose_sentences == 0)) and prose_sentences < 2 and pr < 0.15:
        return True, "syntax_code_block"
    if semi_count == 1 and nc_stripped.endswith(";") and CODE_DECL_START_RE.match(nc_stripped) and len(nc_stripped) <= 120 and pr < 0.10:
        return True, "syntax_decl_stmt"
    if ("{" in no_comments and "}" in no_comments) and (has_type_kw or has_code_op or semi_count >= 1) and pr < 0.15:
        return True, "syntax_brace_block"
    if is_cli and pr < 0.12 and not re.search(r"\.\s+[A-Z]", clean):
        return True, "cli_command_block"
    if line_count >= 3 and indent_levels >= 2 and len(PSEUDOCODE_KW_RE.findall(no_comments)) >= 2 and pr < 0.15:
        return True, "pseudocode_block"

    return False, ""


def _looks_incomplete_end(text):
    t = text.strip()
    if len(t) < 40:
        return False
    if t.startswith("[") and "]" in t[:6]:
        return False
    if re.match(r"^(\d+(\.\d+)*\s+|ASPLOS\b|Table\s+\d+|Figure\s+\d+|Listing\s+\d+|Keywords:|CCS Concepts:)", t):
        return False
    if re.search(r"(\S+@\S+|https?://\S+|doi\.org/\S+)\s*$", t, re.IGNORECASE):
        return False
    if re.search(r"[.?!:;\)\]\u201d\"]$", t):
        return False
    return bool(re.search(r"[a-zA-Z0-9,\-]$", t))


def _looks_continuation_start(text):
    t = text.strip()
    if len(t) < 12:
        return False
    if t.startswith("[") and "]" in t[:6]:
        return False
    t_clean = PLACEHOLDER_RE.sub("", t).lstrip()
    if not t_clean:
        return False
    if t_clean[0].islower():
        first_word = re.split(r"[\s.,:;()]+", t_clean)[0]
        if first_word not in ("safeL4", "seL4", "musl", "http", "https", "doi", "kobj", "pub", "rustc", "rust"):
            return True
    return False


def order_and_pair_segments(ordered, input_pdf, protect_code=True):
    """Sort segments into PDF reading order, detect code blocks, and pair split sentences."""
    layout_idx = _extract_pdf_layout_index(input_pdf)
    full_norm = layout_idx.get("full_norm", "")
    positions = {}
    norm_cache = {}
    if full_norm:
        for idx, seg in enumerate(ordered):
            n = _norm(seg["text"])
            norm_cache[seg["id"]] = n
            if len(n) >= 18:
                pos = full_norm.find(n[:40])
                if pos < 0 and len(n) >= 30:
                    tail_pos = full_norm.find(n[-30:])
                    if tail_pos >= 0:
                        pos = max(0, tail_pos + 30 - len(n))
                if pos >= 0:
                    positions[seg["id"]] = (pos, pos + len(n))
                    continue
            positions[seg["id"]] = (10**12 + idx, 10**12 + idx)

        ordered = sorted(ordered, key=lambda s: positions[s["id"]][0])

    if protect_code:
        for seg in ordered:
            sid = seg["id"]
            n = norm_cache.get(sid) or _norm(seg["text"])
            metrics = _lookup_pdf_metrics(n, layout_idx, positions.get(sid))
            is_code, reason = _detect_code_block(seg["text"], metrics)
            if is_code:
                seg["preserve_verbatim"] = True
                seg["preserve_reason"] = reason

    used_b = set()
    for i, a in enumerate(ordered):
        if a.get("preserve_verbatim") or not _looks_incomplete_end(a["text"]):
            continue
        pa = positions.get(a["id"], (10**12, 10**12))
        best_j = None
        best_gap = 10**9
        for j in range(i + 1, min(i + 15, len(ordered))):
            b = ordered[j]
            if b.get("preserve_verbatim") or b["id"] in used_b or not _looks_continuation_start(b["text"]):
                continue
            pb = positions.get(b["id"], (10**12, 10**12))
            if pa[0] < 10**12 and pb[0] < 10**12:
                gap = pb[0] - pa[1]
                if -30 <= gap <= 500 and gap < best_gap:
                    best_gap = gap
                    best_j = j
                    if gap <= 10:
                        break
        if best_j is not None:
            b = ordered[best_j]
            a["continues_to"] = b["id"]
            a["next_fragment_preview"] = b["text"][:160]
            b["continued_from"] = a["id"]
            b["prev_fragment_preview"] = a["text"][-160:]
            used_b.add(b["id"])

    return ordered


def cmd_extract(args):
    os.makedirs(args.workdir, exist_ok=True)
    raw_jsonl = os.path.join(args.workdir, "segments.jsonl")
    pass1_dir = os.path.join(args.workdir, "pass1_tmp")
    chunks_dir = os.path.join(args.workdir, "chunks")
    trans_dir = os.path.join(args.workdir, "translated")

    if os.path.exists(raw_jsonl):
        os.remove(raw_jsonl)
    shutil.rmtree(pass1_dir, ignore_errors=True)
    shutil.rmtree(chunks_dir, ignore_errors=True)
    os.makedirs(pass1_dir, exist_ok=True)
    os.makedirs(chunks_dir, exist_ok=True)
    os.makedirs(trans_dir, exist_ok=True)

    split_short_lines = not args.no_split_short_lines
    short_line_split_factor = args.short_line_split_factor
    protect_code = not args.no_protect_code
    dedup_font_xrefs = not args.no_dedup_font_xrefs
    formular_font_pattern = args.formular_font_pattern
    ocr_workaround = args.ocr_workaround

    shutil.rmtree(os.path.join(args.workdir, "prepared_input"), ignore_errors=True)
    effective_pdf = _prepare_pdf_for_babeldoc(
        args.input_pdf, args.workdir, dedup_font_xrefs=dedup_font_xrefs
    )

    extract_cfg = {
        "split_short_lines": split_short_lines,
        "short_line_split_factor": short_line_split_factor,
        "protect_code": protect_code,
        "dedup_font_xrefs": dedup_font_xrefs,
        "formular_font_pattern": formular_font_pattern,
        "ocr_workaround": ocr_workaround,
    }
    with open(os.path.join(args.workdir, "extract_config.json"), "w", encoding="utf-8") as f:
        json.dump(extract_cfg, f, ensure_ascii=False, indent=2)

    bridge_cmd = _build_bridge_cmd("dump", raw_jsonl)
    run_pdf2zh(
        effective_pdf,
        pass1_dir,
        bridge_cmd,
        pages=args.pages,
        workers=1,
        split_short_lines=split_short_lines,
        short_line_split_factor=short_line_split_factor,
        formular_font_pattern=formular_font_pattern,
        ocr_workaround=ocr_workaround,
    )
    shutil.rmtree(pass1_dir, ignore_errors=True)

    seen = {}
    ordered = []
    if os.path.exists(raw_jsonl):
        with open(raw_jsonl, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if obj["id"] not in seen:
                    seen[obj["id"]] = obj["text"]
                    ordered.append(obj)

    ordered = order_and_pair_segments(ordered, args.input_pdf, protect_code=protect_code)

    all_path = os.path.join(args.workdir, "all_segments.json")
    with open(all_path, "w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)

    translatable = [s for s in ordered if not s.get("preserve_verbatim")]
    preserved_count = len(ordered) - len(translatable)

    chunk_size = args.chunk_size
    chunk_files = []
    cur = []
    active = set()
    idx = 0
    for seg in translatable:
        cur.append(seg)
        if "continues_to" in seg:
            active.add(seg["continues_to"])
        active.discard(seg["id"])
        if len(cur) >= chunk_size and not active:
            idx += 1
            cpath = os.path.join(chunks_dir, f"chunk_{idx:03d}.json")
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
            chunk_files.append(cpath)
            cur = []
    if cur:
        idx += 1
        cpath = os.path.join(chunks_dir, f"chunk_{idx:03d}.json")
        with open(cpath, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        chunk_files.append(cpath)

    split_count = sum(1 for s in translatable if "continues_to" in s)
    print(
        f"EXTRACTED: {len(translatable)} translatable + {preserved_count} preserved code-block segments "
        f"({split_count} split-sentence links) into {len(chunk_files)} chunks at {chunks_dir}"
    )
    for cf in chunk_files:
        print(f"  {cf}")


def cmd_validate(args):
    all_path = os.path.join(args.workdir, "all_segments.json")
    trans_dir = os.path.join(args.workdir, "translated")
    out_path = os.path.join(args.workdir, "translations.json")

    with open(all_path, "r", encoding="utf-8") as f:
        all_segs = json.load(f)

    merged = {}
    for path in sorted(glob.glob(os.path.join(trans_dir, "*.json"))):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            merged.update(data)
        elif isinstance(data, list):
            for item in data:
                if "id" in item and "translation" in item:
                    merged[item["id"]] = item["translation"]

    missing = []
    placeholder_errors = []
    preserved_count = 0
    for seg in all_segs:
        sid = seg["id"]
        src = seg["text"]
        if seg.get("preserve_verbatim"):
            # Force exact source text (dst == src) so BabelDOC leaves original code-block layout untouched.
            merged[sid] = src
            preserved_count += 1
            continue
        if sid not in merged:
            missing.append(sid)
            continue
        dst = merged[sid]
        src_ph = sorted(PLACEHOLDER_RE.findall(src))
        dst_ph = sorted(PLACEHOLDER_RE.findall(dst))
        if src_ph != dst_ph:
            placeholder_errors.append({"id": sid, "expected": src_ph, "got": dst_ph, "src": src[:120], "dst": dst[:120]})

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    translated_count = len(all_segs) - preserved_count - len(missing)
    print(
        f"MERGED: {len(merged)}/{len(all_segs)} segments "
        f"({translated_count} translated + {preserved_count} preserved verbatim) -> {out_path}"
    )
    if missing:
        print(f"MISSING ({len(missing)}): {missing[:10]}", file=sys.stderr)
    if placeholder_errors:
        print(f"PLACEHOLDER MISMATCH ({len(placeholder_errors)}):", file=sys.stderr)
        for err in placeholder_errors[:10]:
            print(" ", json.dumps(err, ensure_ascii=False), file=sys.stderr)
    if missing or placeholder_errors:
        sys.exit(1)
    print("VALIDATION PASSED: 100% coverage & all {vN} placeholders intact.")


def cmd_render(args):
    trans_path = os.path.join(args.workdir, "translations.json")
    if not os.path.exists(trans_path):
        print(f"ERROR: {trans_path} not found; run 'validate' first.", file=sys.stderr)
        sys.exit(1)

    cfg_path = os.path.join(args.workdir, "extract_config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        split_short_lines = cfg.get("split_short_lines", DEFAULT_SPLIT_SHORT_LINES)
        short_line_split_factor = cfg.get("short_line_split_factor", DEFAULT_SHORT_LINE_SPLIT_FACTOR)
        dedup_font_xrefs = cfg.get("dedup_font_xrefs", False)
        formular_font_pattern = cfg.get("formular_font_pattern")
        ocr_workaround = cfg.get("ocr_workaround", False)
    else:
        # Backward compatibility for workdirs extracted before extract_config.json existed
        split_short_lines = False
        short_line_split_factor = DEFAULT_SHORT_LINE_SPLIT_FACTOR
        dedup_font_xrefs = False
        formular_font_pattern = None
        ocr_workaround = False

    if args.no_split_short_lines:
        split_short_lines = False
    elif args.split_short_lines:
        split_short_lines = True
    if args.short_line_split_factor is not None:
        short_line_split_factor = args.short_line_split_factor
    if args.no_dedup_font_xrefs:
        dedup_font_xrefs = False
    elif args.dedup_font_xrefs:
        dedup_font_xrefs = True
    if args.formular_font_pattern is not None:
        formular_font_pattern = args.formular_font_pattern
    if args.no_ocr_workaround:
        ocr_workaround = False
    elif args.ocr_workaround:
        ocr_workaround = True

    os.makedirs(args.output_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(args.input_pdf))[0]
    effective_pdf = _prepare_pdf_for_babeldoc(
        args.input_pdf, args.workdir, dedup_font_xrefs=dedup_font_xrefs
    )

    bridge_cmd = _build_bridge_cmd("replay", trans_path)
    run_pdf2zh(
        effective_pdf,
        args.output_dir,
        bridge_cmd,
        pages=args.pages,
        split_short_lines=split_short_lines,
        short_line_split_factor=short_line_split_factor,
        formular_font_pattern=formular_font_pattern,
        ocr_workaround=ocr_workaround,
    )

    mono = os.path.join(args.output_dir, f"{stem}.zh-CN.mono.pdf")
    if not os.path.exists(mono):
        candidates = sorted(glob.glob(os.path.join(args.output_dir, f"{stem}*.mono.pdf")), key=os.path.getmtime, reverse=True)
        mono = candidates[0] if candidates else ""

    if not mono or not os.path.exists(mono):
        print(f"ERROR: translated mono PDF not found in {args.output_dir}", file=sys.stderr)
        sys.exit(1)

    for csv in glob.glob(os.path.join(args.output_dir, f"{stem}*.glossary.csv")):
        os.remove(csv)

    final_path = mono
    if args.chinese_name:
        clean = args.chinese_name
        if clean.endswith(".pdf"):
            clean = clean[:-4]
        final_path = os.path.join(args.output_dir, f"{clean}.pdf")
        os.replace(mono, final_path)

    print(f"RENDERED: {final_path}")


def cmd_verify(args):
    os.makedirs(args.out_dir, exist_ok=True)
    py = _find_pymupdf_python()
    pages = [int(p.strip()) for p in args.pages.split(",") if p.strip().isdigit()]
    code = (
        "import json, os, sys, pymupdf\n"
        "pdf_path, out_dir, pages = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])\n"
        "doc = pymupdf.open(pdf_path)\n"
        "print(f'TOTAL_PAGES: {len(doc)}')\n"
        "for p in pages:\n"
        "    if 1 <= p <= len(doc):\n"
        "        out = os.path.join(out_dir, f'p{p}.png')\n"
        "        doc[p - 1].get_pixmap(dpi=130).save(out)\n"
        "        print(f'SAVED: {out}')\n"
    )
    res = subprocess.run([py, "-c", code, args.pdf_path, args.out_dir, json.dumps(pages)])
    if res.returncode != 0:
        sys.exit(res.returncode)


def main():
    parser = argparse.ArgumentParser(description="pdf2zh_next + Hydrogen two-pass translation pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ext = sub.add_parser("extract")
    p_ext.add_argument("input_pdf")
    p_ext.add_argument("workdir")
    p_ext.add_argument("--pages", default=None)
    p_ext.add_argument("--chunk-size", type=int, default=50)
    p_ext.add_argument("--no-split-short-lines", action="store_true")
    p_ext.add_argument("--short-line-split-factor", type=float, default=DEFAULT_SHORT_LINE_SPLIT_FACTOR)
    p_ext.add_argument("--no-protect-code", action="store_true")
    p_ext.add_argument("--no-dedup-font-xrefs", action="store_true")
    p_ext.add_argument("--formular-font-pattern", default=None)
    p_ext.add_argument("--ocr-workaround", action="store_true")

    p_val = sub.add_parser("validate")
    p_val.add_argument("workdir")

    p_ren = sub.add_parser("render")
    p_ren.add_argument("input_pdf")
    p_ren.add_argument("workdir")
    p_ren.add_argument("output_dir")
    p_ren.add_argument("chinese_name", nargs="?", default=None)
    p_ren.add_argument("--pages", default=None)
    p_ren.add_argument("--split-short-lines", action="store_true")
    p_ren.add_argument("--no-split-short-lines", action="store_true")
    p_ren.add_argument("--short-line-split-factor", type=float, default=None)
    p_ren.add_argument("--dedup-font-xrefs", action="store_true")
    p_ren.add_argument("--no-dedup-font-xrefs", action="store_true")
    p_ren.add_argument("--formular-font-pattern", default=None)
    p_ren.add_argument("--ocr-workaround", action="store_true")
    p_ren.add_argument("--no-ocr-workaround", action="store_true")

    p_ver = sub.add_parser("verify")
    p_ver.add_argument("pdf_path")
    p_ver.add_argument("out_dir")
    p_ver.add_argument("--pages", default="1,7")

    args = parser.parse_args()
    if args.cmd == "extract":
        cmd_extract(args)
    elif args.cmd == "validate":
        cmd_validate(args)
    elif args.cmd == "render":
        cmd_render(args)
    elif args.cmd == "verify":
        cmd_verify(args)


if __name__ == "__main__":
    main()
