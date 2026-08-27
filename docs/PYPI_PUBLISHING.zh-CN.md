# PyPI 可信发布操作手册

状态：`v0.3.2`、`v0.3.3`、`v0.3.4` 与 `v0.4.0` 已通过 PyPI Trusted Publishing 发布；
当前最新版 `v0.4.0` 已完成公开 attestation、哈希、隔离安装和 `[layout]` 依赖求解
核验。

## 设计边界

`.github/workflows/publish-pypi.yml` 只接受维护者手动输入稳定标签，不响应标签推送或
GitHub Release 事件。准备作业从对应的公开 GitHub Release 下载已经审计的 wheel 和
sdist，复核元数据、安装入口及 `[layout]` 依赖求解；独立发布作业不检出或执行仓库
代码，只持有 `id-token: write`，通过 PyPI Trusted Publishing 获取短期凭据。

这意味着标签和 GitHub Release 不会自行触发 PyPI 上传。每次发布仍需维护者手动
输入标签，并批准受保护的 `pypi` Environment。

## 当前可信发布配置

GitHub `pypi` Environment 已将 `hazugi2004` 设为 required reviewer，并允许发起者
本人审批；仓库没有保存 PyPI API token。PyPI publisher 与 GitHub OIDC 必须持续
保持以下精确身份：

| 字段 | 精确值 |
|---|---|
| PyPI Project Name | `paperlocale` |
| GitHub Owner | `hazugi2004` |
| Repository name | `paperlocale` |
| Workflow name | `publish-pypi.yml` |
| Environment name | `pypi` |

修改仓库所有者、仓库名、工作流文件名或 Environment 时，必须同步更新 PyPI
publisher；任一字段不一致都会被 PyPI 拒绝。

2026-08-19 对 `https://pypi.org/pypi/paperlocale/json` 的只读检查返回 404，表示检查
当时没有同名公开项目。首次上传于 2026-08-21 成功创建正式
[`paperlocale`](https://pypi.org/project/paperlocale/0.3.2/) 项目。

## 首次发布证据

- [第一次运行](https://github.com/hazugi2004/paperlocale/actions/runs/32239980222)
  在附件校验和 Environment 审批后被 PyPI 以 `invalid-publisher` 拒绝；没有上传文件。
  维护者按实际 OIDC claims 纠正 publisher 后才重试，没有改工作流来猜测匹配；
- [成功运行](https://github.com/hazugi2004/paperlocale/actions/runs/32441153778)
  的准备和发布作业均通过，并从公开 GitHub Release 复用确切 wheel 与 sdist；
- PyPI wheel SHA-256 为
  `594687d6fc474963a485682717cdd746a3d62f6af99cd7fc5611e9bcdfcf04f1`，sdist 为
  `a79570a6d30c4e2c0f2b73d4a4cc292a933b8faf8714a12f4075f6e7ba5a9dfa`，与 GitHub
  Release 完全一致；
- 两个文件的 PyPI provenance 都声明 GitHub 仓库 `hazugi2004/paperlocale`、工作流
  `publish-pypi.yml`、Environment `pypi`；`pypi-attestations==0.0.30` 对 wheel 和
  sdist 的密码学验证均返回 `OK`；
- 全新 Python 3.12 环境从 `https://pypi.org/simple` 安装确切 0.3.2，`pip check`、
  `paperlocale domain-check atmospheric-science` 和 `[layout]` dry-run 求解全部通过。

## v0.3.3 后续发布证据

`v0.3.3` 的后续发布证明同一可信发布路径可以复用，而不是只在首次创建 PyPI 项目时
偶然成功：

- [标签构建](https://github.com/hazugi2004/paperlocale/actions/runs/32444041611)
  从提交 `6002805` 构建并安装确切 wheel，`twine check` 与 `[layout]` 求解通过；
- [GitHub v0.3.3 Release](https://github.com/hazugi2004/paperlocale/releases/tag/v0.3.3)
  的 wheel SHA-256 为
  `9738ff9a515f10c1da9a10852fbfc3c9d44a4d1febfe233df173a2be8edaeaab`，
  sdist 为
  `c35d9e4a16ea1af5cb8c03c4bea30a1f9fee0f379e920eee155215f5e8138198`；
- [PyPI 发布运行](https://github.com/hazugi2004/paperlocale/actions/runs/32445150459)
  的准备和发布作业均成功，PyPI 两个文件与 GitHub Release 哈希完全一致；
- 两个 PyPI Integrity API provenance 均绑定 GitHub 仓库 `hazugi2004/paperlocale`、
  工作流 `publish-pypi.yml` 和 Environment `pypi`；
  `pypi-attestations==0.0.30` 对 wheel 与 sdist 的密码学验证均返回 `OK`；
- 全新 Python 3.12 环境从 PyPI 安装确切 0.3.3 后，模块版本、发行版本、`pip check`、
  `domain-check`、`apply-text-repair` 入口和 `[layout]` dry-run 求解全部通过。

## v0.3.4 后续发布证据

- [标签构建](https://github.com/hazugi2004/paperlocale/actions/runs/32626353951)
  从合并提交 `bcb82e9` 构建并安装确切 wheel，版本一致性、`twine check`、
  领域包和 `[layout]` 求解全部通过；
- [GitHub v0.3.4 Release](https://github.com/hazugi2004/paperlocale/releases/tag/v0.3.4)
  的 wheel SHA-256 为
  `2d40688f2f1d1cf5ae130b33342dcadc987cb2945f8fa9220a85332df94fdc97`，
  sdist 为
  `0571ec37e452f2389f0303d5a3289f8ee670c5b70b7254ac85de629b31e0641e`；
- [PyPI 发布运行](https://github.com/hazugi2004/paperlocale/actions/runs/32626495081)
  的准备和发布作业均成功，PyPI 两个文件与 GitHub Release 哈希完全一致；
- 两个 PyPI provenance 均绑定仓库 `hazugi2004/paperlocale`、工作流
  `publish-pypi.yml` 和 Environment `pypi`；`pypi-attestations==0.0.30`
  验证 wheel 和 sdist 均返回 `OK`；
- 全新 Python 3.12 环境从 PyPI 禁用缓存安装确切 0.3.4 后，模块/发行/CLI
  版本、`pip check`、Qwen-MT Provider 导入和 `domain-check` 全部通过。

## v0.4.0 后续发布证据

- [标签构建](https://github.com/hazugi2004/paperlocale/actions/runs/32644709628)
  从稳定提交 `951993d` 构建并安装确切 wheel，版本一致性、`twine check`、
  领域包和 `[layout]` 求解全部通过；
- [GitHub v0.4.0 Release](https://github.com/hazugi2004/paperlocale/releases/tag/v0.4.0)
  的 wheel SHA-256 为
  `17206e70844ec3e37c3d021923b3a075614af2ad2f1b4aa6d78fe62920f560d9`，
  sdist 为
  `ae31f42a6631fbdd1c37b02ab8f605adfd99fc6809f5a445e029f5e232b50021`；
- [PyPI 发布运行](https://github.com/hazugi2004/paperlocale/actions/runs/32644879147)
  的准备和发布作业均成功，PyPI 两个文件与 GitHub Release 哈希完全一致；
- 两个 PyPI Integrity API provenance 均绑定仓库 `hazugi2004/paperlocale`、工作流
  `publish-pypi.yml` 和 Environment `pypi`；`pypi-attestations==0.0.30`
  验证 wheel 和 sdist 均返回 `OK`；
- 全新 Python 3.12 环境从 PyPI 禁用缓存安装确切 0.4.0 后，模块/发行/CLI
  版本、`pip check`、领域包、ChatGPT Web 命令入口与 `[layout]` 求解全部通过。

## 后续版本操作

1. 在 GitHub Actions 打开 `publish-pypi`，选择 `Run workflow`。
2. 输入已经公开并完成审计、且尚未上传 PyPI 的稳定标签；PyPI 版本不可覆盖，
   不要再次发布 `v0.3.2`、`v0.3.3`、`v0.3.4` 或 `v0.4.0`。
3. 等待 `Verify published distributions` 通过。
4. 在 `pypi` Environment 部署审批中复核标签并手动批准。
5. 发布作业成功后，检查 `https://pypi.org/project/paperlocale/` 的版本、文件和
   attestations，再在全新虚拟环境执行：

   ```bash
   python -m pip install "paperlocale[layout]==X.Y.Z"
   paperlocale domain-check atmospheric-science
   ```

每个新版本都必须复核 PyPI 文件哈希、provenance、attestation 和隔离安装。失败时
保留 Actions 日志，不使用 `skip-existing` 掩盖重复版本或部分上传问题。
