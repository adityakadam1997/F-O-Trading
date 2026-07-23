# F-O-Trading

## What this is

Two single-file Python CLIs for a discretionary NIFTY/BANKNIFTY F&O
(futures & options) buyer. Both are decision support, not a trading
signal or advice — neither places orders, and neither has broker
integration.

- **`option_analyzer.py`** — is this specific strike/expiry a reasonable
  buy? Fetches spot, premium, and IV, prices it with Black-Scholes to
  sanity-check the premium, runs it through 4 pass/fail checks (the
  "verdict"), and estimates how long it would take the premium to hit a
  target gain under an assumed daily point-move.
- **`decision_engine.py`** — right now, on the two strikes nearest to
  spot, should I BUY CE, BUY PE, WAIT, or NO TRADE? See "Decision Engine
  v1" below. **Its default answer is WAIT** — a trade only comes out when
  every one of 6 objective conditions lines up; anything less is WAIT or
  NO TRADE, on purpose.

## Requirements

```
pip install requests
python option_analyzer.py     # single strike/expiry analysis
python decision_engine.py     # right-now BUY CE / BUY PE / WAIT / NO TRADE
```

`requests` is only needed for the live NSE fetch; manual entry mode works
without it in both tools (the fetch failure is caught and reported, then
falls back to typed input).

## NSE fetch

NSE's old `/api/option-chain-indices` endpoint is dead (404). Both tools
now go through `option_analyzer.py`'s two-step fetch:

1. `_nse_session()` — opens a `requests.Session`, sets a browser-like
   User-Agent, and hits `https://www.nseindia.com` and
   `https://www.nseindia.com/option-chain` first to pick up the cookies
   NSE requires before it'll serve the API (it rejects script-like
   requests without that handshake).
2. `fetch_expiries(session, symbol)` — `GET
   /api/option-chain-contract-info?symbol=SYMBOL` for the list of
   available expiry dates.
3. `fetch_chain_for_expiry(session, symbol, expiry)` — `GET
   /api/option-chain-v3?type=Indices&symbol=SYMBOL&expiry=DD-Mon-YYYY`
   for the chain of that one expiry. The `expiry` param is **required** —
   without it NSE returns `{}`.

Because NSE's exact response shape has drifted before and may again,
parsing is defensive rather than assuming one fixed path:

- `_find_rows(data)` looks for the strike-row list under
  `records.data`, `filtered.data`, or `data` (whichever exists).
- `_find_underlying(data, rows)` looks for `underlyingValue` (or
  lowercase `underlyingvalue`) at the top level, under `records`, or
  falls back to a leg's own `underlyingValue` field.
- `_debug_shape(data)` prints the actual top-level and row keys NSE
  returned when parsing fails, so a live shape mismatch can be diagnosed
  and fixed from the printed output rather than guessed at blind.

This endpoint is known to be flaky from scripts regardless of shape: it
blocks many datacenter/cloud IPs outright and rate-limits aggressively.
Both tools catch fetch/parse failures and fall back to manual entry
rather than crashing.

This environment cannot reach NSE at all (outbound network is
restricted), so the live fetch is untested here by design — it was
verified against a real NSE session on a separate machine. Only the
failure/fallback paths and the pure-math logic (Black-Scholes, verdict
checks, OI-buildup classification) have been exercised here, against
synthetic in-memory chains.

## The 4 verdict checks (`option_analyzer.py`)

Computed inline in `main()` for the one strike/expiry entered. `passed`
(0–4) is the **verdict score**; `verdict` is GREEN (4/4), YELLOW (2–3/4),
or RED (0–1/4).

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

## Key constants (`option_analyzer.py` top of file)

- `RISK_FREE_RATE` — annual; update from FBIL/MIBOR periodically.
- `TARGET_PCT` / `STOP_PCT` — +60% target / -30% stop-loss, used to define
  "target" premium for the ETA and touch-probability estimates.
- `AVG_DAILY_RANGE` — assumed average daily point move per index; drives
  both the breakeven-feasibility check and the ETA estimate. Update from
  recent realized behavior; unlisted symbols default to 200. Also reused
  by `decision_engine.py`'s hourly breakeven-achievability check.
- `PLANNED_HOLDING_DAYS` — default holding period if the user doesn't
  override it interactively.

## Known simplifications / caveats (`option_analyzer.py`)

- IV is assumed constant going forward; the tool does not model IV crush
  around events (results, RBI policy, budget, etc.), which can hurt option
  buyers even when the index moves the "right" direction.
- `expected_days_to_target` and `prob_touch_target` assume a constant daily
  point move / GBM zero-drift approximation — a simplification, not a
  forecast.
- Single strike/expiry per run — no chain-wide scan or ranking.
- No persistence, no backtesting, no order placement — purely an
  interactive, single-session CLI.

## Decision Engine v1 (`decision_engine.py`)

Answers exactly one question: **right now**, on the two strikes nearest
spot, is this BUY CE, BUY PE, WAIT, or NO TRADE?

### Philosophy: WAIT is the default answer

This tool reports whether a fixed set of observable conditions currently
line up — it does not predict, and it does not estimate confidence.
There is **no probability, no confidence percentage, and no expected
holding time** printed anywhere (`print_decision()` deliberately omits
them, per the honesty requirements below). A BUY only comes out when
*all 6* conditions align in the same direction; anything less is WAIT
(some conditions conflict) or NO TRADE (a hard blocker is active). In
practice most runs should say WAIT — that's the tool working as
intended, not a bug.

### Flow

1. Fetch the chain for the nearest (or chosen) expiry
   (`fetch_chain()`), reusing `option_analyzer.py`'s `_nse_session()`,
   `fetch_expiries()`, `pick_expiry()`, `fetch_chain_for_expiry()`,
   `_find_rows()`, and `_find_underlying()`.
2. Find the nearest strike below and above spot (`nearest_strikes()`)
   and pull all 4 legs — CE and PE at both strikes — via
   `option_analyzer.get_leg()` (`parse_chain_to_legs()`).
3. Classify each leg's OI buildup from today's price change + OI change
   (`classify_oi_buildup()`): price↑+OI↑ = long buildup, price↓+OI↑ =
   short buildup, price↑+OI↓ = short covering, price↓+OI↓ = long
   unwinding.
4. Evaluate 6 objective PASS/FAIL conditions (`evaluate_decision()`):
   1. **OI signal** — bullish if CE shows long buildup and PE shows
      short buildup at both strikes; bearish if reversed; anything else
      is mixed (`classify_oi_signal()`).
   2. **PCR** — PE OI / CE OI across both strikes; >1.2 bullish, <0.8
      bearish, must match the OI signal's direction to pass
      (`pcr_direction()`).
   3. **Volume** — the candidate leg's traded volume must exceed the
      average volume across every leg in the fetched chain.
   4. **Premium fairness** — reuses `option_analyzer.bs_price()` /
      `implied_vol()` (via this file's own `resolve_iv()`, since
      `option_analyzer.py` no longer exports a `resolve_iv` helper);
      candidate premium / BS fair value ≤ 1.10.
   5. **Risk-reward / breakeven** — SL −30% / T1 +60% is a fixed 2.0 RR;
      separately checks whether the breakeven distance is achievable
      within the *hours left in today's session*
      (`AVG_DAILY_RANGE[symbol] / 6.25h × hours_left`,
      `hours_left_in_session()`), not days like `option_analyzer.py`'s
      breakeven check.
   6. **Time-of-day filter** — hard NO TRADE during 09:15–09:25 and
      15:00–15:30 IST; 12:00–13:15 IST is flagged as lower quality but
      doesn't block a trade (`time_of_day_status()`).
5. Decision (`evaluate_decision()`'s branching, in order):
   - Hard blockers → **NO TRADE**: time-of-day filter active, or the
     candidate leg's volume is below the chain average. (A mixed OI
     signal is *not* a separate hard blocker — it fails both the OI and
     PCR conditions at once, which naturally falls through to WAIT via
     the scoring below.)
   - All 6 conditions pass and direction is bullish → **BUY CE** (nearest
     strike above spot). Bearish → **BUY PE** (nearest strike below
     spot).
   - Otherwise → **WAIT**, printing exactly which condition(s) failed
     and the value that would need to flip.
6. On a BUY, also prints: entry premium range (LTP ±2%), SL (−30%), T1
   (+60%), T2 (+100%), and position size from user-entered capital at 1%
   risk (`LOT_SIZE = 35`).
7. Every run — BUY, WAIT, or NO TRADE — is logged to `decisions_log.csv`
   (gitignored) via `log_decision()`: timestamp, spot, both strikes, each
   condition's value, the alignment score, and the decision.
8. Falls back to manual entry (`manual_entry()`) if the NSE fetch fails —
   typed spot, both strikes, days to expiry, and per-leg premium/IV/OI/OI
   change/price change/volume for all 4 legs, plus the chain-wide average
   volume.

### Phase 2 stubs

`vwap_check()`, `ema_trend()`, `price_action_last5()`, and
`oi_footprint_absorption()` are defined but each just returns `None` —
they need broker-supplied candle/order-flow data this tool doesn't have.
`evaluate_decision()` builds its `conditions` list as plain dicts
(`{"name", "passed", "detail"}`), so wiring these in later is just
appending more entries to that list — the score denominator
(`len(conditions)`) updates automatically, no redesign needed.

### Honesty requirements (non-negotiable, enforced in `print_decision()`)

- Never prints expected holding time.
- Never prints probability or a confidence percentage.
- Always shows the header: "Alignment of current structure - not a
  prediction. Direction risk is yours."
- The score is always labeled "N/6 conditions aligned", never
  "confidence".

## Roadmap

Ideas for future iterations (nothing here is committed or in progress
unless a task explicitly says so):

- Pull recent realized daily range automatically instead of relying on the
  hardcoded `AVG_DAILY_RANGE` table.
- Model IV crush around known event dates (results calendar, RBI policy)
  instead of assuming constant IV.
- Add a lightweight retry/backoff around the NSE fetch for transient
  rate-limiting.
- Support stock F&O (not just NIFTY/BANKNIFTY indices).
- Wire the Decision Engine's phase-2 stubs (VWAP, EMA20/50 trend,
  last-5-candle price action, OI footprint/absorption) up to a real
  broker API for candle/order-flow data.
- Backtest Decision Engine v1's condition set against historical chains
  to see how often 6/6 alignment actually occurs and how it performs.
- A chain-wide scan/ranking mode for `option_analyzer.py` has been tried
  before and reverted; revisit only if explicitly requested.
