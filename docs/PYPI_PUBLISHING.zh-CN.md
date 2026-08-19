# PyPI 可信发布操作手册

状态：工作流已准备，尚未配置 PyPI 待定发布者，也尚未向 PyPI 上传任何版本。

## 设计边界

`.github/workflows/publish-pypi.yml` 只接受维护者手动输入稳定标签，不响应标签推送或
GitHub Release 事件。准备作业从对应的公开 GitHub Release 下载已经审计的 wheel 和
sdist，复核元数据、安装入口及 `[layout]` 依赖求解；独立发布作业不检出或执行仓库
代码，只持有 `id-token: write`，通过 PyPI Trusted Publishing 获取短期凭据。

这意味着工作流合并后仍不会自行发布。PyPI 待定发布者和 GitHub Environment 都必须
由仓库维护者本人配置，发布时还需要再次手动运行并批准 Environment。

## 首次发布前一次性配置

1. 在仓库 `Settings -> Environments` 新建 `pypi` Environment，将 `hazugi2004` 设为
   required reviewer。不要在其中保存 PyPI API token。
2. 登录 PyPI，打开 <https://pypi.org/manage/account/publishing/>，创建 pending
   publisher，逐项填写：

   | 字段 | 精确值 |
   |---|---|
   | PyPI Project Name | `paperlocale` |
   | GitHub Owner | `hazugi2004` |
   | Repository name | `paperlocale` |
   | Workflow name | `publish-pypi.yml` |
   | Environment name | `pypi` |

3. 再次核对项目名、仓库、工作流文件名和 Environment。OIDC 声明与任一字段不一致
   时，PyPI 会拒绝上传。

2026-08-19 对 `https://pypi.org/pypi/paperlocale/json` 的只读检查返回 404，表示检查
当时没有同名公开项目；名称只有在首次成功上传并由 PyPI 创建项目后才真正确定，不能
把这次检查当作永久占位。

## 手动发布

1. 在 GitHub Actions 打开 `publish-pypi`，选择 `Run workflow`。
2. 输入已经公开并完成审计的稳定标签，例如 `v0.3.2`。
3. 等待 `Verify published distributions` 通过。
4. 在 `pypi` Environment 部署审批中复核标签并手动批准。
5. 发布作业成功后，检查 `https://pypi.org/project/paperlocale/` 的版本、文件和
   attestations，再在全新虚拟环境执行：

   ```bash
   python -m pip install "paperlocale[layout]==0.3.2"
   paperlocale domain-check atmospheric-science
   ```

只有上述页面和隔离安装都核验成功后，才能把 README 的普通用户安装方式改为 PyPI，
并在路线图中勾选“发布到 PyPI”。失败时保留 Actions 日志，不使用 `skip-existing` 掩盖
重复版本或部分上传问题。
