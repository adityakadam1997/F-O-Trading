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

**Right now**, on the two strikes nearest to spot, should you BUY CE,
BUY PE, WAIT, or NO TRADE?

```
pip install requests
pip install yfinance   # optional: realized vol + India VIX
python decision_engine.py
```

It checks 7 objective conditions — OI buildup direction, PCR, volume vs
the chain average, premium fairness vs Black-Scholes, whether the
breakeven distance is achievable in the hours left in today's session, a
time-of-day filter, and realized volatility vs IV — and only says BUY
when **all 7** line up in the same direction.

**WAIT is the default answer.** This is not a prediction tool: it never
prints a confidence score, a probability, or an expected holding time —
only which of the 7 conditions currently align and their actual values.
Most runs, honestly, should come back WAIT.

Alongside those 7 conditions it also prints context that doesn't gate the
decision but matters for judgment:

- **Max pain** — the strike that minimizes total option-writer payout,
  and how far spot is from it.
- **India VIX** — printed when available; above 17 it tightens the
  premium-fairness condition's threshold from 1.10 to 1.05.
- **Event-day / expiry-day warning** — a prominent "IV-CRUSH RISK" flag
  on RBI MPC days, Union Budget day, or the last day before expiry.

Realized volatility comes from 30 daily closes via `yfinance`
(`^NSEBANK`/`^NSEI`), or a manual `closes.csv` fallback (one close per
line, oldest first) if `yfinance`/network access isn't available. Unlike
the other three additions, realized vol is a **required** condition —
without RV data the run can never reach 7/7, so it can never produce a
BUY.

Every run (BUY, WAIT, or NO TRADE) is logged to `decisions_log.csv` for
later review; that file is gitignored. Every run also appends today's
date + ATM IV to `iv_history.csv` (also gitignored) — an IV-rank/
percentile feature will activate once that file accumulates roughly 60
days of history; it isn't implemented yet.

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
