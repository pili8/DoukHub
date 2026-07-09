"""DoukHub 入口"""
import sys
from pathlib import Path

# 确保 app 包可导入
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import run

if __name__ == "__main__":
    run()
