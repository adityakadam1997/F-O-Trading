# F-O-Trading

## What this is

A single-file Python CLI (`option_analyzer.py`) that helps a discretionary
F&O (futures & options) buyer decide whether a NIFTY/BANKNIFTY option
purchase is a reasonable trade. It is decision support, not a trading
signal or advice — it does not place orders and has no broker integration.

The tool pulls spot price, last traded premium, and IV from NSE's public
option-chain JSON endpoint, prices the option with Black-Scholes to sanity
check the premium, runs it through four pass/fail checks (the "verdict"),
and estimates how long it would take the premium to hit a target gain
under an assumed daily point-move.

There are two ways to run it:

- **Single strike** (`run_single_strike`): analyze one strike/expiry you
  already have in mind. Falls back to manual entry (typed spot / premium /
  IV / days-to-expiry) if the NSE fetch fails.
- **Chain scanner** (`run_chain_scanner` / `scan_chain`): fetch the entire
  option chain for a chosen expiry and CE/PE side, run every strike with a
  tradeable premium through the same verdict logic, and print the top N
  (default 5) ranked by score. There is no manual equivalent for this mode
  — scanning needs to see every strike at once, so if the live fetch fails
  it reports the error and drops into single-strike manual mode instead.

## Requirements

```
pip install requests
python option_analyzer.py
```

`requests` is only needed for the live NSE fetch; manual single-strike
mode works without it (the import failure is caught and reported).

## NSE fetch reliability

`fetch_nse_chain()` hits `https://www.nseindia.com/api/option-chain-indices`
after first loading the homepage to pick up session cookies (NSE requires
this handshake and a browser-like User-Agent). This endpoint is known to be
flaky from scripts: it blocks many datacenter/cloud IPs outright, rate-limits
aggressively, and sometimes returns an anti-bot HTML page instead of JSON.
`fetch_nse_chain()` distinguishes these failure modes (missing `requests`,
connection/DNS block, timeout, HTTP error, non-JSON response) and prints a
specific reason for each rather than failing silently, then returns `None`
so callers can fall back gracefully.

This environment cannot reach NSE at all (outbound network is restricted),
so the live path is untested here by design — only the failure/fallback
paths and the pure-math logic (Black-Scholes, verdict checks, chain
ranking against a synthetic in-memory chain) have been exercised.

## The 4 verdict checks

Computed in `evaluate_strike()` for every strike, whether in single-strike
or scan mode. `passed` (0–4) is the **verdict score**; `verdict` is
GREEN (4/4), YELLOW (2–3/4), or RED (0–1/4).

1. **Price fairness** — `premium / BS_fair_value <= 1.10`.
   Is the market charging at most a ~10% premium over the Black-Scholes
   fair value for the given IV? Flags strikes trading rich (e.g. ahead of
   an event, or thin liquidity).

2. **Theta burden** — `abs(theta) / premium <= 0.04` (4%/day).
   How much of the position's value bleeds away per day just from time
   decay, independent of index movement. High theta burden means the
   trade needs to move quickly to overcome daily decay.

3. **Breakeven feasibility** — `points_needed_to_breakeven <= AVG_DAILY_RANGE[symbol] * planned_holding_days`.
   Breakeven is `strike + premium` (CE) or `strike - premium` (PE). This
   check asks whether the index can plausibly cover that distance, at its
   recent average daily range, within the holding period you actually
   plan to use.

4. **Time buffer** — `days_to_expiry >= 3 * planned_holding_days`.
   Guards against holding into the last few days before expiry, where
   theta decay accelerates sharply (gamma/theta risk near expiry).

Both single-strike and chain-scan modes share this same function, so the
scanner is just "run the 4 checks across every strike in the chain and
rank the results" — see `scan_chain()`.

### Chain scanner ranking

`scan_chain()` sorts candidates by:

1. `passed` descending (more checks passed ranks higher) — the primary
   verdict score.
2. `premium / fair_value` ascending (tie-break: cheaper relative to fair
   value wins).
3. `eta` ascending, `None` (never reaches target before expiry) last
   (tie-break: faster estimated time-to-target wins).

Each result row shows strike, premium, IV%, score (`passed`/4), verdict,
ETA in days to the +60% target, and points needed to breakeven. The full
single-strike breakdown (fair value, greeks, checks, probability of
touching the target level) is printed for the #1 ranked strike.

## Key constants (`option_analyzer.py` top of file)

- `RISK_FREE_RATE` — annual; update from FBIL/MIBOR periodically.
- `TARGET_PCT` / `STOP_PCT` — +60% target / -30% stop-loss, used to define
  "target" premium for the ETA and touch-probability estimates.
- `AVG_DAILY_RANGE` — assumed average daily point move per index; drives
  both the breakeven-feasibility check and the ETA estimate. Update from
  recent realized behavior; unlisted symbols default to 200.
- `PLANNED_HOLDING_DAYS` — default holding period if the user doesn't
  override it interactively.
- `CHAIN_SCAN_TOP_N` — default number of strikes shown by the scanner (5).

## Known simplifications / caveats

- IV is assumed constant going forward; the tool does not model IV crush
  around events (results, RBI policy, budget, etc.), which can hurt option
  buyers even when the index moves the "right" direction.
- `expected_days_to_target` and `prob_touch_target` assume a constant daily
  point move / GBM zero-drift approximation — a simplification, not a
  forecast.
- The chain scanner only considers one side (CE or PE) per run; it doesn't
  cross-rank CE vs PE in a single pass.
- No persistence, no backtesting, no order placement — purely an
  interactive, single-session CLI.

## Roadmap

Ideas for future iterations (nothing here is committed or in progress
unless a task explicitly says so):

- Cross-rank CE and PE together in one chain scan instead of requiring a
  side to be picked upfront.
- Persist/export scan results (CSV/JSON) for later comparison across days.
- Pull recent realized daily range automatically instead of relying on the
  hardcoded `AVG_DAILY_RANGE` table.
- Model IV crush around known event dates (results calendar, RBI policy)
  instead of assuming constant IV.
- Add a lightweight retry/backoff around the NSE fetch for transient
  rate-limiting, separate from the hard failure modes already handled.
- Support stock F&O (not just NIFTY/BANKNIFTY indices).
