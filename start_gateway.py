#!/usr/bin/env python3
"""
Startup wrapper for the Hermes gateway with Crustocean adapter.
Generic template — agent name derived from CRUSTOCEAN_HANDLE env var.
"""

import logging
import os
import sys
import traceback

sys.path.insert(0, "/app/hermes-agent")
os.chdir("/app/hermes-agent")

agent_name = os.getenv("CRUSTOCEAN_HANDLE", "hermes-agent")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)

print(f"[{agent_name}] Starting gateway...", flush=True)

try:
    from gateway.run import start_gateway
    print(f"[{agent_name}] Gateway module loaded", flush=True)
except Exception:
    print(f"[{agent_name}] FATAL: Failed to import gateway:", flush=True)
    traceback.print_exc()
    sys.exit(1)

try:
    import asyncio
    success = asyncio.run(start_gateway())
    if not success:
        print(f"[{agent_name}] Gateway returned failure — exiting", flush=True)
        sys.exit(1)
except KeyboardInterrupt:
    print(f"[{agent_name}] Shutting down", flush=True)
except Exception:
    print(f"[{agent_name}] FATAL: Gateway crashed:", flush=True)
    traceback.print_exc()
    sys.exit(1)
