# Changelog

本项目遵循语义化版本。未发布内容先进入 `Unreleased`。

## 0.1.1 - 2026-08-18

### Security

- 远程 OpenAI-compatible 端点现在强制使用 HTTPS；仅 loopback 主机可使用 HTTP；
- 拒绝在 Provider URL 中嵌入凭据、查询参数、片段或重复的 `/chat/completions`；
- 渲染清单和 QA 报告绑定源 PDF 与译文 PDF 的 SHA-256；候选在 QA 后被替换时拒绝人工验收。

### Changed

- 运行清单升级到 schema 2；v0.1.0 的旧运行可通过重新执行 `qa` 安全升级；
- 单元测试增加到 27 项，并覆盖正确验收路径、候选替换和旧清单升级。

## 0.1.0 - 2026-08-18

### Added

- 可断点续跑的 `collect -> translate -> validate -> render -> qa -> accept` 工作流；
- 本机 Codex 订阅与 OpenAI-compatible BYOK 两种明确 Provider；
- 大气科学领域包、17 条固定术语和 5 个回归案例；
- 公式、富文本、数字、单位、缩写、URL、DOI 与领域术语硬门禁；
- 全页 PDF 结构检查、渲染对照图与显式人工验收；
- 不调用模型的合成双栏 PDF 版面冒烟脚本。
