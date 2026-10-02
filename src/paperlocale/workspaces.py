"""统一工作区规则：根目录可选，每份源文件用路径摘要区分同名论文。"""
from pathlib import Path
from hashlib import sha256


def default_workspace() -> Path:
    return Path.home() / 'paperlocale'


def run_directory(source: Path, workspace: Path | None = None) -> Path:
    # 身份基于规范化路径，不因内容修改悄悄换断点；源文件内容仍由 manifest 哈希核验。
    source = source.expanduser().resolve()
    key = sha256(str(source).encode('utf-8')).hexdigest()[:12]
    return (workspace or default_workspace()).expanduser().resolve() / (source.stem + '-' + key)
