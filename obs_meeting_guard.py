#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OBS 会议守护 (OBS Meeting Guard)
==========================================================================
功能: 在后台盯着"腾讯会议"(含企业微信内置的融合版腾讯会议), 当出现以下
      任一情况时, 自动停止 OBS 录屏:
        1) 腾讯会议结束(会议窗口消失超过宽限期, 默认 30 秒);
        2) 画面连续静止超过设定时长(默认 900 秒 = 15 分钟);
           —— 仅当桌面音频同时处于安静状态(低于阈值)时才触发:
              会议仍在发声(> -45 dB)时, 即使画面静止也不会停止录屏;
        3) 桌面音频电平(系统输出混音, 不含麦克风)持续低于设定阈值
           (默认 -45 dB)超过设定时长(默认 720 秒 = 12 分钟)。

特点:
  - 纯 Python 标准库实现, 运行无需安装任何第三方依赖;
  - 自带极简 OBS WebSocket 客户端(实现 obs-websocket v5 协议);
  - WASAPI 环回采集桌面音频; ctypes 直接截屏做画面静止检测;
  - 会议检测同时覆盖: 独立版腾讯会议(wemeetapp.exe)与企业微信
    (WXWork.exe / WXWorkWeb.exe, 融合版会议窗口类名为 Meeting* / Qt*);
  - 支持 --selftest 一键自检, --dry-run 模拟运行, --list-windows 查看窗口;
  - 可用 PyInstaller 打包为单文件 exe(见 build_exe.bat)。

用法:
  python obs_meeting_guard.py                  # 正常守护
  python obs_meeting_guard.py --selftest       # 自检(屏幕/音频/会议/OBS)
  python obs_meeting_guard.py --dry-run        # 模拟运行(只记录不真正停止)
  python obs_meeting_guard.py --list-windows   # 列出所有窗口(调试用)

环境要求: Windows 10/11 + Python 3.8 及以上; OBS Studio 28 及以上
          (已内置 WebSocket 服务器, 默认端口 4455)。
"""
import argparse
import base64
import configparser
import ctypes
import hashlib
import json
import logging
import math
import os
import queue
import socket
import struct
import sys
import threading
import time
import uuid
from array import array
from logging.handlers import RotatingFileHandler

# ------------------------------------------------------------------ 平台检查
if sys.platform != "win32":
    print("错误: 本工具仅支持 Windows 平台。")
    sys.exit(2)

from ctypes import wintypes  # noqa: E402  (放在平台检查之后导入)

APP_NAME = "OBS 会议守护"
VERSION = "1.0.0"

# ============================================================================
#  默认配置(与 config.ini 模板保持一致)
# ============================================================================
DEFAULT_CONFIG = {
    "obs": {
        "host": "127.0.0.1",
        "port": "4455",
        "password": "",
        "auto_read_obs_password": "true",
    },
    "guard": {
        "check_interval_sec": "1",
        "meeting_gone_grace_sec": "30",
        "static_check_interval_sec": "30",
        "static_duration_sec": "900",
        "static_diff_threshold": "0.5",
        "quiet_threshold_db": "-45",
        "quiet_duration_sec": "720",
        "static_require_quiet": "true",
        "static_quiet_require_meeting": "false",
        "auto_start_recording": "false",
    },
    "meeting": {
        "process_names": "wemeetapp.exe,WXWork.exe,WXWorkWeb.exe",
        "min_width": "500",
        "min_height": "350",
        "meeting_class_keywords": "Meeting,Qt",
        "meeting_title_keywords": "",
        "class_exclude_keywords": ("PerryShadow,TitleBar,IME,MSCTF,GDI+,SystemEventMonitor,"
                                   "Device Monitor,Thread Task,crashpad,StatusTray,"
                                   "PowerMessage,temp_d3d,Chrome_WidgetWin_0,SearchResult,"
                                   "WeworkAppBar,WeWorkNewTips"),
        "home_titles": "企业微信,微信,腾讯会议,Tencent Meeting,Voov Meeting",
        "title_exclude_keywords": ("加入会议,快速会议,预定会议,设置,邀请,等候室,网络检测,"
                                   "登录,升级,智能助理,文档,日程,邮件,通讯录,微盘,汇报,"
                                   "审批,打卡,客户,直播,收集表,搜索,通话,下载"),
    },
    "log": {
        "level": "INFO",
        "file": "guard.log",
    },
    "gui": {
        "always_on_top": "false",
        "notify_on_stop": "true",
    },
}

# ============================================================================
#  Win32 ctypes 基础声明
# ============================================================================
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32
ole32 = ctypes.windll.ole32

# 注意: 64 位系统下句柄/指针必须用 c_void_p 类型的返回值, 否则高位会被截断
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.SetProcessDPIAware.argtypes = []
user32.SetProcessDPIAware.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, ctypes.c_uint, ctypes.c_uint,
                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.GetLastError.restype = wintypes.DWORD

ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = ctypes.c_long
ole32.CoUninitialize.argtypes = []
ole32.CoUninitialize.restype = None
# 注意: CoCreateInstance 的两个 GUID 参数类型在 GUID 类定义之后再声明(见下方)
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.restype = None

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# COM 接口 GUID(已与 Windows SDK audioclient.h / mmdeviceapi.h 核对)
CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAudioClient = "{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}"
IID_IAudioCaptureClient = "{C8ADBD64-E71E-48A0-A4DE-185C395CD317}"

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
CLSCTX_ALL = 0x17
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
COINIT_MULTITHREADED = 0x0

# ---- COM 虚方法表索引(每个接口都继承 IUnknown 的前 3 个方法) ----
# 顺序已与 Windows SDK 的 audioclient.h / mmdeviceapi.h 逐一核对
IUNKNOWN_ADDREF = 1
IUNKNOWN_RELEASE = 2
IMMDEVICEENUMERATOR_GETDEFAULTAUDIOENDPOINT = 4   # 3=EnumAudioEndpoints, 5=GetDevice
IMMDEVICE_ACTIVATE = 3                              # 4=OpenPropertyStore, 5=GetId, 6=GetState
IAUDIOCLIENT_INITIALIZE = 3
IAUDIOCLIENT_GETBUFFERSIZE = 4
IAUDIOCLIENT_GETSTREAMLATENCY = 5
IAUDIOCLIENT_GETCURRENTPADDING = 6
IAUDIOCLIENT_ISFORMATSUPPORTED = 7
IAUDIOCLIENT_GETMIXFORMAT = 8
IAUDIOCLIENT_GETDEVICEPERIOD = 9
IAUDIOCLIENT_START = 10
IAUDIOCLIENT_STOP = 11
IAUDIOCLIENT_RESET = 12
IAUDIOCLIENT_SETEVENTHANDLE = 13
IAUDIOCLIENT_GETSERVICE = 14
IAUDIOCAPTURECLIENT_GETBUFFER = 3
IAUDIOCAPTURECLIENT_RELEASEBUFFER = 4
IAUDIOCAPTURECLIENT_GETNEXTPACKETSIZE = 5


class GUID(ctypes.Structure):
    """COM GUID 结构体"""
    _fields_ = [("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


def make_guid(text):
    """把 "{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}" 形式的字符串转成 GUID 结构体"""
    parts = text.strip("{}").split("-")
    g = GUID()
    g.Data1 = int(parts[0], 16)
    g.Data2 = int(parts[1], 16)
    g.Data3 = int(parts[2], 16)
    g.Data4 = (ctypes.c_ubyte * 8).from_buffer_copy(bytes.fromhex(parts[3] + parts[4]))
    return g


# GUID 类定义好后, 再声明 CoCreateInstance 的参数类型
ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p,
                                   wintypes.DWORD, ctypes.POINTER(GUID),
                                   ctypes.POINTER(ctypes.c_void_p)]
ole32.CoCreateInstance.restype = ctypes.c_long


def com_call(obj, index, restype, argtypes, *args):
    """
    调用 COM 对象虚方法表中的第 index 个方法(从 0 开始计数, this 指针自动作为首参)。
    obj 为 c_void_p(接口指针地址), index 含继承自 IUnknown 的前 3 个方法。
    """
    if not obj or not obj.value:
        raise cypes.WinError(-1)
    vtable_ptr = ctypes.cast(obj.value, ctypes.POINTER(ctypes.c_void_p))[0]
    func_addr = ctypes.cast(vtable_ptr, ctypes.POINTER(ctypes.c_void_p))[index]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(func_addr)(obj.value, *args)


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", wintypes.WORD),
                ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 3)]


def fmt_db(db):
    """把 dB 数值格式化为可读字符串"""
    if db is None:
        return "N/A"
    if db == float("-inf"):
        return "-inf"
    return "%.1f" % db


def fmt_json(obj, limit=200):
    """把对象格式化为简短 JSON 字符串(用于日志)"""
    try:
        text = json.dumps(obj, ensure_ascii=False)
    except Exception:
        text = str(obj)
    return text if len(text) <= limit else text[:limit] + "..."


# ============================================================================
#  模块一: 桌面音频采集(WASAPI 环回, 即系统输出混音, 不含麦克风)
# ============================================================================
try:
    from math import sumprod as _sumprod

    def _sum_of_squares(samples):
        return _sumprod(samples, samples)
except ImportError:  # Python 3.11 及以下没有 math.sumprod, 用普通循环兜底
    def _sum_of_squares(samples):
        total = 0.0
        for v in samples:
            total += v * v
        return total


class LoopbackAudioMonitor(threading.Thread):
    """
    通过 WASAPI 环回(loopback)模式采集"默认播放设备"的混音数据。
    这就是用户实际听到的"桌面音频"(会议声音、视频声音等), 不包含麦克风。

    工作方式: 独立线程持续采集, 每读取一个音频块就计算 RMS 电平(dBFS),
    并统计"连续低于阈值"的时长(quiet_seconds)。
    """

    def __init__(self, threshold_db, logger, poll_interval=0.05):
        super().__init__(daemon=True, name="audio-monitor")
        self.threshold_db = threshold_db
        self.poll_interval = poll_interval
        self._logger = logger
        self._stop_evt = threading.Event()
        self.lock = threading.Lock()
        self.current_db = None       # 最近一个音频块的电平(dBFS), None 表示暂无数据
        self.quiet_seconds = 0.0     # 连续安静时长(秒)
        self.last_update = 0.0       # 最近一次成功采集的时间(单调时钟)
        self.status = "初始化中"      # 初始化中/运行中/无可用设备/采集错误/已停止
        self.error_msg = ""
        self.device_info = ""        # 音频格式描述(采样率/位深/声道)
        # 以下为内部 COM 对象
        self._enumerator = None
        self._device = None
        self._client = None
        self._capture = None
        self._block_align = 4
        self._sample_kind = "float"  # float 或 pcm
        self._full_scale = 32768.0
        self._rate = 48000

    # ------------------------------------------------------------ 对外接口
    def stop(self):
        self._stop_evt.set()

    def snapshot(self):
        """线程安全地读取当前音频状态"""
        with self.lock:
            return {
                "db": self.current_db,
                "quiet": self.quiet_seconds,
                "age": time.monotonic() - self.last_update if self.last_update else None,
                "status": self.status,
                "error": self.error_msg,
                "fmt": self.device_info,
            }

    def reset_quiet(self):
        with self.lock:
            self.quiet_seconds = 0.0

    # ------------------------------------------------------------ 线程主体
    def run(self):
        # WASAPI 的 COM 调用需要在当前线程做 MTA 初始化
        try:
            ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        except Exception:
            pass
        try:
            self._capture_loop()
        except Exception as exc:  # 意外错误: 记录后退出线程(安静条件将不可用)
            with self.lock:
                self.status = "采集错误"
                self.error_msg = str(exc)
            self._log("错误", "桌面音频采集线程异常退出: %s" % exc)
        finally:
            self._release_device()
            try:
                ole32.CoUninitialize()
            except Exception:
                pass

    def _log(self, level, msg):
        self._logger.log(getattr(logging, level, logging.INFO), msg)

    def _capture_loop(self):
        # 初始化默认播放设备的环回采集(失败则放弃音频条件)
        try:
            self._init_device()
        except Exception as exc:
            with self.lock:
                self.status = "无可用设备"
                self.error_msg = str(exc)
            self._log("WARNING", "无法初始化桌面音频采集: %s(音频安静停止条件将不会触发)" % exc)
            return
        with self.lock:
            self.status = "运行中"
        self._log("INFO", "桌面音频采集已启动: %s(仅采集系统输出混音, 不含麦克风)" % self.device_info)

        while not self._stop_evt.is_set():
            try:
                self._read_once()
            except Exception as exc:
                # 设备被拔出/音频服务重启等情况: 尝试重新初始化
                self._log("WARNING", "音频采集中断(%s), 尝试重新初始化..." % exc)
                self._release_device()
                time.sleep(1.0)
                try:
                    self._init_device()
                    self._log("INFO", "桌面音频采集已恢复: %s" % self.device_info)
                except Exception as exc2:
                    with self.lock:
                        self.status = "无可用设备"
                        self.error_msg = str(exc2)
                    self._log("WARNING", "音频采集恢复失败: %s(安静条件暂时不可用)" % exc2)
                    return
            self._stop_evt.wait(self.poll_interval)

    # ------------------------------------------------------------ WASAPI 细节
    def _init_device(self):
        """打开默认渲染端点的环回采集流"""
        # 1) 创建设备枚举器 COM 对象
        enum = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(make_guid(CLSID_MMDeviceEnumerator)), None,
                                    CLSCTX_ALL, ctypes.byref(make_guid(IID_IMMDeviceEnumerator)),
                                    ctypes.byref(enum))
        if hr != 0:
            raise OSError("CoCreateInstance 失败, HRESULT=0x%08X" % (hr & 0xFFFFFFFF))

        # 2) 取默认渲染端点(默认播放设备)
        device = ctypes.c_void_p()
        hr = com_call(enum, IMMDEVICEENUMERATOR_GETDEFAULTAUDIOENDPOINT, ctypes.c_long,
                      [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)],
                      0, 0, ctypes.byref(device))  # eRender=0, eConsole=0
        if hr != 0:
            raise OSError("GetDefaultAudioEndpoint 失败, HRESULT=0x%08X(可能没有播放设备)" % (hr & 0xFFFFFFFF))

        # 3) 激活 IAudioClient 接口
        client = ctypes.c_void_p()
        hr = com_call(device, IMMDEVICE_ACTIVATE, ctypes.c_long,
                      [ctypes.POINTER(GUID), ctypes.c_uint, ctypes.c_void_p,
                       ctypes.POINTER(ctypes.c_void_p)],
                      ctypes.byref(make_guid(IID_IAudioClient)), CLSCTX_ALL, None,
                      ctypes.byref(client))
        if hr != 0:
            raise OSError("IMMDevice::Activate 失败, HRESULT=0x%08X" % (hr & 0xFFFFFFFF))

        # 4) 获取混音格式(环回采集必须使用设备混音格式)
        pwfx = ctypes.POINTER(WAVEFORMATEX)()
        hr = com_call(client, IAUDIOCLIENT_GETMIXFORMAT, ctypes.c_long,
                      [ctypes.POINTER(ctypes.POINTER(WAVEFORMATEX))], ctypes.byref(pwfx))
        if hr != 0 or not pwfx:
            raise OSError("IAudioClient::GetMixFormat 失败, HRESULT=0x%08X" % (hr & 0xFFFFFFFF))
        fmt = pwfx.contents
        # 注意: 必须先把需要的字段全部拷贝出来, 因为下方 Initialize 之后会
        # CoTaskMemFree 释放 pwfx, 之后再读 fmt 就是"释放后使用"(会读到垃圾值)
        channels = fmt.nChannels
        rate = fmt.nSamplesPerSec
        bits = fmt.wBitsPerSample
        block_align = fmt.nBlockAlign
        tag = fmt.wFormatTag
        kind = "pcm"
        if tag == 0xFFFE:  # WAVEFORMATEXTENSIBLE: 再看子格式 GUID 首字段
            sub = struct.unpack_from("<I", ctypes.string_at(ctypes.addressof(fmt) + 24, 4))[0]
            kind = "float" if sub == 3 else "pcm"
        elif tag == 3:     # WAVE_FORMAT_IEEE_FLOAT
            kind = "float"
        if kind == "float" and bits == 32:
            self._sample_kind = "float"
        elif kind == "pcm" and bits == 16:
            self._sample_kind = "pcm16"
        elif kind == "pcm" and bits == 32:
            self._sample_kind = "pcm32"
        else:
            ole32.CoTaskMemFree(ctypes.cast(pwfx, ctypes.c_void_p))
            raise OSError("不支持的音频格式: tag=%d bits=%d(通常应为 32 位浮点)" % (tag, bits))

        # 5) 以共享模式 + 环回标志初始化采集流
        hr = com_call(client, IAUDIOCLIENT_INITIALIZE, ctypes.c_long,
                      [ctypes.c_uint, ctypes.c_uint, ctypes.c_longlong, ctypes.c_longlong,
                       ctypes.POINTER(WAVEFORMATEX), ctypes.c_void_p],
                      AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK,
                      0, 0, pwfx, None)
        # 混音格式内存已拷贝完, 可以释放
        ole32.CoTaskMemFree(ctypes.cast(pwfx, ctypes.c_void_p))
        if hr != 0:
            raise OSError("IAudioClient::Initialize 失败, HRESULT=0x%08X" % (hr & 0xFFFFFFFF))

        # 6) 取得 IAudioCaptureClient 并开始采集
        capture = ctypes.c_void_p()
        hr = com_call(client, IAUDIOCLIENT_GETSERVICE, ctypes.c_long,
                      [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                      ctypes.byref(make_guid(IID_IAudioCaptureClient)), ctypes.byref(capture))
        if hr != 0:
            raise OSError("IAudioClient::GetService 失败, HRESULT=0x%08X" % (hr & 0xFFFFFFFF))
        hr = com_call(client, IAUDIOCLIENT_START, ctypes.c_long, [])  # Start
        if hr != 0:
            raise OSError("IAudioClient::Start 失败, HRESULT=0x%08X" % (hr & 0xFFFFFFFF))

        self._enumerator = enum
        self._device = device
        self._client = client
        self._capture = capture
        self._block_align = block_align
        self._rate = rate
        self._full_scale = 32768.0 if bits == 16 else 2147483648.0
        self.device_info = "%d Hz / %d bit %s / %d 声道" % (
            rate, bits, "浮点" if kind == "float" else "PCM", channels)

    def _read_once(self):
        """读取一批音频数据并更新电平与安静计时"""
        padding = ctypes.c_uint32(0)
        # GetCurrentPadding: 缓冲区中待读取的帧数
        hr = com_call(self._client, IAUDIOCLIENT_GETCURRENTPADDING, ctypes.c_long,
                      [ctypes.POINTER(ctypes.c_uint32)], ctypes.byref(padding))
        if hr != 0:
            raise OSError("GetCurrentPadding 失败, HRESULT=0x%08X" % (hr & 0xFFFFFFFF))
        if padding.value == 0:
            return

        data_ptr = ctypes.c_void_p()
        frames = ctypes.c_uint32(0)
        flags = ctypes.c_uint32(0)
        dev_pos = ctypes.c_uint64(0)
        qpc_pos = ctypes.c_uint64(0)
        hr = com_call(self._capture, IAUDIOCAPTURECLIENT_GETBUFFER, ctypes.c_long,
                      [ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32),
                       ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint64),
                       ctypes.POINTER(ctypes.c_uint64)],
                      ctypes.byref(data_ptr), ctypes.byref(frames), ctypes.byref(flags),
                      ctypes.byref(dev_pos), ctypes.byref(qpc_pos))
        if hr != 0:
            raise OSError("GetBuffer 失败, HRESULT=0x%08X" % (hr & 0xFFFFFFFF))
        n_frames = frames.value
        try:
            if n_frames == 0 or (flags.value & AUDCLNT_BUFFERFLAGS_SILENT):
                db = float("-inf")  # 静音块
            else:
                nbytes = n_frames * self._block_align
                raw = ctypes.string_at(data_ptr.value, nbytes)
                db = self._calc_db(raw)
        finally:
            com_call(self._capture, IAUDIOCAPTURECLIENT_RELEASEBUFFER, ctypes.c_long,
                     [ctypes.c_uint32], n_frames)  # ReleaseBuffer

        duration = n_frames / float(self._rate)
        with self.lock:
            self.current_db = db
            self.last_update = time.monotonic()
            if db < self.threshold_db:
                self.quiet_seconds += duration
            else:
                self.quiet_seconds = 0.0

    def _calc_db(self, raw):
        """计算一个音频块的 RMS 电平, 返回 dBFS"""
        if self._sample_kind == "float":
            samples = array("f")
            samples.frombytes(raw)
            mean_sq = _sum_of_squares(samples) / len(samples) if samples else 0.0
            rms = math.sqrt(mean_sq)
        elif self._sample_kind == "pcm16":
            samples = array("h")
            samples.frombytes(raw)
            mean_sq = _sum_of_squares(samples) / len(samples) if samples else 0.0
            rms = math.sqrt(mean_sq) / self._full_scale
        else:  # pcm32
            samples = array("i")
            samples.frombytes(raw)
            mean_sq = _sum_of_squares(samples) / len(samples) if samples else 0.0
            rms = math.sqrt(mean_sq) / self._full_scale
        if rms <= 0.0:
            return float("-inf")
        return 20.0 * math.log10(rms)

    def _release_device(self):
        """停止采集并释放 COM 对象(尽力而为, 失败不影响主流程)"""
        try:
            if self._client and self._client.value:
                com_call(self._client, IAUDIOCLIENT_STOP, ctypes.c_long, [])
        except Exception:
            pass
        for obj in (self._capture, self._client, self._device, self._enumerator):
            try:
                if obj and obj.value:
                    com_call(obj, IUNKNOWN_RELEASE, ctypes.c_ulong, [])
            except Exception:
                pass
        self._capture = None
        self._client = None
        self._device = None
        self._enumerator = None


# ============================================================================
#  模块二: 画面静止检测(截屏 + 差异比较)
# ============================================================================
class ScreenMonitor(object):
    """
    定时截取整个虚拟屏幕(所有显示器), 与上一帧比较:
    平均绝对差小于阈值视为"画面没有变化", 累计静止时长。
    """

    def __init__(self, interval, diff_threshold, logger):
        self.interval = interval
        self.diff_threshold = diff_threshold
        self._logger = logger
        self._prev_raw = None
        self.static_seconds = 0.0
        self._next_check = 0.0
        self._last_ok_check = None
        self._last_static_logged = 0.0
        self.frame_size = (0, 0)
        self.capture_ms = 0.0
        self.fail_count = 0
        self._fail_logged = False

    def poll(self, now):
        """到点则截屏比较, 更新 static_seconds"""
        if now < self._next_check:
            return
        self._next_check = now + self.interval
        t0 = time.monotonic()
        raw = self._capture()
        self.capture_ms = (time.monotonic() - t0) * 1000.0
        if raw is None:
            # 截屏失败(如锁屏/安全桌面): 保守处理, 重置静止计时, 避免误判
            self.fail_count += 1
            if not self._fail_logged:
                self._fail_logged = True
                self._logger.warning("屏幕采集失败(可能已锁屏?), 本次不计入静止判定")
            self.static_seconds = 0.0
            self._prev_raw = None
            return
        if self._fail_logged:
            self._fail_logged = False
            self._logger.info("屏幕采集恢复正常")
        self.fail_count = 0
        if self._prev_raw is not None and len(raw) == len(self._prev_raw):
            if self._is_similar(raw, self._prev_raw):
                if self._last_ok_check is not None:
                    self.static_seconds += now - self._last_ok_check
                if self.static_seconds - self._last_static_logged >= 300:
                    self._last_static_logged = int(self.static_seconds / 300) * 300
                    self._logger.info("画面已持续静止 %.0f 秒" % self.static_seconds)
            else:
                if self.static_seconds >= 60:
                    self._logger.info("画面恢复动态(此前连续静止 %.0f 秒)" % self.static_seconds)
                self.static_seconds = 0.0
                self._last_static_logged = 0.0
        self._prev_raw = raw
        self._last_ok_check = now

    def reset(self):
        self.static_seconds = 0.0
        self._last_static_logged = 0.0

    # ------------------------------------------------------------ 内部实现
    def _capture(self):
        """截取虚拟屏幕, 返回 32 位 BGRA 原始字节; 失败返回 None"""
        hdc = user32.GetDC(None)
        if not hdc:
            return None
        try:
            x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
            y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
            w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
            h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
            if w <= 0 or h <= 0:
                return None
            hdc_mem = gdi32.CreateCompatibleDC(hdc)
            if not hdc_mem:
                return None
            try:
                hbmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
                if not hbmp:
                    return None
                try:
                    gdi32.SelectObject(hdc_mem, hbmp)
                    if not gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc, x, y, SRCCOPY):
                        return None
                    bmi = BITMAPINFO()
                    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                    bmi.bmiHeader.biWidth = w
                    bmi.bmiHeader.biHeight = -h  # 负数 = 自上而下
                    bmi.bmiHeader.biPlanes = 1
                    bmi.bmiHeader.biBitCount = 32
                    bmi.bmiHeader.biCompression = BI_RGB
                    buf = ctypes.create_string_buffer(w * h * 4)
                    lines = gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi),
                                            DIB_RGB_COLORS)
                    if lines != h:
                        return None
                    self.frame_size = (w, h)
                    return buf.raw
                finally:
                    gdi32.DeleteObject(hbmp)
            finally:
                gdi32.DeleteDC(hdc_mem)
        finally:
            user32.ReleaseDC(None, hdc)

    def _is_similar(self, cur, prev):
        """抽采样后计算平均绝对差, 小于阈值视为两帧相同"""
        n = len(cur)
        # 目标采样点约 15 万个, 步长取 4 的倍数(一个 BGRA 像素占 4 字节)
        stride = max(16, ((n // 150000) + 3) // 4 * 4)
        a = prev[::stride]
        b = cur[::stride]
        if len(a) != len(b):
            return False
        total = 0
        for x, y in zip(a, b):
            total += x - y if x > y else y - x
        return (total / float(len(a))) <= self.diff_threshold


# ============================================================================
#  模块三: 腾讯会议 / 企业微信会议 检测
# ============================================================================
_PROC_NAME_CACHE = {}  # PID -> (exe 名, 查询时间), 避免频繁 OpenProcess


def get_process_name(pid):
    """根据 PID 获取进程 exe 文件名(小写), 失败返回 None(带 5 秒缓存)"""
    now = time.monotonic()
    cached = _PROC_NAME_CACHE.get(pid)
    if cached is not None and now - cached[1] < 5.0:
        return cached[0]
    name = None
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(handle)
    _PROC_NAME_CACHE[pid] = (name, now)
    return name


class WinInfo(object):
    """一个顶层窗口的信息"""
    __slots__ = ("hwnd", "pid", "exe", "title", "cls", "left", "top", "width", "height",
                 "visible", "minimized")

    def __init__(self, hwnd, pid, exe, title, cls, left, top, width, height, visible, minimized):
        self.hwnd = hwnd
        self.pid = pid
        self.exe = exe
        self.title = title
        self.cls = cls
        self.left = left
        self.top = top
        self.width = width
        self.height = height
        self.visible = visible
        self.minimized = minimized

    @property
    def area(self):
        return self.width * self.height

    def describe(self):
        state = "可见" if self.visible else "隐藏"
        if self.minimized:
            state += "/最小化"
        return "%s | %s | %dx%d | %s" % (self.exe, self.title or "(无标题)",
                                         self.width, self.height, state)


def enum_top_windows():
    """枚举所有顶层窗口(不解析进程名, 由调用方按需处理)"""
    result = []

    def _cb(hwnd, lparam):
        try:
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            title_buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title_buf, 512)
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls_buf, 256)
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            result.append(WinInfo(hwnd, pid.value, "", title_buf.value, cls_buf.value,
                                  rect.left, rect.top,
                                  rect.right - rect.left, rect.bottom - rect.top,
                                  bool(user32.IsWindowVisible(hwnd)),
                                  bool(user32.IsIconic(hwnd))))
        except Exception:
            pass
        return True

    callback = WNDENUMPROC(_cb)
    user32.EnumWindows(callback, 0)
    return result


class MeetingDetector(object):
    """
    检测"腾讯会议是否正在进行"。

    覆盖两种形态:
      - 独立版腾讯会议: 进程 wemeetapp.exe(Qt 原生窗口);
      - 企业微信内置融合版会议: 由 WXWork.exe / WXWorkWeb.exe 承载,
        会议窗口类名包含 Meeting / Qt 等关键词(已从企业微信二进制中核实)。

    判定规则(全部满足才算"会议窗口"):
      1. 属于监视列表中的进程;
      2. 窗口可见或最小化, 且尺寸不小于配置的最小值;
      3. 标题非空, 且不等于主界面标题(home_titles);
      4. 类名不包含任何排除关键词(class_exclude_keywords, 辅助/隐藏窗口);
      5. 命中至少一个正向信号:
         - 类名包含 meeting_class_keywords 中的关键词(强信号), 或
         - 标题包含 meeting_title_keywords 中的关键词(弱信号, 需额外通过
           title_exclude_keywords 过滤)。
    """

    def __init__(self, cfg, logger):
        m = cfg["meeting"]
        self.process_names = set(n.strip().lower() for n in m["process_names"] if n.strip())
        self.min_width = m["min_width"]
        self.min_height = m["min_height"]
        self.class_keywords = [k for k in m["meeting_class_keywords"] if k]
        self.title_keywords = [k for k in m["meeting_title_keywords"] if k]
        self.class_excludes = [k for k in m["class_exclude_keywords"] if k]
        self.home_titles = set(t for t in m["home_titles"] if t)
        self.title_excludes = [k for k in m["title_exclude_keywords"] if k]
        self._logger = logger
        self.last_windows = []       # 最近一次枚举到的目标进程窗口(调试用)
        self.last_meeting_windows = []

    # ------------------------------------------------------------ 对外接口
    def poll(self):
        """返回 (是否在会议中, 详情说明)"""
        windows = self._enum_target_windows()
        self.last_windows = windows
        meeting_wins = [w for w in windows if self.is_meeting_window(w)]
        self.last_meeting_windows = meeting_wins
        if meeting_wins:
            detail = "; ".join(w.describe() for w in meeting_wins[:3])
            return True, "检测到会议窗口 %d 个: %s" % (len(meeting_wins), detail)
        if windows:
            titles = "; ".join("%s[%s]" % (w.title or "(无标题)", w.cls) for w in windows[:5])
            return False, "腾讯会议/企业微信在运行, 但未检测到会议窗口(窗口: %s)" % titles
        return False, "未检测到腾讯会议/企业微信窗口"

    def is_meeting_window(self, w):
        """判断单个窗口是否为"会议窗口" """
        if not w.title:
            return False
        if not (w.visible or w.minimized):
            return False
        if w.width < self.min_width or w.height < self.min_height:
            return False
        if w.title in self.home_titles:
            return False
        cls_lower = w.cls.lower()
        if any(k.lower() in cls_lower for k in self.class_excludes):
            return False
        # 正向信号判定
        class_hit = any(k.lower() in cls_lower for k in self.class_keywords)
        title_hit = bool(self.title_keywords) and any(k in w.title for k in self.title_keywords)
        if not (class_hit or title_hit):
            return False
        if title_hit and not class_hit:
            # 仅靠标题关键词命中的窗口, 再过滤明显的非会议页面
            if any(k in w.title for k in self.title_excludes):
                return False
        return True

    # ------------------------------------------------------------ 内部实现
    def _enum_target_windows(self):
        """枚举属于目标进程的所有顶层窗口(进程名带缓存, 一次枚举完成)"""
        windows = []
        for w in enum_top_windows():
            exe = get_process_name(w.pid)
            if exe and exe in self.process_names:
                w.exe = exe
                windows.append(w)
        return windows

    def debug_dump(self):
        """返回目标进程所有窗口的分类明细(用于自检/调试)"""
        lines = []
        for w in self.last_windows:
            mark = "[会议窗口]" if self.is_meeting_window(w) else "[非会议]"
            lines.append("  %s %s" % (mark, w.describe()))
        return lines


# ============================================================================
#  模块四: OBS WebSocket 客户端(纯标准库实现 obs-websocket v5 协议)
# ============================================================================
class OBSClientError(Exception):
    """OBS 连接/通信错误"""


class OBSRequestError(Exception):
    """OBS 请求被拒绝(带错误码)"""

    def __init__(self, request_type, code, comment):
        super().__init__("%s 失败: code=%s %s" % (request_type, code, comment))
        self.request_type = request_type
        self.code = code
        self.comment = comment


class OBSClient(object):
    """
    极简 obs-websocket v5 客户端:
      - 标准库 socket 手写 WebSocket 握手与帧解析(客户端帧必须掩码);
      - 支持 SHA256 挑战认证;
      - 后台读取线程负责分发 RequestResponse / Event;
      - 提供 GetRecordState / StartRecord / StopRecord 等调用。
    """

    def __init__(self, host, port, password, logger):
        self.host = host
        self.port = port
        self.password = password or ""
        self._logger = logger
        self._sock = None
        self._buf = b""
        self._lock = threading.RLock()
        self._pending = {}          # requestId -> [Event, box]
        self._reader = None
        self._closed = True
        self._intentional_close = False
        self.connected = False
        self.ws_version = ""
        self._record_state_request = None  # 探测到的"查询录制状态"请求名(版本适配)

    # ------------------------------------------------------------ 连接管理
    def connect(self, timeout=8.0):
        """建立连接并完成 Hello/Identify 握手; 失败抛出 OBSClientError"""
        self.close()
        self._closed = False
        self._buf = b""
        try:
            sock = socket.create_connection((self.host, self.port), timeout=timeout)
        except OSError as exc:
            self._closed = True
            raise OBSClientError("无法连接 OBS WebSocket %s:%s (%s)" % (self.host, self.port, exc))
        try:
            sock.settimeout(timeout)
            # --- WebSocket 握手 ---
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = ("GET / HTTP/1.1\r\n"
                       "Host: %s:%d\r\n"
                       "Upgrade: websocket\r\n"
                       "Connection: Upgrade\r\n"
                       "Sec-WebSocket-Key: %s\r\n"
                       "Sec-WebSocket-Version: 13\r\n\r\n" % (self.host, self.port, key))
            sock.sendall(request.encode("ascii"))
            headers = b""
            while b"\r\n\r\n" not in headers:
                chunk = sock.recv(4096)
                if not chunk:
                    raise OBSClientError("OBS 在 WebSocket 握手阶段关闭了连接")
                headers += chunk
            head, _, rest = headers.partition(b"\r\n\r\n")
            status_line = head.split(b"\r\n")[0]
            if b" 101 " not in status_line:
                raise OBSClientError("WebSocket 握手失败: %s" % status_line.decode("ascii", "replace"))
            expect = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii"))
                .digest()).decode("ascii")
            if ("sec-websocket-accept: " + expect).lower() not in head.decode("ascii", "replace").lower():
                raise OBSClientError("WebSocket 握手校验失败(Sec-WebSocket-Accept 不匹配)")
            self._sock = sock
            self._buf = rest

            # --- 读取 Hello (op 0) ---
            hello = self._read_message(timeout)
            if not hello or hello.get("op") != 0:
                raise OBSClientError("未收到 OBS 的 Hello 消息")
            d = hello.get("d", {})
            self.ws_version = d.get("obsWebSocketVersion", "")
            auth = d.get("authentication")
            identify = {"rpcVersion": 1, "eventSubscriptions": 1023}
            if auth:
                if not self.password:
                    raise OBSClientError("OBS WebSocket 启用了身份验证, 请在 config.ini 的 "
                                         "[obs] password 中填写密码")
                identify["authentication"] = self._auth_string(auth.get("challenge", ""),
                                                               auth.get("salt", ""))

            # --- 发送 Identify (op 1), 等待 Identified (op 2) ---
            self._send_message({"op": 1, "d": identify})
            identified = self._read_message(timeout)
            if not identified or identified.get("op") != 2:
                raise OBSClientError("OBS 认证失败(请检查密码是否正确)")

            # --- 启动后台读取线程 ---
            self.connected = True
            self._reader = threading.Thread(target=self._reader_loop, daemon=True,
                                            name="obs-ws-reader")
            self._reader.start()
            # 探测本服务器支持的"查询录制状态"请求名:
            # obs-websocket 5.0~5.4 叫 GetRecordState, 5.5+ 改回了 GetRecordStatus
            self._detect_record_state_request()
            return True
        except Exception:
            self.close()
            raise

    def close(self):
        """关闭连接并唤醒所有等待中的请求"""
        self._intentional_close = True
        self._closed = True
        self.connected = False
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        with self._lock:
            for item in self._pending.values():
                item[0].set()
            self._pending.clear()

    # ------------------------------------------------------------ 请求调用
    def call(self, request_type, request_data=None, timeout=8.0):
        """发送一次请求并等待响应, 成功返回 responseData; 失败抛异常"""
        if not self.connected or self._sock is None:
            raise OBSClientError("尚未连接到 OBS")
        request_id = uuid.uuid4().hex
        event = threading.Event()
        box = {"response": None}
        with self._lock:
            self._pending[request_id] = [event, box]
        try:
            self._send_message({"op": 6, "d": {"requestType": request_type,
                                               "requestId": request_id,
                                               "requestData": request_data or {}}})
            if not event.wait(timeout):
                raise OBSClientError("请求 %s 响应超时" % request_type)
            response = box["response"]
            if response is None:
                raise OBSClientError("连接已断开(请求 %s 未完成)" % request_type)
            d = response.get("d", {})
            status = d.get("requestStatus", {})
            if not status.get("result", False):
                raise OBSRequestError(request_type, status.get("code", 0),
                                      status.get("comment", ""))
            return d.get("responseData", {})
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def get_record_state(self, timeout=6.0):
        """查询 OBS 是否正在录屏, 返回 (是否在录屏, 原始数据)"""
        # 请求名优先级: 连接时探测到的名字 > GetRecordState(5.0~5.4) > GetRecordStatus(5.5+)
        candidates = []
        if self._record_state_request:
            candidates.append(self._record_state_request)
        for name in ("GetRecordState", "GetRecordStatus"):
            if name not in candidates:
                candidates.append(name)
        last_error = None
        data = None
        for name in candidates:
            try:
                data = self.call(name, {}, timeout)
                break
            except OBSRequestError as exc:
                last_error = exc
                if exc.code != 204:  # 204=请求类型不存在, 才继续尝试别的名字
                    raise
        if data is None:
            raise last_error or OBSClientError("无法查询 OBS 录制状态")
        # 响应字段兼容: v5(outputActive/outputState)与 v4(isRecording)
        if "outputActive" in data:
            return bool(data["outputActive"]), data
        if "isRecording" in data:
            return bool(data["isRecording"]), data
        state = data.get("outputState", "")
        active = state in ("OBS_WEBSOCKET_OUTPUT_STARTED", "OBS_WEBSOCKET_OUTPUT_STARTING",
                           "OBS_WEBSOCKET_OUTPUT_PAUSED", "OBS_WEBSOCKET_OUTPUT_RESUMED")
        return active, data

    def _detect_record_state_request(self):
        """通过 GetVersion 的 availableRequests 探测录制状态请求名(失败不致命)"""
        try:
            version_data = self.call("GetVersion", {}, timeout=6)
            available = set(version_data.get("availableRequests") or [])
            if "GetRecordState" in available:
                self._record_state_request = "GetRecordState"
            elif "GetRecordStatus" in available:
                self._record_state_request = "GetRecordStatus"
            if self._record_state_request and self._record_state_request != "GetRecordState":
                self._logger.info("版本适配: 本机 OBS 使用 %s 查询录制状态",
                                  self._record_state_request)
        except Exception:
            pass  # 探测失败时 get_record_state 会按顺序回退尝试

    def start_record(self, timeout=10.0):
        return self.call("StartRecord", {}, timeout)

    def stop_record(self, timeout=10.0):
        return self.call("StopRecord", {}, timeout)

    # ------------------------------------------------------------ WebSocket 底层
    def _auth_string(self, challenge, salt):
        """
        obs-websocket v5 认证算法(严格按协议规范):
            secret = base64(sha256(password + salt))      # 第一步得到 base64 字符串
            auth   = base64(sha256(secret + challenge))   # 第二步拼 challenge 再哈希
        """
        digest = hashlib.sha256(self.password.encode("utf-8") + salt.encode("utf-8")).digest()
        secret = base64.b64encode(digest).decode("ascii")
        auth = hashlib.sha256(secret.encode("utf-8") + challenge.encode("utf-8")).digest()
        return base64.b64encode(auth).decode("ascii")

    def _send_frame(self, opcode, payload):
        """发送一个 WebSocket 帧(客户端发给服务器的帧必须掩码)"""
        sock = self._sock
        if sock is None:
            raise OBSClientError("连接已关闭")
        header = bytearray([0x80 | opcode])  # FIN=1
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(payload[i] ^ mask[i % 4] for i in range(n))
        sock.sendall(bytes(header) + masked)

    def _send_message(self, obj):
        self._send_frame(1, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _read_exact(self, n):
        """从缓冲区+套接字精确读取 n 字节; 连接关闭返回 False"""
        if self._sock is None:
            return False
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                return False
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _read_frame(self, timeout=None):
        """读取一个 WebSocket 帧, 返回 (opcode, payload, fin); 连接关闭返回 None"""
        if timeout is not None:
            self._sock.settimeout(timeout)
        hdr = self._read_exact(2)
        if hdr is False:
            return None
        b1, b2 = hdr[0], hdr[1]
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            ext = self._read_exact(2)
            if ext is False:
                return None
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = self._read_exact(8)
            if ext is False:
                return None
            length = struct.unpack(">Q", ext)[0]
        if b2 & 0x80:  # 服务器帧不应带掩码, 防御性处理
            mask = self._read_exact(4)
            if mask is False:
                return None
        payload = self._read_exact(length) if length else b""
        if payload is False:
            return None
        return opcode, payload, fin

    @staticmethod
    def _try_parse(chunks):
        """把累积的帧数据解析为 JSON 对象; 失败返回 None"""
        data = b"".join(chunks)
        if not data:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return None

    def _read_message(self, timeout=None):
        """
        读取一条完整的应用层消息, 返回 dict; 连接关闭返回 None。
        自动处理 Ping/Pong 与分片(obs-websocket 实际不会分片, 这里做兼容)。
        """
        chunks = []
        while True:
            frame = self._read_frame(timeout)
            if frame is None:
                return None
            opcode, payload, fin = frame
            if opcode == 8:      # 关闭帧
                return None
            if opcode == 9:      # Ping -> Pong
                try:
                    self._send_frame(10, payload)
                except Exception:
                    return None
                continue
            if opcode == 10:     # Pong, 忽略
                continue
            if opcode == 0:      # 延续帧
                chunks.append(payload)
                if fin:
                    msg = self._try_parse(chunks)
                    if msg is not None:
                        return msg
                    chunks = []
                continue
            if opcode in (1, 2):  # 文本帧 / 二进制帧
                if fin:
                    msg = self._try_parse([payload])
                    if msg is not None:
                        return msg
                else:
                    chunks = [payload]
                continue
            # 其他 opcode: 忽略

    def _reader_loop(self):
        """后台线程: 持续读取消息并分发"""
        try:
            while not self._closed and self._sock is not None:
                try:
                    msg = self._read_message(30)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if msg is None:
                    break
                op = msg.get("op")
                d = msg.get("d", {})
                if op == 7:  # RequestResponse
                    request_id = d.get("requestId")
                    with self._lock:
                        item = self._pending.get(request_id)
                    if item:
                        item[1]["response"] = msg
                        item[0].set()
                elif op == 5:  # Event
                    self._handle_event(d)
        except Exception:
            pass  # 读取线程不应对主程序造成影响
        finally:
            self.connected = False
            with self._lock:
                for item in self._pending.values():
                    item[0].set()
                self._pending.clear()
            if not self._intentional_close:
                self._logger.warning("OBS WebSocket 连接已断开(将在主循环中自动重连)")

    def _handle_event(self, d):
        event_type = d.get("eventType", "")
        if event_type == "RecordStateChanged":
            state = d.get("outputState", "")
            active = d.get("outputActive")
            self._logger.info("OBS 事件: 录制状态变化 -> %s(active=%s)" % (state, active))
        else:
            self._logger.debug("OBS 事件: %s %s" % (event_type, fmt_json(d, 120)))


# ============================================================================
#  模块五: 守护主逻辑
# ============================================================================
class MeetingGuard(object):
    """把各模块组合起来: 监视会议/画面/音频, 必要时停止 OBS 录屏"""

    def __init__(self, cfg, logger, dry_run=False, stop_event=None, on_status=None):
        self.cfg = cfg
        self.log = logger
        self.dry_run = dry_run
        # stop_event: 外部(GUI)用于请求停止守护的 threading.Event; on_status: 状态回调
        self.stop_event = stop_event or threading.Event()
        self.on_status = on_status
        g = cfg["guard"]
        self.audio = LoopbackAudioMonitor(g["quiet_threshold_db"], logger)
        self.screen = ScreenMonitor(g["static_check_interval_sec"],
                                    g["static_diff_threshold"], logger)
        self.meeting = MeetingDetector(cfg, logger)
        self.obs = make_obs_client(cfg, logger)
        # ---- 运行状态 ----
        self.recording = None             # None=未知, True=录屏中, False=未录屏
        self.meeting_ever_active = False  # 本次录屏期间是否检测到过会议
        self.meeting_missing_since = None
        self._next_obs_poll = 0.0
        self._next_obs_reconnect = 0.0
        self._obs_fail_count = 0
        self._next_auto_start = 0.0
        self._next_heartbeat = 0.0
        self._stop_retry_at = 0.0
        self._just_stopped = False
        self._audio_warned = False

    # ------------------------------------------------------------ 生命周期
    def run(self):
        """控制台模式运行( Ctrl+C 退出 )"""
        self._log_banner()
        self.audio.start()
        try:
            while not self.stop_event.is_set():
                self._loop_once()
        except KeyboardInterrupt:
            self.log.info("收到 Ctrl+C, 守护停止")
        finally:
            self._shutdown()

    def run_loop(self):
        """GUI 模式运行: 由调用方(子线程)驱动, 通过 stop_event 退出"""
        self._log_banner()
        self.audio.start()
        try:
            while not self.stop_event.is_set():
                self._loop_once()
        except Exception as exc:
            self.log.warning("守护线程异常退出: %s" % exc)
        finally:
            self._shutdown()

    def _shutdown(self):
        self.audio.stop()
        try:
            self.obs.close()
        except Exception:
            pass
        self.log.info("已退出(OBS 录屏不受影响, 不会因退出而被停止)")

    def _log_banner(self):
        g = self.cfg["guard"]
        self.log.info("=" * 60)
        self.log.info("%s v%s 启动%s" % (APP_NAME, VERSION, "(模拟运行)" if self.dry_run else ""))
        self.log.info("停止条件: ①会议结束(宽限 %ds) ②画面静止≥%ds ③桌面音频<%ddB 持续≥%ds" % (
            g["meeting_gone_grace_sec"], g["static_duration_sec"],
            g["quiet_threshold_db"], g["quiet_duration_sec"]))
        if g["static_require_quiet"]:
            self.log.info("注意: 条件②(画面静止)仅在桌面音频也低于 %d dB 时才触发; "
                          "会议仍在发声时画面静止不会停止录屏" % g["quiet_threshold_db"])
        self.log.info("OBS 目标: %s:%s | 自动开始录屏: %s" % (
            self.cfg["obs"]["host"], self.cfg["obs"]["port"],
            "开" if g["auto_start_recording"] else "关"))
        self.log.info("=" * 60)

    # ------------------------------------------------------------ 主循环
    def _loop_once(self):
        now = time.monotonic()
        g = self.cfg["guard"]

        # 1) OBS 连接(断开时每 5 秒重试一次)
        if not self.obs.connected and now >= self._next_obs_reconnect:
            self._next_obs_reconnect = now + 5.0
            try:
                self.obs.connect()
                self._obs_fail_count = 0
                self._next_obs_poll = 0.0  # 连上后立即查一次录制状态
                self.log.info("已连接 OBS WebSocket (obs-websocket %s)" % (self.obs.ws_version or "?"))
            except OBSClientError as exc:
                self._obs_fail_count += 1
                if self._obs_fail_count == 1 or self._obs_fail_count % 12 == 0:
                    self.log.warning("连接 OBS 失败: %s(继续重试)" % exc)

        # 2) 会议状态检测
        meeting_active = False
        try:
            meeting_active, detail = self.meeting.poll()
            if meeting_active:
                if not self.meeting_ever_active:
                    self.meeting_ever_active = True
                    self.log.info("%s", detail)
                self.meeting_missing_since = None
            elif self.meeting_missing_since is None:
                self.meeting_missing_since = now
                self.log.debug("当前未检测到会议: %s", detail)
        except Exception as exc:
            self.log.warning("会议检测异常: %s(本次跳过)" % exc)

        # 3) 音频/屏幕数据更新
        self.screen.poll(now)
        audio_snap = self.audio.snapshot()
        if not self._audio_warned and now > 3.0:
            self._audio_warned = True
            if audio_snap["status"] != "运行中":
                self.log.warning("桌面音频采集不可用(%s: %s), '音频安静停止'条件不会触发",
                                 audio_snap["status"], audio_snap["error"])

        # 4) OBS 录制状态(每 2 秒轮询一次)
        if self.obs.connected and now >= self._next_obs_poll:
            self._next_obs_poll = now + 2.0
            self._refresh_record_state()

        # 5) 录屏中: 评估停止条件
        if self.recording:
            self._evaluate(now, meeting_active)
        elif (g["auto_start_recording"] and self.obs.connected and meeting_active
              and self.recording is False and now >= self._next_auto_start):
            self._try_start_record(now)

        # 6) 心跳日志(每 5 分钟一条, 方便确认守护在正常工作)
        if now >= self._next_heartbeat:
            self._next_heartbeat = now + 300
            self._log_heartbeat(meeting_active, audio_snap)

        # 7) 向 GUI 上报状态快照(回调异常不影响守护)
        if self.on_status is not None:
            try:
                self.on_status(self._status_snapshot(meeting_active, audio_snap))
            except Exception:
                pass

        time.sleep(g["check_interval_sec"])

    def _status_snapshot(self, meeting_active, audio_snap):
        """构造一份当前状态快照(供 GUI 刷新界面用)"""
        g = self.cfg["guard"]
        now = time.monotonic()
        meeting_missing = 0.0
        if self.meeting_missing_since is not None:
            meeting_missing = now - self.meeting_missing_since
        audio_db = audio_snap["db"]
        # 静止条件是否正被"仍有音频"阻塞(供界面提示用户)
        static_blocked = bool(g["static_require_quiet"] and audio_db is not None
                              and audio_db >= g["quiet_threshold_db"])
        return {
            "recording": self.recording,              # True/False/None
            "meeting_active": meeting_active,
            "meeting_ever_active": self.meeting_ever_active,
            "meeting_missing_sec": meeting_missing,
            "obs_connected": self.obs.connected,
            "obs_version": self.obs.ws_version,
            "audio_status": audio_snap["status"],
            "audio_db": audio_db,
            "audio_quiet_sec": audio_snap["quiet"] or 0.0,
            "screen_static_sec": self.screen.static_seconds,
            "static_blocked_by_audio": static_blocked,
            "dry_run": self.dry_run,
            "thresholds": {
                "meeting_gone_grace_sec": g["meeting_gone_grace_sec"],
                "static_duration_sec": g["static_duration_sec"],
                "quiet_duration_sec": g["quiet_duration_sec"],
                "quiet_threshold_db": g["quiet_threshold_db"],
                "static_require_quiet": g["static_require_quiet"],
            },
        }

    # ------------------------------------------------------------ 状态处理
    def _refresh_record_state(self):
        try:
            active, _data = self.obs.get_record_state()
        except OBSClientError as exc:
            self.log.warning("查询 OBS 录制状态失败: %s(将重连)" % exc)
            self.obs.close()
            return
        except OBSRequestError as exc:
            # 请求被 OBS 拒绝(极端情况): 记录并保持上次已知状态, 绝不让守护崩溃
            self.log.warning("OBS 拒绝录制状态查询: %s(暂时保持上次状态)" % exc)
            return
        previous = self.recording
        self.recording = active
        if previous is None:
            if active:
                self.log.info("检测到 OBS 正在录屏, 开始守护")
                self._on_record_start()
        elif previous != active:
            if active:
                self.log.info("检测到 OBS 开始录屏, 开始守护")
                self._on_record_start()
            elif self._just_stopped:
                self._just_stopped = False
                self.log.info("OBS 已确认停止录屏")
            else:
                self.log.info("检测到 OBS 录屏被外部停止, 暂停守护(重新开始录屏后自动恢复)")
                self._on_record_stop()

    def _on_record_start(self):
        """录屏开始: 重置所有计时器"""
        self.meeting_ever_active = False
        self.meeting_missing_since = None
        self.screen.reset()
        self.audio.reset_quiet()
        self._just_stopped = False

    def _on_record_stop(self):
        """录屏结束: 重置计时器(保留 meeting_ever_active 无意义, 一并重置)"""
        self.screen.reset()
        self.audio.reset_quiet()
        self.meeting_missing_since = None
        self.meeting_ever_active = False

    # ------------------------------------------------------------ 条件评估
    def _evaluate(self, now, meeting_active):
        g = self.cfg["guard"]
        reasons = []
        # 条件①: 会议结束(必须本次录屏期间检测到过会议, 避免用户录别的内容被误停)
        if self.meeting_ever_active and self.meeting_missing_since is not None:
            gone = now - self.meeting_missing_since
            if gone >= g["meeting_gone_grace_sec"]:
                reasons.append("腾讯会议已结束(会议窗口消失 %.0f 秒)" % gone)
        # 条件②/③: 画面静止 / 桌面音频安静
        if (not g["static_quiet_require_meeting"]) or meeting_active:
            audio_snap = self.audio.snapshot()
            audio_db = audio_snap["db"]
            # 当前是否"有声": 电平不低于安静阈值。
            # 音频采集异常/暂无数据(db 为 None)时按"未知"处理, 不阻塞静止条件。
            audio_active = audio_db is not None and audio_db >= g["quiet_threshold_db"]
            # 条件②: 画面静止 —— 默认要求桌面音频同时处于安静状态;
            # 会议仍在发声(> -45 dB)时, 即使画面静止也不停止录屏(用户要求)
            static_allowed = (not g["static_require_quiet"]) or (not audio_active)
            if static_allowed and self.screen.static_seconds >= g["static_duration_sec"]:
                reasons.append("画面连续静止 %.0f 秒(阈值 %d 秒)" % (
                    self.screen.static_seconds, g["static_duration_sec"]))
            if audio_snap["quiet"] is not None and audio_snap["quiet"] >= g["quiet_duration_sec"]:
                reasons.append("桌面音频低于 %d dB 已持续 %.0f 秒(阈值 %d 秒)" % (
                    g["quiet_threshold_db"], audio_snap["quiet"], g["quiet_duration_sec"]))
        if reasons and now >= self._stop_retry_at:
            self._stop_recording(reasons, now)

    def _stop_recording(self, reasons, now):
        text = "; ".join(reasons)
        if self.dry_run:
            self.log.info("[模拟运行] 满足停止条件: %s(未真正停止录屏)" % text)
            self._after_stop()
            return
        try:
            data = self.obs.stop_record()
            self.log.info("*** 已自动停止 OBS 录屏 *** 原因: %s | OBS 返回: %s" % (
                text, fmt_json(data)))
            self._after_stop()
        except OBSClientError as exc:
            self.log.warning("停止录屏失败: %s(10 秒后重试)" % exc)
            self._stop_retry_at = now + 10.0

    def _after_stop(self):
        self._just_stopped = True
        self._on_record_stop()
        self._stop_retry_at = 0.0

    def _try_start_record(self, now):
        self._next_auto_start = now + 10.0  # 防止重复发起
        if self.dry_run:
            self.log.info("[模拟运行] 检测到会议且未在录屏, 将自动开始录屏")
            return
        try:
            data = self.obs.start_record()
            self.log.info("已自动开始 OBS 录屏: %s" % fmt_json(data))
        except OBSClientError as exc:
            self.log.warning("自动开始录屏失败: %s" % exc)

    def _log_heartbeat(self, meeting_active, audio_snap):
        static_note = ""
        db = audio_snap["db"]
        if (self.cfg["guard"]["static_require_quiet"] and db is not None
                and db >= self.cfg["guard"]["quiet_threshold_db"]):
            static_note = "(仍有音频, 静止条件不触发)"
        self.log.info("守护中 | 录屏=%s | 会议=%s | 桌面音频=%s dB(连续安静 %.0f 秒) | "
                      "画面静止 %.0f 秒%s | OBS=%s" % (
                          "是" if self.recording else "否",
                          "进行中" if meeting_active else "未检测到",
                          fmt_db(audio_snap["db"]), audio_snap["quiet"] or 0.0,
                          self.screen.static_seconds, static_note,
                          "已连接" if self.obs.connected else "未连接"))


# ============================================================================
#  OBS 密码自动读取(读取本机 obs-websocket 插件配置, 仅本地使用)
# ============================================================================
OBS_WS_CONFIG_PATHS = [
    # 标准安装 / 本机便携版实际使用的路径
    os.path.join(os.environ.get("APPDATA", ""), "obs-studio", "plugin_config",
                 "obs-websocket", "config.json"),
    # 部分版本存放在这里
    os.path.join(os.environ.get("APPDATA", ""), "obs-studio", "obs-websocket", "config.json"),
]


def read_obs_websocket_password():
    """
    从 obs-websocket 插件的配置文件中读取 WebSocket 密码。
    返回 (密码或 None, 来源文件路径或错误原因)。
    只读取本机文件, 不做任何网络操作。
    """
    for path in OBS_WS_CONFIG_PATHS:
        try:
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("auth_required") and data.get("server_password"):
                return data["server_password"], path
        except Exception:
            continue
    return None, "未找到 obs-websocket 配置文件(便携版 OBS 请在 config.ini 中手动填写密码)"


def make_obs_client(cfg, logger):
    """按配置创建 OBS 客户端: 手填密码优先, 为空且开启自动读取时从 OBS 配置读取"""
    password = cfg["obs"]["password"]
    if not password and cfg["obs"]["auto_read_obs_password"]:
        password, source = read_obs_websocket_password()
        if password:
            logger.info("已从 %s 自动读取 OBS WebSocket 密码(可设 auto_read_obs_password=false 关闭)", source)
        else:
            logger.warning("自动读取 OBS 密码失败: %s", source)
    return OBSClient(cfg["obs"]["host"], cfg["obs"]["port"], password or "", logger)


# ============================================================================
#  模块六: 图形界面(Win32 API 直接创建原生窗口, ctypes 实现, 零第三方依赖)
# ============================================================================
# 说明: 本机受管 Python 未附带 tkinter, 因此界面改用 Win32 标准控件
# (按钮/复选框/进度条/多行编辑框)直接构建, 与项目"纯标准库"一致。

class QueueLogHandler(logging.Handler):
    """把日志记录格式化后塞进队列, 供 GUI 主线程消费(跨线程安全)"""

    def __init__(self, target_queue):
        super().__init__()
        self.target_queue = target_queue

    def emit(self, record):
        try:
            self.target_queue.put(("log", self.format(record)))
        except Exception:
            pass


# ---- Win32 常量 ----
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_BORDER = 0x00800000
WS_VSCROLL = 0x00200000
WS_DISABLED = 0x08000000
SS_LEFT = 0x00000000
SS_CENTER = 0x00000001
SS_NOPREFIX = 0x00000080
ES_MULTILINE = 0x0004
ES_AUTOVSCROLL = 0x0040
ES_READONLY = 0x0800
BS_PUSHBUTTON = 0x0000
BS_AUTOCHECKBOX = 0x0003
WM_CREATE = 0x0001
WM_DESTROY = 0x0002
WM_SIZE = 0x0005
WM_CLOSE = 0x0010
WM_SETFONT = 0x0030
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_CTLCOLORSTATIC = 0x0138
BN_CLICKED = 0
EM_SETSEL = 0x00B1
EM_REPLACESEL = 0x00C2
EM_GETLINECOUNT = 0x00BA
EM_LINEINDEX = 0x00BB
PBM_SETRANGE32 = 0x0406
PBM_SETPOS = 0x0402
PBM_GETPOS = 0x0408
MB_OKCANCEL = 0x00000001
MB_ICONINFORMATION = 0x00000040
IDOK = 1
COLOR_BTNFACE = 15
LOGPIXELSX = 88
ICC_PROGRESS_CLASSES = 0x00000020
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004

# 控件 ID
IDC_START = 1001
IDC_STOP = 1002
IDC_DRY = 1003
IDC_SELFTEST = 1004
IDC_CONFIG = 1005
IDC_EXIT = 1006
TIMER_REFRESH = 1
TIMER_AUTOCLOSE = 2

# 状态灯颜色(COLORREF: 0x00BBGGRR)
LIGHT_GREEN = 0x009E1D   # #1D9E75 生效/正常
LIGHT_GRAY = 0x808788    # #888780 未激活/未知
LIGHT_AMBER = 0x279FEF   # #EF9F27 累积中/警告
LIGHT_RED = 0x4A4BE2     # #E24B4A 异常

FONT_FACE = "Microsoft YaHei UI"

# 两种参数形式的 SendMessageW(一个传整数, 一个传字符串指针)
_SendMsg = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                              ctypes.c_ssize_t, ctypes.c_ssize_t)(("SendMessageW", user32))
_SendMsgText = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                  ctypes.c_ssize_t, wintypes.LPWSTR)(("SendMessageW", user32))
gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.GetDeviceCaps.restype = ctypes.c_int
gdi32.CreateFontW.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                              wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                              wintypes.DWORD, wintypes.LPCWSTR]
gdi32.CreateFontW.restype = wintypes.HFONT
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
gdi32.SetTextColor.restype = wintypes.DWORD
gdi32.SetBkColor.argtypes = [wintypes.HDC, wintypes.DWORD]
gdi32.SetBkColor.restype = wintypes.DWORD
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.SetWindowTextW.restype = wintypes.BOOL
user32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
user32.EnableWindow.restype = wintypes.BOOL
user32.IsWindowEnabled.argtypes = [wintypes.HWND]
user32.IsWindowEnabled.restype = wintypes.BOOL
user32.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, wintypes.BOOL]
user32.MoveWindow.restype = wintypes.BOOL
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_void_p
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.KillTimer.restype = wintypes.BOOL
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.GetSysColor.argtypes = [ctypes.c_int]
user32.GetSysColor.restype = wintypes.DWORD
user32.GetSysColorBrush.argtypes = [ctypes.c_int]
user32.GetSysColorBrush.restype = wintypes.HBRUSH
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = ctypes.c_void_p
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
user32.MessageBoxW.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.UpdateWindow.argtypes = [wintypes.HWND]
user32.UpdateWindow.restype = wintypes.BOOL
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             ctypes.c_ssize_t, ctypes.c_ssize_t)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT),
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", ctypes.c_void_p),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON)]


class INITCOMMONCONTROLSEX(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("dwICC", wintypes.DWORD)]


user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.HWND, ctypes.c_void_p,
                                   wintypes.HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_ssize_t,
                                  ctypes.c_ssize_t]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]

_CLASS_REGISTERED = False


class GuardApp(object):
    """
    OBS 会议守护图形界面(Win32 原生窗口)。

    线程模型与 tkinter 版一致:
      - 主线程: 标准消息循环(GetMessage/DispatchMessage), WM_TIMER 驱动界面刷新;
      - 子线程: 运行 MeetingGuard.run_loop(), 通过队列推送日志和状态。
    """

    REFRESH_MS = 200
    LOG_MAX_LINES = 300
    WINDOW_CLASS = "OBSMeetingGuardWindow"

    def __init__(self, cfg, logger, config_path, base_dir, dry_run=False,
                 auto_close_sec=0, show=True):
        self.cfg = cfg
        self.log = logger
        self.config_path = config_path
        self.base_dir = base_dir
        self.dry_run = dry_run
        self.auto_close_sec = auto_close_sec  # >0 时为冒烟测试模式(到点自动关窗)
        self.show = show                      # 是否显示窗口(自动化测试时可设 False)
        self.gui_cfg = cfg.get("gui", {"always_on_top": False, "notify_on_stop": True})
        self.log_lines = []          # 日志行(与编辑框内容同步, 供测试断言)
        self.light_colors = {"obs": LIGHT_GRAY, "meeting": LIGHT_GRAY, "audio": LIGHT_GRAY}
        self.run_color = LIGHT_GRAY
        self.audio_db_color = LIGHT_GRAY
        self.running = False

        self._queue = queue.Queue()
        self._log_handler = QueueLogHandler(self._queue)
        self._log_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        logger.addHandler(self._log_handler)
        self._guard = None
        self._guard_thread = None
        self._stop_event = None

        # 预初始化所有控件句柄: 创建子控件的过程中就会收到 WM_CTLCOLORSTATIC
        # 等消息, 若不预置, 消息处理函数会因属性不存在而抛异常
        self.hwnd = None
        self.hwnd_title = None
        self.hwnd_run = None
        self.hwnd_audio_label = None
        self.hwnd_audio_db = None
        self.hwnd_obs_state = None
        self.hwnd_log = None
        self.hwnd_footer = None
        self.btn_start = None
        self.btn_stop = None
        self.chk_dry = None
        self.btn_selftest = None
        self.btn_config = None
        self.btn_exit = None
        self.lights = {}
        self.bars = {}

        self._register_class()
        self._scale = self._dpi_scale()
        self._create_window()
        self._create_controls()
        self._layout()
        self._refresh_idle_ui()

    # ------------------------------------------------------------ 窗口创建
    @classmethod
    def _register_class(cls):
        global _CLASS_REGISTERED
        if _CLASS_REGISTERED:
            return
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = cls._wnd_proc_raw
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinst
        wc.hIcon = None
        wc.hCursor = user32.LoadCursorW(None, 32512)  # IDC_ARROW
        wc.hbrBackground = user32.GetSysColorBrush(COLOR_BTNFACE)
        wc.lpszMenuName = None
        wc.lpszClassName = cls.WINDOW_CLASS
        wc.hIconSm = None
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            raise OSError("注册窗口类失败")
        _CLASS_REGISTERED = True

    @staticmethod
    @WNDPROC
    def _wnd_proc_raw(hwnd, msg, wparam, lparam):
        """静态入口: 通过窗口属性找到 GuardApp 实例, 再分发给实例方法"""
        app = _APP_BY_HWND.get(hwnd)
        if app is not None:
            return app._wnd_proc(hwnd, msg, wparam, lparam)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _dpi_scale(self):
        hdc = user32.GetDC(None)
        try:
            dpi = gdi32.GetDeviceCaps(hdc, LOGPIXELSX) or 96
        finally:
            user32.ReleaseDC(None, hdc)
        return max(1.0, dpi / 96.0)

    def _create_window(self):
        s = self._scale
        hinst = kernel32.GetModuleHandleW(None)
        w, h = int(660 * s), int(720 * s)
        x = max(0, (user32.GetSystemMetrics(0) - w) // 2)
        y = max(0, (user32.GetSystemMetrics(1) - h) // 3)
        style = 0x00CF0000  # WS_OVERLAPPEDWINDOW
        if self.show:
            style |= WS_VISIBLE
        ex_style = 0
        self.hwnd = user32.CreateWindowExW(
            ex_style, self.WINDOW_CLASS, "%s v%s" % (APP_NAME, VERSION), style,
            x, y, w, h, None, None, hinst, None)
        if not self.hwnd:
            raise OSError("创建主窗口失败")
        _APP_BY_HWND[self.hwnd] = self
        self.font = gdi32.CreateFontW(int(-16 * s), 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 0, 0,
                                      FONT_FACE)
        self.font_bold = gdi32.CreateFontW(int(-17 * s), 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 0,
                                           0, FONT_FACE)
        if self.show:
            user32.ShowWindow(self.hwnd, 5)  # SW_SHOW
            user32.UpdateWindow(self.hwnd)
        # 注意: WM_CREATE 在 CreateWindowExW 返回之前就已派发, 那时实例还未注册,
        # 因此定时器必须在这里(窗口创建完成后)启动, 不能放在 WM_CREATE 分支里
        user32.SetTimer(self.hwnd, TIMER_REFRESH, self.REFRESH_MS, None)
        if self.auto_close_sec > 0:
            user32.SetTimer(self.hwnd, TIMER_AUTOCLOSE, int(self.auto_close_sec * 1000), None)

    def _create_controls(self):
        s = self._scale

        # 进度条依赖 comctl32, 先初始化公共控件
        ice = INITCOMMONCONTROLSEX()
        ice.dwSize = ctypes.sizeof(INITCOMMONCONTROLSEX)
        ice.dwICC = ICC_PROGRESS_CLASSES
        ctypes.windll.comctl32.InitCommonControlsEx(ctypes.byref(ice))

        def child(cls_name, text, style, cid, x=0, y=0, w=10, h=10):
            hwnd = user32.CreateWindowExW(
                0, cls_name, text, WS_CHILD | WS_VISIBLE | style,
                int(x * s), int(y * s), int(w * s), int(h * s), self.hwnd, cid,
                kernel32.GetModuleHandleW(None), None)
            if hwnd and self.font:
                _SendMsg(hwnd, WM_SETFONT, self.font, 1)
            return hwnd

        # 标题行
        self.hwnd_title = child("Static", APP_NAME, SS_NOPREFIX, None, 16, 12, 300, 24)
        if self.hwnd_title and self.font_bold:
            _SendMsg(self.hwnd_title, WM_SETFONT, self.font_bold, 1)
        self.hwnd_run = child("Static", "未守护", SS_CENTER | SS_NOPREFIX, None, 380, 12, 250, 24)

        # 三个状态灯: 彩色标题(带圆点) + 灰色副标题
        self.lights = {}
        light_defs = [("obs", "OBS 录屏", "状态未知"),
                      ("meeting", "腾讯会议", "未检测到"),
                      ("audio", "桌面音频", "状态未知")]
        for key, title, sub in light_defs:
            self.lights[key] = {
                "title": child("Static", "\u25CF " + title, SS_NOPREFIX, None, 0, 0, 180, 20),
                "sub": child("Static", sub, SS_NOPREFIX, None, 0, 0, 180, 18),
            }

        # 三条进度条
        self.bars = {}
        bar_defs = [("meeting", "\u2460 会议结束"), ("static", "\u2461 画面静止"),
                    ("quiet", "\u2462 音频安静")]
        for key, title in bar_defs:
            self.bars[key] = {
                "label": child("Static", title, SS_NOPREFIX, None, 0, 0, 110, 20),
                "bar": child("msctls_progress32", "", WS_BORDER, None, 0, 0, 100, 18),
                "value": child("Static", "", SS_NOPREFIX, None, 0, 0, 180, 20),
            }

        # 音频电平行
        self.hwnd_audio_label = child("Static", "当前桌面音频电平:", SS_NOPREFIX, None,
                                      0, 0, 140, 20)
        self.hwnd_audio_db = child("Static", "N/A", SS_NOPREFIX, None, 0, 0, 90, 20)
        self.hwnd_obs_state = child("Static", "OBS: 未连接", SS_NOPREFIX,
                                    None, 0, 0, 240, 20)

        # 日志编辑框(多行/只读/自动滚动)
        self.hwnd_log = user32.CreateWindowExW(
            0x00000200, "Edit", "",  # WS_EX_CLIENTEDGE
            WS_CHILD | WS_VISIBLE | WS_VSCROLL | WS_BORDER | ES_MULTILINE | ES_AUTOVSCROLL
            | ES_READONLY,
            0, 0, 100, 100, self.hwnd, None, kernel32.GetModuleHandleW(None), None)
        if self.hwnd_log and self.font:
            _SendMsg(self.hwnd_log, WM_SETFONT, self.font, 1)

        # 按钮行
        self.btn_start = child("Button", "开始守护", BS_PUSHBUTTON, IDC_START, 0, 0, 72, 28)
        self.btn_stop = child("Button", "停止守护", BS_PUSHBUTTON, IDC_STOP, 0, 0, 72, 28)
        self.chk_dry = child("Button", "模拟运行", BS_AUTOCHECKBOX, IDC_DRY, 0, 0, 84, 28)
        self.btn_selftest = child("Button", "自检", BS_PUSHBUTTON, IDC_SELFTEST, 0, 0, 52, 28)
        self.btn_config = child("Button", "配置", BS_PUSHBUTTON, IDC_CONFIG, 0, 0, 52, 28)
        self.btn_exit = child("Button", "退出", BS_PUSHBUTTON, IDC_EXIT, 0, 0, 52, 28)

        # 底部提示
        self.hwnd_footer = child(
            "Static", "关闭窗口不会停止 OBS 录屏; 守护在后台运行, 可随时最小化",
            SS_NOPREFIX, None, 0, 0, 400, 18)

        # 初始禁用态
        user32.EnableWindow(self.btn_stop, False)
        if self.dry_run:
            _SendMsg(self.chk_dry, 0x00F1, 1, 0)  # BM_SETCHECK = BST_CHECKED

    def _layout(self):
        s = self._scale
        rc = wintypes.RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(rc))
        W, H = rc.right - rc.left, rc.bottom - rc.top
        m = int(16 * s)
        y = int(12 * s)

        user32.MoveWindow(self.hwnd_title, m, y, int(300 * s), int(24 * s), True)
        user32.MoveWindow(self.hwnd_run, W - m - int(250 * s), y, int(250 * s),
                          int(24 * s), True)
        y += int(32 * s)

        # 三个灯横排
        col_w = (W - 2 * m) // 3
        for i, key in enumerate(("obs", "meeting", "audio")):
            x = m + i * col_w
            user32.MoveWindow(self.lights[key]["title"], x, y, col_w, int(20 * s), True)
            user32.MoveWindow(self.lights[key]["sub"], x, y + int(20 * s), col_w,
                              int(18 * s), True)
        y += int(46 * s)

        # 进度条
        for key in ("meeting", "static", "quiet"):
            user32.MoveWindow(self.bars[key]["label"], m, y, int(112 * s), int(20 * s), True)
            user32.MoveWindow(self.bars[key]["bar"], m + int(116 * s), y,
                              max(60, W - 2 * m - int(310 * s)), int(18 * s), True)
            user32.MoveWindow(self.bars[key]["value"], W - m - int(188 * s), y,
                              int(188 * s), int(20 * s), True)
            y += int(26 * s)
        y += int(8 * s)

        # 音频电平行
        user32.MoveWindow(self.hwnd_audio_label, m, y, int(150 * s), int(20 * s), True)
        user32.MoveWindow(self.hwnd_audio_db, m + int(154 * s), y, int(90 * s),
                          int(20 * s), True)
        user32.MoveWindow(self.hwnd_obs_state, W - m - int(260 * s), y, int(260 * s),
                          int(20 * s), True)
        y += int(32 * s)

        # 按钮行(靠底部)
        btn_h = int(28 * s)
        btn_y = H - m - int(20 * s) - btn_h
        x = m
        for hwnd, w in ((self.btn_start, 72), (self.btn_stop, 72), (self.chk_dry, 84),
                        (self.btn_selftest, 52), (self.btn_config, 52), (self.btn_exit, 52)):
            user32.MoveWindow(hwnd, x, btn_y, int(w * s), btn_h, True)
            x += int((w + 8) * s)

        # 底部提示
        user32.MoveWindow(self.hwnd_footer, m, H - m - int(16 * s), int(500 * s),
                          int(16 * s), True)

        # 日志框填充剩余空间
        log_h = btn_y - int(8 * s) - y
        user32.MoveWindow(self.hwnd_log, m, y, W - 2 * m, max(int(60 * s), log_h), True)

    # ------------------------------------------------------------ 消息处理
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_CREATE:
            # 定时器已在窗口创建完成后启动(见 _create_window), 这里直接返回
            return 0
        if msg == WM_SIZE:
            self._layout()
            return 0
        if msg == WM_TIMER:
            if wparam == TIMER_REFRESH:
                self._poll_queue()
            elif wparam == TIMER_AUTOCLOSE:
                user32.KillTimer(hwnd, TIMER_AUTOCLOSE)
                user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_COMMAND:
            self._on_command(wparam)
            return 0
        if msg == WM_CTLCOLORSTATIC:
            return self._on_ctlcolor_static(wparam, lparam)
        if msg == WM_CLOSE:
            self._on_close(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.KillTimer(hwnd, TIMER_REFRESH)
            _APP_BY_HWND.pop(hwnd, None)
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_command(self, wparam):
        cid = wparam & 0xFFFF
        if cid == IDC_START:
            self.start_guard()
        elif cid == IDC_STOP:
            self.stop_guard()
        elif cid == IDC_SELFTEST:
            self.run_selftest_async()
        elif cid == IDC_CONFIG:
            self.open_config()
        elif cid == IDC_EXIT:
            self._on_close(self.hwnd)
        # IDC_DRY 复选框无需处理(开始守护时读取状态)

    def _on_ctlcolor_static(self, hdc, hwnd_ctrl):
        """给状态灯/运行标签/电平文本上色"""
        color = None
        if hwnd_ctrl == self.hwnd_run:
            color = self.run_color
        elif hwnd_ctrl == self.hwnd_audio_db:
            color = self.audio_db_color
        else:
            for key, light in self.lights.items():
                if hwnd_ctrl == light["title"]:
                    color = self.light_colors[key]
                    break
        if color is not None:
            gdi32.SetTextColor(hdc, color)
            gdi32.SetBkColor(hdc, user32.GetSysColor(COLOR_BTNFACE))
            return user32.GetSysColorBrush(COLOR_BTNFACE)
        return user32.DefWindowProcW(self.hwnd, WM_CTLCOLORSTATIC, hdc, hwnd_ctrl)

    # ------------------------------------------------------------ 界面刷新
    def _refresh_idle_ui(self):
        self.run_color = LIGHT_GRAY
        user32.SetWindowTextW(self.hwnd_run, "未守护")
        self.audio_db_color = LIGHT_GRAY
        user32.SetWindowTextW(self.hwnd_audio_db, "N/A")
        user32.SetWindowTextW(self.hwnd_obs_state, "OBS: 未连接")
        for key, light in self.lights.items():
            self.light_colors[key] = LIGHT_GRAY
            user32.SetWindowTextW(light["sub"], "未采集" if key == "audio" else "未检测")
        for key in self.bars:
            _SendMsg(self.bars[key]["bar"], PBM_SETRANGE32, 0, 1 << 16)
            _SendMsg(self.bars[key]["bar"], PBM_SETPOS, 0, 0)
            user32.SetWindowTextW(self.bars[key]["value"], "等待守护启动")

    def _apply_status(self, s):
        """把守护线程推来的状态快照刷新到界面"""
        th = s["thresholds"]
        # 运行标签
        if s["recording"]:
            self.run_color = LIGHT_GREEN
            text = "守护中 \u00B7 录屏中"
        else:
            self.run_color = LIGHT_AMBER
            text = "守护中 \u00B7 未录屏"
        if s["dry_run"]:
            text += " \u00B7 模拟"
        user32.SetWindowTextW(self.hwnd_run, text)

        # 三个灯
        if s["recording"]:
            self.light_colors["obs"] = LIGHT_GREEN
            user32.SetWindowTextW(self.lights["obs"]["sub"], "录制中(守护生效)")
        elif s["recording"] is False:
            self.light_colors["obs"] = LIGHT_GRAY
            user32.SetWindowTextW(self.lights["obs"]["sub"], "未在录屏")
        else:
            self.light_colors["obs"] = LIGHT_AMBER
            user32.SetWindowTextW(self.lights["obs"]["sub"], "状态未知")

        if s["meeting_active"]:
            self.light_colors["meeting"] = LIGHT_GREEN
            user32.SetWindowTextW(self.lights["meeting"]["sub"], "会议进行中")
        else:
            self.light_colors["meeting"] = LIGHT_GRAY
            user32.SetWindowTextW(self.lights["meeting"]["sub"], "未检测到会议")

        db = s["audio_db"]
        if s["audio_status"] != "运行中":
            self.light_colors["audio"] = LIGHT_RED
            user32.SetWindowTextW(self.lights["audio"]["sub"], "采集不可用")
        elif db is None:
            self.light_colors["audio"] = LIGHT_GRAY
            user32.SetWindowTextW(self.lights["audio"]["sub"], "状态未知")
        elif db >= th["quiet_threshold_db"]:
            self.light_colors["audio"] = LIGHT_GREEN
            user32.SetWindowTextW(self.lights["audio"]["sub"], "有声(%.1f dB)" % db)
        else:
            self.light_colors["audio"] = LIGHT_AMBER
            user32.SetWindowTextW(self.lights["audio"]["sub"],
                                  "安静累积 %.0f 秒" % s["audio_quiet_sec"])
        self.audio_db_color = self.light_colors["audio"]
        user32.SetWindowTextW(self.hwnd_audio_db, fmt_db(db))

        # OBS 连接
        if s["obs_connected"]:
            user32.SetWindowTextW(self.hwnd_obs_state,
                                  "OBS: 已连接 (obs-websocket %s)" % (s["obs_version"] or "?"))
        else:
            user32.SetWindowTextW(self.hwnd_obs_state, "OBS: 连接中...")

        # 三条进度条
        guard_max = max(int(th["meeting_gone_grace_sec"]), 1)
        if s["recording"] and s["meeting_ever_active"]:
            val = min(s["meeting_missing_sec"], guard_max)
        else:
            val = 0.0
        _SendMsg(self.bars["meeting"]["bar"], PBM_SETRANGE32, 0, guard_max)
        _SendMsg(self.bars["meeting"]["bar"], PBM_SETPOS, int(val), 0)
        user32.SetWindowTextW(
            self.bars["meeting"]["value"],
            "会议窗口消失 %.0f / %.0f 秒" % (s["meeting_missing_sec"], guard_max))

        static_max = max(int(th["static_duration_sec"]), 1)
        static_val = min(s["screen_static_sec"], static_max) if s["recording"] else 0.0
        _SendMsg(self.bars["static"]["bar"], PBM_SETRANGE32, 0, static_max)
        _SendMsg(self.bars["static"]["bar"], PBM_SETPOS, int(static_val), 0)
        if s.get("static_blocked_by_audio"):
            # 桌面音频仍在发声: 按设定静止条件不触发, 给用户一个明确提示
            static_text = "静止 %.0f 秒(仍有音频, 不触发)" % s["screen_static_sec"]
        else:
            static_text = "静止 %.0f / %.0f 秒" % (s["screen_static_sec"], static_max)
        user32.SetWindowTextW(self.bars["static"]["value"], static_text)

        quiet_max = max(int(th["quiet_duration_sec"]), 1)
        quiet_val = min(s["audio_quiet_sec"], quiet_max) if s["recording"] else 0.0
        _SendMsg(self.bars["quiet"]["bar"], PBM_SETRANGE32, 0, quiet_max)
        _SendMsg(self.bars["quiet"]["bar"], PBM_SETPOS, int(quiet_val), 0)
        user32.SetWindowTextW(
            self.bars["quiet"]["value"],
            "低于 %d dB 已 %.0f / %.0f 秒" % (th["quiet_threshold_db"],
                                             s["audio_quiet_sec"], quiet_max))

    def _append_log(self, line):
        self.log_lines.append(line)
        if len(self.log_lines) > self.LOG_MAX_LINES:
            del self.log_lines[:len(self.log_lines) - self.LOG_MAX_LINES]
        if self.hwnd_log:
            _SendMsg(self.hwnd_log, EM_SETSEL, -1, -1)
            _SendMsgText(self.hwnd_log, EM_REPLACESEL, 0, line + "\r\n")
            count = _SendMsg(self.hwnd_log, EM_GETLINECOUNT, 0, 0)
            if count > self.LOG_MAX_LINES:
                idx = _SendMsg(self.hwnd_log, EM_LINEINDEX, count - self.LOG_MAX_LINES, 0)
                _SendMsg(self.hwnd_log, EM_SETSEL, 0, idx)
                _SendMsgText(self.hwnd_log, EM_REPLACESEL, 0, "")
        if self.gui_cfg.get("notify_on_stop") and "已自动停止 OBS 录屏" in line:
            user32.MessageBoxW(self.hwnd, "已自动停止 OBS 录屏, 详见日志。",
                               APP_NAME, MB_ICONINFORMATION)

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "status":
                    self._apply_status(payload)
                elif kind == "selftest_done":
                    user32.MessageBoxW(
                        self.hwnd,
                        "自检结束: %s。明细见日志区。" % ("全部通过" if payload == 0
                                                    else "%d 项失败" % payload),
                        APP_NAME, MB_ICONINFORMATION)
        except queue.Empty:
            pass

    # ------------------------------------------------------------ 守护控制
    def start_guard(self):
        if self._guard_thread is not None and self._guard_thread.is_alive():
            return
        self._stop_event = threading.Event()
        dry_run = bool(_SendMsg(self.chk_dry, 0x00F0, 0, 0) & 0x0003)  # BM_GETCHECK
        self._guard = MeetingGuard(self.cfg, self.log, dry_run=dry_run,
                                   stop_event=self._stop_event,
                                   on_status=lambda s: self._queue.put(("status", s)))
        self._guard_thread = threading.Thread(target=self._guard.run_loop, daemon=True,
                                              name="gui-guard")
        self._guard_thread.start()
        self.running = True
        user32.EnableWindow(self.btn_start, False)
        user32.EnableWindow(self.btn_stop, True)
        self.log.info("界面: 守护已启动%s", "(模拟运行)" if dry_run else "")

    def stop_guard(self):
        if self._stop_event is not None:
            self._stop_event.set()
        user32.EnableWindow(self.btn_start, True)
        user32.EnableWindow(self.btn_stop, False)
        self.log.info("界面: 正在停止守护...")

        def _waiter():
            if self._guard_thread is not None:
                self._guard_thread.join(timeout=5)
            self.running = False
            self.log.info("界面: 守护已完全停止")
        threading.Thread(target=_waiter, daemon=True).start()

    def run_selftest_async(self):
        def _worker():
            failed = run_selftest(self.cfg, self.log, gui=True)
            self._queue.put(("selftest_done", failed))
        threading.Thread(target=_worker, daemon=True, name="gui-selftest").start()

    def open_config(self):
        try:
            os.startfile(self.config_path)
        except Exception as exc:
            self._append_log("界面: 打开配置文件失败: %s(路径: %s)" % (exc, self.config_path))

    def _on_close(self, hwnd):
        if self._guard_thread is not None and self._guard_thread.is_alive():
            if user32.MessageBoxW(hwnd, "守护正在运行, 确定退出?\n(退出不会停止 OBS 录屏)",
                                  "退出确认", MB_OKCANCEL | MB_ICONINFORMATION) != IDOK:
                return
            if self._stop_event is not None:
                self._stop_event.set()
            self._guard_thread.join(timeout=3)
        try:
            self.log.removeHandler(self._log_handler)
        except Exception:
            pass
        user32.DestroyWindow(hwnd)

    # ------------------------------------------------------------ 消息循环
    def run(self):
        if self.show:
            user32.ShowWindow(self.hwnd, 5)  # SW_SHOW
            user32.UpdateWindow(self.hwnd)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


_APP_BY_HWND = {}


def run_gui(cfg, logger, config_path, base_dir, dry_run=False):
    """启动图形界面; 创建失败时自动回退为控制台模式"""
    try:
        app = GuardApp(cfg, logger, config_path, base_dir, dry_run=dry_run)
    except Exception as exc:
        logger.warning("图形界面启动失败(%s), 已回退为控制台模式" % exc)
        guard = MeetingGuard(cfg, logger, dry_run=dry_run)
        guard.run()
        return
    app.run()


def gui_smoke_test(cfg, logger, config_path, base_dir, auto_close_sec=1.5):
    """
    GUI 冒烟测试: 创建窗口并刷新一次假状态, 到点自动关闭。
    用于验证 Win32 界面在打包后的 exe 里可以正常工作。
    """
    app = GuardApp(cfg, logger, config_path, base_dir, auto_close_sec=auto_close_sec)
    th = cfg["guard"]
    fake_status = {
        "recording": True, "meeting_active": True, "meeting_ever_active": True,
        "meeting_missing_sec": 12.0, "obs_connected": True, "obs_version": "smoke",
        "audio_status": "运行中", "audio_db": -33.3, "audio_quiet_sec": 5.0,
        "screen_static_sec": 120.0, "dry_run": True,
        "thresholds": {"meeting_gone_grace_sec": th["meeting_gone_grace_sec"],
                       "static_duration_sec": th["static_duration_sec"],
                       "quiet_duration_sec": th["quiet_duration_sec"],
                       "quiet_threshold_db": th["quiet_threshold_db"]},
    }
    app._apply_status(fake_status)
    app._append_log("GUI 冒烟测试: 界面创建与状态刷新正常")
    app.run()
    logger.info("GUI 冒烟测试完成, 窗口已自动关闭")
    return 0


#  自检 / 窗口列表 / 入口
# ============================================================================
def run_selftest(cfg, logger, gui=False):
    """逐项检查屏幕采集/音频采集/会议检测/OBS 连接, 返回失败项数量
    gui=True 时不打印到控制台, 结果只走日志(GUI 模式用)"""
    results = []

    def report(name, ok, detail):
        results.append((name, ok))
        if gui:
            logger.info("自检[%s] %s: %s", "OK" if ok else "失败", name, detail)
        else:
            print("[%s] %-14s %s" % ("OK" if ok else "失败", name, detail))

    if not gui:
        print("=" * 64)
        print("%s v%s 自检" % (APP_NAME, VERSION))
        print("=" * 64)

    # 1) 屏幕采集
    try:
        sm = ScreenMonitor(999, cfg["guard"]["static_diff_threshold"], logger)
        t0 = time.monotonic()
        raw = sm._capture()
        elapsed = (time.monotonic() - t0) * 1000
        if raw is None:
            report("屏幕采集", False, "截屏失败(远程桌面/锁屏状态下可能失败)")
        else:
            report("屏幕采集", True, "虚拟屏幕 %dx%d, 耗时 %.0f ms" % (sm.frame_size[0],
                                                                    sm.frame_size[1], elapsed))
    except Exception as exc:
        report("屏幕采集", False, "异常: %s" % exc)

    # 2) 桌面音频采集(采集 2.5 秒, 可同时播放声音验证电平变化)
    try:
        am = LoopbackAudioMonitor(cfg["guard"]["quiet_threshold_db"], logger)
        am.start()
        time.sleep(2.5)
        snap = am.snapshot()
        am.stop()
        ok = snap["status"] == "运行中"
        report("桌面音频", ok,
               "状态=%s | 当前电平=%s dB | 格式=%s%s" % (
                   snap["status"], fmt_db(snap["db"]), snap["fmt"],
                   "" if ok else " | 错误: %s" % snap["error"]))
        if ok:
            print("      (提示: 自检期间播放视频/声音, 电平应随之变化; 无声环境下约为 -90 dB 以下)")
    except Exception as exc:
        report("桌面音频", False, "异常: %s" % exc)

    # 3) 会议检测
    try:
        md = MeetingDetector(cfg, logger)
        active, detail = md.poll()
        report("会议检测", True, detail)
        print("      --- 目标进程窗口明细 ---")
        for line in md.debug_dump():
            print(line)
        if not md.last_windows:
            print("      (未发现 %s 的窗口, 请确认企业微信/腾讯会议已打开)" %
                  "/".join(sorted(md.process_names)))
    except Exception as exc:
        report("会议检测", False, "异常: %s" % exc)

    # 4) OBS WebSocket
    try:
        oc = make_obs_client(cfg, logger)
        oc.connect()
        active, data = oc.get_record_state()
        oc.close()
        report("OBS 连接", True, "obs-websocket %s | 当前%s在录屏 | %s" % (
            oc.ws_version or "?", "" if active else "未", fmt_json(data, 120)))
    except Exception as exc:
        report("OBS 连接", False, "%s(请在 OBS 中 工具->obs-websocket设置 开启服务器)" % exc)

    failed = sum(1 for _, ok in results if not ok)
    print("-" * 64)
    print("自检完成: %d 项通过, %d 项失败" % (len(results) - failed, failed))
    return failed


def list_windows(keyword=""):
    """列出所有顶层窗口(调试用), 可用关键词过滤进程名或标题"""
    rows = []
    for w in enum_top_windows():
        exe = get_process_name(w.pid) or "?"
        w.exe = exe
        if keyword and keyword.lower() not in exe and keyword.lower() not in w.title.lower():
            continue
        rows.append(w)
    print("%-24s %8s  %-36s %-28s %6s %6s %5s %5s %s" % (
        "进程", "PID", "标题", "类名", "X", "Y", "宽", "高", "状态"))
    print("-" * 128)
    for w in sorted(rows, key=lambda x: (x.exe, -x.area)):
        title = w.title if len(w.title) <= 34 else w.title[:33] + "~"
        cls = w.cls if len(w.cls) <= 26 else w.cls[:25] + "~"
        state = "可见" if w.visible else "隐藏"
        if w.minimized:
            state += "/最小化"
        print("%-24s %8d  %-36s %-28s %6d %6d %5d %5d %s" % (
            w.exe, w.pid, title, cls, w.left, w.top, w.width, w.height, state))
    print("-" * 128)
    print("共 %d 个窗口%s" % (len(rows), ("(关键词: %s)" % keyword) if keyword else ""))


def ensure_single_instance():
    """通过命名互斥体防止重复运行; 返回句柄(需保持存活)、None(已在运行)或 -1(检查失败)"""
    try:
        kernel32.CreateMutexW.use_last_error = True
        handle = kernel32.CreateMutexW(None, False, "OBSMeetingGuardSingletonMutex")
        if not handle:
            return -1  # 创建失败不阻止运行
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return None
        return handle
    except Exception:
        return -1  # 非致命, 继续运行


def load_config(config_path, base_dir):
    """加载配置; 文件不存在时生成默认配置。返回 (配置字典, 使用的文件路径)"""
    parser = configparser.ConfigParser()
    parser.read_dict(DEFAULT_CONFIG)
    path = config_path or os.path.join(base_dir, "config.ini")
    if os.path.exists(path):
        try:
            parser.read(path, encoding="utf-8-sig")
        except Exception as exc:
            raise SystemExit("配置文件解析失败: %s\n%s" % (path, exc))
    else:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(CONFIG_TEMPLATE)
            print("已生成默认配置文件: %s" % path)
        except OSError as exc:
            print("警告: 无法写入默认配置文件 %s (%s), 将使用内置默认值" % (path, exc))

    raw = {section: dict(parser.items(section)) for section in parser.sections()}

    def as_float(section, key, lo, hi):
        try:
            value = float(raw[section][key])
        except (KeyError, ValueError):
            raise SystemExit("配置错误: [%s] %s 必须是数字(当前值: %r)" %
                             (section, key, raw.get(section, {}).get(key)))
        if not (lo <= value <= hi):
            raise SystemExit("配置错误: [%s] %s 超出范围 [%s, %s](当前值: %s)" %
                             (section, key, lo, hi, value))
        return value

    def as_int(section, key, lo, hi):
        try:
            value = int(float(raw[section][key]))
        except (KeyError, ValueError):
            raise SystemExit("配置错误: [%s] %s 必须是整数(当前值: %r)" %
                             (section, key, raw.get(section, {}).get(key)))
        if not (lo <= value <= hi):
            raise SystemExit("配置错误: [%s] %s 超出范围 [%s, %s](当前值: %s)" %
                             (section, key, lo, hi, value))
        return value

    def as_bool(section, key):
        return str(raw[section][key]).strip().lower() in ("1", "true", "yes", "on")

    def as_list(section, key):
        return [item.strip() for item in raw[section][key].split(",") if item.strip()]

    cfg = {
        "obs": {
            "host": raw["obs"]["host"].strip() or "127.0.0.1",
            "port": as_int("obs", "port", 1, 65535),
            "password": raw["obs"]["password"],
            "auto_read_obs_password": as_bool("obs", "auto_read_obs_password"),
        },
        "guard": {
            "check_interval_sec": as_float("guard", "check_interval_sec", 0.2, 60),
            "meeting_gone_grace_sec": as_float("guard", "meeting_gone_grace_sec", 0, 3600),
            "static_check_interval_sec": as_float("guard", "static_check_interval_sec", 1, 600),
            "static_duration_sec": as_float("guard", "static_duration_sec", 0, 86400),
            "static_diff_threshold": as_float("guard", "static_diff_threshold", 0, 255),
            "quiet_threshold_db": as_float("guard", "quiet_threshold_db", -120, 0),
            "quiet_duration_sec": as_float("guard", "quiet_duration_sec", 0, 86400),
            "static_require_quiet": as_bool("guard", "static_require_quiet"),
            "static_quiet_require_meeting": as_bool("guard", "static_quiet_require_meeting"),
            "auto_start_recording": as_bool("guard", "auto_start_recording"),
        },
        "meeting": {
            "process_names": as_list("meeting", "process_names"),
            "min_width": as_int("meeting", "min_width", 0, 10000),
            "min_height": as_int("meeting", "min_height", 0, 10000),
            "meeting_class_keywords": as_list("meeting", "meeting_class_keywords"),
            "meeting_title_keywords": as_list("meeting", "meeting_title_keywords"),
            "class_exclude_keywords": as_list("meeting", "class_exclude_keywords"),
            "home_titles": as_list("meeting", "home_titles"),
            "title_exclude_keywords": as_list("meeting", "title_exclude_keywords"),
        },
        "log": {
            "level": raw["log"]["level"].strip().upper() or "INFO",
            "file": raw["log"]["file"].strip() or "guard.log",
        },
        "gui": {
            "always_on_top": as_bool("gui", "always_on_top"),
            "notify_on_stop": as_bool("gui", "notify_on_stop"),
        },
    }
    return cfg, path


def setup_logging(cfg, base_dir, console_level=None):
    """配置日志: 输出到滚动文件; 有控制台时同时输出到屏幕
    (PyInstaller --windowed 打包后 sys.stdout 为 None, 自动跳过控制台输出)"""
    logger = logging.getLogger("obs-meeting-guard")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(os.path.join(base_dir, cfg["log"]["file"]),
                                       maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        level = console_level or getattr(logging, cfg["log"]["level"], logging.INFO)
        console.setLevel(level)
        console.setFormatter(formatter)
        logger.addHandler(console)
    return logger


def main():
    parser = argparse.ArgumentParser(
        description="%s: 监视腾讯会议(含企业微信会议)/画面静止/桌面音频, 自动停止 OBS 录屏" % APP_NAME)
    parser.add_argument("--config", help="配置文件路径(默认为程序同目录的 config.ini)")
    parser.add_argument("--console", action="store_true",
                        help="使用控制台模式(默认是图形界面模式)")
    parser.add_argument("--selftest", action="store_true", help="运行自检后退出")
    parser.add_argument("--gui-selftest", action="store_true",
                        help="GUI 冒烟测试: 创建窗口并自动关闭, 用于验证界面可用")
    parser.add_argument("--dry-run", action="store_true",
                        help="模拟运行: 只记录日志, 不真正停止/开始录屏(GUI 中也可勾选)")
    parser.add_argument("--list-windows", nargs="?", const="", default=None, metavar="关键词",
                        help="列出所有顶层窗口(可用进程名/标题关键词过滤)后退出")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="控制台日志级别(默认取配置文件的 log.level)")
    args = parser.parse_args()

    # 确定程序所在目录(exe 打包后取 exe 所在目录)
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    # 高 DPI 感知, 保证多显示器下截屏坐标正确
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

    if args.list_windows is not None:
        list_windows(args.list_windows)
        return

    cfg, config_path = load_config(args.config, base_dir)
    logger = setup_logging(cfg, base_dir,
                           getattr(logging, args.log_level) if args.log_level else None)

    if args.selftest:
        sys.exit(1 if run_selftest(cfg, logger) else 0)

    if args.gui_selftest:
        sys.exit(gui_smoke_test(cfg, logger, config_path, base_dir))

    mutex = ensure_single_instance()
    if mutex is None:
        print("错误: %s 已在运行中(同时运行多个实例没有意义)。" % APP_NAME)
        sys.exit(1)

    if args.console:
        # 控制台模式(旧行为)
        guard = MeetingGuard(cfg, logger, dry_run=args.dry_run)
        guard.run()
    else:
        # 图形界面模式(默认); tkinter 不可用时自动回退控制台
        run_gui(cfg, logger, config_path, base_dir, dry_run=args.dry_run)


# ============================================================================
#  配置文件模板(首次运行自动生成)
# ============================================================================
CONFIG_TEMPLATE = r"""# ============================================================
#   OBS 会议守护 - 配置文件
#   所有时长单位为秒; 修改后保存, 重启程序生效
# ============================================================

[obs]
# OBS Studio 的 WebSocket 服务器(OBS 28+ 内置: 菜单 工具 -> obs-websocket 设置)
host = 127.0.0.1
port = 4455
# 密码: 在 obs-websocket 设置中启用了身份验证时必填
password =
# true  = password 留空时, 自动从 OBS 的 obs-websocket 配置文件读取密码
#        (%APPDATA%\obs-studio\plugin_config\obs-websocket\config.json),
#        仅本机读取、不会外传; 便携版 OBS 若不适用请手动填密码
# false = 只使用上面手填的密码
auto_read_obs_password = true

[guard]
# 主循环检测间隔(秒)
check_interval_sec = 1
# 会议窗口消失后, 等待多少秒才判定"会议结束"(防止切换窗口时误判)
meeting_gone_grace_sec = 30
# 每隔多少秒截屏比较一次画面
static_check_interval_sec = 30
# 画面连续静止超过多少秒则停止录屏(默认 900 秒 = 15 分钟)
static_duration_sec = 900
# 画面差异阈值(两次截屏的平均绝对差, 0-255), 小于该值视为"没有变化"
static_diff_threshold = 0.5
# 桌面音频(系统输出混音, 不含麦克风)安静阈值, 单位 dBFS
quiet_threshold_db = -45
# 桌面音频持续安静超过多少秒则停止录屏(默认 720 秒 = 12 分钟)
quiet_duration_sec = 720
# true  = 仅当桌面音频也处于安静状态(低于上面的阈值)时, "画面静止"条件才触发;
#         会议仍在发声(> -45 dB)时, 即使画面静止也不会停止录屏(默认)
# false = 画面静止条件独立生效, 不管当前有没有声音
static_require_quiet = true
# true  = 仅当检测到"会议进行中"时, 静止/安静条件才生效
# false = 只要 OBS 在录屏, 静止/安静条件就生效(默认)
static_quiet_require_meeting = false
# true  = 检测到会议自动开始录屏; false = 只负责停止录屏(默认)
auto_start_recording = false

[meeting]
# 要监视的进程名(逗号分隔, 不区分大小写):
#   wemeetapp.exe       独立版腾讯会议
#   WXWork.exe          企业微信主进程(融合版会议承载进程)
#   WXWorkWeb.exe       企业微信嵌入浏览器进程
process_names = wemeetapp.exe,WXWork.exe,WXWorkWeb.exe
# 会议窗口的最小尺寸(像素); 小于该尺寸的窗口(悬浮工具条/提示)会被忽略
min_width = 500
min_height = 350
# 正向信号①: 类名包含以下任一关键词的窗口, 判定为会议窗口(不区分大小写)
#   Meeting -> 企业微信会议窗口(如 MeetingWindow / WeMeeting* 等, 已从其程序二进制中核实)
#   Qt      -> 腾讯会议 Qt 原生窗口(独立版客户端 / SDK 原生界面)
meeting_class_keywords = Meeting,Qt
# 正向信号②: 标题包含以下任一关键词(默认关闭, 避免群聊名误判;
#   若自检发现会议窗口类名不在①中, 可在此填写"会议"等关键词启用)
meeting_title_keywords =
# 类名包含以下关键词的窗口直接排除(辅助窗口/隐藏窗口)
class_exclude_keywords = PerryShadow,TitleBar,IME,MSCTF,GDI+,SystemEventMonitor,Device Monitor,Thread Task,crashpad,StatusTray,PowerMessage,temp_d3d,Chrome_WidgetWin_0,SearchResult,WeworkAppBar,WeWorkNewTips
# 标题精确等于以下值的窗口排除(各软件主界面)
home_titles = 企业微信,微信,腾讯会议,Tencent Meeting,Voov Meeting
# 标题包含以下关键词的窗口排除(仅对"正向信号②"生效; 非会议功能的页面/对话框)
title_exclude_keywords = 加入会议,快速会议,预定会议,设置,邀请,等候室,网络检测,登录,升级,智能助理,文档,日程,邮件,通讯录,微盘,汇报,审批,打卡,客户,直播,收集表,搜索,通话,下载

[log]
# 日志级别: DEBUG / INFO / WARNING / ERROR
# (DEBUG 会记录每次检测到的窗口明细, 排查会议检测问题时很有用)
level = INFO
# 日志文件名(保存在程序同目录, 自动滚动, 最多 3 个备份)
file = guard.log

[gui]
# 图形界面设置(控制台模式 --console 下无效)
# 窗口总在最前
always_on_top = false
# 自动停止录屏时弹出提醒气泡
notify_on_stop = true
"""


if __name__ == "__main__":
    main()
