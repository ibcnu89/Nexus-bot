"""
PowerTrader AI - FOMO-style Web Interface
Binance market data + Alpaca trading + FOMO-style mobile UI
"""
import os
import sys
import json
import asyncio
import logging
import aiohttp
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Depends, HTTPException, status, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from passlib.context import CryptContext
from jose import jwt, JWTError
from dotenv import load_dotenv

# Load env
load_dotenv(Path(__file__).parent / ".env")

# Add bot directory to path
BOT_DIR = Path(os.getenv("BOT_DIR", "/home/ibcnu/PowerTraderAI"))
sys.path.insert(0, str(BOT_DIR))

# Import FOMO client
from fomo_client import FOMOClient, get_fomo_client, close_fomo_client

# Config
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY environment variable is required. Set a strong secret for JWT signing.")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))

# Security
pwd_context = CryptContext(schemes=["sha256_crypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login", auto_error=False)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("powertrader-web")

# -----------------------------------------------------------------------------
# CoinGecko Market Data Client (keyless, works from cloud IPs)
# -----------------------------------------------------------------------------
class CoinGeckoMarketData:
    BASE = "https://api.coingecko.com/api/v3"
    
    def __init__(self):
        self.session = None
    
    async def get_session(self):
        if self.session is None:
            import aiohttp
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None
    
    async def get_all_tickers(self) -> List[Dict]:
        """Get top coins by market cap"""
        try:
            session = await self.get_session()
            params = {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 50,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "24h"
            }
            async with session.get(f"{self.BASE}/coins/markets", params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                # Check response status
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"CoinGecko API error status {resp.status}: {text[:200]}")
                    return self._get_mock_data()
                
                data = await resp.json()
                if isinstance(data, dict) and "error" in data:
                    logger.error(f"CoinGecko API error: {data}")
                    return self._get_mock_data()
                # Filter out non-dict entries
                if isinstance(data, list):
                    return [d for d in data if isinstance(d, dict)]
                return self._get_mock_data()
        except Exception as e:
            logger.error(f"Failed to get CoinGecko tickers: {e}")
            return self._get_mock_data()
    
    def _get_mock_data(self) -> List[Dict]:
        """Return mock data for development when API is unavailable"""
        return [
            {"symbol": "btc", "current_price": 79354.50, "price_change_percentage_24h": 2.34, "total_volume": 25000000000, "high_24h": 80000, "low_24h": 77000},
            {"symbol": "eth", "current_price": 2487.40, "price_change_percentage_24h": 1.87, "total_volume": 12000000000, "high_24h": 2520, "low_24h": 2400},
            {"symbol": "bnb", "current_price": 421.50, "price_change_percentage_24h": 0.95, "total_volume": 800000000, "high_24h": 425, "low_24h": 415},
            {"symbol": "sol", "current_price": 105.37, "price_change_percentage_24h": 3.42, "total_volume": 2500000000, "high_24h": 108, "low_24h": 102},
            {"symbol": "xrp", "current_price": 0.52, "price_change_percentage_24h": -0.45, "total_volume": 1500000000, "high_24h": 0.53, "low_24h": 0.51},
            {"symbol": "ada", "current_price": 0.38, "price_change_percentage_24h": 1.12, "total_volume": 500000000, "high_24h": 0.39, "low_24h": 0.37},
            {"symbol": "doge", "current_price": 0.091, "price_change_percentage_24h": 2.18, "total_volume": 800000000, "high_24h": 0.093, "low_24h": 0.088},
            {"symbol": "matic", "current_price": 0.68, "price_change_percentage_24h": 1.89, "total_volume": 400000000, "high_24h": 0.70, "low_24h": 0.66},
            {"symbol": "dot", "current_price": 5.23, "price_change_percentage_24h": 0.76, "total_volume": 200000000, "high_24h": 5.30, "low_24h": 5.15},
            {"symbol": "avax", "current_price": 26.45, "price_change_percentage_24h": 2.31, "total_volume": 300000000, "high_24h": 27.00, "low_24h": 25.80},
        ]
    
    async def get_klines(self, symbol: str, interval: str = "1h", limit: int = 200) -> List[Dict]:
        """Fetch OHLCV data - CoinGecko uses days for interval"""
        try:
            session = await self.get_session()
            # Convert symbol from BTCUSDT to bitcoin
            coin_id = self._symbol_to_coin_id(symbol)
            days = min(limit // 24, 90)  # CoinGecko max 90 days for hourly
            params = {
                "vs_currency": "usd",
                "days": days,
                "interval": "hourly" if days <= 90 else "daily"
            }
            async with session.get(f"{self.BASE}/coins/{coin_id}/ohlc", params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                if isinstance(data, dict) and "error" in data:
                    logger.error(f"CoinGecko OHLC error: {data}")
                    return []
                # Convert OHLC to klines format
                return [{
                    "ts": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                } for row in data]
        except Exception as e:
            logger.error(f"Failed to get klines for {symbol}: {e}")
            return []
    
    async def get_ticker_24h(self, symbol: str) -> Dict:
        try:
            session = await self.get_session()
            coin_id = self._symbol_to_coin_id(symbol)
            async with session.get(f"{self.BASE}/coins/{coin_id}", params={"localization": "false", "tickers": "true", "market_data": "true"}) as resp:
                if resp.status != 200:
                    logger.error(f"CoinGecko ticker error status {resp.status}")
                    return self._get_mock_ticker(symbol)
                return await resp.json()
        except Exception as e:
            logger.error(f"Failed to get ticker for {symbol}: {e}")
            return self._get_mock_ticker(symbol)
    
    def _get_mock_ticker(self, symbol: str) -> Dict:
        """Return mock ticker data"""
        mock_data = self._get_mock_data()
        for m in mock_data:
            if m.get("symbol", "").upper() == symbol.replace("USDT", "").lower():
                return {
                    "market_data": {
                        "current_price": {"usd": m.get("current_price", 0)},
                        "price_change_percentage_24h": m.get("price_change_percentage_24h", 0),
                        "total_volume": {"usd": m.get("total_volume", 0)},
                        "high_24h": {"usd": m.get("high_24h", 0)},
                        "low_24h": {"usd": m.get("low_24h", 0)},
                        "current_price": {"usd": m.get("current_price", 0)},
                    }
                }
        return {"market_data": {}}
    
    async def get_all_tickers(self) -> List[Dict]:
        """Get market data for all coins"""
        try:
            session = await self.get_session()
            params = {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 50,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "24h"
            }
            async with session.get(f"{self.BASE}/coins/markets", params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"CoinGecko API error status {resp.status}: {text[:200]}")
                    return self._get_mock_data()
                
                data = await resp.json()
                if isinstance(data, dict):
                    if "error" in data:
                        logger.error(f"CoinGecko API error: {data}")
                    logger.warning(f"Unexpected data type: {type(data)}")
                    return self._get_mock_data()
                if isinstance(data, list):
                    result = [d for d in data if isinstance(d, dict) and d.get("symbol") and d.get("current_price") is not None]
                    if not result:
                        logger.warning("CoinGecko returned empty or invalid list, using mock data")
                        return self._get_mock_data()
                    return result
                logger.warning(f"Unexpected data type: {type(data)}")
                return self._get_mock_data()
        except Exception as e:
            logger.error(f"Failed to get CoinGecko tickers: {e}")
            return self._get_mock_data()
    
    def _symbol_to_coin_id(self, symbol: str) -> str:
        """Convert BTCUSDT -> bitcoin"""
        mapping = {
            "BTCUSDT": "bitcoin",
            "ETHUSDT": "ethereum",
            "BNBUSDT": "binancecoin",
            "SOLUSDT": "solana",
            "XRPUSDT": "ripple",
            "ADAUSDT": "cardano",
            "DOGEUSDT": "dogecoin",
            "MATICUSDT": "matic-network",
            "DOTUSDT": "polkadot",
            "AVAXUSDT": "avalanche-2",
            "LINKUSDT": "chainlink",
            "UNIUSDT": "uniswap",
            "LTCUSDT": "litecoin",
            "BCHUSDT": "bitcoin-cash",
            "ATOMUSDT": "cosmos",
            "NEARUSDT": "near",
            "ALGOUSDT": "algorand",
            "VETUSDT": "vechain",
            "ICPUSDT": "internet-computer",
            "FILUSDT": "filecoin",
            "THETAUSDT": "theta-token",
            "ETCUSDT": "ethereum-classic",
            "XLMUSDT": "stellar",
            "EOSUSDT": "eos",
            "AAVEUSDT": "aave",
            "MKRUSDT": "maker",
            "COMPUSDT": "compound-governance-token",
            "SNXUSDT": "synthetix-network-token",
            "CRVUSDT": "curve-dao-token",
            "SUSHIUSDT": "sushi",
            "YFIUSDT": "yearn-finance",
            "BALUSDT": "balancer",
            "UMAUSDT": "uma",
            "RENUSDT": "republic-protocol",
            "KNCUSDT": "kyber-network-crystal",
            "ZRXUSDT": "0x",
            "BATUSDT": "basic-attention-token",
            "ENJUSDT": "enjin-coin",
            "MANAUSDT": "decentraland",
            "SANDUSDT": "the-sandbox",
            "AXSUSDT": "axie-infinity",
            "CHZUSDT": "chiliz",
            "HOTUSDT": "holotoken",
            "ANKRUSDT": "ankr",
            "GRTUSDT": "the-graph",
            "SKLUSDT": "skale",
            "CELRUSDT": "celer-network",
            "OCEANUSDT": "ocean-protocol",
            "BANDUSDT": "band-protocol",
            "LRCUSDT": "loopring",
            "CTSIUSDT": "cartesi",
            "REEFUSDT": "reef",
            "ALPHAUSDT": "alpha-finance",
        }
        return mapping.get(symbol, symbol.replace("USDT", "").lower())

# -----------------------------------------------------------------------------
# Pydantic Models
# -----------------------------------------------------------------------------
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"

class User(BaseModel):
    username: str
    hashed_password: str

class CoinSettings(BaseModel):
    symbol: str
    enabled: bool = True

class SettingsUpdate(BaseModel):
    coins: List[str] = []
    main_neural_dir: Optional[str] = None
    trade_start_level: Optional[int] = Field(None, ge=1, le=7)
    start_allocation_pct: Optional[float] = Field(None, ge=0, le=100)
    dca_multiplier: Optional[float] = Field(None, ge=0)
    max_dca_buys_per_24h: Optional[int] = Field(None, ge=0)
    pm_start_pct_no_dca: Optional[float] = Field(None, ge=0)
    pm_start_pct_with_dca: Optional[float] = Field(None, ge=0)
    trailing_gap_pct: Optional[float] = Field(None, ge=0)
    lth_profit_alloc_pct: Optional[float] = Field(None, ge=0, le=100)
    long_term_holdings: List[str] = []

class AlpacaCredentials(BaseModel):
    api_key: str
    secret_key: str

# -----------------------------------------------------------------------------
# Auth Helpers
# -----------------------------------------------------------------------------
def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# Custom auth dependency - checks cookie first, then Bearer
async def get_current_user_flexible(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme)
) -> str:
    cookie_token = request.cookies.get("access_token")
    check_token = cookie_token or token
    if not check_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = jwt.decode(check_token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(401, "Invalid token")
        return username
    except JWTError as e:
        logger.warning(f"JWT decode error: {e}")
        raise HTTPException(401, "Invalid token")

async def get_current_user(token: Optional[str] = Depends(oauth2_scheme)) -> str:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(401, "Invalid token")
        return username
    except JWTError:
        raise HTTPException(401, "Invalid token")

# -----------------------------------------------------------------------------
# User Store
# -----------------------------------------------------------------------------
USER_FILE = BOT_DIR / "web_user.json"

def load_user() -> Optional[User]:
    if USER_FILE.exists():
        data = json.loads(USER_FILE.read_text())
        return User(**data)
    return None

def save_user(user: User):
    USER_FILE.write_text(json.dumps(user.model_dump()))

def ensure_user_exists():
    if not USER_FILE.exists():
        import secrets
        default_pass = secrets.token_urlsafe(16)
        user = User(username="admin", hashed_password=get_password_hash(default_pass))
        save_user(user)
        print(f"\n{'='*60}")
        print(f"DEFAULT LOGIN CREATED")
        print(f"Username: admin")
        print(f"Password: {default_pass}")
        print(f"{'='*60}\n")

# -----------------------------------------------------------------------------
# Settings File Helpers
# -----------------------------------------------------------------------------
SETTINGS_FILE = BOT_DIR / "gui_settings.json"

def load_settings() -> Dict[str, Any]:
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text())
    return {
        "coins": ["BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "MATIC", "DOT", "AVAX", "LINK", "UNI", "LTC", "BCH", "ATOM", "NEAR", "ALGO", "VET", "ICP", "FIL", "THETA", "ETC", "XLM", "EOS", "AAVE", "MKR", "COMP", "SNX", "CRV", "SUSHI", "YFI", "BAL", "UMA", "REN", "KNC", "ZRX", "BAT", "ENJ", "MANA", "SAND", "AXS", "CHZ", "HOT", "ANKR", "GRT", "SKL", "CELR", "OCEAN", "BAND", "LRC", "CTSI", "REEF", "ALPHA"],
        "main_neural_dir": str(BOT_DIR),
        "trade_start_level": 3,
        "start_allocation_pct": 0.5,
        "dca_multiplier": 2.0,
        "dca_levels": [-2.5, -5.0, -10.0, -20.0, -30.0, -40.0, -50.0],
        "max_dca_buys_per_24h": 2,
        "pm_start_pct_no_dca": 5.0,
        "pm_start_pct_with_dca": 2.5,
        "trailing_gap_pct": 0.5,
        "lth_profit_alloc_pct": 0.0,
        "long_term_holdings": ["BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "MATIC"],
    }

def save_settings(settings: Dict[str, Any]):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))

# -----------------------------------------------------------------------------
# Bot Integration
# -----------------------------------------------------------------------------
bot_state = {
    "trader": None,
    "running": False,
    "training": {},
    "last_prices": {},
    "status_messages": [],
}

async def get_trader():
    if bot_state["trader"] is None:
        try:
            from pt_trader import CryptoAPITrading
            bot_state["trader"] = CryptoAPITrading()
        except Exception as e:
            logger.error(f"Failed to init trader: {e}")
            raise HTTPException(500, f"Trader init failed: {e}")
    return bot_state["trader"]

async def get_alpaca():
    try:
        from pt_alpaca import create_alpaca_trader
        return create_alpaca_trader(paper=True)
    except Exception as e:
        logger.error(f"Failed to init Alpaca: {e}")
        raise HTTPException(500, f"Alpaca init failed: {e}")

# -----------------------------------------------------------------------------
# Market Data Instance
# -----------------------------------------------------------------------------
market_data = CoinGeckoMarketData()

# -----------------------------------------------------------------------------
# WebSocket Manager (Extended for FOMO Alerts)
# -----------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []
        self.price_subscriptions: Dict[str, set] = {}
        self.fomo_alert_subscriptions: set = set()
        self._fomo_ws_task: Optional[asyncio.Task] = None
        self._fomo_client: Optional[FOMOClient] = None

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        if ws in self.fomo_alert_subscriptions:
            self.fomo_alert_subscriptions.remove(ws)

    async def broadcast(self, message: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast_prices(self, prices: dict):
        await self.broadcast({"type": "prices", "data": prices})

    async def broadcast_fomo_alert(self, alert: dict):
        """Broadcast FOMO alert to all connected clients"""
        message = {"type": "fomo_alert", "data": alert}
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def subscribe_fomo_alerts(self, ws: WebSocket):
        self.fomo_alert_subscriptions.add(ws)

    def unsubscribe_fomo_alerts(self, ws: WebSocket):
        self.fomo_alert_subscriptions.discard(ws)

    async def start_fomo_ws(self):
        """Start FOMO WebSocket connection and forward alerts"""
        if self._fomo_ws_task and not self._fomo_ws_task.done():
            return  # Already running
        
        async def fomo_ws_loop():
            client = FOMOClient()
            self._fomo_client = client
            while True:
                try:
                    logger.info("Connecting to FOMO WebSocket...")
                    async for msg in client.ws_connect():
                        # Normalize FOMO alert to internal format
                        normalized = self._normalize_fomo_alert(msg)
                        if normalized:
                            await self.broadcast_fomo_alert(normalized)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"FOMO WebSocket error: {e}")
                    # Broadcast connection status
                    await self.broadcast({
                        "type": "fomo_status",
                        "data": {"connected": False, "error": str(e)}
                    })
                    # Exponential backoff
                    backoff = 1
                    max_backoff = 60
                    while True:
                        logger.info(f"FOMO WS reconnecting in {backoff}s...")
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, max_backoff)
                        try:
                            # Try to reconnect by breaking inner loop
                            break
                        except:
                            continue
        
        self._fomo_ws_task = asyncio.create_task(fomo_ws_loop())

    async def stop_fomo_ws(self):
        """Stop FOMO WebSocket connection"""
        if self._fomo_ws_task:
            self._fomo_ws_task.cancel()
            try:
                await self._fomo_ws_task
            except asyncio.CancelledError:
                pass
        if self._fomo_client:
            await self._fomo_client.close()
            self._fomo_client = None

    def _normalize_fomo_alert(self, msg: dict) -> Optional[dict]:
        """Normalize FOMO WebSocket message to internal alert format"""
        try:
            # FOMO WS message format:
            # {'type': 'trade', 'trader': 'handle', 'token': {...}, 'chain': '...', 'action': 'buy/sell', ...}
            # {'type': 'thesis', 'trader': 'handle', 'token': {...}, 'thesis': '...', ...}
            # {'type': 'whale', 'trader': 'handle', 'token': {...}, 'value': ..., ...}
            
            msg_type = msg.get("type", "")
            if msg_type not in ("trade", "thesis", "whale", "buy", "sell", "price", "follow"):
                return None
            
            trader = msg.get("trader", "")
            token = msg.get("token", {})
            
            return {
                "timestamp": datetime.utcnow().isoformat(),
                "type": msg_type,
                "trader": trader,
                "token_symbol": token.get("symbol", "") if token else "",
                "token_address": token.get("address", "") if token else "",
                "chain": msg.get("chain", ""),
                "action": msg.get("action", msg_type),
                "source": msg.get("source", "feed"),
                "value_usd": msg.get("valueUsd", msg.get("sizeUsd", 0)),
                "price_usd": msg.get("priceUsd", 0),
                "thesis": msg.get("thesis", ""),
                "trade_id": msg.get("tradeId", ""),
                "pnl_usd": msg.get("pnlUsd", 0),
            }
        except Exception as e:
            logger.warning(f"Failed to normalize FOMO alert: {e}")
            return None

manager = ConnectionManager()

# -----------------------------------------------------------------------------
# Background Price Updater (REST polling + WebSocket)
# -----------------------------------------------------------------------------
async def price_updater():
    """Poll CoinGecko every 5s and broadcast"""
    while True:
        try:
            tickers = await market_data.get_all_tickers()
            prices = {}
            for t in tickers:
                if isinstance(t, dict) and t.get("symbol"):
                    sym = t.get("symbol", "").upper() + "USDT"
                    prices[sym] = {
                        "price": float(t.get("current_price", 0) or 0),
                        "change_24h": float(t.get("price_change_percentage_24h", 0) or 0),
                        "volume": float(t.get("total_volume", 0) or 0),
                        "high": float(t.get("high_24h", 0) or 0),
                        "low": float(t.get("low_24h", 0) or 0),
                        "ts": datetime.utcnow().isoformat(),
                    }
            if prices:
                await manager.broadcast({"type": "prices", "data": prices})
        except Exception as e:
            logger.error(f"Price update error: {e}")
        await asyncio.sleep(5)

# -----------------------------------------------------------------------------
# Lifespan
# -----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_user_exists()
    await market_data.get_session()  # Initialize aiohttp session
    await get_fomo_client()  # Initialize FOMO client
    price_task = asyncio.create_task(price_updater())
    await manager.start_fomo_ws()  # Start FOMO WebSocket
    yield
    price_task.cancel()
    try:
        await price_task
    except asyncio.CancelledError:
        pass
    await manager.stop_fomo_ws()  # Stop FOMO WebSocket
    await market_data.close()
    await close_fomo_client()  # Close FOMO client

# -----------------------------------------------------------------------------
# FastAPI App
# -----------------------------------------------------------------------------
app = FastAPI(title="PowerTrader AI", lifespan=lifespan)

# Static files and templates
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# -----------------------------------------------------------------------------
# Auth Routes
# -----------------------------------------------------------------------------
@app.post("/api/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = load_user()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(401, "Incorrect username or password")
    access_token = create_access_token({"sub": user.username})
    response = JSONResponse(content={"access_token": access_token, "token_type": "bearer"})
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
        samesite="lax"
    )
    return response

@app.get("/api/me")
async def me(user: str = Depends(get_current_user)):
    return {"username": user}

@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("access_token")
    return response

# -----------------------------------------------------------------------------
# API Routes - Market Data (CoinGecko)
# -----------------------------------------------------------------------------
@app.get("/api/markets")
async def get_markets(user: str = Depends(get_current_user_flexible)):
    """Get all market data for homepage"""
    tickers = await market_data.get_all_tickers()
    markets = []
    for t in tickers:
        markets.append({
            "symbol": t.get("symbol", "").upper() + "USDT",
            "price": float(t.get("current_price", 0)),
            "change_24h": float(t.get("price_change_percentage_24h", 0)),
            "volume": float(t.get("total_volume", 0)),
            "high": float(t.get("high_24h", 0)),
            "low": float(t.get("low_24h", 0)),
        })
    return {"markets": markets}

@app.get("/api/markets/{symbol}")
async def get_market_detail(symbol: str, user: str = Depends(get_current_user_flexible)):
    """Get detailed market data for a symbol"""
    ticker = await market_data.get_ticker_24h(symbol)
    if not ticker:
        raise HTTPException(404, "Symbol not found")
    
    # Get klines for chart
    klines = await market_data.get_klines(symbol, "1h", 200)
    
    return {
        "symbol": symbol,
        "price": float(ticker.get("market_data", {}).get("current_price", {}).get("usd", 0)),
        "change_24h": float(ticker.get("market_data", {}).get("price_change_percentage_24h", 0)),
        "volume": float(ticker.get("market_data", {}).get("total_volume", {}).get("usd", 0)),
        "high": float(ticker.get("market_data", {}).get("high_24h", {}).get("usd", 0)),
        "low": float(ticker.get("market_data", {}).get("low_24h", {}).get("usd", 0)),
        "open": float(ticker.get("market_data", {}).get("current_price", {}).get("usd", 0)) * (1 - float(ticker.get("market_data", {}).get("price_change_percentage_24h", 0)) / 100),
        "klines": klines,
    }

@app.get("/api/klines/{symbol}")
async def get_klines(symbol: str, interval: str = "1h", limit: int = 200, user: str = Depends(get_current_user_flexible)):
    klines = await market_data.get_klines(symbol, interval, limit)
    return {"symbol": symbol, "interval": interval, "klines": klines}

# -----------------------------------------------------------------------------
# Partial Routes (htmx)
# -----------------------------------------------------------------------------
@app.get("/partial/home", response_class=HTMLResponse)
async def partial_home(request: Request, user: str = Depends(get_current_user_flexible)):
    return templates.TemplateResponse(request, "partials/home.html", {})

@app.get("/partial/search", response_class=HTMLResponse)
async def partial_search(request: Request, user: str = Depends(get_current_user_flexible)):
    return templates.TemplateResponse(request, "partials/search.html", {})

@app.get("/partial/portfolio", response_class=HTMLResponse)
async def partial_portfolio(request: Request, user: str = Depends(get_current_user_flexible)):
    return templates.TemplateResponse(request, "partials/portfolio.html", {})

@app.get("/partial/feed", response_class=HTMLResponse)
async def partial_feed(request: Request, user: str = Depends(get_current_user_flexible)):
    return templates.TemplateResponse(request, "partials/feed.html", {})

@app.get("/partial/alerts", response_class=HTMLResponse)
async def partial_alerts(request: Request, user: str = Depends(get_current_user_flexible)):
    return templates.TemplateResponse(request, "partials/alerts.html", {})

# -----------------------------------------------------------------------------
# API Routes - Account & Trading
# -----------------------------------------------------------------------------
@app.get("/api/account")
async def get_account(user: str = Depends(get_current_user)):
    trader = await get_trader()
    try:
        return trader.get_account()
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/positions")
async def get_positions(user: str = Depends(get_current_user)):
    trader = await get_trader()
    try:
        return trader.get_positions()
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/orders/{symbol}")
async def get_orders(symbol: str, user: str = Depends(get_current_user)):
    trader = await get_trader()
    try:
        return trader.get_orders(symbol)
    except Exception as e:
        raise HTTPException(500, str(e))

# -----------------------------------------------------------------------------
# API Routes - Settings
# -----------------------------------------------------------------------------
@app.get("/api/settings")
async def get_settings(user: str = Depends(get_current_user_flexible)):
    settings = load_settings()
    try:
        alpaca = await get_alpaca()
        acct = alpaca.get_account()
        settings["alpaca_connected"] = True
        settings["alpaca_account"] = acct.get("account_number")
        settings["buying_power"] = acct.get("buying_power")
    except:
        settings["alpaca_connected"] = False
    return settings

@app.post("/api/settings")
async def update_settings(update: SettingsUpdate, user: str = Depends(get_current_user_flexible)):
    settings = load_settings()
    update_dict = update.model_dump(exclude_unset=True)
    if "coins" in update_dict:
        settings["coins"] = update_dict.pop("coins")
    settings.update(update_dict)
    save_settings(settings)
    return {"ok": True, "settings": settings}

# -----------------------------------------------------------------------------
# API Routes - Control
# -----------------------------------------------------------------------------
@app.post("/api/start")
async def start_bot(user: str = Depends(get_current_user_flexible)):
    if bot_state["running"]:
        return {"ok": True, "message": "Already running"}
    bot_state["running"] = True
    await manager.broadcast({"type": "status", "running": True})
    return {"ok": True, "message": "Bot started"}

@app.post("/api/stop")
async def stop_bot(user: str = Depends(get_current_user_flexible)):
    bot_state["running"] = False
    await manager.broadcast({"type": "status", "running": False})
    return {"ok": True, "message": "Bot stopped"}

@app.get("/api/status")
async def get_status(user: str = Depends(get_current_user_flexible)):
    return {
        "running": bot_state["running"],
        "training": bot_state["training"],
        "last_prices": bot_state["last_prices"],
    }

# -----------------------------------------------------------------------------
# API Routes - Alpaca Credentials
# -----------------------------------------------------------------------------
ALPACA_KEY_FILE = BOT_DIR / "alpaca_key.txt"
ALPACA_SECRET_FILE = BOT_DIR / "alpaca_secret.txt"
FOMO_KEY_FILE = BOT_DIR / "fomo_key.txt"

@app.get("/api/alpaca/status")
async def alpaca_status(user: str = Depends(get_current_user_flexible)):
    configured = ALPACA_KEY_FILE.exists() and ALPACA_SECRET_FILE.exists()
    connected = False
    account = None
    buying_power = 0
    if configured:
        try:
            alpaca = await get_alpaca()
            acct = alpaca.get_account()
            connected = True
            account = acct.get("account_number")
            buying_power = acct.get("buying_power")
        except:
            pass
    return {
        "configured": configured,
        "connected": connected,
        "account": account,
        "buying_power": buying_power,
    }

@app.post("/api/alpaca/credentials")
async def save_alpaca_credentials(creds: AlpacaCredentials, user: str = Depends(get_current_user_flexible)):
    ALPACA_KEY_FILE.write_text(creds.api_key.strip())
    ALPACA_SECRET_FILE.write_text(creds.secret_key.strip())
    bot_state["trader"] = None
    return {"ok": True, "message": "Credentials saved"}

# -----------------------------------------------------------------------------
# API Routes - FOMO Social Trading
# -----------------------------------------------------------------------------
@app.get("/api/fomo/status")
async def fomo_status(user: str = Depends(get_current_user_flexible)):
    """Check FOMO API configuration and connectivity"""
    configured = FOMO_KEY_FILE.exists()
    connected = False
    trader_count = 0
    error = None
    if configured:
        try:
            client = await get_fomo_client()
            lb = await client.get_leaderboard("24h", limit=1)
            connected = True
            trader_count = lb.count
        except Exception as e:
            error = str(e)
    return {
        "configured": configured,
        "connected": connected,
        "trader_count": trader_count,
        "error": error,
    }

@app.get("/api/fomo/leaderboard")
async def fomo_leaderboard(
    window: str = "24h",
    limit: int = 50,
    user: str = Depends(get_current_user_flexible)
):
    """Get FOMO leaderboard for a time window"""
    valid_windows = ["24h", "7d", "30d", "all"]
    if window not in valid_windows:
        raise HTTPException(400, f"Invalid window. Must be one of {valid_windows}")
    
    if window == "all":
        limit = min(limit, 100)
    else:
        limit = min(limit, 150)
    
    try:
        client = await get_fomo_client()
        lb = await client.get_leaderboard(window, limit)
        
        traders = []
        for t in lb.traders:
            traders.append({
                "rank": t.rank,
                "handle": t.handle,
                "display_name": t.display_name,
                "pnl_usd": t.pnl_usd,
                "volume_usd": t.volume_usd,
                "trades": t.trades,
                "followers": t.followers,
                "verified": t.verified,
                "holdings": t.holdings,
            })
        
        return {
            "window": lb.window,
            "source": lb.source,
            "captured_at": lb.captured_at,
            "count": lb.count,
            "traders": traders,
        }
    except Exception as e:
        logger.error(f"FOMO leaderboard error: {e}")
        raise HTTPException(500, f"FOMO API error: {str(e)}")

@app.get("/api/fomo/trader/{handle}")
async def fomo_trader_profile(
    handle: str,
    user: str = Depends(get_current_user_flexible)
):
    """Get detailed trader profile"""
    try:
        client = await get_fomo_client()
        trader = await client.get_trader(handle)
        
        return {
            "handle": trader.handle,
            "display_name": trader.display_name,
            "pnl_usd": trader.pnl_usd,
            "pnl_by_window": trader.pnl_by_window,
            "volume_usd": trader.volume_usd,
            "trades": trader.trades,
            "followers": trader.followers,
            "verified": trader.verified,
            "holdings": trader.holdings,
            "wallets": trader.wallets,
            "account_age_days": trader.account_age_days,
            "created_at": trader.created_at,
            "average_hold_time_seconds": trader.average_hold_time_seconds,
            "clan": trader.clan,
        }
    except Exception as e:
        logger.error(f"FOMO trader profile error: {e}")
        raise HTTPException(500, f"FOMO API error: {str(e)}")

@app.get("/api/fomo/trader/{handle}/trades")
async def fomo_trader_trades(
    handle: str,
    limit: int = 25,
    deep: bool = False,
    user: str = Depends(get_current_user_flexible)
):
    """Get trader's trade history"""
    try:
        client = await get_fomo_client()
        trades = await client.get_trades(handle, limit=limit, deep=deep)
        
        result = []
        for t in trades:
            result.append({
                "trade_id": t.trade_id,
                "token_symbol": t.token_symbol,
                "token_address": t.token_address,
                "status": t.status,
                "avg_entry_price": t.avg_entry_price,
                "avg_exit_price": t.avg_exit_price,
                "realized_pnl_usd": t.realized_pnl_usd,
                "unrealized_pnl_usd": t.unrealized_pnl_usd,
                "created_at": t.created_at,
                "closed_at": t.closed_at,
                "chain": t.chain,
            })
        
        return {"trades": result}
    except Exception as e:
        logger.error(f"FOMO trades error: {e}")
        raise HTTPException(500, f"FOMO API error: {str(e)}")

@app.get("/api/fomo/trader/{handle}/holdings")
async def fomo_trader_holdings(
    handle: str,
    chain: Optional[str] = None,
    user: str = Depends(get_current_user_flexible)
):
    """Get trader's current holdings"""
    try:
        client = await get_fomo_client()
        holdings = await client.get_holdings(handle, chain=chain)
        
        result = []
        for h in holdings:
            result.append({
                "token_symbol": h.token_symbol,
                "token_address": h.token_address,
                "network_id": h.network_id,
                "chain": h.chain,
                "amount": h.amount,
                "total_value_usd": h.total_value_usd,
            })
        
        return {"holdings": result}
    except Exception as e:
        logger.error(f"FOMO holdings error: {e}")
        raise HTTPException(500, f"FOMO API error: {str(e)}")

@app.get("/api/fomo/trader/{handle}/wallets")
async def fomo_trader_wallets(
    handle: str,
    user: str = Depends(get_current_user_flexible)
):
    """Get trader's wallet addresses"""
    try:
        client = await get_fomo_client()
        wallets = await client.get_wallets(handle)
        return {"wallets": wallets}
    except Exception as e:
        logger.error(f"FOMO wallets error: {e}")
        raise HTTPException(500, f"FOMO API error: {str(e)}")

# -----------------------------------------------------------------------------
# WebSocket
# -----------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Send initial state
        await websocket.send_json({
            "type": "init",
            "running": bot_state["running"],
        })
        # Keep alive
        while True:
            data = await websocket.receive_text()
            # Handle subscription messages
            try:
                msg = json.loads(data)
                if msg.get("type") == "subscribe":
                    symbols = msg.get("symbols", [])
                    for sym in symbols:
                        manager.price_subscriptions.setdefault(sym, set()).add(websocket)
                elif msg.get("type") == "fomo_subscribe":
                    manager.subscribe_fomo_alerts(websocket)
                elif msg.get("type") == "fomo_unsubscribe":
                    manager.unsubscribe_fomo_alerts(websocket)
            except:
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.get("/api/fomo/ws/status")
async def fomo_ws_status(user: str = Depends(get_current_user_flexible)):
    """Check FOMO WebSocket connection status"""
    return {
        "connected": manager._fomo_client is not None,
        "ws_task_running": manager._fomo_ws_task is not None and not manager._fomo_ws_task.done() if manager._fomo_ws_task else False,
    }

# -----------------------------------------------------------------------------
# HTML Routes
# -----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        return RedirectResponse("/login")
    try:
        jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return templates.TemplateResponse(request, "index.html", {})
    except JWTError:
        return RedirectResponse("/login")

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT)