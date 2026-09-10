#!/usr/bin/env python3
"""
Helius Bandwidth Probe - Measure actual Pump.fun discovery traffic via Helius WebSocket.

Connects to Helius WSS, subscribes to Pump.fun program logs, and measures:
- raw bytes received
- message count
- events/minute
- average bytes/message
- peak throughput
"""

import asyncio
import json
import os
import time
import websockets
from dataclasses import dataclass, asdict
from typing import Optional
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Pump.fun program ID
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

@dataclass
class MeasurementResult:
    """Results from a measurement session."""
    sample_duration_seconds: float
    total_messages: int
    total_bytes: int
    events_per_minute: float
    bytes_per_message_avg: float
    bytes_per_second_avg: float
    peak_bytes_per_second: float
    projected_mb_per_hour: float
    projected_mb_per_day: float
    projected_gb_per_day: float
    helius_credits_per_day: float
    helius_credits_per_month: float
    pct_free_tier_quota: float
    subscription_method: str
    program_id: str
    timestamp: str

class HeliusBandwidthProbe:
    """Probe Helius WebSocket for Pump.fun discovery traffic."""
    
    def __init__(self, api_key: str, duration_seconds: int = 600):
        self.api_key = api_key
        self.duration_seconds = duration_seconds
        self.ws_url = f"wss://mainnet.helius-rpc.com/?api-key={api_key}"
        self.message_count = 0
        self.total_bytes = 0
        self.bytes_per_second_samples = []
        self.start_time = None
        self.last_second_bytes = 0
        self.last_second_time = None
        self.peak_bytes_per_second = 0
        
    async def connect_and_measure(self) -> MeasurementResult:
        """Connect to Helius WSS and measure traffic."""
        logger.info(f"Connecting to Helius WSS for {self.duration_seconds}s measurement...")
        
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
            async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=10) as ws:
                # Subscribe
                await ws.send(json.dumps(subscription_msg))
                response = await ws.recv()
                logger.info(f"Subscription response: {response}")
                
                # Start measurement
                self.start_time = time.time()
                self.last_second_time = self.start_time
                
                # Measure for specified duration
                end_time = self.start_time + self.duration_seconds
                
                while time.time() < end_time:
                    try:
                        # Wait for message with timeout
                        message = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        self._process_message(message)
                    except asyncio.TimeoutError:
                        # No message in 5s, continue
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        logger.warning("WebSocket connection closed")
                        break
                        
        except Exception as e:
            logger.error(f"Connection error: {e}")
            raise
            
        return self._compute_results()
    
    def _process_message(self, message):
        """Process incoming WebSocket message."""
        if isinstance(message, bytes):
            msg_bytes = len(message)
            message_str = message.decode('utf-8', errors='ignore')
        else:
            msg_bytes = len(message.encode('utf-8'))
            message_str = message
        self.message_count += 1
        self.total_bytes += msg_bytes
        
        # Track bytes per second for peak calculation
        now = time.time()
        if self.last_second_time is None:
            self.last_second_time = now
            self.last_second_bytes = msg_bytes
        elif now - self.last_second_time >= 1.0:
            # New second
            self.bytes_per_second_samples.append(self.last_second_bytes)
            if self.last_second_bytes > self.peak_bytes_per_second:
                self.peak_bytes_per_second = self.last_second_bytes
            self.last_second_bytes = msg_bytes
            self.last_second_time = now
        else:
            self.last_second_bytes += msg_bytes
    
    def _compute_results(self) -> MeasurementResult:
        """Compute final measurement results."""
        elapsed = time.time() - self.start_time if self.start_time else self.duration_seconds
        
        events_per_minute = (self.message_count / elapsed) * 60 if elapsed > 0 else 0
        bytes_per_message_avg = self.total_bytes / self.message_count if self.message_count > 0 else 0
        bytes_per_second_avg = self.total_bytes / elapsed if elapsed > 0 else 0
        
        # Project to daily
        projected_mb_per_hour = (bytes_per_second_avg * 3600) / (1024 * 1024)
        projected_mb_per_day = projected_mb_per_hour * 24
        projected_gb_per_day = projected_mb_per_day / 1024
        
        # Helius cost: 2 credits per 0.1 MB = 20 credits per MB
        helius_credits_per_day = projected_mb_per_day * 20
        helius_credits_per_month = helius_credits_per_day * 30
        
        # Free tier: 1M credits/month
        pct_free_tier_quota = (helius_credits_per_month / 1_000_000) * 100
        
        return MeasurementResult(
            sample_duration_seconds=elapsed,
            total_messages=self.message_count,
            total_bytes=self.total_bytes,
            events_per_minute=events_per_minute,
            bytes_per_message_avg=bytes_per_message_avg,
            bytes_per_second_avg=bytes_per_second_avg,
            peak_bytes_per_second=self.peak_bytes_per_second,
            projected_mb_per_hour=projected_mb_per_hour,
            projected_mb_per_day=projected_mb_per_day,
            projected_gb_per_day=projected_gb_per_day,
            helius_credits_per_day=helius_credits_per_day,
            helius_credits_per_month=helius_credits_per_month,
            pct_free_tier_quota=pct_free_tier_quota,
            subscription_method="logsSubscribe",
            program_id=PUMP_FUN_PROGRAM,
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        )

async def main():
    # Get API key from environment
    api_key = os.environ.get("HELIUS_API_KEY")
    if not api_key:
        logger.error("HELIUS_API_KEY environment variable not set")
        logger.info("Skipping Helius measurement - mark as UNVERIFIED")
        return
    
    probe = HeliusBandwidthProbe(api_key, duration_seconds=600)  # 10 minutes
    result = await probe.connect_and_measure()
    
    # Save results
    output_path = "/home/ibcnu/PowerTraderAI/research/phase4/phase4a61/helius_measurement_results.json"
    with open(output_path, "w") as f:
        json.dump(asdict(result), f, indent=2)
    
    logger.info(f"Results saved to {output_path}")
    logger.info(f"=== MEASUREMENT RESULTS ===")
    logger.info(f"Duration: {result.sample_duration_seconds:.1f}s")
    logger.info(f"Messages: {result.total_messages}")
    logger.info(f"Total bytes: {result.total_bytes:,}")
    logger.info(f"Events/minute: {result.events_per_minute:.1f}")
    logger.info(f"Avg bytes/msg: {result.bytes_per_message_avg:.1f}")
    logger.info(f"Avg bytes/sec: {result.bytes_per_second_avg:.1f}")
    logger.info(f"Peak bytes/sec: {result.peak_bytes_per_second:.1f}")
    logger.info(f"Projected MB/hour: {result.projected_mb_per_hour:.2f}")
    logger.info(f"Projected MB/day: {result.projected_mb_per_day:.2f}")
    logger.info(f"Projected GB/day: {result.projected_gb_per_day:.4f}")
    logger.info(f"Helius credits/day: {result.helius_credits_per_day:.0f}")
    logger.info(f"Helius credits/month: {result.helius_credits_per_month:.0f}")
    logger.info(f"% Free tier quota: {result.pct_free_tier_quota:.1f}%")
    
    return result

if __name__ == "__main__":
    asyncio.run(main())