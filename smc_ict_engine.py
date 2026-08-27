"""
smc_ict_engine.py — DUAL OPTION ENGINE (SMC/ICT + 9 EMA SCALP)
- Setup 1: SMC / ICT (VWAP-Tap, FVG-Bounce, 3mBOS, OB-Support)
- Setup 2: 9 EMA Scalp (9 EMA Pullback Tap, Wick Bounce, 9 EMA Reclaim)
- Output clearly tags: 'Setup-1 (SMC)', 'Setup-2 (9EMA)', or 'SMC + 9EMA' (Double Confirmation)
"""

import json
import os
import sys

MIN_STOCK_DAY_VOLUME = 1_000_000  # 1M Shares minimum for stock signals

def fyers_candles_to_dicts(raw_candles):
    out = []
    for c in raw_candles:
        ts, o, h, l, close, vol = c
        out.append({
            "time": ts, "open": o, "high": h, "low": l,
            "close": close, "volume": vol
        })
    return out

def find_swings(candles, lookback=2):
    n = len(candles)
    highs, lows = [], []
    for i in range(lookback, n - lookback):
        window = candles[i - lookback: i + lookback + 1]
        if candles[i]["high"] == max(c["high"] for c in window):
            highs.append(i)
        if candles[i]["low"] == min(c["low"] for c in window):
            lows.append(i)
    return highs, lows

def calculate_vwap(candles):
    cum_pv = 0.0
    cum_vol = 0.0
    for c in candles:
        typical = (c["high"] + c["low"] + c["close"]) / 3
        cum_pv += typical * c["volume"]
        cum_vol += c["volume"]
    if cum_vol == 0:
        return None
    return cum_pv / cum_vol

def calculate_ema(closes, period=9):
    if len(closes) < period:
        return closes[-1] if closes else 0
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price * k) + (ema * (1 - k))
    return ema

def calculate_rsi(candles, period=14):
    if len(candles) < period + 1:
        return None
    closes = [c["close"] for c in candles[-(period + 1):]]
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calculate_rvol(candles, window=15):
    if len(candles) < window + 1:
        return 1.0
    avg_vol = sum(c["volume"] for c in candles[-window - 1:-1]) / window
    if avg_vol == 0:
        return 1.0
    return round(candles[-1]["volume"] / avg_vol, 2)

def detect_fvg(candles, lookahead=10):
    n = len(candles)
    start = max(2, n - lookahead)
    latest_fvg = None
    for i in range(start, n):
        c0, c2 = candles[i - 2], candles[i]
        if c0["high"] < c2["low"]:
            latest_fvg = {"type": "bullish", "bottom": c0["high"], "top": c2["low"]}
        elif c0["low"] > c2["high"]:
            latest_fvg = {"type": "bearish", "bottom": c2["high"], "top": c0["low"]}
    return latest_fvg

def detect_order_block(candles, lookahead=10):
    n = len(candles)
    if n < 6:
        return None
    start = max(1, n - lookahead)
    latest_ob = None
    for i in range(start, n):
        impulse = candles[i]
        prior = candles[i - 1]
        impulse_bullish = impulse["close"] > impulse["open"]
        prior_bearish = prior["close"] < prior["open"]
        if impulse_bullish and prior_bearish:
            latest_ob = {"type": "bullish", "top": prior["high"], "bottom": prior["low"]}
        elif (not impulse_bullish) and (prior["close"] > prior["open"]):
            latest_ob = {"type": "bearish", "top": prior["high"], "bottom": prior["low"]}
    return latest_ob

# ============================================================
# SPOT & STOCK ANALYZER (5-Min Candles)
# ============================================================

def analyze(candles, lookback=2, htf_candles=None, sector_chg=None, day_vol=0, **kwargs):
    if not candles or len(candles) < 8:
        return {
            "buy_score": 0, "sell_score": 0, "final_signal": "WAIT",
            "smc_label": "Syncing", "ict_label": "-", "evidence": "Syncing candles",
            "vwap": None, "ltp": candles[-1]["close"] if candles else None,
            "rvol": 1.0
        }

    ltp = candles[-1]["close"]
    vwap = calculate_vwap(candles)
    rsi = calculate_rsi(candles, 14)
    rvol = calculate_rvol(candles, 15)

    if day_vol < MIN_STOCK_DAY_VOLUME:
        return {
            "buy_score": 0, "sell_score": 0, "final_signal": "WAIT",
            "smc_label": "Low-Vol", "ict_label": "Vol < 1M",
            "evidence": f"Vol {format_vol_short(day_vol)} (<1M Min)",
            "vwap": round(vwap, 2) if vwap else None,
            "ltp": ltp, "rvol": rvol
        }

    fvg = detect_fvg(candles)
    ob = detect_order_block(candles)
    highs, lows = find_swings(candles, lookback=2)

    c_curr = candles[-1]
    is_green = c_curr["close"] > c_curr["open"]
    is_red = c_curr["close"] < c_curr["open"]

    first_open = candles[0]["open"]
    day_up = ltp > first_open and (vwap is None or ltp > vwap)
    day_down = ltp < first_open and (vwap is None or ltp < vwap)

    bos_bull = highs and c_curr["close"] > candles[highs[-1]]["high"]
    bos_bear = lows and c_curr["close"] < candles[lows[-1]]["low"]

    final_signal = "WAIT"
    buy_score = 0
    sell_score = 0
    evidence_parts = []
    smc_label = "Range"

    if day_up and is_green and rvol >= 1.25 and (rsi is None or rsi >= 48):
        if bos_bull:
            final_signal = "BUY"; buy_score = 95; smc_label = "BOS+1M"
            evidence_parts.append(f"BOS+RVol({rvol}x)+VWAP↑")
        elif fvg and fvg["type"] == "bullish" and (fvg["bottom"] <= c_curr["low"] <= fvg["top"]):
            final_signal = "BUY"; buy_score = 85; smc_label = "FVG-Bounce"
            evidence_parts.append(f"FVG+RVol({rvol}x)")
        elif ob and ob["type"] == "bullish":
            final_signal = "BUY"; buy_score = 80; smc_label = "OB-Support"
            evidence_parts.append(f"OB+RVol({rvol}x)")

    elif day_down and is_red and rvol >= 1.25 and (rsi is None or rsi <= 52):
        if bos_bear:
            final_signal = "SELL"; sell_score = 95; smc_label = "BOS+1M"
            evidence_parts.append(f"BOS+RVol({rvol}x)+VWAP↓")
        elif fvg and fvg["type"] == "bearish" and (fvg["bottom"] <= c_curr["high"] <= fvg["top"]):
            final_signal = "SELL"; sell_score = 85; smc_label = "FVG-Rejection"
            evidence_parts.append(f"FVG+RVol({rvol}x)")
        elif ob and ob["type"] == "bearish":
            final_signal = "SELL"; sell_score = 80; smc_label = "OB-Resistance"
            evidence_parts.append(f"OB+RVol({rvol}x)")

    if buy_score < 65 and sell_score < 65:
        final_signal = "WAIT"

    evidence = "+".join(evidence_parts) if evidence_parts else ("Consolidating" if final_signal == "WAIT" else "-")

    return {
        "buy_score": buy_score,
        "sell_score": sell_score,
        "final_signal": final_signal,
        "smc_label": smc_label,
        "ict_label": f"RSI({rsi})|{rvol}x" if rsi else "-",
        "evidence": evidence,
        "vwap": round(vwap, 2) if vwap else None,
        "ltp": ltp,
        "rvol": rvol,
    }

def format_vol_short(v):
    if v >= 10000000: return f"{v/10000000:.1f}Cr"
    if v >= 100000: return f"{v/100000:.1f}L"
    if v >= 1000: return f"{v/1000:.1f}K"
    return str(int(v))

# ============================================================
# OPTION DUAL ENGINE (SETUP 1: SMC/ICT  +  SETUP 2: 9 EMA)
# ============================================================

def analyze_option(candles, index_signal=None, htf_candles=None, **kwargs):
    empty = {
        "buy_score": 0, "sell_score": 0, "final_signal": "WAIT",
        "smc_label": "-", "ict_label": "-", "evidence": "Syncing candles",
        "vwap": None, "ltp": candles[-1]["close"] if candles else None, "rsi": None,
        "entry": None, "stop_loss": None, "targets": None,
    }

    if not candles or len(candles) < 3:
        return empty

    c_curr = candles[-1]
    c_prev = candles[-2]
    ltp = c_curr["close"]
    vwap = calculate_vwap(candles)
    closes = [c["close"] for c in candles]
    ema9 = calculate_ema(closes, 9)
    rsi = calculate_rsi(candles, period=14)

    is_green = c_curr["close"] > c_curr["open"]

    # HARD LOCK 1: Red Candle = Strictly WAIT
    if not is_green:
        return {
            **empty, "ltp": ltp, "vwap": round(vwap, 2) if vwap else None,
            "evidence": "Red candle (Waiting Green bounce)",
            "entry": f"{round(ltp, 1)}", "stop_loss": round(ltp * 0.95, 1),
            "targets": str(round(ltp * 1.1, 1))
        }

    # HARD LOCK 2: Below VWAP = Strictly WAIT
    if vwap is not None and ltp < (vwap * 0.998):
        return {
            **empty, "ltp": ltp, "vwap": round(vwap, 2) if vwap else None,
            "evidence": f"Below VWAP ₹{round(vwap, 1)} (Resistance)",
            "entry": f"{round(ltp, 1)}", "stop_loss": round(ltp * 0.95, 1),
            "targets": str(round(ltp * 1.1, 1))
        }

    smc_setups = []
    ema_setups = []

    # ------------------------------------------------------------
    # SETUP 1: SMC / ICT ENGINE
    # ------------------------------------------------------------
    # 1. VWAP Pullback Tap & Reclaim
    tapped_vwap = (c_curr["low"] <= vwap * 1.015) or (c_prev["low"] <= vwap * 1.015)
    if tapped_vwap and ltp >= vwap and is_green:
        smc_setups.append("VWAP-Tap")

    # 2. Bullish FVG Bounce
    fvg = detect_fvg(candles, lookahead=6)
    if fvg and fvg["type"] == "bullish":
        if (fvg["bottom"] * 0.98 <= c_curr["low"] <= fvg["top"] * 1.05) and is_green:
            smc_setups.append("FVG-Bounce")

    # 3. 3-Min Break of Structure (BOS)
    if len(candles) >= 4:
        recent_high = max(c["high"] for c in candles[-5:-1])
        if c_curr["close"] > recent_high and is_green:
            smc_setups.append("3mBOS")

    # 4. Bullish Order Block (OB)
    ob = detect_order_block(candles, lookahead=6)
    if ob and ob["type"] == "bullish" and (c_curr["low"] <= ob["top"] * 1.02):
        smc_setups.append("OB-Bounce")

    # ------------------------------------------------------------
    # SETUP 2: 9 EMA SCALP ENGINE
    # ------------------------------------------------------------
    if ema9:
        # 1. 9 EMA Pullback Tap & Green Wick Bounce
        tapped_ema = (c_curr["low"] <= ema9 * 1.012) or (c_prev["low"] <= ema9 * 1.012)
        bounced_ema = ltp >= ema9 and is_green
        if tapped_ema and bounced_ema and (rsi is None or rsi >= 40):
            ema_setups.append("9EMA-Tap")

        # 2. 9 EMA Momentum Crossover / Reclaim
        if c_prev["close"] <= ema9 and c_curr["close"] > ema9 and is_green:
            ema_setups.append("9EMA-Cross")

    # ------------------------------------------------------------
    # DUAL EVALUATION & EVIDENCE STRING
    # ------------------------------------------------------------
    has_smc = len(smc_setups) > 0
    has_ema = len(ema_setups) > 0

    if has_smc and has_ema:
        final_signal = "BUY"
        smc_label = "SMC + 9EMA"
        score = 99
        evidence = f"SMC({smc_setups[0]})+{ema_setups[0]}"
    elif has_smc:
        final_signal = "BUY"
        smc_label = "Setup-1 (SMC)"
        score = 90
        evidence = f"SMC:{'+'.join(smc_setups)}"
    elif has_ema:
        final_signal = "BUY"
        smc_label = "Setup-2 (9EMA)"
        score = 88
        evidence = f"9EMA:{'+'.join(ema_setups)}"
    elif index_signal in ["BUY", "SELL"] and is_green and ltp >= vwap:
        final_signal = "BUY"
        smc_label = "Index-Aligned"
        score = 80
        evidence = "Index-Flow+VWAP↑"
    else:
        final_signal = "WAIT"
        smc_label = "Consolidation"
        score = 0
        evidence = "Waiting Setup 1 or 2"

    # Stop Loss (Recent Low) & Targets (1:2 RR Scalp)
    recent_lows = [c["low"] for c in candles[-4:]]
    base_sl = min(recent_lows) if recent_lows else (ltp * 0.95)
    safe_sl = round(base_sl * 0.97, 1)
    risk = max(ltp - safe_sl, ltp * 0.04)
    target_1 = round(ltp + (risk * 2.0), 1)

    return {
        "buy_score": score if final_signal == "BUY" else 0,
        "sell_score": 0,
        "final_signal": final_signal,
        "smc_label": smc_label,
        "ict_label": f"RSI({rsi})" if rsi else "-",
        "evidence": evidence,
        "vwap": round(vwap, 2) if vwap else None,
        "ltp": ltp,
        "rsi": rsi,
        "entry": f"{round(ltp, 1)}-{round(ltp * 1.02, 1)}",
        "stop_loss": safe_sl,
        "targets": str(target_1),
    }

if __name__ == "__main__":
    print("✅ smc_ict_engine.py upgraded with Dual SMC + 9EMA Engine.")
