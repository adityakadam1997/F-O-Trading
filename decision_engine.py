"""
DECISION ENGINE v1 - BankNifty/Nifty options, 5+5 strike scan
----------------------------------------------------------------
Scans the 5 strikes above and 5 below spot (whatever strikes actually
exist in the fetched chain - not a hardcoded 50/100 step) and prints a
qualification table plus up to 3 conditional trade cards on the
directional side.

This is NOT a prediction tool. Direction still comes from the two
nearest strikes' OI structure + PCR, exactly as before. Four market-wide
conditions (OI signal, PCR, time-of-day filter, realized vol vs ATM IV)
are evaluated once; three per-strike conditions (premium fairness,
volume vs chain average, breakeven achievable in the hours left today)
are evaluated separately for every strike in the band on the directional
side. A strike only QUALIFIES when all 3 of its own conditions pass -
combined with the market-wide 4, that's the same 7 conditions as before,
just split into "once" and "per-strike". There is no confidence score,
no probability, no expected holding time anywhere in this tool.

Flow:
  1. Fetch the option chain for the nearest (or chosen) expiry, reusing
     option_analyzer.py's NSE session/fetch/parsing helpers.
  2. Evaluate the 4 market-wide conditions from the two nearest strikes
     (unchanged from before) - this fixes direction (bullish/bearish/
     unclear).
  3. Classify OI buildup for CE and PE at all 10 band strikes (5 above,
     5 below spot).
  4. On the directional side only (CEs above if bullish, PEs below if
     bearish), evaluate the 3 per-strike conditions for every strike in
     the band and print a qualification table.
  5. If market-wide conditions aren't all aligned (or a hard no-trade
     time window is active), the table still prints but no trade cards
     do - the printed reason states which market-wide condition is
     blocking.
  6. Otherwise, for up to 3 qualified strikes (ranked by conditions
     passed, then IV/realized-vol ratio, then smallest breakeven
     distance), print a conditional trade card: the index level that
     would confirm the move (crossing the adjacent near strike), the
     Black-Scholes-repriced premium at that trigger level (same IV,
     minus 1 hour of time decay), entry/SL/targets, and position size.
     Every card is headed "Conditional plan, not a prediction - valid
     only if the trigger level is hit while conditions still hold."

Falls back to a reduced single-nearest-strike manual assessment (typed
spot/strikes/OI/volume/premium for the 4 nearest-strike legs) if the NSE
fetch fails - a full 5+5 scan needs the whole chain, which manual entry
can't substitute for, same reasoning as option_analyzer.py's chain
scanner. Every run is logged to decisions_log.csv (gitignored). Every
run also appends the ATM IV to iv_history.csv (gitignored) so a future
IV-rank feature has data to work with once ~60 days accumulate.

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
CARD_ENTRY_BAND_PCT = 0.03   # trade-card estimated premium +/- 3%
BAND_SIZE = 5                # strikes above and below spot
TRIGGER_DECAY_HOURS = 1.0    # time decay subtracted when repricing at trigger

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
RV_IV_THRESHOLD = 1.10              # ATM IV / realized vol must be <= this
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
    "pe_above_buildup", "pcr", "oi_bias", "fairness_threshold", "rv",
    "rv_source", "iv_rv_ratio", "vix", "max_pain", "event_flag",
    "event_label", "market_score", "market_total", "side_opt",
    "strikes_scanned", "num_qualified", "qualified_strikes",
    "cards_issued", "decision",
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


def all_strikes(rows):
    return sorted({float(row["strikePrice"]) for row in rows
                  if row.get("strikePrice") is not None})


def strike_band(strikes, spot, n=BAND_SIZE):
    """The n strikes nearest spot on each side, nearest-first. Reads
    whatever strikes actually exist in the chain rather than assuming a
    fixed step (50 for NIFTY / 100 for BANKNIFTY in practice, but this
    doesn't hardcode that)."""
    below = sorted([s for s in strikes if s <= spot], reverse=True)[:n]
    above = sorted([s for s in strikes if s > spot])[:n]
    return below, above


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
    strikes = all_strikes(rows)
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
    strikes = all_strikes(rows)
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
# redesign - just append their (name, passed, detail) dicts to a
# conditions list (market-wide or per-strike). Each currently returns
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


def evaluate_market_wide(symbol, S, T_days, strike_below, strike_above,
                         legs_near, now, rv, rv_source, vix, max_pain):
    """The 4 market-wide conditions (OI signal, PCR, time-of-day, realized
    vol vs ATM IV), evaluated once from the two nearest strikes - unchanged
    from earlier versions. `now`/`rv`/`vix`/`max_pain` are all injectable
    for testing without network/yfinance access."""
    now = now or datetime.now()
    fairness_threshold = (FAIRNESS_THRESHOLD_HIGH_VIX
                          if (vix is not None and vix > VIX_TIGHTEN_LEVEL)
                          else FAIRNESS_THRESHOLD)
    is_event_day, event_label = event_day_status(now, T_days)
    atm_iv = compute_atm_iv(S, strike_below, strike_above, legs_near)  # in %

    buildups = {
        key: classify_oi_buildup(leg.get("pChange") or 0,
                                 leg.get("changeinOpenInterest") or 0)
        for key, leg in legs_near.items()
    }
    ce_below_b = buildups[("CE", "below")]
    ce_above_b = buildups[("CE", "above")]
    pe_below_b = buildups[("PE", "below")]
    pe_above_b = buildups[("PE", "above")]

    oi_bias = classify_oi_signal(ce_below_b, ce_above_b, pe_below_b, pe_above_b)

    ce_oi = ((legs_near[("CE", "below")].get("openInterest") or 0) +
             (legs_near[("CE", "above")].get("openInterest") or 0))
    pe_oi = ((legs_near[("PE", "below")].get("openInterest") or 0) +
             (legs_near[("PE", "above")].get("openInterest") or 0))
    pcr = (pe_oi / ce_oi) if ce_oi else None
    pcr_bias = pcr_direction(pcr)

    hard_no_trade, lunch_flag, time_label = time_of_day_status(now)
    direction = oi_bias if oi_bias in ("bullish", "bearish") else None

    iv_rv_ratio = ((atm_iv / 100) / rv) if (atm_iv and rv) else None
    rv_ok = iv_rv_ratio is not None and iv_rv_ratio <= RV_IV_THRESHOLD

    conditions = [
        {"name": "OI signal",
         "passed": direction is not None,
         "detail": (f"{oi_bias} (CE {ce_below_b}/{ce_above_b}, "
                    f"PE {pe_below_b}/{pe_above_b})")},
        {"name": "PCR (PE OI / CE OI)",
         "passed": direction is not None and pcr_bias == direction,
         "detail": f"{pcr:.2f}" if pcr is not None else "n/a (CE OI is 0)"},
        {"name": "Time-of-day filter",
         "passed": not hard_no_trade,
         "detail": time_label},
        {"name": f"Realized vol vs ATM IV (IV/RV <= {RV_IV_THRESHOLD:.2f})",
         "passed": rv_ok,
         "detail": (f"ATM IV {atm_iv:.1f}% / RV {rv * 100:.1f}% "
                    f"(ratio {iv_rv_ratio:.2f}, source: {rv_source})")
                   if iv_rv_ratio is not None
                   else "n/a (no realized-vol data - install yfinance or provide closes.csv)"},
    ]
    score = sum(1 for c in conditions if c["passed"])
    total = len(conditions)
    all_aligned = (score == total) and (direction is not None)

    return {
        "symbol": symbol, "S": S, "T_days": T_days, "now": now,
        "strike_below": strike_below, "strike_above": strike_above,
        "buildups": {"CE_below": ce_below_b, "CE_above": ce_above_b,
                     "PE_below": pe_below_b, "PE_above": pe_above_b},
        "oi_bias": oi_bias, "pcr": pcr, "direction": direction,
        "hard_no_trade": hard_no_trade, "lunch_flag": lunch_flag,
        "time_label": time_label, "atm_iv": atm_iv,
        "rv": rv, "rv_source": rv_source, "iv_rv_ratio": iv_rv_ratio,
        "vix": vix, "max_pain": max_pain,
        "fairness_threshold": fairness_threshold,
        "is_event_day": is_event_day, "event_label": event_label,
        "conditions": conditions, "score": score, "total": total,
        "all_aligned": all_aligned,
    }


def evaluate_strike_conditions(strike, opt, leg, S, T_days, avg_volume,
                               fairness_threshold, hours_left, daily_range,
                               r=None):
    """The 3 per-strike conditions: premium fairness (VIX-adaptive),
    volume vs chain average, and breakeven achievable in the hours left
    today. A strike QUALIFIES only when all 3 pass."""
    r = oa.RISK_FREE_RATE if r is None else r
    premium = leg.get("lastPrice")
    achievable_pts = (daily_range / TRADING_HOURS) * hours_left
    volume = leg.get("totalTradedVolume") or 0
    volume_ok = volume > avg_volume

    iv = None
    fairness_ratio = None
    fairness_ok = False
    pts_needed = None
    breakeven_ok = False

    if premium and premium > 0:
        T = T_days / 365
        iv_pct = leg.get("impliedVolatility") or 0
        iv = resolve_iv(premium, S, strike, T, r, opt, iv_pct)
        if iv:
            fair = oa.bs_price(S, strike, T, r, iv, opt)
            if fair > 0:
                fairness_ratio = premium / fair
                fairness_ok = fairness_ratio <= fairness_threshold
        breakeven = strike + premium if opt == "CE" else strike - premium
        pts_needed = abs(breakeven - S)
        breakeven_ok = hours_left > 0 and pts_needed <= achievable_pts

    conditions = [
        {"name": f"Premium fairness (prem/fair <= {fairness_threshold:.2f})",
         "passed": fairness_ok,
         "detail": f"{fairness_ratio:.2f}" if fairness_ratio is not None else "n/a"},
        {"name": "Volume vs chain average",
         "passed": volume_ok,
         "detail": f"{volume:.0f} vs avg {avg_volume:.0f}"},
        {"name": "Breakeven achievable (remaining hours)",
         "passed": breakeven_ok,
         "detail": (f"needs {pts_needed:.0f} pts vs {achievable_pts:.0f} pts "
                    f"achievable in {hours_left:.2f}h left")
                   if pts_needed is not None else "n/a"},
    ]
    passed_count = sum(1 for c in conditions if c["passed"])
    qualified = (passed_count == len(conditions) and premium is not None
                and premium > 0 and iv is not None)

    return {
        "strike": strike, "opt": opt, "premium": premium, "iv": iv,
        "fairness_ratio": fairness_ratio, "volume": volume,
        "pts_needed": pts_needed, "achievable_pts": achievable_pts,
        "conditions": conditions, "passed_count": passed_count,
        "qualified": qualified,
    }


def estimate_premium_at_trigger(trigger_S, K, T_days, iv, opt, r=None):
    """Reprice with Black-Scholes at the trigger spot level, same IV,
    minus TRIGGER_DECAY_HOURS of time decay."""
    r = oa.RISK_FREE_RATE if r is None else r
    T_new_days = max(T_days - TRIGGER_DECAY_HOURS / 24.0, 0.0)
    return oa.bs_price(trigger_S, K, T_new_days / 365, r, iv, opt)


def rank_qualified(qualified_results, rv):
    """Ranked by conditions passed (always equal among qualified strikes,
    since qualification requires all 3), then IV/realized-vol ratio
    ascending (cheaper vs realized vol wins), then smallest breakeven
    distance. Top 3."""
    def key(res):
        iv_rv = (res["iv"] / rv) if (res["iv"] and rv) else float("inf")
        pts = res["pts_needed"] if res["pts_needed"] is not None else float("inf")
        return (-res["passed_count"], iv_rv, pts)
    return sorted(qualified_results, key=key)[:3]


def build_trade_card(strike_result, trigger, opt, capital, T_days):
    est_premium = estimate_premium_at_trigger(trigger, strike_result["strike"],
                                              T_days, strike_result["iv"], opt)
    lo = est_premium * (1 - CARD_ENTRY_BAND_PCT)
    hi = est_premium * (1 + CARD_ENTRY_BAND_PCT)
    sl = est_premium * (1 - SL_PCT)
    t1 = est_premium * (1 + T1_PCT)
    t2 = est_premium * (1 + T2_PCT)
    risk_per_lot = (est_premium - sl) * LOT_SIZE
    max_risk = capital * RISK_PCT
    lots = int(max_risk // risk_per_lot) if risk_per_lot > 0 else 0
    return {
        "strike": strike_result["strike"], "opt": opt, "trigger": trigger,
        "est_premium": est_premium, "lo": lo, "hi": hi, "sl": sl,
        "t1": t1, "t2": t2, "risk_per_lot": risk_per_lot, "lots": lots,
        "max_risk": max_risk,
    }


def build_band_lookup(rows):
    """Chain-mode leg lookup: (strike, opt) -> leg dict, via
    option_analyzer.get_leg()."""
    def lookup(strike, opt):
        return oa.get_leg(rows, strike, opt)
    return lookup


def scan_band(symbol, S, T_days, strike_below, strike_above, legs_near,
             avg_volume, capital, now, rv, rv_source, vix, max_pain,
             band_lookup, band_below_strikes, band_above_strikes):
    """Core scan: market-wide conditions once, then per-strike
    qualification over whichever band (5 above or 5 below, or a
    single-strike band in manual mode) matches the resulting direction.
    Returns a result dict for print_scan()/log_scan()."""
    market = evaluate_market_wide(symbol, S, T_days, strike_below,
                                  strike_above, legs_near, now, rv,
                                  rv_source, vix, max_pain)
    daily_range = oa.AVG_DAILY_RANGE.get(symbol, 200)
    hours_left = hours_left_in_session(market["now"])
    direction = market["direction"]

    if direction == "bullish":
        side_opt, side_strikes = "CE", band_above_strikes
    elif direction == "bearish":
        side_opt, side_strikes = "PE", band_below_strikes
    else:
        # No clean direction; still scan a nominal side purely for display.
        side_opt, side_strikes = "CE", band_above_strikes

    strike_results = []
    for k in side_strikes:
        leg = band_lookup(k, side_opt) or {}
        buildup = classify_oi_buildup(leg.get("pChange") or 0,
                                      leg.get("changeinOpenInterest") or 0)
        res = evaluate_strike_conditions(k, side_opt, leg, S, T_days,
                                         avg_volume, market["fairness_threshold"],
                                         hours_left, daily_range)
        res["buildup"] = buildup
        strike_results.append(res)

    qualified = [r for r in strike_results if r["qualified"]]
    ranked = rank_qualified(qualified, market["rv"]) if qualified else []
    trigger = (strike_above if direction == "bullish" else
              strike_below if direction == "bearish" else None)

    if market["hard_no_trade"]:
        decision = "NO TRADE"
        reason = f"Time-of-day filter active: {market['time_label']}."
        cards = []
    elif not market["all_aligned"]:
        failing = "; ".join(f"{c['name']}: {c['detail']}"
                            for c in market["conditions"] if not c["passed"])
        decision = "WAIT"
        reason = f"Market-wide conditions not aligned - {failing}"
        cards = []
    elif not ranked:
        decision = "WAIT"
        reason = (f"Market-wide aligned {direction} but no strike in the "
                  "band met the per-strike bar (fairness/volume/breakeven).")
        cards = []
    else:
        decision = "BUY CE" if direction == "bullish" else "BUY PE"
        reason = (f"Market-wide aligned {direction}; {len(ranked)} strike(s) "
                  "qualified - see conditional trade card(s) below.")
        cards = [build_trade_card(r, trigger, side_opt, capital, T_days)
                for r in ranked]

    return {
        "market": market, "side_opt": side_opt, "strike_results": strike_results,
        "qualified": qualified, "ranked": ranked, "trigger": trigger,
        "decision": decision, "reason": reason, "cards": cards, "capital": capital,
    }


def print_scan(result):
    m = result["market"]
    print("=" * 78)
    print("  DECISION ENGINE v1 - 5+5 STRIKE SCAN")
    print("  Alignment of current structure - not a prediction. Direction risk is yours.")
    print("=" * 78)
    print(f"  Symbol: {m['symbol']}   Spot: {m['S']}   Days to expiry: {m['T_days']}")
    print(f"  Nearest strikes: {m['strike_below']:.0f} (below) / "
          f"{m['strike_above']:.0f} (above)")
    print("-" * 78)
    b = m["buildups"]
    print("  OI BUILDUP at nearest strikes (drives direction):")
    print(f"    CE {m['strike_below']:.0f}: {b['CE_below']}   |   "
          f"CE {m['strike_above']:.0f}: {b['CE_above']}")
    print(f"    PE {m['strike_below']:.0f}: {b['PE_below']}   |   "
          f"PE {m['strike_above']:.0f}: {b['PE_above']}")
    print("-" * 78)
    if m["max_pain"] is not None:
        diff = m["S"] - m["max_pain"]
        dword = "above" if diff > 0 else "below" if diff < 0 else "at"
        print(f"  Max pain: {m['max_pain']:.0f} (spot is {abs(diff):.0f} "
              f"pts {dword} max pain)")
    if m["vix"] is not None:
        tightened = (" (premium-fairness threshold tightened to "
                    f"{FAIRNESS_THRESHOLD_HIGH_VIX:.2f})"
                    if m["fairness_threshold"] < FAIRNESS_THRESHOLD else "")
        print(f"  India VIX: {m['vix']:.2f}{tightened}")
    if m["is_event_day"]:
        print(f"  *** IV-CRUSH RISK: {m['event_label']} - IV can collapse "
              "independent of direction. ***")
    print("-" * 78)
    print("  MARKET-WIDE CONDITIONS (evaluated once):")
    for c in m["conditions"]:
        print(f"    [{'PASS' if c['passed'] else 'FAIL'}] {c['name']}  ({c['detail']})")
    unclear = "  (direction unclear)" if m["direction"] is None else ""
    print(f"  Market-wide alignment: {m['score']}/{m['total']} "
          f"conditions aligned{unclear}")
    if m["lunch_flag"]:
        print("  NOTE: 12:00-13:15 IST lull window - lower quality setup, "
              "size down or wait even if cards are printed below.")
    print("-" * 78)

    side_word = "above" if result["side_opt"] == "CE" else "below"
    print(f"  STRIKE SCAN ({result['side_opt']} {side_word} spot, "
          f"{len(result['strike_results'])} strike(s)):")
    header = (f"  {'Strike':<9}{'Premium':<10}{'IV%':<7}{'Buildup':<16}"
              f"{'Fair':<6}{'Vol':<6}{'BE':<6}{'QUALIFIED':<10}")
    print(header)
    print("  " + "-" * 74)
    for r in result["strike_results"]:
        iv_str = f"{r['iv'] * 100:.1f}" if r["iv"] else "n/a"
        prem_str = f"{r['premium']:.1f}" if r["premium"] else "n/a"
        fairness_pass, volume_pass, be_pass = (c["passed"] for c in r["conditions"])
        print(f"  {r['strike']:<9.0f}{prem_str:<10}{iv_str:<7}{r['buildup']:<16}"
              f"{'PASS' if fairness_pass else 'FAIL':<6}"
              f"{'PASS' if volume_pass else 'FAIL':<6}"
              f"{'PASS' if be_pass else 'FAIL':<6}"
              f"{'YES' if r['qualified'] else 'no':<10}")
    print("  " + "-" * 74)
    print(f"  DECISION: {result['decision']}")
    print(f"  Reason: {result['reason']}")

    if result["cards"]:
        print("=" * 78)
        for i, card in enumerate(result["cards"], 1):
            print(f"  CARD #{i}: {m['symbol']} {card['strike']:.0f} {card['opt']}")
            print("  Conditional plan, not a prediction - valid only if the "
                  "trigger level is hit while conditions still hold.")
            print(f"  IF {m['symbol']} reaches {card['trigger']:.0f}: buy "
                  f"{card['strike']:.0f} {card['opt']} around "
                  f"Rs {card['lo']:.1f}-{card['hi']:.1f}, SL Rs {card['sl']:.1f}, "
                  f"target Rs {card['t1']:.1f} (T2 Rs {card['t2']:.1f})")
            if card["lots"] > 0:
                print(f"  Lots for Rs {result['capital']:.0f} capital at 1% risk: "
                      f"{card['lots']} lot(s) (lot size {LOT_SIZE}), risking "
                      f"Rs {card['risk_per_lot'] * card['lots']:.0f}")
            else:
                print(f"  Position size: 0 lots - risk per lot "
                      f"(Rs {card['risk_per_lot']:.0f}) exceeds 1% of capital "
                      f"(Rs {card['max_risk']:.0f}).")
            if i < len(result["cards"]):
                print("-" * 78)
    print("=" * 78)


def log_scan(result, path=LOG_FILE):
    m = result["market"]
    qualified_strikes = ";".join(f"{r['strike']:.0f}{result['side_opt']}"
                                 for r in result["qualified"])
    row = {
        "timestamp": m["now"].isoformat(timespec="seconds"),
        "symbol": m["symbol"],
        "spot": m["S"],
        "strike_below": m["strike_below"],
        "strike_above": m["strike_above"],
        "ce_below_buildup": m["buildups"]["CE_below"],
        "ce_above_buildup": m["buildups"]["CE_above"],
        "pe_below_buildup": m["buildups"]["PE_below"],
        "pe_above_buildup": m["buildups"]["PE_above"],
        "pcr": f"{m['pcr']:.3f}" if m["pcr"] is not None else "",
        "oi_bias": m["oi_bias"],
        "fairness_threshold": m["fairness_threshold"],
        "rv": f"{m['rv']:.4f}" if m["rv"] is not None else "",
        "rv_source": m["rv_source"] or "",
        "iv_rv_ratio": (f"{m['iv_rv_ratio']:.2f}"
                       if m["iv_rv_ratio"] is not None else ""),
        "vix": f"{m['vix']:.2f}" if m["vix"] is not None else "",
        "max_pain": m["max_pain"] if m["max_pain"] is not None else "",
        "event_flag": m["is_event_day"],
        "event_label": m["event_label"] or "",
        "market_score": m["score"],
        "market_total": m["total"],
        "side_opt": result["side_opt"],
        "strikes_scanned": len(result["strike_results"]),
        "num_qualified": len(result["qualified"]),
        "qualified_strikes": qualified_strikes,
        "cards_issued": len(result["cards"]),
        "decision": result["decision"],
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
    print("=" * 78)
    print("  DECISION ENGINE v1 - 5+5 STRIKE SCAN")
    print("  Alignment of current structure - not a prediction. Direction risk is yours.")
    print("=" * 78)
    symbol = (input("Index (NIFTY / BANKNIFTY) [BANKNIFTY]: ").strip().upper()
              or "BANKNIFTY")
    capital_in = input("Trading capital (Rs) for position sizing [100000]: ").strip()
    capital = float(capital_in) if capital_in else 100000.0

    now = datetime.now()
    rows, S, expiry = fetch_chain(symbol)
    max_pain = None
    if rows is None:
        print("\n  5+5 strike scan needs a live NSE fetch (it has to see many")
        print("  strikes at once) - manual entry can't substitute for that.")
        print("  Falling back to a reduced single-nearest-strike assessment.\n")
        S, T_days, strike_below, strike_above, legs_near, avg_volume = manual_entry()
        band_below_strikes, band_above_strikes = [strike_below], [strike_above]

        def band_lookup(strike, opt):
            slot = "below" if strike == strike_below else "above"
            return legs_near.get((opt, slot), {})
    else:
        parsed = parse_chain_to_legs(rows, S)
        if parsed is None:
            print("Spot price is outside the strikes available in the chain.")
            return
        strike_below, strike_above, legs_near, avg_volume = parsed
        T_days = oa.days_between(expiry)
        max_pain = compute_max_pain(rows)
        strikes = all_strikes(rows)
        band_below_strikes, band_above_strikes = strike_band(strikes, S)
        band_lookup = build_band_lookup(rows)
        print(f"\nFetched: spot={S}, expiry={expiry}, days to expiry={T_days}")

    rv, rv_source = fetch_realized_vol(symbol)
    vix = fetch_india_vix()

    result = scan_band(symbol, S, T_days, strike_below, strike_above,
                       legs_near, avg_volume, capital, now, rv, rv_source,
                       vix, max_pain, band_lookup, band_below_strikes,
                       band_above_strikes)
    print_scan(result)
    log_scan(result)
    log_iv_history(now, result["market"]["atm_iv"])


if __name__ == "__main__":
    main()
