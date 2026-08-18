"""允许使用 ``python -m paperlocale`` 启动命令行。"""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
