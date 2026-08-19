# Codex for Open Source 申请准备

核验日期：2026-08-19。

## 官方条件与权益

OpenAI [Codex for Open Source 官方页面](https://developers.openai.com/community/codex-for-oss)
列出的支持方向包括：

- 6 个月 ChatGPT Pro with Codex；
- 按项目情况提供 Codex Security；
- 可用于 PR 审查、维护自动化、发布工作流等核心开源工作的 API credits。

官方建议核心维护者或广泛使用的公开项目申请；尚不完全符合条件但对生态具有重要作用的项目，也可以申请并解释其价值。[Program Terms](https://learn.chatgpt.com/codex/codex-for-oss-terms)
同时说明：获批者可能获得上述一项或多项权益，具体可用性、范围、时长和时间由
OpenAI 决定；申请不保证入选。评估可考虑仓库使用情况、生态重要性、活跃维护
证据、维护者角色或权限以及计划容量。申请者还需要有效 ChatGPT 账号，并准确、
完整地提供本人、仓库和维护角色信息。正式入口为
[Apply today](https://openai.com/form/codex-for-oss/)；不要在材料中提交机密信息。

## PaperLocale 当前证据

| 要求或说服力 | 当前证据 | 状态 |
|---|---|---|
| 明确的公共开源价值 | 学术 PDF 保版翻译、科学信息硬门禁、可扩展领域术语包 | 已实现 |
| 核心维护者与写权限 | `MAINTAINERS.md` 与 GitHub `hazugi2004/paperlocale` 管理权限 | 已验证 |
| 可运行项目 | 一键断点续跑 CLI、两种 Provider、Provider 评估、参考文献人工确认、单向状态机、54 项不联网测试 | [v0.3.1 PR #11](https://github.com/hazugi2004/paperlocale/pull/11) 的 Python 3.10–3.13 矩阵及 `[layout]` 依赖求解门禁已通过 |
| 可复现质量证据 | 合成双栏 PDF、公式合同、图片与矢量绘图硬门禁、逐页 QA；[v0.3.1 永久审计附件](https://github.com/hazugi2004/paperlocale/releases/download/v0.3.1/paperlocale-v0.3.1-layout-compatibility.zip)保留清单、参考文献映射、日志、对照图与演示 GIF | 已实现，附件 SHA-256 为 `cac1e404b2317316551c98c060ca3649fdd4353f58502bb04442799fcba6781f` |
| 翻译质量证据 | `codex-local` 对大气科学包 5/5 内容合同通过；候选、参考、[逐条复核工作表](evidence/codex-local-atmospheric-science-review.zh-CN.md)与 [Issue #5](https://github.com/hazugi2004/paperlocale/issues/5) 公开 | 初步证据，等待非维护者领域人员提交复核 |
| 可公开演示 | 自有合成论文的原文、译文与全页 QA GIF | 已实现 |
| 发布与维护机制 | CHANGELOG、安全策略、Issue/PR 模板、测试、标签构建和上游兼容性工作流 | v0.1.0–v0.3.1、[v0.3.1 标签构建](https://github.com/hazugi2004/paperlocale/actions/runs/32221420406)与[标签级真实兼容性运行](https://github.com/hazugi2004/paperlocale/actions/runs/32221715819)已通过 |
| 公开版本 | [GitHub v0.3.1](https://github.com/hazugi2004/paperlocale/releases/tag/v0.3.1) | 已发布；确切 wheel 的 `[layout]` 求解通过，wheel 与 sdist 附件哈希已复核 |
| 外部贡献 | 非维护者 fork 与 [两页 PDF QA Draft PR #4](https://github.com/hazugi2004/paperlocale/pull/4)；[贡献分支 CI](https://github.com/hazugi2004/paperlocale/actions/runs/32150697274)及其与最新主线组合验证的 36 项测试均通过，维护者已批准 | 初步证据，等待作者标记 Ready 并合并 |
| 广泛使用或真实用户采用 | 尚无非维护者真实翻译反馈、问题报告或下游引用 | 未证明 |
| 上游生态协作 | [PDFMathTranslate-next #354](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/issues/354) 与 [v0.2.0 证据跟进](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/issues/354#issuecomment-5330814545) 已发出 | 已持续跟进，尚无外部回应 |

在 #354 获得回复前，项目临时按当前 `CLITranslator` 契约继续开发，并由每周真实版面冒烟工作流验证依赖范围内最新 2.x 版本。该策略降低等待成本，但不等同于上游兼容性承诺。

## 申请判断

项目现在具备“可以公开试用和持续维护”的工程基础，并出现首个可复核的外部贡献；
活跃维护、角色权限和生态价值已有公开证据，但仓库使用量仍弱：0 star、1 个外部
fork、v0.3.1 附件尚无公开下载，且没有非维护者真实翻译反馈或下游引用。因此还
不能诚实声称“广泛使用”。应推动 PR #4 完成并继续获得真实试用证据，再提交更
有说服力的申请；如果提前申请，应明确项目尚新，并重点解释科学文献翻译的生态
价值，不能把维护者自测、自己的 Issue 或尚未合并的 Draft PR 写成真实用户采用。

## 发布后四步

1. ~~发布 Public GitHub 仓库和 `v0.1.0`，确认 GitHub Actions 全绿；~~
2. ~~创建 2–3 个有明确验收标准的 `good first issue`，邀请领域包和兼容性贡献；~~
3. ~~获得首个非维护者 fork 与可复核 PR；~~
4. 推动外部 PR 完成合并，并用可公开的合成 PDF 或开放许可论文收集首批真实试用反馈；
5. 获得第二类独立证据后更新采用台账并申请。

可编辑的英文回答见 [CODEX_FOR_OSS_APPLICATION_DRAFT.zh-CN.md](CODEX_FOR_OSS_APPLICATION_DRAFT.zh-CN.md)。
尚未发送的上游讨论与中英文社区发布文本见
[COMMUNITY_OUTREACH_DRAFTS.zh-CN.md](COMMUNITY_OUTREACH_DRAFTS.zh-CN.md)；只有公开发出并收到非维护者回应后，才能形成外部采用证据。
