# PaperLocale Provider 翻译评估证据

核验日期：2026-08-18。

## 当前公开运行

- Provider：`codex-local`；
- Codex CLI：`0.148.0-alpha.15`；
- 模型：未显式传入 `--model`，使用该 CLI 当次默认模型，因此本次结果不能用于跨模型排名；
- 领域包：`atmospheric-science 1.0.0`；
- 案例：5 条项目自有英译中回归案例；
- 内容硬合同：5/5 通过；
- 与参考译文逐字一致：0/5；
- 领域人员人工语义复核：待完成。

机器可复核的逐条结果见 [JSON 报告](evidence/codex-local-atmospheric-science-eval.json)。报告不含 Codex 登录材料、API Key、版权论文或私人数据。

领域人员可直接填写[五案例人工语义复核工作表](evidence/codex-local-atmospheric-science-review.zh-CN.md)，并通过 Pull Request 保留逐条判定、专业理由和建议译文。

## 如何解释

五条候选均保留了案例中的术语、数字、单位、缩写、公式占位符、样式标签和 DOI。0/5 逐字匹配主要来自空格、中文/半角括号和“略微/小幅”等措辞变化。逐字不一致既不能自动证明错误，也不能自动证明语义正确。

因此当前证据只能支持“真实 Codex Provider 输出全部通过可证明的内容合同”。在大气科学领域人员逐条确认限定词、术语含义、关联/因果强度和表达自然度前，项目不会把它写成 100% 翻译准确率。

## 复现命令

```bash
paperlocale provider-eval \
  --provider codex-local \
  --domain atmospheric-science \
  --output tmp/codex-local-atmospheric-eval.json
```

若要比较明确模型，应额外传入 `--model`，并把模型名与 Codex CLI 版本一起记录。模型输出可能变化，因此每次运行都应保留独立报告，不能覆盖为同一次证据。
