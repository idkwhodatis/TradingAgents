# Mainland A-share identity adapter

This additive adapter resolves a six-digit **stock** input into an exact mainland
A-equity listing. It supplies Chinese security short name, Chinese legal full
name, and sourced English name when available. It does not translate names,
create a strategy, change market-data vendor order, or promise two independent
news perspectives.

## Input semantics and examples

- `601868` / `601868.SH` / `601868.SS` → `601868.SS`, when official SSE metadata
  confirms it. Chinese short name: **中国能建**; Chinese legal full name:
  **中国能源建设股份有限公司**; official English full name:
  **China Energy Engineering Corporation Limited**.
- Bare `000001` in the stock input mode resolves **平安银行 / 000001.SZ** only
  after a unique exact official SSE/SZSE A-equity match. A bare code deliberately selects
  the A-equity universe, not a generic index/instrument namespace.
- Explicit `000001.SS` keeps its Shanghai index identity. It is never rewritten
  to Ping An Bank. Explicit ETF/index symbols retain their legacy metadata path
  and receive no A-company enrichment. Use an explicit exchange suffix for
  indices, ETFs, or any non-equity interpretation.
- `920819` / `920819.BJ` → `920819.BJ`, when the exact current BSE listing
  is confirmed by Eastmoney metadata. Chinese short name: **颖泰生物**;
  Chinese full name: **北京颖泰嘉和生物科技股份有限公司**; English name:
  **Nutrichem Company Limited**. All three are provider labels, not official
  exchange-sourced names.
- No first-digit rule assigns an exchange. Bare inputs first query official
  SSE and SZSE metadata. Both must answer successfully, with at most one
  A-equity match. Only when both return no exact match does the adapter probe
  BSE through Eastmoney. A BSE outage therefore cannot break an already
  verified SSE/SZSE identity. Explicit `.BJ` queries only the BSE provider.
  Provider failure, duplicate matches, and unverified bare codes stop before
  analysis/storage. Explicit unknown symbols retain their requested ticker.
- US, HK, crypto and other non-candidate symbols use their existing behavior.
- Legacy BSE codes are **not automatically converted**. Neither an old prefix
  nor `920` proves exchange/type, and replacing a prefix can identify the wrong
  security. An old-code request whose provider response contains a different
  current code is rejected. Use an independently verified current ticker for
  current analysis; historical symbol validity needs separate verification.

## Source contract

Read-only, bounded, per-code requests use existing `requests`; no SDK, paid key,
credentials, or full-company-list download is required.

- SSE official profile:
  `https://query.sse.com.cn/commonQuery.do`, `sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GPGK_GSGK_C`,
  exact `COMPANY_CODE`. An exact `A_STOCK_CODE` and `SEC_TYPE` of `主板A` or
  `科创A` establish eligibility (CDRs are excluded).
  `SECURITY_ABBR_A_CN`, `FULL_NAME`, `FULL_NAME_EN`, `COMPANY_ABBR_EN` supply
  the separately retained names/aliases. Official frontend reference:
  <https://www.sse.com.cn/xhtml/home/2021public/querySearch/search_stocksDepositoryReceipts_2021.js>.
- SZSE official A-share list:
  `https://www.szse.cn/api/report/ShowReport/data`, catalog `1110`, tab `tab1`,
  `txtDMorJC=<code>`. Check the `A股列表` tab and exact `agdm`; strip HTML
  from `agjc`. This endpoint supplies the security short name, **not** a legal
  full name or English name. Those fields remain unknown unless another source
  actually provides them.
- BSE current profile from the independent provider Eastmoney:
  `https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax`,
  `code=BJ<code>`. The public GET sends no custom headers or cookies. Exactly one
  row in `jbzl` must match all of `SECURITY_CODE=<code>`, `STR_CODEA=<code>`,
  `SECUCODE=<code>.BJ`, `TRADE_MARKET=北京证券交易所`, and
  `SECURITY_TYPE=北京证券交易所A股`. Cross-market results, ETFs, indices and
  mismatched codes cannot supply names. `SECURITY_NAME_ABBR`, `ORG_NAME` and
  `ORG_NAME_EN` supply the short, full and English names when present. Every
  name alias is marked `provider_label` with Eastmoney provenance; the English
  name is never promoted to official English. A missing name remains null.
  A valid empty response is a bounded negative lookup; malformed/ambiguous
  responses, HTTP errors and timeouts are failures and are not cached. Either
  outcome leaves an explicit `.BJ` identity unavailable and the ticker intact.
- When SZSE has no English name, optional Yahoo search
  (`https://query1.finance.yahoo.com/v1/finance/search`) may supply an English
  display label. It must match the exact symbol, `quoteType=EQUITY`, and
  `exchange=SHZ`; otherwise it is ignored. It is recorded as `provider_label`
  with Yahoo provenance, **never** as official English. The live request on
  2026-10-08 returned HTTP 429, so SZSE English coverage was not verified live.
  One bounded request is made, without retries; failure preserves the verified
  Chinese identity and explicitly leaves English missing. Parser/failure cases
  are covered by synthetic offline fixtures, not claimed as live observations.

Live observations on 2026-10-08 back the official SSE 600519/601868 and SZSE
000001 fixtures, and representative Eastmoney field subsets for current BSE
920819/920799/920002/920123. The BSE fixture provenance file distinguishes these
verified subsets from full raw responses. Mutated failure and rejection cases
are synthetic offline tests, not claimed as live observations. Provider
availability/schema changes remain a runtime risk; offline tests do not prove
future availability.

## Evidence and temporal limits

Structured state includes names, sourced aliases, exchange/security type,
resolution status, observation timestamp, current-identity temporality,
confidence, and explicit coverage gaps. `confidence=verified` means the
adapter validated an exact listing against the stated source; it does not turn
an independent provider into an official exchange source. Missing names stay
null. These are **current names**, not a point-in-time name history. Historical
analysis uses names only to identify the listing, not as evidence of historical
company facts.

BSE support is a current-identity extension, not a historical security master.
It has no old-to-new mapping table or effective-date conversion. Resolving a
current `920` ticker does not establish that the ticker traded on an earlier
analysis date, that historical prices are available under it, or that a price
vendor accepts the canonical `.BJ` symbol. A provider company profile is not a
live listing-status or trading-availability check. The adapter does not determine
IPO or delisting dates and does not infer trading eligibility from the prefix.

`retrieval_queries` separates official-announcement search suggestions from
English/overseas-news suggestions. Both explicitly say `evidence_retrieved=false`:
a suggested query is not a fetched announcement or an independent viewpoint. The
separate [news evidence extension](news-evidence.md) now performs bounded
source-screened retrieval through the shared news tools. Its actual results
and retrieval status appear in tool output without mutating these identity-only
planning fields.
The configured Yahoo news fallback now also searches the current short/full/
English names with the exact stock code. It accepts only articles carrying the
exact canonical related ticker, applies the existing date window, deduplicates
name-query hits, and keeps the article budget. Sparse tagging can still leave
no usable news. No new Chinese announcement vendor is installed. StockTwits and
Reddit coverage gaps are unchanged.

The report includes a neutral identity-and-coverage section for applicable
runs. The same structured snapshot is retained in the existing state artifact
and SQLite report snapshot; there is no market-specific schema or database.
No raw request exceptions, API keys, headers, or provider credentials enter the
identity or report metadata.

## Configuration and lifecycle

Defaults in `build_default_config()`:

```python
config.update(
    ashare_identity_enabled=True,
    ashare_identity_timeout=5.0,      # each HTTP request; 0.1 through 30 seconds
    ashare_identity_cache_ttl=86400,  # 0 disables reuse; maximum seven days
)
```

The same settings are available to both CLI modes through `.env` or the shell:
`TRADINGAGENTS_ASHARE_IDENTITY_ENABLED=false`,
`TRADINGAGENTS_ASHARE_IDENTITY_TIMEOUT=5`, and
`TRADINGAGENTS_ASHARE_IDENTITY_CACHE_TTL=86400`.

Cache is in-process, bounded to 256 exchange-and-code-keyed entries in total;
negative results have at most 60 seconds of reuse. Primary identity-provider
failures are not cached, and expired values are never returned as a stale
fallback. BSE shares these limits without adding a persistent cache. An optional English lookup failure remains an
explicit missing field within the successful Chinese identity cache entry until
that identity TTL expires. A run pins one snapshot. New runs refresh according
to TTL; graph reuse across tickers cannot transfer a previous ticker's identity.
Disable the extension to retain explicit ticker behavior; bare numeric codes
still require an exchange instead of being sent to an arbitrary vendor.

Both TUI and headless CLI canonicalize before their result paths, archive IDs,
summary, checkpoint, and analysis. Programmatic `propagate()` does the same
before storage/checkpoint setup. The graph's common `create_run_state()` uses
the same preparation for direct lifecycle callers. Analytical identity changes
invalidate checkpoints; timestamps, timeout/TTL and storage locations do not.
Non-A runs retain their existing checkpoint signatures.

## Minimal upstream seams and checks

Implementation lives in `tradingagents/extensions/ashare_identity.py`.
Integration seams are intentionally small:

1. Headless/common CLI plus graph entry preparation, before persistence.
2. Shared agent identity context and typed state fields.
3. Existing report-section registry and snapshot allowlist.
4. Existing Yahoo fallback query generation, without modifying vendor routing.
5. Signature exclusion of operational options and inclusion of analytical data.

When syncing upstream, reconcile these seams with changed lifecycle ordering;
do not copy an older graph/CLI implementation over upstream. Keep canonical
symbol consistent through storage, memory, checkpoints, and reports.

```sh
pytest -q
ruff check .
git diff --check
```

Focused offline suites: `test_ashare_identity.py`, `test_bse_identity.py`,
`test_ashare_identity_integration.py`, `test_ashare_news_identity.py`, alongside
all existing CLI execution, state/report, storage and checkpoint tests.
No model calls, portfolio/trading analysis, paid APIs, deployment, or changes to
user-held assets are needed to test this adapter.
