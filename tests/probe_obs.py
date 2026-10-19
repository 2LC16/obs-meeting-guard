# -*- coding: utf-8 -*-
"""OBS 协议探针: 连接真实 OBS, 逐个发送请求并打印原始响应(用于排查 204 等问题)"""
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import obs_meeting_guard as omg  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger("probe")
    cfg, _ = omg.load_config(os.path.join(BASE, "config.ini"), BASE)
    client = omg.make_obs_client(cfg, logger)
    client.connect()
    print("已连接, obsWebSocketVersion = %r" % client.ws_version)
    for rtype in ["GetVersion", "GetRecordState", "GetRecordStatus"]:
        try:
            data = client.call(rtype, {}, timeout=6)
            print("%-16s -> OK   %s" % (rtype, json.dumps(data, ensure_ascii=False)[:220]))
        except Exception as exc:
            print("%-16s -> %s: %s" % (rtype, type(exc).__name__, exc))
        time.sleep(0.3)
    # 连续快速调用 5 次 GetRecordState, 模拟守护的轮询
    print("--- 连续轮询 GetRecordState x5 ---")
    for i in range(5):
        try:
            active, data = client.get_record_state()
            print("第%d次 -> active=%s" % (i + 1, active))
        except Exception as exc:
            print("第%d次 -> %s: %s" % (i + 1, type(exc).__name__, exc))
        time.sleep(0.2)
    client.close()


if __name__ == "__main__":
    main()
