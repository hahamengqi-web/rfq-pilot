import asyncio
import os
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "manager"))

from gateway_manager import get_gateway


class ManagerGatewayStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_starts_web_channel(self):
        gateway = get_gateway()
        def free_port():
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                return listener.getsockname()[1]

        workspace_port, customer_port = free_port(), free_port()
        environment = {
            "NANOCLAW_WORKSPACE_WEB_ENABLED": "true",
            "NANOCLAW_WORKSPACE_WEB_HOST": "127.0.0.1",
            "NANOCLAW_WORKSPACE_WEB_PORT": str(workspace_port),
            "NANOCLAW_CUSTOMER_PORTAL_ENABLED": "true",
            "NANOCLAW_CUSTOMER_PORTAL_HOST": "127.0.0.1",
            "NANOCLAW_CUSTOMER_PORTAL_PORT": str(customer_port),
            "NANOCLAW_FEISHU_APP_ID": "",
            "NANOCLAW_FEISHU_APP_SECRET": "",
            "NANOCLAW_QQ_APP_ID": "",
            "NANOCLAW_QQ_APP_SECRET": "",
            "NANOCLAW_EMAIL_MANAGED_ACCOUNTS_ENABLED": "false",
        }
        with patch.dict(os.environ, environment, clear=False):
            try:
                message = await gateway.start()
                self.assertIn("已启动", message)
                ready = False
                for _ in range(160):
                    await asyncio.sleep(.25)
                    try:
                        reader, writer = await asyncio.open_connection("127.0.0.1", workspace_port)
                        writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                        await writer.drain(); data = await reader.read(512)
                        writer.close(); await writer.wait_closed()
                        ready = b"200 OK" in data
                        if ready: break
                    except OSError:
                        continue
                if not ready:
                    self.fail("Workspace channel did not become ready. Logs:\n" + "\n".join(gateway.get_status()["logs"]))
            finally:
                await gateway.stop()


if __name__ == "__main__": unittest.main()
