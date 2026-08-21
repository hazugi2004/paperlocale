# PaperLocale 目标与实施路线

## 总目标

构建并发布一个实用的学术 PDF 保版翻译开源项目：支持本地 Codex 订阅与 API Provider，内置可扩展的大气科学领域包，并以可复现的内容与版式质量门禁支撑长期维护。

## 阶段 0：开源基线

- [x] 仓库骨架与包元数据
- [x] AGPL-3.0-only 许可证选择
- [x] 中英文 README
- [x] 架构、来源和安全边界

完成标准：任何贡献者都能理解项目目标、许可证和不能承诺的边界。

## 阶段 1：内容完整性核心

- [x] 稳定片段 ID 和原子 JSONL
- [x] 公式、样式、数字、单位、缩写、URL、DOI 门禁
- [x] 领域包加载与大气科学术语包
- [x] 不联网单元测试

完成标准：故意删除任一公式或关键科学标记时测试必须失败。

## 阶段 2：Provider

- [x] Provider 协议与确定性测试 Provider
- [x] `codex-local`
- [x] `openai-compatible`
- [x] 真实 Provider 领域案例评估与原子 JSON 报告
- [x] 结构化输出、批次和断点测试

完成标准：两个真实 Provider 产生的结果都必须经过同一门禁，且凭据不落盘。

## 阶段 3：PDF 流水线

- [x] `collect -> translate -> validate -> render -> qa -> accept` 状态机
- [x] 复用同一状态机、只推进到机器 QA 的一键断点续跑命令
- [x] PDFMathTranslate-next/BabelDOC CLI 桥接
- [x] 运行清单与断点续跑
- [x] schema 4 Provider/模型/推理强度、工具版本、领域包及人工映射内容哈希溯源
- [x] 批内部分成功保存与失败候选留档
- [x] 默认 preserve 的参考文献确定性发现、人工确认映射与 translate-titles
- [x] 非正文人工透传映射与跨对象碎词/不可见短文本安全复核

完成标准：任一阶段失败不得产生被标记为合格的 PDF。

## 阶段 4：布局和视觉 QA

- [x] 合成双栏 PDF 测试夹具
- [x] 图片、表格和公式测试页
- [x] 页数、尺寸、空白页和占位符检查
- [x] 全页原文-译文对照报告
- [x] 矢量缺失边界框/面积诊断与红框定位
- [x] 受控矢量修复导入、备份与 repair history
- [x] 显式矩形文字修复、字体子集门禁、备份与 repair history

完成标准：所有候选页都完成机器检查和可视化复核。

## 阶段 5：发布

- [x] GitHub Actions
- [x] Wheel 构建与隔离安装验证
- [x] 贡献指南、Issue/PR 模板与标签构建工作流
- [x] Contributor Covenant 3.0 社区准则与 `CITATION.cff` 学术引用元数据
- [x] 仅手动触发、OIDC 最小权限且复用已审计 Release 附件的 PyPI 可信发布工作流
- [x] 通过 Trusted Publishing 与数字 attestation 发布 PyPI `v0.3.2` 和 `v0.3.3`
- [x] GitHub `v0.1.0` Release
- [x] GitHub `v0.2.0` 一键运行、结构门禁与 Provider 评估 Release
- [x] GitHub `v0.3.0` 真实试运行反向校准、参考文献审计与恢复语义 Release
- [x] GitHub `v0.3.1` 版面依赖兼容性热修复与确切 wheel `[layout]` 求解验证
- [x] GitHub `v0.3.2` 行号参考文献、人工透传、碎词安全审查与 schema 4 Release
- [x] GitHub `v0.3.3` 受控文字修复、独立字体子集与 74 项测试 Release
- [x] 三个有验收标准的 good first issues
- [x] 自有合成论文的原文、译文与全页 QA 演示 GIF

## 阶段 6：申请准备

- [x] 发布后审计并发布 `v0.1.1` 安全与完整性修复
- [x] 准备上游讨论与中英文社区发布草稿
- [ ] 跨周持续发布和兼容性维护
- [x] 首个非维护者 fork 与 [已合并 PR #4](https://github.com/hazugi2004/paperlocale/pull/4)
- [x] 合并首个外部 PR
- [ ] 非维护者真实试用反馈或问题报告
- [ ] 领域人员完成 [Issue #5](https://github.com/hazugi2004/paperlocale/issues/5) 的首份 Provider 语义复核
- [ ] 新领域包贡献
- [x] 发起 [PDFMathTranslate-next #354](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/issues/354) 上游接口讨论
- [x] 在等待回复期间按当前 CLI 契约继续，并用每周真实版面冒烟持续验证兼容性
- [x] 提交 [BabelDOC #610](https://github.com/funstory-ai/BabelDOC/issues/610) 与 [PR #611](https://github.com/funstory-ai/BabelDOC/pull/611)
- [ ] 获得上游维护者回应或合并

申请准备的逐项证据与缺口见 [CODEX_FOR_OSS_READINESS.zh-CN.md](CODEX_FOR_OSS_READINESS.zh-CN.md)。

Stars 只是采用信号之一。申请前更重要的是证明项目有真实用户、持续维护责任和生态价值。
