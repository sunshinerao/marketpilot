# StrikePilot calibration report v1 — 2025-08-18 … 2026-08-17

Date: 2026-08-20. Status: **baseline does not promote; NO_TRADE remains correct.**
Pipeline: 251 landed trading days (Databento OPRA/GLBX, audited) → 250 excursion
labels (Cboe-anchored implied SPX) → 250 entry features (BS inversion on real
NBBO) → purged walk-forward → conservative economics on real NBBO.

## 1. Ground truth: realized excursions (250 days, 09:45 ET entry → close)

| quantile | up_max (pts) | down_max (pts) |
|---|---|---|
| p50 | 22.3 | 21.4 |
| p90 | 61.7 | 76.8 |
| p0.975 (rules-v1) | 103.2 | 112.2 |
| p0.99 | 109.9 | 177.8 |
| max | 163.0 | 226.0 |

Empirical breach at the p0.975 distance: 2.8% of days (expected 2.5%) — the
label set is internally coherent. Downside tail is materially fatter.

## 2. Out-of-sample corridor coverage (purged walk-forward, 4 folds, 124 days)

| model | coverage | target | mean down dist | mean up dist |
|---|---|---|---|
| Unconditional baseline | 93.6% | 97.5% | 114.2 | 100.7 |
| IV-regime v1 | 86.3% | 97.5% | 126.8 | 83.4 |

**Neither model is calibrated.** Empirical quantiles fitted on trailing windows
under-cover out of sample (fat tails, regime shifts). Splitting by IV quartile
made coverage *worse* (noisier per-regime tails), although it produced a more
efficient asymmetric shape.

## 3. Economics (conservative NBBO fills, shorts at bid / longs at ask, 0DTE expiry)

Unconditional baseline (165 priced days, 25 unpriceable):

- EV **−0.021 pts/day**; total PnL −3.95; CVaR-95 −2.37; win rate 50.3%;
  max daily loss 5.0 (wing width).

IV-regime v1 (179 priced days, 11 unpriceable):

- EV **−0.079 pts/day**; total PnL −14.99; CVaR-95 −4.38; win rate 62.0%.
- Regime pockets: IV_Q1 −0.135/day (80% wins), IV_Q2 −0.087 (81%),
  IV_Q3 −0.222 (69%), **IV_Q4 +0.030/day (36% wins)** — the only
  positive-EV pocket (53 days; high-IV days carry large credits).

Execution-quality finding: 13% of baseline days were unpriceable at 09:45
under conservative fill rules (missing/zero-bid legs on deep-OTM strikes).

## 4. Conclusions

1. **The platform works**: every number above traces to hashed PIT records,
   Cboe anchors, and append-only ledgers; no lookahead (leak-guard tests).
2. **The baseline does not have a proven edge.** Selling 0DTE iron condors at
   trailing 0.975-quantile distances earned ≈ zero-to-negative EV over this
   year. The promotion gate correctly stays closed; `NO_TRADE` remains the
   correct output. Fees/commissions are not yet modeled, which would only
   lower EV further.
3. **Calibration gap is quantified, not hidden**: 93.6% vs 97.5% target. The
   next iteration must close it with buffer calibration (distance = quantile +
   calibrated buffer chosen for target coverage), not by wishing.
4. **IV_Q4 pocket is hypothesis, not evidence** (53 days): high-IV regimes
   show positive mean PnL; needs more data and is exactly the kind of
   conditional rule a ScoutPilot-style detector must validate on untouched
   data before any use.

## 5. Next iteration (proposed)

1. Buffer-calibrated tail model v2 targeting 97.5% OOS coverage.
2. Fees/slippage sensitivity in economics (per-contract commission model).
3. VIX level/term-structure features once the index feed is licensed.
4. Entry-time sweep (09:45 vs 10:30 vs 13:00) — excursion windows differ.
5. 2–3 years of history (Databento pull is a config change away) before any
   regime-pocket hypothesis is tested again.
