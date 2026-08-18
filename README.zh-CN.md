# PaperLocale

[![tests](https://github.com/hazugi2004/paperlocale/actions/workflows/tests.yml/badge.svg)](https://github.com/hazugi2004/paperlocale/actions/workflows/tests.yml)
[![release](https://img.shields.io/github/v/release/hazugi2004/paperlocale)](https://github.com/hazugi2004/paperlocale/releases/latest)
[![license](https://img.shields.io/github/license/hazugi2004/paperlocale)](LICENSE)

PaperLocale 是一个面向学术论文的可验证保版翻译工具。它的目标不是笼统承诺“原格式完全不变”，而是在明确、可测试的边界内保留页数、页面尺寸、栏位、图片、表格和公式，并在科学信息被破坏时拒绝生成候选 PDF。

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
- `atmospheric-science`：内置 17 条大气科学固定术语和 5 个回归案例；
- `collect -> translate -> validate -> render -> qa -> accept`：有清单、有断点、不可跳步的单向工作流；
- 页数、MediaBox、CropBox、图片对象、空白页和内部占位符机器检查；
- 所有页面的原文—译文并排 PNG，必须显式记录人工视觉验收。

项目不会读取、复制或记录 `~/.codex/auth.json`。Codex 会员额度不会被包装成公共 API。

## 领域包

首个内置领域包为 `atmospheric-science`，包括固定术语、保护规则、翻译约束和回归案例。后续医学、地学、生态学等领域只需新增领域包，不修改翻译核心。

## 安装

需要 Python 3.10–3.13。完整 PDF 流程还需要 Poppler 的 `pdftoppm`：macOS 可执行 `brew install poppler`，Ubuntu 可执行 `sudo apt-get install poppler-utils`。

```bash
git clone https://github.com/hazugi2004/paperlocale.git
cd paperlocale
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[layout]"
paperlocale domain-check atmospheric-science
```

当前验证兼容 `pdf2zh-next 2.9.0`。版面依赖较多，所以被放在可选的 `layout` 依赖组中。

## 五分钟开始翻译

先创建一次绑定源 PDF SHA-256 的运行：

```bash
paperlocale init-run paper.pdf --run-dir runs/paper
paperlocale collect --run-dir runs/paper
```

选择以下一种 Provider。使用本机 Codex 登录态：

```bash
codex login
paperlocale translate \
  --run-dir runs/paper \
  --provider codex-local \
  --domain atmospheric-science
```

或者使用自备密钥的 OpenAI-compatible 接口。`--base-url` 应包含服务的 API 版本前缀，但不要包含 `/chat/completions`：

```bash
export PAPERLOCALE_API_KEY="你的密钥"
paperlocale translate \
  --run-dir runs/paper \
  --provider openai-compatible \
  --base-url https://api.example.com/v1 \
  --model your-model \
  --domain atmospheric-science
```

远程端点必须使用 HTTPS，避免 API Key 经明文网络发送。只有
`localhost`、`127.0.0.1` 和 `::1` 等本机 loopback 服务可以使用 HTTP。

然后执行内容门禁、PDF 重建和全页 QA：

```bash
paperlocale validate --run-dir runs/paper --domain atmospheric-science
paperlocale render --run-dir runs/paper
paperlocale qa --run-dir runs/paper
```

打开 `runs/paper/qa/comparisons/` 逐页核对。确认页面结构、表格、图片、公式和中文断行可接受后，记录验收：

```bash
paperlocale accept --run-dir runs/paper --reviewed-by "你的名字"
paperlocale status --run-dir runs/paper
```

运行中断后，重新执行当前阶段即可：已通过门禁的译文会从 `translations.jsonl` 复用，不会重复消耗额度。项目不会在同一次运行中静默切换 Provider。

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

字段、门禁和贡献要求见 [领域包指南](docs/DOMAIN_PACKS.zh-CN.md)。实施进度见 [路线图](docs/ROADMAP.md)，申请证据边界见 [Codex for Open Source 准备清单](docs/CODEX_FOR_OSS_READINESS.zh-CN.md) 与 [申请草稿](docs/CODEX_FOR_OSS_APPLICATION_DRAFT.zh-CN.md)，安全与订阅边界见 [安全策略](SECURITY.md)。

## 当前证据边界

- 27 项单元测试不联网运行，覆盖内容合同、Provider、断点、PDF 哈希绑定和页面 QA；
- 本地已用 `pdf2zh-next 2.9.0` 跑通合成 A4 双栏 PDF 的收集、查表重建和逐页 QA；
- 合成页包含公式占位、矢量表格与嵌入图片，不提交任何受版权限制的论文；
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
python scripts/layout_smoke.py --output tmp/layout-smoke-001
```

脚本不会自动执行 `accept`。机器检查通过后仍应打开它打印的逐页对照图。
