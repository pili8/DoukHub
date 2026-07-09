#!/bin/bash
# DoukHub 一键启动脚本
cd "$(dirname "$0")"

# 检查虚拟环境是否完整
if [ ! -f "venv/bin/activate" ]; then
    echo "正在初始化虚拟环境..."
    rm -rf venv
    python3 -m venv venv
    if [ ! -f "venv/bin/activate" ]; then
        echo "错误：创建虚拟环境失败"
        echo "请确认已安装 Python 3.10+：python3 --version"
        read -p "按回车键关闭窗口..."
        exit 1
    fi
    source venv/bin/activate
    echo "正在安装依赖（首次需要1~2分钟）..."
    pip install -r requirements.txt
    echo "初始化完成！"
else
    source venv/bin/activate
fi

echo "================================"
echo "  DoukHub 正在启动"
echo "  本机访问: http://127.0.0.1:2999"
echo "  局域网访问: http://$(ipconfig getifaddr en0 2>/dev/null || echo '请查看本机IP'):2999"
echo "  按 Ctrl+C 退出"
echo "================================"

# 自动打开浏览器（等服务完全启动后再打开）
(sleep 4 && open "http://127.0.0.1:2999" 2>/dev/null) &

python main.py

echo ""
echo "DoukHub 已停止运行。"
read -p "按回车键关闭窗口..."
