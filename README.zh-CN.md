# PaperLocale

[![tests](https://github.com/hazugi2004/paperlocale/actions/workflows/tests.yml/badge.svg)](https://github.com/hazugi2004/paperlocale/actions/workflows/tests.yml)
[![release](https://img.shields.io/github/v/release/hazugi2004/paperlocale)](https://github.com/hazugi2004/paperlocale/releases/latest)
[![license](https://img.shields.io/github/license/hazugi2004/paperlocale)](LICENSE)

PaperLocale 是一个面向学术论文的可验证保版翻译工具。它的目标不是笼统承诺“原格式完全不变”，而是在明确、可测试的边界内保留页数、页面尺寸、栏位、图片、表格和公式，并在科学信息被破坏时拒绝生成候选 PDF。

![PaperLocale 可验证工作流](docs/assets/workflow.svg)

下方演示由 PaperLocale 自有的一页双栏合成 PDF 生成，其中包含公式文本、矢量表格和嵌入图片，不会再分发任何受版权限制的论文。

![PaperLocale 原文、译文与全页 QA 演示](docs/assets/demo.gif)

## 当前目标

第一条正式生产路径固定为：

1. 从 PDF 版面引擎收集待译片段；
2. 由用户明确选择一个翻译 Provider；
3. 对公式、富文本、数字、单位、缩写、URL、DOI 和领域术语执行硬门禁；
4. 仅使用通过门禁的译文重建 PDF；
5. 渲染全部页面并生成可复核的质量报告。

中文与英文长度不同，因此逐行断行不可能完全相同。PaperLocale 的可检验承诺是：页面结构和受保护科学信息保持闭合；一旦不闭合，就在渲染前失败。

## 已实现能力

- `codex-local`：调用用户本机已登录的 Codex，仅用于可信本地环境；
- `openai-compatible`：由用户自备 API Key，可连接 OpenAI 及兼容接口；
- `chatgpt-web` 人工桥接：把哈希绑定批次粘贴到 ChatGPT 网页端普通 Chat，再导入
  严格 JSON 回复；项目不登录、抓取或自动点击网页；
- `qwen-mt`：由用户自备百炼 API Key，按 Qwen-MT 专用翻译接口的单片段语义调用；
- `atmospheric-science`：内置 18 条大气科学固定术语和 5 个回归案例；
- `collect -> translate -> validate -> render -> qa -> accept`：有清单、有断点、不可跳步的单向工作流；
- 页数、MediaBox、CropBox、图片对象、矢量表格/图形、空白页和内部占位符机器检查；
- 所有页面的原文—译文并排 PNG，必须显式记录人工视觉验收。

项目不会读取、复制或记录 `~/.codex/auth.json`。Codex 会员额度不会被包装成公共 API。

## 领域包

首个内置领域包为 `atmospheric-science`，包括固定术语、保护规则、翻译约束和回归案例。后续医学、地学、生态学等领域只需新增领域包，不修改翻译核心。

可以用真实 Provider 翻译全部公开案例并生成参考译文—候选译文报告：

```bash
paperlocale provider-eval \
  --provider codex-local \
  --model gpt-5.6-sol \
  --reasoning-effort high \
  --domain atmospheric-science \
  --output provider-eval.json
```

报告只自动判定内容硬合同和是否与参考译文逐字一致，不把字符串相似度冒充语义准确率；每条候选仍明确要求领域人员人工复核。

## 安装

需要 Python 3.10–3.13。完整 PDF 流程还需要 Poppler 的 `pdftoppm`：macOS 可执行 `brew install poppler`，Ubuntu 可执行 `sudo apt-get install poppler-utils`。
PaperLocale 0.4.0 已通过带数字 attestation 的 Trusted Publishing 发布到 PyPI；
维护者发布流程与公开核验证据见 [PyPI 发布手册](docs/PYPI_PUBLISHING.zh-CN.md)。
v0.4.0 网页桥接的操作与额度边界见
[ChatGPT 网页端人工翻译桥接](docs/CHATGPT_WEB_MANUAL.zh-CN.md)。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install "paperlocale[layout]==0.4.0"
paperlocale --version
paperlocale domain-check atmospheric-science
```

当前验证兼容 `pdf2zh-next 2.9.0`。版面依赖较多，所以被放在可选的 `layout` 依赖组中。

## 开始翻译

使用同一条可断点续跑命令推进初始化、片段收集、翻译、内容门禁、PDF 重建和全页 QA。使用本机 Codex 登录态：

```bash
codex login
paperlocale run paper.pdf \
  --run-dir runs/paper \
  --provider codex-local \
  --model gpt-5.6-sol \
  --reasoning-effort high \
  --domain atmospheric-science
```

默认参考文献策略是 `preserve`。首次运行会在收集片段后生成
`reference_review.jsonl` 并明确停止，不会根据片段顺序、作者年份或 DOI 密度
猜测参考文献。检查清单后，把没有被确定性匹配、但经你确认属于参考文献的
片段 ID 逐个补充：

```bash
paperlocale confirm-references \
  --run-dir runs/paper \
  --segment-id 需要补充的片段ID \
  --segment-id 另一个片段ID \
  --confirmed-by "你的名字"
```

已经确定性匹配的 ID 会自动纳入，不需要重复填写；没有额外片段时可省略所有
`--segment-id`。确认后重新执行原来的 `paperlocale run` 命令即可继续。映射会
绑定源 PDF 与 `segments.jsonl` 的哈希，翻译开始后不能静默修改。

如需只翻译参考文献中的作品标题，在原运行命令中显式添加
`--reference-policy translate-titles`。作者、年份、期刊、卷期页码、DOI 和 URL
仍受保留约束，参考文献不会套用正文专业术语门禁。

当前已发布 BabelDOC 仍可能对“译文等于原文”的参考文献重新排版。对象级修复已
提交到上游 [Issue #610](https://github.com/funstory-ai/BabelDOC/issues/610) 和
[PR #611](https://github.com/funstory-ai/BabelDOC/pull/611)；等待上游审查期间，
PaperLocale 不用本地 PDF 区域覆盖伪装成真正的 preserve。

或者使用自备密钥的 OpenAI-compatible 接口。`--base-url` 应包含服务的 API 版本前缀，但不要包含 `/chat/completions`：

```bash
export PAPERLOCALE_API_KEY="你的密钥"
paperlocale run paper.pdf \
  --run-dir runs/paper \
  --provider openai-compatible \
  --base-url https://api.example.com/v1 \
  --model your-model \
  --domain atmospheric-science
```

远程端点必须使用 HTTPS，避免 API Key 经明文网络发送。只有
`localhost`、`127.0.0.1` 和 `::1` 等本机 loopback 服务可以使用 HTTP。

如需使用阿里云百炼 Qwen-MT 专用翻译模型，密钥仍只放在环境变量中，
`--base-url` 填 API 版本前缀，不要包含 `/chat/completions`：

```bash
export PAPERLOCALE_API_KEY="你的-DashScope-Key"
paperlocale run paper.pdf \
  --run-dir runs/paper \
  --provider qwen-mt \
  --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --model qwen-mt-plus \
  --domain atmospheric-science
```

Qwen-MT 每次只接收一个源片段；PaperLocale 从领域包读取语言、学科提示和
术语干预，每条译文通过门禁后立即原子保存，再调用下一条。API Key 只放在
Authorization header，不写入运行清单。

打开 `runs/paper/qa/comparisons/` 逐页核对。确认页面结构、表格、图片、公式和中文断行可接受后，记录验收：

```bash
paperlocale accept --run-dir runs/paper --reviewed-by "你的名字"
paperlocale status --run-dir runs/paper
```

运行中断后，重新执行同一条 `paperlocale run` 命令即可：清单会从最后完成的阶段继续，已通过门禁的译文会从 `translations.jsonl` 复用。同批个别译文失败时，其他合格结果仍会原子保存，失败候选和具体原因写入 `rejected_translations.jsonl`。翻译完成后的续跑可以省略 `--provider` 和 API 凭据。`run` 只推进到 `qa_generated`，不会自动记录人工验收；项目也不会在同一次运行中静默切换 Provider。

如果失败片段确实不是可翻译正文，例如纯公式或作者姓名串，不要添加“公式：”
等虚构中文，也不要放宽全局中文门禁。人工检查原文和失败候选后，显式确认透传：

```bash
paperlocale confirm-passthrough \
  --run-dir runs/paper \
  --segment-id 已确认无需翻译的片段ID \
  --reason "纯公式，没有可翻译正文" \
  --confirmed-by "你的名字"
```

`passthrough_map.json` 会绑定源 PDF 和 `segments.jsonl` 哈希，并逐条记录原因、
确认人和时间。透传片段必须与原文逐字相同、不会发送给 Provider；部分批次失败后
补充确认时，已保存的其他译文仍会复用。

每次调用 Provider 前，PaperLocale 还会把片段与源 PDF 的精确可见页文本比较。
若片段首尾落在同一个 ASCII 单词内部（例如固定 `Figu` + 待译 `re…perio` +
固定 `d`），或短 ASCII 片段完全不在可见页文本中，运行会把证据写入
`segment_safety_review.jsonl` 并停止。逐条检查后，用上面的
`confirm-passthrough` 确认清单中全部 ID。v0.3.2 选择原样保留这些对象；完整翻译
需要上游提供相邻对象上下文或合并能力，不能靠提示词猜测。

schema 4 运行清单会记录 PaperLocale 版本、领域包内容哈希、Provider、模型、推理强度、Codex CLI
版本、collect/render 使用的版面引擎版本，以及人工透传映射的哈希。`codex-local`
因此要求显式提供 `--model`；没有填写推理强度时会如实记录为空，不伪造实际设置。

若机器 QA 报告矢量对象减少，报告会列出缺失对象的页码、边界框和面积，并在对照图两侧用红框标出位置。完成独立文件形式的矢量修复后，使用受控导入命令：

```bash
paperlocale apply-vector-repair \
  --run-dir runs/paper \
  --repaired-pdf repaired-paper.pdf \
  --description "恢复第 1 页链接矢量图标"
```

该命令拒绝改变文字、页尺寸或图片数量的候选，备份修复前 PDF 并写入 `repair_history`；导入后必须重新执行 `qa` 和 `accept`。

若逐页视觉复核发现无法在片段层修复的损坏图注，可只替换一个已经人工确认的页面
矩形：

```bash
paperlocale apply-text-repair \
  --run-dir runs/paper \
  --page 27 \
  --rect 40 120 500 160 \
  --replacement "图5 农业干旱评估" \
  --font-file /path/to/NotoSansCJKsc-Regular.otf \
  --font-size 9.5 \
  --description "修复跨对象碎裂图注"
```

坐标采用 PyMuPDF 的 PDF 点坐标（左、上、右、下）。命令会先确认字体包含全部替换
字形和文字能够放入矩形，只删除矩形内文字，并核对页尺寸、图片、链接、既有矢量及
矩形外文字没有受损。外部字体必须在嵌入前独立完成子集化，不会顺带重写 PDF 已有
字体程序；清单记录原字体与子集哈希、子集前后字节、修复前后文字、矩形、PDF 哈希
和旧 PDF 备份。完成后仍必须重新执行 `qa` 和 `accept`。字体须由用户在本机合法
取得，不能提交到仓库；TTF/OTF 通常比字体集合产生更干净的抽取元数据，但任何
CMap 警告和最终视觉效果仍由同一 QA 流程检查。

若损坏对象只是已确认的孤立残片（例如固定首字母与可译对象切分后遗留
`limate`），可显式传入空 replacement：

```bash
paperlocale apply-text-repair \
  --run-dir runs/paper \
  --page 2 \
  --rect 120 29 144 39 \
  --replacement '' \
  --description "删除已核对的孤立英文残片"
```

只删除模式不需要 `--font-file` 或 `--font-size`，不会嵌入空字体子集；
清单以 `text-removal` 独立记录。只包含空格的 replacement 仍会被拒绝。

需要逐阶段控制时，仍可使用同一生产路径的 `init-run -> collect -> reference-review/confirm-references -> 可选 confirm-passthrough -> translate -> validate -> render -> qa -> accept` 命令序列。

## 领域包扩展

领域包是一个不执行代码的目录，包含：

```text
your-domain/
├── manifest.json
├── prompt.txt
├── glossary.tsv
└── eval_cases.jsonl
```

复制 `src/paperlocale/packs/atmospheric-science/` 后修改内容，再运行：

```bash
paperlocale domain-check /path/to/your-domain
```

四个文件的原始字节与文件名共同生成内容 SHA-256；只修改内容而不提高版本号，也不会绕过运行清单的续跑身份检查。字段、门禁和贡献要求见 [领域包指南](docs/DOMAIN_PACKS.zh-CN.md)。实施进度见 [路线图](docs/ROADMAP.md)，申请证据边界见 [Codex for Open Source 准备清单](docs/CODEX_FOR_OSS_READINESS.zh-CN.md) 与 [申请草稿](docs/CODEX_FOR_OSS_APPLICATION_DRAFT.zh-CN.md)，安全与订阅边界见 [安全策略](SECURITY.md)。

## 引用与社区规范

在研究或教学中使用本项目时，可通过 [`CITATION.cff`](CITATION.cff) 获取机器可读
引用元数据；合并到默认分支后，GitHub 仓库侧栏也会显示 “Cite this repository”。
参与 Issue、PR 和评审须遵守 [PaperLocale Code of Conduct](CODE_OF_CONDUCT.md)。

## 当前证据边界

- 94 项单元测试不联网运行，覆盖内容合同、三种 Provider、ChatGPT 网页人工桥接、单片段批处理边界、Provider 评估、一键断点续跑、参考文献与透传人工确认映射、碎词安全审查、领域包身份、PDF 哈希绑定、图片/矢量对象门禁、受控文字修复、页面 QA、隔离环境入口和演示产物；
- 本地已用 `pdf2zh-next 2.9.0` 跑通合成 A4 双栏 PDF 的收集、查表重建和逐页 QA；
- 版面兼容性夹具包含公式占位、矢量表格与嵌入图片；源文/译文均为 1 个图片对象和 8 次矢量绘制；
- v0.3.3 两页中文文字修复夹具将 23,278,008 字节原始字体独立子集化为 22,912 字节，最终 PDF 为 19,594 字节；机器 QA 为 0 errors / 0 warnings，并已逐页视觉验收。两类夹具均为项目自有，不提交任何受版权限制的论文；
- “保版”不等于像素完全一致。中文长度变化会改变行内断行，因此最终候选必须人工逐页核对。

## 开发与版面兼容性检查

单元测试不调用模型，也不要求安装版面引擎：

```bash
python -m pip install -e ".[test]"
python -m unittest discover -s tests -v
```

上游版面引擎升级后，用自有合成 PDF 跑完整闭环：

```bash
python -m pip install -e ".[layout,test]"
python scripts/layout_smoke.py \
  --output tmp/layout-smoke-001 \
  --demo-gif tmp/layout-smoke-001.gif
```

脚本不会自动执行 `accept`。机器检查通过后仍应打开它打印的逐页对照图。
每周兼容性工作流会安装依赖范围内最新的 `pdf2zh-next`，重复运行这条真实 CLI 闭环；这是对上游接口假设的持续验证，不代表上游已经作出稳定性承诺。

## 参与贡献

可以从已有的 [good first issues](https://github.com/hazugi2004/paperlocale/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22) 开始，或阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。当前入口包括生态学领域包、Ubuntu 安装复核和大气科学 Provider 评估独立复核。
