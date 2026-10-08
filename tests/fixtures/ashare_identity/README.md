# Identity fixture provenance

These JSON files contain representative identity-field subsets, not complete raw
HTTP captures. They preserve field spelling and values from public read-only
samples checked on 2026-10-08. No credentials or financial-account data are used.

- `sse_600519.json`, `sse_601868.json`: official SSE company profile,
  `https://query.sse.com.cn/commonQuery.do`, query
  `sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GPGK_GSGK_C&COMPANY_CODE=<code>`.
- `szse_000001.json`: official SZSE catalog 1110, A-share tab1,
  `https://www.szse.cn/api/report/ShowReport/data`, query
  `SHOWTYPE=JSON&CATALOGID=1110&TABKEY=tab1&PAGENO=1&txtDMorJC=000001`.
  The link wrapper is simplified in this representative fixture.
- `eastmoney_920819.json`, `eastmoney_920799.json`, `eastmoney_920002.json`,
  `eastmoney_920123.json`: Eastmoney company survey endpoint,
  `https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code=BJ<code>`.
  Ordinary unauthenticated GETs returned HTTP 200 at approximately 19:43 UTC.
  These are third-party provider labels, not official exchange name fields.
  Old code `BJ833819`, unmatched `BJ920999`, and wrong-market `BJ000001` returned
  empty `jbzl` lists in the same verification pass. Tests construct empty,
  malformed, mismatched, duplicate, and missing-English payloads separately.

The 2024 regulatory code-transition list demonstrates that prefix replacement is
unsafe (837023→920123, 831396→920496):
https://dataclouds.cninfo.com.cn/sjother/regulatory/2024/20241213/765a685e5d5545248d554c24fefc6684.pdf

The adapter deliberately does not load or apply that dated list. Current exact
provider metadata is not proof of historical ticker availability or a complete
current listing directory. Offline fixtures verify parser behavior; they do not
prove continued live endpoint reliability.
