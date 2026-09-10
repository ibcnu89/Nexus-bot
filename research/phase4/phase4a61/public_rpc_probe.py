#!/usr/bin/env python3
"""
Public Solana RPC Probe - Test free public RPC as fallback discovery source.

Tests whether standard public Solana RPC/WebSocket can be used for:
- Pump.fun program log subscription
- Basic RPC methods
- Rate limits and disconnect behavior
"""

import asyncio
import json
import time
import websockets
import aiohttp
from dataclasses import dataclass, asdict
from typing import Optional
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Pump.fun program ID
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Public Solana RPC endpoints (free, no auth)
PUBLIC_RPC_ENDPOINTS = [
    "wss://api.mainnet-beta.solana.com",
    "wss://solana-mainnet.g.alchemy.com/v2/demo",  # Alchemy demo
    "wss://rpc.ankr.com/solana_ws",  # Ankr
]

PUBLIC_HTTP_ENDPOINTS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-mainnet.g.alchemy.com/v2/demo",
    "https://rpc.ankr.com/solana",
]

@dataclass
class PublicRPCResult:
    """Results from public RPC probe."""
    endpoint: str
    ws_supported: bool
    ws_connect_time_ms: float
    logs_subscribe_supported: bool
    logs_subscribe_time_ms: float
    messages_received: int
    disconnect_after_seconds: Optional[float]
    http_supported: bool
    http_get_health_ms: float
    http_get_slot_ms: float
    http_get_signatures_ms: float
    rate_limit_observed: bool
    notes: str

class PublicRPCProbe:
    """Probe public Solana RPC endpoints."""
    
    def __init__(self, ws_url: str, http_url: str, duration_seconds: int = 60):
        self.ws_url = ws_url
        self.http_url = http_url
        self.duration_seconds = duration_seconds
        
    async def probe(self) -> PublicRPCResult:
        """Run full probe on an endpoint."""
        logger.info(f"Probing {self.ws_url} / {self.http_url}")
        
        result = PublicRPCResult(
            endpoint=self.ws_url,
            ws_supported=False,
            ws_connect_time_ms=0,
            logs_subscribe_supported=False,
            logs_subscribe_time_ms=0,
            messages_received=0,
            disconnect_after_seconds=None,
            http_supported=False,
            http_get_health_ms=0,
            http_get_slot_ms=0,
            http_get_signatures_ms=0,
            rate_limit_observed=False,
            notes=""
        )
        
        # Test WebSocket
        await self._test_websocket(result)
        
        # Test HTTP
        await self._test_http(result)
        
        return result
    
    async def _test_websocket(self, result: PublicRPCResult):
        """Test WebSocket connection and logsSubscribe."""
        subscription_msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [PUMP_FUN_PROGRAM]},
                {"commitment": "processed"}
            ]
        }
        
        try:
            start = time.time()
            async with websockets.connect(self.ws_url, ping_interval=10, ping_timeout=5) as ws:
                result.ws_supported = True
                result.ws_connect_time_ms = (time.time() - start) * 1000
                
                # Subscribe
                start = time.time()
                await ws.send(json.dumps(subscription_msg))
                response = await asyncio.wait_for(ws.recv(), timeout=10)
                result.logs_subscribe_time_ms = (time.time() - start) * 1000
                
                try:
                    resp_data = json.loads(response)
                    if "result" in resp_data:
                        result.logs_subscribe_supported = True
                        logger.info(f"logsSubscribe successful: {resp_data}")
                    elif "error" in resp_data:
                        result.logs_subscribe_supported = False
                        result.notes += f" logsSubscribe error: {resp_data['error']}"
                except json.JSONDecodeError:
                    result.notes += " logsSubscribe response not JSON"
                
                # Listen for messages
                start_listen = time.time()
                while time.time() - start_listen < self.duration_seconds:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=5)
                        result.messages_received += 1
                    except asyncio.TimeoutError:
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        result.disconnect_after_seconds = time.time() - start_listen
                        break
                        
        except Exception as e:
            result.notes += f" WS error: {str(e)[:100]}"
            logger.warning(f"WebSocket test failed for {self.ws_url}: {e}")
    
    async def _test_http(self, result: PublicRPCResult):
        """Test HTTP RPC methods."""
        async with aiohttp.ClientSession() as session:
            # Test getHealth
            try:
                start = time.time()
                async with session.post(self.http_url, json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("result") == "ok":
                            result.http_supported = True
                    result.http_get_health_ms = (time.time() - start) * 1000
            except Exception as e:
                result.notes += f" getHealth error: {str(e)[:50]}"
            
            # Test getSlot
            try:
                start = time.time()
                async with session.post(self.http_url, json={"jsonrpc": "2.0", "id": 1, "method": "getSlot"}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if "result" in data:
                            pass  # Success
                result.http_get_slot_ms = (time.time() - start) * 1000
            except Exception as e:
                result.notes += f" getSlot error: {str(e)[:50]}"
            
            # Test getSignaturesForAddress (Pump.fun program)
            try:
                start = time.time()
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [PUMP_FUN_PROGRAM, {"limit": 10}]
                }
                async with session.post(self.http_url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if "result" in data and isinstance(data["result"], list):
                            pass  # Success
                result.http_get_signatures_ms = (time.time() - start) * 1000
            except Exception as e:
                result.notes += f" getSignatures error: {str(e)[:50]}"
            
            # Quick rate limit test - 10 rapid requests
            try:
                tasks = []
                for i in range(10):
                    payload = {"jsonrpc": "2.0", "id": i, "method": "getSlot"}
                    tasks.append(session.post(self.http_url, json=payload))
                start = time.time()
                responses = await asyncio.gather(*tasks, return_exceptions=True)
                elapsed = time.time() - start
                errors = 0
                for r in responses:
                    if isinstance(r, Exception):
                        errors += 1
                    else:
                        try:
                            if getattr(r, 'status', None) == 429:
                                errors += 1
                        except:
                            pass
                if errors > 0:
                    result.rate_limit_observed = True
                    result.notes += f" Rate limit: {errors}/10 errors"
            except Exception:
                pass

async def main():
    """Test all public endpoints."""
    all_results = []
    
    for ws_url, http_url in zip(PUBLIC_RPC_ENDPOINTS, PUBLIC_HTTP_ENDPOINTS):
        probe = PublicRPCProbe(ws_url, http_url, duration_seconds=30)
        result = await probe.probe()
        all_results.append(result)
        logger.info(f"Result for {ws_url}: WS={result.ws_supported}, logsSub={result.logs_subscribe_supported}, HTTP={result.http_supported}, msgs={result.messages_received}")
    
    # Save results
    output_path = "/home/ibcnu/PowerTraderAI/research/phase4/phase4a61/public_rpc_results.json"
    with open(output_path, "w") as f:
        json.dump([asdict(r) for r in all_results], f, indent=2)
    
    logger.info(f"Results saved to {output_path}")
    
    # Summary
    for r in all_results:
        logger.info(f"\n=== {r.endpoint} ===")
        logger.info(f"  WS: {r.ws_supported} ({r.ws_connect_time_ms:.0f}ms)")
        logger.info(f"  logsSubscribe: {r.logs_subscribe_supported} ({r.logs_subscribe_time_ms:.0f}ms)")
        logger.info(f"  Messages: {r.messages_received}")
        logger.info(f"  Disconnect: {r.disconnect_after_seconds}s")
        logger.info(f"  HTTP: {r.http_supported} (health: {r.http_get_health_ms:.0f}ms, slot: {r.http_get_slot_ms:.0f}ms, sigs: {r.http_get_signatures_ms:.0f}ms)")
        logger.info(f"  Rate limited: {r.rate_limit_observed}")
        logger.info(f"  Notes: {r.notes}")

if __name__ == "__main__":
    asyncio.run(main())