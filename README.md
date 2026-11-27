# `pdf-translate` — 高保真学术论文 PDF 中文翻译插件（Antigravity Skill）

将英文学术论文 PDF 翻译为**保留原双栏/单栏排版、数学公式、矢量插图、表格、代码块与目录结构**的高质量中文单语 PDF。

本插件采用 **“本地排版引擎 + 大模型并行精翻”** 的两阶段解耦架构：
- **版面解析与回写渲染**：本地运行 [`pdf2zh_next`](https://github.com/PDFMathTranslate/PDFMathTranslate-next)（BabelDOC + DocLayout-YOLO），**零外部翻译 API Key**，完整保留矢量图、表格、公式与字体子集化。
- **学术正文并行精翻**：调度 Antigravity **Gemini 4 Hydrogen** 多代理并行翻译，按 PDF 真实阅读顺序重排并对跨栏/跨页断句自动配对合译，保证专业术语统一与行文流畅。

---

## 核心特性与排版增强

1. **代码块 / CLI / 伪代码自动等宽保真**
   - 结合 PyMuPDF 字体/缩进特征与语法/CLI 特征（`_extract_pdf_layout_index` + `_detect_code_block`），自动识别独立代码块、C/Rust/Go/Python 结构体与命令行示例并标记 `preserve_verbatim: true`。
   - 写回阶段强制保持 `dst == src`，触发 BabelDOC 原样保留分支，**100% 保留原始等宽字体、缩进、换行与注释对齐**，不再折叠成衬线散文，同时避免误伤正文中的行内等宽标识符。
2. **修复 BabelDOC 0.7.x 跨页字体缓存导致的目录/列表塌陷 Bug**
   - BabelDOC 0.7.x Rust PDF 解析器在复用跨页共享的字体对象 `xref_id` 时，第 2 页起会将缓存的 `descent` 错误清零，导致相邻行纵坐标重叠并把目录项（TOC）、编号列表与参考文献熔合成单行。
   - `pipeline.py` 预处理阶段（`_prepare_pdf_for_babeldoc`）自动为每页轻量克隆 `/Type /Font` 字典 `xref`（共享底层字形流，体积开销仅数 KB），配合默认 `--split-short-lines --short-line-split-factor 0.65`，确保目录、多行单位与参考文献逐行独立换行，且不拆碎正常单/双栏正文。
3. **跨栏 / 跨页断句自动配对无缝合译**
   - 提取阶段利用 PyMuPDF 对照 PDF 真实阅读顺序排序并自动标记 `continues_to` / `continued_from`，确保被分栏或分页截断的同一句英文分在同一个翻译 chunk 中整句合译后再按比例回填。
4. **扫描版 PDF（带隐藏 OCR 文本层）支持**
   - 支持 `--ocr-workaround` 模式（自动透传 `--ocr-workaround --skip-scanned-detection` 并持久化至 `extract_config.json`），可对 1980s–1990s 经典扫描论文白底黑字清晰覆盖中文译文、同时保留原扫描插图，并由大模型在翻译时结合上下文自动修正 OCR 识错。
5. **100% 占位符完整性校验与视觉抽检**
   - `validate` 强制校验全部段落 100% 覆盖以及每个 `{v0}`, `{v1}`, ... 公式/样式占位符编号与数量完全一致；`verify` 一键将代表性页面渲染为 PNG 供视觉抽检；渲染结束后自动清理 `*.glossary.csv` 与临时目录。

---

## 目录结构

```text
pdf-translate/
├── plugin.json                              # Antigravity 插件清单
├── README.md                                # 说明文档
├── LICENSE                                  # 开源协议
└── skills/
    └── pdf-translate/
        ├── SKILL.md                         # Antigravity Skill 执行规范
        └── scripts/
            ├── pipeline.py                  # 两阶段编排脚本（extract / validate / render / verify）
            └── bridge.py                    # pdf2zh_next CLITranslator 跨平台桥接器（dump / replay）
```

---

## 安装指南

### 1. 安装插件到 Antigravity

#### Linux / macOS / WSL
```bash
git clone https://github.com/Wzhongkai/pdf-translate.git ~/.gemini/config/plugins/pdf-translate
```

#### Windows（PowerShell）
```powershell
git clone https://github.com/Wzhongkai/pdf-translate.git "$env:USERPROFILE\.gemini\config\plugins\pdf-translate"
```

### 2. 安装本地排版依赖 `pdf2zh_next`

#### Linux / macOS / WSL
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv tool install --python 3.12 pdf2zh-next
```

#### Windows（PowerShell）
```powershell
irm https://astral.sh/uv/install.ps1 | iex
uv tool install --python 3.12 pdf2zh-next
```

> **提示**：首次运行翻译时，`pdf2zh_next` 会自动下载 DocLayout-YOLO 布局模型与中文字体到本地缓存（仅需一次，后续全走本地离线缓存）。

---

## 使用方式

### 在 Antigravity 对话中使用（推荐）
安装完成后，在 Antigravity 中新开对话直接输入 `/pdf-translate` 或自然语言即可触发：
- `/pdf-translate 帮我翻译 paper.pdf`
- `/swarm /pdf-translate 并行翻译这个目录下的全部英文论文 PDF`

### 工作流与命令行参考

```text
Pass 1 (extract) ──▶ Gemini 4 Hydrogen 并行翻译 chunks ──▶ validate 校验 ──▶ Pass 2 (render) ──▶ verify 视觉抽检
```

```bash
PIPELINE="$HOME/.gemini/config/plugins/pdf-translate/skills/pdf-translate/scripts/pipeline.py"
WORKDIR="/tmp/pdf_translate_demo"

# 1. Pass 1 提取（零网络；扫描件带 OCR 层可附加 --ocr-workaround）
python3 "$PIPELINE" extract input.pdf "$WORKDIR" --chunk-size 50

# 2. 将 $WORKDIR/chunks/chunk_XXX.json 翻译并写入 $WORKDIR/translated/chunk_XXX.json
#    （由 Antigravity Gemini 4 Hydrogen 代理自动完成）

# 3. 合并并校验 100% 覆盖率与 {vN} 占位符完整性
python3 "$PIPELINE" validate "$WORKDIR"

# 4. Pass 2 回放与排版渲染（自动复用 extract_config.json 中的全部布局参数）
python3 "$PIPELINE" render input.pdf "$WORKDIR" ./output "中文论文标题"

# 5. 渲染代表性页面为 PNG 做视觉抽检
python3 "$PIPELINE" verify "./output/中文论文标题.pdf" "$WORKDIR/verify" --pages 1,7
```

> **内存注意**：单个 `pdf2zh_next` 进程峰值内存约 2.4 GB，批量处理多篇论文时 `extract` 和 `render` 阶段请严格串行执行，翻译阶段则可放心多代理全并行。
