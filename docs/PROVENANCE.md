# 来源与复用边界

## 本地验证来源

PaperLocale 的质量门禁来自一个已经完成多篇大气科学论文保版翻译的本地工作流。新项目只重构通用合同、Provider 边界和领域包格式，不复制源论文、翻译成果、机器登录信息或本机绝对路径。

## 上游项目

- BabelDOC：PDF 解析、版面中间表示和重建能力，AGPL-3.0；
- PDFMathTranslate-next：BabelDOC 的 CLI/WebUI 上层工具，AGPL-3.0；
- OpenAI Codex CLI/SDK：可选本地翻译 Provider，由用户自行登录和授权。

当前仓库不内嵌或分发 BabelDOC/PDFMathTranslate-next 源码。正式接入时固定兼容版本、记录上游提交，并遵守对应许可证和网络服务源码义务。

## 数据与版权

仓库不提交受版权限制的论文 PDF 或完整译本。自动化测试使用项目自行生成的合成 PDF；真实论文只在用户本地运行。
