# PaperLocale 社区沟通草稿

状态：上游讨论已于 2026-08-18 发布为
[PDFMathTranslate-next #354](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/issues/354)；中英文社区发布仍为未发送草稿。维护者自己的帖子不算外部采用，只有非维护者回应、Issue、PR 或下游使用才能形成对应证据。

## PDFMathTranslate-next / BabelDOC 上游讨论

建议标题：

> Feedback requested: two-pass CLITranslator integration with hash-bound PDF QA

建议正文：

> Hi maintainers, I have released [PaperLocale](https://github.com/hazugi2004/paperlocale), an AGPL-3.0 academic PDF translation workflow built around PDFMathTranslate-next/BabelDOC.
>
> PaperLocale uses `CLITranslator` in two deterministic passes:
>
> 1. `collect` records each source segment under a normalized SHA-256 ID and returns the source unchanged;
> 2. a selected provider translates and validates all segments outside the layout engine;
> 3. `lookup` reruns the same layout command and returns only validated translations for exact source IDs.
>
> This lets a failed model call resume without rerunning layout work and prevents rendering when formulas, rich-text placeholders, numbers, units, URLs, DOIs, or required domain terms are missing. The rendered PDF and QA report are also SHA-256 bound before human acceptance.
>
> The current integration was verified with `pdf2zh-next 2.9.0` and BabelDOC `0.6.4` using `--pool-max-workers 1`, `--ignore-cache`, `--no-dual`, and a project-generated double-column PDF containing formulas, a vector table, and an image object.
>
> I would appreciate feedback on two questions:
>
> - Is the current `--clitranslator-command` stdin/stdout contract intended to remain a supported integration surface?
> - Would a small upstream documentation example for collect-then-lookup workflows be useful?
>
> I am not asking the upstream project to support PaperLocale-specific validation rules; I mainly want to avoid relying on accidental CLI behavior. I can prepare a focused documentation PR if this pattern fits the project direction.

目标仓库未启用 GitHub Discussions，但提供专门的 “Discussion Issue” 模板；发布前已检索 `CLITranslator`、`clitranslator-command` 和 collect/lookup，未发现重复项。

## 中文社区发布

建议标题：

> 开源了 PaperLocale：保留公式、表格、图片和页面结构的学术 PDF 可验证翻译流程

建议正文：

> 我发布了一个面向学术论文的开源工具 PaperLocale：
> https://github.com/hazugi2004/paperlocale
>
> 它不是简单把整篇 PDF 丢给模型，而是把流程拆成片段收集、翻译、科学信息门禁、PDF 重建、逐页 QA 和人工验收。当前支持本机 Codex 登录态与自备密钥的 OpenAI-compatible 接口，内置大气科学术语包。
>
> 重点保护公式占位符、富文本标签、数字、单位、缩写、URL、DOI 和固定专业术语；源 PDF、候选 PDF 与 QA 报告通过 SHA-256 绑定。候选文件在 QA 后被替换时，验收会直接失败。
>
> 当前 `main` 基于 v0.1.1，已有 35 项测试覆盖 Python 3.10–3.13，并提供一键断点续跑到机器 QA 的命令；图片或矢量绘图少于原文时 QA 会失败。真实版面冒烟包含双栏、公式、矢量表格和嵌入图片，并提供由自有合成论文生成的演示 GIF。项目不会分发受版权限制的论文或完整译本。
>
> 目前最需要的不是单纯 Star，而是实际试用反馈、Linux 安装复核、新领域术语包和可复现的版面问题。仓库中已经准备了 3 个 good first issues。

## 英文社区发布

> I released [PaperLocale](https://github.com/hazugi2004/paperlocale), an open-source, layout-preserving academic PDF translation workflow.
>
> It supports an authenticated local Codex session and BYOK OpenAI-compatible endpoints, while enforcing hard gates for formulas, style placeholders, numbers, units, abbreviations, URLs, DOIs, and domain terminology. The source PDF, rendered candidate, and QA report are SHA-256 bound, and every page must be reviewed before acceptance.
>
> v0.1.1 has 27 model-free tests across Python 3.10–3.13 and a real PDFMathTranslate-next/BabelDOC smoke path covering two columns, formulas, a vector table, and an embedded image. No copyrighted research paper or full translation is distributed.
>
> I am looking for reproducible feedback, Linux installation verification, and contributors for additional scientific domain packs. Three scoped good first issues are open.

## 发送后记录

每次发布只记录以下可核验事实：目标社区、公开链接、日期、是否收到非维护者回复，以及回复是否形成 Issue、PR 或下游使用。浏览量、维护者自己的点赞和维护者自己的 Star 不作为采用证据。
