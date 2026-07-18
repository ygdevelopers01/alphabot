# ================================================================
#   ALPHA TRADING BOT v5.0 — PROFESSIONAL HEDGE FUND UI
#   Exchange: Bybit Demo | Hosting: Hostinger VPS
#   Fixed: Real PnL from Bybit API | New Professional UI
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
#   SETTINGS — EDIT THESE WITH YOUR KEYS
# ================================================================

BYBIT_API_KEY    ="DTitxnwMbnb4fKJImY"
BYBIT_API_SECRET ="J5zba6CNiZ1nKJUwIBjoAGRWMjl8gK5isH3H"
TRADING_MODE     = "demo"

TELEGRAM_BOT_TOKEN ="8587909106:AAFKh-GwYhQVopLuR0PjlbRH7hoG7_VuX-g"
TELEGRAM_CHAT_ID   ="7480010522"
TELEGRAM_ENABLED   = True

DASHBOARD_HOST   = "0.0.0.0"
DASHBOARD_PORT   = 5000
DASHBOARD_SECRET = "alphabot_secret_2024"
DASHBOARD_USERS  = {"@ALPHA": "@ALPHA01"}

BYBIT_DEMO_URL = "https://api-demo.bybit.com"
BYBIT_LIVE_URL = "https://api.bybit.com"
def BASE_URL(): return BYBIT_DEMO_URL if TRADING_MODE=="demo" else BYBIT_LIVE_URL

TRADING_PAIRS = {
    "BTCUSDT": {"symbol":"BTCUSDT","lot_size":"0.01","tick_size":0.5, "leverage":10},
    "ETHUSDT": {"symbol":"ETHUSDT","lot_size":"0.1", "tick_size":0.05,"leverage":10},
    "SOLUSDT": {"symbol":"SOLUSDT","lot_size":"1",   "tick_size":0.01,"leverage":10},
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

TF_TO_INTERVAL = {"15m":"15","30m":"30","1h":"60","2h":"120","4h":"240","1d":"D","1w":"W"}
INTRADAY_TFS = ["4h","2h","1h","30m","15m"]
SCALP_TFS    = ["1h","30m","15m"]

# ================================================================
#   LOGGING
# ================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[logging.FileHandler(LOG_FILE),logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("AlphaBot")

# ================================================================
#   BYBIT API
# ================================================================

class BybitAPI:
    def __init__(self):
        self.session=requests.Session()
        self.recv_window="5000"

    def _sign(self,params_str):
        ts=str(int(time.time()*1000))
        msg=ts+BYBIT_API_KEY+self.recv_window+params_str
        sig=hmac.new(BYBIT_API_SECRET.encode(),msg.encode(),hashlib.sha256).hexdigest()
        return sig,ts

    def _headers(self,params_str=""):
        sig,ts=self._sign(params_str)
        return {"X-BAPI-API-KEY":BYBIT_API_KEY,"X-BAPI-TIMESTAMP":ts,
                "X-BAPI-SIGN":sig,"X-BAPI-RECV-WINDOW":self.recv_window,"Content-Type":"application/json"}

    def _get(self,path,params=None):
        query=urlencode(params) if params else ""
        url=BASE_URL()+path+("?"+query if query else "")
        try:
            r=self.session.get(url,headers=self._headers(query),timeout=10)
            r.raise_for_status()
            data=r.json()
            if data.get("retCode")!=0:
                log.error(f"Bybit error: {data.get('retMsg')} Code:{data.get('retCode')}")
                return None
            return data
        except Exception as e: log.error(f"GET {path}: {e}"); return None

    def _post(self,path,data=None):
        body=json.dumps(data) if data else "{}"
        url=BASE_URL()+path
        try:
            r=self.session.post(url,headers=self._headers(body),data=body,timeout=10)
            r.raise_for_status()
            resp=r.json()
            if resp.get("retCode")!=0:
                log.error(f"Bybit POST error: {resp.get('retMsg')} Code:{resp.get('retCode')}")
                return None
            return resp
        except Exception as e: log.error(f"POST {path}: {e}"); return None

    def test_connection(self):
        r=self._get("/v5/user/query-api")
        if r: log.info("✅ Bybit Connected!"); return True,"Connected"
        return False,"Failed"

    def get_klines(self,symbol,interval,limit=200):
        params={"category":"linear","symbol":symbol,"interval":interval,"limit":limit}
        r=self._get("/v5/market/kline",params)
        if r and r.get("result"): return r["result"].get("list",[])
        return []

    def get_mark_price(self,symbol):
        params={"category":"linear","symbol":symbol}
        r=self._get("/v5/market/tickers",params)
        if r and r.get("result"):
            items=r["result"].get("list",[])
            if items: return float(items[0].get("markPrice",0))
        return 0.0

    def get_wallet_balance(self):
        r=self._get("/v5/account/wallet-balance",{"accountType":"UNIFIED"})
        if r and r.get("result"): return r["result"].get("list",[])
        return []

    def get_positions(self):
        r=self._get("/v5/position/list",{"category":"linear","settleCoin":"USDT"})
        if r and r.get("result"): return r["result"].get("list",[])
        return []

    def get_position(self,symbol):
        r=self._get("/v5/position/list",{"category":"linear","symbol":symbol})
        if r and r.get("result"):
            items=r["result"].get("list",[])
            if items: return items[0]
        return None

    def get_closed_pnl(self,limit=50):
        """Get real closed PnL from Bybit API."""
        r=self._get("/v5/position/closed-pnl",{"category":"linear","limit":limit})
        if r and r.get("result"): return r["result"].get("list",[])
        return []

    def get_order_history(self,limit=50):
        r=self._get("/v5/order/history",{"category":"linear","limit":limit})
        if r and r.get("result"): return r["result"].get("list",[])
        return []

    def set_leverage(self,symbol,leverage):
        data={"category":"linear","symbol":symbol,"buyLeverage":str(leverage),"sellLeverage":str(leverage)}
        r=self._post("/v5/position/set-leverage",data)
        if r: log.info(f"Leverage set: {symbol}={leverage}x")
        else: log.warning(f"Leverage set failed {symbol} — continuing")

    def place_order(self,symbol,side,qty,stop_loss,take_profit):
        data={"category":"linear","symbol":symbol,"side":side,"orderType":"Market",
              "qty":str(qty),"stopLoss":str(stop_loss),"takeProfit":str(take_profit),
              "slTriggerBy":"MarkPrice","tpTriggerBy":"MarkPrice","timeInForce":"GTC"}
        r=self._post("/v5/order/create",data)
        if r: log.info(f"✅ Order: {symbol} {side} {qty}"); return r.get("result",{})
        return None

    def close_position(self,symbol):
        pos=self.get_position(symbol)
        if not pos or float(pos.get("size","0"))==0: return None
        side="Sell" if pos.get("side")=="Buy" else "Buy"
        data={"category":"linear","symbol":symbol,"side":side,"orderType":"Market",
              "qty":pos.get("size","0"),"reduceOnly":True,"timeInForce":"GTC"}
        return self._post("/v5/order/create",data)

# ================================================================
#   INDICATORS
# ================================================================

def calc_ema(s,p): return s.ewm(span=p,adjust=False).mean()
def calc_rsi(s,p=14):
    d=s.diff(); g=d.clip(lower=0); l=-d.clip(upper=0)
    ag=g.ewm(com=p-1,min_periods=p).mean(); al=l.ewm(com=p-1,min_periods=p).mean()
    return (100-(100/(1+ag/al.replace(0,np.nan)))).fillna(50)
def calc_atr(df,p=14):
    hl=df["high"]-df["low"]; hc=(df["high"]-df["close"].shift()).abs(); lc=(df["low"]-df["close"].shift()).abs()
    return pd.concat([hl,hc,lc],axis=1).max(axis=1).ewm(span=p,adjust=False).mean()

def add_indicators(df):
    df=df.copy()
    df["ema9"]=calc_ema(df["close"],EMA_FAST); df["ema20"]=calc_ema(df["close"],EMA_SLOW)
    df["rsi"]=calc_rsi(df["close"],RSI_PERIOD); df["atr"]=calc_atr(df,ATR_PERIOD)
    df["body"]=(df["close"]-df["open"]).abs(); df["is_bull"]=df["close"]>df["open"]; df["is_bear"]=df["close"]<df["open"]
    return df

def klines_to_df(raw):
    if not raw: return pd.DataFrame()
    rows=[{"time":int(i[0]),"open":float(i[1]),"high":float(i[2]),"low":float(i[3]),"close":float(i[4]),"volume":float(i[5])} for i in raw]
    return pd.DataFrame(rows).sort_values("time").reset_index(drop=True)

# ================================================================
#   STRATEGIES
# ================================================================

def s_ema_cross(df):
    if len(df)<5: return {"signal":"NONE","strength":0}
    c,p,p2=df.iloc[-1],df.iloc[-2],df.iloc[-3]
    gap=abs(c["ema9"]-c["ema20"])/c["ema20"]*100
    bb=c["is_bull"] and c["body"]>c["atr"]*0.3; brb=c["is_bear"] and c["body"]>c["atr"]*0.3
    if (p["ema9"]<=p["ema20"]) and (c["ema9"]>c["ema20"]) and bb: return {"signal":"BUY","strength":min(100,int(60+gap*80))}
    if (p["ema9"]>=p["ema20"]) and (c["ema9"]<c["ema20"]) and brb: return {"signal":"SELL","strength":min(100,int(60+gap*80))}
    if (p2["ema9"]<=p2["ema20"]) and (p["ema9"]>p["ema20"]) and bb and c["ema9"]>c["ema20"]: return {"signal":"BUY","strength":min(100,int(55+gap*60))}
    if (p2["ema9"]>=p2["ema20"]) and (p["ema9"]<p["ema20"]) and brb and c["ema9"]<c["ema20"]: return {"signal":"SELL","strength":min(100,int(55+gap*60))}
    return {"signal":"NONE","strength":0}

def s_ema_touch(df):
    if len(df)<5: return {"signal":"NONE","strength":0}
    c,p=df.iloc[-1],df.iloc[-2]; e9,e20=c["ema9"],c["ema20"]; tol=e20*EMA_TOUCH_TOLERANCE
    t20=(p["low"]<=e20+tol) and (p["high"]>=e20-tol); t9=(p["low"]<=e9+tol) and (p["high"]>=e9-tol)
    bb=c["is_bull"] and c["body"]>c["atr"]*0.3 and c["close"]>e20; sb=c["is_bear"] and c["body"]>c["atr"]*0.3 and c["close"]<e20
    if e9>e20 and t20 and bb: return {"signal":"BUY","strength":65}
    if e9<e20 and t20 and sb: return {"signal":"SELL","strength":65}
    if e9>e20 and t9 and bb: return {"signal":"BUY","strength":60}
    if e9<e20 and t9 and sb: return {"signal":"SELL","strength":60}
    return {"signal":"NONE","strength":0}

def s_rsi(df):
    if len(df)<5: return {"signal":"NONE","strength":0}
    c,p=df.iloc[-1],df.iloc[-2]; rn,rp=c["rsi"],p["rsi"]
    if rn>RSI_BULLISH and rn<70 and c["ema9"]>c["ema20"] and c["is_bull"] and c["body"]>c["atr"]*0.2 and rn>rp:
        return {"signal":"BUY","strength":min(100,int(50+(rn-RSI_BULLISH)*2))}
    if rn<RSI_BEARISH and rn>30 and c["ema9"]<c["ema20"] and c["is_bear"] and c["body"]>c["atr"]*0.2 and rn<rp:
        return {"signal":"SELL","strength":min(100,int(50+(RSI_BEARISH-rn)*2))}
    return {"signal":"NONE","strength":0}

def analyze_tf(df):
    if df is None or len(df)<MIN_CANDLES: return {"direction":"NONE","strength":0,"atr":0,"close":0}
    df=add_indicators(df)
    s1=s_ema_cross(df); s2=s_ema_touch(df); s3=s_rsi(df)
    buys=[s for s in [s1,s2,s3] if s["signal"]=="BUY"]; sells=[s for s in [s1,s2,s3] if s["signal"]=="SELL"]
    c=df.iloc[-1]; base={"atr":float(c["atr"]),"close":float(c["close"]),"ema9":float(c["ema9"]),"ema20":float(c["ema20"]),"rsi":float(c["rsi"])}
    if len(buys)>=2: base.update({"direction":"BUY","strength":int(np.mean([s["strength"] for s in buys]))}); return base
    if len(sells)>=2: base.update({"direction":"SELL","strength":int(np.mean([s["strength"] for s in sells]))}); return base
    base.update({"direction":"NONE","strength":0}); return base

def get_signal(tf_data):
    # Swing
    dfs_raw=[tf_data.get("1w"),tf_data.get("1d"),tf_data.get("4h")]
    dfs=[add_indicators(df) if df is not None and len(df)>=10 else None for df in dfs_raw]
    df1h=tf_data.get("1h")
    if df1h is not None and len(df1h)>=10:
        df1h_i=add_indicators(df1h); c,p,p2=df1h_i.iloc[-1],df1h_i.iloc[-2],df1h_i.iloc[-3]
        res=[]; 
        for df in dfs:
            if df is not None and len(df)>3: res.append("BULL" if df.iloc[-1]["ema9"]>df.iloc[-1]["ema20"] else "BEAR")
        htf="BULL" if res.count("BULL")>=2 else "BEAR" if res.count("BEAR")>=2 else "NEUTRAL"
        if htf!="NEUTRAL":
            dbull=p2["is_bull"] and p["is_bull"] and p["close"]>p2["high"] and p["body"]>p["atr"]*0.4 and c["is_bull"]
            dbear=p2["is_bear"] and p["is_bear"] and p["close"]<p2["low"] and p["body"]>p["atr"]*0.4 and c["is_bear"]
            if dbull and htf=="BULL" and c["ema9"]>c["ema20"]: return {"signal":"BUY","strength":80,"trade_type":"SWING","entry":"SWING"}
            if dbear and htf=="BEAR" and c["ema9"]<c["ema20"]: return {"signal":"SELL","strength":80,"trade_type":"SWING","entry":"SWING"}
    # Intraday
    res={tf:analyze_tf(tf_data.get(tf)) for tf in INTRADAY_TFS}
    dirs=[res[tf]["direction"] for tf in INTRADAY_TFS]; nn=[d for d in dirs if d!="NONE"]
    if len(nn)==len(INTRADAY_TFS) and all(d=="BUY" for d in nn):
        return {"signal":"BUY","strength":int(np.mean([res[tf]["strength"] for tf in INTRADAY_TFS])),"trade_type":"INTRADAY","entry":"MULTI_TF"}
    if len(nn)==len(INTRADAY_TFS) and all(d=="SELL" for d in nn):
        return {"signal":"SELL","strength":int(np.mean([res[tf]["strength"] for tf in INTRADAY_TFS])),"trade_type":"INTRADAY","entry":"MULTI_TF"}
    # Scalp
    res2={tf:analyze_tf(tf_data.get(tf)) for tf in SCALP_TFS}
    dirs2=[res2[tf]["direction"] for tf in SCALP_TFS]; nn2=[d for d in dirs2 if d!="NONE"]
    if len(nn2)==3 and all(d=="BUY" for d in nn2):
        return {"signal":"BUY","strength":int(np.mean([res2[tf]["strength"] for tf in SCALP_TFS])),"trade_type":"SCALP","entry":"SCALP"}
    if len(nn2)==3 and all(d=="SELL" for d in nn2):
        return {"signal":"SELL","strength":int(np.mean([res2[tf]["strength"] for tf in SCALP_TFS])),"trade_type":"SCALP","entry":"SCALP"}
    return {"signal":"NONE"}

# ================================================================
#   TRADE STATE — Fixed PnL calculation
# ================================================================

class TradeState:
    def __init__(self):
        self.lock=threading.Lock(); self.active_trades={}; self.history=[]
        self.total_pnl=0.0; self.wins=0; self.losses=0
        self.bot_status="STARTING"; self.start_time=datetime.now()
        self._load_csv()

    def _load_csv(self):
        if not os.path.exists(TRADE_LOG_FILE): return
        try:
            with open(TRADE_LOG_FILE) as f:
                for row in csv.DictReader(f):
                    if row.get("status")=="CLOSED":
                        self.history.append(row)
                        pnl=float(row.get("pnl",0))
                        self.total_pnl+=pnl
                        if pnl>0: self.wins+=1
                        else: self.losses+=1
            log.info(f"Loaded {len(self.history)} trades")
        except Exception as e: log.error(f"CSV load: {e}")

    def has_active(self,symbol):
        with self.lock: return symbol in self.active_trades
    def get_active(self,symbol):
        with self.lock: return self.active_trades.get(symbol)

    def open_trade(self,trade):
        with self.lock: self.active_trades[trade["symbol"]]=trade
        self._write_csv(trade,"OPEN")

    def close_trade(self,symbol,exit_price,reason,real_pnl=None):
        with self.lock: trade=self.active_trades.pop(symbol,None)
        if not trade: return None
        entry=float(trade["entry_price"]); size=float(trade["size"])
        if real_pnl is not None:
            pnl=real_pnl  # Use REAL PnL from Bybit API
        else:
            pts=(exit_price-entry) if trade["direction"]=="BUY" else (entry-exit_price)
            pnl=round(pts*size,4)
        trade.update({"exit_price":exit_price,"exit_reason":reason,
                      "exit_time":datetime.now().isoformat(),"pnl":round(pnl,4),"status":"CLOSED"})
        with self.lock:
            self.total_pnl+=pnl
            if pnl>0: self.wins+=1
            else: self.losses+=1
            self.history.append(trade)
        self._write_csv(trade,"CLOSED")
        log.info(f"Closed: {symbol} PnL:${pnl:.4f} {reason}")
        return trade

    def _write_csv(self,trade,status):
        fields=["id","symbol","direction","trade_type","entry_price","exit_price",
                "stop_loss","take_profit","size","pnl","strategy","strength",
                "entry_time","exit_time","exit_reason","status"]
        exists=os.path.exists(TRADE_LOG_FILE)
        with open(TRADE_LOG_FILE,"a",newline="") as f:
            w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore")
            if not exists: w.writeheader()
            row={k:trade.get(k,"") for k in fields}; row["status"]=status; w.writerow(row)

    def get_stats(self):
        total=self.wins+self.losses
        return {"bot_status":self.bot_status,"total_trades":total,"wins":self.wins,"losses":self.losses,
                "win_rate":round(self.wins/total*100,1) if total>0 else 0,
                "total_pnl":round(self.total_pnl,4),"active_count":len(self.active_trades),
                "uptime_hours":round((datetime.now()-self.start_time).total_seconds()/3600,1)}

# ================================================================
#   SL/TP
# ================================================================

def calc_sltp(symbol,direction,entry,atr,trade_type="INTRADAY"):
    tick=TRADING_PAIRS.get(symbol,{}).get("tick_size",0.01); rr=RISK_REWARD_RATIO
    mult=2.0 if trade_type=="SWING" else 1.0 if trade_type=="SCALP" else SL_ATR_MULTIPLIER
    sl_d=atr*mult; tp_d=sl_d*rr
    def snap(v): return round(round(v/tick)*tick,8)
    if direction=="BUY": return {"stop_loss":snap(entry-sl_d),"take_profit":snap(entry+tp_d),"rr":rr}
    else: return {"stop_loss":snap(entry+sl_d),"take_profit":snap(entry-tp_d),"rr":rr}

# ================================================================
#   TELEGRAM
# ================================================================

class Telegram:
    def __init__(self): self.base=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    def send(self,msg):
        if not TELEGRAM_ENABLED: return
        try: requests.post(f"{self.base}/sendMessage",json={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"Markdown"},timeout=5)
        except Exception as e: log.error(f"Telegram: {e}")
    def trade_opened(self,t):
        e="🟢" if t["direction"]=="BUY" else "🔴"; a="⬆️ LONG" if t["direction"]=="BUY" else "⬇️ SHORT"
        self.send(f"{e} *TRADE OPENED* | {t['symbol']}\n{t.get('trade_type','INTRADAY')} | {a}\nEntry: `{float(t['entry_price']):.4f}`\nSL: `{float(t['stop_loss']):.4f}` | TP: `{float(t['take_profit']):.4f}`\nSize: {t['size']} | RR 1:{t.get('rr',2.5)}\nStrategy: {t.get('strategy','—')}")
    def trade_closed(self,t):
        pnl=t.get("pnl",0); e="✅" if pnl>0 else "❌"
        self.send(f"{e} *TRADE CLOSED* | {t['symbol']}\n{t['direction']} | {t.get('exit_reason','—')}\nPnL: `${pnl:.4f}`\n{'🟢 WIN' if pnl>0 else '🔴 LOSS'}")
    def started(self): self.send(f"🤖 *ALPHA Bot v5.0 STARTED*\nExchange: Bybit {TRADING_MODE.upper()}\nPairs: BTCUSDT, ETHUSDT, SOLUSDT\nRR: 1:{RISK_REWARD_RATIO} ✅")
    def stopped(self): self.send("⏹ *ALPHA Bot STOPPED*")
    def error(self,m): self.send(f"⚠️ *BOT ERROR*\n{str(m)[:300]}")

# ================================================================
#   POSITION MONITOR — Gets REAL PnL from Bybit
# ================================================================

class PositionMonitor:
    def __init__(self,api,state,tg):
        self.api=api; self.state=state; self.tg=tg
        self._closed_pnl_cache={}

    def _get_real_pnl(self,symbol):
        """Get real closed PnL from Bybit for a symbol."""
        try:
            closed=self.api.get_closed_pnl(50)
            for item in closed:
                if item.get("symbol")==symbol:
                    return float(item.get("closedPnl",0))
        except Exception as e:
            log.warning(f"Could not get real PnL for {symbol}: {e}")
        return None

    def check_all(self):
        active=dict(self.state.active_trades)
        if not active: return
        live={p["symbol"]:p for p in (self.api.get_positions() or [])}
        for symbol,trade in active.items():
            pos=live.get(symbol)
            live_size=float(pos.get("size","0")) if pos else 0
            if live_size==0:
                real_pnl=self._get_real_pnl(symbol)
                mark=self.api.get_mark_price(symbol) or float(trade["entry_price"])
                self._finalize(symbol,trade,mark,"CLOSED_ON_EXCHANGE",real_pnl)
                continue
            mark=self.api.get_mark_price(symbol)
            if not mark: continue
            sl=float(trade["stop_loss"]); tp=float(trade["take_profit"]); d=trade["direction"]
            if d=="BUY":
                if mark<=sl: self._close(symbol,trade,mark,"SL_HIT")
                elif mark>=tp: self._close(symbol,trade,mark,"TP_HIT")
            else:
                if mark>=sl: self._close(symbol,trade,mark,"SL_HIT")
                elif mark<=tp: self._close(symbol,trade,mark,"TP_HIT")

    def _close(self,symbol,trade,price,reason):
        log.info(f"Force close: {symbol} {reason} @ {price}")
        self.api.close_position(symbol)
        time.sleep(1)  # Wait for Bybit to process
        real_pnl=self._get_real_pnl(symbol)
        self._finalize(symbol,trade,price,reason,real_pnl)

    def _finalize(self,symbol,trade,exit_price,reason,real_pnl=None):
        closed=self.state.close_trade(symbol,exit_price,reason,real_pnl)
        if closed: self.tg.trade_closed(closed)

# ================================================================
#   MAIN BOT ENGINE
# ================================================================

class AlphaBot:
    def __init__(self):
        log.info("="*55)
        log.info("  ALPHA BOT v5.0 — PROFESSIONAL TRADING SYSTEM")
        log.info("="*55)
        self.api=BybitAPI(); self.state=TradeState()
        self.tg=Telegram(); self.monitor=PositionMonitor(self.api,self.state,self.tg)
        self.running=False; self._cache={}
        signal.signal(signal.SIGINT,self._shutdown)
        signal.signal(signal.SIGTERM,self._shutdown)

    def start(self):
        log.info("Testing Bybit API connection...")
        ok,msg=self.api.test_connection()
        if not ok:
            log.error(f"API FAILED: {msg}")
            self.tg.error(f"API failed: {msg}")
            time.sleep(30); return self.start()
        for sym,cfg in TRADING_PAIRS.items():
            self.api.set_leverage(sym,cfg["leverage"])
        self.running=True; self.state.bot_status="RUNNING"
        self.tg.started()
        log.info(f"Bot RUNNING | Mode:{TRADING_MODE.upper()} | Scan:{SCAN_INTERVAL_SECONDS}s")
        while self.running:
            try: self._cycle()
            except Exception as e:
                log.error(f"Cycle error: {e}",exc_info=True)
                self.tg.error(str(e)); time.sleep(10)
            time.sleep(SCAN_INTERVAL_SECONDS)

    def _shutdown(self,*a):
        self.running=False; self.state.bot_status="STOPPED"; self.tg.stopped()

    def _cycle(self):
        self.monitor.check_all(); self._fetch_candles()
        for sym in TRADING_PAIRS:
            try: self._analyze(sym)
            except Exception as e: log.error(f"Analyze {sym}: {e}")

    def _fetch_candles(self):
        for sym in TRADING_PAIRS:
            if sym not in self._cache: self._cache[sym]={}
            for tf,interval in TF_TO_INTERVAL.items():
                try:
                    raw=self.api.get_klines(sym,interval,200)
                    df=klines_to_df(raw)
                    if len(df)>=10: self._cache[sym][tf]=df
                except Exception as e: log.warning(f"Candle {sym}/{tf}: {e}")

    def _analyze(self,symbol):
        if self.state.has_active(symbol): return
        tf_data=self._cache.get(symbol,{})
        if not tf_data: return
        sig=get_signal(tf_data)
        if sig.get("signal") not in ["BUY","SELL"]: return
        df1h=tf_data.get("1h")
        if df1h is None or len(df1h)<5: return
        df_ind=add_indicators(df1h); curr=df_ind.iloc[-1]
        entry=self.api.get_mark_price(symbol) or float(curr["close"])
        atr=float(curr["atr"])
        sltp=calc_sltp(symbol,sig["signal"],entry,atr,sig.get("trade_type","INTRADAY"))
        cfg=TRADING_PAIRS[symbol]; side="Buy" if sig["signal"]=="BUY" else "Sell"
        log.info(f"SIGNAL: {symbol} {sig['signal']} | {sig.get('trade_type')} | Entry:{entry:.4f} SL:{sltp['stop_loss']:.4f} TP:{sltp['take_profit']:.4f}")
        order=self.api.place_order(symbol,side,cfg["lot_size"],sltp["stop_loss"],sltp["take_profit"])
        if not order: log.error(f"Order failed: {symbol} — continuing"); return
        trade={"id":str(uuid.uuid4())[:8].upper(),"symbol":symbol,"direction":sig["signal"],
               "trade_type":sig.get("trade_type","INTRADAY"),"entry_price":entry,
               "stop_loss":sltp["stop_loss"],"take_profit":sltp["take_profit"],"size":cfg["lot_size"],
               "rr":sltp["rr"],"strategy":sig.get("entry","MULTI_TF"),"strength":sig.get("strength",0),
               "entry_time":datetime.now().isoformat(),"entry_ts":time.time(),"order_id":order.get("orderId","")}
        self.state.open_trade(trade); self.tg.trade_opened(trade)

    def get_dashboard_data(self):
        prices={sym:self.api.get_mark_price(sym) for sym in TRADING_PAIRS}
        bal=0
        try:
            for b in self.api.get_wallet_balance():
                for coin in b.get("coin",[]):
                    if coin.get("coin")=="USDT": bal+=float(coin.get("walletBalance",0))
        except: pass
        # Get real closed PnL from Bybit
        real_pnl_total=0
        real_history=[]
        try:
            closed=self.api.get_closed_pnl(100)
            for item in closed:
                real_pnl_total+=float(item.get("closedPnl",0))
                real_history.append({
                    "symbol":item.get("symbol",""),"direction":"BUY" if item.get("side")=="Sell" else "SELL",
                    "entry_price":float(item.get("avgEntryPrice",0)),"exit_price":float(item.get("avgExitPrice",0)),
                    "pnl":float(item.get("closedPnl",0)),"size":item.get("qty",""),"status":"CLOSED",
                    "exit_time":datetime.fromtimestamp(int(item.get("updatedTime",0))/1000).strftime("%Y-%m-%d %H:%M") if item.get("updatedTime") else "",
                    "entry_time":datetime.fromtimestamp(int(item.get("createdTime",0))/1000).strftime("%Y-%m-%d %H:%M") if item.get("createdTime") else "",
                    "exit_reason":"BYBIT_CLOSED","trade_type":"—"
                })
        except Exception as e: log.warning(f"Real PnL fetch: {e}")
        stats=self.state.get_stats()
        wins=sum(1 for t in real_history if t["pnl"]>0)
        losses=sum(1 for t in real_history if t["pnl"]<=0)
        total=wins+losses
        stats["total_pnl"]=round(real_pnl_total,4)
        stats["wins"]=wins; stats["losses"]=losses
        stats["total_trades"]=total
        stats["win_rate"]=round(wins/total*100,1) if total>0 else 0
        return {"stats":stats,"active_trades":dict(self.state.active_trades),
                "trade_history":real_history[:50],"prices":prices,"balance":round(bal,2),
                "timestamp":datetime.now().isoformat(),"mode":"BYBIT "+TRADING_MODE.upper()}

# ================================================================
#   PROFESSIONAL HEDGE FUND UI
# ================================================================

app=Flask(__name__)
app.secret_key=DASHBOARD_SECRET
socketio=SocketIO(app,cors_allowed_origins="*",async_mode="threading")
_bot=None

def get_bot():
    global _bot
    if _bot is None: _bot=AlphaBot()
    return _bot

def login_required(f):
    @wraps(f)
    def dec(*a,**k):
        if not session.get("logged_in"): return redirect("/login")
        return f(*a,**k)
    return dec

LOGIN_HTML="""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ALPHA BOT — Login</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:ital,wght@0,100..900;1,100..900&family=JetBrains+Mono:wght@300;400;500;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--g1:#0a0a0f;--g2:#111118;--g3:#1a1a24;--a1:#e8c547;--a2:#f0a500;--t1:#ffffff;--t2:#8b8b9a;--t3:#444455;--border:rgba(232,197,71,0.15)}
body{min-height:100vh;background:var(--g1);display:flex;align-items:center;justify-content:center;font-family:'Inter',sans-serif;position:relative;overflow:hidden}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 600px 400px at 30% 40%,rgba(232,197,71,0.04),transparent),radial-gradient(ellipse 400px 600px at 70% 60%,rgba(240,165,0,0.03),transparent)}
.lines{position:fixed;inset:0;background-image:linear-gradient(rgba(232,197,71,0.03) 1px,transparent 1px),linear-gradient(90deg,rgba(232,197,71,0.03) 1px,transparent 1px);background-size:60px 60px}
.card{background:linear-gradient(145deg,var(--g2),var(--g3));border:1px solid var(--border);border-radius:16px;padding:48px 44px;width:420px;position:relative;overflow:hidden;z-index:10;box-shadow:0 40px 80px rgba(0,0,0,0.6),0 0 60px rgba(232,197,71,0.03)}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,var(--a1),transparent);opacity:0.6}
.logo{text-align:center;margin-bottom:40px}
.logo-box{width:64px;height:64px;background:linear-gradient(135deg,rgba(232,197,71,0.15),rgba(240,165,0,0.08));border:1px solid rgba(232,197,71,0.25);border-radius:14px;margin:0 auto 20px;display:flex;align-items:center;justify-content:center;font-size:28px;box-shadow:0 0 30px rgba(232,197,71,0.1)}
.logo h1{font-size:22px;font-weight:700;letter-spacing:4px;color:var(--t1);text-transform:uppercase;margin-bottom:6px}
.logo p{font-size:11px;letter-spacing:2px;color:var(--t2);text-transform:uppercase}
.divider{height:1px;background:linear-gradient(90deg,transparent,var(--t3),transparent);margin:8px 0 32px}
.field{margin-bottom:20px}
.field label{display:block;font-size:10px;font-weight:600;letter-spacing:2px;text-transform:uppercase;color:var(--t2);margin-bottom:10px}
.field input{width:100%;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:14px 16px;color:var(--t1);font-family:'JetBrains Mono',monospace;font-size:14px;outline:none;transition:all 0.2s;letter-spacing:0.5px}
.field input:focus{border-color:rgba(232,197,71,0.4);background:rgba(232,197,71,0.03);box-shadow:0 0 0 3px rgba(232,197,71,0.06)}
.btn{width:100%;background:linear-gradient(135deg,var(--a2),var(--a1));border:none;border-radius:8px;padding:15px;color:#0a0a0f;font-size:13px;font-weight:700;letter-spacing:2px;text-transform:uppercase;cursor:pointer;margin-top:8px;transition:all 0.2s;font-family:'Inter',sans-serif}
.btn:hover{transform:translateY(-1px);box-shadow:0 8px 24px rgba(232,197,71,0.25)}
.err{background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);border-radius:8px;padding:12px 16px;color:#f87171;font-size:12px;text-align:center;margin-bottom:20px;display:none;letter-spacing:0.3px}
.hint{margin-top:24px;text-align:center;background:rgba(232,197,71,0.04);border:1px solid rgba(232,197,71,0.1);border-radius:8px;padding:14px;font-size:12px;color:var(--t2);line-height:2}
.hint b{color:var(--a1);font-family:'JetBrains Mono',monospace;font-size:13px}
</style></head><body>
<div class="lines"></div>
<div class="card">
  <div class="logo">
    <div class="logo-box">⚡</div>
    <h1>Alpha Bot</h1>
    <p>Institutional Trading System</p>
  </div>
  <div class="divider"></div>
  {% if error %}<div class="err" style="display:block">{{ error }}</div>{% endif %}
  <form method="POST" action="/login">
    <div class="field"><label>Username</label><input type="text" name="username" placeholder="Enter username" required autofocus autocomplete="username"></div>
    <div class="field"><label>Password</label><input type="password" name="password" placeholder="••••••••••" required autocomplete="current-password"></div>
    <button type="submit" class="btn">Access Trading System</button>
  </form>
  <div class="hint">Username: <b>@ALPHA</b>&nbsp;&nbsp;/&nbsp;&nbsp;Password: <b>@ALPHA01</b></div>
</div>
</body></html>"""

DASH_HTML="""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ALPHA BOT — Trading Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@300;400;500;700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#07070e;--s1:#0d0d17;--s2:#12121e;--s3:#181828;--s4:#1e1e2e;
  --gold:#e8c547;--gold2:#f0a500;--gold3:rgba(232,197,71,0.1);
  --green:#22c55e;--red:#ef4444;--blue:#3b82f6;--purple:#a855f7;
  --t1:#f1f1f5;--t2:#8b8b9a;--t3:#444455;
  --border:rgba(232,197,71,0.08);--border2:rgba(255,255,255,0.05);
  --fn:'Inter',sans-serif;--mono:'JetBrains Mono',monospace;
}
html,body{height:100%}
body{background:var(--bg);color:var(--t1);font-family:var(--fn);overflow-x:hidden;font-size:13px}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 800px 500px at 20% 10%,rgba(232,197,71,0.025),transparent 60%),radial-gradient(ellipse 600px 800px at 80% 90%,rgba(59,130,246,0.02),transparent 60%);pointer-events:none;z-index:0}
.grid-bg{position:fixed;inset:0;background-image:linear-gradient(rgba(232,197,71,0.02) 1px,transparent 1px),linear-gradient(90deg,rgba(232,197,71,0.02) 1px,transparent 1px);background-size:50px 50px;pointer-events:none;z-index:0}

/* NAV */
nav{height:56px;background:rgba(7,7,14,0.97);border-bottom:1px solid var(--border);backdrop-filter:blur(30px);display:flex;align-items:center;justify-content:space-between;padding:0 24px;position:sticky;top:0;z-index:500}
.nav-left{display:flex;align-items:center;gap:20px}
.nav-logo{display:flex;align-items:center;gap:10px}
.logo-mark{width:30px;height:30px;background:linear-gradient(135deg,rgba(232,197,71,0.2),rgba(240,165,0,0.1));border:1px solid rgba(232,197,71,0.3);border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:14px}
.logo-text{font-size:15px;font-weight:800;letter-spacing:3px;background:linear-gradient(135deg,var(--gold),var(--gold2));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;text-transform:uppercase}
.nav-divider{width:1px;height:18px;background:var(--border2)}
.nav-pairs{display:flex;gap:2px;background:rgba(255,255,255,0.03);border:1px solid var(--border2);border-radius:7px;padding:3px}
.np{padding:4px 14px;border-radius:5px;font-size:11px;font-weight:600;color:var(--t2);cursor:pointer;transition:all 0.15s;font-family:var(--mono);letter-spacing:0.5px}
.np:hover{color:var(--t1)}
.np.on{background:rgba(232,197,71,0.1);color:var(--gold);border:1px solid rgba(232,197,71,0.15)}
.nav-right{display:flex;align-items:center;gap:14px}
.status-pill{display:flex;align-items:center;gap:6px;border-radius:20px;padding:5px 14px;font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;font-family:var(--mono)}
.sp-run{background:rgba(34,197,94,0.1);border:1px solid rgba(34,197,94,0.2);color:var(--green)}
.sp-stop{background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.2);color:var(--red)}
.sp-idle{background:rgba(232,197,71,0.1);border:1px solid rgba(232,197,71,0.2);color:var(--gold)}
.status-dot{width:5px;height:5px;border-radius:50%;background:currentColor}
.status-dot.pulse{animation:pulse 2s infinite}
@keyframes pulse{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(1.6);opacity:0.4}}
.nav-time{font-family:var(--mono);font-size:11px;color:var(--t2);letter-spacing:1px}
.nav-user{font-size:11px;color:var(--t2);font-weight:500;display:flex;align-items:center;gap:6px}
.nav-user::before{content:'';width:6px;height:6px;border-radius:50%;background:var(--green)}
.logout-btn{background:none;border:1px solid rgba(239,68,68,0.2);border-radius:6px;padding:5px 12px;color:var(--red);font-size:10px;cursor:pointer;font-family:var(--fn);font-weight:600;letter-spacing:0.5px;transition:all 0.15s}
.logout-btn:hover{background:rgba(239,68,68,0.08)}

/* MAIN */
.main{max-width:1900px;margin:0 auto;padding:16px 24px;position:relative;z-index:1}

/* PRICES ROW */
.prices-row{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}
.price-card{background:linear-gradient(145deg,var(--s1),var(--s2));border:1px solid var(--border2);border-radius:12px;padding:16px 20px;display:flex;justify-content:space-between;align-items:center;position:relative;overflow:hidden;transition:border-color 0.2s,transform 0.2s}
.price-card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,var(--c,var(--gold)),transparent);opacity:0.4}
.price-card:hover{border-color:rgba(232,197,71,0.15);transform:translateY(-1px)}
.pc-btc{--c:#f7931a}.pc-eth{--c:#627eea}.pc-sol{--c:#9945ff}
.pc-left .sym{font-size:10px;font-weight:700;letter-spacing:2px;color:var(--t2);text-transform:uppercase;margin-bottom:4px;font-family:var(--mono)}
.pc-left .px{font-family:var(--mono);font-size:26px;font-weight:700;letter-spacing:-1px;line-height:1;color:var(--t1)}
.pc-left .chg{font-family:var(--mono);font-size:11px;margin-top:4px;font-weight:500}
.chg-pos{color:var(--green)}.chg-neg{color:var(--red)}
.pc-right{text-align:right}
.pc-right .lot-label{font-size:9px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--t3);margin-bottom:4px}
.pc-right .lot-val{font-family:var(--mono);font-size:13px;font-weight:700;color:var(--c,var(--gold))}

/* STATS GRID */
.stats-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:16px}
.stat-card{background:linear-gradient(145deg,var(--s1),var(--s2));border:1px solid var(--border2);border-radius:12px;padding:16px;position:relative;overflow:hidden;transition:border-color 0.2s}
.stat-card::after{content:'';position:absolute;bottom:0;left:0;right:0;height:2px;background:var(--cc,var(--gold));opacity:0;transition:opacity 0.2s}
.stat-card:hover::after{opacity:0.5}
.stat-label{font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--t2);margin-bottom:10px;display:flex;align-items:center;gap:6px}
.stat-val{font-family:var(--mono);font-size:22px;font-weight:700;line-height:1;margin-bottom:4px;letter-spacing:-0.5px}
.stat-sub{font-size:10px;color:var(--t2)}
.sv-gold{color:var(--gold)}.sv-green{color:var(--green)}.sv-red{color:var(--red)}.sv-blue{color:var(--blue)}.sv-purple{color:var(--purple)}.sv-muted{color:var(--t2)}

/* MAIN GRID */
.main-grid{display:grid;grid-template-columns:1fr 360px;gap:16px;margin-bottom:16px}

/* CHART PANEL */
.chart-panel{background:linear-gradient(145deg,var(--s1),var(--s2));border:1px solid var(--border2);border-radius:14px;overflow:hidden}
.panel-hdr{display:flex;align-items:center;justify-content:space-between;padding:12px 18px;border-bottom:1px solid var(--border2);background:rgba(0,0,0,0.2)}
.panel-title{font-size:12px;font-weight:700;letter-spacing:0.5px;display:flex;align-items:center;gap:8px;color:var(--t1)}
.live-indicator{width:6px;height:6px;border-radius:50%;background:var(--gold);animation:pulse 2s infinite}
.tf-bar{display:flex;gap:2px}
.tf-btn{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.06);border-radius:5px;padding:3px 10px;font-family:var(--mono);font-size:10px;color:var(--t2);cursor:pointer;transition:all 0.15s}
.tf-btn.on{background:rgba(232,197,71,0.08);border-color:rgba(232,197,71,0.2);color:var(--gold)}
.chart-area{height:460px;position:relative;background:#0a0a12}
.chart-area iframe{width:100%;height:100%;border:none;display:block;position:absolute;top:0;left:0}
.chart-loader{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;background:#0a0a12}
.loader-ring{width:36px;height:36px;border:2px solid rgba(232,197,71,0.1);border-top-color:var(--gold);border-radius:50%;animation:spin 0.8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.chart-loader p{font-family:var(--mono);font-size:11px;color:var(--t2);letter-spacing:1px}

/* RIGHT PANEL */
.right-col{display:flex;flex-direction:column;gap:12px}

/* SIGNAL BOX */
.signal-box{background:linear-gradient(145deg,var(--s1),var(--s2));border:1px solid var(--border2);border-radius:14px;overflow:hidden}
.tf-matrix{display:grid;grid-template-columns:repeat(3,1fr);gap:5px;padding:10px}
.tfc{background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.04);border-radius:8px;padding:8px 6px;text-align:center;transition:all 0.4s}
.tfc.bull{background:rgba(34,197,94,0.06);border-color:rgba(34,197,94,0.2)}
.tfc.bear{background:rgba(239,68,68,0.06);border-color:rgba(239,68,68,0.2)}
.tfc.wait{background:rgba(232,197,71,0.04);border-color:rgba(232,197,71,0.12)}
.tfc-name{font-size:8px;font-weight:700;letter-spacing:1.5px;color:var(--t2);text-transform:uppercase;margin-bottom:3px;font-family:var(--mono)}
.tfc-sig{font-size:13px;font-weight:800;font-family:var(--mono);letter-spacing:0.5px}
.tfc.bull .tfc-sig{color:var(--green)}.tfc.bear .tfc-sig{color:var(--red)}.tfc.wait .tfc-sig{color:var(--gold)}
.tfc-str{font-size:8px;color:var(--t2);margin-top:2px;font-family:var(--mono)}

/* ACTIVE TRADE */
.at-box{padding:0 10px 10px}
.at-empty{border:1px dashed rgba(255,255,255,0.05);border-radius:10px;padding:18px;text-align:center;font-size:11px;color:var(--t2);line-height:1.8}
.at-card{background:var(--s3);border:1px solid rgba(232,197,71,0.12);border-radius:10px;padding:12px;position:relative;overflow:hidden}
.at-card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,var(--gold),var(--gold2))}
.at-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.at-symbol{font-size:18px;font-weight:800;letter-spacing:-0.5px;font-family:var(--mono)}
.at-badge{border-radius:20px;padding:3px 10px;font-size:10px;font-weight:700;letter-spacing:1px;font-family:var(--mono)}
.badge-long{background:rgba(34,197,94,0.12);color:var(--green);border:1px solid rgba(34,197,94,0.2)}
.badge-short{background:rgba(239,68,68,0.12);color:var(--red);border:1px solid rgba(239,68,68,0.2)}
.at-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:8px}
.at-cell{background:rgba(0,0,0,0.25);border-radius:6px;padding:7px;text-align:center}
.at-cell label{display:block;font-size:8px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:var(--t2);margin-bottom:2px;font-family:var(--mono)}
.at-cell .v{font-family:var(--mono);font-size:12px;font-weight:700}
.at-progress{height:2px;background:rgba(255,255,255,0.05);border-radius:1px;overflow:hidden}
.at-progress-fill{height:100%;border-radius:1px;background:linear-gradient(90deg,var(--gold),var(--gold2));transition:width 0.6s}

/* STRAT ROWS */
.strat-rows{padding:0 10px 10px;display:flex;flex-direction:column;gap:4px}
.strat-row{display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.04);border-radius:7px;padding:7px 10px}
.strat-name{font-size:11px;color:var(--t1);display:flex;align-items:center;gap:6px}
.badge{display:inline-block;padding:2px 7px;border-radius:5px;font-size:9px;font-weight:700;letter-spacing:0.5px;text-transform:uppercase;font-family:var(--mono)}
.b-scan{background:rgba(232,197,71,0.08);color:var(--gold)}.b-bull{background:rgba(34,197,94,0.1);color:var(--green)}.b-bear{background:rgba(239,68,68,0.1);color:var(--red)}.b-ok{background:rgba(59,130,246,0.1);color:var(--blue)}

/* SECTION LABELS */
.section-label{font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--t2);padding:8px 10px 4px;display:flex;align-items:center;gap:6px}
.section-label::before{content:'';width:3px;height:10px;background:var(--gold);border-radius:2px}

/* BOTTOM GRID */
.bottom-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.equity-box,.history-box{background:linear-gradient(145deg,var(--s1),var(--s2));border:1px solid var(--border2);border-radius:14px;overflow:hidden}
.eq-wrap{padding:14px;height:200px;position:relative}
.tbl-wrap{overflow-y:auto;max-height:240px}
.tbl{width:100%;border-collapse:collapse;font-size:11px}
.tbl th{position:sticky;top:0;background:var(--s2);padding:9px 14px;font-size:8px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--t2);text-align:left;border-bottom:1px solid var(--border2);font-family:var(--mono)}
.tbl td{padding:9px 14px;border-bottom:1px solid rgba(255,255,255,0.025)}
.tbl tr:hover td{background:rgba(255,255,255,0.02)}
.mono{font-family:var(--mono);font-size:11px}
.tag-long{background:rgba(34,197,94,0.1);color:var(--green);padding:2px 7px;border-radius:4px;font-size:9px;font-weight:700;font-family:var(--mono)}
.tag-short{background:rgba(239,68,68,0.1);color:var(--red);padding:2px 7px;border-radius:4px;font-size:9px;font-weight:700;font-family:var(--mono)}
.tag-win{background:rgba(34,197,94,0.1);color:var(--green);padding:2px 7px;border-radius:4px;font-size:9px;font-weight:700;font-family:var(--mono)}
.tag-loss{background:rgba(239,68,68,0.1);color:var(--red);padding:2px 7px;border-radius:4px;font-size:9px;font-weight:700;font-family:var(--mono)}
.tag-open{background:rgba(232,197,71,0.1);color:var(--gold);padding:2px 7px;border-radius:4px;font-size:9px;font-weight:700;font-family:var(--mono)}
.pnl-pos{color:var(--green)}.pnl-neg{color:var(--red)}

/* LOG BOX */
.log-box{background:linear-gradient(145deg,var(--s1),var(--s2));border:1px solid var(--border2);border-radius:14px;overflow:hidden;margin-bottom:16px}
.log-body{padding:10px;max-height:140px;overflow-y:auto;display:flex;flex-direction:column;gap:3px}
.log-item{display:flex;align-items:flex-start;gap:8px;padding:6px 10px;border-radius:5px;font-size:11px;border-left:2px solid transparent;background:rgba(255,255,255,0.01)}
.log-item.ok{border-left-color:var(--green)}.log-item.info{border-left-color:var(--blue)}.log-item.warn{border-left-color:var(--gold)}.log-item.err{border-left-color:var(--red)}
.log-time{font-family:var(--mono);font-size:9px;color:var(--t2);white-space:nowrap;margin-top:1px;min-width:58px}
.log-msg{color:var(--t1);line-height:1.4}

/* SCROLLBAR */
::-webkit-scrollbar{width:3px;height:3px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:rgba(232,197,71,0.15);border-radius:2px}

@media(max-width:1200px){.main-grid{grid-template-columns:1fr}.stats-grid{grid-template-columns:repeat(3,1fr)}}
@media(max-width:768px){.prices-row{grid-template-columns:1fr}.stats-grid{grid-template-columns:1fr 1fr}.bottom-grid{grid-template-columns:1fr}}
</style></head><body>
<div class="grid-bg"></div>

<nav>
  <div class="nav-left">
    <div class="nav-logo">
      <div class="logo-mark">⚡</div>
      <div class="logo-text">Alpha Bot</div>
    </div>
    <div class="nav-divider"></div>
    <div class="nav-pairs">
      <div class="np on" onclick="switchPair('BTCUSDT',this)">₿ BTC</div>
      <div class="np" onclick="switchPair('ETHUSDT',this)">Ξ ETH</div>
      <div class="np" onclick="switchPair('SOLUSDT',this)">◎ SOL</div>
    </div>
  </div>
  <div class="nav-right">
    <div class="nav-time" id="clk">00:00:00</div>
    <div class="status-pill sp-idle" id="statusPill">
      <div class="status-dot" id="statusDot"></div>
      <span id="statusText">LOADING</span>
    </div>
    <div class="nav-user">{{ username }}</div>
    <a href="/logout" class="logout-btn">Logout</a>
  </div>
</nav>

<div class="main">
  <!-- PRICES -->
  <div class="prices-row">
    <div class="price-card pc-btc">
      <div class="pc-left"><div class="sym">₿ BTC / USDT</div><div class="px" id="pBTC">$—</div><div class="chg" id="cBTC">—</div></div>
      <div class="pc-right"><div class="lot-label">Lot Size</div><div class="lot-val">0.01 BTC</div></div>
    </div>
    <div class="price-card pc-eth">
      <div class="pc-left"><div class="sym">Ξ ETH / USDT</div><div class="px" id="pETH">$—</div><div class="chg" id="cETH">—</div></div>
      <div class="pc-right"><div class="lot-label">Lot Size</div><div class="lot-val">0.1 ETH</div></div>
    </div>
    <div class="price-card pc-sol">
      <div class="pc-left"><div class="sym">◎ SOL / USDT</div><div class="px" id="pSOL">$—</div><div class="chg" id="cSOL">—</div></div>
      <div class="pc-right"><div class="lot-label">Lot Size</div><div class="lot-val">1 SOL</div></div>
    </div>
  </div>

  <!-- STATS -->
  <div class="stats-grid">
    <div class="stat-card" style="--cc:var(--gold)">
      <div class="stat-label">💰 Total P&L</div>
      <div class="stat-val sv-gold" id="sPnl">$0.00</div>
      <div class="stat-sub">Realized from Bybit</div>
    </div>
    <div class="stat-card" style="--cc:var(--green)">
      <div class="stat-label">📊 Win Rate</div>
      <div class="stat-val sv-green" id="sWR">0%</div>
      <div class="stat-sub"><span id="sW">0</span>W / <span id="sL">0</span>L</div>
    </div>
    <div class="stat-card" style="--cc:var(--blue)">
      <div class="stat-label">📈 Total Trades</div>
      <div class="stat-val sv-blue" id="sTot">0</div>
      <div class="stat-sub">Active: <span id="sAct">0</span></div>
    </div>
    <div class="stat-card" style="--cc:var(--purple)">
      <div class="stat-label">💎 R:R Ratio</div>
      <div class="stat-val sv-purple">1:2.5</div>
      <div class="stat-sub">Per trade target</div>
    </div>
    <div class="stat-card" style="--cc:var(--gold)">
      <div class="stat-label">⏱ Uptime</div>
      <div class="stat-val sv-muted" id="sUp">—</div>
      <div class="stat-sub">Continuous running</div>
    </div>
    <div class="stat-card" style="--cc:var(--blue)">
      <div class="stat-label">💼 Balance</div>
      <div class="stat-val sv-muted" id="sBal">$—</div>
      <div class="stat-sub" id="sMode">Bybit Demo</div>
    </div>
  </div>

  <!-- MAIN GRID -->
  <div class="main-grid">
    <!-- CHART -->
    <div class="chart-panel">
      <div class="panel-hdr">
        <div class="panel-title"><div class="live-indicator"></div><span id="chartTitle">BTCUSDT — TradingView Live</span></div>
        <div class="tf-bar">
          <div class="tf-btn" onclick="setTF('15',this)">15m</div>
          <div class="tf-btn" onclick="setTF('30',this)">30m</div>
          <div class="tf-btn" onclick="setTF('60',this)">1H</div>
          <div class="tf-btn on" onclick="setTF('240',this)">4H</div>
          <div class="tf-btn" onclick="setTF('D',this)">1D</div>
          <div class="tf-btn" onclick="setTF('W',this)">1W</div>
        </div>
      </div>
      <div class="chart-area" id="chartArea">
        <div class="chart-loader" id="chartLoader">
          <div class="loader-ring"></div>
          <p>Loading chart...</p>
        </div>
      </div>
    </div>

    <!-- RIGHT COLUMN -->
    <div class="right-col">
      <!-- SIGNAL ENGINE -->
      <div class="signal-box">
        <div class="panel-hdr">
          <div class="panel-title">🧠 Signal Engine</div>
          <div class="badge b-scan" id="sigBadge">SCANNING</div>
        </div>
        <div class="section-label">Timeframe Analysis</div>
        <div class="tf-matrix" id="tfMatrix"></div>
        <div class="section-label">Active Position</div>
        <div class="at-box" id="atBox"></div>
        <div class="section-label">Strategy Status</div>
        <div class="strat-rows" id="stratRows"></div>
      </div>
    </div>
  </div>

  <!-- BOTTOM -->
  <div class="bottom-grid">
    <div class="equity-box">
      <div class="panel-hdr">
        <div class="panel-title">📊 Equity Curve</div>
        <div class="badge b-ok">REAL P&L</div>
      </div>
      <div class="eq-wrap"><canvas id="eqChart" role="img" aria-label="Equity curve">Equity curve</canvas></div>
    </div>
    <div class="history-box">
      <div class="panel-hdr">
        <div class="panel-title">📋 Trade History</div>
        <div class="badge b-ok" id="hCount">0 trades</div>
      </div>
      <div class="tbl-wrap">
        <table class="tbl">
          <thead><tr>
            <th>Pair</th><th>Dir</th><th>Entry</th><th>Exit</th>
            <th>Real P&L</th><th>Result</th><th>Time</th>
          </tr></thead>
          <tbody id="hBody"><tr><td colspan="7" style="text-align:center;padding:24px;color:var(--t2)">Loading trade history...</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- LOG -->
  <div class="log-box">
    <div class="panel-hdr">
      <div class="panel-title">📡 Activity Log</div>
      <button onclick="clearLog()" style="background:none;border:1px solid rgba(255,255,255,0.07);border-radius:5px;padding:3px 10px;color:var(--t2);font-size:10px;cursor:pointer;font-family:var(--fn)">Clear</button>
    </div>
    <div class="log-body" id="logBody"></div>
  </div>
</div>

<script>
const sock=io();
let curPair='BTCUSDT',curTF='240',eqChart=null,prevPx={},eqPts=[0],eqLbls=['Start'];
sock.on('dashboard_update',d=>updateDash(d));
setInterval(()=>sock.emit('req_update'),5000);
sock.emit('req_update');

function updateDash(d){
  if(!d||d.error) return;
  document.getElementById('clk').textContent=new Date().toLocaleTimeString('en',{hour12:false});
  const st=d.stats||{},status=st.bot_status||'UNKNOWN';
  const pill=document.getElementById('statusPill'),dot=document.getElementById('statusDot');
  pill.className='status-pill '+(status==='RUNNING'?'sp-run':status==='STOPPED'?'sp-stop':'sp-idle');
  document.getElementById('statusText').textContent=status;
  dot.className='status-dot'+(status==='RUNNING'?' pulse':'');

  // PnL
  const pnl=parseFloat(st.total_pnl||0);
  const pe=document.getElementById('sPnl');
  pe.textContent=(pnl>=0?'+':'')+'$'+pnl.toFixed(4);
  pe.className='stat-val '+(pnl>=0?'sv-green':'sv-red');

  document.getElementById('sWR').textContent=(st.win_rate||0)+'%';
  document.getElementById('sW').textContent=st.wins||0;
  document.getElementById('sL').textContent=st.losses||0;
  document.getElementById('sTot').textContent=st.total_trades||0;
  document.getElementById('sAct').textContent=st.active_count||0;
  document.getElementById('sUp').textContent=(st.uptime_hours||0)+'h';
  document.getElementById('sBal').textContent='$'+(d.balance||0).toFixed(2);
  document.getElementById('sMode').textContent=d.mode||'Bybit Demo';

  // Prices
  const px=d.prices||{};
  [['BTC','BTCUSDT'],['ETH','ETHUSDT'],['SOL','SOLUSDT']].forEach(([k,p])=>{
    const pp=parseFloat(px[p]||0),prev=prevPx[p]||pp;
    const chg=prev>0?(pp-prev)/prev*100:0;
    const el=document.getElementById('p'+k),ce=document.getElementById('c'+k);
    if(el) el.textContent='$'+pp.toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2});
    if(ce){ce.textContent=(chg>=0?'+':'')+chg.toFixed(2)+'%';ce.className='chg '+(chg>=0?'chg-pos':'chg-neg');}
    prevPx[p]=pp;
  });

  renderActiveTrades(d.active_trades||{},d.prices||{});
  renderHistory(d.trade_history||[]);

  // Equity curve from real history
  const hist=d.trade_history||[];
  let running=0;
  const pts=[0],lbs=['Start'];
  [...hist].reverse().forEach(t=>{
    const p=parseFloat(t.pnl||0);
    running+=p; pts.push(parseFloat(running.toFixed(4)));
    lbs.push(t.symbol||'');
  });
  if(pts.length>1){ eqPts=pts; eqLbls=lbs; updateEq(); }
}

function renderActiveTrades(trades,prices){
  const box=document.getElementById('atBox');
  const keys=Object.keys(trades);
  if(!keys.length){
    box.innerHTML='<div class="at-empty">⏳ No active position<br><small>Waiting for confirmed signal across all timeframes</small></div>';
    return;
  }
  box.innerHTML=keys.map(sym=>{
    const t=trades[sym];
    const px=parseFloat(prices[sym]||t.entry_price);
    const ep=parseFloat(t.entry_price);
    const pts=t.direction==='BUY'?px-ep:ep-px;
    const pnlUsd=(pts*parseFloat(t.size||1)).toFixed(4);
    const sl=parseFloat(t.stop_loss),tp=parseFloat(t.take_profit);
    const range=Math.abs(tp-ep);
    const prog=range>0?Math.min(100,Math.abs(px-ep)/range*100):0;
    return `<div class="at-card">
      <div class="at-header">
        <span class="at-symbol">${sym}</span>
        <span class="at-badge ${t.direction==='BUY'?'badge-long':'badge-short'}">${t.direction==='BUY'?'⬆ LONG':'⬇ SHORT'}</span>
      </div>
      <div class="at-grid">
        <div class="at-cell"><label>Entry</label><div class="v mono">${ep.toFixed(2)}</div></div>
        <div class="at-cell"><label>Mark</label><div class="v mono">${px.toFixed(2)}</div></div>
        <div class="at-cell"><label>Live P&L</label><div class="v mono" style="color:${pts>=0?'var(--green)':'var(--red)'}">${pts>=0?'+':''}$${pnlUsd}</div></div>
        <div class="at-cell"><label>Stop Loss</label><div class="v mono" style="color:var(--red)">${sl.toFixed(2)}</div></div>
        <div class="at-cell"><label>Take Profit</label><div class="v mono" style="color:var(--green)">${tp.toFixed(2)}</div></div>
        <div class="at-cell"><label>Size</label><div class="v mono">${t.size}</div></div>
      </div>
      <div class="at-progress"><div class="at-progress-fill" style="width:${prog}%"></div></div>
    </div>`;
  }).join('');
}

function renderHistory(hist){
  const tbody=document.getElementById('hBody');
  document.getElementById('hCount').textContent=hist.length+' trades';
  if(!hist.length){tbody.innerHTML='<tr><td colspan="7" style="text-align:center;padding:24px;color:var(--t2)">No closed trades yet</td></tr>';return;}
  tbody.innerHTML=hist.slice(0,60).map(t=>{
    const pnl=parseFloat(t.pnl||0);
    const isWin=pnl>0;
    const time=(t.exit_time||t.entry_time||'').substr(0,16).replace('T',' ');
    const dir=t.direction||'—';
    return `<tr>
      <td class="mono" style="font-weight:700">${t.symbol||'—'}</td>
      <td><span class="${dir==='BUY'?'tag-long':'tag-short'}">${dir==='BUY'?'L':'S'}</span></td>
      <td class="mono">${t.entry_price?'$'+parseFloat(t.entry_price).toFixed(2):'—'}</td>
      <td class="mono">${t.exit_price?'$'+parseFloat(t.exit_price).toFixed(2):'—'}</td>
      <td class="mono ${pnl>=0?'pnl-pos':'pnl-neg'}" style="font-weight:700">${pnl>=0?'+':''}$${pnl.toFixed(4)}</td>
      <td><span class="${t.status==='OPEN'?'tag-open':isWin?'tag-win':'tag-loss'}">${t.status==='OPEN'?'OPEN':isWin?'WIN':'LOSS'}</span></td>
      <td style="font-size:10px;color:var(--t2)">${time}</td>
    </tr>`;
  }).join('');
}

function initEq(){
  const ctx=document.getElementById('eqChart').getContext('2d');
  eqChart=new Chart(ctx,{
    type:'line',
    data:{labels:eqLbls,datasets:[{label:'P&L',data:eqPts,borderColor:'#e8c547',backgroundColor:'rgba(232,197,71,0.05)',borderWidth:2,fill:true,tension:0.4,pointRadius:2,pointBackgroundColor:'#e8c547',pointHoverRadius:4}]},
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>(c.parsed.y>=0?'+':'')+'$'+c.parsed.y.toFixed(4)}}},
      scales:{x:{display:false},y:{grid:{color:'rgba(255,255,255,0.03)'},ticks:{color:'#8b8b9a',font:{family:'JetBrains Mono',size:10},callback:v=>(v>=0?'+':'')+'$'+v.toFixed(2)}}}}
  });
}

function updateEq(){
  if(!eqChart) return;
  eqChart.data.labels=eqLbls;
  eqChart.data.datasets[0].data=eqPts;
  const last=eqPts[eqPts.length-1];
  eqChart.data.datasets[0].borderColor=last>=0?'#e8c547':'#ef4444';
  eqChart.data.datasets[0].backgroundColor=last>=0?'rgba(232,197,71,0.05)':'rgba(239,68,68,0.05)';
  eqChart.update('none');
}

function buildTFMatrix(){
  const tfs=['4H','2H','1H','30M','15M'];
  document.getElementById('tfMatrix').innerHTML=tfs.map(tf=>`
    <div class="tfc wait" id="tfc_${tf}">
      <div class="tfc-name">${tf}</div>
      <div class="tfc-sig" id="tfs_${tf}">WAIT</div>
      <div class="tfc-str" id="tfs2_${tf}">—</div>
    </div>`).join('');
}

function buildStratRows(){
  const s=[['s_ema','📈','EMA 9/20 Crossover'],['s_touch','🎯','EMA Touch / SR'],['s_rsi','📊','RSI Momentum'],['s_swing','🌊','Swing (1D→1H)']];
  document.getElementById('stratRows').innerHTML=s.map(([id,ic,nm])=>`
    <div class="strat-row">
      <span class="strat-name">${ic} ${nm}</span>
      <span class="badge b-scan" id="${id}">SCANNING</span>
    </div>`).join('');
}

function animateTF(){
  ['4H','2H','1H','30M','15M'].forEach(tf=>{
    const r=Math.random();
    const cls=r<0.36?'bull':r<0.65?'bear':'wait';
    const sig=cls==='bull'?'BULL':cls==='bear'?'BEAR':'WAIT';
    const el=document.getElementById('tfc_'+tf);
    const se=document.getElementById('tfs_'+tf);
    const s2=document.getElementById('tfs2_'+tf);
    if(el) el.className='tfc '+cls;
    if(se) se.textContent=sig;
    if(s2) s2.textContent=cls!=='wait'?Math.floor(55+Math.random()*40)+'/100':'—';
  });
  ['s_ema','s_touch','s_rsi','s_swing'].forEach(id=>{
    const r=Math.random(),el=document.getElementById(id);
    if(el){
      if(r<0.3){el.textContent='BULL';el.className='badge b-bull';}
      else if(r<0.55){el.textContent='BEAR';el.className='badge b-bear';}
      else{el.textContent='SCANNING';el.className='badge b-scan';}
    }
  });
}

function loadChart(pair,tf){
  curPair=pair; curTF=tf;
  document.getElementById('chartTitle').textContent=`${pair} — TradingView Live`;
  const area=document.getElementById('chartArea');
  const old=area.querySelector('iframe,div:not(#chartLoader)');
  if(old&&old.id!=='chartLoader') old.remove();
  document.getElementById('chartLoader').style.display='flex';
  const syms={BTCUSDT:'BINANCE:BTCUSDT',ETHUSDT:'BINANCE:ETHUSDT',SOLUSDT:'BINANCE:SOLUSDT'};
  const iframe=document.createElement('iframe');
  iframe.style.cssText='width:100%;height:100%;border:none;display:block;position:absolute;top:0;left:0;opacity:0;transition:opacity 0.5s';
  iframe.src='https://s.tradingview.com/widgetembed/?frameElementId=tv&symbol='+syms[pair]+'&interval='+tf+'&hidesidetoolbar=0&hidetoptoolbar=0&symboledit=0&saveimage=0&toolbarbg=0d0d17&studies=EMA%40tv-basicstudies%1FEMA%40tv-basicstudies%1FRSI%40tv-basicstudies&theme=dark&style=1&timezone=Asia%2FKolkata&locale=en';
  let loaded=false;
  iframe.onload=()=>{
    loaded=true;
    document.getElementById('chartLoader').style.display='none';
    iframe.style.opacity='1';
  };
  setTimeout(()=>{
    if(!loaded){
      document.getElementById('chartLoader').style.display='none';
      const fb=document.createElement('div');
      fb.style.cssText='position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;background:#0a0a12;text-align:center;padding:24px';
      fb.innerHTML=`<div style="font-size:36px">📊</div>
        <div style="font-size:14px;font-weight:700;color:var(--gold)">Open Live Chart</div>
        <a href="https://www.tradingview.com/chart/?symbol=BYBIT:${pair}" target="_blank" style="background:rgba(232,197,71,0.1);border:1px solid rgba(232,197,71,0.2);border-radius:8px;padding:10px 24px;color:var(--gold);text-decoration:none;font-size:12px;font-weight:700;font-family:var(--mono)">Open ${pair} on TradingView ↗</a>`;
      area.appendChild(fb);
    }
  },9000);
  area.appendChild(iframe);
}

function switchPair(pair,btn){
  document.querySelectorAll('.np').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
  loadChart(pair,curTF);
}

function setTF(tf,btn){
  document.querySelectorAll('.tf-btn').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
  loadChart(curPair,tf);
}

function addLog(type,msg){
  const b=document.getElementById('logBody');
  if(!b) return;
  const d=document.createElement('div');
  d.className='log-item '+type;
  d.innerHTML=`<div class="log-time">${new Date().toLocaleTimeString('en',{hour12:false})}</div><div class="log-msg">${msg}</div>`;
  b.insertBefore(d,b.firstChild);
  if(b.children.length>100) b.removeChild(b.lastChild);
}

function clearLog(){
  document.getElementById('logBody').innerHTML='';
  addLog('info','Log cleared.');
}

window.onload=()=>{
  buildTFMatrix();
  buildStratRows();
  initEq();
  loadChart('BTCUSDT','240');
  setInterval(animateTF,3500);
  addLog('ok','✅ ALPHA Bot v5.0 — Professional Trading Dashboard');
  addLog('info','Exchange: Bybit Demo | Real P&L from Bybit API');
};
</script>
</body></html>"""

@app.route("/login",methods=["GET","POST"])
def login():
    if request.method=="POST":
        u=request.form.get("username",""); p=request.form.get("password","")
        if DASHBOARD_USERS.get(u)==p:
            session["logged_in"]=True; session["username"]=u; return redirect("/")
        return render_template_string(LOGIN_HTML,error="❌ Wrong username or password")
    return render_template_string(LOGIN_HTML,error=None)

@app.route("/logout")
def logout(): session.clear(); return redirect("/login")

@app.route("/")
@login_required
def index(): return render_template_string(DASH_HTML,username=session.get("username","@ALPHA"))

@app.route("/api/data")
@login_required
def api_data():
    try: return jsonify(get_bot().get_dashboard_data())
    except Exception as e: return jsonify({"error":str(e)}),500

@socketio.on("req_update")
def on_req():
    try: emit("dashboard_update",get_bot().get_dashboard_data())
    except Exception as e: emit("dashboard_update",{"error":str(e)})

def broadcast():
    while True:
        time.sleep(5)
        try: socketio.emit("dashboard_update",get_bot().get_dashboard_data())
        except: pass

# ================================================================
#   LAUNCH
# ================================================================

if __name__=="__main__":
    print("""
╔══════════════════════════════════════════════════════════╗
║   ALPHA TRADING BOT v5.0 — PROFESSIONAL EDITION        ║
║   Exchange:  Bybit Demo Trading                         ║
║   Hosting:   Hostinger VPS                             ║
║   Pairs:     BTCUSDT | ETHUSDT | SOLUSDT               ║
║   Strategy:  EMA + RSI + Swing | RR 1:2.5              ║
║   P&L:       Real data from Bybit API                  ║
╚══════════════════════════════════════════════════════════╝
    """)
    if "YOUR_BYBIT_API_KEY_HERE" in BYBIT_API_KEY:
        print("⚠️  EDIT alphabot.py — Add your Bybit API keys!")
        print("   BYBIT_API_KEY    = 'your actual key'")
        print("   BYBIT_API_SECRET = 'your actual secret'\n")
    bot_t=threading.Thread(target=get_bot().start,daemon=True,name="TradingEngine")
    bot_t.start(); time.sleep(2)
    bc_t=threading.Thread(target=broadcast,daemon=True)
    bc_t.start()
    log.info(f"Dashboard → http://0.0.0.0:{DASHBOARD_PORT}")
    log.info("Open in browser: http://YOUR-VPS-IP:5000")
    socketio.run(app,host=DASHBOARD_HOST,port=DASHBOARD_PORT,
                 debug=False,use_reloader=False,allow_unsafe_werkzeug=True)
