# -*- coding: utf-8 -*-
"""
画面静止检测测试
================
1. 合成数据单元测试: 验证 _is_similar 的差异阈值判断;
2. 真实截屏测试: 连续两帧(画面不变)应判定相似; 打开记事本制造画面
   变化后应判定不相似; 关闭后恢复相似。
"""
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import obs_meeting_guard as omg  # noqa: E402


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger("screen-test")
    sm = omg.ScreenMonitor(1, 0.5, logger)
    passed = failed = 0

    def check(name, ok, detail=""):
        nonlocal passed, failed
        if ok:
            passed += 1
        else:
            failed += 1
        print("[%s] %s %s" % ("OK" if ok else "失败", name, detail))

    # ---- 1. 合成数据 ----
    base = bytes([128]) * (1920 * 1080 * 4)          # 一整屏灰色
    same = bytes([128]) * (1920 * 1080 * 4)
    tiny = bytearray(base)                           # 只改 10 个字节
    for i in range(0, 10):
        tiny[i * 4] = 200
    tiny = bytes(tiny)
    half = bytes([200]) * (1920 * 1080 * 4)          # 半屏大变
    check("相同帧判定为静止", sm._is_similar(base, same))
    check("微小变化仍判静止", sm._is_similar(base, tiny))
    check("大幅变化判定为动态", not sm._is_similar(base, half))

    # ---- 2. 真实截屏 ----
    f1 = sm._capture()
    check("截屏成功", f1 is not None and len(f1) > 0,
          "尺寸=%s 字节数=%s" % (sm.frame_size, len(f1) if f1 else 0))
    time.sleep(0.3)
    f2 = sm._capture()
    check("画面不变时两帧相似", sm._is_similar(f1, f2))

    # 打开记事本制造画面变化
    subprocess.Popen(["notepad.exe"])
    time.sleep(1.5)
    f3 = sm._capture()
    check("打开窗口后判定为变化", not sm._is_similar(f2, f3))

    # 关闭记事本, 画面应恢复静止(与关闭后的新基线比较, 不依赖与初始帧完全相同)
    subprocess.run(["taskkill", "/IM", "notepad.exe", "/F"],
                   capture_output=True, creationflags=0x08000000)
    time.sleep(1.2)
    f4 = sm._capture()
    time.sleep(0.4)
    f5 = sm._capture()
    check("关闭窗口后恢复静止", sm._is_similar(f4, f5))

    # ---- 3. poll 累计逻辑 ----
    sm2 = omg.ScreenMonitor(0.3, 0.5, logger)
    now = time.monotonic()
    for _ in range(6):  # 画面不变, 累计约 1.5 秒(首次仅建立基线)
        sm2.poll(now)
        now += 0.3
    # 留 0.1 秒容差: 单调时钟上反复 +0.3 存在浮点累积误差
    check("poll 累计静止时长", sm2.static_seconds >= 1.4,
          "static_seconds=%.3f" % sm2.static_seconds)

    print("-" * 56)
    print("屏幕测试: %d 通过, %d 失败" % (passed, failed))
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
