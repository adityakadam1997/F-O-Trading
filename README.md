# F-O-Trading

Decision-support tools for a discretionary NIFTY/BANKNIFTY options buyer.
Neither tool places orders, has broker integration, or gives trading
advice — they analyze data you already have (or can fetch from NSE) and
show you the math.

## Tools

### `option_analyzer.py`

Is this specific strike/expiry a reasonable buy? Prices it with
Black-Scholes, runs it through 4 pass/fail checks (price fairness, theta
burden, breakeven feasibility, time buffer), and estimates time-to-target.
Greeks shown include Gamma, with a caution note inside 2 days of expiry.

```
pip install requests
python option_analyzer.py
```

### `decision_engine.py`

Scans the **5 strikes above and 5 below spot** and prints up to 3
conditional trade cards on whichever side the market is aligned toward.

```
pip install requests
pip install yfinance   # optional: realized vol + India VIX
python decision_engine.py
```

Direction is decided the same way as before - from the two strikes
nearest spot - via 4 **market-wide** conditions checked once: OI buildup
direction, PCR, a time-of-day filter, and realized volatility vs ATM IV.
Then, only on the directional side (CEs above spot if bullish, PEs below
if bearish), each of the 5 band strikes is checked against 3 of its own
**per-strike** conditions: premium fairness vs Black-Scholes, volume vs
the chain average, and whether its breakeven distance is achievable in
the hours left in today's session. A strike only **QUALIFIES** when all
3 of its own conditions pass - combined with the 4 market-wide ones,
that's the same 7 conditions as the original single-candidate design,
just split into "once" and "per-strike".

A printed table always shows all 5 band strikes (premium, IV, OI
buildup, fairness/volume/breakeven PASS-FAIL, QUALIFIED yes/no) — even
when market-wide conditions aren't aligned, so you can see why. Trade
cards only print when market-wide is fully aligned **and** at least one
strike qualifies (up to 3, ranked by conditions passed, then
IV/realized-vol ratio, then smallest breakeven distance). Each card
gives:

- **Entry trigger** — the nearest strike in the trade's direction from
  current spot; crossing it is what would confirm the move.
- **Estimated premium at that trigger** — the strike repriced with
  Black-Scholes at the trigger level, same IV, minus 1 hour of time
  decay.
- Entry range (±3%), stop-loss (−30%), target (+60%, T2 +100%), and
  position size from your capital at 1% risk.
- Its own header: "Conditional plan, not a prediction - valid only if
  the trigger level is hit while conditions still hold."

**WAIT is the default answer.** This is not a prediction tool: it never
prints a confidence score, a probability, or an expected holding time —
only which conditions currently align and their actual values. Most
runs, honestly, should print zero cards.

Alongside all of this it also prints context that doesn't gate the
decision but matters for judgment:

- **Max pain** — the strike that minimizes total option-writer payout,
  and how far spot is from it.
- **India VIX** — printed when available; above 17 it tightens the
  premium-fairness condition's threshold from 1.10 to 1.05.
- **Event-day / expiry-day warning** — a prominent "IV-CRUSH RISK" flag
  on RBI MPC days (verified FY27 dates), the day before each MPC
  decision, Union Budget day, or the last day before expiry.

Realized volatility comes from 30 daily closes via `yfinance`
(`^NSEBANK`/`^NSEI`), or a manual `closes.csv` fallback (one close per
line, oldest first) if `yfinance`/network access isn't available. Unlike
the other three additions, realized vol is a **required** market-wide
condition — without RV data no strike can ever qualify for a card.

If the live NSE fetch fails, a full 5+5 scan can't be substituted with
manual entry (it needs the whole chain), so it falls back to a reduced
single-strike assessment - the same nearest-strike behavior the tool
originally had.

Every run is logged to `decisions_log.csv` for later review; that file
is gitignored. Every run also appends today's date + ATM IV to
`iv_history.csv` (also gitignored) — an IV-rank/percentile feature will
activate once that file accumulates roughly 60 days of history; it isn't
implemented yet.

## NSE fetch

Both tools pull live data from NSE's public option-chain API, using
`option_analyzer.py`'s helpers: a session/cookie handshake
(`_nse_session()`), the expiry list (`fetch_expiries()`), and the chain
for one expiry (`fetch_chain_for_expiry()`; the `expiry` param is
required). NSE blocks many datacenter/cloud IPs and rate-limits
aggressively, so if the fetch fails both tools report why and fall back
to manual entry instead of failing silently.

See `CLAUDE.md` for the full design notes, condition definitions, and
roadmap.
