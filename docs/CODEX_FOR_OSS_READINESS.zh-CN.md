# Codex for Open Source 申请准备

核验日期：2026-08-18。

## 官方条件与权益

OpenAI 官方页面说明，该计划面向开源维护者，权益包括：

- 6 个月 ChatGPT Pro with Codex；
- 按项目情况提供 Codex Security；
- 可用于 PR 审查、维护自动化、发布工作流等核心开源工作的 API credits。

官方建议核心维护者或广泛使用的公开项目申请；尚不完全符合条件但对生态具有重要作用的项目，也可以申请并解释其价值。申请入口和最新条款以 [Codex for Open Source 官方页面](https://learn.chatgpt.com/community/codex-for-oss) 为准。

## PaperLocale 当前证据

| 要求或说服力 | 当前证据 | 状态 |
|---|---|---|
| 明确的公共开源价值 | 学术 PDF 保版翻译、科学信息硬门禁、可扩展领域术语包 | 已实现 |
| 核心维护者与写权限 | `MAINTAINERS.md` 与 GitHub `hazugi2004/paperlocale` 管理权限 | 已验证 |
| 可运行项目 | CLI、两种 Provider、单向状态机、27 项不联网测试 | 已实现 |
| 可复现质量证据 | 合成双栏 PDF、公式/表格/图片回归、逐页 QA | 已实现 |
| 发布与维护机制 | CHANGELOG、安全策略、Issue/PR 模板、测试和标签构建工作流 | v0.1.0 与 v0.1.1 远端 CI 已通过 |
| 公开版本 | [GitHub v0.1.1](https://github.com/hazugi2004/paperlocale/releases/tag/v0.1.1) | 已发布 |
| 广泛使用或外部采用 | 尚无真实外部用户、Issue、PR 或下游引用 | 未证明 |
| 上游生态协作 | 尚无公开的 BabelDOC/PDFMathTranslate-next Issue 或 PR | 未证明 |

## 申请判断

项目现在具备“可以公开试用和持续维护”的工程基础，但还不能诚实声称“广泛使用”。应先获得真实采用证据，再提交更有说服力的申请；如果提前申请，应明确项目尚新，并重点解释科学文献翻译的生态价值，不能把维护者自测或自行创建的 Issue 写成外部采用。

## 发布后四步

1. ~~发布 Public GitHub 仓库和 `v0.1.0`，确认 GitHub Actions 全绿；~~
2. ~~创建 2–3 个有明确验收标准的 `good first issue`，邀请领域包和兼容性贡献；~~
3. 用可公开的合成 PDF 或开放许可论文收集首批非维护者反馈；
4. 在本项目和上游留下可复核的 Issue/PR 记录，再更新采用证据台账并申请。

可编辑的英文回答见 [CODEX_FOR_OSS_APPLICATION_DRAFT.zh-CN.md](CODEX_FOR_OSS_APPLICATION_DRAFT.zh-CN.md)。
尚未发送的上游讨论与中英文社区发布文本见
[COMMUNITY_OUTREACH_DRAFTS.zh-CN.md](COMMUNITY_OUTREACH_DRAFTS.zh-CN.md)；只有公开发出并收到非维护者回应后，才能形成外部采用证据。
