# -*- coding: utf-8 -*-
"""
GUI 版 exe 端到端验证
====================
启动打包好的 obs_meeting_guard.exe(--dry-run 模拟运行), 通过 Win32 消息
模拟点击"开始守护"按钮, 验证守护线程在 GUI 进程中完整跑起来, 最后结束
进程并检查 guard.log 关键日志。
用法: python e2e_gui_exe.py [exe路径]
"""
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.EnumChildWindows.argtypes = [wintypes.HWND,
                                    ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                                       wintypes.LPARAM),
                                    wintypes.LPARAM]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
BM_CLICK = 0x00F5


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else "obs_meeting_guard.exe"
    proc = subprocess.Popen([exe, "--dry-run"])
    time.sleep(3.0)  # 等窗口创建

    hwnd = user32.FindWindowW("OBSMeetingGuardWindow", None)
    print("主窗口 hwnd =", hwnd)
    if not hwnd:
        proc.kill()
        print("[失败] 未找到 GUI 窗口")
        return 1

    buttons = []

    def enum_child(h, lparam):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetWindowTextW(h, buf, 64)
        if buf.value == "开始守护":
            buttons.append(h)
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(enum_child), 0)
    print("开始守护按钮 hwnd =", buttons[0] if buttons else None)
    if not buttons:
        proc.kill()
        print("[失败] 未找到'开始守护'按钮")
        return 1

    user32.PostMessageW(buttons[0], BM_CLICK, 0, 0)
    print("已模拟点击'开始守护'(模拟运行模式)")

    time.sleep(7.0)  # 让守护跑一会儿
    subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)], capture_output=True)
    try:
        proc.wait(timeout=10)
    except Exception:
        pass
    print("进程已结束")

    try:
        with open("guard.log", encoding="utf-8") as f:
            log = f.read()
    except OSError as exc:
        print("[失败] 读取 guard.log 失败: %s" % exc)
        return 1

    checks = [
        ("守护启动横幅", "OBS 会议守护 v1.0.0 启动" in log),
        ("模拟运行生效", "模拟运行" in log),
        ("界面点击生效", "守护已启动" in log),
        ("OBS 已连接", "已连接 OBS WebSocket" in log),
        ("音频采集启动", "桌面音频采集已启动" in log),
        ("未误停止录屏", "已自动停止 OBS 录屏" not in log),
    ]
    failed = 0
    for name, passed in checks:
        print("[%s] %s" % ("OK" if passed else "失败", name))
        if not passed:
            failed += 1
    print("-" * 50)
    print("GUI exe 端到端: %d 通过, %d 失败" % (len(checks) - failed, failed))
    return failed


if __name__ == "__main__":
    sys.exit(main())
