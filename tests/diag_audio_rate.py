# -*- coding: utf-8 -*-
"""
音频安静计时速率诊断(真实声卡)
============================
启动 LoopbackAudioMonitor 静默运行数秒, 对比 quiet_seconds 累计值
与真实经过时间, 验证"安静计时与真实世界的一秒一致"。
供数快慢(引擎行为)不应影响计时速率。
用法: python diag_audio_rate.py [秒数]
"""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import obs_meeting_guard as omg  # noqa: E402

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
THRESHOLD = -45.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    datefmt="%H:%M:%S")

mon = omg.LoopbackAudioMonitor(THRESHOLD, logging.getLogger("diag"))
mon.start()
time.sleep(1.0)  # 等采集初始化

t0 = time.monotonic()
while time.monotonic() - t0 < DURATION:
    time.sleep(1.0)
    snap = mon.snapshot()
    print("真实 %.1fs | 电平=%s | 安静计时=%.2fs"
          % (time.monotonic() - t0, omg.fmt_db(snap["db"]), snap["quiet"] or 0.0))

mon.stop()
mon.join(timeout=3)
snap = mon.snapshot()
quiet = snap["quiet"] or 0.0
print("-" * 60)
print("真实经过: %.2f 秒 | 安静计时: %.2f 秒 | 速率比: %.1f%%"
      % (DURATION, quiet, quiet / DURATION * 100 if DURATION else 0))
if quiet < DURATION * 0.8:
    print("[结论] 安静计时明显慢于真实时间")
    sys.exit(1)
print("[结论] 安静计时与真实时间基本一致")
