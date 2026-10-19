# -*- coding: utf-8 -*-
"""
音频安静计时器单元测试
=====================
验证 LoopbackAudioMonitor 的安静计时按"真实时间"累计, 且 -inf 算安静:
  1. 首次读数只建立基准, 不计时;
  2. 安静计时按读数的真实间隔增长(与音频块时长无关, 引擎供数快慢不影响);
  3. 电平达到/超过阈值(有声)立即清零;
  4. reset_quiet 后只累计之后新经过的间隔;
  5. -inf(数字静音)被认定为"低于阈值", 即安静;
  6. 电平恰好等于阈值不算安静(与"低于阈值才累计"的约定一致)。
不依赖真实声卡: 直接调用 _update_quiet_timer 注入时间与电平。
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import obs_meeting_guard as omg  # noqa: E402

logging.basicConfig(level=logging.WARNING)


def make_monitor(threshold=-45.0):
    """构造一个不启动采集线程的音频监视器(只测计时逻辑)"""
    return omg.LoopbackAudioMonitor(threshold, logging.getLogger("audio-timer-test"))


def main():
    passed = failed = 0

    def check(name, ok, detail=""):
        nonlocal passed, failed
        if ok:
            passed += 1
        else:
            failed += 1
        print("[%s] %s %s" % ("OK" if ok else "失败", name, detail))

    # 1) 首次读数建立基准, 不计时
    mon = make_monitor()
    t = 1000.0
    mon._update_quiet_timer(omg.SILENCE_DB, t)
    check("首次读数不计时", mon.quiet_seconds == 0.0,
          "quiet=%.3f" % mon.quiet_seconds)

    # 2) -inf 按真实间隔累计: 1 秒后仍静音 -> 计时 +1 秒
    mon._update_quiet_timer(omg.SILENCE_DB, t + 1.0)
    check("-inf 视为安静, 累计 1 秒", abs(mon.quiet_seconds - 1.0) < 1e-9,
          "quiet=%.3f" % mon.quiet_seconds)

    # 3) 计时速率等于真实时间: 每秒一次读数共 10 秒 -> 正好 10 秒
    #    (旧实现按音频块时长累加, 静音时引擎供数慢, 实测只有约 14%)
    mon2 = make_monitor()
    t = 2000.0
    mon2._update_quiet_timer(-60.0, t)
    for i in range(1, 11):
        mon2._update_quiet_timer(-60.0, t + i)
    check("安静计时速率=真实时间(10 秒)", abs(mon2.quiet_seconds - 10.0) < 1e-9,
          "quiet=%.3f" % mon2.quiet_seconds)

    # 4) 读数稀疏也不丢时间: 两次读数相隔 30 秒 -> 一次补满 30 秒
    mon2b = make_monitor()
    t = 2500.0
    mon2b._update_quiet_timer(-60.0, t)
    mon2b._update_quiet_timer(-60.0, t + 30.0)
    check("稀疏读数按真实间隔补齐(30 秒)", abs(mon2b.quiet_seconds - 30.0) < 1e-9,
          "quiet=%.3f" % mon2b.quiet_seconds)

    # 5) 有声立即清零
    mon2._update_quiet_timer(-30.0, t + 11)
    check("有声(>=阈值)立即清零", mon2.quiet_seconds == 0.0,
          "quiet=%.3f" % mon2.quiet_seconds)

    # 6) reset_quiet 之后只累计新间隔, 不会把清零前的时间补回来
    mon3 = make_monitor()
    t = 3000.0
    mon3._update_quiet_timer(-60.0, t)
    mon3._update_quiet_timer(-60.0, t + 5.0)   # 先累计 5 秒
    mon3.reset_quiet()
    check("reset_quiet 清零", mon3.quiet_seconds == 0.0)
    mon3._update_quiet_timer(-60.0, t + 6.0)   # 又过 1 秒
    check("reset 后只累计新间隔(1 秒)", abs(mon3.quiet_seconds - 1.0) < 1e-9,
          "quiet=%.3f" % mon3.quiet_seconds)

    # 7) -inf 不是"有声": 有声时才会清零, -inf 期间计时持续增长
    mon4 = make_monitor()
    t = 4000.0
    mon4._update_quiet_timer(omg.SILENCE_DB, t)
    mon4._update_quiet_timer(omg.SILENCE_DB, t + 2.0)
    check("-inf 期间计时持续增长(2 秒)", abs(mon4.quiet_seconds - 2.0) < 1e-9,
          "quiet=%.3f" % mon4.quiet_seconds)

    # 8) 恰好等于阈值不算安静(约定: 严格"低于"阈值才算安静)
    mon4._update_quiet_timer(-45.0, t + 3.0)
    check("电平等于阈值不算安静", mon4.quiet_seconds == 0.0,
          "quiet=%.3f" % mon4.quiet_seconds)

    # 9) current_db / last_update 同步更新
    check("current_db 已更新", mon4.current_db == -45.0)
    check("last_update 已更新", mon4.last_update == t + 3.0)

    print("-" * 56)
    print("音频计时器测试: %d 通过, %d 失败" % (passed, failed))
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
