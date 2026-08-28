import os
import json
import base64
import asyncio
import pyotp
import requests
import logging
from datetime import datetime
from collections import defaultdict
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

try:
    from fyers_apiv3 import fyersModel
except ImportError:
    fyersModel = None

try:
    import smc_ict_engine as engine
except ImportError:
    import sys
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    import smc_ict_engine as engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

LIVE_TOKEN = ""
LAST_AUTH_ERROR = "None"
LAST_QUOTES_STATUS = "Initializing..."
LAST_AUTH_TIME = 0
AUTH_COOLDOWN_SEC = 5
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
    "index_option_journal": [],
    "signals": []
}

def load_fno_symbols():
    global ALL_STOCKS_LIST
    if os.path.exists("fyers_symbols.json"):
        try:
            with open("fyers_symbols.json") as f:
                data = json.load(f)
                ALL_STOCKS_LIST = data.get("stocks", [])
                logging.info(f"✓ Loaded {len(ALL_STOCKS_LIST)} symbols")
                return
        except Exception as e:
            logging.warning(f"Failed to load symbols: {e}")
    
    logging.warning("⚠️ No FNO symbols loaded")
    ALL_STOCKS_LIST = []

def safe_parse(resp_text):
    try:
        txt = resp_text.strip()
        if txt.startswith("{") and txt.endswith("}"):
            return json.loads(txt)
        s, e = txt.find("{"), txt.rfind("}")
        if s != -1 and e != -1 and e > s:
            return json.loads(txt[s:e + 1])
    except Exception:
        pass
    return {}

def get_credentials():
    cfg = {}
    
    # Try file first
    if os.path.exists("fyers_config.json"):
        try:
            with open("fyers_config.json") as f:
                cfg = json.load(f)
                logging.info("✓ Loaded config from file")
        except Exception as e:
            logging.warning(f"Failed to load config file: {e}")
    
    # Fall back to environment variables
    return {
        "client_id": (cfg.get("client_id") or os.environ.get("client_id", "")).strip(),
        "secret_key": (cfg.get("secret_key") or os.environ.get("secret_key", "")).strip(),
        "fy_id": (cfg.get("fy_id") or os.environ.get("fy_id", "")).strip(),
        "pin": (cfg.get("pin") or os.environ.get("pin", "")).strip(),
        "totp_key": (cfg.get("totp_key") or os.environ.get("totp_key", "")).strip().replace(" ", ""),
        "redirect_uri": (cfg.get("redirect_uri") or os.environ.get("redirect_uri", "https://trade.fyers.in/api-login/redirect-uri/index.html")).strip()
    }

def auto_generate_token(force_fresh=False):
    global LIVE_TOKEN, LAST_AUTH_ERROR, LAST_AUTH_TIME
    now = datetime.now().timestamp()
    if now - LAST_AUTH_TIME < AUTH_COOLDOWN_SEC:
        return False
    LAST_AUTH_TIME = now

    if not force_fresh and os.path.exists("access_token.txt"):
        try:
            with open("access_token.txt") as f:
                t = f.read().strip()
                if len(t) > 30:
                    LIVE_TOKEN = t
                    LAST_AUTH_ERROR = "✓ Loaded saved token"
                    logging.info("✓ Using cached token")
                    return True
        except Exception:
            pass

    try:
        cfg = get_credentials()
        app_id, secret_id, fy_id, pin, totp_key, redirect_uri = (
            cfg["client_id"], cfg["secret_key"], cfg["fy_id"], cfg["pin"], cfg["totp_key"], cfg["redirect_uri"]
        )

        if not all([app_id, secret_id, fy_id, pin, totp_key]):
            LAST_AUTH_ERROR = "❌ Credentials missing"
            logging.error(f"Missing: client_id={bool(app_id)}, secret={bool(secret_id)}, fy_id={bool(fy_id)}, pin={bool(pin)}, totp={bool(totp_key)}")
            return False

        logging.info("🔐 Attempting fresh token generation...")
        totp = pyotp.TOTP(totp_key).now()
        session = requests.Session()
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Content-Type": "application/json"}

        # Step 1: Send OTP request
        fy_b64 = base64.b64encode(fy_id.encode("utf-8")).decode("utf-8")
        r1 = session.post("https://api.fyers.in/vagator/v2/send_login_otp", json={"fy_id": fy_b64, "app_id": "2"}, headers=headers, timeout=10)
        res1 = safe_parse(r1.text)
        req_key = res1.get("request_key")
        
        if not req_key:
            r1 = session.post("https://api-t1.fyers.in/vagator/v2/send_login_otp", json={"fy_id": fy_id, "app_id": "2"}, headers=headers, timeout=10)
            res1 = safe_parse(r1.text)
            req_key = res1.get("request_key")

        if not req_key:
            LAST_AUTH_ERROR = f"Step 1 Failed"
            logging.error(f"Step 1 error: {r1.text[:100]}")
            return False

        # Step 2: Verify OTP
        r2 = session.post("https://api.fyers.in/vagator/v2/verify_otp", json={"request_key": req_key, "otp": str(totp)}, headers=headers, timeout=10)
        res2 = safe_parse(r2.text)
        req_key2 = res2.get("request_key")
        
        if not req_key2:
            r2 = session.post("https://api-t1.fyers.in/vagator/v2/verify_otp", json={"request_key": req_key, "otp": str(totp)}, headers=headers, timeout=10)
            res2 = safe_parse(r2.text)
            req_key2 = res2.get("request_key")

        if not req_key2:
            LAST_AUTH_ERROR = f"Step 2 TOTP Failed"
            logging.error(f"Step 2 error: {r2.text[:100]}")
            return False

        # Step 3: Verify PIN
        pin_b64 = base64.b64encode(pin.encode("utf-8")).decode("utf-8")
        r3 = session.post("https://api.fyers.in/vagator/v2/verify_pin", json={"request_key": req_key2, "identity_type": "pin", "identifier": pin_b64}, headers=headers, timeout=10)
        res3 = safe_parse(r3.text)
        token_bearer = res3.get("data", {}).get("token_result", {}).get("token")
        
        if not token_bearer:
            r3 = session.post("https://api.fyers.in/vagator/v2/verify_pin", json={"request_key": req_key2, "identity_type": "pin", "identifier": pin}, headers=headers, timeout=10)
            res3 = safe_parse(r3.text)
            token_bearer = res3.get("data", {}).get("token_result", {}).get("token")

        if not token_bearer:
            LAST_AUTH_ERROR = f"Step 3 PIN Failed"
            logging.error(f"Step 3 error: {r3.text[:100]}")
            return False

        # Step 4: Get Access Token
        headers["Authorization"] = f"Bearer {token_bearer}"
        clean_app_id = app_id.split("-")[0] if "-" in app_id else app_id
        payload = {"fyers_id": fy_id, "app_id": clean_app_id, "redirect_uri": redirect_uri, "appType": "100", "code_challenge": "", "state": "None", "scope": "", "nonce": "", "response_type": "code", "create_cookie": True}
        r4 = session.post("https://api-t1.fyers.in/api/v3/token", json=payload, headers=headers, timeout=10)
        res4 = safe_parse(r4.text)
        url_val = res4.get("Url", "")
        
        if "auth_code=" not in url_val:
            LAST_AUTH_ERROR = f"Step 4 AuthCode Failed"
            logging.error(f"Step 4 error: {r4.text[:100]}")
            return False

        auth_code = url_val.split("auth_code=")[1].split("&")[0]

        if fyersModel:
            fyers_session = fyersModel.SessionModel(client_id=app_id, secret_key=secret_id, redirect_uri=redirect_uri, response_type="code", grant_type="authorization_code")
            fyers_session.set_token(auth_code)
            resp = fyers_session.generate_token()
            if "access_token" in resp:
                LIVE_TOKEN = resp["access_token"]
                with open("access_token.txt", "w") as f:
                    f.write(LIVE_TOKEN)
                LAST_AUTH_ERROR = "✓ Fresh token generated"
                logging.info("✓ Token generated successfully")
                return True
            else:
                LAST_AUTH_ERROR = f"SDK generate_token Failed"
                logging.error(f"SDK error: {resp}")
        else:
            logging.warning("⚠️ fyersModel not available, using manual token flow")
            LAST_AUTH_ERROR = "fyersModel not available"
            return False

    except Exception as e:
        LAST_AUTH_ERROR = f"Auth Exception: {str(e)}"
        logging.error(f"Auth error: {e}")
    
    return False

def query_single_batch(symbols, client_id, token):
    if not symbols or not token:
        return []
    app_id_full = client_id if "-100" in client_id else f"{client_id}-100"
    sym_str = ",".join(symbols)
    headers = {"Authorization": f"{app_id_full}:{token}", "User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get("https://api-t1.fyers.in/data/quotes", params={"symbols": sym_str}, headers=headers, timeout=4)
        if r.status_code == 200:
            data = safe_parse(r.text)
            if data.get("s") == "ok" and data.get("d"):
                logging.debug(f"✓ Fetched {len(data.get('d', []))} quotes")
                return data.get("d")
        elif r.status_code in [401, 403]:
            logging.warning("⚠️ Token expired")
            if os.path.exists("access_token.txt"):
                os.remove("access_token.txt")
            return "EXPIRED"
        else:
            logging.warning(f"Quotes API returned {r.status_code}")
    except Exception as e:
        logging.warning(f"Quotes fetch error: {e}")
    
    return []

async def fetch_all_quotes_parallel(all_symbols, client_id, token, chunk_size=40):
    if not all_symbols:
        return []
    
    chunks = [all_symbols[i:i + chunk_size] for i in range(0, len(all_symbols), chunk_size)]
    tasks = [asyncio.to_thread(query_single_batch, chunk, client_id, token) for chunk in chunks]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    combined = []
    has_expired = False
    
    for res in results:
        if res == "EXPIRED":
            has_expired = True
        elif isinstance(res, list) and res:
            combined.extend(res)
        elif isinstance(res, Exception):
            logging.warning(f"Batch error: {res}")
    
    if has_expired and not combined:
        return "EXPIRED"
    
    return combined

def fmt_vol(v):
    if v >= 10000000: return f"{v/10000000:.2f}Cr"
    if v >= 100000: return f"{v/100000:.2f}L"
    if v >= 1000: return f"{v/1000:.1f}K"
    return str(int(v)) if v else "—"

def build_snapshot(raw_quotes=None):
    global GLOBAL_DATA, LATEST_QUOTES_MAP
    if raw_quotes and isinstance(raw_quotes, list):
        for item in raw_quotes:
            sym = item.get("n", "")
            v = item.get("v", {})
            lp = float(v.get("lp", 0) or v.get("prev_close_price", 0) or v.get("open_price", 0))
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
        
        step = 100 if idx_name == "BANKNIFTY" else 50
        atm = round(ltp / step) * step
        
        ce_res = engine.analyze_option(
            [{"open": ltp*0.005, "high": ltp*0.007, "low": ltp*0.0048, "close": ltp*0.0065, "volume": 150000}],
            index_signal=idx_sig
        )
        pe_res = engine.analyze_option(
            [{"open": ltp*0.006, "high": ltp*0.0062, "low": ltp*0.0045, "close": ltp*0.0048, "volume": 90000}],
            index_signal=idx_sig
        )
        
        GLOBAL_DATA["candidates"][idx_name] = {
            "CE": {**ce_res, "strike": atm - step},
            "PE": {**pe_res, "strike": atm + step}
        }

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

        res = engine.analyze([{"open": op, "high": hi, "low": lo, "close": ltp, "volume": vol}], day_vol=vol)
        sig = res.get("final_signal", "BUY" if chg > 0.8 else ("SELL" if chg < -0.8 else "WAIT"))
        score = res.get("buy_score", 0) if sig == "BUY" else (res.get("sell_score", 0) if sig == "SELL" else 50)

        stock_rows.append([
            ticker, sector, chg, ltp, fmt_vol(vol), res.get("smc_label", "Range"),
            score, sig, str(round(ltp, 2)), vol, round((hi + lo + ltp) / 3, 2), 52,
            sig, res.get("ict_label", "-"), res.get("evidence", "-")
        ])

    GLOBAL_DATA["stocks"] = stock_rows

async def market_worker():
    global LAST_QUOTES_STATUS, LIVE_TOKEN
    
    while True:
        try:
            if not LIVE_TOKEN:
                auto_generate_token(force_fresh=False)

            cfg = get_credentials()
            client_id = cfg.get("client_id")

            if LIVE_TOKEN and client_id:
                index_symbols = [v[0] for v in INDEX_MAP.values()]
                stock_symbols = [s.get("symbol") for s in ALL_STOCKS_LIST if s.get("symbol")]
                full_symbols = index_symbols + stock_symbols

                logging.info(f"📊 Fetching {len(full_symbols)} quotes...")
                res = await fetch_all_quotes_parallel(full_symbols, client_id, LIVE_TOKEN, chunk_size=40)
                
                if res == "EXPIRED":
                    LAST_QUOTES_STATUS = "⚠️ Token expired - regenerating..."
                    auto_generate_token(force_fresh=True)
                    build_snapshot()
                elif isinstance(res, list) and len(res) > 0:
                    LAST_QUOTES_STATUS = f"✓ {len(res)}/{len(full_symbols)} quotes"
                    build_snapshot(res)
                    logging.info(f"✓ Built snapshot with {len(res)} quotes")
                else:
                    LAST_QUOTES_STATUS = "⚠️ No quotes - check credentials"
                    build_snapshot()
            else:
                LAST_QUOTES_STATUS = "⚠️ No token - auth pending"
                build_snapshot()

        except Exception as e:
            LAST_QUOTES_STATUS = f"❌ Worker error: {str(e)}"
            logging.error(LAST_QUOTES_STATUS)
            build_snapshot()

        if connected_clients and GLOBAL_DATA.get("stocks"):
            payload = {
                "stocks": GLOBAL_DATA.get("stocks", []),
                "data": GLOBAL_DATA.get("data", {}),
                "candidates": GLOBAL_DATA.get("candidates", {}),
                "index_signals": GLOBAL_DATA.get("index_signals", {}),
                "index_option_journal": GLOBAL_DATA.get("index_option_journal", [])
            }
            text = json.dumps(payload)
            
            for ws in list(connected_clients):
                try:
                    await ws.send_text(text)
                except Exception as e:
                    logging.warning(f"WS send error: {e}")
                    connected_clients.discard(ws)

        await asyncio.sleep(2)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("🚀 FNO Scanner starting...")
    load_fno_symbols()
    auto_generate_token(force_fresh=False)
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
    logging.info(f"✓ WebSocket client connected (total: {len(connected_clients)})")
    
    try:
        if GLOBAL_DATA.get("stocks"):
            payload = {
                "stocks": GLOBAL_DATA.get("stocks", []),
                "data": GLOBAL_DATA.get("data", {}),
                "candidates": GLOBAL_DATA.get("candidates", {}),
                "index_signals": GLOBAL_DATA.get("index_signals", {}),
                "index_option_journal": GLOBAL_DATA.get("index_option_journal", [])
            }
            await websocket.send_text(json.dumps(payload))
        
        while True:
            await asyncio.sleep(10)
    except (WebSocketDisconnect, Exception) as e:
        logging.warning(f"WebSocket disconnected: {e}")
        connected_clients.discard(websocket)
        logging.info(f"✓ Client removed (total: {len(connected_clients)})")

@app.get("/api/status")
def status():
    return {
        "status": "online",
        "authenticated": bool(LIVE_TOKEN),
        "auth_message": LAST_AUTH_ERROR,
        "quotes_status": LAST_QUOTES_STATUS,
        "total_fno_stocks": len(ALL_STOCKS_LIST),
        "connected_clients": len(connected_clients),
        "config_found": bool(get_credentials().get("client_id"))
    }

@app.get("/api/refresh_token")
def refresh_token():
    success = auto_generate_token(force_fresh=True)
    return {"success": success, "message": LAST_AUTH_ERROR}

@app.get("/journal")
def get_journal():
    return {"signals": GLOBAL_DATA.get("index_option_journal", [])}

@app.get("/")
def serve_app():
    for name in ["FNO_AI_Screener.html", "index.html"]:
        if os.path.exists(name):
            with open(name, encoding="utf-8") as f:
                return HTMLResponse(f.read())
    
    return HTMLResponse("""
    <h1>FNO Engine Online</h1>
    <p>Status: """ + LAST_AUTH_ERROR + """</p>
    <p>Quotes: """ + LAST_QUOTES_STATUS + """</p>
    <p><a href="/api/status">Full Status</a></p>
    """)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8765))
    logging.info(f"Starting server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
