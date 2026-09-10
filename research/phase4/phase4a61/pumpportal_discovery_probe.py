#!/usr/bin/env python3
"""
PumpPortal Discovery Probe - Measure free subscribeNewToken stream for token discovery.

Connects to PumpPortal WebSocket, subscribes to new token events, and measures:
- new token events/minute
- message payload size
- reconnect behavior
- duplicate events
- malformed events
- approximate discovery latency
- missing metadata
"""

import asyncio
import json
import os
import time
import websockets
from dataclasses import dataclass, asdict
from typing import Optional, Set
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

@dataclass
class PumpPortalMeasurement:
    """Results from PumpPortal discovery measurement."""
    sample_duration_seconds: float
    total_messages: int
    new_token_events: int
    unique_mints: int
    duplicate_events: int
    malformed_events: int
    events_per_minute: float
    avg_payload_size_bytes: float
    unique_mints_per_minute: float
    duplicate_rate_pct: float
    reconnect_count: int
    missing_metadata_fields: dict
    sample_mint: Optional[str] = None
    sample_payload: Optional[dict] = None
    timestamp: str = ""

class PumpPortalDiscoveryProbe:
    """Probe PumpPortal WebSocket for new token discovery."""
    
    def __init__(self, api_key: Optional[str] = None, duration_seconds: int = 600):
        self.api_key = api_key
        self.duration_seconds = duration_seconds
        if api_key:
            self.ws_url = f"wss://pumpportal.fun/api/data?api-key={api_key}"
        else:
            self.ws_url = "wss://pumpportal.fun/api/data"
        
        self.message_count = 0
        self.new_token_events = 0
        self.unique_mints: Set[str] = set()
        self.duplicate_events = 0
        self.malformed_events = 0
        self.total_payload_bytes = 0
        self.reconnect_count = 0
        self.missing_metadata = {
            "name": 0,
            "symbol": 0,
            "uri": 0,
            "creator": 0,
            "bondingCurve": 0
        }
        self.start_time = None
        self.sample_mint = None
        self.sample_payload = None
        
    async def connect_and_measure(self) -> PumpPortalMeasurement:
        """Connect to PumpPortal WSS and measure discovery traffic."""
        logger.info(f"Connecting to PumpPortal WSS for {self.duration_seconds}s measurement...")
        
        subscription_msg = {
            "method": "subscribeNewToken"
        }
        
        end_time = time.time() + self.duration_seconds
        
        while time.time() < end_time:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=10) as ws:
                    # Subscribe
                    await ws.send(json.dumps(subscription_msg))
                    logger.info("Subscribed to new token events")
                    
                    self.start_time = time.time()
                    
                    while time.time() < end_time:
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=10.0)
                            self._process_message(message)
                        except asyncio.TimeoutError:
                            # Check if we should continue
                            continue
                        except websockets.exceptions.ConnectionClosed:
                            logger.warning("WebSocket connection closed, reconnecting...")
                            self.reconnect_count += 1
                            break
                            
            except Exception as e:
                logger.error(f"Connection error: {e}, reconnecting in 5s...")
                self.reconnect_count += 1
                await asyncio.sleep(5)
                continue
        
        return self._compute_results()
    
    def _process_message(self, message):
        """Process incoming WebSocket message."""
        if isinstance(message, bytes):
            payload_bytes = len(message)
            message_str = message.decode('utf-8', errors='ignore')
        else:
            payload_bytes = len(message.encode('utf-8'))
            message_str = message
        
        self.message_count += 1
        self.total_payload_bytes += payload_bytes
        
        try:
            data = json.loads(message_str)
        except json.JSONDecodeError:
            self.malformed_events += 1
            return
        
        # Check if it's a new token event
        if isinstance(data, dict):
            if data.get("mint"):  # New token event has mint field
                self.new_token_events += 1
                mint = data.get("mint", "")
                
                if mint in self.unique_mints:
                    self.duplicate_events += 1
                else:
                    self.unique_mints.add(mint)
                    if self.sample_mint is None:
                        self.sample_mint = mint
                        self.sample_payload = data
                
                # Check for missing metadata
                for field in self.missing_metadata:
                    if field not in data or not data.get(field):
                        self.missing_metadata[field] += 1
            elif "method" in data and data.get("method") == "subscribeNewToken":
                # Subscription confirmation
                logger.info(f"Subscription confirmed: {data}")
            else:
                logger.debug(f"Other message: {data}")
        else:
            self.malformed_events += 1
    
    def _compute_results(self) -> PumpPortalMeasurement:
        """Compute final measurement results."""
        elapsed = time.time() - self.start_time if self.start_time else self.duration_seconds
        
        events_per_minute = (self.new_token_events / elapsed) * 60 if elapsed > 0 else 0
        unique_per_minute = (len(self.unique_mints) / elapsed) * 60 if elapsed > 0 else 0
        avg_payload = self.total_payload_bytes / self.message_count if self.message_count > 0 else 0
        duplicate_rate = (self.duplicate_events / self.new_token_events * 100) if self.new_token_events > 0 else 0
        
        return PumpPortalMeasurement(
            sample_duration_seconds=elapsed,
            total_messages=self.message_count,
            new_token_events=self.new_token_events,
            unique_mints=len(self.unique_mints),
            duplicate_events=self.duplicate_events,
            malformed_events=self.malformed_events,
            events_per_minute=events_per_minute,
            avg_payload_size_bytes=avg_payload,
            unique_mints_per_minute=unique_per_minute,
            duplicate_rate_pct=duplicate_rate,
            reconnect_count=self.reconnect_count,
            missing_metadata_fields=self.missing_metadata,
            sample_mint=self.sample_mint,
            sample_payload=self.sample_payload,
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        )

async def main():
    # Get API key from environment (optional for subscribeNewToken)
    api_key = os.environ.get("PUMPPORTAL_API_KEY")
    if api_key:
        logger.info("Using PumpPortal API key from environment")
    else:
        logger.info("No PumpPortal API key - testing without credentials")
    
    probe = PumpPortalDiscoveryProbe(api_key, duration_seconds=600)  # 10 minutes
    result = await probe.connect_and_measure()
    
    # Save results
    output_path = "/home/ibcnu/PowerTraderAI/research/phase4/phase4a61/pumpportal_measurement_results.json"
    with open(output_path, "w") as f:
        json.dump(asdict(result), f, indent=2)
    
    logger.info(f"Results saved to {output_path}")
    logger.info(f"=== PUMPPORTAL DISCOVERY MEASUREMENT ===")
    logger.info(f"Duration: {result.sample_duration_seconds:.1f}s")
    logger.info(f"Total messages: {result.total_messages}")
    logger.info(f"New token events: {result.new_token_events}")
    logger.info(f"Unique mints: {result.unique_mints}")
    logger.info(f"Duplicate events: {result.duplicate_events}")
    logger.info(f"Malformed events: {result.malformed_events}")
    logger.info(f"Events/minute: {result.events_per_minute:.1f}")
    logger.info(f"Unique mints/minute: {result.unique_mints_per_minute:.1f}")
    logger.info(f"Avg payload size: {result.avg_payload_size_bytes:.1f} bytes")
    logger.info(f"Duplicate rate: {result.duplicate_rate_pct:.1f}%")
    logger.info(f"Reconnect count: {result.reconnect_count}")
    logger.info(f"Missing metadata: {result.missing_metadata_fields}")
    if result.sample_mint:
        logger.info(f"Sample mint: {result.sample_mint}")
        logger.info(f"Sample payload keys: {list(result.sample_payload.keys()) if result.sample_payload else 'N/A'}")
    
    return result

if __name__ == "__main__":
    asyncio.run(main())