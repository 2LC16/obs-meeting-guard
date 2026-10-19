# -*- coding: utf-8 -*-
"""
守护主逻辑端到端测试
====================
用"假组件"(假音频/假屏幕/假会议检测) + mock OBS 服务器驱动 MeetingGuard
的主循环, 验证:
  1. 会议结束 -> 自动停止录屏;
  2. 画面静止 + 桌面音频安静 -> 自动停止录屏;
  3. 桌面音频安静 -> 自动停止录屏;
  4. 未在录屏 -> 不会误停止;
  5. 从未检测到会议 -> 会议条件不触发;
  6. --dry-run 模拟运行 -> 不真正停止;
  7. auto_start_recording -> 检测到会议自动开始录屏;
  8. 画面静止 + 桌面音频仍有声(> -45 dB) -> 不停止(静止门控);
  9. static_require_quiet=false -> 静止条件可独立触发;
  10. 有音频时静止计时被清零(音频转安静后需重新累积才触发);
  11. db=-inf(数字静音)被视为安静: 静止条件可触发, 且不会误判为"有声"清零计时。
不依赖真实的腾讯会议与 OBS。
"""
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mock_obs_server import MockOBSServer  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import obs_meeting_guard as omg  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeAudio(object):
    """假音频采集器: quiet 可由测试直接设置"""

    def __init__(self):
        self.quiet = 0.0
        self.db = -60.0
        self.status = "运行中"

    def start(self):
        pass

    def stop(self):
        pass

    def snapshot(self):
        return {"db": self.db, "quiet": self.quiet, "age": 0.1, "status": self.status,
                "error": "", "fmt": "mock"}

    def reset_quiet(self):
        self.quiet = 0.0


class FakeScreen(object):
    """假屏幕监视器: static_seconds 可由测试直接设置"""

    def __init__(self):
        self.static_seconds = 0.0

    def poll(self, now):
        pass

    def reset(self):
        self.static_seconds = 0.0


class FakeMeeting(object):
    """假会议检测器: active 可在运行中切换"""

    def __init__(self, active=False):
        self.active = active

    def poll(self):
        return self.active, "fake meeting detector"


def make_guard(server, meeting_active=False, dry_run=False):
    """按真实 config.ini 构造守护器, 再把各模块换成假组件并调小阈值"""
    cfg, _ = omg.load_config(os.path.join(BASE, "config.ini"), BASE)
    cfg["guard"]["meeting_gone_grace_sec"] = 0.5
    cfg["guard"]["static_duration_sec"] = 0.5
    cfg["guard"]["quiet_duration_sec"] = 0.5
    cfg["guard"]["check_interval_sec"] = 0.2
    cfg["guard"]["auto_start_recording"] = False
    logger = logging.getLogger("e2e")
    guard = omg.MeetingGuard(cfg, logger, dry_run=dry_run)
    guard.audio = FakeAudio()
    guard.screen = FakeScreen()
    guard.meeting = FakeMeeting(meeting_active)
    guard.obs = omg.OBSClient("127.0.0.1", server.port, "test123", logger)
    guard.obs.connect()
    guard.recording = True
    return guard


def run_loop(guard, seconds):
    """手动驱动主循环指定时长"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        guard._loop_once()
        time.sleep(0.02)


def main():
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
    server = MockOBSServer(password="test123").start()
    time.sleep(0.3)
    passed = failed = 0

    def check(name, ok, detail=""):
        nonlocal passed, failed
        if ok:
            passed += 1
        else:
            failed += 1
        print("[%s] %s %s" % ("OK" if ok else "失败", name, detail))

    guards = []
    try:
        # 场景1: 会议进行中 -> 会议结束 -> 应停止
        before = server.stop_count
        g1 = make_guard(server, meeting_active=True)
        guards.append(g1)
        run_loop(g1, 0.8)               # 先让 ever_active=True
        g1.meeting.active = False       # 模拟会议结束
        run_loop(g1, 2.0)
        check("① 会议结束自动停止", server.stop_count == before + 1,
              "stop_count=%d(+%d)" % (server.stop_count, server.stop_count - before))

        # 场景2: 会议中进行中 + 画面静止(且桌面音频安静) -> 应停止
        server.recording = True
        before = server.stop_count
        g2 = make_guard(server, meeting_active=True)
        guards.append(g2)
        run_loop(g2, 0.8)
        g2.audio.db = -60.0            # 桌面音频安静(低于 -45 dB)
        g2.screen.static_seconds = 999.0  # 模拟画面长时间静止
        run_loop(g2, 2.0)
        check("② 画面静止自动停止(音频安静)", server.stop_count == before + 1,
              "stop_count=%d(+%d)" % (server.stop_count, server.stop_count - before))

        # 场景3: 会议中进行中 + 桌面音频安静 -> 应停止
        server.recording = True
        before = server.stop_count
        g3 = make_guard(server, meeting_active=True)
        guards.append(g3)
        run_loop(g3, 0.8)
        g3.audio.quiet = 999.0           # 模拟长时间安静
        run_loop(g3, 2.0)
        check("③ 音频安静自动停止", server.stop_count == before + 1,
              "stop_count=%d(+%d)" % (server.stop_count, server.stop_count - before))

        # 场景4: 未在录屏 -> 不应停止
        server.recording = False
        before = server.stop_count
        g4 = make_guard(server, meeting_active=True)
        guards.append(g4)
        g4.recording = False
        run_loop(g4, 0.8)
        g4.meeting.active = False
        run_loop(g4, 2.0)
        check("④ 未录屏不误停", server.stop_count == before,
              "stop_count=%d(+%d)" % (server.stop_count, server.stop_count - before))

        # 场景5: 从未检测到会议 -> 会议条件不应触发
        server.recording = True
        before = server.stop_count
        g5 = make_guard(server, meeting_active=False)
        guards.append(g5)
        run_loop(g5, 2.0)
        check("⑤ 无会议时会议条件不触发", server.stop_count == before,
              "stop_count=%d(+%d)" % (server.stop_count, server.stop_count - before))

        # 场景6: 模拟运行(dry-run) -> 不真正停止
        server.recording = True
        before = server.stop_count
        g6 = make_guard(server, meeting_active=True, dry_run=True)
        guards.append(g6)
        run_loop(g6, 0.8)
        g6.meeting.active = False
        run_loop(g6, 2.0)
        check("⑥ 模拟运行不真停", server.stop_count == before,
              "stop_count=%d(+%d)" % (server.stop_count, server.stop_count - before))

        # 场景7: 自动开始录屏
        server.recording = False
        before = server.start_count
        g7 = make_guard(server, meeting_active=True)
        guards.append(g7)
        g7.recording = False
        g7.cfg["guard"]["auto_start_recording"] = True
        run_loop(g7, 2.5)
        check("⑦ 自动开始录屏", server.start_count == before + 1,
              "start_count=%d(+%d)" % (server.start_count, server.start_count - before))

        # 场景8: 画面静止 + 桌面音频仍有声(> -45 dB) -> 不应停止
        server.recording = True
        before = server.stop_count
        g8 = make_guard(server, meeting_active=True)
        guards.append(g8)
        run_loop(g8, 0.8)
        g8.audio.db = -30.0            # 会议仍在发声
        g8.audio.quiet = 0.0
        g8.screen.static_seconds = 999.0
        run_loop(g8, 2.0)
        check("⑧ 有音频时静止不停止", server.stop_count == before,
              "stop_count=%d(+%d)" % (server.stop_count, server.stop_count - before))
        # 音频随后安静下来, 但有声期间静止计时已被清零 -> 不会凭陈旧计时立即停止
        g8.audio.db = -60.0
        run_loop(g8, 2.0)
        check("⑧b 音频转安静不凭陈旧计时误停", server.stop_count == before,
              "stop_count=%d(+%d)" % (server.stop_count, server.stop_count - before))

        # 场景9: 关闭 static_require_quiet -> 静止条件独立触发(仍有音频也停)
        server.recording = True
        before = server.stop_count
        g9 = make_guard(server, meeting_active=True)
        guards.append(g9)
        g9.cfg["guard"]["static_require_quiet"] = False
        run_loop(g9, 0.8)
        g9.audio.db = -30.0            # 仍有声
        g9.audio.quiet = 0.0
        g9.screen.static_seconds = 999.0
        run_loop(g9, 2.0)
        check("⑨ 关闭门控后静止可独立触发", server.stop_count == before + 1,
              "stop_count=%d(+%d)" % (server.stop_count, server.stop_count - before))

        # 场景10: 有音频时清除静止计时(不清会让陈旧计时在音频转安静瞬间误触停)
        server.recording = True
        before = server.stop_count
        g10 = make_guard(server, meeting_active=True)
        guards.append(g10)
        run_loop(g10, 0.8)
        g10.audio.db = -30.0           # 仍有声
        g10.audio.quiet = 0.0
        g10.screen.static_seconds = 999.0
        run_loop(g10, 2.0)
        check("⑩ 有音频时静止计时被清零", g10.screen.static_seconds == 0.0,
              "static_seconds=%.1f" % g10.screen.static_seconds)
        check("⑩b 有音频时不会误停", server.stop_count == before,
              "stop_count=%d(+%d)" % (server.stop_count, server.stop_count - before))
        # 音频转安静 + 画面重新静止 -> 重新累积后才停(不会一安静就立即触发)
        g10.audio.db = -60.0
        g10.screen.static_seconds = 999.0
        run_loop(g10, 2.0)
        check("⑩c 安静后静止仍可触发", server.stop_count == before + 1,
              "stop_count=%d(+%d)" % (server.stop_count, server.stop_count - before))

        # 场景11: db=-inf(数字静音, RMS=0)必须被认定为"安静"
        server.recording = True
        before = server.stop_count
        g11 = make_guard(server, meeting_active=True)
        guards.append(g11)
        run_loop(g11, 0.8)
        g11.audio.db = float("-inf")   # 数字静音: 低于任何阈值, 等价于极安静
        g11.audio.quiet = 0.0
        g11.screen.static_seconds = 999.0
        run_loop(g11, 2.0)
        check("⑪ -inf 视为安静, 静止条件可触发", server.stop_count == before + 1,
              "stop_count=%d(+%d)" % (server.stop_count, server.stop_count - before))

        # 场景11b: -inf 不是"有声" -> 不清除静止计时(不清零才算认定它为安静)
        # 注意: 先让 OBS 侧变为"未录屏", 否则守护会因静止满阈值走停止流程重置计时,
        # 就隔离不出"有声清零逻辑是否触发"了。
        server.recording = False
        g11b = make_guard(server, meeting_active=True)
        guards.append(g11b)
        g11b.recording = False          # 不评估停止条件, 单独验证清零逻辑
        run_loop(g11b, 0.8)
        g11b.audio.db = float("-inf")
        g11b.screen.static_seconds = 50.0
        run_loop(g11b, 1.0)
        check("⑪b -inf 不清除静止计时(是安静不是有声)",
              g11b.screen.static_seconds == 50.0,
              "static_seconds=%.1f" % g11b.screen.static_seconds)
        server.recording = True

    except Exception:
        import traceback
        traceback.print_exc()
        check("端到端流程", False, "发生异常")
    finally:
        for g in guards:
            try:
                g.obs.close()
            except Exception:
                pass
        server.stop()

    print("-" * 56)
    print("端到端测试: %d 通过, %d 失败" % (passed, failed))
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
