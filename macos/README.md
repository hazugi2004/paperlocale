# PaperLocale for macOS（测试版）

这是 PaperLocale 0.8.1 的原生窗口前端，支持选择 PDF、选择翻译服务、模型和适用的推理强度、开始/继续、显示日志和打开结果目录。支持 `codex-local`、`openai-compatible` 与 `qwen-mt`，目前使用英译中 atmospheric-science 领域包。

## 安装

1. 解压 `PaperLocale-0.8.1-macOS-universal2.zip`，将 `PaperLocale.app` 放入“应用程序”。需要 macOS 13 或更新版本。
2. **它不是包含全部运行环境的独立安装包。** 先安装 Python 3.10–3.13、Poppler（`brew install poppler`）、所选服务的登录或 API 密钥，以及本版 CLI（执行 `python -m pip install "paperlocale[layout]==0.8.1"`）。例如在虚拟环境中安装后，在窗口的“本地安装与运行目录”选择该环境的 `bin/paperlocale`。默认读取 `~/.local/bin/paperlocale`。
3. 选择论文，确认模型与推理强度，点击“开始 / 继续”。账户不支持所选模型时，程序报错，不会自动改用另一模型。

应用不读取或复制 Codex 登录材料，也不包含用户的论文、翻译缓存或 Python 环境。首次字体/版面模型下载由现有 CLI 负责，需要联网。

发行仅有 **ad-hoc 签名，没有 Apple Developer ID 签名或公证**。首次打开可能被 Gatekeeper 阻止；核对发行来源与 SHA256 后，可使用系统“隐私与安全性”中的“仍要打开”。不建议关闭系统安全检查。参见 [Apple 的首次打开说明](https://support.apple.com/102445)。Universal 2 包含 Apple Silicon 和 Intel 二进制；本次实际运行验证在 Apple Silicon 上完成，Intel 未实机验证。

## 断点与输出

运行目录位于源 PDF 旁边，名为 `<源文件名>.paperlocale`。错误时保留断点并展示包含原文、页码的修复弹窗；可按当前错误选择重试、重新翻译失败片段、降低字号下限、保留原文或回退。不能直接用不同模型覆盖已翻译断点，需通过 CLI 显式导入缓存。原来的其它运行目录请继续通过 CLI 使用。

默认运行输出后机器 QA；只有明确取消勾选时才传入 `--no-qa`，此时 PDF 保存在运行目录中、状态为 `rendered`。界面不会把进程退出 0 当作人工验收。默认 QA 通过的 PDF 按 CLI 规则导出到原文件目录。

首版没有取消整棵翻译子进程的功能，任务执行期间会阻止退出应用；关闭窗口不会停止翻译。不要用强制退出来暂停任务。

## 构建

在 macOS 安装 Xcode Command Line Tools，安装 Pillow 后，用 Python 3.11+ 执行 `python3 scripts/build_macos_app.py`。脚本从当前源码编译 arm64 和 x86_64，使用 ad-hoc 签名，并生成可下载 ZIP。许可证见仓库 AGPL-3.0-only。

## 模型与保存位置

- Codex 使用本机登录，可选择推理强度。其他两种 API 服务不显示或传递此参数。
- 兼容 API 填写服务地址（含版本前缀、不含 `/chat/completions`）、模型名和密钥。模型必须支持当前 CLI 的聊天翻译合同；不自动切换服务或模型。
- Qwen-MT 提供百炼中国区地址与 `qwen-mt-plus` / `qwen-mt-flash` 选项，其他地区请使用账户对应地址与模型。
- API 密钥仅保留在窗口内存并传入翻译子进程环境，不写日志或断点；留空时读取 app 启动环境的 `PAPERLOCALE_API_KEY`。
- “保存到…”可指定完整 PDF 文件名。默认保存到原 PDF 目录；续跑沿用已记录选择。原论文、符号链接和无关现有文件不能覆盖。
- 关闭机器 QA 时仍可指定保存位置，结果始终是未检查候选，不能视为 QA 或人工验收通过。

界面顺序为“大模型 → 具体模型 → 推理强度（若支持）”。GPT 读取本机 Codex 模型目录与支持档位；只显示 CLI 支持的档位。其他兼容 API 可填写自定义模型。

## 0.8.1

翻译失败弹窗和 CLI 直接显示片段编号、页码、逐条规则差异、原文及本次失败译文；详见 [更新说明](../docs/releases/v0.8.1.md)。

## 0.8.0 交互更新

支持将单个 PDF 拖入窗口。奶龙图标和奶蛋进度指示取自用户提供的图片；奶蛋随真实阶段进度移动和滚动，减少动态效果模式下不旋转。进度不是耗时预测。修复选项和跳过记录详见 [更新说明](../docs/releases/v0.8.0.md)。

如同步目录的 FinderInfo 导致签名失败，可指定本机缓存目录：`python3 scripts/build_macos_app.py --output-dir ~/Library/Caches/PaperLocale/0.8.0`。
