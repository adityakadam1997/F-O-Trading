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
- **`decision_engine.py`** — right now, should I BUY CE, BUY PE, WAIT, or
  NO TRADE? Scans the 5 strikes above and 5 below spot and prints up to 3
  conditional trade cards on the qualifying side. See "Decision Engine
  v1" below. **Its default answer is WAIT** — a card only comes out for a
  strike where all 7 objective conditions line up (4 market-wide + 3
  per-strike); anything less is WAIT or NO TRADE, on purpose.

## Requirements

```
pip install requests
pip install yfinance           # optional: realized vol + India VIX in decision_engine.py
python option_analyzer.py     # single strike/expiry analysis
python decision_engine.py     # right-now BUY CE / BUY PE / WAIT / NO TRADE
```

`requests` is only needed for the live NSE fetch; manual entry mode works
without it in both tools (the fetch failure is caught and reported, then
falls back to typed input). `yfinance` is only needed for
`decision_engine.py`'s realized-volatility condition and India VIX
context — both degrade gracefully without it (see "Realized vol, India
VIX, max pain, event days" below).

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

Greeks display also includes **Gamma** (`bs_greeks()` returns
`delta, gamma, theta, vega`) alongside Delta/Theta/Vega. When
`days_to_expiry <= 2`, a "GAMMA-DAY CAUTION" line prints — gamma rises
sharply into expiry, so small index moves swing the premium hard either
way.

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

Scans the 5 strikes above and 5 below spot and, on the directional side,
prints a qualification table plus up to 3 conditional trade cards.
Direction itself still comes from just the two nearest strikes, exactly
as in the original single-candidate version.

### Philosophy: WAIT is the default answer

This tool reports whether a fixed set of observable conditions currently
line up — it does not predict, and it does not estimate confidence.
There is **no probability, no confidence percentage, and no expected
holding time** printed anywhere (`print_scan()` deliberately omits them,
per the honesty requirements below). A strike only produces a trade card
when *all 7* conditions align for it — 4 market-wide (shared by every
strike in a scan) plus 3 of its own; anything less is WAIT (market-wide
misaligned, or aligned but no strike qualifies) or NO TRADE (a hard
blocker is active). In practice most runs should print zero cards —
that's the tool working as intended, not a bug.

### The 7 conditions, split into market-wide (once) and per-strike (each)

**Market-wide** (`evaluate_market_wide()`, computed once from the two
nearest strikes, unchanged from the original design):

1. **OI signal** — bullish if CE shows long buildup and PE shows short
   buildup at both nearest strikes; bearish if reversed; anything else
   is mixed (`classify_oi_signal()`), which fails this condition (not a
   hard NO TRADE — see decision logic below).
2. **PCR** — PE OI / CE OI across both nearest strikes; >1.2 bullish,
   <0.8 bearish, must match the OI signal's direction to pass
   (`pcr_direction()`).
3. **Time-of-day filter** — hard NO TRADE during 09:15–09:25 and
   15:00–15:30 IST; 12:00–13:15 IST is flagged as lower quality but
   doesn't block a trade (`time_of_day_status()`).
4. **Realized vol vs ATM IV** — ATM IV (average of CE/PE IV at whichever
   nearest strike is closer to spot, `compute_atm_iv()`) / 30-day
   realized vol ≤ 1.10 (`RV_IV_THRESHOLD`). RV comes from
   `fetch_realized_vol()`: 30 daily closes via yfinance
   (`^NSEBANK`/`^NSEI`), or a manual `closes.csv` fallback (one close per
   line, oldest first) if yfinance/network is unavailable. **Fails when
   no RV data exists at all** — no strike can ever qualify without RV
   data, by design.

**Per-strike** (`evaluate_strike_conditions()`, evaluated separately for
every strike in the 5-wide band on the directional side):

5. **Premium fairness** — reuses `option_analyzer.bs_price()` /
   `implied_vol()` (via this file's own `resolve_iv()`); this strike's
   premium / BS fair value ≤ 1.10, **tightened to ≤ 1.05 when India VIX
   > 17** (`FAIRNESS_THRESHOLD_HIGH_VIX`).
6. **Volume** — this strike's traded volume must exceed the average
   volume across every leg in the fetched chain.
7. **Breakeven achievable** — whether the breakeven distance for this
   strike is achievable within the *hours left in today's session*
   (`AVG_DAILY_RANGE[symbol] / 6.25h × hours_left`,
   `hours_left_in_session()`), not days like `option_analyzer.py`'s
   breakeven check.

A strike **QUALIFIES** only when all 3 of its own conditions pass (and
market-wide is separately checked before any cards are built).

### Flow

1. Fetch the chain for the nearest (or chosen) expiry (`fetch_chain()`),
   reusing `option_analyzer.py`'s `_nse_session()`, `fetch_expiries()`,
   `pick_expiry()`, `fetch_chain_for_expiry()`, `_find_rows()`, and
   `_find_underlying()`.
2. Find the nearest strike below and above spot (`nearest_strikes()`)
   and pull all 4 legs via `parse_chain_to_legs()`; evaluate the 4
   market-wide conditions from them.
3. Build the 5-above/5-below strike band from whatever strikes actually
   exist in the fetched chain (`strike_band()`) — not a hardcoded 50/100
   step. Classify OI buildup for CE and PE at every band strike
   (`classify_oi_buildup()`: price↑+OI↑ = long buildup, price↓+OI↑ =
   short buildup, price↑+OI↓ = short covering, price↓+OI↓ = long
   unwinding).
4. On the directional side only (CEs above if bullish, PEs below if
   bearish; defaults to CE-above for display if direction is unclear),
   evaluate the 3 per-strike conditions for all 5 strikes and print a
   table: strike, premium, IV%, buildup, fairness/volume/breakeven
   PASS-FAIL, and QUALIFIED yes/no (`scan_band()`, `print_scan()`).
5. Decision (`scan_band()`'s branching, in order):
   - Hard blocker → **NO TRADE**: time-of-day filter active. The table
     still prints; no cards do.
   - Market-wide not fully aligned (mixed OI, PCR mismatch, or missing
     RV data) → **WAIT**, printing exactly which market-wide
     condition(s) failed and their values. Table still prints, no cards.
   - Market-wide aligned but zero strikes qualify → **WAIT**, saying so
     explicitly. Table still prints, no cards.
   - Market-wide aligned and ≥1 strike qualifies → **BUY CE**/**BUY PE**,
     with up to 3 conditional trade cards (`rank_qualified()`: ranked by
     conditions passed, then IV/realized-vol ratio ascending, then
     smallest breakeven distance).
6. Each trade card (`build_trade_card()`) states:
   - **Entry trigger**: the nearest strike in the trade's direction from
     current spot (the same nearest strike used for direction) — the
     level that would confirm the move.
   - **Estimated premium at that trigger**: this strike repriced with
     Black-Scholes at the trigger spot, same IV, minus 1 hour of time
     decay (`estimate_premium_at_trigger()`, `TRIGGER_DECAY_HOURS`).
   - Entry range (±3%, `CARD_ENTRY_BAND_PCT`), SL (−30%), target (+60%,
     T2 +100%), and position size from user-entered capital at 1% risk
     (`LOT_SIZE = 35`).
   - A card-specific header: "Conditional plan, not a prediction - valid
     only if the trigger level is hit while conditions still hold."
7. Every run is logged to `decisions_log.csv` (gitignored) via
   `log_scan()`: timestamp, spot, both nearest strikes with their
   buildups, all market-wide values (RV, VIX, max pain, event flag),
   which side was scanned, how many strikes qualified (and which), how
   many cards were issued, and the decision. Every run also appends
   today's date + ATM IV to `iv_history.csv` (gitignored) via
   `log_iv_history()`.
8. Falls back to a **reduced single-strike** manual assessment
   (`manual_entry()`) if the NSE fetch fails — a full 5+5 scan needs the
   whole chain (many strikes' OI/volume/premium), which manual entry
   can't substitute for, the same reasoning as `option_analyzer.py`'s
   chain scanner. Manual mode still runs the same `scan_band()` machinery
   with a single-strike band on each side, so it's really just the
   original v1 single-candidate behavior wearing the new plumbing. Max
   pain is `None` in manual mode (needs the full chain); realized vol and
   India VIX are fetched independently via yfinance regardless of mode.

### Realized vol, India VIX, max pain, event days

- **Max pain** (`compute_max_pain()`) — for every strike `Kc` in the
  fetched chain, `Pain(Kc) = sum(CE_OI(K) * max(Kc-K, 0)) +
  sum(PE_OI(K) * max(K-Kc, 0))`; the strike that minimizes this is
  printed as "Max pain: X (spot is Y pts above/below)". Needs the full
  chain, so it's `None` in manual-entry mode.
- **India VIX** (`fetch_india_vix()`, via yfinance's `^INDIAVIX`) is
  printed when available. Above 17 it tightens the premium-fairness
  threshold from 1.10 to 1.05 (per-strike condition 5) and says so in
  the output. Fetch failures here are **caught and skipped with no
  message at all** (including yfinance's own internal error print,
  which is suppressed via `contextlib.redirect_stdout`/`redirect_stderr`
  around the call) — VIX is pure context, unlike the RV fetch which
  prints why it failed since missing RV blocks every strike from
  qualifying.
- **Event-day flag** (`event_day_status()`) — a hardcoded 2026 event
  calendar: Union Budget (2026-02-01) and the verified FY27 RBI MPC
  announcement dates (`MPC_DATES_2026`: 2026-04-08, 2026-06-05,
  2026-08-05, 2026-10-07, 2026-12-04). The day **before** each MPC date
  (`PRE_MPC_DATES_2026`) is separately flagged as elevated IV-crush risk
  ahead of the decision. A dynamic rule (`days_to_expiry <= 1` = expiry
  day) applies when none of the above match. On any flagged day, prints
  a prominent "IV-CRUSH RISK" warning and logs it (`event_flag`/
  `event_label` columns in `decisions_log.csv`).

### Phase 2 stubs

`vwap_check()`, `ema_trend()`, `price_action_last5()`, and
`oi_footprint_absorption()` are defined but each just returns `None` —
they need broker-supplied candle/order-flow data this tool doesn't have.
Both `evaluate_market_wide()` and `evaluate_strike_conditions()` build
their `conditions` lists as plain dicts (`{"name", "passed", "detail"}`),
so wiring these in later is just appending more entries to the relevant
list — the score denominators update automatically, no redesign needed.

### Honesty requirements (non-negotiable, enforced in `print_scan()`)

- Never prints expected holding time.
- Never prints probability or a confidence percentage.
- Always shows the header: "Alignment of current structure - not a
  prediction. Direction risk is yours."
- Every trade card additionally carries: "Conditional plan, not a
  prediction - valid only if the trigger level is hit while conditions
  still hold."
- Scores are always labeled "N/4 conditions aligned" (market-wide) or
  shown as PASS/FAIL per condition (per-strike table), never
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
  to see how often any strike qualifies and how the trade cards perform.
- A chain-wide scan/ranking mode for `option_analyzer.py` has been tried
  before and reverted; revisit only if explicitly requested.
- IV rank/percentile once `iv_history.csv` has ~60 days of accumulated
  ATM IV — not implemented yet, just logged in preparation for it.
- `MPC_DATES_2026`/`BUDGET_DATE_2026` only cover 2026; extend with FY28
  dates once RBI publishes its next calendar.
- Cross-rank CE and PE together in one scan instead of requiring a side
  to be picked upfront (carried over from `option_analyzer.py`'s
  now-reverted scanner idea).
- Widen the band size (`BAND_SIZE`) or make it configurable per run
  instead of the fixed 5+5.
- Track whether a trigger level was actually hit intraday and whether
  the card's estimated premium held up, to backtest the reprice math.
