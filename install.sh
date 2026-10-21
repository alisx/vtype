#!/usr/bin/env bash
# vtype 一键安装（Ubuntu 24.04+ / GNOME Wayland）
set -euo pipefail
cd "$(dirname "$0")"

echo "==> 安装系统依赖"
sudo apt update
sudo apt install -y alsa-utils wl-clipboard git golang-go gcc \
    libxkbcommon-dev scdoc python3-venv python3-tk

echo "==> 安装 dotool（Ubuntu 源里没有，从源码编译）"
if ! command -v dotool >/dev/null 2>&1; then
    tmp=$(mktemp -d)
    if ! git clone https://git.sr.ht/~geb/dotool "$tmp/dotool"; then
        # SourceHut 不通时走 GitHub 镜像
        git clone https://github.com/nick-tgcs/dotool "$tmp/dotool"
    fi
    (cd "$tmp/dotool" && ./build.sh && sudo ./build.sh install)
    rm -rf "$tmp"
    sudo modprobe uinput
    sudo udevadm control --reload && sudo udevadm trigger
else
    echo "    dotool 已存在，跳过"
fi

echo "==> 把当前用户加入 input 组（热键监听和文字注入需要）"
if ! groups | grep -qw input; then
    sudo usermod -aG input "$USER"
    NEED_RELOGIN=1
else
    NEED_RELOGIN=0
fi

echo "==> 安装 Python 依赖"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

echo
echo "安装完成。接下来："
echo "  1. 去阿里云百炼开通服务拿 API Key（sk- 开头）"
echo "  2. export DASHSCOPE_API_KEY=\"sk-你的Key\""
echo "  3. .venv/bin/python vtype.py --test-injection   # 验证注入"
echo "  4. .venv/bin/python vtype.py                    # 按住 ScrollLock 说话"
if [ "$NEED_RELOGIN" = "1" ]; then
    echo
    echo "!! 你刚被加入 input 组，需要先【注销重新登录】再运行，否则热键不生效"
fi
