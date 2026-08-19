# Changelog

本项目遵循语义化版本。未发布内容先进入 `Unreleased`。

## Unreleased

### Added

- 增加 Contributor Covenant 3.0 社区准则和 CFF 1.2.0 学术软件引用元数据，完善私密行为问题报告与 GitHub “Cite this repository” 入口。
- 增加仅手动触发的 PyPI Trusted Publishing 工作流：从已审计 GitHub Release 复用确切发行包，把 OIDC 权限限制在独立发布作业，并固定关键第三方 Action 到完整提交。

## 0.3.2 - 2026-08-19

### Added

- macOS CI 在源码目录和虚拟环境都含中文与空格时执行非 editable 安装、`pip check`、领域包检查和 CLI 入口验证，覆盖普通用户推荐安装路径。
- 增加绑定源 PDF、`segments.jsonl` 哈希及逐条确认原因的 `passthrough_map.json`；纯公式、作者姓名串等非正文片段可在人工确认后严格原样保留，无需削弱全局中文门禁，部分批次失败后也可安全续跑。

### Fixed

- 参考文献区域标题确定性支持单数或复数、可选章节号及投稿手稿行号，例如 `6 Reference 353`；仍拒绝正文中的普通 `reference` 词语。
- 翻译前用源 PDF 精确可见文本识别跨对象碎词和不可见短 ASCII 片段，生成哈希绑定的 `segment_safety_review.jsonl` 并要求人工透传，避免 `Figu图…d` 一类无法由单片段译文修复的损坏。

### Documentation

- 按 BabelDOC 贡献规范提交上游 [Issue #610](https://github.com/funstory-ai/BabelDOC/issues/610) 与最小修复及回归测试 [PR #611](https://github.com/funstory-ai/BabelDOC/pull/611)，解决同文返回仍触发重排的对象类型比较错误；合并和发版仍由上游决定。

## 0.3.1 - 2026-08-19

### Fixed

- 真实版面兼容性脚本现在显式完成合成夹具的参考文献复核与空映射确认，恢复 v0.3.0 默认 `preserve` 策略下的 `collect -> render -> qa` 闭环。
- 上游兼容性工作流固定到已验证的 Ubuntu 官方镜像，并对齐当前 Actions 版本，避免 Azure 软件源超时掩盖版面接口结果。
- `PyMuPDF` 版本范围与 `pdf2zh-next 2.9.x` 对齐到已真实验证的 1.25.2，并增加 `[layout]` 依赖解析门禁，修复干净环境无法安装版面工具链的问题；首次版本探测冷启动超时会受控重试一次。

## 0.3.0 - 2026-08-19

### Added

- Codex 本地 Provider 增加 `--reasoning-effort`，运行清单记录 Provider、模型、推理强度、Codex CLI、pdf2zh-next 与 BabelDOC 版本。
- 领域包四个数据文件生成统一内容 SHA-256；相同 id/version 下的静默改写会被续跑门禁拒绝。
- 同批单条失败时保留其他合格译文，并把失败候选与具体合同错误写入 `rejected_translations.jsonl`。
- 矢量绘图减少时报告缺失对象的页码、边界框和面积，并在逐页对照图的源文与译文位置绘制红框。
- 增加 `apply-vector-repair`：只接受文字、页尺寸和图片不变且矢量数量增加的候选，备份旧 PDF、记录 `repair_history`，并强制重新 QA 与人工验收。
- 参考文献默认采用 `preserve`：确定性匹配后生成全片段复核清单，人工确认映射绑定源 PDF 与片段文件哈希；可显式选择 `translate-titles`，且参考文献不套用正文术语门禁。

### Fixed

- `ABSTRACT`、`KEYWORDS`、`REFERENCES` 不再被误判为必须原样保留的科学缩写。
- `mid-1960s` 继续保护年代 `1960`，但不再把连接号误判为负号。

### Documentation

- 发布 v0.2.0 后，在 PDFMathTranslate-next #354 补充真实兼容性与标签发行构建证据，继续等待外部回应。
- 增加五案例大气科学人工语义复核工作表、[Issue #5](https://github.com/hazugi2004/paperlocale/issues/5) 和逐条贡献要求，避免把模型自评当作领域验收。

## 0.2.0 - 2026-08-18

### Added

- 增加由真实 QA 对照图生成的 README 演示 GIF；
- 增加每周与手动触发的 `pdf2zh-next` 真实版面兼容性工作流。
- 增加 `paperlocale run`，复用现有状态机从当前断点一键推进到机器 QA，并保留显式人工验收。
- 增加 `paperlocale provider-eval`，用领域案例原子保存真实候选、参考译文与内容合同结果，并明确保留人工语义复核。

### Fixed

- 发行元数据改用 SPDX 许可证表达式与显式 `license-files`，消除 setuptools 旧格式弃用警告。
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
