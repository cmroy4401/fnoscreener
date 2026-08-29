import os
import json
import asyncio
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
LAST_AUTH_ERROR = "No token"
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
        except Exception as e:
            logging.warning(f"Symbol load failed: {e}")

def get_token():
    global LIVE_TOKEN, LAST_AUTH_ERROR
    
    # Try file first
    if os.path.exists("access_token.txt"):
        try:
            with open("access_token.txt") as f:
                t = f.read().strip()
                if len(t) > 30:
                    LIVE_TOKEN = t
                    LAST_AUTH_ERROR = "✓ Token loaded"
                    logging.info("✓ Token from file")
                    return True
        except Exception:
            pass
    
    # Try environment
    env_token = os.environ.get("access_token", "").strip()
    if env_token and len(env_token) > 30:
        LIVE_TOKEN = env_token
        LAST_AUTH_ERROR = "✓ Token from env"
        logging.info("✓ Token from environment")
        return True
    
    LAST_AUTH_ERROR = "No token available"
    return False

def build_snapshot():
    for idx_name, (sym, ltp, chg) in INDEX_MAP.items():
        GLOBAL_DATA["data"][sym] = {"ltp": ltp, "change_pct": chg}
        GLOBAL_DATA["index_signals"][idx_name] = {
            "ltp": ltp, "change_pct": chg, "final_signal": "WAIT"
        }

    stock_rows = []
    for item in ALL_STOCKS_LIST[:50]:
        sym = item.get("symbol")
        ticker = item.get("name", sym.replace("NSE:", ""))
        stock_rows.append([ticker, "F&O", 0.0, 1000.0, "1.5L", "Range", 50, "WAIT", "1000", 150000, 1000, 52, "WAIT", "-", "-"])
    
    GLOBAL_DATA["stocks"] = stock_rows

async def market_worker():
    while True:
        try:
            build_snapshot()
            
            if connected_clients and GLOBAL_DATA.get("stocks"):
                payload = json.dumps(GLOBAL_DATA)
                for ws in list(connected_clients):
                    try:
                        await ws.send_text(payload)
                    except Exception as e:
                        connected_clients.discard(ws)
        except Exception as e:
            logging.error(f"Worker error: {e}")
        
        await asyncio.sleep(2)

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
        connected_clients.discard(websocket)

@app.get("/api/status")
def status():
    return {
        "status": "online",
        "authenticated": bool(LIVE_TOKEN),
        "auth_message": LAST_AUTH_ERROR,
        "total_fno_stocks": len(ALL_STOCKS_LIST),
        "connected_clients": len(connected_clients)
    }

@app.get("/journal")
def get_journal():
    return {"signals": []}

@app.get("/")
def serve_app():
    for name in ["FNO_AI_Screener.html", "index.html"]:
        if os.path.exists(name):
            with open(name, encoding="utf-8") as f:
                return HTMLResponse(f.read())
    return HTMLResponse("<h1>FNO Engine Online</h1>")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port)
