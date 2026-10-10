"""yfinance-based news data fetching functions."""

import contextlib
from datetime import UTC, datetime

import yfinance as yf
from dateutil.relativedelta import relativedelta

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import coverage_gap, in_window
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import yf_retry
from tradingagents.extensions.ashare_identity import identity_for, news_queries
from tradingagents.extensions.canadian_market import canadian_exchange
from tradingagents.extensions.news_evidence import NewsText


def _extract_article_data(article: dict) -> dict:
    """Extract article data from yfinance news format (handles nested 'content' structure).

    Yahoo sends some fields as null rather than leaving them out, so each falls
    back to its default on a null as well as on a missing key (#1458).
    """
    content = article.get("content")
    if isinstance(content, dict):
        url_obj = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
        pub_date_str = content.get("pubDate") or ""
        pub_date = None
        if pub_date_str:
            with contextlib.suppress(ValueError, AttributeError):
                pub_date = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))

        return {
            "title": content.get("title") or "No title",
            "summary": content.get("summary") or "",
            "publisher": (content.get("provider") or {}).get("displayName") or "Unknown",
            "link": url_obj.get("url") or "",
            "pub_date": pub_date,
        }
    # Fallback for flat structure. Parse the epoch publish time so flat
    # articles are date-filterable too (otherwise they bypass the
    # historical window and leak future news, #992/#1007).
    pub_date = None
    ts = article.get("providerPublishTime")
    if ts:
        # Epoch seconds are UTC; parse them as UTC-aware so filtering does
        # not shift with the host timezone (#1126).
        with contextlib.suppress(ValueError, OSError, TypeError):
            pub_date = datetime.fromtimestamp(ts, tz=UTC)
    return {
        "title": article.get("title") or "No title",
        "summary": article.get("summary") or "",
        "publisher": article.get("publisher") or "Unknown",
        "link": article.get("link") or "",
        "pub_date": pub_date,
    }


def _search_news(canonical: str, limit: int) -> tuple[list, bool]:
    """Yahoo search's articles tagged with ``canonical``, and whether it knows the symbol.

    Search answers when the quote feed is empty, but its articles are a
    relevance-picked sample, and some are tagged loosely; only those tagged with
    the symbol are about it.
    """
    # Names come only from this run's exchange-verified identity snapshot. They
    # improve discovery, never attribution: even a perfect name/title match
    # must carry Yahoo's exact canonical relatedTicker before we include it.
    queries = list(dict.fromkeys([canonical, *news_queries(canonical)]))
    tagged = []
    seen = set()
    answered = False
    for query in queries:
        try:
            search = yf_retry(lambda q=query: yf.Search(q, news_count=limit))
        except VendorUnavailableError:
            if query == canonical:
                raise  # Preserve the existing primary-query vendor fallback.
            continue  # Optional name enrichment must not discard valid results.
        if search is None:
            continue
        knows = any((q.get("symbol") or "").upper() == canonical for q in search.quotes or [])
        answered = answered or knows or bool(search.news)
        for article in search.news or []:
            tickers = article.get("relatedTickers") or []
            if canonical not in (t.upper() for t in tickers if isinstance(t, str)):
                continue
            # Preserve other markets' single-query behavior. Across A-share
            # name queries, the same article can appear repeatedly or in both
            # Yahoo formats, so use the id/link before a content fingerprint.
            if len(queries) > 1:
                data = _extract_article_data(article)
                content = article.get("content") or {}
                article_id = article.get("uuid") or article.get("id")
                if isinstance(content, dict):
                    article_id = article_id or content.get("id")
                keys = set()
                if article_id:
                    keys.add(("id", article_id))
                if data["link"]:
                    keys.add(("link", data["link"]))
                if not keys:
                    keys.add(("content", data["title"], data["publisher"], data["pub_date"]))
                duplicate = bool(seen.intersection(keys))
                seen.update(keys)
                if duplicate:
                    continue
            tagged.append(article)
    return tagged, answered


def _format_articles(articles: list[dict]) -> str:
    out = ""
    for data in articles:
        out += f"### {data['title']} (source: {data['publisher']})\n"
        if data["summary"]:
            out += f"{data['summary']}\n"
        if data["link"]:
            out += f"Link: {data['link']}\n"
        out += "\n"
    return out


def get_news_yfinance(
    ticker: str,
    start_date: str,
    end_date: str,
) -> str:
    """News about ``ticker`` published in the window, from Yahoo Finance.

    The symbol's quote feed comes first. When it is empty, Yahoo search serves
    the articles tagged with the symbol; when it has none, or neither answers,
    Yahoo news is unavailable, which is not an absence of news, and a configured
    next vendor is tried. Search results are a sample, so finding none in the
    window never reads as "no news".
    """
    config = get_config()
    article_limit = config["news_article_limit"]
    # Query Yahoo with the canonical symbol, like every other yfinance path —
    # a raw broker/forex/crypto alias (XAUUSD, BTCUSD) otherwise silently
    # returns no news. Keep the user's ticker in the report header.
    canonical = normalize_symbol(ticker)
    identity = identity_for(canonical, config=config)
    verified_ashare = bool(
        identity and identity.get("status") == "resolved"
        and identity.get("security_type") == "A-share" and identity.get("confidence") == "verified"
    )
    coverage_note = (
        "\n\nA-share coverage note: exchange identity and its provenance concern company "
        "identification only. They do not establish Yahoo/overseas news coverage. "
        "This feed may be incomplete and does not replace official exchange/issuer announcements."
        if verified_ashare else ""
    )
    if canadian_exchange(canonical):
        coverage_note += ("\n\nCanadian coverage note: exact .TO/.V listing retained. "
                          "Yahoo news coverage can be incomplete, especially TSXV and funds; "
                          "this is not a complete SEDAR+/issuer disclosure search. "
                          "No US namesake or unsuffixed ticker is substituted.")
    resolved = "" if canonical == ticker else f" (resolved to {canonical})"
    subject = f"news for {ticker}{resolved}"
    feed = yf_retry(lambda: yf.Ticker(canonical).get_news(count=article_limit)) or []
    articles = [_extract_article_data(a) for a in feed]
    if not feed:
        found, answered = _search_news(canonical, article_limit)
        if not answered:
            raise VendorUnavailableError(
                f"Yahoo Finance returned no news response for {canonical}{coverage_note}"
            )
        if not found:
            # With the quote feed empty, no tagged article says nothing about the
            # company's news (Yahoo tags no article with most non-US listings).
            raise VendorUnavailableError(
                f"Yahoo Finance has no news articles tagged {canonical}{coverage_note}"
            )
        articles = [_extract_article_data(a) for a in found]

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    # Keep only articles within the requested window (look-ahead safe).
    in_range = [a for a in articles if in_window(a["pub_date"], start_dt, end_dt)]
    if verified_ashare:
        # The budget counts usable articles, not the candidates accumulated
        # across bilingual searches. Out-of-window hits cannot spend it.
        in_range = in_range[:article_limit]
    if in_range:
        return NewsText(
            f"## {ticker}{resolved} News, from {start_date} to {end_date}:\n\n"
            f"{_format_articles(in_range)}{coverage_note}",
            sum(bool(a["pub_date"] and a["link"] and a["summary"] and a["title"] != "No title") for a in in_range),
        )

    if not feed:
        return NewsText(f"<Yahoo Finance news unavailable for {start_date}..{end_date}: Yahoo search "
                f"returns only a sample of recent articles about {canonical}, so this is not "
                f"an absence of {subject}>{coverage_note}")
    gap = coverage_gap(
        (a["pub_date"] for a in articles),
        start_date, end_date, "Yahoo Finance news", subject,
    )
    result = gap or f"No news found for {ticker}{resolved} between {start_date} and {end_date}"
    return NewsText(result + coverage_note)


def get_global_news_yfinance(
    as_of_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """
    Retrieve global/macro economic news using yfinance Search.

    Args:
        as_of_date: Current date in yyyy-mm-dd format
        look_back_days: Number of days to look back. ``None`` falls back to
            ``global_news_lookback_days`` from the active config.
        limit: Maximum number of articles to return. ``None`` falls back to
            ``global_news_article_limit`` from the active config.

    Returns:
        Formatted string containing global news articles
    """
    config = get_config()
    if look_back_days is None:
        look_back_days = config["global_news_lookback_days"]
    if limit is None:
        limit = config["global_news_article_limit"]
    search_queries = config["global_news_queries"]

    curr_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    start_dt = curr_dt - relativedelta(days=look_back_days)
    start_date = start_dt.strftime("%Y-%m-%d")

    in_window_news = []
    seen_titles = set()

    for query in search_queries:
        found = yf_retry(lambda q=query: yf.Search(
            query=q,
            news_count=limit,
            enable_fuzzy_query=True,
        ).news)

        for article in found or []:
            # Window first: the limit counts what the run may read, so an
            # out-of-window article must not spend the budget or cut the
            # remaining searches short (#1356). Flat articles are filtered
            # on the same rule, so none can leak future news (#1007).
            data = _extract_article_data(article)
            if not in_window(data["pub_date"], start_dt, curr_dt):
                continue
            if data["title"] and data["title"] not in seen_titles:
                seen_titles.add(data["title"])
                in_window_news.append(data)

        if len(in_window_news) >= limit:
            break

    news_str = _format_articles(in_window_news[:limit])

    # Nothing fell inside the window -> say so rather than return an
    # empty-bodied report (#993).
    if not news_str:
        # Results merge several fuzzy searches, so their timestamps prove no
        # continuous coverage; judge the window against the present only.
        gap = coverage_gap((), start_date, as_of_date, "Yahoo Finance global news", "market news")
        return NewsText(gap or f"No global news found between {start_date} and {as_of_date}")

    return NewsText(
        f"## Global Market News, from {start_date} to {as_of_date}:\n\n{news_str}",
        sum(bool(a["pub_date"] and a["link"] and a["summary"] and a["title"] != "No title") for a in in_window_news[:limit]),
    )
