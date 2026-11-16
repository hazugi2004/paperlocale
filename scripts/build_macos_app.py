"""构建 Universal 2 原生启动器；不打包用户环境、论文或任何登录凭据。

输入是当前 checkout 的 Swift 源码、pyproject 版本和可选的同版本 wheel，
输出为 dist 下的 .app 和 ZIP。指定 wheel 时，ZIP 也提供 CLI 安装命令。
只使用 macOS/Xcode Command Line Tools；无 Developer ID 时使用 ad-hoc 签名，
不能将其描述为 Apple 已签名公证的独立翻译安装包。
"""
from pathlib import Path
import argparse
import json
import plistlib
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib
from zipfile import ZipFile
from PIL import Image


def main():
    root = Path(__file__).resolve().parents[1]
    if sys.platform != "darwin":
        raise SystemExit("macOS app 构建需要 macOS 与 Xcode Command Line Tools")
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=root / "dist",
                        help="构建产物目录；File Provider 自动附加 FinderInfo 时请选择本机非同步目录")
    parser.add_argument("--wheel", type=Path, help="随 ZIP 分发的同版本 PaperLocale wheel")
    args = parser.parse_args()
    wheel = args.wheel.expanduser().resolve() if args.wheel else None
    if wheel is not None:
        if not wheel.is_file() or wheel.name != f"paperlocale-{version}-py3-none-any.whl":
            parser.error(f"--wheel 必须指向已构建的 paperlocale-{version}-py3-none-any.whl")
        # 从即将分发的 wheel 读取术语库版本，安装命令据此验证本机 CLI 加载的领域包。
        with ZipFile(wheel) as distribution:
            manifest = json.loads(distribution.read("paperlocale/packs/atmospheric-science/manifest.json"))
    destination = args.output_dir.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    app = destination / "PaperLocale.app"
    binary_dir = app / "Contents" / "MacOS"
    resources = app / "Contents" / "Resources"
    binary_dir.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    build = root / "build" / "macos"
    build.mkdir(parents=True, exist_ok=True)
    # 两种 CPU 均由同一源码编译；运行验证仍需分别报告实际测试过的架构。
    binaries = []
    for arch in ("arm64", "x86_64"):
        binary = build / f"PaperLocale-{arch}"
        subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                        "-O", "-target", f"{arch}-apple-macosx13.0",
                        str(root / "macos" / "PaperLocaleApp.swift"),
                        str(root / "macos" / "TranslationOptions.swift"), "-o", str(binary)], check=True)
        binaries.append(str(binary))
    subprocess.run(["lipo", "-create", *binaries, "-output", str(binary_dir / "PaperLocale")], check=True)
    # 直接核对实际 Mach-O 架构，避免不同 Xcode 的 -verify_arch 参数解析差异。
    architectures = set(subprocess.check_output(
        ["lipo", "-archs", str(binary_dir / "PaperLocale")], text=True).split())
    if architectures != {"arm64", "x86_64"}:
        raise RuntimeError(f"Universal 2 架构不完整：{architectures}")
    # 从同一高分辨率图源生成 macOS 的标准 1x/2x 图标集合；保留透明通道。
    iconset = build / "AppIcon.iconset"
    iconset.mkdir(exist_ok=True)
    with Image.open(root / "macos/Assets/AppIcon.png") as icon:
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                suffix = "@2x" if scale == 2 else ""
                icon.resize((size*scale, size*scale), Image.Resampling.LANCZOS).save(
                    iconset / f"icon_{size}x{size}{suffix}.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(resources / "AppIcon.icns")], check=True)
    for name in ("AppIcon.png", "ProgressEgg.png"):
        (resources / name).write_bytes((root / "macos/Assets" / name).read_bytes())
    metadata = {"CFBundleName": "PaperLocale", "CFBundleDisplayName": "PaperLocale",
                "CFBundleIdentifier": "io.github.hazugi2004.paperlocale",
                "CFBundleExecutable": "PaperLocale", "CFBundleIconFile": "AppIcon.icns", "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": version, "CFBundleVersion": version,
                "PaperLocaleBundledCLI": wheel is not None,
                "LSMinimumSystemVersion": "13.0", "NSHighResolutionCapable": True,
                "NSHumanReadableCopyright": "PaperLocale contributors · AGPL-3.0-only"}
    if wheel is not None:
        metadata["PaperLocaleAtmosphericScienceVersion"] = manifest["version"]
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(metadata))
    for source, name in [(root / "LICENSE", "LICENSE"),
                         (root / "macos" / "README.md", "README.md")]:
        (resources / name).write_bytes(source.read_bytes())
    # iCloud/File Provider 所在源码目录可能给新 app 附加 FinderInfo，
    # codesign 会拒绝这种构建产物；只清理本次生成的 app，不触碰源文件。
    subprocess.run(["xattr", "-cr", str(app)], check=True)
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    archive = destination / f"PaperLocale-{version}-macOS-universal2.zip"
    archive.unlink(missing_ok=True)
    if wheel is None:
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app), str(archive)], check=True)
    else:
        # 暂存目录使 App、同一份 wheel 与安装命令处于 ZIP 的同一级。
        # ditto 保留 macOS 应用的文件属性和签名，安装命令可以从 Finder 执行。
        with TemporaryDirectory() as directory:
            staging = Path(directory)
            subprocess.run(["ditto", str(app), str(staging / app.name)], check=True)
            shutil.copy2(wheel, staging / wheel.name)
            installer = staging / "Install PaperLocale CLI.command"
            shutil.copy2(root / "macos" / installer.name, installer)
            installer.chmod(0o755)
            subprocess.run(["ditto", "-c", "-k", str(staging), str(archive)], check=True)
    print(archive)


if __name__ == "__main__":
    main()
