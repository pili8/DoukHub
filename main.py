"""DoukHub 入口"""
import sys
from pathlib import Path

# 确保 app 包可导入（指向项目根目录，不是 main.py 文件本身）
sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    try:
        from app.main import run
        run()
    except Exception as exc:
        if type(exc).__name__ == "DataRootError":
            print(str(exc), file=sys.stderr)
            input("按 Enter 关闭...")
        else:
            raise
