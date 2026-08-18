# Security Policy

## Secrets

- 不要提交 API Key、`.env`、`~/.codex/auth.json` 或任何 OAuth Token。
- API Provider 只从进程环境读取密钥。
- 远程 API Provider 强制使用 HTTPS；明文 HTTP 只允许本机 loopback 服务。
- `codex-local` 只复用 Codex 自己管理的本机登录态，不读取认证文件。

## Trust boundary

Codex 会员登录只允许在用户控制的可信本机运行。不得将其部署为公共多用户翻译服务，也不得把本机 Codex 执行暴露给不可信输入或远程调用者。

`run_manifest.json` 与 `qa_report.json` 分别记录源 PDF 和候选 PDF 的
SHA-256。QA 后替换任一 PDF 会使 `accept` 失败；不要手工修改清单或报告来绕过
此门禁。

安全问题请通过 GitHub 私密漏洞报告渠道提交，不要在公开 Issue 中粘贴凭据或受版权保护的论文。
