#!/usr/bin/env python3
"""vtype — 按住热键说话的流式语音输入。

架构（仿照真输入法的 预编辑/提交 模型）：
  - 正在识别的中间结果（text+stash）只进内存，显示在悬浮预览窗里，
    可以看到边说边调整的过程
  - 定稿的句子（completed 事件）才一次性粘贴进焦点输入框，
    只追加、永不退格 —— 不存在重叠和错删
  - 松手后若末尾句子没等到定稿，兜底把已知内容提交，不丢字

用法:
  DASHSCOPE_API_KEY=sk-xxx ./vtype.py                 # 按住 ScrollLock 说话
  ./vtype.py --key F9                                  # 换热键
  ./vtype.py --test-injection                          # 不识别，只测试注入和预览窗
  ./vtype.py --dry-run                                 # 不注入，只打印识别结果
"""

import argparse
import base64
import os
import queue
import select
import signal
import subprocess
import sys
import threading
import time

import dashscope
import evdev
from dashscope.audio.qwen_omni import (
    MultiModality,
    OmniRealtimeCallback,
    OmniRealtimeConversation,
)
from dashscope.audio.qwen_omni.omni_realtime import TranscriptionParams

ASR_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
ASR_MODEL = "qwen3-asr-flash-realtime"
AUDIO_CHUNK = 3200  # 100ms of 16kHz/16bit/mono PCM
FINISH_TIMEOUT = 30  # 松开后等服务端最终结果的最长时间
VAD_SILENCE_MS = 1200  # 静音多久算一句话说完（太小会把自然停顿切成碎句，满屏句号）


# ---------------------------------------------------------------- 悬浮预览窗

_atspi = {"tried": False, "ok": False, "Atspi": None}

def focus_window_rect():
    """AT-SPI 无障碍接口查询全局焦点窗口的屏幕坐标。

    GNOME Wayland 下其他进程拿不到焦点窗口位置（Shell.Eval 被禁、
    X 层 _NET_ACTIVE_WINDOW 对原生 Wayland 窗口为空），AT-SPI 是唯一通道。
    依赖系统 python3-gi + gir1.2-atspi-2.0；venv 没装就借系统 dist-packages。
    不可用时返回 None，调用方回退到鼠标指针定位。
    """
    if not _atspi["tried"]:
        _atspi["tried"] = True
        try:
            import gi
        except ImportError:
            sys.path.append("/usr/lib/python3/dist-packages")
            try:
                import gi
            except ImportError:
                return None
        try:
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi
            Atspi.init()
            _atspi["Atspi"] = Atspi
            _atspi["ok"] = True
        except Exception:
            return None
    if not _atspi["ok"]:
        return None
    Atspi = _atspi["Atspi"]
    try:
        desktop = Atspi.get_desktop(0)
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            for j in range(app.get_child_count()):
                try:
                    w = app.get_child_at_index(j)
                    if w.get_state_set().contains(Atspi.StateType.ACTIVE):
                        e = w.get_component_iface().get_extents(Atspi.CoordType.SCREEN)
                        return (e.x, e.y, e.width, e.height)
                except Exception:
                    continue
    except Exception:
        return None
    return None


class Overlay:
    """预编辑悬浮窗：显示正在识别、尚未定稿的文字。

    tkinter override-redirect 窗口：不抢键盘焦点、不出现在任务栏。
    show(text) 更新内容，show("") 隐藏。owner 令牌防止旧会话关掉新会话的预览。
    没有 tkinter 时静默禁用，不影响主功能。
    """

    def __init__(self):
        self._q = queue.Queue()
        self._owner = None
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def show(self, text, owner=None):
        with self._lock:
            if owner is not None:
                self._owner = owner
            self._q.put(("text", text))

    def hide(self, owner=None):
        with self._lock:
            if owner is not None and owner is not self._owner:
                return
            self._owner = None
            self._q.put(("text", ""))

    def set_clipboard(self, text):
        """由本进程（常驻 X11 窗口）直接充当剪贴板属主。
        不再使用 wl-copy：没有新进程、没有驻留进程、没有桥接幽灵窗口。"""
        self._q.put(("clip", text))

    def _run(self):
        try:
            import tkinter as tk
        except ImportError:
            return
        while True:
            try:
                root = tk.Tk()
                break
            except tk.TclError:
                # 图形会话还没就绪（开机自启早于 DISPLAY 导入），2 秒后重试
                time.sleep(2)
        root.withdraw()
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="#2b2b2b")
        label = tk.Label(win, text="", fg="#f0f0f0", bg="#2b2b2b",
                         font=("sans-serif", 14), justify="left",
                         wraplength=600, padx=14, pady=10)
        label.pack()
        win.withdraw()
        visible = [False]
        clip_text = [""]
        monitors = {"t": 0.0, "list": []}  # xrandr 输出缓存，5 秒有效

        def monitor_list():
            if time.monotonic() - monitors["t"] < 5:
                return monitors["list"]
            out = []
            try:
                import re, subprocess
                txt = subprocess.run(["xrandr", "--query"], capture_output=True,
                                     text=True, timeout=2).stdout
                for m in re.finditer(r" connected(?: primary)? (\d+)x(\d+)\+(\d+)\+(\d+)", txt):
                    w, h, x, y = (int(g) for g in m.groups())
                    out.append((x, y, w, h))
            except Exception:
                pass
            if not out:  # 退路：整块 X 屏幕
                out = [(0, 0, win.winfo_screenwidth(), win.winfo_screenheight())]
            monitors["t"] = time.monotonic()
            monitors["list"] = out
            return out

        def focused_monitor():
            # 优先跟焦点窗口（输入光标所在），取不到再跟鼠标指针；0.5 秒内缓存
            now = time.monotonic()
            if now - focused_monitor.t < 0.5 and focused_monitor.mon:
                return focused_monitor.mon
            focused_monitor.t = now
            rect = focus_window_rect()
            if rect:
                cx, cy = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
            else:
                cx, cy = win.winfo_pointerx(), win.winfo_pointery()
            for m in monitor_list():
                x, y, mw, mh = m
                if x <= cx < x + mw and y <= cy < y + mh:
                    focused_monitor.mon = m
                    return m
            focused_monitor.mon = monitor_list()[0]
            return focused_monitor.mon
        focused_monitor.t = 0.0
        focused_monitor.mon = None

        def place(w, h):
            x, y, mw, mh = focused_monitor()
            return x + (mw - w) // 2, y + mh - h - 90

        def serve_primary(offset, maxchars):
            # 有终端把 shift+insert 映射到 PRIMARY，剪贴板两个选择区都要供
            o, m = int(offset), int(maxchars)
            return clip_text[0][o:o + m]

        def poll():
            try:
                while True:
                    kind, text = self._q.get_nowait()
                    if kind == "clip":
                        clip_text[0] = text
                        win.clipboard_clear()          # CLIPBOARD
                        win.clipboard_append(text)
                        win.selection_handle(serve_primary, selection="PRIMARY")
                        win.selection_own(selection="PRIMARY")
                    elif text:
                        label.configure(text=text)
                        win.update_idletasks()
                        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
                        win.geometry("+%d+%d" % place(w, h))
                        if not visible[0]:
                            win.deiconify()
                            visible[0] = True
                    elif visible[0]:
                        win.withdraw()
                        visible[0] = False
            except queue.Empty:
                pass
            if visible[0]:
                # 浮窗实时跟随焦点窗口所在显示器（取不到焦点时跟鼠标指针）
                win.geometry("+%d+%d" % place(win.winfo_reqwidth(), win.winfo_reqheight()))
            root.after(60, poll)

        root.after(60, poll)
        root.mainloop()


# ---------------------------------------------------------------- 文字注入

class Renderer:
    """把已定稿的文本追加注入焦点输入框（唯一可行通道：剪贴板 + 模拟粘贴键）。

    GNOME/mutter 不提供 virtual-keyboard 和 input-method 协议，
    ydotool 的 Unicode 键位重映射又是 X11 技巧，所以中文只能走剪贴板。
    剪贴板由 Overlay 的常驻 tkinter 窗口直接充当属主（进程内更新），
    不再使用 wl-copy，没有新进程、没有驻留进程、没有桥接幽灵窗口。
    - 两次粘贴之间保留 PASTE_SETTLE 秒：给应用留异步取数的时间窗口
    - dotool 常驻进程：uinput 虚拟键盘只创建一次
    - owner 令牌：只有最近一次按键创建的会话可以提交
    """

    PASTE_SETTLE = 0.8  # 两次剪贴板写入的最小间隔

    def __init__(self, overlay, paste_key="shift+insert", dry_run=False):
        self.overlay = overlay
        self.owner = None
        self.paste_key = paste_key
        self.dry_run = dry_run
        self._cv = threading.Condition()
        self._pending = []  # [(owner, text)] 提交是追加语义，用队列
        self._dotool_proc = None
        self._injecting = False
        threading.Thread(target=self._worker, daemon=True).start()

    def wait_idle(self, timeout=10.0):
        """等所有已提交的文本真正注入完成（用于测试脚本退出前排空队列）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._cv:
                if not self._pending and not self._injecting:
                    return True
            time.sleep(0.05)
        return False

    def acquire(self, owner):
        with self._cv:
            self.owner = owner

    def commit(self, text, owner=None):
        # 换行在聊天框里等于"发送"，必须剔除
        text = text.replace("\r", "").replace("\n", " ")
        if not text.strip():
            return
        with self._cv:
            self._pending.append((owner, text))
            self._cv.notify()

    def _worker(self):
        while True:
            with self._cv:
                while not self._pending:
                    self._cv.wait()
                owner, text = self._pending.pop(0)
                if owner is not None and owner is not self.owner:
                    continue
                # 相邻提交都是追加语义，合并成一次粘贴，减少剪贴板操作
                while (self._pending
                       and (self._pending[0][0] is None or self._pending[0][0] is owner)):
                    text += self._pending.pop(0)[1]
            try:
                if self.dry_run:
                    print(text, end="", flush=True)
                else:
                    self._injecting = True
                    try:
                        self._paste(text)
                    finally:
                        self._injecting = False
            except Exception as e:
                print(f"\n[警告] 注入失败: {e}", file=sys.stderr)

    def _paste(self, text):
        self.overlay.set_clipboard(text)  # 由常驻 tkinter 窗口充当剪贴板属主
        time.sleep(0.15)  # 等 tk 事件循环处理剪贴板更新
        self._dotool(f"key {self.paste_key}")
        time.sleep(self.PASTE_SETTLE - 0.15)  # 给应用留足异步取数时间

    def _dotool(self, command):
        for _ in range(2):  # 进程挂了重试一次
            try:
                if self._dotool_proc is None or self._dotool_proc.poll() is not None:
                    self._dotool_proc = subprocess.Popen(
                        ["dotool"], stdin=subprocess.PIPE, text=True)
                self._dotool_proc.stdin.write(command + "\n")
                self._dotool_proc.stdin.flush()
                return
            except (BrokenPipeError, OSError):
                self._dotool_proc = None


# ---------------------------------------------------------------- 录音

class Recorder:
    def __init__(self):
        self.chunks = queue.Queue()
        self._proc = None

    def start(self):
        self._proc = subprocess.Popen(
            ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", "16000", "-c", "1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            data = self._proc.stdout.read(AUDIO_CHUNK)
            if not data:
                break
            self.chunks.put(data)

    def stop(self):
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()


# ---------------------------------------------------------------- ASR 会话

class _Callback(OmniRealtimeCallback):
    def __init__(self, session):
        self.s = session

    def on_open(self):
        if self.s.debug:
            print("[event] on_open")

    def on_close(self, code, msg):
        if self.s.debug:
            print(f"[event] on_close code={code} msg={msg}")
        self.s.finished.set()

    def on_event(self, resp):
        t = resp.get("type", "")
        if self.s.debug:
            print(f"[event] {t}")
        if t == "session.updated":
            self.s.ready.set()
            self.s.status("● 正在听…")
        elif t == "conversation.item.input_audio_transcription.text":
            # 中间结果：只更新内存和预览窗，不上屏
            self.s.current = resp.get("text", "") + resp.get("stash", "")
            self.s.overlay.show(self.s.current or "● 正在听…", owner=self.s)
        elif t == "conversation.item.input_audio_transcription.completed":
            # 定稿：一次性提交到输入框；会话还在继续，预览窗回到"正在听"
            self.s.current = ""
            self.s.commit_transcript(resp.get("transcript", ""))
            self.s.status("● 正在听…")
        elif t == "session.finished":
            self.s.finished.set()
        elif t == "error":
            print(f"\n[ASR 错误] {resp.get('error', resp)}", file=sys.stderr)
            self.s.finished.set()


class AsrSession:
    """一次按键对应一个会话：连接期间音频在本地排队，连接成功后立刻补发。

    run() 在后台线程里走完整个生命周期；stop() 只发信号、立即返回，
    等服务端最终结果（可能数秒）不会卡住热键响应。
    """

    def __init__(self, renderer, overlay, language, debug=False):
        self.renderer = renderer
        self.overlay = overlay
        self.language = language
        self.debug = debug
        self.finals = ""     # 服务端定稿的全文
        self.committed = ""  # 已提交到输入框的部分（finals 的前缀）
        self.current = ""    # 当前句子的中间结果
        self.ready = threading.Event()      # session.updated 收到
        self.released = threading.Event()   # 用户松开了热键
        self.finished = threading.Event()   # 服务端 finished / 出错 / 连接关闭
        self.done = threading.Event()       # run() 完全退出
        self._stop_send = threading.Event()
        self._sender_done = threading.Event()
        self.recorder = Recorder()
        self.conversation = OmniRealtimeConversation(
            model=ASR_MODEL, url=ASR_URL, callback=_Callback(self)
        )

    def commit_transcript(self, transcript):
        self.finals += transcript
        self.renderer.commit(transcript, owner=self)
        self.committed += transcript

    def status(self, text):
        """在预览窗显示状态信息（连接中/正在听/收尾中/异常）。"""
        self.overlay.show(text, owner=self)

    def run(self):
        self.renderer.acquire(self)
        self.status("● 连接中…")
        self.recorder.start()
        threading.Thread(target=self._sender, daemon=True).start()
        try:
            self._connect()
            self.conversation.update_session(
                output_modalities=[MultiModality.TEXT],
                enable_turn_detection=True,
                turn_detection_type="server_vad",
                turn_detection_threshold=0.2,
                turn_detection_silence_duration_ms=VAD_SILENCE_MS,
                enable_input_audio_transcription=True,
                transcription_params=TranscriptionParams(
                    language=self.language,
                    sample_rate=16000,
                    input_audio_format="pcm",
                ),
            )
            end_sent = False
            end_deadline = None
            while not self.finished.wait(0.2):
                if self.released.is_set() and not end_sent:
                    end_sent = True
                    if not self.ready.is_set():
                        print("\n[警告] 会话未就绪，本次识别取消", file=sys.stderr)
                        self.status("⚠ 连接超时，本次识别取消")
                        time.sleep(2.5)  # 让用户来得及看到
                        break
                    self.status("○ 收尾中…")
                    self._sender_done.wait(timeout=5)  # 把剩余音频发完
                    # 收尾的网络写操作放独立线程：网络死掉时 send 可能永久阻塞，
                    # 绝不能因此让会话退不出（否则浮窗常挂、文字丢失）
                    threading.Thread(target=self._end_async_safe, daemon=True).start()
                    end_deadline = time.time() + FINISH_TIMEOUT
                if end_deadline and time.time() > end_deadline:
                    print("\n[警告] 等待最终结果超时", file=sys.stderr)
                    self.status("⚠ 等待结果超时，已提交内容保留")
                    time.sleep(2.5)
                    break
        except Exception as e:
            print(f"\n[警告] 识别服务连接失败: {e}（本次取消，网络恢复后直接用）",
                  file=sys.stderr)
            self.status("⚠ 网络异常，本次识别取消")
            time.sleep(2.5)
        finally:
            threading.Thread(target=self._close_safe, daemon=True).start()
            self.recorder.stop()
            # 兜底：没提交的部分（包括没等到定稿的当前句）补提交，不丢字
            missing = (self.finals + self.current)[len(self.committed):]
            if missing:
                self.renderer.commit(missing, owner=self)
            self.overlay.hide(owner=self)
            self.done.set()

    def _end_async_safe(self):
        try:
            self.conversation.end_session_async()
        except Exception:
            pass

    def _close_safe(self):
        try:
            self.conversation.close()
        except Exception:
            pass

    def _connect(self):
        """连接识别服务，网络抖动时自动重试一次。"""
        for attempt in (1, 2):
            try:
                self.conversation.connect()
                return
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(1)
                self.conversation = OmniRealtimeConversation(
                    model=ASR_MODEL, url=ASR_URL, callback=_Callback(self)
                )

    def stop(self):
        """松开热键：停录音、发信号，立即返回；收尾在后台完成。"""
        self.recorder.stop()
        self._stop_send.set()
        self.released.set()

    def abort(self):
        """新一次按键开始，强制结束本会话。"""
        self.stop()
        self.finished.set()

    def _sender(self):
        try:
            # 等会话就绪；连接失败（run 退出）时立刻放弃
            deadline = time.time() + 15
            while not self.ready.wait(0.5):
                if self.done.is_set() or time.time() > deadline:
                    if not self.done.is_set():
                        print("\n[警告] 等待服务端就绪超时，音频未发送", file=sys.stderr)
                    return
            while True:
                try:
                    chunk = self.recorder.chunks.get(timeout=0.2)
                except queue.Empty:
                    if self._stop_send.is_set():
                        return
                    continue
                if self.finished.is_set():
                    return
                try:
                    self.conversation.append_audio(
                        base64.b64encode(chunk).decode("ascii"))
                except Exception:
                    return
        finally:
            self._sender_done.set()


# ---------------------------------------------------------------- 热键

TAP_MAX = 0.4  # 秒：短按视为点按切换（Pause 等键按下即自发松开，天然落入此分支）

def find_key_devices(key_codes):
    devices = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        caps = dev.capabilities()
        if evdev.ecodes.EV_KEY in caps and any(k in caps[evdev.ecodes.EV_KEY] for k in key_codes):
            devices.append(dev)
    return devices


def hotkey_loop(key_codes, renderer, overlay, language, debug=False):
    devices = find_key_devices(key_codes)
    if not devices:
        sys.exit("找不到能按下该热键的输入设备（确认在 input 组且已重新登录）")
    print(f"监听热键设备: {', '.join(d.name for d in devices)}", flush=True)
    recording = None   # 正在录音的会话
    last = None        # 上一次会话（可能还在后台收尾）
    pressed_at = None  # 当前按下的时刻
    tapped = False     # 当前会话由点按开启，等下次点按结束
    while True:
        if not devices:  # 键盘全部拔出，等重新插入
            time.sleep(1.0)
            devices = find_key_devices(key_codes)
            continue
        readable, _, _ = select.select(devices, [], [], 2.0)
        if not readable:
            # 周期性热插拔扫描：新插入的键盘加入监听
            current = {d.path for d in devices}
            for d in find_key_devices(key_codes):
                if d.path not in current:
                    print(f"热键设备上线: {d.name}", flush=True)
                    devices.append(d)
            continue
        for dev in readable:
            try:
                events = dev.read()
            except OSError:  # 设备被拔出
                print(f"热键设备掉线: {dev.name}", flush=True)
                devices.remove(dev)
                continue
            for event in events:
                if event.type != evdev.ecodes.EV_KEY or event.code not in key_codes:
                    continue
                if event.value == 1:  # 按下
                    if recording is None:
                        if last is not None and not last.done.is_set():
                            last.abort()
                        print("● 录音中...", flush=True)
                        recording = AsrSession(renderer, overlay, language, debug=debug)
                        threading.Thread(target=recording.run, daemon=True).start()
                        pressed_at = time.monotonic()
                        tapped = False
                    elif tapped:
                        print("○ 再次点按，后台收尾中...", flush=True)
                        recording.stop()
                        last = recording
                        recording = None
                        tapped = False
                elif event.value == 0 and recording is not None and pressed_at is not None:  # 松开
                    held = time.monotonic() - pressed_at
                    pressed_at = None
                    if held < TAP_MAX:
                        # 短按（或 Pause 这类按下即自发松开的键）→ 点按模式，保持录音
                        tapped = True
                        print("● 点按模式：再按一次热键结束", flush=True)
                    else:
                        print("○ 松手，后台收尾中...", flush=True)
                        recording.stop()
                        last = recording
                        recording = None


# ---------------------------------------------------------------- 入口

def test_injection(renderer, overlay):
    print("3 秒后开始测试，请把焦点放到一个输入框...", flush=True)
    time.sleep(3)
    overlay.show("正在识别：你好……")
    time.sleep(1.2)
    overlay.show("正在识别：你好，这是一段语音……（中间结果会调整）")
    time.sleep(1.2)
    renderer.commit("你好，这是一段语音输入测试。")
    overlay.hide()
    time.sleep(0.5)
    renderer.commit("定稿的句子直接上屏，中间结果只出现在预览窗。")
    renderer.wait_idle()  # 等两句都真正贴完再退出（worker 是 daemon 线程，进程退出会杀死在途粘贴）
    print("测试完成：预览窗应显示过两行中间结果，输入框应追加了两个定稿句子。")


def main():
    p = argparse.ArgumentParser(description="按住热键说话的流式语音输入（千问实时 ASR）")
    p.add_argument("--key", action="append", default=None,
                   help="热键名，如 SCROLLLOCK、PAUSE、F9；可重复指定多个，默认 SCROLLLOCK")
    p.add_argument("--language", default="zh", help="识别语言（zh/yue/en/ja...）")
    p.add_argument("--paste-key", default="shift+insert",
                   help="粘贴快捷键；shift+insert 通吃终端和 GUI，备选 ctrl+v / ctrl+shift+v")
    p.add_argument("--dry-run", action="store_true", help="只打印识别结果，不注入")
    p.add_argument("--test-injection", action="store_true", help="只测试文字注入和预览窗")
    p.add_argument("--debug", action="store_true", help="打印所有 ASR 事件和 SDK 日志")
    args = p.parse_args()

    overlay = Overlay()
    renderer = Renderer(overlay, paste_key=args.paste_key, dry_run=args.dry_run)

    if args.test_injection:
        test_injection(renderer, overlay)
        return

    if args.debug:
        import logging
        logging.basicConfig(level=logging.DEBUG,
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")

    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key.startswith("sk-"):
        sys.exit("请先设置环境变量 DASHSCOPE_API_KEY（阿里云百炼的 sk- 开头 Key）")
    dashscope.api_key = api_key

    key_names = args.key or ["SCROLLLOCK"]
    key_codes = []
    for name in key_names:
        full = name if name.startswith("KEY_") else "KEY_" + name
        code = evdev.ecodes.ecodes.get(full)
        if code is None:
            sys.exit(f"未知按键名: {name}")
        key_codes.append(code)

    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    print(f"就绪：按住 {'/'.join(key_names)} 说话，松开出字；短按一下则再按一次结束（Ctrl+C 退出）", flush=True)
    hotkey_loop(key_codes, renderer, overlay, args.language, debug=args.debug)


if __name__ == "__main__":
    main()
