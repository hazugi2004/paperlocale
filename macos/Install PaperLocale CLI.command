#!/bin/bash
set -euo pipefail

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

# Finder 启动的终端可使用与交互终端不同的 PATH。补充两种架构的 Homebrew 目录。
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
archive_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
plist="$archive_dir/PaperLocale.app/Contents/Info.plist"
[[ -f "$plist" ]] || fail '请先完整解压下载的 ZIP，将此文件、PaperLocale.app 和 wheel 保持在同一目录。'
version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$plist")"
domain_version="$(/usr/libexec/PlistBuddy -c 'Print :PaperLocaleAtmosphericScienceVersion' "$plist")" ||
    fail '应用中缺少随附术语库的版本。请重新下载同版本的完整 ZIP。'
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
    fail '应用发布版本无效。请重新下载完整的 ZIP。'
wheel="$archive_dir/paperlocale-$version-py3-none-any.whl"
[[ -f "$wheel" ]] || fail "找不到安装文件 ${wheel}。请重新解压完整的 ZIP。"

python=''
for name in python3.12 python3.13 python3.11 python3.10 python3; do
    if candidate="$(command -v "$name")" &&
        "$candidate" -c 'import sys; sys.exit(not (3, 10) <= sys.version_info[:2] <= (3, 13))'; then
        python="$candidate"
        break
    fi
done
[[ -n "$python" ]] || fail '需要 Python 3.10–3.13。安装 Python 3.12 后重新运行此文件。'

# 与 App 默认路径一致。每个发布版本独立存放，重跑可以继续完成此前中断的安装。
venv="$HOME/Library/Application Support/PaperLocale/cli/$version"
"$python" -m venv "$venv" || fail "无法创建 Python 环境。请检查安装目录是否可写：$venv"
# 重装 PaperLocale 本身以确保现有同版本环境也加载本 ZIP 中的文件；依赖交给第二步补齐。
"$venv/bin/python" -m pip install --force-reinstall --no-deps "$wheel" ||
    fail '安装随附的 PaperLocale wheel 失败。请检查文件是否完整后重试。'
"$venv/bin/python" -m pip install "${wheel}[layout]" ||
    fail '安装排版依赖失败。请检查网络和上方的 pip 输出后重试。'

cli="$venv/bin/paperlocale"
cli_version="$("$cli" --version)" || fail '无法启动安装后的 PaperLocale CLI。'
[[ "$cli_version" == "paperlocale $version" ]] || fail "CLI 版本与应用不一致：$cli_version"
domain_status="$("$cli" domain-check atmospheric-science)" || fail '安装后的大气科学术语库检查失败。'
[[ "$domain_status" == *"atmospheric-science ${domain_version}，"* ]] ||
    fail "大气科学术语库版本与下载包不一致：$domain_status"
printf '\n%s\n%s\n\n%s\n%s\n' "$cli_version" "$domain_status" '已完成安装。CLI 路径' "$cli"
printf '可将 PaperLocale.app 放入“应用程序”并打开。若要从终端运行 CLI，请执行\n%q --help\n' "$cli"
