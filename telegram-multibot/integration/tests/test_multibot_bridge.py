import http.client
import importlib.util
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class LauncherHandler(QuietHandler):
    def do_GET(self):
        if self.path == "/api/auth/status":
            authenticated = "launcher_session=ok" in (self.headers.get("Cookie") or "")
            body = json.dumps({"authenticated": authenticated}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


class AdapterHandler(QuietHandler):
    calls = []

    def _handle(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        self.__class__.calls.append((self.command, self.path, raw))
        body = json.dumps({"ok": True, "method": self.command}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle


def start_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class MultibotBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.launcher = start_server(LauncherHandler)
        cls.adapter = start_server(AdapterHandler)
        os.environ["LAUNCHER_UPSTREAM"] = f"http://127.0.0.1:{cls.launcher.server_port}"
        os.environ["TELEGRAM_MULTIBOT_UPSTREAM"] = f"http://127.0.0.1:{cls.adapter.server_port}"
        os.environ["AGENT_STATUS_BASE"] = "http://127.0.0.1:9"
        os.environ["PICO_HTTP_UPSTREAM"] = "http://127.0.0.1:9"
        path = Path(__file__).resolve().parents[1] / "launcher_bridge.py"
        spec = importlib.util.spec_from_file_location("launcher_bridge_test", path)
        cls.bridge_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.bridge_module)
        cls.bridge = ThreadingHTTPServer(("127.0.0.1", 0), cls.bridge_module.BridgeHandler)
        cls.thread = threading.Thread(target=cls.bridge.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.bridge, cls.adapter, cls.launcher):
            server.shutdown()
            server.server_close()

    def request(self, method, path, *, auth=True, origin=True, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.bridge.server_port, timeout=3)
        headers = {}
        if auth:
            headers["Cookie"] = "launcher_session=ok"
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{self.bridge.server_port}"
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body).encode()
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response.status, payload

    def test_list_requires_launcher_auth(self):
        self.assertEqual(401, self.request("GET", "/api/telegram-bots", auth=False)[0])
        self.assertEqual(200, self.request("GET", "/api/telegram-bots")[0])

    def test_write_requires_same_origin(self):
        self.assertEqual(
            403,
            self.request("POST", "/api/telegram-bots", origin=False, body={"name": "a"})[0],
        )
        self.assertEqual(
            200,
            self.request("POST", "/api/telegram-bots", body={"name": "a"})[0],
        )

    def test_put_and_delete_route_to_adapter(self):
        self.assertEqual(200, self.request("PUT", "/api/telegram-bots/a", body={"enabled": False})[0])
        self.assertEqual(200, self.request("DELETE", "/api/telegram-bots/a")[0])
        methods = [entry[0] for entry in AdapterHandler.calls]
        self.assertIn("PUT", methods)
        self.assertIn("DELETE", methods)


if __name__ == "__main__":
    unittest.main()
