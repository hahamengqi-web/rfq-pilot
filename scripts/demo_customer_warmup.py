"""Warm the exact customer-portal demo question and require grounded citations."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import time
import uuid

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_config
from main import build_customer_agent


PROMPTS = (
    "Archer AX55 有几个 LAN 口？",
    "AX5400Super Wi-Fi 6 如何快速配置上网",
    "AX5400双频Super Wi-Fi 6 无线路由器有什么软件功能",
)


async def main() -> int:
    os.environ.setdefault("NANOCLAW_CUSTOMER_HARNESS_TOOL_TIMEOUT_SECONDS", "45")
    os.environ.setdefault("NANOCLAW_CUSTOMER_HARNESS_TOOL_MAX_ATTEMPTS", "1")
    os.environ.setdefault("NANOCLAW_CUSTOMER_HARNESS_RESPONSE_CACHE_TTL_SECONDS", "86400")
    started = time.monotonic()
    citation_count = 0
    for prompt in PROMPTS:
        agent = build_customer_agent(load_config(), "demo-warmup:" + uuid.uuid4().hex)
        agent.set_request_context({
            "language": "zh",
            "conversation_id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
        })
        try:
            answer = await asyncio.wait_for(agent.run(prompt), timeout=55)
        except Exception as exc:
            print(f"CUSTOMER_WARMUP_FAILED error={type(exc).__name__}")
            return 1
        metadata = agent.consume_response_metadata()
        citations = list(metadata.get("citations") or ())
        # agent.run stores every grounded answer using the harness's current
        # cache schema. Verify the public contract instead of duplicating the
        # private cache-key construction.
        if not answer.strip() or not citations:
            print("CUSTOMER_WARMUP_FAILED error=grounded_answer_unavailable")
            return 1
        citation_count += len(citations)
    print(
        "CUSTOMER_WARMUP_OK "
        f"latency_seconds={time.monotonic() - started:.2f} "
        f"prompts={len(PROMPTS)} citations={citation_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
