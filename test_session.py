#!/usr/bin/env python3
"""隔离测试 ASR 会话通路：合成 3 秒正弦波 PCM 发给千问实时接口，
不走麦克风、不按热键。用来定位 end_session 超时问题。

用法:  .venv/bin/python test_session.py
（正弦波不是语音，识别不出文字是正常的；要看的是事件流是否完整走到 finished）
"""

import math
import os
import queue
import struct
import sys
import threading
import time

import dashscope

from vtype import AsrSession, Overlay, Renderer


class FakeRecorder:
    """生成 3 秒 440Hz 正弦波，100ms 一块。"""

    def __init__(self):
        self.chunks = queue.Queue()

    def start(self):
        for i in range(30):
            buf = bytearray()
            for j in range(1600):
                t = (i * 1600 + j) / 16000
                buf += struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * t)))
            self.chunks.put(bytes(buf))

    def stop(self):
        pass


def main():
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key.startswith("sk-"):
        sys.exit("请先 export DASHSCOPE_API_KEY")
    dashscope.api_key = api_key

    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    renderer = Renderer(dry_run=True)
    s = AsrSession(renderer, Overlay(), "zh", debug=True)
    s.recorder = FakeRecorder()

    t = threading.Thread(target=s.run, daemon=True)
    t.start()
    time.sleep(3.5)  # 等音频发完
    print("--- 调用 stop()（现在是非阻塞的）---")
    t0 = time.time()
    s.stop()
    s.done.wait(timeout=40)
    print(f"--- 会话完全结束，松手到 finished 耗时约 {time.time() - t0 - 0:.1f}s ---")
    print("测试结束，run() 已退出" if s.done.is_set() else "run() 仍卡住！")


if __name__ == "__main__":
    main()
