"""
DECISION ENGINE v1 - BankNifty/Nifty options, right now
--------------------------------------------------------
Answers exactly one question: right now, on the two strikes nearest to
spot, is this BUY CE, BUY PE, WAIT, or NO TRADE?

This is NOT a prediction tool. It reports whether a fixed set of
objective, observable conditions currently point the same way. When they
don't - which is most of the time - the honest answer is WAIT. There is
no confidence score, no probability, no expected holding time anywhere
in this tool; only "N/7 conditions aligned" plus the actual value behind
each one, so you can see for yourself what's missing.

Flow:
  1. Fetch the option chain for the nearest (or chosen) expiry, reusing
     option_analyzer.py's NSE session/fetch/parsing helpers.
  2. Find the nearest strike below and above spot; analyze only those
     two strikes, CE and PE both (4 contracts).
  3. Classify each leg's OI buildup from today's price change + OI change.
  4. Evaluate 7 objective conditions (OI signal, PCR, volume vs chain
     average, premium fairness vs Black-Scholes, risk-reward/breakeven
     achievable in the hours left today, time-of-day filter, and realized
     vol vs IV).
  5. BUY only when all 7 align in one direction. Otherwise WAIT (some
     conditions conflict) or NO TRADE (volume below average, or a hard
     no-trade time window is active).
  6. Alongside the 7 conditions, prints context that doesn't gate the
     decision but matters for judgment: max pain, India VIX (which also
     tightens the premium-fairness threshold when elevated), and an
     event-day/expiry-day IV-crush warning.

Falls back to manual entry (typed spot/strikes/OI/volume/premium per
leg) if the NSE fetch fails, same as option_analyzer.py. Every run is
logged to decisions_log.csv (gitignored) regardless of the decision.
Every run also appends the ATM IV to iv_history.csv (gitignored) so a
future IV-rank feature has data to work with once ~60 days accumulate.

Run:  python decision_engine.py
Needs: pip install requests (NSE fetch), pip install yfinance (realized
vol / India VIX - optional; both degrade gracefully without it)
"""

import csv
import math
import os
import statistics
from datetime import date, datetime, time as dtime, timedelta

import option_analyzer as oa

# ----------------- user-adjustable defaults -----------------
LOT_SIZE = 35
RISK_PCT = 0.01              # 1% of capital risked per trade
SL_PCT = 0.30                # -30% stop-loss
T1_PCT = 0.60                # +60% target 1
T2_PCT = 1.00                # +100% target 2
ENTRY_BAND_PCT = 0.02        # LTP +/- 2% entry zone

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
TRADING_HOURS = 6.25          # 9:15-15:30 IST
NO_TRADE_WINDOWS = [
    (dtime(9, 15), dtime(9, 25), "09:15-09:25 opening volatility window"),
    (dtime(15, 0), dtime(15, 30), "15:00-15:30 closing/settlement window"),
]
LUNCH_WINDOW = (dtime(12, 0), dtime(13, 15))

FAIRNESS_THRESHOLD = 1.10           # tightened to 1.05 when India VIX > 17
FAIRNESS_THRESHOLD_HIGH_VIX = 1.05
VIX_TIGHTEN_LEVEL = 17.0
RV_IV_THRESHOLD = 1.10              # option IV / realized vol must be <= this
RV_LOOKBACK_DAYS = 30

YF_INDEX_TICKERS = {"BANKNIFTY": "^NSEBANK", "NIFTY": "^NSEI"}
YF_INDIA_VIX_TICKER = "^INDIAVIX"
CLOSES_CSV_FILE = "closes.csv"

# Verified FY27 RBI MPC announcement dates, plus Union Budget 2026-02-01.
MPC_DATES_2026 = [
    date(2026, 4, 8),
    date(2026, 6, 5),
    date(2026, 8, 5),
    date(2026, 10, 7),
    date(2026, 12, 4),
]
BUDGET_DATE_2026 = date(2026, 2, 1)

EVENT_DATES_2026 = {BUDGET_DATE_2026: "Union Budget"}
EVENT_DATES_2026.update({d: "RBI MPC decision" for d in MPC_DATES_2026})

# The day before each MPC announcement carries elevated IV-crush risk in
# its own right (positioning/unwind ahead of the decision).
PRE_MPC_DATES_2026 = {
    d - timedelta(days=1): "day before RBI MPC decision (elevated IV-crush risk)"
    for d in MPC_DATES_2026
}

LOG_FILE = "decisions_log.csv"
LOG_FIELDS = [
    "timestamp", "symbol", "spot", "strike_below", "strike_above",
    "ce_below_buildup", "ce_above_buildup", "pe_below_buildup",
    "pe_above_buildup", "pcr", "oi_bias", "volume_ok", "fairness_ratio",
    "fairness_threshold", "pts_needed", "rv", "rv_source", "iv_rv_ratio",
    "vix", "max_pain", "event_flag", "event_label", "score", "total",
    "decision", "candidate_opt", "candidate_strike",
]

IV_HISTORY_FILE = "iv_history.csv"
IV_HISTORY_FIELDS = ["date", "atm_iv"]
# ------------------------------------------------------------


def resolve_iv(premium, S, K, T, r, opt, iv_pct):
    """Prefer NSE chain IV; else back out from premium (via
    option_analyzer.implied_vol). Returns iv as a fraction, or None."""
    if iv_pct and iv_pct > 0:
        return iv_pct / 100
    return oa.implied_vol(premium, S, K, T, r, opt)


def classify_oi_buildup(price_change_pct, oi_change):
    """price UP + OI UP = long buildup; price DOWN + OI UP = short buildup;
    price UP + OI DOWN = short covering; price DOWN + OI DOWN = long
    unwinding."""
    price_up, price_down = price_change_pct > 0, price_change_pct < 0
    oi_up, oi_down = oi_change > 0, oi_change < 0
    if price_up and oi_up:
        return "long buildup"
    if price_down and oi_up:
        return "short buildup"
    if price_up and oi_down:
        return "short covering"
    if price_down and oi_down:
        return "long unwinding"
    return "flat"


def classify_oi_signal(ce_below, ce_above, pe_below, pe_above):
    """bullish: CE longs building + PE shorts building at both strikes.
    bearish: the reverse. Anything else is mixed (fail)."""
    ce_bullish = ce_below == "long buildup" and ce_above == "long buildup"
    pe_bullish = pe_below == "short buildup" and pe_above == "short buildup"
    ce_bearish = ce_below == "short buildup" and ce_above == "short buildup"
    pe_bearish = pe_below == "long buildup" and pe_above == "long buildup"
    if ce_bullish and pe_bullish:
        return "bullish"
    if ce_bearish and pe_bearish:
        return "bearish"
    return "mixed"


def pcr_direction(pcr):
    if pcr is None:
        return "neutral"
    if pcr > 1.2:
        return "bullish"
    if pcr < 0.8:
        return "bearish"
    return "neutral"


def time_of_day_status(now):
    """Returns (hard_no_trade, lunch_flag, label)."""
    t = now.time()
    for start, end, label in NO_TRADE_WINDOWS:
        if start <= t < end:
            return True, False, label
    lunch_flag = LUNCH_WINDOW[0] <= t < LUNCH_WINDOW[1]
    return False, lunch_flag, "outside no-trade windows"


def hours_left_in_session(now):
    t = now.time()
    if t <= MARKET_OPEN:
        return TRADING_HOURS
    if t >= MARKET_CLOSE:
        return 0.0
    open_dt = datetime.combine(now.date(), MARKET_OPEN)
    close_dt = datetime.combine(now.date(), MARKET_CLOSE)
    return max((close_dt - now).total_seconds() / 3600.0, 0.0)


def nearest_strikes(strikes, spot):
    below_list = [s for s in strikes if s <= spot]
    above_list = [s for s in strikes if s > spot]
    if not below_list or not above_list:
        return None, None
    return max(below_list), min(above_list)


def realized_vol(closes):
    """Annualized realized vol from a list of daily closes (oldest first):
    stdev of log returns * sqrt(252). None if there aren't enough closes
    for at least 2 log returns."""
    if not closes or len(closes) < 3:
        return None
    log_returns = [math.log(closes[i] / closes[i - 1])
                   for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(log_returns) < 2:
        return None
    return statistics.stdev(log_returns) * math.sqrt(252)


def fetch_realized_vol_yfinance(yf_symbol, days=RV_LOOKBACK_DAYS):
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        hist = yf.Ticker(yf_symbol).history(period=f"{days + 20}d")
        closes = hist["Close"].dropna().tolist()
    except Exception as e:
        print(f"  [!] yfinance fetch for {yf_symbol} failed: {e}")
        return None
    return closes[-days:] if len(closes) >= 3 else None


def load_closes_csv(path=CLOSES_CSV_FILE):
    """Manual fallback: one close per line, oldest first."""
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            closes = [float(line.strip()) for line in f if line.strip()]
    except (OSError, ValueError) as e:
        print(f"  [!] Could not read {path}: {e}")
        return None
    return closes if len(closes) >= 3 else None


def fetch_realized_vol(symbol, days=RV_LOOKBACK_DAYS):
    """Try yfinance first (^NSEBANK/^NSEI), then a manual closes.csv
    fallback. Returns (rv, source) or (None, None)."""
    yf_symbol = YF_INDEX_TICKERS.get(symbol, "^NSEI")
    closes = fetch_realized_vol_yfinance(yf_symbol, days)
    if closes:
        rv = realized_vol(closes)
        if rv:
            return rv, "yfinance"
    closes = load_closes_csv()
    if closes:
        rv = realized_vol(closes[-(days + 1):])
        if rv:
            return rv, "closes.csv"
    return None, None


def fetch_india_vix():
    """India VIX via yfinance. Returns a float, or None on any failure -
    fallback is to skip silently (no error printed), since VIX is
    context, not a required condition. Output is redirected because
    yfinance prints its own fetch errors directly rather than raising
    only, which would otherwise defeat "skip silently"."""
    import contextlib
    import io
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            import yfinance as yf
            closes = yf.Ticker(YF_INDIA_VIX_TICKER).history(period="5d")["Close"].dropna()
        return float(closes.iloc[-1]) if len(closes) else None
    except Exception:
        return None


def compute_max_pain(rows):
    """Strike that minimizes total option-writer payout obligation:
    Pain(Kc) = sum(CE_OI(K) * max(Kc-K, 0)) + sum(PE_OI(K) * max(K-Kc, 0)).
    None if there are fewer than 2 strikes with OI data."""
    strikes = sorted({float(row["strikePrice"]) for row in rows
                      if row.get("strikePrice") is not None})
    if len(strikes) < 2:
        return None
    ce_oi = {float(row["strikePrice"]): (row.get("CE") or {}).get("openInterest") or 0
             for row in rows if row.get("strikePrice") is not None}
    pe_oi = {float(row["strikePrice"]): (row.get("PE") or {}).get("openInterest") or 0
             for row in rows if row.get("strikePrice") is not None}
    best_strike, best_pain = None, None
    for kc in strikes:
        pain = (sum(ce_oi[k] * max(kc - k, 0) for k in strikes) +
                sum(pe_oi[k] * max(k - kc, 0) for k in strikes))
        if best_pain is None or pain < best_pain:
            best_pain, best_strike = pain, kc
    return best_strike


def event_day_status(now, T_days):
    """Returns (is_event_day, label). Priority: the event day itself, then
    the day before an MPC decision, then expiry day (<=1 day left)."""
    today = now.date()
    if today in EVENT_DATES_2026:
        return True, EVENT_DATES_2026[today]
    if today in PRE_MPC_DATES_2026:
        return True, PRE_MPC_DATES_2026[today]
    if T_days <= 1:
        return True, "expiry day"
    return False, None


def compute_atm_iv(S, strike_below, strike_above, legs):
    """Average of CE/PE impliedVolatility (NSE-reported, in %) at
    whichever of the two nearest strikes is closer to spot."""
    prefix = "below" if abs(S - strike_below) <= abs(strike_above - S) else "above"
    ce_iv = legs[("CE", prefix)].get("impliedVolatility") or 0
    pe_iv = legs[("PE", prefix)].get("impliedVolatility") or 0
    ivs = [v for v in (ce_iv, pe_iv) if v]
    return (sum(ivs) / len(ivs)) if ivs else None


def fetch_chain(symbol):
    """Fetch expiries, let the user pick one, then fetch that expiry's
    chain via option_analyzer's NSE helpers. Returns (rows, S, expiry) on
    success, (None, None, None) on any failure."""
    try:
        s = oa._nse_session()
        expiries = oa.fetch_expiries(s, symbol)
        expiry = oa.pick_expiry(expiries)
        data = oa.fetch_chain_for_expiry(s, symbol, expiry)
        rows = oa._find_rows(data)
        if not rows:
            print("  [!] Could not locate strike rows in NSE response.")
            oa._debug_shape(data)
            return None, None, None
        S = oa._find_underlying(data, rows)
        if not S:
            print("  [!] Could not locate the underlying spot value in "
                  "the NSE response.")
            oa._debug_shape(data)
            return None, None, None
        return rows, S, expiry
    except Exception as e:
        print(f"  [!] NSE fetch failed ({e}). Switching to manual entry.")
        return None, None, None


def parse_chain_to_legs(rows, S):
    """Pick the two strikes nearest spot out of the fetched rows and
    return (strike_below, strike_above, legs, avg_volume), or None if
    spot isn't bracketed by the available strikes."""
    strikes = sorted({float(row["strikePrice"]) for row in rows
                      if row.get("strikePrice") is not None})
    below, above = nearest_strikes(strikes, S)
    if below is None or above is None:
        return None
    legs = {
        ("CE", "below"): oa.get_leg(rows, below, "CE") or {},
        ("PE", "below"): oa.get_leg(rows, below, "PE") or {},
        ("CE", "above"): oa.get_leg(rows, above, "CE") or {},
        ("PE", "above"): oa.get_leg(rows, above, "PE") or {},
    }
    volumes = [leg["totalTradedVolume"] for row in rows
               for leg in (row.get("CE"), row.get("PE"))
               if isinstance(leg, dict) and leg.get("totalTradedVolume") is not None]
    avg_volume = statistics.mean(volumes) if volumes else 0
    return below, above, legs, avg_volume


# ---- Phase 2 stubs: wired into the conditions list later without a
# redesign - just append their (name, passed, detail) dicts to the
# `conditions` list built in evaluate_decision(). Each currently returns
# None because it needs broker-supplied candle/order-flow data this tool
# doesn't have access to. ----

def vwap_check(*args, **kwargs):
    # requires broker API (candle/order-flow data) - phase 2
    return None


def ema_trend(*args, **kwargs):
    # requires broker API (candle/order-flow data) - phase 2
    return None


def price_action_last5(*args, **kwargs):
    # requires broker API (candle/order-flow data) - phase 2
    return None


def oi_footprint_absorption(*args, **kwargs):
    # requires broker API (candle/order-flow data) - phase 2
    return None


def evaluate_decision(symbol, S, T_days, strike_below, strike_above, legs,
                      avg_volume, capital, now=None, rv=None, rv_source=None,
                      vix=None, max_pain=None):
    """Run the 7 objective conditions on the 4 legs and return a decision
    dict. `now` is injectable so the time-of-day filter is testable; `rv`
    (realized vol), `vix` (India VIX), and `max_pain` are injectable too
    so tests don't need network/yfinance access - callers normally get
    them from fetch_realized_vol(), fetch_india_vix(), and
    compute_max_pain()."""
    now = now or datetime.now()
    fairness_threshold = (FAIRNESS_THRESHOLD_HIGH_VIX
                          if (vix is not None and vix > VIX_TIGHTEN_LEVEL)
                          else FAIRNESS_THRESHOLD)
    is_event_day, event_label = event_day_status(now, T_days)
    atm_iv = compute_atm_iv(S, strike_below, strike_above, legs)

    buildups = {
        key: classify_oi_buildup(leg.get("pChange") or 0,
                                 leg.get("changeinOpenInterest") or 0)
        for key, leg in legs.items()
    }
    ce_below_b = buildups[("CE", "below")]
    ce_above_b = buildups[("CE", "above")]
    pe_below_b = buildups[("PE", "below")]
    pe_above_b = buildups[("PE", "above")]

    oi_bias = classify_oi_signal(ce_below_b, ce_above_b, pe_below_b, pe_above_b)

    ce_oi = ((legs[("CE", "below")].get("openInterest") or 0) +
             (legs[("CE", "above")].get("openInterest") or 0))
    pe_oi = ((legs[("PE", "below")].get("openInterest") or 0) +
             (legs[("PE", "above")].get("openInterest") or 0))
    pcr = (pe_oi / ce_oi) if ce_oi else None
    pcr_bias = pcr_direction(pcr)

    hard_no_trade, lunch_flag, time_label = time_of_day_status(now)
    direction = oi_bias if oi_bias in ("bullish", "bearish") else None

    if direction == "bullish":
        candidate_key, candidate_strike = ("CE", "above"), strike_above
    elif direction == "bearish":
        candidate_key, candidate_strike = ("PE", "below"), strike_below
    else:
        # No clean OI-implied direction. Still pick a nominal candidate so
        # the per-leg conditions below have something to compute/display -
        # this can never lead to a BUY since direction is None.
        candidate_key, candidate_strike = ("CE", "above"), strike_above

    candidate_leg = legs[candidate_key]
    candidate_opt = candidate_key[0]
    premium = candidate_leg.get("lastPrice")

    daily_range = oa.AVG_DAILY_RANGE.get(symbol, 200)
    hours_left = hours_left_in_session(now)
    achievable_pts = (daily_range / TRADING_HOURS) * hours_left

    volume = candidate_leg.get("totalTradedVolume") or 0
    volume_ok = volume > avg_volume

    fairness_ratio = None
    fairness_ok = False
    pts_needed = None
    rr_ok = False
    iv = None

    if premium and premium > 0:
        T = T_days / 365
        iv_pct = candidate_leg.get("impliedVolatility") or 0
        iv = resolve_iv(premium, S, candidate_strike, T, oa.RISK_FREE_RATE,
                        candidate_opt, iv_pct)
        if iv:
            fair = oa.bs_price(S, candidate_strike, T, oa.RISK_FREE_RATE,
                               iv, candidate_opt)
            if fair > 0:
                fairness_ratio = premium / fair
                fairness_ok = fairness_ratio <= fairness_threshold
        breakeven = (candidate_strike + premium if candidate_opt == "CE"
                     else candidate_strike - premium)
        pts_needed = abs(breakeven - S)
        rr_ok = hours_left > 0 and pts_needed <= achievable_pts

    iv_rv_ratio = (iv / rv) if (iv and rv) else None
    rv_ok = iv_rv_ratio is not None and iv_rv_ratio <= RV_IV_THRESHOLD

    conditions = [
        {"name": "OI signal",
         "passed": direction is not None,
         "detail": (f"{oi_bias} (CE {ce_below_b}/{ce_above_b}, "
                    f"PE {pe_below_b}/{pe_above_b})")},
        {"name": "PCR (PE OI / CE OI)",
         "passed": direction is not None and pcr_bias == direction,
         "detail": f"{pcr:.2f}" if pcr is not None else "n/a (CE OI is 0)"},
        {"name": "Volume vs chain average",
         "passed": volume_ok,
         "detail": f"{volume:.0f} vs avg {avg_volume:.0f}"},
        {"name": f"Premium fairness (prem/fair <= {fairness_threshold:.2f})",
         "passed": fairness_ok,
         "detail": f"{fairness_ratio:.2f}" if fairness_ratio is not None else "n/a"},
        {"name": "Risk-reward / breakeven achievable (RR fixed 2.0)",
         "passed": rr_ok,
         "detail": (f"needs {pts_needed:.0f} pts vs {achievable_pts:.0f} pts "
                    f"achievable in {hours_left:.2f}h left today")
                   if pts_needed is not None else "n/a"},
        {"name": "Time-of-day filter",
         "passed": not hard_no_trade,
         "detail": time_label},
        {"name": f"Realized vol vs IV (IV/RV <= {RV_IV_THRESHOLD:.2f})",
         "passed": rv_ok,
         "detail": (f"IV {iv * 100:.1f}% / RV {rv * 100:.1f}% "
                    f"(ratio {iv_rv_ratio:.2f}, source: {rv_source})")
                   if iv_rv_ratio is not None
                   else "n/a (no realized-vol data - install yfinance or provide closes.csv)"},
    ]
    score = sum(1 for c in conditions if c["passed"])
    total = len(conditions)

    if hard_no_trade:
        decision = "NO TRADE"
        reason = f"Time-of-day filter active: {time_label}."
    elif not volume_ok:
        decision = "NO TRADE"
        reason = (f"Candidate leg volume ({volume:.0f}) is below the chain "
                  f"average ({avg_volume:.0f}) - no participation to confirm the move.")
    elif score == total and direction == "bullish":
        decision = "BUY CE"
        reason = f"All {total} conditions align bullish at the nearest strikes."
    elif score == total and direction == "bearish":
        decision = "BUY PE"
        reason = f"All {total} conditions align bearish at the nearest strikes."
    else:
        decision = "WAIT"
        failing = "; ".join(f"{c['name']}: {c['detail']}"
                            for c in conditions if not c["passed"])
        reason = f"Not all conditions align yet - {failing}"

    return {
        "symbol": symbol, "S": S, "T_days": T_days,
        "strike_below": strike_below, "strike_above": strike_above,
        "buildups": {"CE_below": ce_below_b, "CE_above": ce_above_b,
                     "PE_below": pe_below_b, "PE_above": pe_above_b},
        "oi_bias": oi_bias, "pcr": pcr, "direction": direction,
        "candidate_opt": candidate_opt, "candidate_strike": candidate_strike,
        "premium": premium, "conditions": conditions,
        "score": score, "total": total,
        "decision": decision, "reason": reason,
        "lunch_flag": lunch_flag, "capital": capital, "now": now,
        "fairness_threshold": fairness_threshold,
        "rv": rv, "rv_source": rv_source, "iv_rv_ratio": iv_rv_ratio,
        "vix": vix, "max_pain": max_pain,
        "is_event_day": is_event_day, "event_label": event_label,
        "atm_iv": atm_iv,
    }


def print_decision(result):
    print("=" * 70)
    print("  DECISION ENGINE v1")
    print("  Alignment of current structure - not a prediction. Direction risk is yours.")
    print("=" * 70)
    print(f"  Symbol: {result['symbol']}   Spot: {result['S']}   "
          f"Days to expiry: {result['T_days']}")
    print(f"  Nearest strikes: {result['strike_below']:.0f} (below) / "
          f"{result['strike_above']:.0f} (above)")
    print("-" * 70)
    b = result["buildups"]
    print("  OI BUILDUP (today's price change + OI change):")
    print(f"    CE {result['strike_below']:.0f}: {b['CE_below']}")
    print(f"    CE {result['strike_above']:.0f}: {b['CE_above']}")
    print(f"    PE {result['strike_below']:.0f}: {b['PE_below']}")
    print(f"    PE {result['strike_above']:.0f}: {b['PE_above']}")
    print("-" * 70)
    if result["max_pain"] is not None:
        diff = result["S"] - result["max_pain"]
        direction = "above" if diff > 0 else "below" if diff < 0 else "at"
        print(f"  Max pain: {result['max_pain']:.0f} (spot is {abs(diff):.0f} "
              f"pts {direction} max pain)")
    if result["vix"] is not None:
        tightened = (" (premium-fairness threshold tightened to "
                    f"{FAIRNESS_THRESHOLD_HIGH_VIX:.2f})"
                    if result["fairness_threshold"] < FAIRNESS_THRESHOLD else "")
        print(f"  India VIX: {result['vix']:.2f}{tightened}")
    if result["is_event_day"]:
        print(f"  *** IV-CRUSH RISK: {result['event_label']} - IV can collapse "
              "independent of direction. ***")
    if result["max_pain"] is not None or result["vix"] is not None or result["is_event_day"]:
        print("-" * 70)
    print("  CONDITIONS:")
    for c in result["conditions"]:
        print(f"    [{'PASS' if c['passed'] else 'FAIL'}] {c['name']}  ({c['detail']})")
    print("-" * 70)
    print(f"  ALIGNMENT SCORE: {result['score']}/{result['total']} "
          "conditions aligned")
    print(f"  DECISION: {result['decision']}")
    print(f"  Reason: {result['reason']}")
    if result["lunch_flag"]:
        print("  NOTE: 12:00-13:15 IST lull window - lower quality setup, "
              "size down or wait even if the decision above is BUY.")

    if result["decision"] in ("BUY CE", "BUY PE"):
        premium = result["premium"]
        lo, hi = premium * (1 - ENTRY_BAND_PCT), premium * (1 + ENTRY_BAND_PCT)
        sl = premium * (1 - SL_PCT)
        t1 = premium * (1 + T1_PCT)
        t2 = premium * (1 + T2_PCT)
        risk_per_lot = (premium - sl) * LOT_SIZE
        max_risk = result["capital"] * RISK_PCT
        lots = int(max_risk // risk_per_lot) if risk_per_lot > 0 else 0
        print("-" * 70)
        print(f"  Candidate: {result['symbol']} {result['candidate_strike']:.0f} "
              f"{result['candidate_opt']}")
        print(f"  Entry premium range (LTP +/- 2%): Rs {lo:.1f} - Rs {hi:.1f}")
        print(f"  SL (-30%): Rs {sl:.1f}   T1 (+60%): Rs {t1:.1f}   "
              f"T2 (+100%): Rs {t2:.1f}")
        if lots > 0:
            print(f"  Position size at 1% risk on Rs {result['capital']:.0f} "
                  f"capital: {lots} lot(s) (lot size {LOT_SIZE}), risking "
                  f"Rs {risk_per_lot * lots:.0f}")
        else:
            print(f"  Position size: 0 lots - risk per lot (Rs {risk_per_lot:.0f}) "
                  f"exceeds 1% of capital (Rs {max_risk:.0f}).")
    print("=" * 70)


def log_decision(result, path=LOG_FILE):
    fairness_detail = next(c["detail"] for c in result["conditions"]
                           if c["name"].startswith("Premium fairness"))
    pts_detail = next(c["detail"] for c in result["conditions"]
                      if c["name"].startswith("Risk-reward"))
    volume_ok = next(c["passed"] for c in result["conditions"]
                     if c["name"] == "Volume vs chain average")
    row = {
        "timestamp": result["now"].isoformat(timespec="seconds"),
        "symbol": result["symbol"],
        "spot": result["S"],
        "strike_below": result["strike_below"],
        "strike_above": result["strike_above"],
        "ce_below_buildup": result["buildups"]["CE_below"],
        "ce_above_buildup": result["buildups"]["CE_above"],
        "pe_below_buildup": result["buildups"]["PE_below"],
        "pe_above_buildup": result["buildups"]["PE_above"],
        "pcr": f"{result['pcr']:.3f}" if result["pcr"] is not None else "",
        "oi_bias": result["oi_bias"],
        "volume_ok": volume_ok,
        "fairness_ratio": fairness_detail,
        "fairness_threshold": result["fairness_threshold"],
        "pts_needed": pts_detail,
        "rv": f"{result['rv']:.4f}" if result["rv"] is not None else "",
        "rv_source": result["rv_source"] or "",
        "iv_rv_ratio": (f"{result['iv_rv_ratio']:.2f}"
                       if result["iv_rv_ratio"] is not None else ""),
        "vix": f"{result['vix']:.2f}" if result["vix"] is not None else "",
        "max_pain": result["max_pain"] if result["max_pain"] is not None else "",
        "event_flag": result["is_event_day"],
        "event_label": result["event_label"] or "",
        "score": result["score"],
        "total": result["total"],
        "decision": result["decision"],
        "candidate_opt": result["candidate_opt"],
        "candidate_strike": result["candidate_strike"],
    }
    write_header = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def log_iv_history(now, atm_iv_value, path=IV_HISTORY_FILE):
    """Append date + ATM IV so a future IV-rank feature has history to
    work with. No-ops if ATM IV couldn't be computed for this run."""
    if atm_iv_value is None:
        return
    write_header = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=IV_HISTORY_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({"date": now.date().isoformat(),
                         "atm_iv": f"{atm_iv_value:.4f}"})


def _manual_leg(label):
    print(f"  -- {label} --")
    premium = float(input("    Premium (LTP): "))
    iv_pct = float(input("    IV %% (0 if unknown): "))
    oi = float(input("    Open interest: "))
    oi_chg = float(input("    Change in OI: "))
    price_chg_pct = float(input("    Price %% change today: "))
    volume = float(input("    Total traded volume: "))
    return {
        "lastPrice": premium, "impliedVolatility": iv_pct,
        "openInterest": oi, "changeinOpenInterest": oi_chg,
        "pChange": price_chg_pct, "totalTradedVolume": volume,
    }


def manual_entry():
    print("  Switching to manual entry - you'll need values for all 4 legs.")
    S = float(input("Spot price: "))
    strike_below = float(input("Nearest strike BELOW spot: "))
    strike_above = float(input("Nearest strike ABOVE spot: "))
    T_days = int(input("Days to expiry: "))
    legs = {
        ("CE", "below"): _manual_leg(f"CE {strike_below:.0f}"),
        ("PE", "below"): _manual_leg(f"PE {strike_below:.0f}"),
        ("CE", "above"): _manual_leg(f"CE {strike_above:.0f}"),
        ("PE", "above"): _manual_leg(f"PE {strike_above:.0f}"),
    }
    avg_volume = float(input("Average total volume across all strikes today: "))
    return S, T_days, strike_below, strike_above, legs, avg_volume


def main():
    print("=" * 70)
    print("  DECISION ENGINE v1")
    print("  Alignment of current structure - not a prediction. Direction risk is yours.")
    print("=" * 70)
    symbol = (input("Index (NIFTY / BANKNIFTY) [BANKNIFTY]: ").strip().upper()
              or "BANKNIFTY")
    capital_in = input("Trading capital (Rs) for position sizing [100000]: ").strip()
    capital = float(capital_in) if capital_in else 100000.0

    now = datetime.now()
    rows, S, expiry = fetch_chain(symbol)
    max_pain = None
    if rows is None:
        S, T_days, strike_below, strike_above, legs, avg_volume = manual_entry()
    else:
        parsed = parse_chain_to_legs(rows, S)
        if parsed is None:
            print("Spot price is outside the strikes available in the chain.")
            return
        strike_below, strike_above, legs, avg_volume = parsed
        T_days = oa.days_between(expiry)
        max_pain = compute_max_pain(rows)
        print(f"\nFetched: spot={S}, expiry={expiry}, days to expiry={T_days}")

    rv, rv_source = fetch_realized_vol(symbol)
    vix = fetch_india_vix()

    result = evaluate_decision(symbol, S, T_days, strike_below, strike_above,
                               legs, avg_volume, capital, now,
                               rv=rv, rv_source=rv_source, vix=vix,
                               max_pain=max_pain)
    print_decision(result)
    log_decision(result)
    log_iv_history(now, result["atm_iv"])


if __name__ == "__main__":
    main()
