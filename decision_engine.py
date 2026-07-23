"""
DECISION ENGINE v1 - BankNifty/Nifty options, right now
--------------------------------------------------------
Answers exactly one question: right now, on the two strikes nearest to
spot, is this BUY CE, BUY PE, WAIT, or NO TRADE?

This is NOT a prediction tool. It reports whether a fixed set of
objective, observable conditions currently point the same way. When they
don't - which is most of the time - the honest answer is WAIT. There is
no confidence score, no probability, no expected holding time anywhere
in this tool; only "N/6 conditions aligned" plus the actual value behind
each one, so you can see for yourself what's missing.

Flow:
  1. Fetch the option chain for the nearest (or chosen) expiry, reusing
     option_analyzer.py's NSE session/fetch/parsing helpers.
  2. Find the nearest strike below and above spot; analyze only those
     two strikes, CE and PE both (4 contracts).
  3. Classify each leg's OI buildup from today's price change + OI change.
  4. Evaluate 6 objective conditions (OI signal, PCR, volume vs chain
     average, premium fairness vs Black-Scholes, risk-reward/breakeven
     achievable in the hours left today, time-of-day filter).
  5. BUY only when all 6 align in one direction. Otherwise WAIT (some
     conditions conflict) or NO TRADE (volume below average, or a hard
     no-trade time window is active).

Falls back to manual entry (typed spot/strikes/OI/volume/premium per
leg) if the NSE fetch fails, same as option_analyzer.py. Every run is
logged to decisions_log.csv (gitignored) regardless of the decision.

Run:  python decision_engine.py
Needs: pip install requests (only for the live NSE fetch)
"""

import csv
import os
from datetime import datetime, time as dtime
from statistics import mean

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

LOG_FILE = "decisions_log.csv"
LOG_FIELDS = [
    "timestamp", "symbol", "spot", "strike_below", "strike_above",
    "ce_below_buildup", "ce_above_buildup", "pe_below_buildup",
    "pe_above_buildup", "pcr", "oi_bias", "volume_ok", "fairness_ratio",
    "pts_needed", "score", "total", "decision", "candidate_opt",
    "candidate_strike",
]
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
    avg_volume = mean(volumes) if volumes else 0
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
                      avg_volume, capital, now=None):
    """Run the 6 objective conditions on the 4 legs and return a decision
    dict. `now` is injectable so the time-of-day filter is testable."""
    now = now or datetime.now()

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
                fairness_ok = fairness_ratio <= 1.10
        breakeven = (candidate_strike + premium if candidate_opt == "CE"
                     else candidate_strike - premium)
        pts_needed = abs(breakeven - S)
        rr_ok = hours_left > 0 and pts_needed <= achievable_pts

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
        {"name": "Premium fairness (prem/fair <= 1.10)",
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
        reason = "All 6 conditions align bullish at the nearest strikes."
    elif score == total and direction == "bearish":
        decision = "BUY PE"
        reason = "All 6 conditions align bearish at the nearest strikes."
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
    print("  CONDITIONS:")
    for c in result["conditions"]:
        print(f"    [{'PASS' if c['passed'] else 'FAIL'}] {c['name']}  ({c['detail']})")
    print("-" * 70)
    print(f"  ALIGNMENT SCORE: {result['score']}/{result['total']} conditions aligned")
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
        "pts_needed": pts_detail,
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
    if rows is None:
        S, T_days, strike_below, strike_above, legs, avg_volume = manual_entry()
    else:
        parsed = parse_chain_to_legs(rows, S)
        if parsed is None:
            print("Spot price is outside the strikes available in the chain.")
            return
        strike_below, strike_above, legs, avg_volume = parsed
        T_days = oa.days_between(expiry)
        print(f"\nFetched: spot={S}, expiry={expiry}, days to expiry={T_days}")

    result = evaluate_decision(symbol, S, T_days, strike_below, strike_above,
                               legs, avg_volume, capital, now)
    print_decision(result)
    log_decision(result)


if __name__ == "__main__":
    main()
