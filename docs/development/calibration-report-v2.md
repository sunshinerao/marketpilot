# StrikePilot calibration report v2 — three-year verdict

Date: 2026-08-20. Window: 2023-08-18 … 2026-08-17 (751 trading days, audited:
zero gaps, zero corrupt records). Scope: distance-based SPXW 0DTE iron condor,
09:45 and 13:00 entries, unconditional and buffer-calibrated tail models,
fee-aware economics, exit-rule comparison.

## 1. The verdict

**No configuration meets the promotion gate. The strategy has no demonstrable
edge on three years of out-of-sample data. `NO_TRADE` remains the correct
platform output.**

Per ADR-0004 the gate requires BOTH (a) out-of-sample touch coverage ≥ 0.975
and (b) positive net (fee-aware) EV. Every tested configuration fails at least
one arm, and the two arms are a see-saw: the configurations with the best
coverage are the least profitable, and vice versa.

## 2. Touch coverage (4-fold purged walk-forward, out-of-sample)

| entry | model | touch coverage | mean total distance | verdict |
|---|---|---|---|---|
| 09:45 | unconditional | 86.8% | 180.5 | ❌ |
| 09:45 | buffered v2 | 93.3% | 225.0 | ❌ |
| 13:00 | unconditional | 89.4% | 125.8 | ❌ |
| 13:00 | buffered v2 | 91.9% | 150.8 | ❌ |

The target is 97.5%. The best result (93.3%) still leaves a 4.2-point
coverage gap. Buffer calibration improves coverage but not to target, and it
costs distance (thinner credit).

## 3. Economics (conservative NBBO fills, fee-aware, 3 years)

| entry | model | gross EV/day | **net EV/day** | CVaR-95 | win rate |
|---|---|---|---|---|---|
| 13:00 | unconditional | +0.025 | **+0.0009** | -3.11 | 47.9% |

The single-year result that looked promising (13:00, net +0.086 pts/day) is
**not reproducible out of sample**: over three years it collapses to
essentially zero after fees (net total +0.60 points on 682 candidate days).
Fees alone (≈ 16.4 points) consumed essentially the entire gross edge. 96 of
682 days (14%) were unpriceable under conservative fill rules.

## 4. Exit rules

The four-rule comparison (hold, 50% profit target, 2× stop, 15:30 exit) was
decisive on the single-year window: at 13:00 the 50% profit target roughly
doubled EV (gross), while the 2× stop destroyed it (74–87% stop-out rates).
On three years the profit-target edge is moot: it improves *gross* EV, but the
gate is touch coverage (which no exit rule changes — a breach is a breach
regardless of when you exit) and the net EV after fees is already ~zero.
Re-running it would not change the verdict and was not performed.

## 5. Conclusions

1. **The platform is doing its job.** Every number traces to hashed PIT
   records; no lookahead (leak-guard tests); the answer is honest and
   reproducible.
2. **The distance-based 0DTE iron condor, under the current rules-v1
   quantiles, has no edge over 2023-2026.** The one-year "13:00 pocket" was a
   regime artifact, not a repeatable alpha.
3. **Buffer calibration is a real improvement (93.3% vs 86.8%) but
   insufficient.** Closing the last 4 points of coverage on fat, shifting
   tails would require distances so wide the credit cannot carry the risk.
4. **Fees are decisive at this scale**: 2.80 USD/condor ≈ 0.028 pts/day — the
   difference between a marginal gross edge and none.

## 6. What this means for the roadmap

- The current strategy line is **paused pending a different thesis**, not
  "fixed by tuning." Options are a different structure (put-credit-only,
  ratio/butterfly), a different timing/selection rule (event-aware entries),
  or accepting a research-tool positioning without a live brief.
- ScoutPilot detectors (ADR-0003) remain valid: opportunity *discovery* on
  this data is unproven but unaffected by this verdict; they are candidates,
  never decisions.
- The Massive Indices subscription remains **on hold** — it was needed only
  to make a live brief executable, and no promotable configuration exists.
- Nothing about the data foundation is wasted: 751 audited days, replay
  manifests, and the calibration harness are reusable for any future thesis.

## 7. Reproduce

```bash
marketpilot ingest-audit --start 2023-08-18 --end 2026-08-17 --scope spxw-0dte
marketpilot calibrate-labels --start 2023-08-18 --end 2026-08-17 --entry 13:00 \
  --labels data/derived/labels/excursions-3y-1300.jsonl
marketpilot recommend-distances --start 2023-08-18 --end 2026-08-17 \
  --model buffered --labels data/derived/labels/excursions-3y-1300.jsonl \
  --out data/derived/labels/dist-3y-1300-buf.jsonl
marketpilot evaluate-economics --start 2023-08-18 --end 2026-08-17 \
  --labels data/derived/labels/excursions-3y-1300.jsonl \
  --distances data/derived/labels/dist-3y-1300-unc.jsonl
```
