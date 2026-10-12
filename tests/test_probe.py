"""健康检查（12.2）与采集接口调用的各类失败：用本机真实的 HTTP/TCP 服务测试 backend 中的实现。"""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend import AgentCallFailed, Backend


class Handler(BaseHTTPRequestHandler):
    routes = {}

    def _reply(self):
        status, body, delay = self.routes.get(self.path, (404, b"not found", 0))
        if delay:
            time.sleep(delay)
        try:
            self.send_response(status)
            if status in (301, 302):
                self.send_header("Location", "/elsewhere")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # 客户端已超时断开
            pass

    do_GET = do_POST = _reply

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    Handler.routes = {"/probe": (200, json.dumps({"status": "Probe successful"}).encode(), 0),
                      "/": (200, b"<html>novnc</html>", 0)}
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture
def listening_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    yield sock.getsockname()[1]
    sock.close()


def closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def probe(port, vnc=None, rdp=None, path="/probe", timeout=1):
    return Backend().probe(port, path, vnc, rdp, timeout)


def test_probe_success(server, listening_port):
    assert probe(server, vnc=server, rdp=listening_port) is True
    assert probe(server) is True  # 未开放 VNC/RDP 时不检查


def test_probe_ignores_proxy_env(server, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert probe(server) is True


@pytest.mark.parametrize("route", [
    (500, json.dumps({"status": "Probe successful"}).encode(), 0),  # 非 200
    (200, b"not json", 0),  # 不是 JSON
    (200, json.dumps({"status": "starting"}).encode(), 0),  # status 不符
    (200, json.dumps(["Probe successful"]).encode(), 0),  # JSON 但不是对象
    (200, json.dumps({"status": "Probe successful"}).encode(), 2),  # 超时
])
def test_probe_agent_failures(server, route):
    Handler.routes["/probe"] = route
    assert probe(server) is False


def test_probe_wrong_path_and_refused(server):
    assert probe(server, path="/no-such-path") is False
    assert probe(closed_port()) is False


def test_probe_vnc_status(server):
    Handler.routes["/"] = (500, b"", 0)
    assert probe(server, vnc=server) is False
    Handler.routes["/"] = (302, b"", 0)
    assert probe(server, vnc=server) is True
    assert probe(server, vnc=closed_port()) is False


def test_probe_rdp_closed(server):
    assert probe(server, rdp=closed_port()) is False


def test_agent_post(server):
    Handler.routes["/recording/start"] = (200, b"{}", 0)
    Backend().agent_post(server, "/recording/start", {"recording_id": "rec_1"}, 1)
    with pytest.raises(AgentCallFailed):
        Backend().agent_post(server, "/recording/stop", {"recording_id": "rec_1"}, 1)  # 404
    Handler.routes["/recording/stop"] = (200, b"{}", 2)
    with pytest.raises(AgentCallFailed):
        Backend().agent_post(server, "/recording/stop", {"recording_id": "rec_1"}, 1)  # 超时
    with pytest.raises(AgentCallFailed):
        Backend().agent_post(closed_port(), "/recording/stop", {}, 1)
