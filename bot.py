#!/usr/bin/env python3
"""
Paper-trading crypto bot (NO real money, NO API keys, NO paid services).

- Starts with fake ₦100,000.
- Uses free public price data (Binance public endpoint, CoinGecko fallback).
- Strategy: trend-following (fast/slow moving average) with RSI filter.
- Strict risk rules: max % of wallet per trade, stop-loss, take-profit,
  daily loss halt, max open positions, trading fees included.

Usage:
    python bot.py live        # run once: fetch live prices, trade on paper (schedule every hour)
    python bot.py backtest    # test on simulated prices (works offline)
    python bot.py report      # rebuild dashboard.html from saved state

Only uses the Python standard library.
"""
import json, os, sys, time, random, math, urllib.request, urllib.error
from datetime import datetime, timezone

# ---------------- SETTINGS (edit these) ----------------
START_NGN = 100_000          # fake starting money in naira
NGN_PER_USDT = 1500.0        # fallback rate; bot tries to fetch a live one
COINS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]
CANDLE_INTERVAL = "1h"
FAST, SLOW, RSI_LEN = 9, 21, 14
MAX_PER_TRADE_PCT = 0.20     # never put more than 20% of wallet in one coin
MAX_OPEN_POSITIONS = 3
STOP_LOSS_PCT = 0.04         # sell if down 4% from entry
TAKE_PROFIT_PCT = 0.08       # sell if up 8% from entry
DAILY_LOSS_HALT_PCT = 0.05   # stop buying for the day if down 5% today
KILL_SWITCH_PCT = 0.15       # stop everything if total down 15% from start
FEE_PCT = 0.001              # 0.1% per trade (typical exchange fee)
STATE_FILE = "state.json"
DASHBOARD_FILE = "dashboard.html"
# -------------------------------------------------------


def http_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "paper-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_closes(symbol, limit=100):
    """Free public data. Binance first, CoinGecko fallback."""
    try:
        d = http_json(f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={CANDLE_INTERVAL}&limit={limit}")
        return [float(c[4]) for c in d]
    except Exception:
        pass
    cg = {"BTCUSDT": "bitcoin", "ETHUSDT": "ethereum", "BNBUSDT": "binancecoin",
          "SOLUSDT": "solana", "XRPUSDT": "ripple"}.get(symbol)
    if cg:
        try:
            d = http_json(f"https://api.coingecko.com/api/v3/coins/{cg}/market_chart?vs_currency=usd&days=5")
            return [p[1] for p in d["prices"]][-limit:]
        except Exception:
            pass
    return None


def fetch_ngn_rate():
    try:
        d = http_json("https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=ngn")
        return float(d["tether"]["ngn"])
    except Exception:
        return NGN_PER_USDT


def sma(v, n):
    return sum(v[-n:]) / n


def rsi(v, n=14):
    if len(v) < n + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(-n, 0):
        ch = v[i] - v[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def signal(closes):
    """Returns 'buy', 'sell' or 'hold'."""
    if len(closes) < SLOW + 2:
        return "hold"
    f_now, s_now = sma(closes, FAST), sma(closes, SLOW)
    f_prev, s_prev = sma(closes[:-1], FAST), sma(closes[:-1], SLOW)
    r = rsi(closes, RSI_LEN)
    if f_prev <= s_prev and f_now > s_now and r < 70:
        return "buy"
    if f_prev >= s_prev and f_now < s_now:
        return "sell"
    return "hold"


def new_state(rate):
    usdt = START_NGN / rate
    return {"rate": rate, "start_usdt": usdt, "cash": usdt, "positions": {},
            "trades": [], "equity_history": [], "day": today(), "day_start_equity": usdt,
            "halted": False, "halt_reason": ""}


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state(rate):
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return new_state(rate)


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1)


def equity(s, prices):
    return s["cash"] + sum(p["qty"] * prices.get(sym, p["entry"]) for sym, p in s["positions"].items())


def log_trade(s, ts, side, sym, price, qty, note, pnl=None):
    s["trades"].append({"time": ts, "side": side, "coin": sym, "price": round(price, 4),
                        "qty": qty, "note": note, "pnl_usdt": None if pnl is None else round(pnl, 4)})


def step(s, ts, closes_by_coin):
    """One decision round. Same code is used for live and backtest."""
    prices = {c: v[-1] for c, v in closes_by_coin.items() if v}
    eq = equity(s, prices)

    if s["day"] != ts[:10]:
        s["day"], s["day_start_equity"] = ts[:10], eq
        if s["halted"] and s["halt_reason"] == "daily":
            s["halted"], s["halt_reason"] = False, ""

    if not s["halted"]:
        if eq < s["start_usdt"] * (1 - KILL_SWITCH_PCT):
            s["halted"], s["halt_reason"] = True, "kill_switch"
        elif eq < s["day_start_equity"] * (1 - DAILY_LOSS_HALT_PCT):
            s["halted"], s["halt_reason"] = True, "daily"

    # exits first
    for sym in list(s["positions"]):
        if sym not in prices:
            continue
        p, px = s["positions"][sym], prices[sym]
        chg = px / p["entry"] - 1
        reason = None
        if chg <= -STOP_LOSS_PCT:
            reason = "stop-loss"
        elif chg >= TAKE_PROFIT_PCT:
            reason = "take-profit"
        elif signal(closes_by_coin[sym]) == "sell":
            reason = "trend-reversal"
        if reason or s["halt_reason"] == "kill_switch":
            gross = p["qty"] * px
            fee = gross * FEE_PCT
            s["cash"] += gross - fee
            pnl = gross - fee - p["cost"]
            log_trade(s, ts, "SELL", sym, px, p["qty"], reason or "kill-switch", pnl)
            del s["positions"][sym]

    # entries
    if not s["halted"]:
        for sym, closes in closes_by_coin.items():
            if sym in s["positions"] or len(s["positions"]) >= MAX_OPEN_POSITIONS:
                continue
            if signal(closes) == "buy":
                eq = equity(s, prices)
                spend = min(eq * MAX_PER_TRADE_PCT, s["cash"])
                if spend < 1:
                    continue
                fee = spend * FEE_PCT
                qty = (spend - fee) / prices[sym]
                s["cash"] -= spend
                s["positions"][sym] = {"entry": prices[sym], "qty": qty, "cost": spend, "time": ts}
                log_trade(s, ts, "BUY", sym, prices[sym], qty, "ma-cross + rsi ok")

    eq = equity(s, prices)
    s["equity_history"].append({"t": ts, "usdt": round(eq, 4), "ngn": round(eq * s["rate"], 2)})


def make_dashboard(s, label):
    rate = s["rate"]
    eq = s["equity_history"][-1]["usdt"] if s["equity_history"] else s["start_usdt"]
    start_ngn = s["start_usdt"] * rate
    now_ngn = eq * rate
    pnl = now_ngn - start_ngn
    sells = [t for t in s["trades"] if t["side"] == "SELL"]
    wins = [t for t in sells if (t["pnl_usdt"] or 0) > 0]
    winrate = (100 * len(wins) / len(sells)) if sells else 0
    hist = s["equity_history"]
    step_n = max(1, len(hist) // 120)
    pts = [h["ngn"] for h in hist[::step_n]] or [start_ngn]
    lo, hi = min(pts + [start_ngn]), max(pts + [start_ngn])
    span = (hi - lo) or 1
    W, H = 340, 140
    poly = " ".join(f"{i * W / max(1, len(pts) - 1):.1f},{H - (v - lo) / span * H:.1f}" for i, v in enumerate(pts))
    base_y = H - (start_ngn - lo) / span * H
    color = "#16a34a" if pnl >= 0 else "#dc2626"
    rows = ""
    for t in reversed(s["trades"][-15:]):
        pl = "" if t["pnl_usdt"] is None else "₦{:,.0f}".format(t["pnl_usdt"] * rate)
        rows += ("<tr><td>" + t["time"][5:16] + "</td><td class='" + t["side"].lower() + "'>" + t["side"]
                 + "</td><td>" + t["coin"][:-4] + "</td><td>" + t["note"] + "</td><td>" + pl + "</td></tr>")
    if not rows:
        rows = "<tr><td colspan=5>No trades yet</td></tr>"
    pos = "".join(f"<li>{k[:-4]}: entered at ${v['entry']:,.2f}</li>" for k, v in s["positions"].items()) or "<li>None (holding cash)</li>"
    status = "HALTED (" + s["halt_reason"] + ")" if s["halted"] else "Running"
    sign = "+" if pnl >= 0 else ""
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    html = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Bot</title><style>
body{{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#f8fafc;color:#0f172a;max-width:480px;margin:auto}}
.card{{background:#fff;border-radius:12px;padding:14px;margin-bottom:12px;box-shadow:0 1px 3px #0001}}
.big{{font-size:30px;font-weight:700}} .pl{{color:{color};font-weight:600}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px}} small{{color:#64748b}}
table{{width:100%;font-size:12px;border-collapse:collapse}} td{{padding:4px 2px;border-bottom:1px solid #eee}}
.buy{{color:#16a34a}} .sell{{color:#dc2626}} ul{{margin:4px 0;padding-left:18px}}
.badge{{background:#fef3c7;color:#92400e;padding:2px 8px;border-radius:8px;font-size:12px}}
</style></head><body>
<div class="card"><span class="badge">PAPER MONEY - NOT REAL</span> <small>{label}</small>
<div class="big">₦{now_ngn:,.0f}</div>
<div class="pl">{sign}₦{pnl:,.0f} ({pnl/start_ngn*100:+.2f}%)</div>
<small>Started with ₦{start_ngn:,.0f} · Status: {status}</small></div>
<div class="card"><svg viewBox="0 0 {W} {H}" width="100%"><line x1="0" x2="{W}" y1="{base_y:.1f}" y2="{base_y:.1f}" stroke="#94a3b8" stroke-dasharray="4"/>
<polyline fill="none" stroke="{color}" stroke-width="2" points="{poly}"/></svg><small>Dashed line = starting money</small></div>
<div class="card grid"><div><small>Trades closed</small><br><b>{len(sells)}</b></div><div><small>Win rate</small><br><b>{winrate:.0f}%</b></div></div>
<div class="card"><b>Open positions</b><ul>{pos}</ul></div>
<div class="card"><b>Recent trades</b><table>{rows}</table></div>
<small>Updated {updated} UTC. Past paper results do not guarantee future profit.</small>
</body></html>"""
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)


# ---------------- MODES ----------------
def run_live():
    rate = fetch_ngn_rate()
    s = load_state(rate)
    s["rate"] = rate
    data = {c: fetch_closes(c) for c in COINS}
    data = {c: v for c, v in data.items() if v}
    if not data:
        print("Could not fetch prices (no internet or data source down). Nothing traded.")
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    step(s, ts, data)
    save_state(s)
    make_dashboard(s, "live paper trading")
    e = s["equity_history"][-1]
    print(f"[{ts}] wallet ≈ ₦{e['ngn']:,.0f} | open: {list(s['positions'])} | halted: {s['halted']}")


def synthetic_series(n, seed, drift=0.0, vol=0.012, persist=0.97):
    """persist=0.97 -> prices move in streaks (flattering to trend bots, NOT realistic proof).
    persist=0    -> pure random walk (the fair 'no edge' test)."""
    random.seed(seed)
    price, out, trend = 100.0, [], 0.0
    for _ in range(n):
        trend = persist * trend + random.gauss(0, 0.0008) if persist else 0.0
        price *= math.exp(drift + trend + random.gauss(0, vol))
        out.append(price)
    return out


def run_backtest(seed=1, n=1500, drift=0.0, persist=0.97):
    s = new_state(NGN_PER_USDT)  # backtest never touches the live state file
    series = {c: synthetic_series(n, seed * 100 + i, drift, persist=persist) for i, c in enumerate(COINS)}
    for i in range(SLOW + 2, n):
        ts = datetime.fromtimestamp(1_700_000_000 + i * 3600, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        step(s, ts, {c: v[: i + 1] for c, v in series.items()})
    return s


def summary(s):
    e = s["equity_history"][-1]["ngn"]
    sells = [t for t in s["trades"] if t["side"] == "SELL"]
    peak, mdd = 0, 0
    for h in s["equity_history"]:
        peak = max(peak, h["ngn"])
        mdd = max(mdd, (peak - h["ngn"]) / peak)
    return e, len(sells), mdd


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "live"
    if mode == "live":
        run_live()
    elif mode == "backtest":
        for name, persist in (("STREAKY prices (flattering, proves nothing)", 0.97),
                              ("PURE RANDOM prices (fair 'no edge' test)", 0.0)):
            results = []
            for seed in range(1, 21):
                s = run_backtest(seed, persist=persist)
                results.append(summary(s) + (s["halted"],))
            ends = [r[0] for r in results]
            worst = max(r[2] for r in results)
            print(f"{name}: avg end ₦{sum(ends)/len(ends):,.0f} | profitable {sum(e>START_NGN for e in ends)}/20 "
                  f"| worst dip {worst*100:.1f}% | avg trades {sum(r[1] for r in results)/20:.0f}")
        s = run_backtest(1, persist=0.0)
        make_dashboard(s, "SIMULATED demo on random prices (not real data)")
    elif mode == "report":
        s = load_state(NGN_PER_USDT)
        make_dashboard(s, "live paper trading")
    else:
        print(__doc__)
