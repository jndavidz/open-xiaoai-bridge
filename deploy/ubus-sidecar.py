#!/usr/bin/env python3
"""票 03 验证期侧车（NAS 运行）：ssh LX06 `ubus listen` → 广播给 127.0.0.1:9100 的连接方。

bridge（host 网络）以 native_cmd='bash -c "exec 3<>/dev/tcp/127.0.0.1/9100; cat <&3"' 消费。
ssh 断开 3s 退避重连；已验 `ubus listen` 对管道自带行缓冲（2026-10-25 探针）。
启动：nohup python3 ubus-sidecar.py >> /tmp/ubus-sidecar.log 2>&1 &
"""
import socket
import subprocess
import threading
import time

SSH_CMD = [
    "ssh", "-o", "BatchMode=yes",
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
    "-i", "/var/services/homes/zxsadmin/.ssh/lx06-ir/id_rsa",
    "root@10.10.10.20", "ubus listen",
]
clients = []
lock = threading.Lock()


def broadcast(line: bytes) -> None:
    with lock:
        dead = []
        for c in clients:
            try:
                c.sendall(line)
            except Exception:
                dead.append(c)
        for c in dead:
            clients.remove(c)


def pump():
    while True:
        proc = None
        try:
            proc = subprocess.Popen(SSH_CMD, stdout=subprocess.PIPE)
            assert proc.stdout is not None
            for line in proc.stdout:
                broadcast(line)
        except Exception as exc:
            print(f"pump error: {exc}", flush=True)
        finally:
            if proc is not None:
                proc.kill()
        print("ssh 断开，3s 后重连", flush=True)
        time.sleep(3)


def server():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 9100))
    srv.listen(2)
    while True:
        conn, addr = srv.accept()
        with lock:
            clients.append(conn)
        print(f"client connected: {addr}", flush=True)


threading.Thread(target=pump, daemon=True).start()
server()
