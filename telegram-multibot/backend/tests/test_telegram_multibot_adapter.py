import asyncio
import importlib.util
import base64
import hashlib
import json
import os
import stat
import struct
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "telegram_multibot_adapter.py"
SPEC = importlib.util.spec_from_file_location("telegram_multibot_adapter", MODULE_PATH)
adapter_module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = adapter_module
SPEC.loader.exec_module(adapter_module)


TOKEN_A = "123456789:abcdefghijklmnopqrstuvwxyzABCDE_12345"
TOKEN_B = "987654321:ABCDEFGHIJKLMNOPQRSTUVWXYZabcde_98765"


class FakeTelegramClient:
    instances = []

    def __init__(self, token, base_url):
        self.token = token
        self.base_url = base_url
        self.messages = []
        self.typing = []
        type(self).instances.append(self)

    async def get_me(self):
        return {"id": 42, "username": "safe_bot", "first_name": "Safe"}

    async def get_updates(self, offset, timeout):
        await asyncio.sleep(3600)

    async def send_message(self, chat_id, text):
        self.messages.append((str(chat_id), text))

    async def send_typing(self, chat_id):
        self.typing.append(str(chat_id))


class FakeStore:
    def __init__(self, bots=None):
        self.bots = bots or []
        self.saved = []

    def load(self):
        return [dict(bot) for bot in self.bots]

    def save(self, bots):
        self.saved = json.loads(json.dumps(bots))


class FailingStore(FakeStore):
    def save(self, bots):
        raise OSError("disk full")


def sample_bot(bot_id="alpha", token=TOKEN_A, enabled=False):
    return {
        "id": bot_id,
        "name": bot_id.title(),
        "token": token,
        "enabled": enabled,
        "allow_from": ["1001"],
        "base_url": "https://api.telegram.org",
    }


class ConfigTests(unittest.TestCase):
    def test_create_payload_can_omit_internal_id(self):
        bot = adapter_module.validate_bot({
            "name": "客服机器人",
            "token": TOKEN_A,
            "enabled": False,
            "allow_from": [],
        })
        self.assertRegex(bot["id"], r"^bot-[a-f0-9]{8}$")

    def test_atomic_round_trip_and_private_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "bots.json")
            store = adapter_module.ConfigStore(path)
            store.save([sample_bot()])
            self.assertEqual(store.load()[0]["token"], TOKEN_A)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertFalse(any(".tmp." in name for name in os.listdir(directory)))

    def test_public_shape_never_contains_token(self):
        output = adapter_module.public_bot(sample_bot(), {"state": "running"})
        encoded = json.dumps(output)
        self.assertNotIn("token\"", encoded)
        self.assertNotIn(TOKEN_A, encoded)
        self.assertTrue(output["token_configured"])
        self.assertEqual(output["state"], "running")

    def test_validation_rejects_bad_token_and_unknown_field(self):
        with self.assertRaises(adapter_module.ValidationError):
            adapter_module.validate_bot({**sample_bot(), "token": "secret"})
        with self.assertRaises(adapter_module.ValidationError):
            adapter_module.validate_bot({**sample_bot(), "password": "x"})

    def test_redaction_hides_bot_token_in_url(self):
        value = adapter_module.redact("https://api.telegram.org/bot%s/getMe" % TOKEN_A, [TOKEN_A])
        self.assertNotIn(TOKEN_A, value)


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeTelegramClient.instances.clear()
        self.store = FakeStore([sample_bot(enabled=True)])
        self.adapter = adapter_module.Adapter(self.store, FakeTelegramClient)
        await self.adapter.start()

    async def asyncTearDown(self):
        await self.adapter.stop()

    async def test_allow_from_variants(self):
        message = {"from": {"id": 1001, "username": "Alice"}}
        self.assertTrue(adapter_module.sender_allowed([], message))
        self.assertTrue(adapter_module.sender_allowed(["telegram:1001"], message))
        self.assertTrue(adapter_module.sender_allowed(["@alice"], message))
        self.assertFalse(adapter_module.sender_allowed(["2002"], message))

    async def test_pico_reply_returns_to_matching_bot_chat(self):
        runtime = self.adapter.runtimes["alpha"]
        session = adapter_module.PicoSession(self.adapter, "alpha", "555")
        session.busy = True
        await self.adapter.handle_pico_message(
            session,
            json.dumps({"type": "message.create", "id": "event-1", "payload": {"content": "完成"}}),
        )
        self.assertEqual(runtime.client.messages, [("555", "完成")])
        self.assertFalse(session.busy)
        # Event IDs are de-duplicated.
        await self.adapter.handle_pico_message(
            session,
            json.dumps({"type": "message.create", "id": "event-1", "payload": {"content": "完成"}}),
        )
        self.assertEqual(len(runtime.client.messages), 1)

    async def test_crud_keeps_token_out_of_response_and_preserves_on_put(self):
        created = await self.adapter.create_bot(sample_bot("beta", TOKEN_B, False))
        self.assertNotIn("token", created)
        updated = await self.adapter.update_bot("beta", {"name": "Beta 2", "allow_from": ["*"]})
        self.assertEqual(updated["name"], "Beta 2")
        self.assertEqual(self.adapter.bots["beta"]["token"], TOKEN_B)
        await self.adapter.delete_bot("beta")
        self.assertNotIn("beta", self.adapter.bots)

    async def test_duplicate_token_is_rejected(self):
        with self.assertRaises(adapter_module.ValidationError):
            await self.adapter.create_bot(sample_bot("beta", TOKEN_A, False))

    async def test_failed_save_does_not_mutate_memory(self):
        adapter = adapter_module.Adapter(FailingStore([sample_bot()]), FakeTelegramClient)
        await adapter.start()
        try:
            with self.assertRaises(OSError):
                await adapter.create_bot(sample_bot("beta", TOKEN_B, False))
            self.assertNotIn("beta", adapter.bots)
        finally:
            await adapter.stop()

    async def test_bot_errors_are_isolated(self):
        other = sample_bot("beta", TOKEN_B, False)
        await self.adapter.create_bot(other)
        self.adapter.runtimes["alpha"].state.last_error = "alpha failed"
        self.assertIn("beta", self.adapter.bots)
        self.assertEqual(self.adapter.status_for("beta")["state"], "disabled")


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = adapter_module.Adapter(FakeStore([sample_bot()]), FakeTelegramClient)
        await self.adapter.start()
        self.server = adapter_module.ApiServer(self.adapter, "127.0.0.1", 0)
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()
        await self.adapter.stop()

    async def request(self, method, path, body=None):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
        payload = b"" if body is None else json.dumps(body).encode()
        request = (
            "%s %s HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: %s\r\nConnection: close\r\n\r\n"
            % (method, path, len(payload))
        ).encode() + payload
        writer.write(request)
        await writer.drain()
        raw = await reader.read()
        writer.close()
        await writer.wait_closed()
        head, data = raw.split(b"\r\n\r\n", 1)
        return int(head.split()[1]), json.loads(data)

    async def test_http_crud_health_and_test(self):
        status, listing = await self.request("GET", "/api/telegram-bots")
        self.assertEqual(status, 200)
        self.assertNotIn(TOKEN_A, json.dumps(listing))
        status, health = await self.request("GET", "/health")
        self.assertEqual((status, health["service"]), (200, "telegram-multibot-adapter"))
        status, tested = await self.request("POST", "/api/telegram-bots/alpha/test", {})
        self.assertEqual(status, 200)
        self.assertEqual(tested["bot"]["username"], "safe_bot")
        status, _ = await self.request("PUT", "/api/telegram-bots/alpha", {"enabled": True})
        self.assertEqual(status, 200)
        status, _ = await self.request("DELETE", "/api/telegram-bots/alpha")
        self.assertEqual(status, 200)


class MiniWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_masked_send_ping_pong_and_fragmented_receive(self):
        observed = {"authorization": None, "text": None, "pong": None}

        async def read_frame(reader):
            first, second = await reader.readexactly(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", await reader.readexactly(2))[0]
            mask = await reader.readexactly(4)
            payload = await reader.readexactly(length)
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            return opcode, payload, bool(second & 0x80)

        def frame(opcode, payload, fin=True):
            return bytes([(0x80 if fin else 0) | opcode, len(payload)]) + payload

        async def handler(reader, writer):
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin1")
            headers = {}
            for line in head.split("\r\n")[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.lower()] = value.strip()
            observed["authorization"] = headers.get("authorization")
            accept = base64.b64encode(
                hashlib.sha1((headers["sec-websocket-key"] + adapter_module.MiniWebSocket.GUID).encode()).digest()
            ).decode()
            writer.write((
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Accept: %s\r\n\r\n" % accept
            ).encode())
            await writer.drain()
            opcode, payload, masked = await read_frame(reader)
            observed["text"] = (opcode, payload.decode(), masked)
            writer.write(frame(0x9, b"hi") + frame(0x1, "你".encode(), False) + frame(0x0, "好".encode(), True))
            await writer.drain()
            opcode, payload, masked = await read_frame(reader)
            observed["pong"] = (opcode, payload, masked)
            await asyncio.sleep(0.05)
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            websocket = await adapter_module.ws_connect(
                "ws://127.0.0.1:%s/pico/ws?session_id=test" % port,
                extra_headers={"Authorization": "Bearer local-secret"},
            )
            await websocket.send("task")
            self.assertEqual(await websocket.recv(), "你好")
            await asyncio.sleep(0.1)
            self.assertEqual(observed["authorization"], "Bearer local-secret")
            self.assertEqual(observed["text"], (1, "task", True))
            self.assertEqual(observed["pong"], (10, b"hi", True))
            await websocket.close()
        finally:
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
