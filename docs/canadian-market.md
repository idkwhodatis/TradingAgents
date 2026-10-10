# Canadian exchange-listed instruments

## Inputs and compatibility

The interactive CLI, prompt-free CLI and Python graph accept explicit TSX/TSXV
listings. Bare symbols are **not** inferred to be Canadian: `RY` stays `RY`.

```sh
tradingagents analyze TSX:RY
tradingagents analyze ZSP.TO
tradingagents analyze REI-UN.TO
tradingagents analyze TSXV:RCK
```

`TSX:REI.UN` becomes `REI-UN.TO`; `TSX:BBD.B` becomes `BBD-B.TO`;
`TSX:DLR.U` becomes `DLR-U.TO`. Conflicting exchange prefix/suffix combinations
are rejected. A canonical suffix is a listing request, not proof of current
listing, liquidity, quote currency, fund type or data availability.

Native TSX preferred-share `.PR.<series>` notation is handled explicitly:
`TSX:ENB.PR.V` and `ENB.PR.V.TO` both become Yahoo's `ENB-PV.TO`.
Here `V` names the preferred series, not TSXV. An unqualified `ENB.PR.V`
is ambiguous and is rejected; use the TSX prefix or exact Yahoo symbol.
Unsupported preferred notation (such as `.PF.<series>`) requires the exact
Yahoo symbol rather than a guessed hyphenation. Genuine exchange conflicts,
including `TSX:RCK.V`, still fail.

Portfolio comparisons recognize the same Canadian aliases without modifying
the supplied holdings. A holding in `BBD.B.TO` matches an analysis of
`BBD-B.TO`; `RY` and `RY.TO` remain distinct. After a Python SDK analysis,
`graph.save_reports(state, "TSX:RY")` uses the canonical `RY.TO` report name
and safe output directory. A mismatching state listing is rejected.

Stocks, ETFs, REITs and other **exchange-listed** funds are in scope.
Non-exchange NAV-priced mutual funds are outside this adapter. Existing
`asset_type="stock"` remains the graph/CLI compatibility category for all these
listings; provider `quoteType` and instrument context distinguish funds.

## Data contract and limits

- Yahoo is the supported provider for Canadian symbol-bound prices, technicals,
  profile, statements and news. The configured chain is filtered to compatible
  vendors; it never adds a vendor the user did not configure. A chain containing
  only SEC EDGAR/Alpha Vantage returns `DATA_UNAVAILABLE`, with instructions to
  configure yfinance. No unsuffixed or US-listed ticker is substituted.
- Existing OHLCV and calculated indicators retain `.TO`/`.V` in requests and
  caches. Data coverage, timeliness and sufficient history remain provider
  dependent; a successful mock test is not a live-data availability guarantee.
- Identity is Yahoo's current profile for the exact listing, not exchange-
  verified metadata. Explicit mismatched provider symbols are rejected. Failed
  identity reads leave ticker-only context. News search requires exact canonical
  related tickers, and the normal dated-source and DDG safety policies remain.
  Yahoo/DDG are incomplete news sources, not a complete SEDAR+ disclosure feed.
- ETFs use returned fund profile fields (name, family, category, description,
  NAV and assets when present) instead of fabricated operating-company metrics.
  Corporate statement requests for provider-identified funds return
  `NOT_APPLICABLE`. The adapter does not promise complete holdings, MER/fees,
  distributions, tracking error or historical NAV. Missing data stays unavailable.
  Corporate statements for equities/REITs remain best-effort Yahoo data;
  FFO/AFFO is not calculated from unrelated accounting fields.
- Quote currency and reporting currency are separate provider fields. `.TO`
  does not imply CAD: USD-traded classes retain provider currency. No automatic
  FX conversion is performed. Fund assets retain an explicit currency caveat.
- Default analysis dates, future-date validation, current-profile/statement
  guards and memory cutoff use `America/Toronto` for Canadian listings, including
  DST. This is the calendar date, **not** the last trading session. The adapter
  does not install a holiday calendar or fabricate missing holiday bars.
  News windows retain the upstream UTC timestamp convention.
- Historical Yahoo profiles/statements remain withheld because they lack
  reliable point-in-time vintages. Canada support does not add historical
  Canadian filings or remove the fork's anti-lookahead protection.

Shared agent context covers Canadian sessions, BoC/CAD exposure, SEDAR+/issuer
sources, sparse TSXV coverage, fund analysis and REIT metrics without implying
that unsupported tools can retrieve them. LLM providers, keys, budgets, DDG
cooldowns, SQLite/Zstd storage and US/other-market routing are unchanged.

## PanWatch boundary

PanWatch must install this fork at the reviewed commit to consume the adapter.
Its former upstream v0.5.0 pin has older module locations. Consumers migrating
to this fork should use these current APIs:

- `tradingagents.graph.trading_graph.TradingAgentsGraph`
- `tradingagents.dataflows.router.route_to_vendor`
- `tradingagents.agents.tools` (tool import sites use injected graph ticker/date)
- `tradingagents.dataflows.vendors.yahoo.ohlcv.load_ohlcv`
- `tradingagents.dataflows.vendors.yahoo.snapshot.build_verified_market_snapshot`

`propagate(symbol, date, asset_type="stock", portfolio=None)` and
`propagator.create_initial_state(..., instrument_context=..., portfolio_context=...)`
remain the integration boundary. Caller-specific routing/metadata patches
belong in PanWatch, not in this extension. Always test wrappers against the
actual pinned fork commit rather than using only stub modules.

## Upstream sync

Policy lives in `tradingagents/extensions/canadian_market.py`. The small hooks
cover normalization, CLI input/date defaults, agent context, historical guards,
Yahoo fund/profile formatting and vendor compatibility. Keep these hooks when
upstream reorganizes modules; do not duplicate the full graph or provider.

Run `pytest -q` and `ruff check .`. `tests/test_canadian_market.py` covers
company, ETF, REIT, TSXV, USD-class notation, rejected namesake metadata,
unsupported vendor chains, exact news attribution, OHLCV and winter/summer date
boundaries without external requests or LLM calls.
`tests/test_canadian_run_boundaries.py` covers portfolio aliases, CLI/SDK entry
points, preferred shares, report exports and unsafe path rejection.
