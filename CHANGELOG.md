# Changelog

本项目遵循语义化版本。未发布内容先进入 `Unreleased`。

## Unreleased

### Added

- 增加由真实 QA 对照图生成的 README 演示 GIF；
- 增加每周与手动触发的 `pdf2zh-next` 真实版面兼容性工作流。
- 增加 `paperlocale run`，复用现有状态机从当前断点一键推进到机器 QA，并保留显式人工验收。

### Fixed

- 未激活虚拟环境时，也能定位与当前 Python 解释器同目录安装的 `pdf2zh_next`。
- 断点续跑验证现在核对翻译阶段记录的领域包 ID 和版本，拒绝静默更换同语言术语合同。
- PDF QA 现在把图片对象或矢量绘图减少视为硬错误，并在逐页报告中记录两侧数量。

### Documentation

- 记录 v0.1.1 远端测试、发行构建和哈希绑定维护证据。
- 增加 README 工作流图、贡献入口和未经发送的社区沟通草稿。
- 记录 PDFMathTranslate-next #354 上游集成讨论及其待回应状态。
- 记录首个非维护者 fork、Draft PR #4 与维护者验证结论，严格区分外部贡献和真实用户采用。

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
