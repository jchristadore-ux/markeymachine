# MarkeyMachine — Managed Trading Service: Business Plan

> Draft v0.1 · 2026-06-30 · internal working document
>
> **This is a business-planning document, not legal, tax, or investment advice.**
> The model described here — trading other people's accounts for a fee — is a
> regulated activity. Read the [Risk & Compliance](#7-risk--compliance-read-this-first)
> section first and obtain qualified legal counsel before onboarding any paying
> client. Nothing here is a promise of returns.

---

## 1. Executive Summary

MarkeyMachine is a production quantitative trading bot for Kalshi's 15-minute
BTC up/down prediction markets. The strategy, risk controls, and operational
tooling already exist and are battle-tested in paper and live trading. The
**service** wraps that engine into a managed offering:

> **"You bring your Kalshi account. We run the strategy."**

The only thing unique to each client is **their Kalshi account** — their funds,
their API key. Everything else (the strategy, the infrastructure, the
monitoring, the risk presets) is shared product. We never take custody of client
money: it stays in the client's own Kalshi account the entire time. We operate a
trade-scoped API key on their behalf and charge a fee for the service.

The newly added **Trading Formats** (selectable risk presets) and the
**management dashboard** are what make this productizable: a non-technical
operator can onboard a client, choose a risk tier, and start/stop/monitor their
bot from a web GUI — no code, no environment-variable spelunking.

**What we're selling:** convenience, a vetted strategy, and disciplined
execution/risk management — packaged as a subscription. **What we are not:** a
fund, a custodian, or a guarantor of profit.

---

## 2. The Product

### 2.1 Core engine (exists today)
- One strategy: trend-confirming near-money order-book pressure on Kalshi
  KXBTC15M markets, with momentum confirmation, regime filtering, a time-of-day
  learned prior, Bayesian win-probability, and a 9-layer entry checklist
  (see [`TRADING_DOCTRINE.md`](TRADING_DOCTRINE.md)).
- Disciplined sizing: two-tier Recovery + graduated Probation re-entry, an
  opt-in performance Ladder overlay, and always-on guardrails (streak pause,
  session stop, stale-order cancel).
- Full Telegram reporting and Wilson-interval win-rate confidence tracking.

### 2.2 Trading Formats (the productization layer)
Named risk presets — **Conservative, Balanced, Aggressive, Recovery-First** —
that bundle ~40 tuning knobs into one switch (see [`formats.py`](formats.py) and
the README). This is how we offer **risk tiers** to clients without maintaining
separate codebases. A client picks a tier; we map it to a format.

### 2.3 Management dashboard (the operator layer)
A web control panel (see [`dashboard/`](dashboard/)) to onboard accounts, pick a
format, start/stop each client's bot, and monitor balance, P&L, win rate, sizing
mode, and open positions. Single-account today, **architected to become a
multi-tenant operator console** (one bot worker per client, isolated state).

### 2.4 Paper-first by default
Every account and every format defaults to **paper (demo) mode**. A client can
watch the strategy trade their real market data with zero capital at risk before
ever flipping to live — a powerful trust-builder and a natural trial.

---

## 3. Customer & Value Proposition

### 3.1 Who
- **Retail Kalshi users** who believe there's edge in short-dated BTC markets but
  lack the time, discipline, or quant tooling to trade them systematically.
- **Crypto-native, automation-comfortable individuals** who already hold or will
  open a Kalshi account and want a "set-and-monitor" managed strategy.

### 3.2 Why they pay
| Pain | What we provide |
|---|---|
| No time to watch 15-minute markets | 24/7 automated execution |
| Emotional / undisciplined trading | Rules-based entry + mechanical risk controls |
| No quant tooling or backtested edge | A vetted strategy with confidence tracking |
| Fear of blowing up the account | Risk-tiered formats, session stops, paper-first |
| "I don't want to hand over my money" | **Funds never leave their own account** |

### 3.3 Positioning
Not a hedge fund, not signals-in-a-Discord. A **managed execution service** on an
account the client owns and can revoke access to at any time. The custody model
is the differentiator and the trust anchor.

---

## 4. Service & Custody Model

1. Client opens / already has a **Kalshi account** and funds it themselves.
2. Client generates a **Kalshi API key** (trade-scoped) and provides it to us
   through the dashboard. **They retain full ownership and can revoke the key
   instantly.**
3. We never receive, hold, or move client cash. Withdrawals and deposits are the
   client's, to their own bank — we have no custody and cannot pull funds.
4. We select a **Trading Format** (risk tier) with the client and run an isolated
   bot worker for their account.
5. Client gets read access to their dashboard + Telegram reporting and can ask us
   to pause/stop at any time.

> **Key compliance posture:** non-custodial. This reduces (but does **not**
> eliminate — see §7) the regulatory surface versus pooling client funds.

---

## 5. Pricing

Pricing is **illustrative** and must be validated against the regulatory
analysis in §7 (performance fees in particular are heavily regulated).

### Option A — Flat subscription (simplest, least regulatory baggage)
| Tier | Price/mo | Includes |
|---|---|---|
| Paper / Trial | $0 | Paper mode only, full dashboard + reporting |
| Standard | $49 | Live, one account, Conservative/Balanced formats |
| Pro | $99 | Live, all formats incl. Aggressive, priority support |

Subscription revenue is **decoupled from client P&L** — the cleanest model
legally and the easiest to forecast.

### Option B — Management + performance fee (higher upside, higher scrutiny)
- e.g. 1–2% monthly management fee on account equity **+** 10–20% performance fee
  on profits above a **high-water mark**.
- Performance/asset-based fees on others' trading strongly implicate
  adviser/CTA regulation (§7). **Do not adopt without counsel.**

**Recommendation:** launch on **Option A (flat subscription)**. It aligns
incentives reasonably (clients only keep paying if they see value), keeps revenue
predictable, and carries materially less regulatory risk than charging on AUM or
profits.

---

## 6. Operations

- **Onboarding:** guided flow — open/fund Kalshi → create API key → enter in
  dashboard → choose risk tier → run in paper → flip to live with explicit
  confirmation.
- **Isolation:** one worker + one state directory per client; no shared trading
  state. Already supported by the supervisor (`dashboard/supervisor.py`).
- **Monitoring & reporting:** dashboard status snapshots + per-client Telegram
  alerts (entries, wins/losses, heartbeats, daily summaries).
- **Support & incident response:** documented runbook for halts, session stops,
  and key revocation; clear SLAs by tier.
- **Infra:** cloud-hosted (Railway today); scale to a worker-per-client model,
  then a job scheduler / container orchestrator as client count grows.

---

## 7. Risk & Compliance (read this first)

This section is deliberately blunt. The product is technically ready; the
**legal/regulatory path is the gating risk**, not the code.

### 7.1 Trading is high-risk
Short-dated prediction-market trading can and does lose money. The strategy has a
*probabilistic* edge, not a guaranteed one — win rate drifts with regime and
liquidity. **Clients can lose part or all of their traded capital.** Every
client-facing surface must carry a prominent risk disclosure, and onboarding
should require explicit acknowledgment. Past or backtested performance must never
be presented as a promise of future results.

### 7.2 Regulatory exposure — managing others' accounts for a fee
Trading someone else's account for compensation can trigger registration and
conduct obligations, potentially including:
- **CFTC / NFA:** Kalshi is a CFTC-regulated designated contract market. Advising
  on or trading commodity-interest/event contracts for others for a fee may
  implicate **Commodity Trading Advisor (CTA)** / **Commodity Pool Operator**
  regimes (and NFA membership), subject to available exemptions.
- **Securities/adviser law:** depending on how the offering and fees are
  structured (especially asset-/performance-based fees), **investment-adviser**
  rules (federal and state) may be implicated.
- **Money transmission / custody:** our **non-custodial** design is specifically
  intended to avoid money-transmitter and custody obligations — preserve it.
- **Marketing & performance claims:** advertising rules restrict performance
  representations and testimonials.

**Action:** obtain qualified securities/commodities counsel **before** taking a
single paying live client. Validate the fee model (favor flat subscription),
required registrations/exemptions, and disclosure/agreement documents.

### 7.3 Platform (Kalshi) Terms of Service
Automated/API trading and **operating an API key on another user's behalf** may
be restricted or conditioned by Kalshi's Terms of Service and API agreement.
Account/credential sharing and third-party-managed trading can violate platform
terms even where it's otherwise lawful.

**Action:** review Kalshi's ToS and API terms; where the managed-third-party
model isn't permitted, pursue an approved path (e.g. an official API/partner
program, or a self-serve software-tool model where the *client* runs the bot on
their own account and we sell only the software/dashboard).

### 7.4 Strategic fallback if managed-accounts is blocked
If trading clients' accounts for a fee proves non-viable under §7.2/§7.3, pivot
to **software-as-a-product**: license the bot + dashboard + formats and have each
client run it **on their own account, under their own control**. This likely
moves us from "money manager" toward "software vendor," a materially lighter
regulatory posture — at the cost of the white-glove managed experience. The code
already supports this (a client can self-host `bot.py` + the dashboard).

### 7.5 Operational & key risks
- API-key compromise → scope keys to trading only; encrypt at rest; support
  instant revocation; never log secrets (the dashboard already masks them).
- Single strategy / single market (BTC 15-min) = concentration risk; diversify
  formats/markets over time.
- Outages and stuck state → existing boot reconciliation, session stops, and
  stale-order cancellation; add health checks and alerting per client.

---

## 8. Go-to-Market & Roadmap

### Phase 0 — Validate (now)
Run our own account(s) live across formats; publish *transparent, caveated*
performance (with confidence intervals, not cherry-picked). **Complete the §7
legal review and choose the fee/operating model.**

### Phase 1 — Closed beta
A handful of hand-onboarded clients on the **flat subscription**, paper-first.
Prove onboarding, isolation, reporting, and support.

### Phase 2 — Multi-tenant console
Build out the operator dashboard into true multi-tenant (auth per client,
client-scoped views, per-client process orchestration, billing integration).

### Phase 3 — Scale
Self-serve onboarding, more risk tiers/markets, partner/affiliate channel,
and — if and only if the law allows — premium performance-fee tiers.

### KPIs
Trial→paid conversion, net revenue retention, churn, paper→live conversion,
client account survival rate, support load per client, and risk-adjusted (not
raw) strategy performance.

---

## 9. Illustrative Financials

**Illustrative only — not a forecast.** Flat-subscription model, blended ARPU
~$70/mo:

| Clients | MRR | Annualized |
|---|---:|---:|
| 25 | ~$1,750 | ~$21,000 |
| 100 | ~$7,000 | ~$84,000 |
| 500 | ~$35,000 | ~$420,000 |

Primary costs: cloud/compute (scales ~linearly per worker), support, and
**legal/compliance (significant and front-loaded)**. The dominant early
investment is the §7 legal work, not engineering.

---

## 10. Summary

The engine and the productization layer (formats + dashboard, paper-first,
non-custodial) are in place. The business is gated not by technology but by
getting the **regulatory and platform-terms** path right. Recommended path:
validate transparently → flat-subscription closed beta → multi-tenant console,
with the software-product pivot held in reserve if managed-accounts proves
non-viable. Lead with the trust story — *the client's money never leaves their
own account* — and never sell a return.
