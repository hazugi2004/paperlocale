# PaperLocale for macOS（测试版）

这是 PaperLocale 0.7.7 的原生窗口前端，支持选择 PDF、明确选择 Codex 模型与推理强度、开始/继续、显示日志和打开结果目录。首次仅支持 `codex-local` 和英译中 atmospheric-science 领域包。

## 安装

1. 解压 `PaperLocale-0.7.7-macOS-universal2.zip`，将 `PaperLocale.app` 放入“应用程序”。需要 macOS 13 或更新版本。
2. **它不是包含全部运行环境的独立安装包。** 先安装 Python 3.10–3.13、Poppler（`brew install poppler`）、已登录的 Codex CLI，以及 `paperlocale[layout]==0.7.7`。例如在虚拟环境中安装后，在窗口的“本地安装与运行目录”选择该环境的 `bin/paperlocale`。默认读取 `~/.local/bin/paperlocale`。
3. 选择论文，确认模型与推理强度，点击“开始 / 继续”。账户不支持所选模型时，程序报错，不会自动改用另一模型。

应用不读取或复制 Codex 登录材料，也不包含用户的论文、翻译缓存或 Python 环境。首次字体/版面模型下载由现有 CLI 负责，需要联网。

发行仅有 **ad-hoc 签名，没有 Apple Developer ID 签名或公证**。首次打开可能被 Gatekeeper 阻止；核对发行来源与 SHA256 后，可使用系统“隐私与安全性”中的“仍要打开”。不建议关闭系统安全检查。参见 [Apple 的首次打开说明](https://support.apple.com/102445)。Universal 2 包含 Apple Silicon 和 Intel 二进制；本次实际运行验证在 Apple Silicon 上完成，Intel 未实机验证。

## 断点与输出

运行目录位于源 PDF 旁边，名为 `<源文件名>.paperlocale`。错误时程序退出并保留断点，修正问题后点击同一按钮继续。不能直接用不同模型覆盖已翻译断点，需通过 CLI 显式导入缓存。原来的其它运行目录请继续通过 CLI 使用。

默认运行输出后机器 QA；只有明确取消勾选时才传入 `--no-qa`，此时 PDF 保存在运行目录中、状态为 `rendered`。界面不会把进程退出 0 当作人工验收。默认 QA 通过的 PDF 按 CLI 规则导出到原文件目录。

首版没有取消整棵翻译子进程的功能，任务执行期间会阻止退出应用；关闭窗口不会停止翻译。不要用强制退出来暂停任务。

## 构建

在 macOS 安装 Xcode Command Line Tools，用 Python 3.11+ 执行 `python3 scripts/build_macos_app.py`。脚本从当前源码编译 arm64 和 x86_64，使用 ad-hoc 签名，并生成可下载 ZIP。许可证见仓库 AGPL-3.0-only。
