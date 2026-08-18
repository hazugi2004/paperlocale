# Security Policy

## Secrets

- 不要提交 API Key、`.env`、`~/.codex/auth.json` 或任何 OAuth Token。
- API Provider 只从进程环境读取密钥。
- `codex-local` 只复用 Codex 自己管理的本机登录态，不读取认证文件。

## Trust boundary

Codex 会员登录只允许在用户控制的可信本机运行。不得将其部署为公共多用户翻译服务，也不得把本机 Codex 执行暴露给不可信输入或远程调用者。

安全问题请通过 GitHub 私密漏洞报告渠道提交，不要在公开 Issue 中粘贴凭据或受版权保护的论文。
