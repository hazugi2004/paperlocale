# Codex for Open Source 申请草稿

状态：可编辑草稿，尚不建议把“广泛使用”作为申请理由。

本文件不保存邮箱、ChatGPT 账号或其他私人申请信息。提交时只在 OpenAI 官方表单中填写这些内容。

## 项目与维护者

- Project: PaperLocale
- Repository: https://github.com/hazugi2004/paperlocale
- Core maintainer: `hazugi2004`
- Role evidence: 仓库管理员、提交者和 `v0.1.0` 发布者
- License: AGPL-3.0-only

## 英文项目简介

> PaperLocale is an open-source, layout-preserving translation workflow for academic PDFs. It separates PDF layout reconstruction from model translation, supports an authenticated local Codex subscription as well as BYOK OpenAI-compatible APIs, and rejects translations that lose formulas, rich-text placeholders, numbers, units, abbreviations, URLs, DOIs, or required domain terminology. Its first domain pack covers atmospheric science and can be extended without changing the translation core. Every candidate PDF is checked for page geometry, image objects, blank pages, and unresolved placeholders, rendered page by page, and left unaccepted until a human reviews all side-by-side comparisons. The repository distributes only project-generated synthetic fixtures, not copyrighted papers or full translations.

## 为什么对开源生态有价值

> Scientific PDF translation often forces researchers to choose between readable Chinese text and preservation of formulas, tables, figures, and page structure. PaperLocale turns that problem into an auditable pipeline with stable intermediate files, resumable model calls, explicit failure states, domain terminology packs, and reproducible PDF QA. It is particularly useful for researchers and students who need to read technical literature but cannot safely trust an opaque one-click translation result.

## 希望如何使用计划权益

> ChatGPT Pro with Codex would support day-to-day maintenance, issue triage, provider compatibility work, test expansion, documentation, and review of contributed domain packs. API credits would be used only for reproducible OSS maintainer workflows such as evaluating translation contracts and reviewing release changes; local Codex authentication would never be exposed as a public API. If granted, Codex Security would be used to review the CLI bridge, subprocess boundaries, credential handling, and release workflow.

## 已有证据链接

- Release: https://github.com/hazugi2004/paperlocale/releases/tag/v0.1.0
- CI: https://github.com/hazugi2004/paperlocale/actions/workflows/tests.yml
- Security policy: https://github.com/hazugi2004/paperlocale/security/policy
- Good first issues: https://github.com/hazugi2004/paperlocale/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22
- Architecture: https://github.com/hazugi2004/paperlocale/blob/main/docs/ARCHITECTURE.md
- Adoption ledger: https://github.com/hazugi2004/paperlocale/blob/main/docs/ADOPTION_EVIDENCE.zh-CN.md

## 提交前仍需补强

以下任意两类真实证据出现后，申请说服力会显著提高：

1. 至少一个非维护者提交的可复现 Issue、试用反馈或 PR；
2. 一个被合并的新领域包或兼容性修复；
3. 一个公开下游使用、课程、研究方法说明或工具清单引用；
4. 与 BabelDOC/PDFMathTranslate-next 的公开 Issue 或 PR；
5. 第二个经过 CI 和变更记录验证的 Release。

如果在这些证据出现前申请，应明确写“new public project”，并依照官方说明解释其生态重要性，不能把维护者创建的 Issue、测试调用或自己的 Star 描述为外部采用。
