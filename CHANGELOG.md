# Changelog

本项目遵循语义化版本。未发布内容先进入 `Unreleased`。

## 0.1.0 - 2026-08-18

### Added

- 可断点续跑的 `collect -> translate -> validate -> render -> qa -> accept` 工作流；
- 本机 Codex 订阅与 OpenAI-compatible BYOK 两种明确 Provider；
- 大气科学领域包、17 条固定术语和 5 个回归案例；
- 公式、富文本、数字、单位、缩写、URL、DOI 与领域术语硬门禁；
- 全页 PDF 结构检查、渲染对照图与显式人工验收；
- 不调用模型的合成双栏 PDF 版面冒烟脚本。
