"""
Alpaca Trading Integration for PowerTrader AI
Replaces Robinhood-specific trading execution with Alpaca Markets API.

Uses alpaca-py SDK (official). Supports both paper and live trading.
"""

import os
import json
import time
import logging
from typing import Any, Dict, List, Optional, Tuple
from decimal import Decimal, ROUND_HALF_UP

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetOrdersRequest,
    GetAssetsRequest,
    MarketOrderRequest,
    LimitOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoLatestQuoteRequest
from alpaca.data.enums import DataFeed

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
# Paper trading base URL (no env var needed - SDK handles it)
# For live trading, keys must have live permissions in Alpaca dashboard

class AlpacaTrading:
    """
    Alpaca trading execution layer.
    Provides the same interface the rest of the bot expects.
    """
    
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        paper: bool = True,
        timeout: int = 30
    ):
        self.api_key = (api_key or "").strip()
        self.secret_key = (secret_key or "").strip()
        self.paper = paper
        self.timeout = timeout
        
        if not self.api_key or not self.secret_key:
            raise RuntimeError("Alpaca API key and secret key are required")
        
        # Initialize trading client (handles auth, signing, base URL)
        self.trading_client = TradingClient(
            api_key=self.api_key,
            secret_key=self.secret_key,
            paper=paper,
        )
        
        # Initialize market data client (for quotes - free for crypto)
        # Crypto uses IEX feed by default, no subscription needed
        self.market_data_client = CryptoHistoricalDataClient()
        
        # Cache for symbol mapping (Alpaca uses BTC/USD format)
        self._symbol_cache: Dict[str, str] = {}
        
    # -------------------------------------------------------------------------
    # Symbol conversion: BTC-USD (Robinhood) <-> BTC/USD (Alpaca)
    # -------------------------------------------------------------------------
    def _to_alpaca_symbol(self, symbol: str) -> str:
        """Convert 'BTC-USD' -> 'BTC/USD'"""
        symbol = symbol.strip().upper()
        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]
        # Handle various input formats
        if "-" in symbol:
            base, quote = symbol.split("-", 1)
            alpaca_symbol = f"{base}/{quote}"
        elif "/" in symbol:
            alpaca_symbol = symbol
        else:
            # Assume it's just the base currency, default to USD
            alpaca_symbol = f"{symbol}/USD"
        self._symbol_cache[symbol] = alpaca_symbol
        return alpaca_symbol
    
    def _to_standard_symbol(self, symbol: str) -> str:
        """Convert 'BTC/USD' -> 'BTC-USD'"""
        return symbol.replace("/", "-")
    
    # -------------------------------------------------------------------------
    # Account & Positions
    # -------------------------------------------------------------------------
    def get_account(self) -> Dict[str, Any]:
        """Get account info - returns dict compatible with Robinhood format"""
        try:
            account = self.trading_client.get_account()
            # Convert to Robinhood-like format for compatibility
            return {
                "account_number": account.account_number,
                "status": account.status.value if hasattr(account.status, 'value') else str(account.status),
                "buying_power": float(account.buying_power),
                "cash": float(account.cash),
                "portfolio_value": float(account.portfolio_value),
                "equity": float(account.equity),
                "multiplier": account.multiplier,
                "currency": account.currency,
                "pattern_day_trader": account.pattern_day_trader,
                "trading_blocked": account.trading_blocked,
                "transfers_blocked": account.transfers_blocked,
                "account_blocked": account.account_blocked,
            }
        except Exception as e:
            logger.error(f"Failed to get account: {e}")
            raise RuntimeError(f"Alpaca get_account failed: {e}")
    
    def get_positions(self) -> List[Dict[str, Any]]:
        """Get current positions - returns list compatible with Robinhood format"""
        try:
            positions = self.trading_client.get_all_positions()
            result = []
            for pos in positions:
                # Only include crypto positions (Alpaca returns both stocks and crypto)
                if pos.asset_class and pos.asset_class.value == "crypto":
                    result.append({
                        "symbol": self._to_standard_symbol(pos.symbol),
                        "qty": float(pos.qty),
                        "avg_entry_price": float(pos.avg_entry_price),
                        "current_price": float(pos.current_price) if pos.current_price else 0.0,
                        "market_value": float(pos.market_value),
                        "unrealized_pl": float(pos.unrealized_pl),
                        "unrealized_plpc": float(pos.unrealized_plpc) if pos.unrealized_plpc else 0.0,
                        "side": pos.side.value if hasattr(pos.side, 'value') else str(pos.side),
                        "asset_class": pos.asset_class.value if hasattr(pos.asset_class, 'value') else str(pos.asset_class),
                    })
            return result
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            raise RuntimeError(f"Alpaca get_positions failed: {e}")
    
    def get_holdings(self) -> List[Dict[str, Any]]:
        """Alias for get_positions for compatibility"""
        return self.get_positions()
    
    # -------------------------------------------------------------------------
    # Market Data (Current Ask Price)
    # -------------------------------------------------------------------------
    def get_current_ask(self, symbol: str) -> float:
        """
        Get current ask price for a symbol.
        Uses Alpaca's latest quote endpoint (free for crypto).
        """
        try:
            alpaca_symbol = self._to_alpaca_symbol(symbol)
            request = CryptoLatestQuoteRequest(symbol_or_symbols=alpaca_symbol)
            quotes = self.market_data_client.get_crypto_latest_quote(request)
            
            if alpaca_symbol in quotes:
                quote = quotes[alpaca_symbol]
                # Alpaca quote has ask_price and bid_price
                ask = float(quote.ask_price) if quote.ask_price else 0.0
                if ask > 0:
                    return ask
            
            # Fallback: try to get from latest trade
            raise RuntimeError(f"No quote available for {symbol}")
            
        except Exception as e:
            logger.error(f"Failed to get current ask for {symbol}: {e}")
            raise RuntimeError(f"Alpaca get_current_ask failed for {symbol}: {e}")
    
    def get_current_bid(self, symbol: str) -> float:
        """Get current bid price for a symbol"""
        try:
            alpaca_symbol = self._to_alpaca_symbol(symbol)
            request = CryptoLatestQuoteRequest(symbol_or_symbols=alpaca_symbol)
            quotes = self.market_data_client.get_crypto_latest_quote(request)
            
            if alpaca_symbol in quotes:
                quote = quotes[alpaca_symbol]
                bid = float(quote.bid_price) if quote.bid_price else 0.0
                if bid > 0:
                    return bid
            raise RuntimeError(f"No quote available for {symbol}")
        except Exception as e:
            logger.error(f"Failed to get current bid for {symbol}: {e}")
            raise RuntimeError(f"Alpaca get_current_bid failed for {symbol}: {e}")
    
    def get_current_price(self, symbol: str) -> float:
        """Get mid price (average of bid/ask)"""
        ask = self.get_current_ask(symbol)
        bid = self.get_current_bid(symbol)
        if ask > 0 and bid > 0:
            return (ask + bid) / 2
        return ask if ask > 0 else bid
    
    # -------------------------------------------------------------------------
    # Order Management
    # -------------------------------------------------------------------------
    def place_market_order(
        self,
        symbol: str,
        qty: float,
        side: str,  # "buy" or "sell"
        client_order_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Place a market order.
        Returns order info in Robinhood-compatible format.
        """
        try:
            alpaca_symbol = self._to_alpaca_symbol(symbol)
            order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
            
            # For crypto, qty is in base currency (e.g., BTC)
            # Notional orders (USD amount) also supported but we'll use qty
            request = MarketOrderRequest(
                symbol=alpaca_symbol,
                qty=Decimal(str(qty)).quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP),
                side=order_side,
                time_in_force=TimeInForce.GTC,
                client_order_id=client_order_id,
            )
            
            order = self.trading_client.submit_order(request)
            
            return self._format_order(order)
            
        except Exception as e:
            logger.error(f"Failed to place market order: {e}")
            raise RuntimeError(f"Alpaca place_market_order failed: {e}")
    
    def place_limit_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        limit_price: float,
        client_order_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Place a limit order"""
        try:
            alpaca_symbol = self._to_alpaca_symbol(symbol)
            order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
            
            request = LimitOrderRequest(
                symbol=alpaca_symbol,
                qty=Decimal(str(qty)).quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP),
                side=order_side,
                time_in_force=TimeInForce.GTC,
                limit_price=Decimal(str(limit_price)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                client_order_id=client_order_id,
            )
            
            order = self.trading_client.submit_order(request)
            return self._format_order(order)
            
        except Exception as e:
            logger.error(f"Failed to place limit order: {e}")
            raise RuntimeError(f"Alpaca place_limit_order failed: {e}")
    
    def get_order(self, order_id: str) -> Dict[str, Any]:
        """Get order by ID"""
        try:
            order = self.trading_client.get_order_by_id(order_id)
            return self._format_order(order)
        except Exception as e:
            logger.error(f"Failed to get order {order_id}: {e}")
            raise RuntimeError(f"Alpaca get_order failed: {e}")
    
    def get_orders(
        self,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        after: Optional[str] = None,
        until: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get orders with optional filtering.
        Returns list in Robinhood-compatible format.
        """
        try:
            request_params = {"limit": limit, "nested": True}
            
            if symbol:
                request_params["symbols"] = [self._to_alpaca_symbol(symbol)]
            if status:
                request_params["status"] = QueryOrderStatus(status.upper())
            if after:
                request_params["after"] = after
            if until:
                request_params["until"] = until
                
            request = GetOrdersRequest(**request_params)
            orders = self.trading_client.get_orders(filter=request)
            
            return [self._format_order(o) for o in orders]
            
        except Exception as e:
            logger.error(f"Failed to get orders: {e}")
            raise RuntimeError(f"Alpaca get_orders failed: {e}")
    
    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order by ID"""
        try:
            self.trading_client.cancel_order_by_id(order_id)
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False
    
    def cancel_all_orders(self) -> int:
        """Cancel all open orders"""
        try:
            result = self.trading_client.cancel_orders()
            return len(result) if result else 0
        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")
            return 0
    
    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------
    def _format_order(self, order: Any) -> Dict[str, Any]:
        """Convert Alpaca order to Robinhood-compatible dict"""
        # Alpaca order has: id, client_order_id, symbol, qty, filled_qty, side, type,
        # time_in_force, status, limit_price, filled_avg_price, created_at, updated_at,
        # submitted_at, legs (for multi-leg)
        
        symbol = self._to_standard_symbol(order.symbol)
        filled_qty = float(order.filled_qty) if order.filled_qty else 0.0
        avg_fill_price = float(order.filled_avg_price) if order.filled_avg_price else 0.0
        
        # Build executions list for compatibility
        executions = []
        if filled_qty > 0 and avg_fill_price > 0:
            executions.append({
                "quantity": str(filled_qty),
                "effective_price": str(avg_fill_price),
                "timestamp": order.filled_at.isoformat() if order.filled_at else order.updated_at.isoformat(),
            })
        
        return {
            "id": order.id,
            "client_order_id": order.client_order_id,
            "symbol": symbol,
            "side": order.side.value.lower() if hasattr(order.side, 'value') else str(order.side).lower(),
            "type": order.order_type.value.lower() if hasattr(order.order_type, 'value') else str(order.order_type).lower(),
            "time_in_force": order.time_in_force.value.lower() if hasattr(order.time_in_force, 'value') else str(order.time_in_force).lower(),
            "qty": str(float(order.qty)) if order.qty else "0",
            "filled_qty": str(filled_qty),
            "filled_avg_price": str(avg_fill_price),
            "limit_price": str(float(order.limit_price)) if order.limit_price else None,
            "stop_price": str(float(order.stop_price)) if order.stop_price else None,
            "status": order.status.value.lower() if hasattr(order.status, 'value') else str(order.status).lower(),
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "updated_at": order.updated_at.isoformat() if order.updated_at else None,
            "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
            "filled_at": order.filled_at.isoformat() if order.filled_at else None,
            "executions": executions,
            "asset_class": order.asset_class.value if hasattr(order.asset_class, 'value') else str(order.asset_class),
        }
    
    # -------------------------------------------------------------------------
    # Trading Pairs / Assets
    # -------------------------------------------------------------------------
    def get_trading_pairs(self) -> List[Dict[str, Any]]:
        """Get tradeable crypto pairs"""
        try:
            request = GetAssetsRequest(asset_class="crypto", status="active")
            assets = self.trading_client.get_all_assets(request)
            
            result = []
            for asset in assets:
                if asset.tradable and asset.asset_class and asset.asset_class.value == "crypto":
                    result.append({
                        "symbol": self._to_standard_symbol(asset.symbol),
                        "name": asset.name,
                        "min_order_size": str(float(asset.min_order_size)) if asset.min_order_size else None,
                        "status": asset.status,
                        "asset_class": asset.asset_class.value if hasattr(asset.asset_class, 'value') else str(asset.asset_class),
                    })
            return result
        except Exception as e:
            logger.error(f"Failed to get trading pairs: {e}")
            raise RuntimeError(f"Alpaca get_trading_pairs failed: {e}")

    # -------------------------------------------------------------------------
    # Historical Data (for compatibility with existing candle fetching)
    # -------------------------------------------------------------------------
    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 200,
        start: Optional[str] = None,
        end: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get historical candles from Alpaca.
        Timeframe mapping: 1Min, 5Min, 15Min, 30Min, 1Hour, 2Hour, 4Hour, 1Day, 1Week
        """
        try:
            from alpaca.data.requests import CryptoBarsRequest
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
            
            alpaca_symbol = self._to_alpaca_symbol(symbol)
            
            # Map timeframe string to Alpaca TimeFrame
            tf_map = {
                "1min": TimeFrame(1, TimeFrameUnit.Minute),
                "5min": TimeFrame(5, TimeFrameUnit.Minute),
                "15min": TimeFrame(15, TimeFrameUnit.Minute),
                "30min": TimeFrame(30, TimeFrameUnit.Minute),
                "1hour": TimeFrame(1, TimeFrameUnit.Hour),
                "2hour": TimeFrame(2, TimeFrameUnit.Hour),
                "4hour": TimeFrame(4, TimeFrameUnit.Hour),
                "8hour": TimeFrame(8, TimeFrameUnit.Hour),
                "12hour": TimeFrame(12, TimeFrameUnit.Hour),
                "1day": TimeFrame(1, TimeFrameUnit.Day),
                "1week": TimeFrame(1, TimeFrameUnit.Week),
            }
            
            tf = tf_map.get(timeframe.lower(), TimeFrame(1, TimeFrameUnit.Hour))
            
            request = CryptoBarsRequest(
                symbol_or_symbols=[alpaca_symbol],
                timeframe=tf,
                limit=limit,
                start=start,
                end=end,
                feed=DataFeed.IEX,  # Free crypto feed
            )
            
            bars = self.market_data_client.get_crypto_bars(request)
            
            result = []
            for bar in bars[alpaca_symbol]:
                result.append({
                    "ts": int(bar.timestamp.timestamp()),
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": float(bar.volume),
                })
            
            return result
            
        except Exception as e:
            logger.error(f"Failed to get candles for {symbol}: {e}")
            raise RuntimeError(f"Alpaca get_candles failed: {e}")


# -----------------------------------------------------------------------------
# Convenience function for drop-in replacement
# -----------------------------------------------------------------------------
def create_alpaca_trader(
    api_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    paper: bool = True,
    key_file: str = "alpaca_key.txt",
    secret_file: str = "alpaca_secret.txt"
) -> AlpacaTrading:
    """
    Create AlpacaTrading instance from key files or env vars.
    
    Priority:
    1. Explicit api_key/secret_key params
    2. Files (alpaca_key.txt, alpaca_secret.txt in working dir)
    3. Environment variables (ALPACA_API_KEY, ALPACA_SECRET_KEY)
    """
    # Try explicit params first
    if api_key and secret_key:
        return AlpacaTrading(api_key, secret_key, paper=paper)
    
    # Try files
    base_dir = os.path.dirname(os.path.abspath(__file__))
    key_path = os.path.join(base_dir, key_file)
    secret_path = os.path.join(base_dir, secret_file)
    
    if os.path.isfile(key_path) and os.path.isfile(secret_path):
        with open(key_path, "r") as f:
            api_key = f.read().strip()
        with open(secret_path, "r") as f:
            secret_key = f.read().strip()
        return AlpacaTrading(api_key, secret_key, paper=paper)
    
    # Try environment variables
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if api_key and secret_key:
        paper_env = os.environ.get("ALPACA_PAPER", "true").lower()
        paper = paper_env in ("true", "1", "yes")
        return AlpacaTrading(api_key, secret_key, paper=paper)
    
    raise RuntimeError(
        "Alpaca credentials not found. Provide api_key/secret_key params, "
        f"or create {key_file}/{secret_file} in the working directory, "
        "or set ALPACA_API_KEY/ALPACA_SECRET_KEY environment variables."
    )


# -----------------------------------------------------------------------------
# Simple test
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    
    # Quick test with paper trading keys from files
    try:
        trader = create_alpaca_trader(paper=True)
        print("Alpaca connection successful!")
        
        # Test account
        acct = trader.get_account()
        print(f"Account: {acct['account_number']}, Buying Power: ${acct['buying_power']:,.2f}")
        
        # Test positions
        positions = trader.get_positions()
        print(f"Positions: {len(positions)}")
        
        # Test quote
        price = trader.get_current_ask("BTC-USD")
        print(f"BTC-USD Ask: ${price:,.2f}")
        
    except Exception as e:
        print(f"Test failed: {e}")
        sys.exit(1)