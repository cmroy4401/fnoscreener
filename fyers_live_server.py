import os
import json
import asyncio
import requests
import logging
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

try:
    import smc_ict_engine as engine
except ImportError:
    import sys
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    import smc_ict_engine as engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

LIVE_TOKEN = ""
LAST_AUTH_ERROR = "Initializing..."
LAST_QUOTES_STATUS = "Initializing..."
connected_clients = set()
LATEST_QUOTES_MAP = {}

INDEX_MAP = {
    "NIFTY": ("NSE:NIFTY50-INDEX", 24850.50, 0.42),
    "BANKNIFTY": ("NSE:NIFTYBANK-INDEX", 51320.10, 0.65),
    "FINNIFTY": ("NSE:FINNIFTY-INDEX", 23410.80, 0.51),
    "SENSEX": ("BSE:SENSEX-INDEX", 81550.25, 0.38)
}

ALL_STOCKS_LIST = []

GLOBAL_DATA = {
    "stocks": [],
    "data": {},
    "candidates": {},
    "index_signals": {},
    "index_option_journal": []
}

def load_fno_symbols():
    global ALL_STOCKS_LIST
    if os.path.exists("fyers_symbols.json"):
        try:
            with open("fyers_symbols.json") as f:
                ALL_STOCKS_LIST = json.load(f).get("stocks", [])
                logging.info(f"✓ Loaded {len(ALL_STOCKS_LIST)} symbols")
                return
        except Exception as e:
            logging.warning(f"Symbol load failed: {e}")
    logging.warning("⚠️ No FNO symbols loaded")

def get_token():
    global LIVE_TOKEN, LAST_AUTH_ERROR
    
    # Try file first
    if os.path.exists("access_token.txt"):
        try:
            with open("access_token.txt") as f:
                t = f.read().strip()
                if len(t) > 30:
                    LIVE_TOKEN = t
                    LAST_AUTH_ERROR = "✓ Token from file"
                    logging.info("✓ Token loaded from file")
                    return True
        except Exception as e:
            logging.warning(f"File read failed: {e}")
    
    # Try environment variable
    env_token = os.environ.get("access_token", "").strip()
    if env_token and len(env_token) > 30:
        LIVE_TOKEN = env_token
        LAST_AUTH_ERROR = "✓ Token from environment"
        logging.info("✓ Token loaded from environment")
        return True
    
    LAST_AUTH_ERROR = "❌ No token available"
    logging.error("No token found")
    return False

def fetch_quotes(symbols, client_id, token):
    """Fetch quotes from Fyers API"""
    if not symbols or not token or not client_id:
        return []
    
    try:
        app_id_full = client_id if "-100" in client_id else f"{client_id}-100"
        sym_str = ",".join(symbols)
        headers = {"Authorization": f"{app_id_full}:{token}", "User-Agent": "Mozilla/5.0"}
        
        r = requests.get(
            "https://api-t1.fyers.in/data/quotes",
            params={"symbols": sym_str},
            headers=headers,
            timeout=5
        )
        
        if r.status_code == 200:
            try:
                data = r.json()
                if data.get("s") == "ok" and data.get("d"):
                    logging.debug(f"✓ Fetched {len(data.get('d', []))} quotes")
                    return data.get("d", [])
            except Exception as e:
                logging.warning(f"JSON parse error: {e}")
        elif r.status_code in [401, 403]:
            logging.warning("⚠️ Token unauthorized - may be expired")
        else:
            logging.warning(f"API returned {r.status_code}")
    except Exception as e:
        logging.warning(f"Quotes fetch error: {e}")
    
    return []

def fmt_vol(v):
    """Format volume for display"""
    if v >= 10000000: return f"{v/10000000:.2f}Cr"
    if v >= 100000: return f"{v/100000:.2f}L"
    if v >= 1000: return f"{v/1000:.1f}K"
    return str(int(v)) if v else "—"

def build_snapshot(raw_quotes=None):
    """Build complete data snapshot"""
    global GLOBAL_DATA, LATEST_QUOTES_MAP
    
    # Update quotes map
    if raw_quotes and isinstance(raw_quotes, list):
        for item in raw_quotes:
            sym = item.get("n", "")
            v = item.get("v", {})
            lp = float(v.get("lp", 0) or v.get("prev_close_price", 0) or 0)
            chp = float(v.get("chp", 0.0))
            vol = int(v.get("volume", 0))
            
            if lp > 0:
                LATEST_QUOTES_MAP[sym] = {
                    "ltp": lp,
                    "change_pct": chp,
                    "volume": vol,
                    "open": float(v.get("open_price", lp)),
                    "high": float(v.get("high_price", lp)),
                    "low": float(v.get("low_price", lp))
                }

    # Build index signals
    for idx_name, (sym, def_ltp, def_chg) in INDEX_MAP.items():
        q = LATEST_QUOTES_MAP.get(sym)
        ltp = q["ltp"] if q else def_ltp
        chg = q["change_pct"] if q else def_chg
        
        GLOBAL_DATA["data"][sym] = {"ltp": ltp, "change_pct": chg}
        
        idx_sig = "BUY" if chg > 0.25 else ("SELL" if chg < -0.25 else "WAIT")
        GLOBAL_DATA["index_signals"][idx_name] = {
            "ltp": ltp,
            "change_pct": chg,
            "final_signal": idx_sig
        }

    # Build stock rows
    stock_rows = []
    for item in ALL_STOCKS_LIST:
        sym = item.get("symbol")
        ticker = item.get("name", sym.replace("NSE:", "").replace("-EQ", ""))
        sector = item.get("sector", "F&O")
        
        q = LATEST_QUOTES_MAP.get(sym)
        ltp = q["ltp"] if q else 1000.0
        chg = q["change_pct"] if q else 0.0
        vol = q["volume"] if q else 100000
        op = q["open"] if q else ltp * 0.995
        hi = q["high"] if q else ltp * 1.01
        lo = q["low"] if q else ltp * 0.99

        # Run analysis
        try:
            res = engine.analyze([{"open": op, "high": hi, "low": lo, "close": ltp, "volume": vol}], day_vol=vol)
            sig = res.get("final_signal", "BUY" if chg > 0.8 else ("SELL" if chg < -0.8 else "WAIT"))
            score = res.get("buy_score", 0) if sig == "BUY" else (res.get("sell_score", 0) if sig == "SELL" else 50)
        except Exception:
            res = {}
            sig = "WAIT"
            score = 50

        stock_rows.append([
            ticker, sector, chg, ltp, fmt_vol(vol), res.get("smc_label", "Range"),
            score, sig, str(round(ltp, 2)), vol, round((hi + lo + ltp) / 3, 2), 52,
            sig, res.get("ict_label", "-"), res.get("evidence", "-")
        ])

    GLOBAL_DATA["stocks"] = stock_rows

async def market_worker():
    """Main worker loop"""
    global LAST_QUOTES_STATUS, LIVE_TOKEN
    
    while True:
        try:
            # Load token if not set
            if not LIVE_TOKEN:
                get_token()

            cfg_file = {}
            if os.path.exists("fyers_config.json"):
                try:
                    with open("fyers_config.json") as f:
                        cfg_file = json.load(f)
                except Exception:
                    pass

            client_id = cfg_file.get("client_id") or os.environ.get("client_id", "")

            if LIVE_TOKEN and client_id and ALL_STOCKS_LIST:
                # Fetch quotes
                index_symbols = [v[0] for v in INDEX_MAP.values()]
                stock_symbols = [s.get("symbol") for s in ALL_STOCKS_LIST if s.get("symbol")]
                full_symbols = index_symbols + stock_symbols

                logging.info(f"📊 Fetching {len(full_symbols)} quotes...")
                
                # Fetch in batches
                batch_size = 40
                all_quotes = []
                for i in range(0, len(full_symbols), batch_size):
                    batch = full_symbols[i:i+batch_size]
                    quotes = fetch_quotes(batch, client_id, LIVE_TOKEN)
                    if isinstance(quotes, list):
                        all_quotes.extend(quotes)

                if all_quotes:
                    LAST_QUOTES_STATUS = f"✓ {len(all_quotes)}/{len(full_symbols)} quotes"
                    build_snapshot(all_quotes)
                    logging.info(f"✓ Snapshot built with {len(all_quotes)} quotes")
                else:
                    LAST_QUOTES_STATUS = "⚠️ No quotes received"
                    build_snapshot()
            else:
                LAST_QUOTES_STATUS = "⚠️ Missing credentials or symbols"
                build_snapshot()

        except Exception as e:
            LAST_QUOTES_STATUS = f"❌ Error: {str(e)}"
            logging.error(f"Worker error: {e}")
            build_snapshot()

        # Broadcast to connected clients
        if connected_clients and GLOBAL_DATA.get("stocks"):
            payload = json.dumps(GLOBAL_DATA)
            for ws in list(connected_clients):
                try:
                    await ws.send_text(payload)
                except Exception as e:
                    logging.warning(f"Send error: {e}")
                    connected_clients.discard(ws)

        await asyncio.sleep(30)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("🚀 FNO Scanner starting...")
    load_fno_symbols()
    get_token()
    build_snapshot()
    
    worker_task = asyncio.create_task(market_worker())
    logging.info("✓ Market worker started")
    
    yield
    
    worker_task.cancel()
    logging.info("🛑 FNO Scanner stopped")

app = FastAPI(title="FNO Engine", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)
    logging.info(f"✓ WebSocket connected (total: {len(connected_clients)})")
    
    try:
        if GLOBAL_DATA.get("stocks"):
            await websocket.send_text(json.dumps(GLOBAL_DATA))
        
        while True:
            await asyncio.sleep(10)
    except (WebSocketDisconnect, Exception) as e:
        logging.warning(f"WebSocket error: {e}")
        connected_clients.discard(websocket)
        logging.info(f"Client disconnected (total: {len(connected_clients)})")

@app.get("/api/status")
def status():
    return {
        "status": "online",
        "authenticated": bool(LIVE_TOKEN),
        "auth_message": LAST_AUTH_ERROR,
        "quotes_status": LAST_QUOTES_STATUS,
        "total_fno_stocks": len(ALL_STOCKS_LIST),
        "connected_clients": len(connected_clients)
    }

@app.get("/api/refresh_token")
def refresh_token():
    result = get_token()
    return {"success": result, "message": LAST_AUTH_ERROR}

@app.get("/journal")
def get_journal():
    return {"signals": GLOBAL_DATA.get("index_option_journal", [])}

@app.get("/")
def serve_app():
    for name in ["FNO_AI_Screener.html", "index.html"]:
        if os.path.exists(name):
            with open(name, encoding="utf-8") as f:
                return HTMLResponse(f.read())
    return HTMLResponse("<h1>FNO Engine Online</h1><p>Status: " + LAST_AUTH_ERROR + "</p>")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8765))
    logging.info(f"Starting on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
