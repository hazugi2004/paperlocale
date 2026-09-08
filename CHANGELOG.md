# Changelog

本项目遵循语义化版本。未发布内容先进入 `Unreleased`。

## 0.6.2 - 2026-09-08

- Validate numeric values together with their scientific units across GPT/Codex and Qwen, allowing registered Chinese aliases and equivalent product, quotient, and integer-power notation.
- Reject omitted units, changed prefixes/temperature scales, and swapped number-unit associations; do not infer physical conversions or weaken formula/identifier gates.
- Let Qwen translate intact quantities naturally first. On an invalid response, protect entire source quantities and standalone units in the existing single bounded source-gap recovery.
- Preserve layout-split decimal punctuation and explicit math-only expressions, including lowercase variables.
- Archive cached rows that fail only the new quantity or scientific-literal contract, then retranslate those rows while retaining valid checkpoints. Other invalid cache errors remain hard failures.
- Keep GPT execution, short-key response schema, rendering and PDF acceptance requirements unchanged; expose source quantity signatures in the common prompt.

## 0.6.1 - 2026-09-08

- Route Qwen-MT unknown, duplicate, and malformed full-response markers into the existing bounded source-gap recovery. Discard the entire invalid candidate; never guess marker mappings.
- Merge overlapping/adjacent source protection spans so version identifiers and resolution formulas stay intact; protect URL breaks before a slash.
- Reject protocol markers emitted during recovery without recursive retries; keep fragment and full-content gates intact.
- Preserve the Codex/GPT and OpenAI-compatible providers, common pipeline/contracts, rendering, and QA logic byte-for-byte from 0.6.0. Runtime changes are limited to the Qwen provider.

## 0.6.0 - 2026-09-08

- Protect Qwen-MT URL/markup, number and abbreviation occurrences with collision-safe local markers; restore the exact source identifiers, including wrapped URL text.
- Validate restored full responses; on missing markers or contract failures, perform one bounded source-gap translation pass with fragment and full-content gates. Unknown/duplicate markers remain hard errors.
- Pace normal and repair requests through one path; retry explicit request-rate throttling at most once, leaving auth and other business errors as checkpointed failures.
- Add Qwen-only `--api-key-csv` to read complete opaque credential fields, avoiding truncated keys caused by character-whitelist extraction.
- Compare unchanged text coordinates with a strict 0.0001-point absolute tolerance instead of rounding, rejecting actual movement/text/count changes.
- Recognize a journal-volume/page continuation aligned with the References heading without including upper publisher prose.
- Add regressions for URLs with embedded formulas, repeated identifiers, glued years, invalid markers, bounded repairs, missing units, pacing, throttling, and complete CSV credentials.

## 0.5.3 - 2026-09-08

- Restore two-column bibliography continuations where an author surname ends the left column and initials begin the right column.
- Require matching author-list evidence on both sides, ignore repeated footer interference, and retain body/publisher-text exclusion and out-of-region protection.
- Add pixel and text regressions for initials-led continuation and non-reference publisher prose with repeated journal footers.

## 0.5.2 - 2026-09-08

- Fix reference-region truncation when a wrapped bibliography line starts with a four-digit year followed by an alphabetic title. These lines no longer match numbered section headings.
- Count image placements from PDF content streams rather than image resources, avoiding soft-mask/unused-resource false positives while detecting lost placements, including nested Forms.
- Preserve real section boundaries and the existing body-overlap rejection, atomic repair, and QA requirements.
- Add a two-page regression with a year-led continuation, overlapping translation, and a subsequent numbered Figures section; verify restored pixels and unchanged surrounding text.

## 0.5.1 - 2026-09-07

- 修复 Codex 翻译时漏抄长片段哈希导致整批 ID 校验失败的问题：模型只填充
  严格 schema 下的必填短键对象，原始哈希由程序在本地绑定，不再由模型生成。
- 按键恢复对应关系，支持响应键乱序；明确拒绝缺键、额外键、重复 JSON 键和
  非字符串译文，不按响应位置配对、不模糊匹配错误哈希、不增加盲目重试。
- 参考文献标记和内容修复反馈随批次短键映射；磁盘缓存仍用原哈希，可继续
  已通过验证的旧断点。其它 Provider 的公共返回接口保持兼容。
- 增加真实错误哈希、短键闭合、重复键解析、反馈映射与缓存复用回归；144项
  本地测试通过。公式、数字、术语、PDF QA 与逐页视觉验收要求保持不变。

- 为多段书目和相邻出版元数据增加显式 `restore-reference-layout --regions-file`
  入口，审核文件必须绑定当前源/译PDF哈希及复核者；拒绝越界或重叠区域。

## 0.5.0 - 2026-09-07

- `run` 默认执行一次有界矢量恢复，保留显式禁用选项、备份、哈希及完整 QA 门禁。
- 矢量身份由边界升级为实际路径、样式及重复次数；保留透明度 0，拒绝改变
  既有路径或文字位置的修复候选。QA 统计嵌套 Form XObject 内的实际绘图。
- `preserve` 直接恢复确定的源书目区域，保留原始字形和排版；验证源区域文字
  及区域外译文位置，支持独立修复入口、断点复用及回滚。
- 修正左栏书目标题上方右栏仍是出版商说明时的边界误判；不改写历史片段映射。
- 增加单栏、双栏、横向页面的真实区域像素回归，以及重叠、越界、删除标注、
  同边界异形路径、透明度和回滚验证。版面阶段打印开始/完成及日志位置。
- 保留逐页视觉验收要求；不承诺所有未知版式、扫描件和公式均能自动无损完成。

## 0.4.3 - 2026-09-07

### Fixed

- 参考文献从左栏下方开始时，纳入同页右栏顶部的后续书目；有双栏坐标证据时
  按栏排序，并在独立的无编号致谢、作者贡献等小节处停止，避免正文被误保留。
- 归一化 PDF 字体连字，使 `identiﬁcation` 与 `identification` 正确对应；
  真正位于单词内部的残缺片段仍由原安全门禁阻断。
- 中文文字修复允许抽取产生的汉字/中文标点相邻换行，保留英文词及数字分隔检查；
  同一译文可通过显式改变断行进行受控重排。

### Added

- `run --restore-source-vectors` 显式启用一次带哈希绑定和备份的源矢量恢复；
  仅在所有 QA 错误都是矢量减少时执行，恢复后必须重新通过完整机器 QA。
  默认停止行为、逐页视觉审查和显式 `accept` 边界保持不变。

## 0.4.2 - 2026-08-28

### Added

- `paperlocale run --unattended` 可在单条非交互命令中自动完成确定性
  参考文献映射、版面安全透传、全文翻译、内容门禁、PDF 重建与全页
  机器 QA；成功后直接打印完整候选 PDF 路径；
- 无人值守决定以 `paperlocale-unattended` 身份和明确原因写入哈希
  绑定映射，运行清单记录自动参考文献与透传数量，不冒充人工复核；
- 新增 `restore-source-vectors`，先以源/译哈希绑定当前机器 QA，再只在
  `source_vector_drawings > translated_vector_drawings` 的页面重放按
  0.01 PDF 点边界确认缺失的源矢量路径，复用现有文字、页面、图片、
  备份和修复历史门禁；
- `apply-text-repair --single-line` 可按子集字体的实际宽度、升部与降部
  在浅矩形中写入一行，容量超界仍在修改 PDF 前拒绝；
- 新增 `rollback-last-repair`，只允许从 `repair_history` 链尾按
  after/before 哈希和绑定备份逆向恢复，将回滚项移入
  `repair_rollback_history`，并清除已过期的 QA/人工验收绑定。

### Changed

- Provider 首轮译文未通过内容合同时，用同一 Provider 传入原候选与
  具体错误定向修复一次；合格片段继续原子保存，两次失败证据保留在
  `rejected_translations.jsonl`；
- Qwen-MT 继续保持单片段调用边界，对真实运行中观察到的临时连接断开
  做有界重试，HTTP 业务错误仍立即失败。

### Fixed

- 修正真实论文中全大写章节词、国家/地址缩写、非 ASCII 单词中单位
  误匹配、术语左边界与期刊艺术字大小写造成的内容合同误拒绝；
- 断点续跑不再因为 Codex CLI 补丁版本变化而拒绝同一
  `codex-local` 模型与推理强度；新旧 CLI 版本和切换前译文数量保存在
  `translation_provider_history`，Provider、模型或推理强度变化仍明确失败。

### Safety boundary

- `--unattended` 只自动采用确定性算法结果，不把普通翻译失败静默改为
  透传，不自动执行 `accept`；无人值守候选仍如实标记为 `qa_generated`。

## 0.4.1 - 2026-08-23

### Fixed

- 右栏中途开始 `References` 时，首页区域改为仅收集标题所在右栏，避免把左栏同高度的结论或结果正文误标为参考文献；
- `confirm-references` 新增可审计的 `--exclude-segment-id`，允许复核者排除致谢、作者贡献、Publisher note 或许可证等自动越界片段；只能排除当次自动匹配 ID，并在 `reference_map.json` 留痕；
- 已经人工确认为参考文献且由 `preserve` 保留的碎片，不再被片段安全审查重复要求 `confirm-passthrough`；Codex/API 和 ChatGPT Web 导出路径共用同一修复。

### Validation

- 新增右栏参考文献、显式排除和参考文献/安全审查重叠回归；第 28 篇真实双栏 PDF 回放中的 4 个已知正文误匹配全部消失。

## 0.4.0 - 2026-08-23

### Added

- 增加 `chatgpt-web-export` / `chatgpt-web-import` 人工网页桥接：导出带哈希的
  大气科学翻译批次，在 ChatGPT 网页端普通 Chat 中人工交换严格 JSON，再复用
  现有内容合同、部分批次恢复、参考文献 `preserve`、PDF 重建和逐页 QA。
- 网页批次绑定源 PDF、片段、领域包、参考文献和透传映射身份；导入记录人工可见
  模型标签，保存每次回复原始字节快照和导入历史，不保存登录态、Cookie 或 API Key。
- `apply-text-repair` 支持显式空 replacement 的只删除模式；不嵌入字体，
  以 `text-removal` 独立记录，并继续强制矩形外文字、页面结构、备份和 QA 门禁。

### Fixed

- ChatGPT Web 提示改为单一 `json` 代码块，并要求使用代码块的复制按钮，
  避免裸 JSON 中的 URL、DOI 和邮箱被网页富文本层改成 Markdown 链接。
- 数字合同现在一致识别紧贴英文单词或中文汉字的引文号，例如
  `productivity3,17,18` 与 `生产力3,17,18`；`mid-1960s` 仍不会被误判为负数。

### Security

- PaperLocale 不登录、不抓取也不自动点击 `chatgpt.com`；网页 Chat 不是官方 API，
  因此不能作为无人值守 Provider，也不能保证任何账户的具体用量或额度归属。

### Verification

- 使用 9 页大气科学开放获取论文完成受监督 Computer Use 网页实验：
  106 个片段中 83 个通过普通 Chat 的 6 个哈希批次翻译，19 个人工透传，
  4 个参考文献片段保留；106/106 内容合同、9/9 页机器 QA 和逐页视觉验收通过。
- 真实运行证据见 [`docs/releases/v0.4.0.md`](docs/releases/v0.4.0.md)；不上传或再分发
  源 PDF、完整译本或私人 ChatGPT 会话链接。

## 0.3.4 - 2026-08-23

### Added

- 新增独立 `qwen-mt` Provider，使用 Qwen-MT 官方单消息语义、DomainPack 提示与术语干预，并对公式/样式标记执行可逆保护；
- 新运行清单记录 `paperlocale_version`，CLI 新增 `paperlocale --version`，标签构建核对源码、包元数据和命令行版本一致。

### Changed

- Provider 可声明自身的最大单批片段数；正式翻译和 Provider 评估共用该边界，Qwen-MT 每条成功译文立即进入原子检查点；
- Qwen-MT 实现去除第 13 篇论文的地名、阶段和学科提示硬编码；`compound hot-dry event` 作为通用词序变体收入 v1.1.0 大气科学领域包；
- URL/DOI 和缩写门禁可容忍已证实的 PDF 断行空格与中文紧邻文本，但不改写原始数字或猜测公式含义。

### Fixed

- `paperlocale run` 现在把 `--max-segments` 和 `--max-characters` 正确传入一键工作流；
- 参考文献区域改为按可见文本行识别，修复 PyMuPDF 将 `References` 与整页书目合并为同一文本块时的整区漏检；
- Qwen-MT 删除保护标记时明确失败，不再根据译文中首个缩写位置猜测回填。

## 0.3.3 - 2026-08-21

### Added

- 增加 Contributor Covenant 3.0 社区准则和 CFF 1.2.0 学术软件引用元数据，完善私密行为问题报告与 GitHub “Cite this repository” 入口。
- 增加仅手动触发的 PyPI Trusted Publishing 工作流：从已审计 GitHub Release 复用确切发行包，把 OIDC 权限限制在独立发布作业，并固定关键第三方 Action 到完整提交；[首次成功运行](https://github.com/hazugi2004/paperlocale/actions/runs/32441153778)发布 `v0.3.2`，[后续运行](https://github.com/hazugi2004/paperlocale/actions/runs/32445150459)发布 `v0.3.3`，两次均生成公开 attestation。
- 合并首个非维护者贡献 [PR #4](https://github.com/hazugi2004/paperlocale/pull/4)，将项目自有 PDF QA 合成夹具扩展为两页并增加第二页占位符回归。
- 增加 `apply-text-repair` 受控文字修复：显式绑定页码、矩形、替换文字、字体和字号，保留页结构、图片、链接、既有矢量与矩形外正文，只为本次替换独立生成字体子集，并记录修复前后文字、字体/PDF 哈希、备份及 QA 重置。

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
