# -*- coding: utf-8 -*-
"""
桌面音频采集验证
================
运行本脚本, 程序会:
  1. 启动桌面音频(WASAPI 环回, 系统输出混音)采集;
  2. 前 2 秒保持安静;
  3. 第 2 秒开始播放 2.5 秒 750 Hz 提示音(通过默认播放设备);
  4. 之后恢复安静, 全程约 8 秒。
预期: 播放提示音期间电平明显升高(高于 -45 dB), 安静期间为 -inf 或极低。
"""
import ctypes
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import obs_meeting_guard as omg  # noqa: E402


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger("audio-test")
    am = omg.LoopbackAudioMonitor(-45.0, logger)
    am.start()

    def play_tone():
        time.sleep(2.0)
        print(">>> 开始播放 750Hz 提示音 2.5 秒 <<<")
        ctypes.windll.kernel32.Beep(750, 2500)
        print(">>> 提示音结束 <<<")

    threading.Thread(target=play_tone, daemon=True).start()

    peak_db = None
    snap = am.snapshot()
    for i in range(16):
        time.sleep(0.5)
        snap = am.snapshot()
        db = snap["db"]
        if db is not None and (peak_db is None or db > peak_db):
            peak_db = db
        print("t=%4.1fs  状态=%-10s 电平=%9s dB  连续安静=%5.1f 秒" % (
            i * 0.5 + 0.5, snap["status"], omg.fmt_db(db), snap["quiet"] or 0.0))
    am.stop()
    print("-" * 64)
    print("采集格式: %s" % am.device_info)
    print("期间最高电平: %s dB" % omg.fmt_db(peak_db))
    if peak_db is not None and peak_db > -45:
        print("结论: [通过] 采集到声音, 电平随播放正常变化")
    elif snap["status"] == "运行中":
        print("结论: [存疑] 采集器工作正常, 但没测到明显声音"
              "(Beep 可能不走默认播放设备, 请播放视频/音乐后重试)")
    else:
        print("结论: [失败] 采集器未正常运行: %s" % snap["error"])


if __name__ == "__main__":
    main()
