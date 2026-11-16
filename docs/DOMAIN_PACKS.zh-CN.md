# PaperLocale 领域包指南

领域包把专业翻译规则与 PDF/Provider 核心分开。它只包含四个数据文件，不加载 Python 代码，因此可以独立审查和贡献。

## 目录合同

```text
your-domain/
├── manifest.json
├── prompt.txt
├── glossary.tsv
└── eval_cases.jsonl
```

`manifest.json` 必须声明唯一 `id`、语义化 `version`、`source_language` 和 `target_language`。运行语言必须与领域包一致，否则 PaperLocale 会拒绝翻译。

加载时会用固定文件名和四个文件的原始字节生成内容 SHA-256，并写入运行清单。
即使沿用原有 `id/version`，修改提示词、术语或案例也会改变内容身份。续跑时需要与运行清单中记录的内容一致。
内置大气科学包保留了 1.1.0 的原始副本。已经绑定到该版本的任务会按记录的哈希加载旧包，新任务默认使用 2.0.0。
为同一 PDF 新建独立运行并显式导入旧缓存时，PaperLocale 会按 2.0.0 逐段复查，复用源文、锚点和译文都符合要求的段落。术语不合格的段落会重新翻译，旧运行的文件仍可用来追溯历史译文。

`prompt.txt` 只写本领域的消歧规则和表达要求。公式、数字、单位等通用完整性要求由核心合同统一注入，不需要复制。

`glossary.tsv` 必须恰好有四列：

```text
source\ttarget\trequired\tnote
```

- `source`：源语言术语；
- `target`：给定的译名；
- `required`：只能为 `true` 或 `false`；
- `note`：适用范围、易混词或必须保留的缩写。

翻译请求逐段匹配原文，只发送该段命中的词条。匹配考虑大小写、空白、常见连接号和受支持的末词复数。
长短词在原文同一位置重叠时，优先使用完整短语。短词在段内其他位置独立出现时仍会参与匹配。
当 `required=true` 且原文命中时，译文验收会检查给定译名。`required=false` 的词条作为语境建议，可按所给注释采用常见异译。相同源术语只登记一次。
Qwen-MT 使用官方术语参数接收必用译名，通过翻译记忆参数提供推荐译名作为双语示例。模型返回后仍由 PaperLocale 检查必用译名。 [阿里云 Qwen-MT 翻译参数](https://www.alibabacloud.com/help/en/model-studio/machine-translation)

`eval_cases.jsonl` 每行只允许 `source` 和 `target` 两个字段。至少提供：正常术语、缩写、数值单位、容易混淆概念和公式占位案例。

## 创建与验证

复制内置包后修改：

```bash
cp -R src/paperlocale/packs/atmospheric-science /tmp/your-domain
paperlocale domain-check /tmp/your-domain
```

检查数据文件后，再用计划采用的真实 Provider 运行公开案例：

```bash
paperlocale provider-eval \
  --provider codex-local \
  --model gpt-5.6-sol \
  --reasoning-effort high \
  --domain /tmp/your-domain \
  --output tmp/your-domain-provider-eval.json
```

命令会原子写入每条原文、参考译文、候选译文、内容合同错误和逐字匹配结果。合同失败时报告仍会保留，但命令返回非零。逐字不匹配不等于语义错误；报告始终标记 `manual_semantic_review_required=true`，必须由理解该领域的人逐条判断术语、限定词、因果强度和专业含义。

提交新领域包时，还应在 `tests/` 增加一个加载测试，并说明术语来源与专业边界。不要提交受版权保护的整篇论文、完整译文或数据库导出。

## 内置大气科学术语 2.0.0

英译简中词表面向论文正文，覆盖天气学与动力气象、边界层与湍流、辐射和云降水微物理、大气化学和气溶胶、气候变化与极端事件、陆气相互作用和生态通量、仪器观测与遥感、数值模式、资料同化及预报检验。每条记录对应一个英文拼写，部分概念收录了英美拼写或词序变体。强制条目用于译名稳定的术语，常见异译和需依据量纲、定义或语境处理的词保留为建议。

只出现缩写时沿用原文缩写。出现全称时，按词条翻译并原样保留原有缩写。论文的数据产品、变量字段、阈值、单位、通量符号和统计分母以原文定义为准。

词表为面向论文翻译的自行编排。选词、译名和定义主要参考以下资料，词条中的用法说明针对论文上下文编写。

- 综合气象、天气和云分类采用全国科学技术名词审定委员会、美国气象学会、世界气象组织与中国气象局的术语和分类资料。 [《大气科学名词》第三版](https://cnterm.cn/sd/dqkx/sdml/202311/t20231127_761118.html) · [AMS Glossary of Meteorology](https://glossary.ametsoc.org/wiki/all-terms/) · [WMO《国际云图》术语表](https://cloudatlas.wmo.int/zh-hans/glossary.html) · [天气现象术语 GB/T 35663](https://www.cma.gov.cn/zfxxgk/gknr/flfgbz/bz/202209/P020220921691655269159.pdf)
- 气候变化、干旱和陆气通量参照 IPCC、国家标准、FAO、气候极端指数说明和 ONEFlux 方法论文。 [IPCC AR6 第一工作组中文术语表](https://www.ipcc.ch/report/ar6/wg1/downloads/report/IPCC_AR6_WGI_Glossary_Chinese.pdf) · [IPCC AR6 第 11 章](https://www.ipcc.ch/report/ar6/wg1/chapter/chapter-11/) · [气象干旱等级 GB/T 20481](https://www.cma.gov.cn/zfxxgk/gknr/flfgbz/bz/202209/P020220921673304171703.pdf) · [FAO-56 蒸散方法](https://www.fao.org/4/x0490e/x0490e06.htm) · [Climdex 指数说明](https://www.climdex.org/learn/indices/) · [ONEFlux 方法](https://www.nature.com/articles/s41597-020-0534-3)
- 大气化学、气溶胶及污染物名称参考大气化学模式说明与中国环境空气质量标准。 [EPA CMAQ 化学过程](https://www.epa.gov/cmaq/chemical-process-overview) · [环境空气质量标准](https://www.mee.gov.cn/ywgz/fgbz/bz/bzwb/dqhjbh/dqhjzlbz/202602/t20260225_1144419.shtml)
- 观测和遥感及数值预报术语采用 WMO 仪器指南、中国气象卫星术语、ECMWF 同化说明、WRF 技术资料与国内降水预报检验标准。 [WMO-No. 8](https://community.wmo.int/site/knowledge-hub/programmes-and-initiatives/instruments-and-methods-of-observation-programme-imop/guide-instruments-and-methods-of-observation-wmo-no-8) · [气象卫星术语](https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=2E6E6DB9085B48F9295840D48A825791) · [ECMWF 数据同化](https://www.ecmwf.int/sites/default/files/elibrary/2023/81368-ifs-documentation-cy48r1-part-ii-data-assimilation.pdf) · [WRF 技术说明](https://www2.mmm.ucar.edu/wrf/site/documentation/technote.html) · [降水预报检验](https://www.cma.gov.cn/zfxxgk/gknr/flfgbz/bz/202209/P020220921682224502673.pdf)
