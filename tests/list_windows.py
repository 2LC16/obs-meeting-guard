# -*- coding: utf-8 -*-
"""
窗口枚举诊断工具
================
枚举当前所有顶层窗口, 显示: 进程名 / PID / 标题 / 类名 / 位置 / 尺寸 / 可见性。

用途:
  1. 调试"腾讯会议/企业微信会议"窗口检测: 开会时运行本工具,
     找到会议窗口的标题和类名, 填到 config.ini 的 [meeting] 段;
  2. 确认企业微信会议是由哪个进程承载的(wwmapp.exe / wemeetapp.exe / WXWorkWeb.exe)。

用法:
  python list_windows.py              # 列出所有窗口
  python list_windows.py wxwork       # 只列出进程名或标题包含 wxwork 的窗口(不区分大小写)
  python list_windows.py 会议         # 按关键词过滤
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

# ---------------------------------------------------------------- Win32 声明
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
                               wintypes.LPARAM]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def get_process_name(pid):
    """根据 PID 获取进程 exe 文件名(小写), 失败返回 None"""
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(handle)
    return None


def main():
    keyword = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    rows = []

    def enum_proc(hwnd, lparam):
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = get_process_name(pid.value) or "?"
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        row = {
            "exe": name, "pid": pid.value, "title": title.value, "class": cls.value,
            "x": rect.left, "y": rect.top,
            "w": rect.right - rect.left, "h": rect.bottom - rect.top,
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "minimized": bool(user32.IsIconic(hwnd)),
        }
        if not keyword or keyword in name or keyword in title.value.lower():
            rows.append(row)
        return True

    user32.EnumWindows(WNDENUMPROC(enum_proc), 0)

    print("%-22s %8s  %-34s %-26s %6s %6s %5s %5s %3s %3s" % (
        "进程", "PID", "标题", "类名", "X", "Y", "宽", "高", "可见", "最小"))
    print("-" * 130)
    for r in sorted(rows, key=lambda r: (r["exe"], -r["w"] * r["h"])):
        title = r["title"] if len(r["title"]) <= 32 else r["title"][:31] + "~"
        cls = r["class"] if len(r["class"]) <= 24 else r["class"][:23] + "~"
        print("%-22s %8d  %-34s %-26s %6d %6d %5d %5d %3s %3s" % (
            r["exe"], r["pid"], title, cls, r["x"], r["y"], r["w"], r["h"],
            "Y" if r["visible"] else "N", "Y" if r["minimized"] else "N"))
    print("-" * 130)
    print("共 %d 个窗口%s" % (len(rows), ("(关键词: %s)" % keyword) if keyword else ""))


if __name__ == "__main__":
    main()
