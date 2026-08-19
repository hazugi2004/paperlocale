# PaperLocale 采用证据台账

核验日期：2026-08-19。

本文件只记录可以由公开链接复核的真实采用，不把维护者自己创建的测试 Issue、下载或 Star 当作外部用户证据。

## 当前状态

项目已于 2026-08-18 发布 [GitHub v0.1.0](https://github.com/hazugi2004/paperlocale/releases/tag/v0.1.0)，并由维护者创建三个 good first issues 作为贡献入口。它们证明项目已经公开并可参与，但不属于外部采用证据。

同日的发布后审计发现 API 明文传输与 QA 后候选替换边界，并发布了
[v0.1.1](https://github.com/hazugi2004/paperlocale/releases/tag/v0.1.1)。该记录证明维护者能够发现问题、增加回归测试并交付补丁版本，但时间跨度仍不足以证明长期维护。

随后发布的 [v0.2.0](https://github.com/hazugi2004/paperlocale/releases/tag/v0.2.0) 增加一键断点续跑、图片与矢量绘图硬门禁、真实 Provider 领域评估及 37 项测试。它证明用户可见功能已经进入正式发行，但同日版本迭代仍不能替代跨周维护或真实用户采用。

[v0.3.0](https://github.com/hazugi2004/paperlocale/releases/tag/v0.3.0) 根据本地真实 13 页论文试运行增加可审计参考文献确认、Provider/模型/版面引擎溯源、批内部分成功恢复、矢量缺失定位和受控修复，并由 53 项测试覆盖 Python 3.10–3.13。标签构建完成元数据检查和确切 wheel 安装，公开附件哈希已重新下载复核。该版本证明维护者能把真实边界转化为通用发布能力，但仍属于维护者证据，不是外部采用。

发布后的干净 Runner 检查发现 v0.3.0 的 `PyMuPDF>=1.26` 与 `pdf2zh-next 2.9.x` 约束冲突。维护者先公开保留[失败运行](https://github.com/hazugi2004/paperlocale/actions/runs/32219957135)，再通过 [PR #10](https://github.com/hazugi2004/paperlocale/pull/10) 修复依赖和增加独立 `[layout]` 求解门禁，并发布 [v0.3.1](https://github.com/hazugi2004/paperlocale/releases/tag/v0.3.1)。[v0.3.1 标签兼容性运行](https://github.com/hazugi2004/paperlocale/actions/runs/32221715819)完成真实两阶段版面流程且 QA 错误为 0；[标签构建](https://github.com/hazugi2004/paperlocale/actions/runs/32221420406)从确切 wheel 复核了 `[layout]`。公开重新下载附件的 SHA-256 为 `3c590e16a62419e07025f6f4ccc246d029f8345de2fff7a17ca2069c47aef873`（wheel）和 `f91b41a736944cde17fdc2386115fd807bee6a139180085aaaa5dec8746bd355`（sdist）。原始标签兼容性审计产物另以[永久 Release 附件](https://github.com/hazugi2004/paperlocale/releases/download/v0.3.1/paperlocale-v0.3.1-layout-compatibility.zip)保存，SHA-256 为 `cac1e404b2317316551c98c060ca3649fdd4353f58502bb04442799fcba6781f`。这些仍是维护者维护能力与可复现性证据，不是外部采用。

[v0.3.2](https://github.com/hazugi2004/paperlocale/releases/tag/v0.3.2) 根据第二次
本地 27 页、79 片段真实论文试运行，把行号参考文献识别、非正文人工透传、碎词
安全审查和 schema 4 溯源分别通过 [PR #17](https://github.com/hazugi2004/paperlocale/pull/17)、
[#18](https://github.com/hazugi2004/paperlocale/pull/18) 与
[#19](https://github.com/hazugi2004/paperlocale/pull/19) 交付，并由 63 项测试、
[标签构建](https://github.com/hazugi2004/paperlocale/actions/runs/32234314229) 和
[公开版面兼容性运行](https://github.com/hazugi2004/paperlocale/actions/runs/32234356256)
验证。[永久审计附件](https://github.com/hazugi2004/paperlocale/releases/download/v0.3.2/paperlocale-v0.3.2-layout-compatibility.zip)
SHA-256 为 `3f50a2a3adfe5b66d037cb905827b0d8c5ba9d6bc9307908223b77628ec61dc7`。
真实论文及完整译本未上传或分发；该证据仍是维护者测试，不是外部用户采用。

维护者还在 PDFMathTranslate-next 上游发布了
[CLITranslator 两阶段集成讨论 #354](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/issues/354)，并在 v0.2.0 发布后补充了[真实兼容性与标签构建证据](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/issues/354#issuecomment-5330814545)。它证明上游沟通和可复现跟进已经开始；在上游维护者或其他参与者回复前，它仍不构成外部认可或采用。

针对同文返回仍触发重排的 BabelDOC 核心错误，维护者另提交了
[Issue #610](https://github.com/funstory-ai/BabelDOC/issues/610) 和最小修复及回归
测试 [PR #611](https://github.com/funstory-ai/BabelDOC/pull/611)。本地测试和 Ruff
通过，但上游首次 fork Actions 仍等待维护者批准运行；在获得审查或合并前，它只
证明主动上游协作，不属于外部认可。

非维护者 `icecold009` 已公开 fork 仓库，并针对 good first issue #2 提交
[两页 PDF QA 回归 PR #4](https://github.com/hazugi2004/paperlocale/pull/4)。维护者已在隔离工作树中将该提交与最新 `main` 组合验证，36 项测试全部通过，并公开提交批准评审和[最新主线兼容性说明](https://github.com/hazugi2004/paperlocale/pull/4#issuecomment-5330379908)；GitHub 的[首次贡献者 CI](https://github.com/hazugi2004/paperlocale/actions/runs/32150697274)也已通过。该 PR 仍由作者标记为 Draft，尚未合并，因此它可以证明贡献入口已吸引首个外部参与者，但不能写成已采用、已发布或已合并贡献。

截至目前，尚无可验证的非维护者真实翻译反馈、外部问题报告、下游引用或 PyPI 下载数据。v0.3.1 与 v0.3.2 各三个 Release 附件都只有维护者公开重下载复核期间产生的 1 次下载，不能据此声称外部采用。

## 记录格式

公开后按下表追加，不回填无法核验的数据：

| 日期 | 证据类型 | 公开链接 | 能证明什么 | 是否为外部贡献者 |
|---|---|---|---|---|
| 2026-08-18 | 维护者补丁 Release | [v0.1.1](https://github.com/hazugi2004/paperlocale/releases/tag/v0.1.1) | 发布后审计、27 项测试、远端构建 | 否 |
| 2026-08-18 | 功能 Release | [v0.2.0](https://github.com/hazugi2004/paperlocale/releases/tag/v0.2.0) | 一键运行、结构门禁、Provider 评估、37 项测试与发行构建 | 否 |
| 2026-08-19 | 功能 Release | [v0.3.0](https://github.com/hazugi2004/paperlocale/releases/tag/v0.3.0) / [构建](https://github.com/hazugi2004/paperlocale/actions/runs/32219122015) | 真实试运行反向校准、参考文献审计、53 项测试、发行附件哈希 | 否 |
| 2026-08-19 | 维护者兼容性热修复 | [v0.3.1](https://github.com/hazugi2004/paperlocale/releases/tag/v0.3.1) / [构建](https://github.com/hazugi2004/paperlocale/actions/runs/32221420406) / [标签兼容性运行](https://github.com/hazugi2004/paperlocale/actions/runs/32221715819) / [永久审计附件](https://github.com/hazugi2004/paperlocale/releases/download/v0.3.1/paperlocale-v0.3.1-layout-compatibility.zip) | 公开保留失败证据后修复 `[layout]` 安装；54 项测试、确切 wheel 依赖求解、真实版面 QA 0 错误、附件哈希复核 | 否 |
| 2026-08-19 | 功能 Release | [v0.3.2](https://github.com/hazugi2004/paperlocale/releases/tag/v0.3.2) / [构建](https://github.com/hazugi2004/paperlocale/actions/runs/32234314229) / [兼容性运行](https://github.com/hazugi2004/paperlocale/actions/runs/32234356256) / [永久附件](https://github.com/hazugi2004/paperlocale/releases/download/v0.3.2/paperlocale-v0.3.2-layout-compatibility.zip) | 第二次真实试运行反向校准、schema 4、人工透传、碎词安全审查、63 项测试与公开附件哈希 | 否 |
| 2026-08-18 | 维护者发起的上游讨论 | [PDFMathTranslate-next #354](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/issues/354) | 两阶段 CLITranslator 接口稳定性与文档协作请求 | 否，等待外部回复 |
| 2026-08-19 | 维护者上游跟进 | [v0.2.0 兼容性证据](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/issues/354#issuecomment-5330814545) | 真实兼容性运行、标签构建和结构 QA 证据 | 否，等待外部回复 |
| 2026-08-19 | 维护者上游修复 | [BabelDOC #610](https://github.com/funstory-ai/BabelDOC/issues/610) / [PR #611](https://github.com/funstory-ai/BabelDOC/pull/611) | 同文返回保持原版面对象的最小修复和回归测试；三条自动审查意见已处理 | 否，等待上游维护者审查 |
| 2026-08-18 | 外部 fork 与 Draft PR | [PR #4](https://github.com/hazugi2004/paperlocale/pull/4) / [CI](https://github.com/hazugi2004/paperlocale/actions/runs/32150697274) | 非维护者响应 good first issue；贡献分支 CI 通过，维护者将其与最新主线组合验证 36 项测试并批准 | 是，尚未合并 |
| 待记录 | 真实试用反馈 / Issue / 引用 / 下游项目 / 下载统计 | 待记录 | 待记录 | 是 / 否 |

## 可接受证据

- 非维护者提交的可复现问题或功能请求；
- 被合并的外部 PR 或新领域包；
- 公开下游项目、论文方法附录、课程或工具清单中的引用；
- GitHub Release 下载与 PyPI 下载的可核验统计；
- 与 BabelDOC/PDFMathTranslate-next 等上游项目的公开 Issue 或 PR。
