"""
F&O OPTION BUYER ANALYZER (CE/PE) - Python version
---------------------------------------------------
You enter: index (NIFTY/BANKNIFTY), strike, CE or PE.
The tool fetches spot, premium, IV from the NSE option chain,
runs the 4 verdict checks, and estimates:
  - expected days to hit the +60% target (at avg daily speed)
  - probability of hitting the target before expiry

Two modes:
  1) Single strike  - analyze one strike/expiry you already have in mind.
  2) Chain scanner  - fetch the whole option chain for an expiry and rank
                       the top 5 strikes by verdict score.

Run:  python option_analyzer.py
Needs: pip install requests

If NSE fetch fails (it sometimes blocks scripts), single-strike mode asks
you to type spot / premium / IV manually and everything else still works.
Chain scanner mode needs a live fetch (it has to see every strike), so if
NSE is unreachable it reports the error and drops you into single-strike
manual mode instead.
"""

import math
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
CHAIN_SCAN_TOP_N = 5
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


def fetch_nse_chain(symbol):
    """Fetch NSE option chain. Returns parsed JSON, or None on any failure.

    NSE's option-chain endpoint requires cookies from a prior homepage hit
    and a browser-like User-Agent; it also blocks many cloud/datacenter IPs
    outright, so failures here are expected in sandboxed or headless
    environments. Every failure path prints a specific reason so it's clear
    whether the problem is a missing dependency, a network block, or a bad
    response, rather than failing silently.
    """
    try:
        import requests
    except ImportError:
        print("  [!] 'requests' is not installed. Run: pip install requests")
        return None

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36"),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/option-chain",
    }
    try:
        s = requests.Session()
        s.headers.update(headers)
        # first hit homepage to collect cookies
        s.get("https://www.nseindia.com", timeout=10)
        url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
        r = s.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError as e:
        print(f"  [!] NSE unreachable (network/DNS blocked from this "
              f"environment): {e}")
    except requests.exceptions.Timeout as e:
        print(f"  [!] NSE request timed out: {e}")
    except requests.exceptions.HTTPError as e:
        print(f"  [!] NSE returned an HTTP error (likely bot-blocked): {e}")
    except ValueError as e:
        print(f"  [!] NSE response was not valid JSON (likely an "
              f"anti-bot HTML page): {e}")
    except Exception as e:
        print(f"  [!] NSE fetch failed: {e}")
    return None


def pick_expiry(chain):
    expiries = chain["records"]["expiryDates"]
    print("\nAvailable expiries:")
    for i, e in enumerate(expiries[:6]):
        print(f"  {i + 1}. {e}")
    n = input("Pick expiry number [1]: ").strip() or "1"
    return expiries[int(n) - 1]


def get_leg(chain, expiry, strike, opt):
    key = "CE" if opt == "CE" else "PE"
    for row in chain["records"]["data"]:
        if row.get("expiryDate") == expiry and row.get("strikePrice") == strike:
            leg = row.get(key)
            if leg:
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


def resolve_iv(premium, S, K, T, r, opt, iv_pct):
    """Prefer NSE chain IV; else back out from premium. Returns (iv, note)."""
    if iv_pct and iv_pct > 0:
        return iv_pct / 100, None
    iv = implied_vol(premium, S, K, T, r, opt)
    if iv is None:
        return None, None
    return iv, f"Backed-out IV from premium: {iv * 100:.1f}%"


def evaluate_strike(S, strike, opt, premium, iv, T_days, r, hold, daily_range):
    """Run the 4 verdict checks + time-to-target for one strike. Returns a
    dict with everything needed to print or rank the result."""
    T = T_days / 365
    fair = bs_price(S, strike, T, r, iv, opt)
    delta, theta, vega = bs_greeks(S, strike, T, r, iv, opt)
    breakeven = strike + premium if opt == "CE" else strike - premium
    pts_needed = (breakeven - S) if opt == "CE" else (S - breakeven)
    target = premium * (1 + TARGET_PCT)
    stop = premium * (1 - STOP_PCT)
    fairness_ratio = premium / fair if fair > 0 else float("inf")

    checks = [
        ("Price fairness (prem/fair <= 1.10)", fairness_ratio <= 1.10,
         f"{fairness_ratio:.2f}" if fair > 0 else "n/a (fair value is 0)"),
        ("Theta burden (<= 4%/day)",
         premium > 0 and abs(theta) / premium <= 0.04,
         f"{abs(theta) / premium * 100:.1f}%/day" if premium > 0 else "n/a"),
        ("Breakeven feasible in holding days", pts_needed <= daily_range * hold,
         f"needs {pts_needed:.0f} pts vs {daily_range * hold} achievable"),
        ("Time buffer (expiry >= 3x holding)", T_days >= 3 * hold,
         f"{T_days}d vs {3 * hold}d needed"),
    ]
    passed = sum(1 for _, ok, _ in checks if ok)
    verdict = ("GREEN - FAVOURABLE" if passed == 4 else
               "YELLOW - MARGINAL" if passed >= 2 else "RED - AVOID")

    eta = expected_days_to_target(S, strike, T_days, r, iv, opt, premium,
                                  target, daily_range)
    p_touch, barrier = prob_touch_target(S, strike, T_days, r, iv, opt, target)

    return {
        "strike": strike, "opt": opt, "premium": premium, "iv": iv,
        "fair": fair, "delta": delta, "theta": theta, "vega": vega,
        "breakeven": breakeven, "pts_needed": pts_needed,
        "target": target, "stop": stop, "fairness_ratio": fairness_ratio,
        "checks": checks, "passed": passed, "verdict": verdict,
        "eta": eta, "p_touch": p_touch, "barrier": barrier,
    }


def print_single_result(symbol, S, T_days, hold, daily_range, result):
    strike, opt = result["strike"], result["opt"]
    premium = result["premium"]
    print("\n" + "=" * 60)
    print(f"  {symbol} {strike} {opt} | premium Rs {premium}")
    print("=" * 60)
    print(f"  Fair value (BS)     : Rs {result['fair']:.1f}")
    print(f"  Delta / Theta / Vega: {result['delta']:.2f} / "
          f"{result['theta']:.1f}/day / {result['vega']:.1f}")
    print(f"  Breakeven at expiry : {result['breakeven']:.0f} "
          f"({result['pts_needed']:.0f} pts away)")
    print(f"  Target / Stop-loss  : Rs {result['target']:.1f} / "
          f"Rs {result['stop']:.1f}")
    print("-" * 60)
    for name, ok, detail in result["checks"]:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  ({detail})")
    print(f"\n  VERDICT: {result['verdict']}  ({result['passed']}/4 checks)")
    print("-" * 60)
    print("  TIME-TO-TARGET ESTIMATE (not a guarantee):")
    eta = result["eta"]
    if eta:
        tag = "" if eta <= hold else "  <-- LONGER than your holding period!"
        print(f"  If index moves ~{daily_range} pts/day your way: "
              f"target in ~{eta} day(s){tag}")
    else:
        print(f"  Even at {daily_range} pts/day in your favour, premium never "
              f"reaches the target before expiry - target unrealistic.")
    print(f"  Index level needed  : ~{result['barrier']:.0f}")
    print(f"  Probability of touching that level before expiry: "
          f"{result['p_touch'] * 100:.0f}%")
    print("=" * 60)
    print("  Assumes IV stays constant. IV crush after events will lower")
    print("  actual premiums. This is decision support, not advice.")


def run_single_strike():
    symbol = (input("Index (NIFTY / BANKNIFTY) [BANKNIFTY]: ").strip().upper()
              or "BANKNIFTY")
    strike = int(input("Strike: ").strip())
    opt = input("CE or PE: ").strip().upper()
    if opt not in ("CE", "PE"):
        print("Type CE or PE only."); return
    hold = int(input(f"Planned holding days [{PLANNED_HOLDING_DAYS}]: ")
               .strip() or PLANNED_HOLDING_DAYS)
    daily_range = AVG_DAILY_RANGE.get(symbol, 200)

    chain = fetch_nse_chain(symbol)
    if chain:
        expiry = pick_expiry(chain)
        leg = get_leg(chain, expiry, strike, opt)
        if leg is None:
            print("Strike not found in chain for that expiry."); return
        S = chain["records"]["underlyingValue"]
        premium = leg["lastPrice"]
        iv_pct = leg.get("impliedVolatility") or 0
        T_days = days_between(expiry)
        print(f"\nFetched: spot={S}, premium={premium}, "
              f"NSE IV={iv_pct}%, days to expiry={T_days}")
    else:
        print("  Switching to manual entry.")
        S = float(input("Spot price: "))
        premium = float(input("Market premium (Rs): "))
        iv_pct = float(input("IV %% from option chain (0 if unknown): "))
        T_days = int(input("Days to expiry: "))

    r = RISK_FREE_RATE
    T = T_days / 365
    iv, note = resolve_iv(premium, S, strike, T, r, opt, iv_pct)
    if iv is None:
        print("Could not solve IV from premium - check inputs."); return
    if note:
        print(note)

    result = evaluate_strike(S, strike, opt, premium, iv, T_days, r, hold,
                             daily_range)
    print_single_result(symbol, S, T_days, hold, daily_range, result)


def scan_chain(chain, expiry, opt, hold, daily_range, r=RISK_FREE_RATE,
               top_n=CHAIN_SCAN_TOP_N):
    """Evaluate every strike in `chain` for `expiry`/`opt`, rank by verdict
    score, and return (spot, days_to_expiry, top_n results, n_evaluated).

    Ranking: most checks passed first; ties broken by cheaper-vs-fair
    (lower premium/fair ratio), then by faster time-to-target (None last).
    """
    S = chain["records"]["underlyingValue"]
    T_days = days_between(expiry)
    T = T_days / 365
    results = []

    for row in chain["records"]["data"]:
        if row.get("expiryDate") != expiry:
            continue
        strike = row.get("strikePrice")
        leg = row.get(opt)
        if not leg or strike is None:
            continue
        premium = leg.get("lastPrice")
        if not premium or premium <= 0:
            continue
        iv_pct = leg.get("impliedVolatility") or 0
        iv, _ = resolve_iv(premium, S, strike, T, r, opt, iv_pct)
        if iv is None:
            continue
        results.append(evaluate_strike(S, strike, opt, premium, iv, T_days,
                                       r, hold, daily_range))

    def sort_key(res):
        eta_key = res["eta"] if res["eta"] is not None else 10 ** 6
        return (-res["passed"], res["fairness_ratio"], eta_key)

    results.sort(key=sort_key)
    return S, T_days, results[:top_n], len(results)


def print_chain_scan(symbol, expiry, opt, S, T_days, hold, daily_range,
                     results, n_evaluated):
    print("\n" + "=" * 78)
    print(f"  CHAIN SCAN: {symbol} {opt} | expiry {expiry} | spot {S}")
    print(f"  Evaluated {n_evaluated} strike(s) with tradeable premium; "
          f"showing top {len(results)} by verdict score.")
    print("=" * 78)
    if not results:
        print("  No strikes had usable premium/IV data for this expiry.")
        print("=" * 78)
        return

    header = (f"  {'#':<3}{'Strike':<9}{'Premium':<10}{'IV%':<8}"
              f"{'Score':<8}{'Verdict':<12}{'ETA(d)':<8}{'Pts needed':<12}")
    print(header)
    print("  " + "-" * 74)
    for i, res in enumerate(results, 1):
        eta_str = str(res["eta"]) if res["eta"] is not None else "never"
        verdict_short = res["verdict"].split(" - ")[0]
        print(f"  {i:<3}{res['strike']:<9.0f}{res['premium']:<10.1f}"
              f"{res['iv'] * 100:<8.1f}{res['passed']}/4{'':<5}"
              f"{verdict_short:<12}{eta_str:<8}{res['pts_needed']:<12.0f}")
    print("  " + "-" * 74)
    print("  Score = verdict checks passed (price fairness, theta burden,")
    print("  breakeven feasibility, time buffer). ETA = days to +60% target")
    print(f"  at {daily_range} pts/day in your favour, given a {hold}-day hold.")
    print("=" * 78)
    print("  Full breakdown of the #1 ranked strike:")
    print_single_result(symbol, S, T_days, hold, daily_range, results[0])


def run_chain_scanner():
    symbol = (input("Index (NIFTY / BANKNIFTY) [BANKNIFTY]: ").strip().upper()
              or "BANKNIFTY")
    opt = input("CE or PE: ").strip().upper()
    if opt not in ("CE", "PE"):
        print("Type CE or PE only."); return
    hold = int(input(f"Planned holding days [{PLANNED_HOLDING_DAYS}]: ")
               .strip() or PLANNED_HOLDING_DAYS)
    daily_range = AVG_DAILY_RANGE.get(symbol, 200)

    chain = fetch_nse_chain(symbol)
    if chain is None:
        print("\n  Chain scanning needs a live NSE fetch (it has to see every")
        print("  strike at once) - manual entry can't substitute for that.")
        print("  Falling back to single-strike manual mode instead.\n")
        run_single_strike()
        return

    expiry = pick_expiry(chain)
    n_in = input(f"How many top strikes to show [{CHAIN_SCAN_TOP_N}]: ").strip()
    top_n = int(n_in) if n_in else CHAIN_SCAN_TOP_N

    S, T_days, results, n_evaluated = scan_chain(chain, expiry, opt, hold,
                                                 daily_range, RISK_FREE_RATE,
                                                 top_n)
    print_chain_scan(symbol, expiry, opt, S, T_days, hold, daily_range,
                     results, n_evaluated)


def main():
    print("=" * 60)
    print("F&O OPTION BUYER ANALYZER - live version")
    print("=" * 60)
    mode = input("Mode: 1) Analyze single strike  2) Scan full chain "
                "[1]: ").strip() or "1"
    if mode == "2":
        run_chain_scanner()
    else:
        run_single_strike()


if __name__ == "__main__":
    main()
