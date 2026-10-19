# -*- coding: utf-8 -*-
"""
OBS WebSocket 客户端协议测试(对接 mock 服务器)
覆盖: 握手 / 认证(对与错) / GetRecordState / StartRecord / StopRecord /
      连接失败 / 认证失败 / 未知请求回退。
"""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mock_obs_server import MockOBSServer  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import obs_meeting_guard as omg  # noqa: E402


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger("ws-test")
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

    try:
        # 1) 正确密码: 连接 + 认证 + 查询
        client = omg.OBSClient("127.0.0.1", server.port, "test123", logger)
        client.connect()
        check("连接+认证", client.connected and client.ws_version == "5.5.6-mock",
              "ws_version=%s" % client.ws_version)
        active, data = client.get_record_state()
        check("GetRecordState", active is True, "outputActive=%s" % data.get("outputActive"))

        # 2) StopRecord
        client.stop_record()
        check("StopRecord", server.stop_count == 1 and not server.recording,
              "stop_count=%d recording=%s" % (server.stop_count, server.recording))

        # 3) 状态应已更新为未录屏
        active, data = client.get_record_state()
        check("停止后状态更新", active is False, "outputActive=%s" % data.get("outputActive"))

        # 4) StartRecord
        client.start_record()
        check("StartRecord", server.start_count == 1 and server.recording,
              "start_count=%d recording=%s" % (server.start_count, server.recording))

        # 5) 请求顺序(去掉连接时 GetVersion 探测的第一条)
        expected = ["GetRecordState", "StopRecord", "GetRecordState", "StartRecord"]
        actual = (server.requests[1:] if server.requests and server.requests[0] == "GetVersion"
                  else server.requests)
        check("请求序列", actual == expected, str(server.requests))
        client.close()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        check("协议主流程", False, "异常: %s" % exc)

    # 6) 错误密码必须被拒绝
    try:
        bad = omg.OBSClient("127.0.0.1", server.port, "wrong-pass", logger)
        bad.connect()
        check("错误密码被拒绝", False, "竟然连接成功了")
        bad.close()
    except omg.OBSClientError:
        check("错误密码被拒绝", True)
    except Exception as exc:
        check("错误密码被拒绝", False, "异常类型: %r" % exc)

    # 7) 连不上的端口必须报错
    try:
        none = omg.OBSClient("127.0.0.1", 1, "", logger)
        none.connect()
        check("无效端口报错", False, "竟然连接成功了")
    except omg.OBSClientError:
        check("无效端口报错", True)
    except Exception as exc:
        check("无效端口报错", False, "异常类型: %r" % exc)

    # 8) 无需认证的服务器(require_auth=False)
    try:
        server2 = MockOBSServer(password="", require_auth=False).start()
        time.sleep(0.2)
        c2 = omg.OBSClient("127.0.0.1", server2.port, "", logger)
        c2.connect()
        check("无密码服务器连接", c2.connected)
        active, _ = c2.get_record_state()
        check("无密码服务器查询", active is True)
        c2.close()
        server2.stop()
    except Exception as exc:
        check("无密码服务器", False, "异常: %s" % exc)

    # 9) 模拟 obs-websocket 5.5+(只支持 GetRecordStatus, 不支持 GetRecordState)
    try:
        server3 = MockOBSServer(password="test123", record_request_name="GetRecordStatus").start()
        time.sleep(0.2)
        c3 = omg.OBSClient("127.0.0.1", server3.port, "test123", logger)
        c3.connect()
        check("5.5版服务器: 探测到请求名", c3._record_state_request == "GetRecordStatus",
              "detected=%s" % c3._record_state_request)
        active, _ = c3.get_record_state()
        check("5.5版服务器: 查询录制状态", active is True)
        c3.stop_record()
        check("5.5版服务器: 停止录屏", server3.stop_count == 1)
        c3.close()
        server3.stop()
    except Exception as exc:
        check("5.5版服务器适配", False, "异常: %s" % exc)

    server.stop()
    print("-" * 56)
    print("协议测试: %d 通过, %d 失败" % (passed, failed))
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
