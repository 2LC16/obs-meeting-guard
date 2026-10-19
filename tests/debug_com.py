# -*- coding: utf-8 -*-
"""COM 调用诊断: 逐步执行 WASAPI 初始化, 打印每步 HRESULT 与 GUID 原始字节"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from obs_meeting_guard import (CLSCTX_ALL, CLSID_MMDeviceEnumerator, GUID, IID_IAudioClient,
                               IID_IMMDeviceEnumerator, com_call, make_guid, ole32)  # noqa: E402


def dump_guid(text):
    g = make_guid(text)
    raw = bytes(g)
    print("%s -> %s" % (text, raw.hex(" ")))
    return raw


print("=== GUID 原始字节(小端) ===")
dump_guid(CLSID_MMDeviceEnumerator)
dump_guid(IID_IMMDeviceEnumerator)
dump_guid(IID_IAudioClient)
print("期望 IID_IAudioClient = 4c ad b9 1c fa db 32 4c b1 78 c2 f5 68 a7 03 b1")

print("\n=== 逐步初始化 ===")
ole32.CoInitializeEx(None, 0x0)  # MTA, 与音频线程一致
enum = ctypes.c_void_p()
hr = ole32.CoCreateInstance(ctypes.byref(make_guid(CLSID_MMDeviceEnumerator)), None,
                            CLSCTX_ALL, ctypes.byref(make_guid(IID_IMMDeviceEnumerator)),
                            ctypes.byref(enum))
print("CoCreateInstance          hr=0x%08X  enum=0x%X" % (hr & 0xFFFFFFFF, enum.value or 0))
if hr != 0:
    sys.exit(1)

device = ctypes.c_void_p()
hr = com_call(enum, 4, ctypes.c_long,
              [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)],
              0, 0, ctypes.byref(device))
print("GetDefaultAudioEndpoint   hr=0x%08X  device=0x%X" % (hr & 0xFFFFFFFF, device.value or 0))
if hr != 0:
    sys.exit(1)

vtbl = ctypes.cast(device.value, ctypes.POINTER(ctypes.c_void_p))[0]
print("device 虚表地址 = 0x%X" % vtbl)
for i in range(6):
    print("   vtbl[%d] = 0x%X" % (i, ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[i]))

client = ctypes.c_void_p()
hr = com_call(device, 3, ctypes.c_long,
              [ctypes.POINTER(GUID), ctypes.c_uint, ctypes.c_void_p,
               ctypes.POINTER(ctypes.c_void_p)],
              ctypes.byref(make_guid(IID_IAudioClient)), CLSCTX_ALL, None, ctypes.byref(client))
print("Activate(IAudioClient)    hr=0x%08X  client=0x%X" % (hr & 0xFFFFFFFF, client.value or 0))
