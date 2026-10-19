# -*- coding: utf-8 -*-
"""
GUI 单元测试(Win32 原生窗口, 不弹窗常驻, 手动驱动刷新)
=====================================================
验证: 窗口可创建(测试模式不显示); 状态快照能正确刷新三个状态灯/三条
进度条/电平文本; 日志能进入编辑框; 开始/停止守护按钮状态切换; 并能
真实跑一小段守护(dry-run, 只读 OBS)。
"""
import ctypes
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import obs_meeting_guard as omg  # noqa: E402
from ctypes import wintypes  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
user32 = ctypes.windll.user32


def get_text(hwnd):
    """读取控件文本"""
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def main():
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("gui-test")
    cfg, config_path = omg.load_config(os.path.join(BASE, "config.ini"), BASE)
    app = omg.GuardApp(cfg, logger, config_path, BASE, show=False)
    th = cfg["guard"]
    passed = failed = 0

    def check(name, ok, detail=""):
        nonlocal passed, failed
        if ok:
            passed += 1
        else:
            failed += 1
        print("[%s] %s %s" % ("OK" if ok else "失败", name, detail))

    try:
        check("窗口创建", bool(app.hwnd), "hwnd=%s" % app.hwnd)
        check("窗口标题", get_text(app.hwnd) == "%s v%s" % (omg.APP_NAME, omg.VERSION),
              "title=%r" % get_text(app.hwnd))

        # 1) 假状态刷新
        status = {
            "recording": True, "meeting_active": True, "meeting_ever_active": True,
            "meeting_missing_sec": 12.0, "obs_connected": True, "obs_version": "test",
            "audio_status": "运行中", "audio_db": -33.3, "audio_quiet_sec": 5.0,
            "screen_static_sec": 120.0, "dry_run": True,
            "thresholds": {"meeting_gone_grace_sec": th["meeting_gone_grace_sec"],
                           "static_duration_sec": th["static_duration_sec"],
                           "quiet_duration_sec": th["quiet_duration_sec"],
                           "quiet_threshold_db": th["quiet_threshold_db"]},
        }
        app._apply_status(status)
        check("会议灯变绿", app.light_colors["meeting"] == omg.LIGHT_GREEN,
              "color=0x%06X" % app.light_colors["meeting"])
        check("音频灯变绿(有声)", app.light_colors["audio"] == omg.LIGHT_GREEN,
              "color=0x%06X" % app.light_colors["audio"])
        check("OBS灯变绿", app.light_colors["obs"] == omg.LIGHT_GREEN)
        check("会议副标题", get_text(app.lights["meeting"]["sub"]) == "会议进行中",
              "text=%r" % get_text(app.lights["meeting"]["sub"]))
        pos = omg._SendMsg(app.bars["static"]["bar"], omg.PBM_GETPOS, 0, 0)
        check("静止进度条数值", pos == 120, "pos=%s" % pos)
        check("电平文本", get_text(app.hwnd_audio_db) == "-33.3",
              "text=%r" % get_text(app.hwnd_audio_db))
        check("运行标签", "守护中" in get_text(app.hwnd_run),
              "text=%r" % get_text(app.hwnd_run))
        check("OBS连接文本", "已连接" in get_text(app.hwnd_obs_state),
              "text=%r" % get_text(app.hwnd_obs_state))

        # 1b) 安静场景: 低于阈值 -> 黄灯 + 安静进度条
        status["audio_db"] = -60.0
        status["audio_quiet_sec"] = 300.0
        app._apply_status(status)
        check("音频灯变黄(安静累积)", app.light_colors["audio"] == omg.LIGHT_AMBER,
              "color=0x%06X" % app.light_colors["audio"])
        check("安静进度条数值",
              omg._SendMsg(app.bars["quiet"]["bar"], omg.PBM_GETPOS, 0, 0) == 300)
        check("安静副标题", "安静累积" in get_text(app.lights["audio"]["sub"]),
              "text=%r" % get_text(app.lights["audio"]["sub"]))

        # 1c) 静止被"仍有音频"阻塞: 进度条文本应给出提示
        status["static_blocked_by_audio"] = True
        app._apply_status(status)
        check("静止阻塞提示", "仍有音频" in get_text(app.bars["static"]["value"]),
              "text=%r" % get_text(app.bars["static"]["value"]))
        status.pop("static_blocked_by_audio")
        app._apply_status(status)
        check("静止提示恢复", "仍有音频" not in get_text(app.bars["static"]["value"]),
              "text=%r" % get_text(app.bars["static"]["value"]))

        # 2) 日志入队并进入编辑框
        logger.info("GUI测试日志一行")
        app._poll_queue()
        check("日志进入列表", any("GUI测试日志一行" in line for line in app.log_lines))

        # 3) 初始按钮状态
        check("初始按钮状态",
              user32.IsWindowEnabled(app.btn_start)
              and not user32.IsWindowEnabled(app.btn_stop))

        # 4) 真实启动守护 3 秒(dry-run, 只读 OBS, 不会停止任何录屏)
        omg._SendMsg(app.chk_dry, 0x00F1, 1, 0)  # BM_SETCHECK 勾选模拟运行
        app.start_guard()
        time.sleep(3.0)
        app._poll_queue()
        check("守护启动后按钮切换",
              not user32.IsWindowEnabled(app.btn_start)
              and user32.IsWindowEnabled(app.btn_stop))
        check("收到状态推送", "守护中" in get_text(app.hwnd_run),
              "text=%r" % get_text(app.hwnd_run))
        check("守护启动日志", any("守护已启动" in line for line in app.log_lines))

        # 5) 停止守护(轮询等待"完全停止"日志, 最多 6 秒)
        app.stop_guard()
        stopped_log = False
        for _ in range(30):
            time.sleep(0.2)
            app._poll_queue()
            if any("守护已完全停止" in line for line in app.log_lines):
                stopped_log = True
                break
        check("停止后按钮恢复", user32.IsWindowEnabled(app.btn_start))
        check("出现完全停止日志", stopped_log)

        # 6) 可重入: 再启动一轮
        app.start_guard()
        time.sleep(1.0)
        app.stop_guard()
        time.sleep(2.0)
        check("可重复启停", user32.IsWindowEnabled(app.btn_start))
    finally:
        try:
            if app._stop_event is not None:
                app._stop_event.set()
            app.log.removeHandler(app._log_handler)
            user32.DestroyWindow(app.hwnd)
        except Exception:
            pass

    print("-" * 56)
    print("GUI 测试: %d 通过, %d 失败" % (passed, failed))
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
