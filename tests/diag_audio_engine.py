# -*- coding: utf-8 -*-
"""
Windows 音频引擎供数行为探针
==========================
直接驱动 WASAPI 环回采集, 逐次打印 GetCurrentPadding / GetBuffer 的
返回值, 用来观察三种桌面状态下引擎是否供数:
  A. 无任何程序播放(无渲染流)
  B. 有渲染流但输出静音
  C. 有程序在播放声音
供数情况决定"安静计时"按什么时钟累计才不会失真。
"""
import ctypes
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import obs_meeting_guard as omg  # noqa: E402

logging.basicConfig(level=logging.WARNING)

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 14.0

mon = omg.LoopbackAudioMonitor(-45.0, logging.getLogger("probe"))
# 主线程调用 COM 前必须先做 MTA 初始化(采集线程内部自己会做, 这里直接调
# _init_device 走的是主线程, 需要手动初始化)
omg.ole32.CoInitializeEx(None, omg.COINIT_MULTITHREADED)
mon._init_device()
print("设备: %s" % mon.device_info)

t0 = time.monotonic()
reads = zeros = 0
try:
    while time.monotonic() - t0 < DURATION:
        now = time.monotonic() - t0
        padding = ctypes.c_uint32(0)
        hr = omg.com_call(mon._client, omg.IAUDIOCLIENT_GETCURRENTPADDING,
                          ctypes.c_long, [ctypes.POINTER(ctypes.c_uint32)],
                          ctypes.byref(padding))
        if hr != 0:
            print("t=%5.2f GetCurrentPadding 失败 hr=0x%08X" % (now, hr & 0xFFFFFFFF))
            break
        if padding.value == 0:
            zeros += 1
            if zeros <= 3 or zeros % 20 == 0:
                print("t=%5.2f padding=0 (引擎未供数, 第 %d 次)" % (now, zeros))
        else:
            data_ptr = ctypes.c_void_p()
            frames = ctypes.c_uint32(0)
            flags = ctypes.c_uint32(0)
            dev_pos = ctypes.c_uint64(0)
            qpc_pos = ctypes.c_uint64(0)
            hr = omg.com_call(mon._capture, omg.IAUDIOCAPTURECLIENT_GETBUFFER,
                              ctypes.c_long,
                              [ctypes.POINTER(ctypes.c_void_p),
                               ctypes.POINTER(ctypes.c_uint32),
                               ctypes.POINTER(ctypes.c_uint32),
                               ctypes.POINTER(ctypes.c_uint64),
                               ctypes.POINTER(ctypes.c_uint64)],
                              ctypes.byref(data_ptr), ctypes.byref(frames),
                              ctypes.byref(flags), ctypes.byref(dev_pos),
                              ctypes.byref(qpc_pos))
            if hr != 0:
                print("t=%5.2f GetBuffer 失败 hr=0x%08X" % (now, hr & 0xFFFFFFFF))
                break
            n = frames.value
            reads += 1
            silent = bool(flags.value & omg.AUDCLNT_BUFFERFLAGS_SILENT)
            if n and not silent:
                raw = ctypes.string_at(data_ptr.value, n * mon._block_align)
                db = "%.1f dB(有声)" % mon._calc_db(raw)
            else:
                db = "-inf(静音块)"
            print("t=%5.2f padding=%5d frames=%5d flags=%d %s"
                  % (now, padding.value, n, flags.value, db))
            omg.com_call(mon._capture, omg.IAUDIOCAPTURECLIENT_RELEASEBUFFER,
                         ctypes.c_long, [ctypes.c_uint32], n)
        time.sleep(0.05)
finally:
    mon._release_device()
    omg.ole32.CoUninitialize()

print("-" * 60)
print("成功读数 %d 次, padding=0 %d 次(共 %.1f 秒)"
      % (reads, zeros, time.monotonic() - t0))
