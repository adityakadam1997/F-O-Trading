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

```
pip install requests
python option_analyzer.py
```

### `decision_engine.py`

**Right now**, on the two strikes nearest to spot, should you BUY CE,
BUY PE, WAIT, or NO TRADE?

```
pip install requests
python decision_engine.py
```

It checks 6 objective conditions — OI buildup direction, PCR, volume vs
the chain average, premium fairness vs Black-Scholes, whether the
breakeven distance is achievable in the hours left in today's session,
and a time-of-day filter — and only says BUY when **all 6** line up in
the same direction.

**WAIT is the default answer.** This is not a prediction tool: it never
prints a confidence score, a probability, or an expected holding time —
only which of the 6 conditions currently align and their actual values.
Most runs, honestly, should come back WAIT.

Every run (BUY, WAIT, or NO TRADE) is logged to `decisions_log.csv` for
later review; that file is gitignored.

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
