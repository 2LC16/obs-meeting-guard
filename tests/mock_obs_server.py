# -*- coding: utf-8 -*-
"""
模拟 OBS WebSocket 服务器(测试用, 不需要真实的 OBS)
====================================================
实现 obs-websocket v5 协议的最小子集:
  - WebSocket 握手(正确计算 Sec-WebSocket-Accept);
  - Hello(op 0, 带认证挑战 challenge/salt);
  - Identify(op 1)并按协议算法校验认证字符串;
  - Identified(op 2);
  - GetRecordState / StartRecord / StopRecord 请求(op 6)-> 响应(op 7)。
每个连接一个线程, 记录收到的请求与录制状态变化, 供测试断言。
"""
import base64
import hashlib
import json
import socket
import struct
import threading

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class MockOBSServer(object):
    def __init__(self, password="test123", port=0, require_auth=True,
                 record_request_name="GetRecordState"):
        self.password = password
        self.require_auth = require_auth
        self.record_request_name = record_request_name  # 模拟不同 obs-websocket 版本的请求名
        self.recording = True
        self.stop_count = 0
        self.start_count = 0
        self.requests = []          # 收到的请求类型(按顺序)
        self.auth_ok_count = 0      # 认证成功次数
        self._lock = threading.Lock()
        self._running = True
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]

    def start(self):
        threading.Thread(target=self._serve, daemon=True).start()
        return self

    def stop(self):
        self._running = False
        try:
            self._sock.close()
        except Exception:
            pass

    # ------------------------------------------------------------ 服务循环
    def _serve(self):
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(10)
            self._handle_connection(conn)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _handle_connection(self, conn):
        # ---- 1. HTTP 握手 ----
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        key = None
        for line in head.decode("ascii", "replace").split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        if not key:
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_MAGIC).encode("ascii")).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\n"
                      "Upgrade: websocket\r\n"
                      "Connection: Upgrade\r\n"
                      "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode("ascii"))
        buf = rest

        # ---- 帧读写工具 ----
        def read_frame():
            nonlocal buf
            while len(buf) < 2:
                chunk = conn.recv(4096)
                if not chunk:
                    return None
                buf += chunk
            b1, b2 = buf[0], buf[1]
            buf = buf[2:]
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                while len(buf) < 2:
                    buf += conn.recv(4096)
                length = struct.unpack(">H", buf[:2])[0]
                buf = buf[2:]
            elif length == 127:
                while len(buf) < 8:
                    buf += conn.recv(4096)
                length = struct.unpack(">Q", buf[:8])[0]
                buf = buf[8:]
            mask = b""
            if b2 & 0x80:  # 客户端帧必须带掩码
                while len(buf) < 4:
                    buf += conn.recv(4096)
                mask = buf[:4]
                buf = buf[4:]
            while len(buf) < length:
                chunk = conn.recv(4096)
                if not chunk:
                    return None
                buf += chunk
            payload = buf[:length]
            buf = buf[length:]
            if mask:
                payload = bytes(payload[i] ^ mask[i % 4] for i in range(length))
            return opcode, payload

        def send_msg(obj):
            payload = json.dumps(obj).encode("utf-8")
            header = bytearray([0x81])
            n = len(payload)
            if n < 126:
                header.append(n)
            elif n < 65536:
                header.append(126)
                header += struct.pack(">H", n)
            else:
                header.append(127)
                header += struct.pack(">Q", n)
            conn.sendall(bytes(header) + payload)

        # ---- 2. 发送 Hello(带认证挑战) ----
        salt = "mocksalt123456"
        challenge = "mockchallenge1234"
        hello = {"op": 0, "d": {"obsWebSocketVersion": "5.5.6-mock", "rpcVersion": 1}}
        if self.require_auth:
            hello["d"]["authentication"] = {"challenge": challenge, "salt": salt}
        send_msg(hello)

        # ---- 3. 等待 Identify 并校验认证 ----
        identify = None
        while True:
            frame = read_frame()
            if frame is None:
                return
            opcode, payload = frame
            if opcode == 8:
                return
            if opcode != 1:
                continue
            try:
                identify = json.loads(payload.decode("utf-8"))
            except Exception:
                continue
            break
        if not identify or identify.get("op") != 1:
            return
        d = identify.get("d", {})
        if self.require_auth:
            digest = hashlib.sha256(self.password.encode("utf-8") + salt.encode("utf-8")).digest()
            secret = base64.b64encode(digest).decode("ascii")
            expect = base64.b64encode(
                hashlib.sha256(secret.encode("utf-8") + challenge.encode("utf-8")).digest()).decode()
            if d.get("authentication") != expect:
                return  # 认证失败: 直接断开
        with self._lock:
            self.auth_ok_count += 1
        send_msg({"op": 2, "d": {"negotiatedRpcVersion": 1}})

        # ---- 4. 请求处理循环 ----
        while True:
            frame = read_frame()
            if frame is None:
                return
            opcode, payload = frame
            if opcode == 8:
                return
            if opcode != 1:
                continue
            try:
                req = json.loads(payload.decode("utf-8"))
            except Exception:
                continue
            if req.get("op") != 6:
                continue
            rd = req.get("d", {})
            rtype = rd.get("requestType")
            rid = req.get("d", {}).get("requestId")
            with self._lock:
                self.requests.append(rtype)
                if rtype == "GetVersion":
                    data = {"obsStudioVersion": "30.0.0-mock",
                            "obsWebSocketVersion": "5.5.6-mock",
                            "platform": "windows",
                            "availableRequests": ["GetVersion", "StartRecord", "StopRecord",
                                                  "PauseRecord", self.record_request_name]}
                elif rtype == self.record_request_name:
                    data = {"outputActive": self.recording, "outputPaused": False,
                            "outputDuration": 1000, "outputBytes": 12345,
                            "outputTimecode": "00:00:01.000"}
                elif rtype == "StartRecord":
                    self.recording = True
                    self.start_count += 1
                    data = {"outputPath": "C:/mock/video.mp4"}
                elif rtype == "StopRecord":
                    self.recording = False
                    self.stop_count += 1
                    data = {"outputPath": "C:/mock/video.mp4", "outputDuration": 1000}
                else:
                    send_msg({"op": 7, "d": {
                        "requestType": rtype, "requestId": rid,
                        "requestStatus": {"result": False, "code": 204,
                                          "comment": "unknown request type"},
                        "responseData": {}}})
                    continue
            send_msg({"op": 7, "d": {"requestType": rtype, "requestId": rid,
                                     "requestStatus": {"result": True, "code": 100},
                                     "responseData": data}})
