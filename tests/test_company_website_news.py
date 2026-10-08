"""Company-scoped issuer excerpts retain every existing news evidence guard."""
from copy import deepcopy
from datetime import timedelta

import pytest

from tests.test_duckduckgo_news import END, NOW, START, article
from tradingagents.extensions import duckduckgo_news as ddg

pytestmark = pytest.mark.unit
TICKER = "601868.SS"


def website(host="www.ceec.net.cn", path="/"):
    return {
        "adapter_version": 1, "status": "resolved", "canonical_symbol": TICKER,
        "company_key": "SSE:601868",
        "scopes": [{"url": f"https://{host}{path}", "host": host,
                    "path_prefix": path, "kind": "company_website"}],
        "verified_at": (NOW - timedelta(hours=1)).isoformat(),
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "source": {"provider": "SSE", "url": "https://query.sse.com.cn/commonQuery.do",
                   "identity": TICKER}, "reason": None,
    }


def config(bundle=None, ticker=TICKER):
    return {"_company_website": bundle or website(), "_company_website_ticker": ticker,
            "duckduckgo_news_aliases": ["中国能建"]}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    ddg._CACHE.clear()
    monkeypatch.setattr(ddg, "_utcnow", lambda: NOW)
    monkeypatch.setattr(ddg, "_BLOCK_UNTIL", 0.0)
    yield
    ddg._CACHE.clear()


def search(monkeypatch, rows):
    calls = []
    def run(*args):
        calls.append(args[1])
        return deepcopy(rows)
    monkeypatch.setattr(ddg, "_search", run)
    return calls


def fetch(settings):
    return ddg.fetch_news(["中国能建"], START, END, settings)


def test_dynamic_scope_labels_issuer_without_widening_global_allowlist(monkeypatch):
    search(monkeypatch, [article(url="https://www.ceec.net.cn/news/contract.html")])
    before = ddg.NEWS_DOMAINS, ddg.DISCLOSURE_DOMAINS
    result = fetch(config())
    item = result["evidence"][0]
    assert item["source_category"] == "issuer_self_published"
    assert item["content_kind"] == "snippet"
    assert item["independently_verified"] is False
    assert item["issuer_website_verification"]["company_key"] == "SSE:601868"
    assert item["matched_aliases"] == ["中国能建"]
    assert before == (ddg.NEWS_DOMAINS, ddg.DISCLOSURE_DOMAINS)
    assert fetch(config(ticker="600519.SS"))["evidence"] == []
    assert fetch({"duckduckgo_news_aliases": ["中国能建"]})["evidence"] == []


@pytest.mark.parametrize("url", [
    "https://www.ceec.net.cn.evil.com/news/contract", "https://ceec.net.cn/news/contract",
    "https://other.ceec.net.cn/news/contract", "https://127.0.0.1/news/contract",
    "https://www.ceec.net.cn:444/news/contract", "https://user@www.ceec.net.cn/news/contract",
    "https://www.ceec.net.cn/news/contract?token=secret",
])
def test_company_scope_does_not_relax_url_guards(monkeypatch, url):
    search(monkeypatch, [article(url=url)])
    assert fetch(config())["evidence"] == []


@pytest.mark.parametrize("changes", [
    {"date": ""}, {"date": "2026-10-09"}, {"date": "2026-09-30"},
    {"excerpt": ""}, {"title": "Another company", "excerpt": "Unrelated company stock earnings"},
])
def test_company_scope_preserves_date_content_and_relevance_guards(monkeypatch, changes):
    search(monkeypatch, [article(url="https://www.ceec.net.cn/news/contract", **changes)])
    assert fetch(config())["evidence"] == []


def test_blocked_source_cannot_be_promoted_by_company_bundle(monkeypatch):
    search(monkeypatch, [article(url="https://medium.com/ceec/news")])
    assert fetch(config(website("medium.com", "/ceec")))["evidence"] == []


def test_expired_bundle_cannot_reuse_news_cache(monkeypatch):
    calls = search(monkeypatch, [article(url="https://www.ceec.net.cn/news/contract")])
    settings = config()
    assert len(fetch(settings)["evidence"]) == 1
    assert fetch(settings)["diagnostics"]["cache_hit"]
    monkeypatch.setattr(ddg, "_utcnow", lambda: NOW + timedelta(hours=2))
    expired = fetch(settings)
    assert expired["evidence"] == []
    assert not expired["diagnostics"]["cache_hit"]
    assert len(calls) == 2


def test_changed_scope_cannot_reuse_previous_domain_cache(monkeypatch):
    search(monkeypatch, [article(url="https://www.ceec.net.cn/news/contract")])
    assert fetch(config())["evidence"]
    assert fetch(config(website("www.newissuer.com")))["evidence"] == []


def test_scope_expiring_during_search_never_admits_issuer_row(monkeypatch):
    def delayed(*args):
        monkeypatch.setattr(ddg, "_utcnow", lambda: NOW + timedelta(hours=2))
        return [article(url="https://www.ceec.net.cn/news/contract")]
    monkeypatch.setattr(ddg, "_search", delayed)
    assert fetch(config())["evidence"] == []


def test_shared_host_path_boundary_and_result_limit(monkeypatch):
    search(monkeypatch, [
        article(url="https://www.q4web.com/tenant-two/news", title="中国能建 wrong tenant"),
        article(url="https://www.q4web.com/tenant/news", title="中国能建 one"),
        article(url="https://www.q4web.com/tenant/second", title="中国能建 two"),
    ])
    settings = config(website("www.q4web.com", "/tenant"))
    settings["duckduckgo_news_max_results"] = 1
    result = fetch(settings)
    assert [item["title"] for item in result["evidence"]] == ["中国能建 one"]
    assert result["diagnostics"]["result_limit_reached"]


@pytest.mark.parametrize("issuer_first", [True, False])
def test_syndicated_alternate_keeps_issuer_provenance(monkeypatch, issuer_first):
    issuer = article(url="https://www.ceec.net.cn/news/contract")
    media = article()
    search(monkeypatch, [issuer, media] if issuer_first else [media, issuer])
    result = fetch(config())
    assert len(result["evidence"]) == 1
    item = result["evidence"][0]
    assert len(item["alternate_sources"]) == 1
    issuer_item = item if issuer_first else item["alternate_sources"][0]
    assert issuer_item["source_category"] == "issuer_self_published"
    assert issuer_item["independently_verified"] is False
    assert issuer_item["issuer_website_verification"]["company_key"] == "SSE:601868"
