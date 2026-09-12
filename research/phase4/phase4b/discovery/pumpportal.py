"""
PumpPortal Discovery - Real-time token discovery via PumpPortal WebSocket.

Implements the primary discovery source for Phase 4B.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import websockets
from typing import Optional, Callable, Dict, Any

from .models import (
    DiscoveryEvent,
    Candidate,
    DiscoverySource,
    DiscoveryQueue,
)

logger = logging.getLogger(__name__)


class PumpPortalDiscovery:
    """
    PumpPortal WebSocket discovery client.
    
    Connects to PumpPortal's subscribeNewToken stream and emits DiscoveryEvents.
    Handles reconnection, deduplication, and graceful degradation.
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        ws_url: Optional[str] = None,
        queue: Optional["DiscoveryQueue"] = None,
        on_event: Optional[Callable[[DiscoveryEvent], None]] = None,
        max_reconnect_attempts: int = 10,
        base_reconnect_delay: float = 5.0,
        max_reconnect_delay: float = 300.0,
    ):
        self.api_key = api_key or os.environ.get("PUMPPORTAL_API_KEY")
        self.ws_url = ws_url or "wss://pumpportal.fun/api/data"
        if self.api_key:
            self.ws_url += f"?api-key={self.api_key}"
        
        self.queue = queue
        self.on_event = on_event
        
        # Reconnection config
        self.max_reconnect_attempts = max_reconnect_attempts
        self.base_reconnect_delay = base_reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        
        # State
        self._ws: Optional[websockets.ClientConnection] = None
        self._running = False
        self._reconnect_count = 0
        self._last_message_time = 0.0
        self._message_count = 0
        self._duplicate_count = 0
        self._malformed_count = 0
        
        # Subscription message
        self._subscribe_msg = json.dumps({"method": "subscribeNewToken"})
        
        # Liveness
        self._heartbeat_interval = 30.0  # seconds
        self._last_ping_time = 0.0
    
    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.state == websockets.protocol.State.OPEN
    
    @property
    def stats(self) -> dict:
        return {
            "connected": self.is_connected,
            "reconnect_count": self._reconnect_count,
            "message_count": self._message_count,
            "duplicate_count": self._duplicate_count,
            "malformed_count": self._malformed_count,
            "last_message_age_sec": time.time() - self._last_message_time if self._last_message_time else None,
        }
    
    async def start(self) -> None:
        """Start the discovery loop."""
        self._running = True
        self._reconnect_count = 0
        
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Discovery loop error: {e}")
            
            if not self._running:
                break
            
            # Reconnection logic
            if self._reconnect_count >= self.max_reconnect_attempts:
                logger.error(f"Max reconnect attempts ({self.max_reconnect_attempts}) reached. Stopping.")
                break
            
            delay = min(
                self.base_reconnect_delay * (2 ** self._reconnect_count),
                self.max_reconnect_delay
            )
            logger.info(f"Reconnecting in {delay:.1f}s (attempt {self._reconnect_count + 1}/{self.max_reconnect_attempts})")
            await asyncio.sleep(delay)
            self._reconnect_count += 1
    
    async def shutdown(self) -> None:
        """Stop the discovery loop."""
        self._running = False
        if self._ws and self._ws.state == websockets.protocol.State.OPEN:
            await self._ws.close()
    
    async def _connect_and_listen(self) -> None:
        """Connect to WebSocket and listen for messages."""
        logger.info(f"Connecting to PumpPortal: {self.ws_url}")
        
        async with websockets.connect(
            self.ws_url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=10,
        ) as ws:
            self._ws = ws
            self._reconnect_count = 0  # Reset on successful connection
            
            # Subscribe
            await ws.send(self._subscribe_msg)
            logger.info("Subscribed to new token events")
            
            # Listen loop
            async for message in ws:
                if not self._running:
                    break
                
                await self._process_message(message)
                
                # Heartbeat check
                now = time.time()
                if now - self._last_message_time > 60:  # No messages for 60s
                    logger.warning("No messages for 60s, checking connection...")
                    try:
                        pong = await ws.ping()
                        await asyncio.wait_for(pong, timeout=10)
                    except Exception:
                        logger.warning("Ping failed, connection may be stale")
                        break
    
    async def _process_message(self, message) -> None:
        """Process incoming WebSocket message."""
        self._last_message_time = time.time()
        self._message_count += 1
        
        try:
            if isinstance(message, bytes):
                message_str = message.decode('utf-8', errors='ignore')
            else:
                message_str = str(message)
            data = json.loads(message_str)
        except json.JSONDecodeError as e:
            self._malformed_count += 1
            logger.warning(f"Malformed message: {e}")
            return
        
        # Handle subscription confirmation
        if isinstance(data, dict) and data.get("method") == "subscribeNewToken":
            logger.info(f"Subscription confirmed: {data}")
            return
        
        # Handle new token event
        if isinstance(data, dict) and data.get("mint"):
            event = self._parse_token_event(data)
            if event:
                await self._handle_event(event)
            return
        
        # Other message types
        logger.debug(f"Other message: {data}")
    
    def _parse_token_event(self, data: dict) -> Optional[DiscoveryEvent]:
        """Parse PumpPortal new token event into DiscoveryEvent."""
        try:
            mint = data.get("mint", "")
            if not mint:
                return None
            
            now = time.time()
            
            # Extract fields
            event = DiscoveryEvent(
                mint=mint,
                source=DiscoverySource.PUMPPORTAL,
                event_timestamp=now,  # PumpPortal doesn't provide on-chain timestamp
                receive_timestamp=now,
                symbol=data.get("symbol"),
                name=data.get("name"),
                creator=data.get("creator"),
                uri=data.get("uri"),
                bonding_curve_key=data.get("bondingCurveKey"),
                initial_buy=data.get("initialBuy"),
                sol_amount=data.get("solAmount"),
                v_tokens_in_bonding_curve=data.get("vTokensInBondingCurve"),
                v_sol_in_bonding_curve=data.get("vSolInBondingCurve"),
                market_cap_sol=data.get("marketCapSol"),
                is_mayhem_mode=data.get("is_mayhem_mode"),
                pool=data.get("pool"),
                signature=data.get("signature"),
                raw_payload=data,
            )
            return event
        except Exception as e:
            logger.warning(f"Failed to parse token event: {e}")
            return None
    
    async def _handle_event(self, event: DiscoveryEvent) -> None:
        """Handle a parsed discovery event."""
        # Add to queue if available
        if self.queue:
            candidate = self.queue.add_or_update(event)
            if candidate.duplicate_count > 1:
                self._duplicate_count += 1
        
        # Call callback if provided
        if self.on_event:
            try:
                self.on_event(event)
            except Exception as e:
                logger.error(f"Event callback error: {e}")
    
    def get_stats(self) -> dict:
        return {
            "connected": self.is_connected,
            "reconnect_count": self._reconnect_count,
            "message_count": self._message_count,
            "duplicate_count": self._duplicate_count,
            "malformed_count": self._malformed_count,
            "last_message_age_sec": time.time() - self._last_message_time if self._last_message_time else None,
            "queue_stats": self.queue.stats() if self.queue else None,
        }


class SolanaPublicRPCDiscovery:
    """
    Fallback discovery using Solana public RPC logsSubscribe.
    
    Only works with api.mainnet-beta.solana.com among free endpoints.
    """
    
    def __init__(
        self,
        rpc_url: str = "wss://api.mainnet-beta.solana.com",
        program_id: str = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        queue: Optional["DiscoveryQueue"] = None,
        on_event: Optional[Callable[[DiscoveryEvent], None]] = None,
    ):
        self.rpc_url = rpc_url
        self.program_id = program_id
        self.queue = queue
        self.on_event = on_event
        
        self._ws: Optional[websockets.ClientConnection] = None
        self._running = False
        self._subscription_id: Optional[int] = None
        
        self._message_count = 0
        self._parse_errors = 0
    
    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.state == websockets.protocol.State.OPEN
    
    async def start(self) -> None:
        """Start the discovery loop."""
        self._running = True
        
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Public RPC discovery error: {e}")
            
            if not self._running:
                break
            
            await asyncio.sleep(5)
    
    async def shutdown(self) -> None:
        """Stop the discovery loop."""
        self._running = False
        if self._ws and self._ws.state == websockets.protocol.State.OPEN:
            await self._ws.close()
    
    async def _connect_and_listen(self) -> None:
        logger.info(f"Connecting to Solana public RPC: {self.rpc_url}")
        
        async with websockets.connect(
            self.rpc_url,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            self._ws = ws
            
            # Subscribe to Pump.fun program logs
            subscribe_msg = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [self.program_id]},
                    {"commitment": "processed"}
                ]
            }
            
            await ws.send(json.dumps(subscribe_msg))
            response = await ws.recv()
            resp_data = json.loads(response)
            
            if "result" in resp_data:
                self._subscription_id = resp_data["result"]
                logger.info(f"Subscribed to logs (id={self._subscription_id})")
            else:
                logger.error(f"Subscription failed: {resp_data}")
                return
            
            # Listen loop
            async for message in ws:
                if not self._running:
                    break
                
                await self._process_message(message)
    
    async def _process_message(self, message) -> None:
        """Process log notification."""
        self._message_count += 1
        
        try:
            if isinstance(message, bytes):
                message_str = message.decode('utf-8', errors='ignore')
            else:
                message_str = str(message)
            data = json.loads(message_str)
        except json.JSONDecodeError:
            self._parse_errors += 1
            return
        
        # Check if it's a log notification
        if not isinstance(data, dict):
            return
        
        params = data.get("params", {})
        result = params.get("result", {})
        logs = result.get("value", {}).get("logs", [])
        
        if not logs:
            return
        
        # Parse for Create instruction
        event = self._parse_logs(logs)
        if event:
            await self._handle_event(event)
    
    def _parse_logs(self, logs: list) -> Optional[DiscoveryEvent]:
        """Parse program logs for token creation."""
        try:
            # Look for "Instruction: Create" and subsequent data
            create_idx = -1
            for i, log in enumerate(logs):
                if "Instruction: Create" in log:
                    create_idx = i
                    break
            
            if create_idx == -1:
                return None
            
            # Extract mint from subsequent logs
            mint = None
            for log in logs[create_idx + 1:]:
                if "mint:" in log.lower():
                    parts = log.split()
                    for part in parts:
                        if len(part) >= 32 and part.endswith("pump"):  # Pump.fun mint pattern
                            mint = part
                            break
                    if mint:
                        break
            
            if not mint:
                return None
            
            now = time.time()
            return DiscoveryEvent(
                mint=mint,
                source=DiscoverySource.SOLANA_PUBLIC_RPC,
                event_timestamp=now,
                receive_timestamp=now,
                raw_payload={"logs": logs},
            )
        except Exception as e:
            logger.warning(f"Failed to parse logs: {e}")
            return None
    
    async def _handle_event(self, event: DiscoveryEvent) -> None:
        if self.queue:
            self.queue.add_or_update(event)
        if self.on_event:
            try:
                self.on_event(event)
            except Exception as e:
                logger.error(f"Event callback error: {e}")
    
    async def shutdown(self) -> None:
        self._running = False
        if self._ws:
            # Check if websocket is closed (works for both ClientWebSocketResponse and ClientConnection)
            ws_closed = getattr(self._ws, 'closed', False)
            if not ws_closed:
                # Unsubscribe
                if self._subscription_id is not None:
                    try:
                        unsub_msg = {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "logsUnsubscribe",
                            "params": [self._subscription_id]
                        }
                        await self._ws.send(json.dumps(unsub_msg))
                    except Exception:
                        pass
                try:
                    await self._ws.close()
                except Exception:
                    pass


class DiscoveryManager:
    """
    High-level discovery manager coordinating primary and fallback sources.
    """
    
    def __init__(
        self,
        queue: DiscoveryQueue,
        pumpportal_api_key: Optional[str] = None,
        enable_fallback: bool = True,
    ):
        self.queue = queue
        self.enable_fallback = enable_fallback
        
        # Primary: PumpPortal
        self.pumpportal = PumpPortalDiscovery(
            api_key=pumpportal_api_key,
            queue=queue,
        )
        
        # Fallback: Public RPC
        self.fallback = None
        if enable_fallback:
            self.fallback = SolanaPublicRPCDiscovery(queue=queue)
        
        self._running = False
        self._tasks: list = []
    
    async def start(self) -> None:
        """Start all discovery sources."""
        self._running = True
        
        # Start primary
        self._tasks.append(asyncio.create_task(self.pumpportal.start()))
        
        # Start fallback if enabled
        if self.fallback:
            self._tasks.append(asyncio.create_task(self.fallback.start()))
        
        logger.info("Discovery manager started")
    
    async def shutdown(self) -> None:
        """Stop all discovery sources."""
        self._running = False
        
        await self.pumpportal.shutdown()
        if self.fallback:
            await self.fallback.shutdown()
        
        # Cancel tasks
        for task in self._tasks:
            task.cancel()
        
        # Wait for tasks
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        
        logger.info("Discovery manager stopped")
    
    def get_stats(self) -> dict:
        stats = {
            "pumpportal": self.pumpportal.get_stats(),
        }
        if self.fallback:
            stats["fallback"] = {
                "connected": self.fallback.is_connected,
                "message_count": self.fallback._message_count,
                "parse_errors": self.fallback._parse_errors,
            }
        return stats