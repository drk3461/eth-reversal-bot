# eth_reversal_testnet_bot.py
# PAPER/TRAINING USE ONLY — connects to Binance SPOT TESTNET (no real money).
# IMPORTANT: Read and understand every line before running.

import os, time, math
import numpy as np, pandas as pd
from datetime import datetime, timezone
import ccxt
from dotenv import load_dotenv

# ---------------- CONFIG ----------------
SYMBOL = "ETH/USDT"
TIMEFRAME = "1m"
LOOKBACK = 7
TP_PCT = 0.006      # 0.6% take profit
SL_PCT = 0.004      # 0.4% stop loss
TRAIL_DROP = 0.003  # 0.3% trailing drop
SPEND_USDT = 12.0   # amount of USDT to spend per entry
FEE_RATE = 0.001    # assumed maker/taker fee (used for ledger calc)
POLL_SEC = 15
SANDBOX = True      # TESTNET mode ON (do NOT set False unless you know what you're doing)
LEDGER_CSV = "testnet_ledger.csv"
LOG_ORDERS = "testnet_orders.csv"
# ----------------------------------------

load_dotenv()

def nowu(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def ema(series, n): return series.ewm(span=n, adjust=False).mean()

def pivots(df, look=LOOKBACK):
    n = len(df)
    lows = np.zeros(n, dtype=bool)
    highs = np.zeros(n, dtype=bool)
    # strict pivot detection (center point min/max in window)
    for i in range(look, n - look):
        window_low = df['low'].iloc[i - look:i + look + 1]
        window_high = df['high'].iloc[i - look:i + look + 1]
        if df['low'].iloc[i] == window_low.min():
            lows[i] = True
        if df['high'].iloc[i] == window_high.max():
            highs[i] = True
    return lows, highs

def make_exchange():
    key = os.getenv("BINANCE_KEY", "")
    secret = os.getenv("BINANCE_SECRET", "")
    ex = ccxt.binance({
        "apiKey": key,
        "secret": secret,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    ex.set_sandbox_mode(SANDBOX)
    ex.load_markets()
    return ex

def fetch_ohlcv_df(ex, limit=300):
    ohlcv = ex.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","vol"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df

def market_info(ex):
    m = ex.market(SYMBOL)
    step = m["limits"]["amount"]["min"] or 0.000001
    min_notional = (m["limits"].get("cost") or {}).get("min", 10.0)
    return step, min_notional, m["precision"]["amount"], m["precision"]["price"]

def floor_to_step(q, step):
    if step <= 0: return q
    return math.floor(q/step) * step

def ensure_files():
    for f in [LEDGER_CSV, LOG_ORDERS]:
        if not os.path.exists(f):
            pd.DataFrame(columns=["time","side","qty","price","fees","realized_pnl","net"]).to_csv(f, index=False)

def append_ledger(side, qty, price, fees, realized):
    df = pd.read_csv(LEDGER_CSV)
    net = (df["realized_pnl"].sum() if len(df) else 0.0) + realized
    df = pd.concat([df, pd.DataFrame([{
        "time": nowu(), "side": side, "qty": qty, "price": price,
        "fees": fees, "realized_pnl": realized, "net": net
    }])], ignore_index=True)
    df.to_csv(LEDGER_CSV, index=False)
    print(f"[{nowu()}] LEDGER {side.upper()} qty={qty:.8f} px={price:.2f} fees={fees:.6f} pnl={realized:+.6f} NET={net:+.6f}")

def log_order(order):
    df = pd.read_csv(LOG_ORDERS)
    row = {
        "time": nowu(),
        "side": order.get("side"),
        "qty": order.get("amount"),
        "price": order.get("average") or order.get("price") or 0.0,
        "fees": sum([f.get("cost",0) for f in (order.get("fees") or [])]),
        "realized_pnl": 0.0,
        "net": 0.0
    }
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(LOG_ORDERS, index=False)

def create_market_buy(ex, step, spend_usdt, min_notional):
    price = float(ex.fetch_ticker(SYMBOL)["last"])
    raw_amount = spend_usdt / price
    amount = floor_to_step(raw_amount, step)
    if amount <= 0:
        raise ValueError("Order amount 0 after rounding to step.")
    order = ex.create_order(symbol=SYMBOL, type="market", side="buy", amount=amount)
    return order, amount, float(order.get("average") or price)

def create_market_sell(ex, amount):
    order = ex.create_order(symbol=SYMBOL, type="market", side="sell", amount=amount)
    return order, float(order.get("average") or 0.0)

def run_testnet_bot():
    ex = make_exchange()
    step, min_notional, amt_prec, price_prec = market_info(ex)
    ensure_files()
    print(f"[{nowu()}] START (SANDBOX={SANDBOX}) SYMBOL={SYMBOL} minNotional≈{min_notional}")

    in_pos = False
    entry_px = 0.0
    entry_amt = 0.0
    entry_time = None

    while True:
        try:
            df = fetch_ohlcv_df(ex, limit=500)
            lows, highs = pivots(df, LOOKBACK)
            i = len(df)-1
            k = i-1   # use previous closed candle for confirmation
            last_close = float(df["close"].iloc[k])

            if not in_pos:
                # entry: pivot low confirmed then price closes above that pivot bar high
                if lows[k] and float(df["close"].iloc[i]) > float(df["high"].iloc[k]):
                    spend = max(SPEND_USDT, min_notional)
                    free_usdt = float(ex.fetch_balance()["free"].get("USDT", 0.0))
                    if free_usdt < spend:
                        print(f"[{nowu()}] TESTNET: not enough USDT free ({free_usdt:.2f}) — skipping buy")
                    else:
                        print(f"[{nowu()}] SIGNAL: pivot low bounce detected. Attempting BUY spend={spend:.2f} USDT")
                        order, amount, fill_px = create_market_buy(ex, step, spend, min_notional)
                        log_order(order)
                        in_pos = True
                        entry_px = fill_px
                        entry_amt = amount
                        entry_time = datetime.now(timezone.utc)
                        append_ledger("buy", amount, fill_px, 0.0, 0.0)
                else:
                    # idle
                    pass
            else:
                cur_px = float(ex.fetch_ticker(SYMBOL)["last"])
                # update peak
                # compute exit levels
                tp = entry_px * (1 + TP_PCT)
                sl = entry_px * (1 - SL_PCT)
                # check exit conditions
                held_minutes = (datetime.now(timezone.utc) - entry_time).total_seconds()/60.0
                if cur_px >= tp or cur_px <= sl or held_minutes >= 120:
                    print(f"[{nowu()}] EXIT condition met. SELLing amount={entry_amt:.8f} cur={cur_px:.2f}")
                    sell_order, sell_px = create_market_sell(ex, entry_amt)
                    log_order(sell_order)
                    entry_notional = entry_px * entry_amt
                    exit_notional = sell_px * entry_amt
                    fees = entry_notional*FEE_RATE + exit_notional*FEE_RATE
                    realized = (exit_notional - entry_notional) - fees
                    append_ledger("sell", entry_amt, sell_px, fees, realized)
                    in_pos = False
                    entry_px = entry_amt = None
                    entry_time = None

            time.sleep(POLL_SEC)

        except ccxt.BaseError as e:
            print(f"[{nowu()}] EXCHANGE ERROR: {type(e).__name__}: {e}")
            time.sleep(3)
        except Exception as e:
            print(f"[{nowu()}] ERROR: {type(e).__name__}: {e}")
            time.sleep(3)

if __name__ == "__main__":
    run_testnet_bot()
