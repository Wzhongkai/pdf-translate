---
name: pdf-translate
description: >-
  Translate English academic paper PDFs into Chinese monolingual PDFs using pdf2zh_next
  (BabelDOC + DocLayout-YOLO) for layout/formula/table preservation and Antigravity's
  Gemini 4 Hydrogen model for high-quality paragraph translation. Use this skill whenever
  the user asks to translate a PDF paper into Chinese.
---

# PDF Paper Translation (`pdf2zh_next` Layout + Gemini 4 Hydrogen Translation)

This skill combines:
- **`pdf2zh_next` (BabelDOC + DocLayout-YOLO)** for PDF parsing, two-column layout analysis, formula `{vN}` placeholder protection, vector figure/table preservation, font subsetting, and final PDF typesetting (100% local, zero external API calls).
- **Gemini 4 Hydrogen** (`gemini-4-hydrogen-medium` / `gemini-4-hydrogen-high`) for translating every extracted paragraph with full academic context and terminology consistency — **never** falling back to `SiliconFlowFree` / `GLM-4-9B` unless explicitly requested.

## User Preferences (Baked In)

1. **Monolingual Chinese only**: Always `--no-dual` unless the user explicitly asks for bilingual (`dual`).
2. **Clean Chinese filename without `(中文版)`**: Name the final PDF using a concise Chinese paper title (Windows/NTFS-safe — no `:`, `/`, `\`, `*`, `?`, `"`, `<`, `>`, `|`).
3. **Clean up all intermediate artifacts**: Delete the temp workdir, any `*.glossary.csv`, and temporary rendered verification PNGs once done.
4. **Sequential PDF rendering**: Each `pdf2zh_next` invocation peaks at ~2.4 GB RAM. Do not run two `pdf2zh_next` processes simultaneously.

## Architecture & Workflow

The helper scripts live alongside this `SKILL.md` in `./scripts/` (typically `$HOME/.gemini/config/plugins/pdf-translate/skills/pdf-translate/scripts/`):
- [pipeline.py](./scripts/pipeline.py): Cross-platform orchestrator for `extract`, `validate`, `render`, and `verify`.
- [bridge.py](./scripts/bridge.py): Cross-platform `CLITranslator` hook for `pdf2zh_next` (`dump` and `replay`).

### Prerequisites
Verify `pdf2zh_next` is available on `PATH` (or in `$HOME/.local/bin`). If missing:
- **Linux / macOS / WSL**:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  uv tool install --python 3.12 pdf2zh-next
  ```
- **Windows (PowerShell)**:
  ```powershell
  irm https://astral.sh/uv/install.ps1 | iex
  uv tool install --python 3.12 pdf2zh-next
  ```

### Step 1: Extract Layout-Preserved Segments (Pass 1, Zero Network)
Resolve `PIPELINE` to the absolute path of `./scripts/pipeline.py` next to this `SKILL.md`, then run:
```bash
WORKDIR="/tmp/pdf_translate_$(date +%s)"
python3 "$PIPELINE" extract "<input.pdf>" "$WORKDIR" --chunk-size 50
```
This runs `pdf2zh_next` locally (`--pool-max-workers 1`, `--no-auto-extract-glossary`, `--ignore-cache`, plus `--split-short-lines --short-line-split-factor 0.65` so Table of Contents entries, bulleted/numbered lists, and multi-line author affiliations stay on separate lines without splitting normal prose; and lightweight per-page `/Type /Font` dictionary xref deduplication to work around BabelDOC 0.7.x's cross-page font-cache bug that zeroes `descent` on page 2+ and collapses multi-line blocks into one `PdfLine`), aligns segments into true PDF reading order via PyMuPDF, **automatically detects and verbatim-protects code blocks / CLI listings / indented pseudocode** (preserving original monospace typography, indentation, and line breaks by keeping `dst == src`), pairs cross-column/cross-page split sentences (`continues_to` / `continued_from`) within the same chunk, and writes:
- `$WORKDIR/chunks/chunk_001.json`, `chunk_002.json`, ... (translatable prose segments only; protected code blocks are recorded in `$WORKDIR/all_segments.json` with `"preserve_verbatim": true` and automatically merged verbatim in `validate`)
- `$WORKDIR/extract_config.json` (layout flags automatically reused during `render` for 100% Pass-1/Pass-2 alignment)
- Each entry has `{"id": "<16-hex>", "text": "<source text with {vN} placeholders>"}` plus optional split-sentence links.
- For **scanned PDFs with an embedded OCR text layer**, pass `--ocr-workaround` to `extract` (passes `--ocr-workaround --skip-scanned-detection` to `pdf2zh_next` to overlay black translated text on white background boxes over scanned images, and persists the flag in `extract_config.json` for `render`).

### Step 2: Translate All Chunks Using Gemini 4 Hydrogen
- Spawn sharded **Gemini 4 Hydrogen** workers via `swarm` (`action="add"`, `model="gemini-4-hydrogen-medium"` or `"gemini-4-hydrogen-high"` for dense papers, `workspace="inherit"`) — e.g., 2–6 workers splitting the chunk files in `tasks=[...]`.
- **Mandatory translation instructions for every Hydrogen worker**:
  1. Read assigned `$WORKDIR/chunks/chunk_XXX.json` and write a JSON object `{ "<id>": "<zh translation>", ... }` to `$WORKDIR/translated/chunk_XXX.json`.
  2. **Preserve EVERY `{v0}`, `{v1}`, ... placeholder verbatim** (exact token and exact count — never translate, drop, duplicate, or alter braces).
  3. **CRITICAL — Cross-column / split-sentence handling (`continues_to` / `continued_from`)**:
     - When `A` has `"continues_to"` pointing to `B` and semantically `A + " " + B` is ONE continuous English sentence split across a column break, page break, or footnote divider:
       - **NEVER translate `A` in isolation and NEVER invent filler text like `"以下理念："` / `"如下："` or add a fake colon/period at the break!**
       - First translate the **entire merged sentence** into natural, idiomatic Chinese, then split that single Chinese sentence proportionally across `A` and `B` so reading from box `A` to box `B` flows **100% seamlessly** (keeping every `{vN}` placeholder in whichever segment originally contained it).
       - Example: `A: "...We propose safeL4, an approach to constructing microkernels based on"` + `B: "safe languages. It guarantees..."` -> `A: "...我们提出了 safeL4，一种基于"` + `B: "安全语言构建微内核的方法。它通过..."`.
     - Note: `continues_to` is a heuristic hint — if `A` and `B` are clearly separate table cells, translate each independently.
  4. **CRITICAL — Code blocks / CLI listings / Pseudocode**:
     - Most code blocks are already auto-protected by `pipeline.py extract` and excluded from `chunks/`.
     - If any multi-line code snippet, struct/class definition, shell command block, or indented pseudocode still appears in a chunk, **keep the ENTIRE segment 100% verbatim (`dst == src`), including any English `/* ... */` or `//` comments!** Modifying even a single comment causes BabelDOC to erase the original vector code layout and collapse all lines/indentation into flowing serif prose.
     - Note: BabelDOC strips `\n` inside a single translated paragraph; do not expect `\n` inside a translation string to produce line breaks.
  5. **Author names & technical identifiers**: Keep English author names, emails, DOIs, URLs, code identifiers, and domain terms accurate. Never invent Chinese characters for English author names or machine-mangle code types.
  6. **References section**: Keep bibliographic entries in original English verbatim (preserving all `{vN}` placeholders).
  7. **Fluent academic Chinese**: Translate into natural, publication-grade Chinese prose, fixing PDF line-break hyphenation (`capa- bility` -> `能力`).
  8. Keep `"Hello"` (if present from `pdf2zh_next` ping) mapped to `"你好"`.
- Wait for all Hydrogen workers to finish, then kill them once validated.

### Step 3: Merge & Validate 100% Coverage + Placeholders
```bash
python3 "$PIPELINE" validate "$WORKDIR"
```
- If `validate` reports any missing ID or `{vN}` placeholder mismatch, fix those exact entries in `$WORKDIR/translated/` and re-run `validate` until it prints `VALIDATION PASSED`.

### Step 4: Replay & Typeset Final Chinese PDF (Pass 2, Zero Network)
```bash
python3 "$PIPELINE" render "<input.pdf>" "$WORKDIR" "<output_dir>" "<CleanChineseTitle>"
```
- Replays `$WORKDIR/translations.json` through `pdf2zh_next`, restores all `{vN}` formulas/styles, subsets Chinese fonts, saves `<output_dir>/<CleanChineseTitle>.pdf`, and deletes any `*.glossary.csv`.

### Step 5: Visual Spot-Check & Cleanup
1. Render 2 representative pages (e.g., page 1 and a figure/table page) to `$WORKDIR/verify/`:
   ```bash
   python3 "$PIPELINE" verify "<output_dir>/<CleanChineseTitle>.pdf" "$WORKDIR/verify" --pages 1,7
   ```
   Inspect the PNGs with `read_file` to confirm clean typography, aligned tables/figures, and seamless cross-column splits.
2. Clean up temporary directories:
   ```bash
   rm -rf "$WORKDIR"
   ```
3. Return clickable `file://` links to the final Chinese PDF.
