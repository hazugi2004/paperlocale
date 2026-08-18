# PaperLocale 采用证据台账

本文件只记录可以由公开链接复核的真实采用，不把维护者自己创建的测试 Issue、下载或 Star 当作外部用户证据。

## 当前状态

项目已于 2026-08-18 发布 [GitHub v0.1.0](https://github.com/hazugi2004/paperlocale/releases/tag/v0.1.0)，并由维护者创建三个 good first issues 作为贡献入口。它们证明项目已经公开并可参与，但不属于外部采用证据。

同日的发布后审计发现 API 明文传输与 QA 后候选替换边界，并发布了
[v0.1.1](https://github.com/hazugi2004/paperlocale/releases/tag/v0.1.1)。该记录证明维护者能够发现问题、增加回归测试并交付补丁版本，但时间跨度仍不足以证明长期维护。

维护者还在 PDFMathTranslate-next 上游发布了
[CLITranslator 两阶段集成讨论 #354](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/issues/354)。它证明上游沟通已经开始；在上游维护者或其他参与者回复前，它仍不构成外部认可或采用。

非维护者 `icecold009` 已公开 fork 仓库，并针对 good first issue #2 提交
[两页 PDF QA 回归 PR #4](https://github.com/hazugi2004/paperlocale/pull/4)。维护者已在隔离工作树中将该提交与最新 `main` 组合验证，36 项测试全部通过，并公开提交批准评审和[最新主线兼容性说明](https://github.com/hazugi2004/paperlocale/pull/4#issuecomment-5330379908)；GitHub 的[首次贡献者 CI](https://github.com/hazugi2004/paperlocale/actions/runs/32150697274)也已通过。该 PR 仍由作者标记为 Draft，尚未合并，因此它可以证明贡献入口已吸引首个外部参与者，但不能写成已采用、已发布或已合并贡献。

截至目前，尚无可验证的非维护者真实翻译反馈、外部问题报告、下游引用或 PyPI 下载数据。

## 记录格式

公开后按下表追加，不回填无法核验的数据：

| 日期 | 证据类型 | 公开链接 | 能证明什么 | 是否为外部贡献者 |
|---|---|---|---|---|
| 2026-08-18 | 维护者补丁 Release | [v0.1.1](https://github.com/hazugi2004/paperlocale/releases/tag/v0.1.1) | 发布后审计、27 项测试、远端构建 | 否 |
| 2026-08-18 | 维护者发起的上游讨论 | [PDFMathTranslate-next #354](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/issues/354) | 两阶段 CLITranslator 接口稳定性与文档协作请求 | 否，等待外部回复 |
| 2026-08-18 | 外部 fork 与 Draft PR | [PR #4](https://github.com/hazugi2004/paperlocale/pull/4) / [CI](https://github.com/hazugi2004/paperlocale/actions/runs/32150697274) | 非维护者响应 good first issue；贡献分支 CI 通过，维护者将其与最新主线组合验证 36 项测试并批准 | 是，尚未合并 |
| 待记录 | 真实试用反馈 / Issue / 引用 / 下游项目 / 下载统计 | 待记录 | 待记录 | 是 / 否 |

## 可接受证据

- 非维护者提交的可复现问题或功能请求；
- 被合并的外部 PR 或新领域包；
- 公开下游项目、论文方法附录、课程或工具清单中的引用；
- GitHub Release 下载与 PyPI 下载的可核验统计；
- 与 BabelDOC/PDFMathTranslate-next 等上游项目的公开 Issue 或 PR。
