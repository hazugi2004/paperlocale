# PaperLocale 架构

## 单一生产路径

```text
PDF
  -> 版面解析与片段收集
  -> segments.jsonl
  -> 领域包 + 单一 Provider
  -> translations.jsonl
  -> 科学信息硬门禁
  -> PDF 重建
  -> 结构检查与全页视觉 QA
```

同一次运行不得静默切换 Provider。失败必须保留断点并明确退出，避免一篇论文混用多个模型后无法追溯。

## 稳定中间格式

`segments.jsonl` 每行至少包含：

- `id`：规范化原文的 SHA-256；
- `source`：版面引擎交给翻译器的原始片段。

源语言、目标语言、源 PDF 绝对路径和 SHA-256 保存在同一运行的
`run_manifest.json`，避免在每条片段中重复。每个后续阶段都会重新核对源 PDF
身份；源文件变化后必须创建新运行。

`translations.jsonl` 每行至少包含：

- `id`；
- `source`；
- `target`。

渲染前必须确认两份文件的 ID 集合完全一致，并逐片段通过合同。

## Provider 边界

Provider 只负责把带稳定 ID 的片段批量翻译为结构化结果。它不得修改 PDF、跳过验证或直接发布输出。

Codex Provider 只在本机调用官方 CLI 或 SDK，并使用只读沙箱。API Provider 的密钥只从环境变量读取。

## 领域包边界

领域包只提供术语与翻译约束，不包含可执行代码。内置包与外部包使用同一格式，因此新增专业方向不需要修改核心模块。
