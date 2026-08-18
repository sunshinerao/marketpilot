# Glossary

- **MarketPilot**: Cross-market decision intelligence platform.
- **StrikePilot**: Options strike and multi-leg structure model family.
- **ScoutPilot**: Opportunity-discovery scanner family. Emits candidates, never
  decisions (ADR 0003).
- **Candidate**: An evidence-bounded opportunity or risk observation with
  invalidation conditions and a next checkpoint; never an executable
  recommendation.
- **Gamma squeeze**: Forced directional acceleration when estimated dealer gamma
  positioning requires hedging into price moves. Dealer positioning is inferred,
  not observed; candidates must label the estimate method.
- **Short squeeze**: Forced covering rally when crowded short positioning meets
  a catalyst and scarce borrow. Requires licensed short-interest/borrow data.
- **Volatility squeeze**: A compressed implied/realized volatility regime that
  historically precedes expansion; a long-volatility opportunity candidate.
- **IV crush**: The rapid implied-volatility collapse after a scheduled event; a
  risk warning for long-premium exposure into events.
- **Implied SPX**: Cash coordinate inferred from an explicit ES contract and synchronized anchor.
- **Event-cleared**: The event is released and its market reaction has passed stability gates.
- **Joint calibration**: Calibration of the probability that neither side of the corridor is exceeded.
- **No Trade**: A normal safety result produced when one or more hard gates fail.
- **Snapshot**: Canonically serialized point-in-time inputs with a deterministic hash.

