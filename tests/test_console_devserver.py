import asyncio
import json
import unittest

import helpers  # noqa: F401  (sets sys.path)

from orchestrator.console.devserver import DevServer, build_stack


async def http(port, method, path, body=None, headers=None, read_bytes=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    payload = json.dumps(body).encode() if body is not None else b""
    head = f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(payload)}\r\n"
    for k, v in (headers or {}).items():
        head += f"{k}: {v}\r\n"
    writer.write(head.encode() + b"\r\n" + payload)
    await writer.drain()
    if read_bytes:
        data = b""
        while b"event: decision" not in data and len(data) < read_bytes:
            chunk = await reader.read(1024)
            if not chunk:
                break
            data += chunk
    else:
        data = await reader.read()
    writer.close()
    status = int(data.split(b" ", 2)[1])
    return status, data.split(b"\r\n\r\n", 1)[1]


class DevServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.console, self.traffic, self.workflows = await build_stack(traffic_per_min=0, seed=3)
        self.traffic.rng.seed(3)
        for f in self.traffic.service.c.executor._a2a._transport.faults.values():  # noqa: SLF001 - make the test fast
            f.latency_ms = (0, 1)
            f.failure_rate = 0.0
        self.server = await asyncio.start_server(DevServer(self.console).handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def test_traffic_and_transactions_flow(self):
        for _ in range(40):
            await self.traffic.one_turn()
        snap = self.console.telemetry.snapshot(300)
        self.assertEqual(snap["turns"], 40)
        self.assertGreater(snap["types"].get("answer", 0), 10)
        tx = [wf for wf in self.workflows.runs.values()]
        self.assertTrue(tx, "the traffic mix should include trades")

    async def test_http_api_and_stream(self):
        status, body = await http(self.port, "GET", "/admin/cc/me")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["chaos"])
        status, body = await http(self.port, "PUT", "/admin/cc/chaos", {"agents": {"market-agent": {"failure_rate": 0.5}}, "traffic_per_min": 30})
        self.assertEqual((status, json.loads(body)["agents"]["market-agent"]["failure_rate"]), (200, 0.5))
        status, body = await http(self.port, "PUT", "/admin/cc/kill-switch", {"disabled_intents": ["trade.sell"], "reason": "test"}, {"X-Operator-Roles": "viewer"})
        self.assertEqual(status, 403)
        status, _ = await http(self.port, "GET", "/console")
        self.assertEqual(status, 200)
        status, _ = await http(self.port, "GET", "/nope")
        self.assertEqual(status, 404)

        task = asyncio.create_task(http(self.port, "GET", "/admin/cc/stream", read_bytes=4096))
        await asyncio.sleep(0.1)
        await self.traffic.one_turn()
        status, chunk = await asyncio.wait_for(task, 2)
        self.assertEqual(status, 200)
        self.assertIn(b"event: decision", chunk)


if __name__ == "__main__":
    unittest.main()
