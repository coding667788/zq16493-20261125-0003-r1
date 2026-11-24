"""支持 `python -m archivecut plan ...` 形式调用。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
