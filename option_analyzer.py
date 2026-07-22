"""
F&O OPTION BUYER ANALYZER (CE/PE) - Python version
---------------------------------------------------
You enter: index (NIFTY/BANKNIFTY), strike, CE or PE.
The tool fetches spot, premium, IV from the NSE option chain,
runs the 4 verdict checks, and estimates:
  - expected days to hit the +60% target (at avg daily speed)
  - probability of hitting the target before expiry

Run:  python option_analyzer.py
Needs: pip install requests

If NSE fetch fails (it sometimes blocks scripts), the tool asks you
to type spot / premium / IV manually and everything else still works.
"""

import math
import json
from datetime import datetime

# ----------------- user-adjustable defaults -----------------
RISK_FREE_RATE = 0.0665     # annual; update from FBIL/MIBOR
TARGET_PCT = 0.60           # +60% target
STOP_PCT = 0.30             # -30% stop-loss
AVG_DAILY_RANGE = {         # points; update from recent behaviour
    "NIFTY": 150,
    "BANKNIFTY": 350,
}
PLANNED_HOLDING_DAYS = 3
# ------------------------------------------------------------


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(S, K, T, r, iv, opt):
    """Black-Scholes price. T in years. opt = 'CE' or 'PE'."""
    if T <= 0:
        return max(S - K, 0.0) if opt == "CE" else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + iv * iv / 2) * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    if opt == "CE":
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def bs_greeks(S, K, T, r, iv, opt):
    d1 = (math.log(S / K) + (r + iv * iv / 2) * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    delta = norm_cdf(d1) if opt == "CE" else norm_cdf(d1) - 1
    common = -(S * norm_pdf(d1) * iv) / (2 * math.sqrt(T))
    if opt == "CE":
        theta = (common - r * K * math.exp(-r * T) * norm_cdf(d2)) / 365
    else:
        theta = (common + r * K * math.exp(-r * T) * norm_cdf(-d2)) / 365
    vega = S * norm_pdf(d1) * math.sqrt(T) / 100
    return delta, theta, vega


def implied_vol(price, S, K, T, r, opt):
    """Bisection solve for IV from market premium."""
    lo, hi = 0.005, 3.0
    if not (bs_price(S, K, T, r, lo, opt) <= price <= bs_price(S, K, T, r, hi, opt)):
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if bs_price(S, K, T, r, mid, opt) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def _nse_session():
    import requests
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36"),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/option-chain",
    }
    s = requests.Session()
    s.headers.update(headers)
    s.get("https://www.nseindia.com", timeout=10)
    s.get("https://www.nseindia.com/option-chain", timeout=10)
    return s


def fetch_expiries(s, symbol):
    """Expiry list from NSE contract-info endpoint."""
    url = ("https://www.nseindia.com/api/option-chain-contract-info"
           f"?symbol={symbol}")
    r = s.get(url, timeout=10)
    r.raise_for_status()
    return r.json()["expiryDates"]


def fetch_chain_for_expiry(s, symbol, expiry):
    """Chain data from the v3 endpoint (expiry is required)."""
    from urllib.parse import quote
    url = ("https://www.nseindia.com/api/option-chain-v3"
           f"?type=Indices&symbol={symbol}&expiry={quote(expiry)}")
    r = s.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


def _find_rows(data):
    """Locate the list of strike rows in the response, wherever it lives."""
    for path in (("records", "data"), ("filtered", "data"), ("data",)):
        node = data
        ok = True
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                ok = False
                break
        if ok and isinstance(node, list) and node:
            return node
    return None


def _find_underlying(data, rows):
    for node in (data.get("records", {}), data):
        if isinstance(node, dict):
            for key in ("underlyingValue", "underlyingvalue"):
                if node.get(key):
                    return node[key]
    for row in rows or []:
        for side in ("CE", "PE"):
            leg = row.get(side)
            if isinstance(leg, dict) and leg.get("underlyingValue"):
                return leg["underlyingValue"]
    return None


def _debug_shape(data):
    print("  [debug] Top-level keys:", list(data.keys()))
    rows = _find_rows(data)
    if rows:
        print("  [debug] First row keys:", list(rows[0].keys()))
        for side in ("CE", "PE"):
            if isinstance(rows[0].get(side), dict):
                print(f"  [debug] {side} keys:",
                      list(rows[0][side].keys())[:15])
    else:
        import json as _json
        print("  [debug] Response snippet:", _json.dumps(data)[:400])
    print("  Paste the [debug] lines above to Claude to fix the parser.")


def pick_expiry(expiries):
    print("\nAvailable expiries:")
    for i, e in enumerate(expiries[:6]):
        print(f"  {i + 1}. {e}")
    n = input("Pick expiry number [1]: ").strip() or "1"
    return expiries[int(n) - 1]


def get_leg(rows, strike, opt):
    for row in rows:
        sp = row.get("strikePrice")
        try:
            sp = float(sp)
        except (TypeError, ValueError):
            continue
        if sp == float(strike):
            leg = row.get(opt)
            if isinstance(leg, dict):
                return leg
    return None


def days_between(expiry_str):
    exp = datetime.strptime(expiry_str, "%d-%b-%Y")
    return max((exp - datetime.now()).days, 1)


def expected_days_to_target(S, K, T_days, r, iv, opt, premium, target,
                            daily_range):
    """Smallest whole day d such that, if the index moves daily_range points
    per day in the favourable direction, BS premium >= target. None if it
    never gets there before expiry."""
    direction = 1 if opt == "CE" else -1
    for d in range(1, T_days):
        S_new = S + direction * daily_range * d
        T_new = (T_days - d) / 365
        if bs_price(S_new, K, T_new, r, iv, opt) >= target:
            return d
    return None


def prob_touch_target(S, K, T_days, r, iv, opt, target):
    """Probability the index touches the spot level at which the premium
    equals the target, at least once before expiry (GBM, zero drift approx).
    P(touch) ~ 2 * N(-|ln(B/S)| / (iv * sqrt(T)))."""
    # find barrier spot level B where premium(B, T/2 remaining) = target
    # (use half-life remaining as a middle-of-the-road time assumption)
    direction = 1 if opt == "CE" else -1
    lo, hi = S, S + direction * 0.5 * S
    T_mid = (T_days / 2) / 365
    for _ in range(200):
        mid = (lo + hi) / 2
        p = bs_price(mid, K, T_mid, r, iv, opt)
        if (p < target) == (direction == 1):
            lo = mid
        else:
            hi = mid
    barrier = (lo + hi) / 2
    T = T_days / 365
    x = abs(math.log(barrier / S)) / (iv * math.sqrt(T))
    return min(2 * norm_cdf(-x), 1.0), barrier


def main():
    print("=" * 60)
    print("F&O OPTION BUYER ANALYZER - live version")
    print("=" * 60)

    symbol = (input("Index (NIFTY / BANKNIFTY) [BANKNIFTY]: ").strip().upper()
              or "BANKNIFTY")
    strike = int(input("Strike: ").strip())
    opt = input("CE or PE: ").strip().upper()
    if opt not in ("CE", "PE"):
        print("Type CE or PE only."); return
    hold = int(input(f"Planned holding days [{PLANNED_HOLDING_DAYS}]: ")
               .strip() or PLANNED_HOLDING_DAYS)
    daily_range = AVG_DAILY_RANGE.get(symbol, 200)

    fetched = False
    try:
        s = _nse_session()
        expiries = fetch_expiries(s, symbol)
        expiry = pick_expiry(expiries)
        data = fetch_chain_for_expiry(s, symbol, expiry)
        rows = _find_rows(data)
        if not rows:
            print("  [!] Could not locate strike rows in NSE response.")
            _debug_shape(data)
            raise ValueError("unrecognized response shape")
        leg = get_leg(rows, strike, opt)
        if leg is None:
            print(f"  [!] Strike {strike} not found for {expiry}. "
                  "Check the strike exists for this expiry.")
            _debug_shape(data)
            raise ValueError("strike not found")
        S = _find_underlying(data, rows)
        premium = leg.get("lastPrice")
        iv_pct = leg.get("impliedVolatility") or 0
        if not S or not premium:
            print("  [!] Row found but missing price fields.")
            _debug_shape(data)
            raise ValueError("missing fields")
        T_days = days_between(expiry)
        print(f"\nFetched: spot={S}, premium={premium}, "
              f"NSE IV={iv_pct}%, days to expiry={T_days}")
        fetched = True
    except Exception as e:
        print(f"  [!] NSE fetch failed ({e}). Switching to manual entry.")
    if not fetched:
        S = float(input("Spot price: "))
        premium = float(input("Market premium (Rs): "))
        iv_pct = float(input("IV %% from option chain (0 if unknown): "))
        T_days = int(input("Days to expiry: "))

    r = RISK_FREE_RATE
    T = T_days / 365

    # IV: prefer NSE chain IV; else back out from premium
    if iv_pct and iv_pct > 0:
        iv = iv_pct / 100
    else:
        iv = implied_vol(premium, S, strike, T, r, opt)
        if iv is None:
            print("Could not solve IV from premium - check inputs."); return
        print(f"Backed-out IV from premium: {iv * 100:.1f}%")

    fair = bs_price(S, strike, T, r, iv, opt)
    delta, theta, vega = bs_greeks(S, strike, T, r, iv, opt)
    breakeven = strike + premium if opt == "CE" else strike - premium
    pts_needed = (breakeven - S) if opt == "CE" else (S - breakeven)
    target = premium * (1 + TARGET_PCT)
    stop = premium * (1 - STOP_PCT)

    # ---- 4 verdict checks ----
    checks = [
        ("Price fairness (prem/fair <= 1.10)", premium / fair <= 1.10,
         f"{premium / fair:.2f}"),
        ("Theta burden (<= 4%/day)", abs(theta) / premium <= 0.04,
         f"{abs(theta) / premium * 100:.1f}%/day"),
        ("Breakeven feasible in holding days", pts_needed <= daily_range * hold,
         f"needs {pts_needed:.0f} pts vs {daily_range * hold} achievable"),
        ("Time buffer (expiry >= 3x holding)", T_days >= 3 * hold,
         f"{T_days}d vs {3 * hold}d needed"),
    ]
    passed = sum(1 for _, ok, _ in checks if ok)
    verdict = ("GREEN - FAVOURABLE" if passed == 4 else
               "YELLOW - MARGINAL" if passed >= 2 else "RED - AVOID")

    # ---- time-to-target estimate ----
    eta = expected_days_to_target(S, strike, T_days, r, iv, opt,
                                  premium, target, daily_range)
    p_touch, barrier = prob_touch_target(S, strike, T_days, r, iv, opt, target)

    print("\n" + "=" * 60)
    print(f"  {symbol} {strike} {opt} | premium Rs {premium}")
    print("=" * 60)
    print(f"  Fair value (BS)     : Rs {fair:.1f}")
    print(f"  Delta / Theta / Vega: {delta:.2f} / {theta:.1f}/day / {vega:.1f}")
    print(f"  Breakeven at expiry : {breakeven:.0f} ({pts_needed:.0f} pts away)")
    print(f"  Target / Stop-loss  : Rs {target:.1f} / Rs {stop:.1f}")
    print("-" * 60)
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  ({detail})")
    print(f"\n  VERDICT: {verdict}  ({passed}/4 checks)")
    print("-" * 60)
    print("  TIME-TO-TARGET ESTIMATE (not a guarantee):")
    if eta:
        tag = "" if eta <= hold else "  <-- LONGER than your holding period!"
        print(f"  If index moves ~{daily_range} pts/day your way: "
              f"target in ~{eta} day(s){tag}")
    else:
        print(f"  Even at {daily_range} pts/day in your favour, premium never "
              f"reaches the target before expiry - target unrealistic.")
    print(f"  Index level needed  : ~{barrier:.0f}")
    print(f"  Probability of touching that level before expiry: "
          f"{p_touch * 100:.0f}%")
    print("=" * 60)
    print("  Assumes IV stays constant. IV crush after events will lower")
    print("  actual premiums. This is decision support, not advice.")


if __name__ == "__main__":
    main()
