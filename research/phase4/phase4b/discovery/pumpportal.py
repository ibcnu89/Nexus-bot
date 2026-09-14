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
        db_path: Optional[str] = None,
    ):
        self.api_key = api_key or os.environ.get("PUMPPORTAL_API_KEY")
        self.ws_url = ws_url or "wss://pumpportal.fun/api/data"
        if self.api_key:
            self.ws_url += f"?api-key={self.api_key}"
        
        self.queue = queue
        self.on_event = on_event
        self.db_path = db_path
        
        # Reconnection config
        self.max_reconnect_attempts = max_reconnect_attempts
        self.base_reconnect_delay = base_reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        
        # State
        self._ws: Optional[websockets.ClientConnection] = None
        self._running = False
        self._reconnect_count = 0
        self._reconnect_attempts_total = 0
        self._disconnect_count = 0
        self._last_message_time = 0.0
        self._message_count = 0
        self._valid_discovery_count = 0
        self._persisted_count = 0
        self._duplicate_count = 0
        self._malformed_count = 0
        self._subscription_count = 0
        self._other_message_count = 0
        self._persistence_failure_count = 0
        
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
            "reconnect_count": self._reconnect_attempts_total,
            "disconnect_count": self._disconnect_count,
            "message_count": self._message_count,
            "valid_discovery_events": self._valid_discovery_count,
            "unique_mints": self._valid_discovery_count - self._duplicate_count,
            "duplicate_count": self._duplicate_count,
            "malformed_count": self._malformed_count,
            "subscription_count": self._subscription_count,
            "other_message_count": self._other_message_count,
            "persistence_failure_count": self._persistence_failure_count,
            "last_message_age_sec": time.time() - self._last_message_time if self._last_message_time else None,
            "discovery_events_persisted": self._persisted_count,
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
                if self._ws is not None:
                    self._disconnect_count += 1
                    self._ws = None
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
            self._reconnect_attempts_total += 1
    
    async def shutdown(self) -> None:
        """Stop the discovery loop."""
        self._running = False
        if self._ws:
            # Check if websocket is closed (works for both ClientWebSocketResponse and ClientConnection)
            ws_closed = getattr(self._ws, 'closed', False)
            if not ws_closed:
                try:
                    await self._ws.close()
                except Exception:
                    pass
    
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

        if self._running:
            self._disconnect_count += 1
        self._ws = None
    
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
            self._subscription_count += 1
            logger.info(f"Subscription confirmed: {data}")
            return
        
        # Handle new token event
        if isinstance(data, dict) and data.get("mint"):
            event = self._parse_token_event(data)
            if event:
                await self._handle_event(event)
            return
        
        # Other message types
        self._other_message_count += 1
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
        self._valid_discovery_count += 1
        # Add to queue if available
        if self.queue:
            was_duplicate = self.queue.get_candidate(event.mint) is not None
            candidate = self.queue.add_or_update(event)
            if was_duplicate:
                self._duplicate_count += 1
        
        # Persist discovery event to database
        if self.db_path:
            if await self._persist_discovery_event(event):
                self._persisted_count += 1
            else:
                self._persistence_failure_count += 1
        
        # Call callback if provided
        if self.on_event:
            try:
                self.on_event(event)
            except Exception as e:
                logger.error(f"Event callback error: {e}")
    
    async def _persist_discovery_event(self, event: DiscoveryEvent) -> bool:
        """Persist raw discovery event to database."""
        conn = None
        try:
            import sqlite3
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                INSERT INTO discovery_events
                (mint, source, event_timestamp, receive_timestamp, symbol, name,
                 creator, uri, bonding_curve_key, initial_buy, sol_amount,
                 v_tokens_in_bonding_curve, v_sol_in_bonding_curve, market_cap_sol,
                 is_mayhem_mode, pool, signature, raw_payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event.mint,
                event.source.value,
                event.event_timestamp,
                event.receive_timestamp,
                event.symbol,
                event.name,
                event.creator,
                event.uri,
                event.bonding_curve_key,
                event.initial_buy,
                event.sol_amount,
                event.v_tokens_in_bonding_curve,
                event.v_sol_in_bonding_curve,
                event.market_cap_sol,
                1 if event.is_mayhem_mode else 0 if event.is_mayhem_mode is not None else None,
                event.pool,
                event.signature,
                json.dumps(event.raw_payload) if event.raw_payload else None,
                time.time()
            ))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to persist discovery event: {e}")
            return False
        finally:
            if conn is not None:
                conn.close()
    
    def get_stats(self) -> dict:
        stats = dict(self.stats)
        stats["queue_stats"] = self.queue.stats() if self.queue else None
        return stats


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
        db_path: Optional[str] = None,
    ):
        self.rpc_url = rpc_url
        self.program_id = program_id
        self.queue = queue
        self.on_event = on_event
        self.db_path = db_path
        
        self._ws: Optional[websockets.ClientConnection] = None
        self._running = False
        self._subscription_id: Optional[int] = None
        
        self._message_count = 0
        self._parse_errors = 0
        self._valid_discovery_count = 0
        self._persisted_count = 0
        self._duplicate_count = 0
        self._persistence_failure_count = 0
    
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
        if self._ws:
            # Check if websocket is closed (works for both ClientWebSocketResponse and ClientConnection)
            ws_closed = getattr(self._ws, 'closed', False)
            if not ws_closed:
                try:
                    await self._ws.close()
                except Exception:
                    pass
    
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
        self._valid_discovery_count += 1
        if self.queue:
            was_duplicate = self.queue.get_candidate(event.mint) is not None
            self.queue.add_or_update(event)
            if was_duplicate:
                self._duplicate_count += 1
        if self.on_event:
            try:
                self.on_event(event)
            except Exception as e:
                logger.error(f"Event callback error: {e}")
        
        # Persist discovery event to database
        if self.db_path:
            if await self._persist_discovery_event(event):
                self._persisted_count += 1
            else:
                self._persistence_failure_count += 1
    
    async def _persist_discovery_event(self, event: DiscoveryEvent) -> bool:
        """Persist raw discovery event to database."""
        conn = None
        try:
            import sqlite3
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                INSERT INTO discovery_events
                (mint, source, event_timestamp, receive_timestamp, symbol, name,
                 creator, uri, bonding_curve_key, initial_buy, sol_amount,
                 v_tokens_in_bonding_curve, v_sol_in_bonding_curve, market_cap_sol,
                 is_mayhem_mode, pool, signature, raw_payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event.mint,
                event.source.value,
                event.event_timestamp,
                event.receive_timestamp,
                event.symbol,
                event.name,
                event.creator,
                event.uri,
                event.bonding_curve_key,
                event.initial_buy,
                event.sol_amount,
                event.v_tokens_in_bonding_curve,
                event.v_sol_in_bonding_curve,
                event.market_cap_sol,
                1 if event.is_mayhem_mode else 0 if event.is_mayhem_mode is not None else None,
                event.pool,
                event.signature,
                json.dumps(event.raw_payload) if event.raw_payload else None,
                time.time()
            ))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to persist discovery event: {e}")
            return False
        finally:
            if conn is not None:
                conn.close()

    def get_stats(self) -> dict:
        return {
            "connected": self.is_connected,
            "message_count": self._message_count,
            "valid_discovery_events": self._valid_discovery_count,
            "unique_mints": self._valid_discovery_count - self._duplicate_count,
            "duplicate_count": self._duplicate_count,
            "malformed_count": self._parse_errors,
            "discovery_events_persisted": self._persisted_count,
            "persistence_failure_count": self._persistence_failure_count,
        }
    
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
        db_path: Optional[str] = None,
    ):
        self.queue = queue
        self.enable_fallback = enable_fallback
        self.db_path = db_path
        
        # Primary: PumpPortal
        self.pumpportal = PumpPortalDiscovery(
            api_key=pumpportal_api_key,
            queue=queue,
            db_path=db_path,
        )
        
        # Fallback: Public RPC
        self.fallback = None
        if enable_fallback:
            self.fallback = SolanaPublicRPCDiscovery(queue=queue, db_path=db_path)
        
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
            stats["fallback"] = self.fallback.get_stats()

        sources = list(stats.values())
        stats["totals"] = {
            "message_count": sum(source.get("message_count", 0) for source in sources),
            "valid_discovery_events": sum(source.get("valid_discovery_events", 0) for source in sources),
            "duplicate_count": sum(source.get("duplicate_count", 0) for source in sources),
            "discovery_events_persisted": sum(
                source.get("discovery_events_persisted", 0) for source in sources
            ),
            "persistence_failure_count": sum(
                source.get("persistence_failure_count", 0) for source in sources
            ),
            # The shared queue is authoritative across discovery sources.
            "unique_mints": self.queue.stats()["total"],
        }
        return stats
