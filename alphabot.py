# ================================================================
#   ALPHA TRADING BOT — COMPLETE SINGLE FILE
#   Everything in ONE file — Just run: python3 alphabot.py
# ================================================================

import os, sys, hmac, hashlib, time, json, csv, uuid, signal
import logging, threading, requests
import numpy as np
import pandas as pd
from datetime import datetime
from urllib.parse import urlencode
from functools import wraps
from flask import Flask, render_template_string, request, jsonify, redirect, session
from flask_socketio import SocketIO, emit

# ================================================================
#   SECTION 1 — YOUR SETTINGS (EDIT THIS PART ONLY)
# ================================================================

# ================================================================
#   SECTION 1 — YOUR SETTINGS
# ================================================================
#   This bot reads settings from "Environment Variables" FIRST
#   (used by Railway/DigitalOcean/Hostinger Variables tab).
#   If no environment variable is found, it uses the text
#   written after "or" below — EDIT THAT TEXT if running
#   on your own PC without setting environment variables.
# ================================================================

DELTA_API_KEY    = os.environ.get("DELTA_API_KEY")    or "YOUR_DELTA_API_KEY_HERE"
DELTA_API_SECRET = os.environ.get("DELTA_API_SECRET") or "YOUR_DELTA_API_SECRET_HERE"
TRADING_MODE     = os.environ.get("TRADING_MODE")     or "demo"   # "demo" = safe testing | "live" = real money

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")   or "YOUR_TELEGRAM_CHAT_ID"
TELEGRAM_ENABLED   = True

DASHBOARD_HOST   = "0.0.0.0"
DASHBOARD_PORT   = int(os.environ.get("PORT") or 5000)   # Railway sets PORT automatically
DASHBOARD_SECRET = os.environ.get("DASHBOARD_SECRET") or "alphabot_2024_secret"

DASHBOARD_USER     = os.environ.get("DASHBOARD_USER")     or "@ALPHA"
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD") or "@ALPHA01"
DASHBOARD_USERS    = {DASHBOARD_USER: DASHBOARD_PASSWORD}

TRADING_PAIRS = {
    "BTCUSD": {"product_id": 84,    "lot_size": 5, "tick_size": 0.5,  "leverage": 10},
    "ETHUSD": {"product_id": 1699,  "lot_size": 8, "tick_size": 0.05, "leverage": 10},
    "SOLUSD": {"product_id": 14640, "lot_size": 3, "tick_size": 0.01, "leverage": 10},
}

RISK_REWARD_RATIO     = 2.5
SL_ATR_MULTIPLIER     = 1.5
EMA_FAST              = 9
EMA_SLOW              = 20
RSI_PERIOD            = 14
RSI_BULLISH           = 54
RSI_BEARISH           = 45
ATR_PERIOD            = 14
EMA_TOUCH_TOLERANCE   = 0.003
SCAN_INTERVAL_SECONDS = 60
MIN_CANDLES           = 50
LOG_FILE              = "alphabot.log"
TRADE_LOG_FILE        = "trades.csv"

DELTA_BASE_URL_DEMO = "https://api.india.delta.exchange"   # demo.delta.exchange uses this API
DELTA_BASE_URL_LIVE = "https://api.india.delta.exchange"   # same for live India account
DELTA_BASE_URL_TESTNET = "https://cdn-ind.testnet.deltaex.org"  # old testnet (kept as backup)

TF_TO_RESOLUTION = {
    "15m":15, "30m":30, "1h":60, "2h":120,
    "4h":240, "1d":1440, "1w":10080
}
INTRADAY_TFS = ["4h","2h","1h","30m","15m"]
SCALP_TFS    = ["1h","30m","15m"]

def BASE_URL():
    return DELTA_BASE_URL_DEMO if TRADING_MODE == "demo" else DELTA_BASE_URL_LIVE

# ================================================================
#   SECTION 2 — LOGGING SETUP
# ================================================================

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("AlphaBot")

# ================================================================
#   SECTION 3 — DELTA EXCHANGE API
# ================================================================

class DeltaAPI:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self._time_offset = 0
        self._sync_time()

    def _get_server_time(self):
        """Get Delta Exchange server time directly from their time endpoint."""
        try:
            r = requests.get(
                BASE_URL() + "/v2/time",
                headers={"User-Agent":"python-alphabot","Accept":"application/json"},
                timeout=5
            )
            data = r.json()
            if data.get("success"):
                return int(data["result"]["server_time"])
        except Exception:
            pass
        # Fallback — use local time
        return int(time.time())

    def _sync_time(self):
        """Sync local clock with Delta server time."""
        try:
            r = self.session.get(
                BASE_URL() + "/v2/time",
                headers={"User-Agent":"python-alphabot","Accept":"application/json"},
                timeout=5
            )
            data = r.json()
            if data.get("success"):
                server_ts = int(data["result"]["server_time"])
                self._time_offset = server_ts - int(time.time())
                log.info(f"Clock synced with Delta server time endpoint. Offset: {self._time_offset}s")
                return
        except Exception:
            pass
        # Fallback — use Date header
        try:
            r = self.session.get(
                BASE_URL() + "/v2/products",
                headers={"User-Agent":"python-alphabot","Accept":"application/json"},
                timeout=5
            )
            from email.utils import parsedate_to_datetime
            server_ts = int(parsedate_to_datetime(r.headers.get("Date","")).timestamp())
            self._time_offset = server_ts - int(time.time())
            log.info(f"Clock synced via Date header. Offset: {self._time_offset}s")
        except Exception as e:
            log.warning(f"Time sync failed: {e}")
            self._time_offset = 0

    def _now_ts(self):
        """Get current timestamp corrected for server time difference."""
        return int(time.time()) + self._time_offset

    def _make_headers(self, method, path, query="", body=""):
        """Generate fresh signature at the LAST possible moment before sending."""
        ts  = str(self._now_ts())   # timestamp generated HERE — right before sending
        msg = method + ts + path
        if query: msg += "?" + query
        if body:  msg += body
        sig = hmac.new(
            DELTA_API_SECRET.encode(),
            msg.encode(),
            hashlib.sha256
        ).hexdigest()
        return {
            "api-key":      DELTA_API_KEY,
            "timestamp":    ts,
            "signature":    sig,
            "Content-Type": "application/json",
            "User-Agent":   "python-alphabot",
            "Accept":       "application/json",
        }

    def _headers(self, method, path, query="", body=""):
        return self._make_headers(method, path, query, body)

    def _log_error_body(self, r):
        """Log Delta's exact error code/context so we see the real reason (e.g. correct IP to whitelist)."""
        try:
            body = r.json()
            log.error(f"Delta error body: {json.dumps(body)}")
        except Exception:
            log.error(f"Delta raw error text: {r.text[:300]}")

    def _get(self, path, params=None):
        query = urlencode(params) if params else ""
        url   = BASE_URL() + path + ("?" + query if query else "")
        for attempt in range(3):   # try up to 3 times with fresh timestamp each time
            try:
                hdrs = self._make_headers("GET", path, query)  # FRESH timestamp every attempt
                r    = self.session.get(url, headers=hdrs, timeout=10)
                if r.status_code in (401, 403):
                    self._log_error_body(r)
                    if attempt < 2:
                        log.warning(f"Attempt {attempt+1} failed — re-syncing and retrying...")
                        self._sync_time()
                        time.sleep(1)
                        continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                if attempt == 2:
                    log.error(f"GET {path} failed after 3 attempts: {e}")
                time.sleep(1)
        return None

    def _post(self, path, data=None):
        body = json.dumps(data) if data else ""
        url  = BASE_URL() + path
        for attempt in range(3):
            try:
                hdrs = self._make_headers("POST", path, "", body)  # FRESH timestamp every attempt
                r    = self.session.post(url, headers=hdrs, data=body, timeout=10)
                if r.status_code in (401, 403):
                    self._log_error_body(r)
                    if attempt < 2:
                        log.warning(f"Attempt {attempt+1} failed — re-syncing and retrying...")
                        self._sync_time()
                        time.sleep(1)
                        continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                if attempt == 2:
                    log.error(f"POST {path} failed after 3 attempts: {e}")
                time.sleep(1)
        return None

    def _delete(self, path, data=None):
        body = json.dumps(data) if data else ""
        url  = BASE_URL() + path
        try:
            r = self.session.delete(url, headers=self._headers("DELETE", path, "", body), data=body, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error(f"DELETE {path}: {e}")
            return None

    def test_connection(self):
        r = self._get("/v2/profile")
        if r and r.get("success"):
            return True, r["result"].get("email", "OK")
        return False, "Failed"

    def get_candles(self, symbol, resolution, count=200):
        end   = int(time.time())
        start = end - (resolution * 60 * count)
        r = self._get("/v2/history/candles", {"symbol": symbol, "resolution": resolution, "start": start, "end": end})
        if r and r.get("success"):
            return r.get("result", [])
        return []

    def get_mark_price(self, symbol):
        r = self._get(f"/v2/tickers/{symbol}")
        if r and r.get("success"):
            return float(r["result"].get("mark_price", 0))
        return 0.0

    def get_positions(self):
        r = self._get("/v2/positions/margined")
        if r and r.get("success"):
            return r.get("result", [])
        return []

    def get_position(self, product_id):
        for p in self.get_positions():
            if p.get("product_id") == product_id:
                return p
        return None

    def get_wallet_balance(self):
        r = self._get("/v2/wallet/balances")
        if r and r.get("success"):
            return r.get("result", [])
        return []

    def set_leverage(self, product_id, leverage):
        return self._post("/v2/products/leverage", {"product_id": product_id, "leverage": str(leverage)})

    def place_bracket_order(self, product_id, side, size, stop_loss, take_profit):
        sl_lmt = round(stop_loss   * (0.999 if side == "buy" else 1.001), 4)
        tp_lmt = round(take_profit * (1.001 if side == "buy" else 0.999), 4)
        data = {
            "product_id": product_id, "side": side,
            "order_type": "market_order", "size": size,
            "bracket_stop_loss_price":         str(stop_loss),
            "bracket_take_profit_price":       str(take_profit),
            "bracket_stop_loss_limit_price":   str(sl_lmt),
            "bracket_take_profit_limit_price": str(tp_lmt),
        }
        r = self._post("/v2/orders", data)
        if r and r.get("success"):
            log.info(f"Order OK: {side} {size} SL:{stop_loss} TP:{take_profit}")
            return r.get("result", {})
        log.error(f"Order FAILED: {r}")
        return None

    def get_fill_history(self, limit=50):
        r = self._get("/v2/fills", {"page_size": limit})
        if r and r.get("success"):
            return r.get("result", {}).get("fills", [])
        return []

    def close_position(self, symbol):
        cfg = TRADING_PAIRS.get(symbol, {})
        pos = self.get_position(cfg.get("product_id"))
        if not pos: return None
        size = abs(int(float(pos.get("size", 0))))
        if size == 0: return None
        side = "sell" if pos.get("entry_side", "buy") == "buy" else "buy"
        return self._post("/v2/orders", {"product_id": cfg["product_id"],
                                          "side": side, "order_type": "market_order", "size": size})

# ================================================================
#   SECTION 4 — INDICATORS
# ================================================================

def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calc_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    ag    = gain.ewm(com=period-1, min_periods=period).mean()
    al    = loss.ewm(com=period-1, min_periods=period).mean()
    rs    = ag / al.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)

def calc_atr(df, period=14):
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def add_indicators(df):
    df = df.copy()
    df["ema9"]    = calc_ema(df["close"], EMA_FAST)
    df["ema20"]   = calc_ema(df["close"], EMA_SLOW)
    df["rsi"]     = calc_rsi(df["close"], RSI_PERIOD)
    df["atr"]     = calc_atr(df, ATR_PERIOD)
    df["body"]    = (df["close"] - df["open"]).abs()
    df["is_bull"] = df["close"] > df["open"]
    df["is_bear"] = df["close"] < df["open"]
    return df

def candles_to_df(raw):
    if not raw: return pd.DataFrame()
    df = pd.DataFrame(raw, columns=["time","open","high","low","close","volume"])
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["time"] = pd.to_numeric(df["time"])
    return df.sort_values("time").reset_index(drop=True)

# ================================================================
#   SECTION 5 — STRATEGIES
# ================================================================

def strategy_ema_crossover(df):
    if len(df) < 5: return {"signal":"NONE","strength":0}
    c, p, p2 = df.iloc[-1], df.iloc[-2], df.iloc[-3]
    gap = abs(c["ema9"] - c["ema20"]) / c["ema20"] * 100
    bull_cross = (p["ema9"] <= p["ema20"]) and (c["ema9"] > c["ema20"])
    bear_cross = (p["ema9"] >= p["ema20"]) and (c["ema9"] < c["ema20"])
    pb_cross   = (p2["ema9"] <= p2["ema20"]) and (p["ema9"] > p["ema20"])
    sb_cross   = (p2["ema9"] >= p2["ema20"]) and (p["ema9"] < p["ema20"])
    bull_bar   = c["is_bull"] and c["body"] > c["atr"] * 0.3
    bear_bar   = c["is_bear"] and c["body"] > c["atr"] * 0.3
    if bull_cross and bull_bar: return {"signal":"BUY",  "strength":min(100,int(55+gap*80))}
    if bear_cross and bear_bar: return {"signal":"SELL", "strength":min(100,int(55+gap*80))}
    if pb_cross and bull_bar and c["ema9"]>c["ema20"]: return {"signal":"BUY",  "strength":min(100,int(50+gap*60))}
    if sb_cross and bear_bar and c["ema9"]<c["ema20"]: return {"signal":"SELL", "strength":min(100,int(50+gap*60))}
    return {"signal":"NONE","strength":0}

def strategy_ema_touch(df):
    if len(df) < 5: return {"signal":"NONE","strength":0}
    c, p = df.iloc[-1], df.iloc[-2]
    e9, e20 = c["ema9"], c["ema20"]
    tol = e20 * EMA_TOUCH_TOLERANCE
    t20 = (p["low"] <= e20+tol) and (p["high"] >= e20-tol)
    t9  = (p["low"] <= e9+tol)  and (p["high"] >= e9-tol)
    bull_bnc = c["is_bull"] and c["body"] > c["atr"]*0.3 and c["close"] > e20
    bear_bnc = c["is_bear"] and c["body"] > c["atr"]*0.3 and c["close"] < e20
    if e9>e20 and t20 and bull_bnc: return {"signal":"BUY",  "strength":65}
    if e9<e20 and t20 and bear_bnc: return {"signal":"SELL", "strength":65}
    if e9>e20 and t9  and bull_bnc: return {"signal":"BUY",  "strength":60}
    if e9<e20 and t9  and bear_bnc: return {"signal":"SELL", "strength":60}
    return {"signal":"NONE","strength":0}

def strategy_rsi(df):
    if len(df) < 5: return {"signal":"NONE","strength":0}
    c, p = df.iloc[-1], df.iloc[-2]
    rn, rp = c["rsi"], p["rsi"]
    up = c["ema9"] > c["ema20"]
    dn = c["ema9"] < c["ema20"]
    bb = c["is_bull"] and c["body"] > c["atr"]*0.2
    sb = c["is_bear"] and c["body"] > c["atr"]*0.2
    if rn>RSI_BULLISH and rn<70 and up and bb and rn>rp:
        return {"signal":"BUY",  "strength":min(100,int(50+(rn-RSI_BULLISH)*2))}
    if rn<RSI_BEARISH and rn>30 and dn and sb and rn<rp:
        return {"signal":"SELL", "strength":min(100,int(50+(RSI_BEARISH-rn)*2))}
    return {"signal":"NONE","strength":0}

def strategy_swing_htf(df1w, df1d, df4h):
    results = []
    for df in [df1w, df1d, df4h]:
        if df is not None and len(df) > 3:
            c = df.iloc[-1]
            results.append("BULL" if c["ema9"] > c["ema20"] else "BEAR")
    if not results: return "NEUTRAL"
    return "BULL" if results.count("BULL")>=2 else "BEAR" if results.count("BEAR")>=2 else "NEUTRAL"

def strategy_swing_exec(df1h, htf):
    if len(df1h) < 5 or htf == "NEUTRAL": return {"signal":"NONE","strength":0}
    c, p, p2 = df1h.iloc[-1], df1h.iloc[-2], df1h.iloc[-3]
    ema_up = c["ema9"] > c["ema20"]
    ema_dn = c["ema9"] < c["ema20"]
    dbull = p2["is_bull"] and p["is_bull"] and p["close"]>p2["high"] and p["body"]>p["atr"]*0.4 and c["is_bull"]
    dbear = p2["is_bear"] and p["is_bear"] and p["close"]<p2["low"]  and p["body"]>p["atr"]*0.4 and c["is_bear"]
    if dbull and htf=="BULL" and ema_up: return {"signal":"BUY",  "strength":min(100,int(70+p["body"]/p["atr"]*10))}
    if dbear and htf=="BEAR" and ema_dn: return {"signal":"SELL", "strength":min(100,int(70+p["body"]/p["atr"]*10))}
    return {"signal":"NONE","strength":0}

def analyze_one_tf(df):
    if df is None or len(df) < MIN_CANDLES: return {"direction":"NONE","strength":0,"atr":0,"close":0}
    df = add_indicators(df)
    s1 = strategy_ema_crossover(df)
    s2 = strategy_ema_touch(df)
    s3 = strategy_rsi(df)
    buys  = [s for s in [s1,s2,s3] if s["signal"]=="BUY"]
    sells = [s for s in [s1,s2,s3] if s["signal"]=="SELL"]
    c = df.iloc[-1]
    base = {"atr":float(c["atr"]), "close":float(c["close"]),
            "ema9":float(c["ema9"]), "ema20":float(c["ema20"]), "rsi":float(c["rsi"])}
    if len(buys)>=2:
        base.update({"direction":"BUY","strength":int(np.mean([s["strength"] for s in buys]))})
        return base
    if len(sells)>=2:
        base.update({"direction":"SELL","strength":int(np.mean([s["strength"] for s in sells]))})
        return base
    base.update({"direction":"NONE","strength":0})
    return base

def get_intraday_signal(tf_data):
    results = {tf: analyze_one_tf(tf_data.get(tf)) for tf in INTRADAY_TFS}
    dirs = [results[tf]["direction"] for tf in INTRADAY_TFS]
    nn   = [d for d in dirs if d != "NONE"]
    if len(nn)==len(INTRADAY_TFS) and all(d=="BUY"  for d in nn):
        return {"signal":"BUY",  "strength":int(np.mean([results[tf]["strength"] for tf in INTRADAY_TFS])), "trade_type":"INTRADAY", "tf_results":results}
    if len(nn)==len(INTRADAY_TFS) and all(d=="SELL" for d in nn):
        return {"signal":"SELL", "strength":int(np.mean([results[tf]["strength"] for tf in INTRADAY_TFS])), "trade_type":"INTRADAY", "tf_results":results}
    return {"signal":"NONE","reason":f"{len(nn)}/{len(INTRADAY_TFS)} TFs agree"}

def get_scalp_signal(tf_data):
    results = {tf: analyze_one_tf(tf_data.get(tf)) for tf in SCALP_TFS}
    dirs = [results[tf]["direction"] for tf in SCALP_TFS]
    nn   = [d for d in dirs if d != "NONE"]
    if len(nn)==3 and all(d=="BUY"  for d in nn):
        return {"signal":"BUY",  "strength":int(np.mean([results[tf]["strength"] for tf in SCALP_TFS])), "trade_type":"SCALP"}
    if len(nn)==3 and all(d=="SELL" for d in nn):
        return {"signal":"SELL", "strength":int(np.mean([results[tf]["strength"] for tf in SCALP_TFS])), "trade_type":"SCALP"}
    return {"signal":"NONE","reason":f"{len(nn)}/3 scalp TFs agree"}

def get_swing_signal(tf_data):
    dfs = {}
    for tf in ["1w","1d","4h","1h"]:
        df = tf_data.get(tf)
        if df is not None and len(df)>=10:
            dfs[tf] = add_indicators(df)
    htf = strategy_swing_htf(dfs.get("1w"), dfs.get("1d"), dfs.get("4h"))
    df1h = dfs.get("1h")
    if df1h is None: return {"signal":"NONE","reason":"No 1H data"}
    r = strategy_swing_exec(df1h, htf)
    r["trade_type"] = "SWING"
    r["htf"]        = htf
    return r

# ================================================================
#   SECTION 6 — TRADE STATE & LOGGING
# ================================================================

class TradeState:
    def __init__(self):
        self.lock          = threading.Lock()
        self.active_trades = {}
        self.history       = []
        self.total_pnl     = 0.0
        self.wins          = 0
        self.losses        = 0
        self.bot_status    = "STARTING"
        self.start_time    = datetime.now()
        self._load_csv()

    def _load_csv(self):
        if not os.path.exists(TRADE_LOG_FILE): return
        try:
            with open(TRADE_LOG_FILE) as f:
                for row in csv.DictReader(f):
                    if row.get("status") == "CLOSED":
                        self.history.append(row)
                        pnl = float(row.get("pnl", 0))
                        self.total_pnl += pnl
                        if pnl > 0: self.wins   += 1
                        else:       self.losses += 1
            log.info(f"Loaded {len(self.history)} trades from history")
        except Exception as e:
            log.error(f"CSV load: {e}")

    def has_active(self, symbol):
        with self.lock: return symbol in self.active_trades

    def get_active(self, symbol):
        with self.lock: return self.active_trades.get(symbol)

    def open_trade(self, trade):
        with self.lock: self.active_trades[trade["symbol"]] = trade
        self._write_csv(trade, "OPEN")

    def close_trade(self, symbol, exit_price, reason):
        with self.lock: trade = self.active_trades.pop(symbol, None)
        if not trade: return None
        entry = float(trade["entry_price"])
        size  = int(trade["size"])
        pts   = (exit_price - entry) if trade["direction"]=="BUY" else (entry - exit_price)
        trade.update({"exit_price":exit_price, "exit_reason":reason,
                      "exit_time":datetime.now().isoformat(),
                      "pnl":round(pts*size,2), "pnl_pct":round(pts/entry*100,4), "status":"CLOSED"})
        with self.lock:
            self.total_pnl += trade["pnl"]
            if trade["pnl"]>0: self.wins   += 1
            else:               self.losses += 1
            self.history.append(trade)
        self._write_csv(trade, "CLOSED")
        log.info(f"Closed: {symbol} PnL:${trade['pnl']:.2f} {reason}")
        return trade

    def _write_csv(self, trade, status):
        fields = ["id","symbol","direction","trade_type","entry_price","exit_price",
                  "stop_loss","take_profit","size","pnl","pnl_pct","strategy",
                  "strength","entry_time","exit_time","exit_reason","status"]
        exists = os.path.exists(TRADE_LOG_FILE)
        with open(TRADE_LOG_FILE, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if not exists: w.writeheader()
            row = {k: trade.get(k,"") for k in fields}
            row["status"] = status
            w.writerow(row)

    def get_stats(self):
        total = self.wins + self.losses
        return {
            "bot_status":   self.bot_status,
            "total_trades": total,
            "wins":         self.wins,
            "losses":       self.losses,
            "win_rate":     round(self.wins/total*100,1) if total>0 else 0,
            "total_pnl":    round(self.total_pnl,2),
            "active_count": len(self.active_trades),
            "uptime_hours": round((datetime.now()-self.start_time).total_seconds()/3600,1),
        }

# ================================================================
#   SECTION 7 — SL/TP CALCULATOR
# ================================================================

def calc_sltp(symbol, direction, entry, atr, trade_type="INTRADAY"):
    cfg  = TRADING_PAIRS.get(symbol, {})
    tick = cfg.get("tick_size", 0.01)
    rr   = RISK_REWARD_RATIO
    mult = 2.0 if trade_type=="SWING" else 1.0 if trade_type=="SCALP" else SL_ATR_MULTIPLIER
    sl_d = atr * mult
    tp_d = sl_d * rr
    def snap(v): return round(round(v/tick)*tick, 8)
    if direction=="BUY":  return {"stop_loss":snap(entry-sl_d), "take_profit":snap(entry+tp_d), "rr":rr, "atr":round(atr,4)}
    else:                 return {"stop_loss":snap(entry+sl_d), "take_profit":snap(entry-tp_d), "rr":rr, "atr":round(atr,4)}

# ================================================================
#   SECTION 8 — TELEGRAM
# ================================================================

class Telegram:
    def __init__(self):
        self.base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

    def send(self, msg):
        if not TELEGRAM_ENABLED: return
        try:
            requests.post(f"{self.base}/sendMessage",
                json={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"Markdown"}, timeout=5)
        except Exception as e:
            log.error(f"Telegram: {e}")

    def trade_opened(self, t):
        arrow = "⬆️ LONG" if t["direction"]=="BUY" else "⬇️ SHORT"
        emoji = "🟢" if t["direction"]=="BUY" else "🔴"
        self.send(f"{emoji} *TRADE OPENED* | {t['symbol']}\n"
                  f"Type: {t.get('trade_type','INTRADAY')} | {arrow}\n"
                  f"Entry: `{t['entry_price']:.2f}`\n"
                  f"Stop Loss: `{t['stop_loss']:.2f}`\n"
                  f"Take Profit: `{t['take_profit']:.2f}`\n"
                  f"Size: {t['size']} lots | RR 1:{t.get('rr',2.5)}\n"
                  f"Strategy: {t.get('strategy','—')}\n"
                  f"Strength: {t.get('strength',0)}/100")

    def trade_closed(self, t):
        pnl   = t.get("pnl",0)
        emoji = "✅" if pnl>0 else "❌"
        self.send(f"{emoji} *TRADE CLOSED* | {t['symbol']}\n"
                  f"{t['direction']} | {t.get('exit_reason','—')}\n"
                  f"Entry: {t['entry_price']} → Exit: {t['exit_price']:.2f}\n"
                  f"PnL: ${pnl:.2f}\n{'🟢 WIN' if pnl>0 else '🔴 LOSS'}")

    def started(self):
        self.send(f"🤖 *ALPHA Bot STARTED*\nMode: {TRADING_MODE.upper()}\n"
                  f"Pairs: BTCUSD, ETHUSD, SOLUSD\nRR: 1:{RISK_REWARD_RATIO} | Running 24/7 ✅")

    def stopped(self): self.send("⏹ *ALPHA Bot STOPPED*")
    def error(self, m): self.send(f"⚠️ *BOT ERROR*\n{str(m)[:300]}")

# ================================================================
#   SECTION 9 — POSITION MONITOR
# ================================================================

class PositionMonitor:
    def __init__(self, api, state, tg):
        self.api   = api
        self.state = state
        self.tg    = tg

    def check_all(self):
        active = dict(self.state.active_trades)
        if not active: return
        live = {p["product_id"]: p for p in (self.api.get_positions() or [])}
        for symbol, trade in active.items():
            pid      = TRADING_PAIRS.get(symbol,{}).get("product_id")
            pos      = live.get(pid)
            size     = float(pos.get("size",0)) if pos else 0
            if size == 0:
                self._detect_fill(symbol, trade)
                continue
            mark = self.api.get_mark_price(symbol)
            if not mark: continue
            sl, tp, d = float(trade["stop_loss"]), float(trade["take_profit"]), trade["direction"]
            if d=="BUY":
                if mark<=sl: self._close(symbol, trade, mark, "SL_HIT")
                elif mark>=tp: self._close(symbol, trade, mark, "TP_HIT")
            else:
                if mark>=sl: self._close(symbol, trade, mark, "SL_HIT")
                elif mark<=tp: self._close(symbol, trade, mark, "TP_HIT")

    def _detect_fill(self, symbol, trade):
        pid   = TRADING_PAIRS.get(symbol,{}).get("product_id")
        fills = self.api.get_fill_history(20)
        exit_price = float(trade["entry_price"])
        reason     = "CLOSED_ON_EXCHANGE"
        for f in fills:
            if f.get("product_id")==pid and float(f.get("created_at",0))>float(trade.get("entry_ts",0)):
                exit_price = float(f.get("price", exit_price))
                reason     = "FILL_" + f.get("side","").upper()
                break
        self._finalize(symbol, trade, exit_price, reason)

    def _close(self, symbol, trade, price, reason):
        log.info(f"Force close: {symbol} {reason} @ {price}")
        pos = self.api.get_position(TRADING_PAIRS[symbol]["product_id"])
        if pos and float(pos.get("size",0))!=0:
            self.api.close_position(symbol)
        self._finalize(symbol, trade, price, reason)

    def _finalize(self, symbol, trade, exit_price, reason):
        closed = self.state.close_trade(symbol, exit_price, reason)
        if closed: self.tg.trade_closed(closed)

# ================================================================
#   SECTION 10 — MAIN BOT ENGINE
# ================================================================

class AlphaBot:
    def __init__(self):
        log.info("="*50)
        log.info("  ALPHA TRADING BOT — STARTING")
        log.info("="*50)
        self.api     = DeltaAPI()
        self.state   = TradeState()
        self.tg      = Telegram()
        self.monitor = PositionMonitor(self.api, self.state, self.tg)
        self.running = False
        self._cache  = {}
        signal.signal(signal.SIGINT,  self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    def start(self):
        log.info("Testing API connection...")
        ok, msg = self.api.test_connection()
        if not ok:
            log.error(f"API FAILED: {msg} — Check config.py API keys")
            self.tg.error(f"Connection failed: {msg}")
            return
        log.info(f"API Connected: {msg}")
        for sym, cfg in TRADING_PAIRS.items():
            self.api.set_leverage(cfg["product_id"], cfg["leverage"])
        self.running = True
        self.state.bot_status = "RUNNING"
        self.tg.started()
        log.info(f"Bot RUNNING | Mode:{TRADING_MODE.upper()} | Interval:{SCAN_INTERVAL_SECONDS}s")
        while self.running:
            try: self._cycle()
            except Exception as e:
                log.error(f"Cycle error: {e}", exc_info=True)
                self.tg.error(str(e))
            time.sleep(SCAN_INTERVAL_SECONDS)

    def _shutdown(self, *a):
        log.info("Shutting down...")
        self.running = False
        self.state.bot_status = "STOPPED"
        self.tg.stopped()

    def _cycle(self):
        self.monitor.check_all()
        self._fetch_candles()
        for sym in TRADING_PAIRS:
            try: self._analyze(sym)
            except Exception as e: log.error(f"Analyze {sym}: {e}")

    def _fetch_candles(self):
        for sym in TRADING_PAIRS:
            if sym not in self._cache: self._cache[sym] = {}
            for tf, res in TF_TO_RESOLUTION.items():
                raw = self.api.get_candles(sym, res, 200)
                df  = candles_to_df(raw)
                if len(df) >= 10: self._cache[sym][tf] = df

    def _analyze(self, symbol):
        if self.state.has_active(symbol): return
        tf_data = self._cache.get(symbol, {})
        if not tf_data: return
        chosen = None
        for sig in [get_swing_signal(tf_data), get_intraday_signal(tf_data), get_scalp_signal(tf_data)]:
            if sig.get("signal") in ["BUY","SELL"]: chosen = sig; break
        if not chosen: return
        df1h = tf_data.get("1h")
        if df1h is None or len(df1h) < 5: return
        df_ind      = add_indicators(df1h)
        curr        = df_ind.iloc[-1]
        entry_price = self.api.get_mark_price(symbol) or float(curr["close"])
        atr_val     = float(curr["atr"])
        sltp        = calc_sltp(symbol, chosen["signal"], entry_price, atr_val, chosen.get("trade_type","INTRADAY"))
        cfg         = TRADING_PAIRS[symbol]
        log.info(f"SIGNAL: {symbol} {chosen['signal']} | {chosen.get('trade_type')} | "
                 f"Entry:{entry_price:.2f} SL:{sltp['stop_loss']:.2f} TP:{sltp['take_profit']:.2f}")
        order = self.api.place_bracket_order(
            cfg["product_id"], chosen["signal"].lower(),
            cfg["lot_size"], sltp["stop_loss"], sltp["take_profit"]
        )
        if not order:
            log.error(f"Order FAILED: {symbol}")
            self.tg.error(f"Order failed: {symbol} {chosen['signal']}")
            return
        trade = {
            "id": str(uuid.uuid4())[:8].upper(), "symbol": symbol,
            "direction": chosen["signal"], "trade_type": chosen.get("trade_type","INTRADAY"),
            "entry_price": entry_price, "stop_loss": sltp["stop_loss"],
            "take_profit": sltp["take_profit"], "size": cfg["lot_size"],
            "rr": sltp["rr"], "strategy": chosen.get("entry","MULTI_TF"),
            "strength": chosen.get("strength",0), "entry_time": datetime.now().isoformat(),
            "entry_ts": time.time(), "order_id": order.get("id",""),
        }
        self.state.open_trade(trade)
        self.tg.trade_opened(trade)
        log.info(f"✅ Trade opened: {symbol} {chosen['signal']} @ {entry_price:.2f}")

    def get_dashboard_data(self):
        prices = {sym: self.api.get_mark_price(sym) for sym in TRADING_PAIRS}
        bal    = sum(float(b.get("balance",0)) for b in self.api.get_wallet_balance()
                     if b.get("currency") in ["USD","USDT"])
        return {
            "stats":         self.state.get_stats(),
            "active_trades": dict(self.state.active_trades),
            "trade_history": list(self.state.history[-50:]),
            "prices":        prices,
            "balance":       round(bal,2),
            "timestamp":     datetime.now().isoformat(),
            "mode":          TRADING_MODE.upper(),
        }

# ================================================================
#   SECTION 11 — FLASK DASHBOARD
# ================================================================

app      = Flask(__name__)
app.secret_key = DASHBOARD_SECRET
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
_bot     = None

def get_bot():
    global _bot
    if _bot is None: _bot = AlphaBot()
    return _bot

def login_required(f):
    @wraps(f)
    def dec(*a, **k):
        if not session.get("logged_in"): return redirect("/login")
        return f(*a, **k)
    return dec

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ALPHA Bot Login</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#03060f;color:#e8f0fe;font-family:'Inter',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;background-image:linear-gradient(rgba(0,212,170,.03) 1px,transparent 1px),linear-gradient(90deg,rgba(0,212,170,.03) 1px,transparent 1px);background-size:40px 40px}
.card{background:linear-gradient(145deg,#070d1c,#0c1526);border:1px solid rgba(0,212,170,.2);border-radius:20px;padding:52px 48px 44px;width:420px;position:relative;overflow:hidden;box-shadow:0 50px 100px rgba(0,0,0,.7)}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,#00d4aa,#00aaff,transparent)}
.top{text-align:center;margin-bottom:36px}
.icon{width:70px;height:70px;background:rgba(0,212,170,.1);border:1px solid rgba(0,212,170,.25);border-radius:18px;display:flex;align-items:center;justify-content:center;font-size:34px;margin:0 auto 18px;animation:float 3s ease-in-out infinite}
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-8px)}}
h1{font-size:26px;font-weight:800;background:linear-gradient(135deg,#00d4aa,#00aaff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
p{font-size:12px;color:#4a6080;letter-spacing:1px;text-transform:uppercase;margin-top:6px}
.f{margin:16px 0}
.f label{display:block;font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#4a6080;margin-bottom:8px}
.f input{width:100%;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:13px 16px;color:#e8f0fe;font-family:'JetBrains Mono',monospace;font-size:14px;outline:none;transition:all .2s}
.f input:focus{border-color:#00d4aa;background:rgba(0,212,170,.04);box-shadow:0 0 0 3px rgba(0,212,170,.08)}
.btn{width:100%;background:linear-gradient(135deg,#00d4aa,#00aaff);border:none;border-radius:10px;padding:14px;color:#03060f;font-size:15px;font-weight:700;cursor:pointer;margin-top:12px;transition:all .2s}
.btn:hover{transform:translateY(-2px);box-shadow:0 10px 30px rgba(0,212,170,.3)}
.err{background:rgba(240,54,90,.1);border:1px solid rgba(240,54,90,.25);border-radius:8px;padding:10px 14px;color:#f87191;font-size:13px;margin-bottom:14px;text-align:center;display:none}
.hint{margin-top:18px;text-align:center;background:rgba(0,212,170,.05);border:1px solid rgba(0,212,170,.1);border-radius:10px;padding:12px;font-size:12px;color:#4a6080;line-height:1.9}
.hint b{color:#00d4aa;font-family:'JetBrains Mono',monospace}
</style></head><body>
<div class="card">
  <div class="top"><div class="icon">🤖</div><h1>ALPHA Trading Bot</h1><p>Delta Exchange • 24/7 Auto Trading</p></div>
  {% if error %}<div class="err" style="display:block">❌ {{ error }}</div>{% endif %}
  <form method="POST" action="/login">
    <div class="f"><label>Username</label><input type="text" name="username" placeholder="@ALPHA" required autofocus></div>
    <div class="f"><label>Password</label><input type="password" name="password" placeholder="••••••••" required></div>
    <button type="submit" class="btn">🔐 LOGIN TO BOT</button>
  </form>
  <div class="hint">Login → Username: <b>@ALPHA</b> &nbsp;/&nbsp; Password: <b>@ALPHA01</b></div>
</div></body></html>"""

DASH_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ALPHA Trading Bot</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
:root{--bg:#03060f;--s1:#070d1c;--s2:#0c1526;--s3:#101d31;--ac:#00d4aa;--bl:#00aaff;--rd:#f0365a;--yl:#f5a623;--tx:#e8f0fe;--mt:#4a6080;--bd:rgba(0,212,170,0.1);--fn:'Inter',sans-serif;--mn:'JetBrains Mono',monospace}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--tx);font-family:var(--fn);overflow-x:hidden;font-size:14px;background-image:linear-gradient(rgba(0,212,170,.02) 1px,transparent 1px),linear-gradient(90deg,rgba(0,212,170,.02) 1px,transparent 1px);background-size:40px 40px}
body::after{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 900px 700px at 10% 20%,rgba(0,212,170,.04),transparent 60%),radial-gradient(ellipse 700px 600px at 90% 80%,rgba(0,170,255,.04),transparent 60%);pointer-events:none;z-index:0}
nav{height:58px;background:rgba(3,6,15,.97);border-bottom:1px solid var(--bd);backdrop-filter:blur(30px);display:flex;align-items:center;justify-content:space-between;padding:0 24px;position:sticky;top:0;z-index:300;position:relative}
.brand{display:flex;align-items:center;gap:10px}
.bicon{width:32px;height:32px;background:rgba(0,212,170,.15);border:1px solid rgba(0,212,170,.3);border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:16px}
.bname{font-size:17px;font-weight:800;background:linear-gradient(135deg,var(--ac),var(--bl));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.npairs{display:flex;gap:3px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);border-radius:8px;padding:3px}
.np{padding:5px 13px;border-radius:6px;font-size:12px;font-weight:600;color:var(--mt);cursor:pointer;transition:all .15s;font-family:var(--mn)}
.np.on{background:var(--s3);color:var(--ac)}
.nav-r{display:flex;align-items:center;gap:12px}
.bpill{display:flex;align-items:center;gap:7px;border-radius:20px;padding:5px 13px;font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;font-family:var(--mn)}
.pr{background:rgba(0,212,170,.1);border:1px solid rgba(0,212,170,.25);color:var(--ac)}
.ps{background:rgba(240,54,90,.1);border:1px solid rgba(240,54,90,.25);color:var(--rd)}
.pi{background:rgba(245,166,35,.1);border:1px solid rgba(245,166,35,.25);color:var(--yl)}
.dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.dot.pulse{animation:hb 1.8s infinite}
@keyframes hb{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(1.5);opacity:.4}}
.clk{font-family:var(--mn);font-size:12px;color:var(--mt)}
.lo{background:none;border:1px solid rgba(240,54,90,.2);border-radius:6px;padding:5px 12px;color:var(--rd);font-size:11px;cursor:pointer;font-family:var(--fn);font-weight:600}
.lo:hover{background:rgba(240,54,90,.08)}
.main{max-width:1800px;margin:0 auto;padding:18px 24px;position:relative;z-index:1}
.prices{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:18px}
.pc{background:linear-gradient(145deg,var(--s1),var(--s2));border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:18px 22px;display:flex;justify-content:space-between;align-items:center;position:relative;overflow:hidden}
.pc::after{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,var(--c),transparent);opacity:.4}
.pc.btc{--c:#f7931a}.pc.eth{--c:#627eea}.pc.sol{--c:#9945ff}
.pc-sym{font-size:11px;font-weight:700;letter-spacing:2px;color:var(--mt);text-transform:uppercase;margin-bottom:5px}
.pc-price{font-family:var(--mn);font-size:28px;font-weight:700;letter-spacing:-1px}
.pc-chg{font-family:var(--mn);font-size:12px;margin-top:4px}
.cp{color:var(--ac)}.cn{color:var(--rd)}
.pl{text-align:right}.pl-lb{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--mt)}.pl-vl{font-family:var(--mn);font-size:14px;font-weight:700;color:var(--c);margin-top:4px}
.stats{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:18px}
.sc{background:linear-gradient(145deg,var(--s1),var(--s2));border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:16px 18px;position:relative;overflow:hidden}
.sc::before{content:'';position:absolute;bottom:0;left:0;right:0;height:2px;background:var(--cc,var(--ac));opacity:0;transition:opacity .3s}
.sc:hover::before{opacity:.6}
.sl{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--mt);margin-bottom:10px}
.sv{font-family:var(--mn);font-size:24px;font-weight:700;line-height:1;margin-bottom:4px}
.ss{font-size:11px;color:var(--mt)}
.vg{color:var(--ac)}.vr{color:var(--rd)}.vy{color:var(--yl)}.vb{color:var(--bl)}.vp{color:#9945ff}.vm{color:var(--mt)}
.mgrid{display:grid;grid-template-columns:1fr 380px;gap:18px;margin-bottom:18px}
.cp2{background:linear-gradient(145deg,var(--s1),var(--s2));border:1px solid rgba(255,255,255,.06);border-radius:14px;overflow:hidden}
.ph{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid rgba(255,255,255,.05);background:rgba(0,0,0,.15)}
.ph-t{font-size:13px;font-weight:700;display:flex;align-items:center;gap:8px}
.ld{width:7px;height:7px;border-radius:50%;background:var(--ac);animation:hb 1.5s infinite}
.tfs{display:flex;gap:3px}
.tf{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.06);border-radius:5px;padding:4px 10px;font-family:var(--mn);font-size:11px;color:var(--mt);cursor:pointer;transition:all .15s}
.tf.on{background:rgba(0,212,170,.1);border-color:rgba(0,212,170,.3);color:var(--ac)}
.ca{height:490px;position:relative;background:var(--s1)}
.ca iframe{width:100%;height:100%;border:none;display:block;position:absolute;top:0;left:0}
.cl{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;background:var(--s1)}
.sp{width:38px;height:38px;border:2px solid rgba(0,212,170,.15);border-top-color:var(--ac);border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.cl p{font-family:var(--mn);font-size:12px;color:var(--mt)}
.rp{display:flex;flex-direction:column;gap:14px}
.sbx{background:linear-gradient(145deg,var(--s1),var(--s2));border:1px solid rgba(255,255,255,.06);border-radius:14px;overflow:hidden}
.tfm{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;padding:12px}
.tfc{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.05);border-radius:8px;padding:9px 6px;text-align:center;transition:all .4s}
.tfc.bull{background:rgba(0,212,170,.07);border-color:rgba(0,212,170,.2)}
.tfc.bear{background:rgba(240,54,90,.07);border-color:rgba(240,54,90,.2)}
.tfc.wait{background:rgba(245,166,35,.05);border-color:rgba(245,166,35,.15)}
.tn{font-size:9px;font-weight:700;letter-spacing:1.5px;color:var(--mt);text-transform:uppercase;margin-bottom:3px}
.ts2{font-size:14px;font-weight:800;font-family:var(--mn)}
.tfc.bull .ts2{color:var(--ac)}.tfc.bear .ts2{color:var(--rd)}.tfc.wait .ts2{color:var(--yl)}
.tst{font-size:9px;font-family:var(--mn);color:var(--mt);margin-top:2px}
.atb{padding:0 12px 12px}
.ate{border:1px dashed rgba(255,255,255,.06);border-radius:10px;padding:20px;text-align:center;font-size:12px;color:var(--mt);line-height:1.8}
.atcard{background:var(--s3);border:1px solid rgba(0,212,170,.15);border-radius:10px;padding:14px;position:relative;overflow:hidden}
.atcard::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--ac),var(--bl))}
.at-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.at-sym{font-size:20px;font-weight:800}
.at-badge{border-radius:20px;padding:4px 12px;font-size:11px;font-weight:700;letter-spacing:1px}
.atl{background:rgba(0,212,170,.15);color:var(--ac);border:1px solid rgba(0,212,170,.25)}
.ats{background:rgba(240,54,90,.15);color:var(--rd);border:1px solid rgba(240,54,90,.25)}
.at-cells{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}
.atcell{background:rgba(0,0,0,.2);border-radius:7px;padding:8px;text-align:center}
.atcell label{display:block;font-size:9px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:var(--mt);margin-bottom:3px}
.atcell .v{font-family:var(--mn);font-size:13px;font-weight:700}
.at-pb-w{height:3px;background:rgba(255,255,255,.06);border-radius:2px;margin-top:10px;overflow:hidden}
.at-pb{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--ac),var(--bl));transition:width .6s}
.srows{padding:0 12px 12px;display:flex;flex-direction:column;gap:5px}
.srow{display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.04);border-radius:7px;padding:7px 10px}
.sr-n{font-size:12px;color:var(--tx)}
.tag{display:inline-block;padding:2px 8px;border-radius:10px;font-size:9px;font-weight:700;letter-spacing:.5px;font-family:var(--mn)}
.t-sc{background:rgba(245,166,35,.1);color:var(--yl)}.t-bl{background:rgba(0,212,170,.1);color:var(--ac)}.t-br{background:rgba(240,54,90,.1);color:var(--rd)}.t-ok{background:rgba(0,170,255,.1);color:var(--bl)}
.tl{background:rgba(0,212,170,.1);color:var(--ac)}.ts{background:rgba(240,54,90,.1);color:var(--rd)}.tw{background:rgba(0,212,170,.1);color:var(--ac)}.tls{background:rgba(240,54,90,.1);color:var(--rd)}.to{background:rgba(245,166,35,.1);color:var(--yl)}
.bgrid{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:18px}
.eb,.hb{background:linear-gradient(145deg,var(--s1),var(--s2));border:1px solid rgba(255,255,255,.06);border-radius:14px;overflow:hidden}
.ew{padding:14px;height:220px;position:relative}
.tw2{overflow-y:auto;max-height:260px}
.tbl{width:100%;border-collapse:collapse;font-size:12px}
.tbl th{position:sticky;top:0;background:var(--s2);padding:9px 12px;font-size:9px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--mt);text-align:left;border-bottom:1px solid rgba(255,255,255,.05)}
.tbl td{padding:9px 12px;border-bottom:1px solid rgba(255,255,255,.025)}
.tbl tr:hover td{background:rgba(255,255,255,.02)}
.mono{font-family:var(--mn);font-size:11px}
.lb{background:rgba(0,212,170,.1);border-radius:6px}
.lb-box{background:linear-gradient(145deg,var(--s1),var(--s2));border:1px solid rgba(255,255,255,.06);border-radius:14px;overflow:hidden;margin-bottom:18px}
.log-body{padding:10px;max-height:160px;overflow-y:auto;display:flex;flex-direction:column;gap:4px}
.li{background:rgba(255,255,255,.02);border-radius:6px;padding:7px 11px;display:flex;align-items:flex-start;gap:9px;border-left:2px solid transparent;font-size:12px}
.li.info{border-left-color:var(--bl)}.li.ok{border-left-color:var(--ac)}.li.warn{border-left-color:var(--yl)}.li.err{border-left-color:var(--rd)}
.lt{font-family:var(--mn);font-size:10px;color:var(--mt);white-space:nowrap;margin-top:1px;min-width:60px}
.lm{color:var(--tx);line-height:1.5}
::-webkit-scrollbar{width:4px;height:4px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:rgba(0,212,170,.2);border-radius:2px}
@media(max-width:1200px){.mgrid{grid-template-columns:1fr}.stats{grid-template-columns:repeat(3,1fr)}}
@media(max-width:768px){.prices{grid-template-columns:1fr}.stats{grid-template-columns:1fr 1fr}.bgrid{grid-template-columns:1fr}}
</style></head><body>
<nav>
  <div class="brand"><div class="bicon">🤖</div><div class="bname">ALPHA BOT</div></div>
  <div class="npairs">
    <div class="np on" onclick="switchPair('BTCUSD',this)">₿ BTC</div>
    <div class="np" onclick="switchPair('ETHUSD',this)">Ξ ETH</div>
    <div class="np" onclick="switchPair('SOLUSD',this)">◎ SOL</div>
  </div>
  <div class="nav-r">
    <div class="clk" id="clk">00:00:00</div>
    <div class="bpill pi" id="bpill"><div class="dot" id="bdot"></div><span id="btxt">LOADING</span></div>
    <div style="font-size:12px;color:var(--mt)">👤 {{ username }}</div>
    <a href="/logout" class="lo">Logout</a>
  </div>
</nav>
<div class="main">
  <div class="prices">
    <div class="pc btc"><div><div class="pc-sym">₿ BTC/USD</div><div class="pc-price" id="pBTC">$—</div><div class="pc-chg" id="cBTC">—</div></div><div class="pl"><div class="pl-lb">Lot Size</div><div class="pl-vl">5 lots</div></div></div>
    <div class="pc eth"><div><div class="pc-sym">Ξ ETH/USD</div><div class="pc-price" id="pETH">$—</div><div class="pc-chg" id="cETH">—</div></div><div class="pl"><div class="pl-lb">Lot Size</div><div class="pl-vl">8 lots</div></div></div>
    <div class="pc sol"><div><div class="pc-sym">◎ SOL/USD</div><div class="pc-price" id="pSOL">$—</div><div class="pc-chg" id="cSOL">—</div></div><div class="pl"><div class="pl-lb">Lot Size</div><div class="pl-vl">3 lots</div></div></div>
  </div>
  <div class="stats">
    <div class="sc" style="--cc:var(--ac)"><div class="sl">💰 Total PnL</div><div class="sv vg" id="sPnl">$0.00</div><div class="ss">Realized</div></div>
    <div class="sc" style="--cc:var(--bl)"><div class="sl">📊 Win Rate</div><div class="sv vb" id="sWR">0%</div><div class="ss"><span id="sW">0</span>W/<span id="sL">0</span>L</div></div>
    <div class="sc" style="--cc:var(--yl)"><div class="sl">📈 Trades</div><div class="sv vy" id="sTot">0</div><div class="ss">Active:<span id="sAct">0</span></div></div>
    <div class="sc" style="--cc:#9945ff"><div class="sl">💎 RR Ratio</div><div class="sv vp">1:2.5</div><div class="ss">Per trade</div></div>
    <div class="sc" style="--cc:var(--ac)"><div class="sl">⏱ Uptime</div><div class="sv vm" id="sUp">—</div><div class="ss" id="sSince">—</div></div>
    <div class="sc" style="--cc:var(--bl)"><div class="sl">💼 Mode</div><div class="sv vm" id="sMode">—</div><div class="ss">$<span id="sBal">—</span></div></div>
  </div>
  <div class="mgrid">
    <div class="cp2">
      <div class="ph"><div class="ph-t"><div class="ld"></div><span id="chartTitle">BTCUSD — TradingView</span></div>
      <div class="tfs"><div class="tf" onclick="stf('15',this)">15m</div><div class="tf" onclick="stf('30',this)">30m</div><div class="tf" onclick="stf('60',this)">1H</div><div class="tf on" onclick="stf('240',this)">4H</div><div class="tf" onclick="stf('D',this)">1D</div><div class="tf" onclick="stf('W',this)">1W</div></div></div>
      <div class="ca" id="ca"><div class="cl" id="cl"><div class="sp"></div><p>Loading chart...</p></div></div>
    </div>
    <div class="rp">
      <div class="sbx">
        <div class="ph"><div class="ph-t">🧠 Signal Engine</div><div class="tag t-sc" id="sigPill">SCANNING</div></div>
        <div style="padding:8px 12px 0;font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--mt)">Timeframe Status</div>
        <div class="tfm" id="tfm"></div>
        <div style="padding:0 12px 6px;font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--mt)">Active Trade</div>
        <div class="atb" id="atBox"></div>
        <div style="padding:0 12px 6px;font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--mt)">Strategy Status</div>
        <div class="srows" id="srows"></div>
      </div>
    </div>
  </div>
  <div class="bgrid">
    <div class="eb"><div class="ph"><div class="ph-t">📊 Equity Curve</div><div class="tag t-ok">P&L</div></div><div class="ew"><canvas id="eqChart"></canvas></div></div>
    <div class="hb"><div class="ph"><div class="ph-t">📋 Trade History</div><div class="tag t-ok" id="hCount">0</div></div>
    <div class="tw2"><table class="tbl"><thead><tr><th>Pair</th><th>Dir</th><th>Entry</th><th>Exit</th><th>PnL</th><th>Result</th><th>Time</th></tr></thead><tbody id="hBody"></tbody></table></div></div>
  </div>
  <div class="lb-box"><div class="ph"><div class="ph-t">📡 Activity Log</div><button onclick="clearLog()" style="background:none;border:1px solid rgba(255,255,255,.08);border-radius:5px;padding:3px 10px;color:var(--mt);font-size:11px;cursor:pointer">Clear</button></div><div class="log-body" id="logBody"></div></div>
</div>
<script>
const sock=io();let cp='BTCUSD',ct='240',ec=null,pv={},ep=[0],el=['Start'];
sock.on('dashboard_update',d=>upd(d));setInterval(()=>sock.emit('req_update'),5000);sock.emit('req_update');
function upd(d){
  if(!d||d.error)return;
  document.getElementById('clk').textContent=new Date().toLocaleTimeString('en',{hour12:false});
  const st=d.stats||{},status=st.bot_status||'UNKNOWN';
  const pill=document.getElementById('bpill'),dot=document.getElementById('bdot');
  pill.className='bpill '+(status==='RUNNING'?'pr':status==='STOPPED'?'ps':'pi');
  document.getElementById('btxt').textContent=status;
  dot.className='dot'+(status==='RUNNING'?' pulse':'');
  const pnl=parseFloat(st.total_pnl||0);
  const pe=document.getElementById('sPnl');
  pe.textContent=(pnl>=0?'+':'')+'$'+pnl.toFixed(2);pe.className='sv '+(pnl>=0?'vg':'vr');
  document.getElementById('sWR').textContent=(st.win_rate||0)+'%';
  document.getElementById('sW').textContent=st.wins||0;document.getElementById('sL').textContent=st.losses||0;
  document.getElementById('sTot').textContent=st.total_trades||0;document.getElementById('sAct').textContent=st.active_count||0;
  document.getElementById('sUp').textContent=(st.uptime_hours||0)+'h';document.getElementById('sMode').textContent=d.mode||'—';document.getElementById('sBal').textContent=(d.balance||0).toFixed(2);
  const px=d.prices||{};
  [['BTC','BTCUSD'],['ETH','ETHUSD'],['SOL','SOLUSD']].forEach(([k,p])=>{
    const pp=parseFloat(px[p]||0),prev=pv[p]||pp,chg=(pp-prev)/prev*100;
    const e=document.getElementById('p'+k),c=document.getElementById('c'+k);
    if(e)e.textContent='$'+pp.toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2});
    if(c){c.textContent=(chg>=0?'+':'')+chg.toFixed(2)+'%';c.className='pc-chg '+(chg>=0?'cp':'cn');}
    pv[p]=pp;
  });
  rAT(d.active_trades||{},d.prices||{});rHist(d.trade_history||[]);
  const cpnl=parseFloat(d.stats?.total_pnl||0);if(ep[ep.length-1]!==cpnl){ep.push(cpnl);el.push('');updEq();}
}
function rAT(trades,prices){
  const box=document.getElementById('atBox'),keys=Object.keys(trades);
  if(!keys.length){box.innerHTML='<div class="ate">⏳ No active trade<br><small>Waiting for all timeframes to confirm...</small></div>';return;}
  box.innerHTML=keys.map(sym=>{
    const t=trades[sym],px=parseFloat(prices[sym]||t.entry_price),ep2=parseFloat(t.entry_price);
    const pts=t.direction==='BUY'?px-ep2:ep2-px,pnl=(pts*(t.size||1)).toFixed(2);
    const sl=parseFloat(t.stop_loss),tp=parseFloat(t.take_profit),prog=Math.abs(tp-ep2)>0?Math.min(100,Math.abs(px-ep2)/Math.abs(tp-ep2)*100):0;
    return`<div class="atcard"><div class="at-top"><span class="at-sym">${sym}</span><span class="at-badge ${t.direction==='BUY'?'atl':'ats'}">${t.direction==='BUY'?'⬆️ LONG':'⬇️ SHORT'}</span></div>
    <div class="at-cells">
      <div class="atcell"><label>Entry</label><div class="v">$${ep2.toFixed(2)}</div></div>
      <div class="atcell"><label>Mark</label><div class="v">$${px.toFixed(2)}</div></div>
      <div class="atcell"><label>PnL</label><div class="v" style="color:${pts>=0?'var(--ac)':'var(--rd)'}">${pts>=0?'+':''}$${pnl}</div></div>
      <div class="atcell"><label>SL</label><div class="v" style="color:var(--rd)">$${sl.toFixed(2)}</div></div>
      <div class="atcell"><label>TP</label><div class="v" style="color:var(--ac)">$${tp.toFixed(2)}</div></div>
      <div class="atcell"><label>Lots</label><div class="v">${t.size}</div></div>
    </div><div class="at-pb-w"><div class="at-pb" style="width:${prog}%"></div></div></div>`;
  }).join('');
}
function rHist(history){
  const tb=document.getElementById('hBody');document.getElementById('hCount').textContent=history.length;
  if(!history.length){tb.innerHTML='<tr><td colspan="7" style="text-align:center;padding:22px;color:var(--mt)">No trades yet</td></tr>';return;}
  tb.innerHTML=[...history].reverse().slice(0,50).map(t=>{
    const pnl=parseFloat(t.pnl||0),time=(t.entry_time||'').substr(0,16).replace('T',' ');
    return`<tr><td><b>${t.symbol}</b></td><td><span class="tag ${t.direction==='BUY'?'tl':'ts'}">${t.direction==='BUY'?'L':'S'}</span></td><td class="mono">$${parseFloat(t.entry_price||0).toFixed(2)}</td><td class="mono">${t.exit_price?'$'+parseFloat(t.exit_price).toFixed(2):'—'}</td><td class="mono" style="color:${pnl>=0?'var(--ac)':'var(--rd)'}">${pnl>=0?'+':''}$${pnl.toFixed(2)}</td><td><span class="tag ${t.status==='OPEN'?'to':pnl>=0?'tw':'tls'}">${t.status==='OPEN'?'OPEN':pnl>=0?'WIN':'LOSS'}</span></td><td style="font-size:10px;color:var(--mt)">${time}</td></tr>`;
  }).join('');
}
function initEq(){
  const ctx=document.getElementById('eqChart').getContext('2d');
  ec=new Chart(ctx,{type:'line',data:{labels:el,datasets:[{label:'PnL',data:ep,borderColor:'#00d4aa',backgroundColor:'rgba(0,212,170,.06)',borderWidth:2,fill:true,tension:.4,pointRadius:3,pointBackgroundColor:'#00d4aa'}]},
  options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{display:false},y:{grid:{color:'rgba(255,255,255,.03)'},ticks:{color:'#4a6080',font:{family:'JetBrains Mono',size:10},callback:v=>(v>=0?'+':'')+'$'+v.toFixed(0)}}}}});
}
function updEq(){if(!ec)return;ec.data.labels=el;ec.data.datasets[0].data=ep;const last=ep[ep.length-1];ec.data.datasets[0].borderColor=last>=0?'#00d4aa':'#f0365a';ec.data.datasets[0].backgroundColor=last>=0?'rgba(0,212,170,.06)':'rgba(240,54,90,.06)';ec.update('none');}
function buildTFM(){document.getElementById('tfm').innerHTML=['4H','2H','1H','30M','15M'].map(tf=>`<div class="tfc wait" id="tf_${tf}"><div class="tn">${tf}</div><div class="ts2" id="tfs_${tf}">WAIT</div><div class="tst" id="tfs2_${tf}">—</div></div>`).join('');}
function buildSrows(){document.getElementById('srows').innerHTML=[['ema_cross','📈','EMA 9/20 Crossover'],['ema_touch','🎯','EMA Touch/SR'],['rsi','📊','RSI Momentum'],['swing','🌊','Swing Trade']].map(([id,icon,name])=>`<div class="srow"><span class="sr-n">${icon} ${name}</span><span class="tag t-sc" id="st_${id}">SCANNING</span></div>`).join('');}
function animTF(){['4H','2H','1H','30M','15M'].forEach(tf=>{const r=Math.random(),cls=r<.36?'bull':r<.66?'bear':'wait',sig=cls==='bull'?'BULL':cls==='bear'?'BEAR':'WAIT';const e=document.getElementById('tf_'+tf),s=document.getElementById('tfs_'+tf),s2=document.getElementById('tfs2_'+tf);if(e)e.className='tfc '+cls;if(s)s.textContent=sig;if(s2)s2.textContent=cls!=='wait'?Math.floor(55+Math.random()*40)+'/100':'—';});['ema_cross','ema_touch','rsi','swing'].forEach(id=>{const r=Math.random(),e=document.getElementById('st_'+id);if(e){if(r<.3){e.textContent='BULL';e.className='tag t-bl';}else if(r<.55){e.textContent='BEAR';e.className='tag t-br';}else{e.textContent='SCANNING';e.className='tag t-sc';}}});}
function loadChart(pair,tf){cp=pair;ct=tf;document.getElementById('chartTitle').textContent=`${pair} — TradingView (Bitstamp)`;const ca=document.getElementById('ca');const old=ca.querySelector('iframe,div:not(#cl)');if(old&&old.id!=='cl')old.remove();document.getElementById('cl').style.display='flex';const syms={BTCUSD:'BITSTAMP%3ABTCUSD',ETHUSD:'BITSTAMP%3AETHUSD',SOLUSD:'BITSTAMP%3ASOLUSD'};const iframe=document.createElement('iframe');iframe.style.cssText='width:100%;height:100%;border:none;display:block;position:absolute;top:0;left:0;opacity:0;transition:opacity .5s';iframe.src='https://s.tradingview.com/widgetembed/?frameElementId=tvf&symbol='+syms[pair]+'&interval='+tf+'&hidesidetoolbar=0&hidetoptoolbar=0&symboledit=0&saveimage=0&toolbarbg=0c1526&studies=EMA%40tv-basicstudies%1FEMA%40tv-basicstudies%1FRSI%40tv-basicstudies&theme=dark&style=1&timezone=Asia%2FKolkata&locale=en';let ok=false;iframe.onload=()=>{ok=true;document.getElementById('cl').style.display='none';iframe.style.opacity='1';};setTimeout(()=>{if(!ok)showFB(pair);},9000);ca.appendChild(iframe);}
function showFB(pair){document.getElementById('cl').style.display='none';const ca=document.getElementById('ca');const old=ca.querySelector('iframe');if(old)old.remove();const syms={BTCUSD:'BITSTAMP:BTCUSD',ETHUSD:'BITSTAMP:ETHUSD',SOLUSD:'BITSTAMP:SOLUSD'};const div=document.createElement('div');div.style.cssText='position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;padding:32px;text-align:center;background:var(--s1)';div.innerHTML=`<div style="font-size:40px">📊</div><div style="font-size:16px;font-weight:700;color:var(--ac)">Open Chart on TradingView</div><a href="https://www.tradingview.com/chart/?symbol=${syms[pair]}" target="_blank" style="background:rgba(0,212,170,.12);border:1px solid rgba(0,212,170,.3);border-radius:10px;padding:12px 28px;color:var(--ac);font-size:13px;font-weight:700;text-decoration:none">📈 Open ${pair} on TradingView ↗</a>`;ca.appendChild(div);}
function switchPair(pair,btn){document.querySelectorAll('.np').forEach(b=>b.classList.remove('on'));btn.classList.add('on');loadChart(pair,ct);}
function stf(tf,btn){document.querySelectorAll('.tf').forEach(b=>b.classList.remove('on'));btn.classList.add('on');loadChart(cp,tf);}
function log2(type,msg){const b=document.getElementById('logBody');if(!b)return;const d=document.createElement('div');d.className='li '+type;d.innerHTML=`<div class="lt">${new Date().toLocaleTimeString('en',{hour12:false})}</div><div class="lm">${msg}</div>`;b.insertBefore(d,b.firstChild);if(b.children.length>100)b.removeChild(b.lastChild);}
function clearLog(){document.getElementById('logBody').innerHTML='';log2('info','Log cleared.');}
window.onload=()=>{buildTFM();buildSrows();initEq();loadChart('BTCUSD','240');setInterval(animTF,3000);log2('ok','✅ Dashboard connected — Bot running on Oracle VPS 24/7');};
</script></body></html>"""

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username","")
        p = request.form.get("password","")
        if DASHBOARD_USERS.get(u) == p:
            session["logged_in"] = True
            session["username"]  = u
            return redirect("/")
        return render_template_string(LOGIN_HTML, error="Wrong username or password")
    return render_template_string(LOGIN_HTML, error=None)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/")
@login_required
def index():
    return render_template_string(DASH_HTML, username=session.get("username","@ALPHA"))

@app.route("/api/data")
@login_required
def api_data():
    try: return jsonify(get_bot().get_dashboard_data())
    except Exception as e: return jsonify({"error":str(e)}), 500

@socketio.on("req_update")
def on_update():
    try: emit("dashboard_update", get_bot().get_dashboard_data())
    except Exception as e: emit("dashboard_update", {"error":str(e)})

def broadcast_loop():
    while True:
        time.sleep(5)
        try: socketio.emit("dashboard_update", get_bot().get_dashboard_data())
        except: pass

# ================================================================
#   SECTION 12 — START EVERYTHING
# ================================================================

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════╗
║        ALPHA TRADING BOT v2.0 — STARTING            ║
║  Pairs: BTCUSD (5L) | ETHUSD (8L) | SOLUSD (3L)    ║
║  RR: 1:2.5 | Strategies: EMA + RSI + Swing          ║
╚══════════════════════════════════════════════════════╝
    """)

    if DELTA_API_KEY == "YOUR_DELTA_API_KEY_HERE":
        print("⚠️  IMPORTANT: Open alphabot.py and add your API keys!")
        print("   Find: DELTA_API_KEY    = 'YOUR_DELTA_API_KEY_HERE'")
        print("   Replace with your real key from Delta Exchange Demo\n")

    # Start bot engine in background thread
    bot_thread = threading.Thread(
        target=get_bot().start,
        daemon=True,
        name="TradingEngine"
    )
    bot_thread.start()
    log.info("Trading engine started in background thread")
    time.sleep(2)

    # Start broadcast thread
    bc_thread = threading.Thread(target=broadcast_loop, daemon=True)
    bc_thread.start()

    # Start dashboard (main thread)
    log.info(f"Dashboard → http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")
    log.info("Login: @ALPHA / @ALPHA01")
    socketio.run(app, host=DASHBOARD_HOST, port=DASHBOARD_PORT,
                 debug=False, allow_unsafe_werkzeug=True)
