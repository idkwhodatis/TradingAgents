# Source-screened news fallback

The existing primary news vendor chain still runs first. The shared `get_news`
and `get_global_news` tools now have a separately configurable DuckDuckGo search
fallback. This serves news and sentiment analysts through the same tool in the
TUI, headless CLI and Python graph API. No paid API key is needed. Company website
validation uses pinned tldextract 5.4.0’s bundled offline Public Suffix List
(no runtime PSL downloads). Updating that dependency deliberately updates the snapshot.

## Behavior

- Known empty/unavailable primary results or primary errors trigger fallback.
  Yahoo carries a string-compatible evidence-count marker; Alpha Vantage's
  `feed` JSON is checked for usable dated entries. Arbitrary custom-vendor prose
  remains unchanged and unassessed: a nonempty string is not proof of evidence.
- A-share searches use the pinned Chinese and official/provider English names,
  prioritizing one query in each language. Other instruments use canonical
  ticker symbols or explicit descriptions for known commodities, indices,
  crypto and FX pairs. No company name is translated or guessed.
- Search discovery calls only DuckDuckGo's public search/news endpoints, not a DDGS
  mixed-engine `auto` backend. DuckDuckGo itself may syndicate other providers.
  This is an unofficial public search interface, so schema changes and access
  restrictions may make it unavailable.
- Returned URLs are independently screened against exact trusted hostnames and
  their subdomains, including worldwide established media and exchange/regulator
  sources. Unknown domains, user-generated platforms, misleading lookalikes,
  credentials in URLs and private/non-web targets are excluded. The publisher
  label supplied by search is not used to prove source identity.
- Instrument results must match a meaningful company name or acceptable exact
  ticker token in the title/excerpt. Generic short acronyms are insufficient.
  This is disclosed search relevance, not independent verification of a story.
- Each accepted item includes the actual search excerpt, source URL/domain,
  publisher, supplied publication time and date provenance, query and retrieval
  time. Excerpts are **not full article text**. Date-unknown, ambiguous, future
  and out-of-window items are withheld. An empty sample never proves no news.
- URLs are normalized and deduplicated; syndication/title duplicates are limited.
  A media article is not an official issuer statement. English text alone does
  not establish an independent overseas perspective.

## Per-company official website trust

When a company-news fallback is needed, a separate metadata adapter can verify
an issuer website from an exact exchange/regulator company profile. This never
adds the domain to the global publisher allowlist. Its bundle binds the exact
canonical instrument and provider identity to an exact hostname and path prefix,
with source provenance, `verified_at` and `expires_at` in UTC. Search-only domains,
provider-supplied publisher labels and Eastmoney profiles cannot establish this
trust. Unsupported markets and empty/invalid website fields remain explicitly
unavailable, while ordinary publisher news continues normally.

- Matching is exact-host only: no inferred apex, `www`, sibling or subdomain
  trust. Shared-hosting paths must remain inside the verified tenant path.
  Public/private suffix roots, blocked UGC platforms, credentials, IP addresses,
  localhost and non-web URLs remain excluded.
- Issuer excerpts are labelled `issuer_self_published`, with their website
  verification provenance and `independently_verified: false`. They remain
  search snippets; date, relevance, deduplication and result limits still apply.
- The metadata cache is bounded and company-specific. TTL is checked on use;
  expiration triggers an on-demand refresh during subsequent analysis, never a
  cron task. Refresh errors and changed issuer identities do not reuse stale
  scopes. News cache keys include the active company scope and verification
  period, preventing cross-company or expired-domain cache reuse.
- Only fixed exchange/regulator endpoints are fetched. Discovered issuer URLs
  are never visited, so their redirects cannot grant additional trust. A new
  host/path must appear in fresh official metadata before it is admitted.
- Current metadata does not establish historical domain ownership. Archived
  reports retain their dated provenance; they do not become a reusable allowlist.
  The pinned A-share identity remains immutable across frontends/checkpoints.

Current adapter coverage is deliberately narrow:

- SZSE A-shares: the exchange’s company-detail JSON explicitly identifies the
  A-share code, full/short names and company website. A changed name must agree
  with the run’s pinned identity before a website is trusted.
- US SEC operating-company filers: the SEC ticker map must identify one CIK,
  and the submissions document must repeat that exact CIK and ticker on a
  supported exchange. `website` and `investorWebsite` are used only when present
  and safe. Many real SEC profiles leave both blank; this is unavailable
  coverage, not a reason to infer a domain. Requests honor the existing
  `SEC_EDGAR_USER_AGENT` identification setting.
- SSE (including 601868), BSE, HKEX and other markets: no supported structured
  official website field is currently available to this adapter. No website
  trust is inferred from names, email addresses or search results. Existing
  publisher news and exchange/regulator disclosure screening remain available.

Set `company_website_enabled=False` (or
`TRADINGAGENTS_COMPANY_WEBSITE_ENABLED=false`) to disable this extra trust.
`company_website_timeout` defaults to 5 seconds and `company_website_cache_ttl`
to 86400 seconds. TTL zero disables cache storage and reuse, while a freshly
verified scope has a 120-second consumption lease for the current retrieval.
Metadata work shares the fallback’s bounded deadline.
The primary vendor path and global-news requests do not perform these lookups.

## Official A-share discovery

For a verified current A-share identity, one additional DuckDuckGo HTML query
looks for CNInfo disclosures. Only exact `static.cninfo.com.cn/finalpage/YYYY-MM-DD/ID.PDF`
URLs with matching company name **and** security code in the title/excerpt are
admitted. The date is explicitly `url_path`, precision `day`, not a verified
publication clock. The excerpt is retrieved; the PDF is **not fetched or read**.
Issuer authorship is unverified until the underlying document is checked.
This is bounded disclosure discovery, not complete historical announcement
coverage or an SSE/SZSE/BSE filing API. It never calls the BSE announcement API.

News fallback runs before optional official discovery, so an unavailable official
search cannot discard already-retrieved news. They share the per-call query
allowance: with at least two slots, one is reserved for official discovery when
applicable; a one-query allowance prioritizes news. A challenge, 403,
429 or redirect stops further same-service requests in that call. No CAPTCHA
solving, proxy rotation, browser switching or access-control bypass is used.
Repeated successful query/range/settings combinations use a bounded in-process
cache. Neither cache contents nor successful search results mutate the pinned
identity snapshot. Tool messages and resulting analyst reports follow the
existing checkpoint and report-storage paths. Changing evidence settings
invalidates the run signature; resuming a completed tool result does not rewrite
its evidence.

## Configuration

Defaults in `DEFAULT_CONFIG`:

```python
{
    "duckduckgo_news_enabled": True,
    "ashare_announcements_enabled": True,
    "duckduckgo_news_min_interval": 3.0, # seconds between DDG request starts; 0..30
    "duckduckgo_news_pause_hours": 6.0,  # persistent pause after block; 1..168
    "duckduckgo_news_timeout": 15.0,      # seconds per bounded request
    "duckduckgo_news_total_timeout": 45,  # shared official + news search budget
    "duckduckgo_news_max_queries": 2,    # shared per call; 1..8
    "duckduckgo_news_max_results": 10,   # 1..30 per retrieval category
    "duckduckgo_news_cache_ttl": 300,    # seconds; 0 disables cache
    "duckduckgo_news_region": "auto",   # Chinese query cn-zh; otherwise us-en
    "duckduckgo_news_allowed_domains": [],
}
```

Additional trusted publisher/issuer domains can be configured explicitly after
verifying who owns them. They are labelled `configured_trusted_domain`, never
silently promoted to official issuer sources. Existing blocked user-generated
platforms remain excluded. The default allowlist favors precision over coverage;
relevant stories can be missed. Numeric settings are bounded and invalid inputs
return an explicit unavailable/invalid-request result rather than querying
without limits. The shared search deadline covers official discovery and news fallback together;
it does not include the configured primary vendor or the rest of the analysis.
Budget exhaustion is distinguished from provider blocking and empty results.

Set `TRADINGAGENTS_DUCKDUCKGO_NEWS_ENABLED=false` and
`TRADINGAGENTS_ASHARE_ANNOUNCEMENTS_ENABLED=false` to restore the primary-only
path in both CLI modes. All the search settings above also support environment
overrides: `TRADINGAGENTS_DUCKDUCKGO_NEWS_MAX_QUERIES`, `_MAX_RESULTS`, `_TIMEOUT`,
`_TOTAL_TIMEOUT`, `_MIN_INTERVAL`, `_PAUSE_HOURS`, `_CACHE_TTL`, `_REGION`, and `_ALLOWED_DOMAINS`
(each shorthand uses the same `TRADINGAGENTS_DUCKDUCKGO_NEWS` prefix).
Allowed domains use a comma-separated list of plain ASCII hostnames, for example
`reuters.com,apnews.com`, without schemes, paths, wildcards or empty entries.
An empty environment value keeps the built-in default. Invalid numeric types or
malformed lists fail loudly; bounded search validation still applies.

The query cap is shared with optional A-share announcement discovery. When both
fallback news and verified A-share announcement discovery run, the default cap of
2 reserves one query for Chinese news and one for announcements. Set
`TRADINGAGENTS_DUCKDUCKGO_NEWS_MAX_QUERIES=3` for Chinese + English news plus the
announcement query, or disable announcements to use both of the default two
queries for news. A cap of 1 prioritizes news and skips optional announcements.
These are bounded search attempts, not guaranteed evidence coverage.

Pacing is process-shared across news and announcement calls, including repeated
agent tool calls. It spaces **every HTTP request start**, including the token
lookup and the subsequent news request for one query. A query therefore may use
two requests; `MAX_QUERIES` counts queries, not HTTP requests. Waiting consumes
the shared search budget, and no wait is added after the last request. When the
next permitted start cannot fit within the budget, the search stops. Provider
blocks/challenges stop the remaining work without automatic retries; an active
pause also stops without waiting. Lower query counts and pacing reduce request
bursts but cannot guarantee that DuckDuckGo will admit requests. No live-service
success should be inferred from deterministic offline tests.
No primary price/fundamental vendor, signal logic or trade execution is changed.

Run deterministic checks with `pytest -q`; ordinary tests prohibit network.
A live smoke must be run separately and should report both admitted evidence
and access/protocol failures, without describing fixture results as live news.

### Standalone live diagnostic (no LLM or paid key)

From an installed checkout / its Python environment:

```sh
python -m tradingagents.extensions.news_diagnostics --query 中国能建 --days 7
```

This explicitly calls only DuckDuckGo news, with one query, up to three admitted
items and a 45-second search budget. It does not invoke Yahoo, Alpha Vantage,
LLMs or official-disclosure discovery. JSON output includes the date window,
HTTP status/content type/timing, raw hit counts, rejection reasons and admitted
source URLs/publication metadata. A successful empty response is distinct from
a transport error or block. It is a finite recent search sample, not an archive.
Do not widen the analysis window just to manufacture evidence; an explicitly
chosen wider diagnostic window can test connectivity but proves no coverage in
the original window. Do not retry or switch endpoints to evade a challenge.

The deadline uses monotonic checks, bounded socket timeouts and streaming-body
checks with read-time headroom. It is not an OS-level hard kill: unusual DNS or
HTTP-header stalls can exceed what Requests can interrupt. No background request
thread is left running when the helper returns.


### Persistent DuckDuckGo pause

A challenge, HTTP 202/401/403/429, or redirect immediately stops DuckDuckGo
requests and records a **6-hour local pause**. Configure
`duckduckgo_news_pause_hours` or `TRADINGAGENTS_DUCKDUCKGO_NEWS_PAUSE_HOURS`
with a finite value from **1 through 168 hours**. Six hours is a conservative
local default, not a server-provided retry time or a guarantee of admission.
A shorter setting in another process cannot shorten an existing pause.

News analysis, official-announcement search and the standalone diagnostic share
`$XDG_CACHE_HOME/tradingagents/ddg-block.json`, or
`~/.cache/tradingagents/ddg-block.json` when XDG_CACHE_HOME is unset. Use the same
OS user and cache home for processes that must share it. The state contains only
schema/provider markers, block time, expiry and a sanitized reason: no query,
response, credential, token or company data. The directory is owner-only and
state writes are atomic with owner-only permissions. A file lock serializes
requests and updates across processes, preventing a waiting process from making
an HTTP request after another process records a block. Existing in-process
pacing and the shared request budget still apply; lock waiting spends that budget.

While paused, subsequent DuckDuckGo calls make no HTTP requests. Previously
cached evidence may still be returned without HTTP. Expiry only permits the next
user-triggered call; it does not start a probe, retry, background job or request,
and does not mean DuckDuckGo has unblocked the connection. There is no force flag.
The pause is local to this user/cache home, not an IP-wide lock across machines.
Linux/Pi and macOS use directory-descriptor operations and `flock`. Windows
uses the Windows-only `pywin32` dependency for native ACLs, handle-based checks
and byte-range locks. A separate short metadata lock coordinates JSON reads
and replacement, so status reads do not wait for an HTTP request and an open
reader does not interfere with native replacement. New Windows objects have a protected DACL granting access
only to the current user; `chmod` is not treated as a Windows privacy check.
The store rejects reparse points/junctions, hard-linked files, unsafe existing
permissions, UNC/device paths and filesystems without persistent ACLs. Checked
ancestors remain open against rename while their paths are used. Other supported
callers still share the same JSON schema and pause semantics. Missing native
support fails closed with `pause_state_unsupported_platform`, without falling
back to process memory. Primary news/data vendors are unaffected.

Inspect the pause without contacting any provider:

```sh
python -m tradingagents.extensions.news_diagnostics --status
```

Both search results and reports expose the pause reason and expiry in diagnostic
metadata. Corrupt, unreadable, oversized or unsafe state fails closed with
`pause_state_unavailable`; clock rollback before the recorded block time also
fails closed. Inspect the local clock, ownership/permissions and state file before
running another analysis. The program does not delete or silently reset invalid
state. Status inspection never rewrites or extends a pause.

A one-byte durable marker in `ddg-block.lock` is set before each HTTP request and
cleared after its outcome is safely handled. If a process is killed during a
request or cannot persist a block, the marker remains and later processes fail
closed (`pause_state_unavailable`), even when the JSON file is absent or expired.
A live request is distinguished by its held lock; other callers wait within
their existing budget and recheck the pause after acquiring the gate. Ordinary
handled network errors do not leave this marker.
An interrupted/failed-write marker requires inspection rather than automatic
expiry or repair; restarting alone does not bypass it. No state is automatically
deleted to recover. These protections require a local filesystem and native locking/atomic rename.
On Windows, file data and the request marker are flushed using native handles;
Windows has no supported directory-handle equivalent of POSIX directory fsync,
so first-use directory metadata power-loss durability is not guaranteed. Process
interruption and failed state writes remain fail-closed. Shared network
filesystems are not a guaranteed cross-machine coordination mechanism.

Windows verification is an offline CI job on native Windows Python 3.12 and 3.14.
It exercises real ACLs, junction rejection, atomic replacement and subprocess
locks, with HTTP mocked. Linux fixture tests alone do not establish Windows
runtime compatibility.
