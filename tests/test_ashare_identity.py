"""Official payload fixtures; never contact market services or model APIs."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
import requests

from tradingagents.dataflows.config import run_config
from tradingagents.extensions import ashare_identity as adapter

FIXTURES = Path(__file__).parent / "fixtures" / "ashare_identity"


@pytest.fixture(autouse=True)
def clear_cache():
    adapter._CACHE.clear()
    yield
    adapter._CACHE.clear()


@pytest.fixture
def official(monkeypatch):
    calls = []
    sse = json.loads((FIXTURES / "sse_600519.json").read_text())
    szse = json.loads((FIXTURES / "szse_000001.json").read_text())

    def get(url, params, referer, timeout):
        calls.append((url, deepcopy(params), referer, timeout))
        if url == adapter.SSE_URL:
            return deepcopy(sse if params["COMPANY_CODE"] == "600519" else {"result": []})
        if url == adapter.YAHOO_SEARCH_URL:
            return {"quotes": []}
        assert url == adapter.SZSE_URL
        result = deepcopy(szse)
        if params["txtDMorJC"] != "000001":
            result[0]["data"] = []
        return result

    monkeypatch.setattr(adapter, "_get_json", get)
    return calls


def test_bare_equity_resolves_actual_metadata_and_does_not_mutate_config(official):
    config = {"data_vendors": {"news": "yfinance"}, "header": {"Authorization": "SECRET"}}
    original = deepcopy(config)
    symbol, run = adapter.prepare_instrument("600519", "stock", config)
    assert symbol == "600519.SS"
    assert config == original
    identity = adapter.identity_for(symbol, run)
    assert identity["chinese_full_name"] == "贵州茅台酒股份有限公司"
    assert identity["chinese_short_name"] == "贵州茅台"
    assert identity["english_name"] == "Kweichow Moutai Co.,Ltd."
    assert identity["english_name_kind"] == "official"
    assert identity["security_type"] == "A-share"
    assert identity["exchange"] == "SSE"
    assert identity["confidence"] == "verified"
    assert identity["identity_temporality"] == "current"
    assert {a["source"] for a in identity["aliases"]} == {"SSE"}
    assert "SECRET" not in json.dumps(identity)
    assert len(official) == 2
    assert all(call[3] == 5 for call in official)


def test_bare_stock_scope_bank_vs_explicit_shanghai_index(official):
    symbol, run = adapter.prepare_instrument("000001", "stock", {})
    assert symbol == "000001.SZ"
    bank = adapter.identity_for(symbol, run)
    assert bank["chinese_short_name"] == "平安银行"
    assert bank["chinese_full_name"] is None
    assert bank["english_name"] is None
    assert bank["english_name_kind"] is None
    assert "english_name unavailable" in " ".join(bank["coverage"])
    explicit, other = adapter.prepare_instrument("000001.SS", "stock", run)
    assert explicit == "000001.SS"
    assert adapter.identity_for(explicit, other)["status"] == "not_a_share"
    assert "平安银行" not in adapter.render_identity(other["_ashare_identity"])


@pytest.mark.parametrize("symbol", ["NVDA", "0700.HK", "000001.HK", "BTC-USD", "^GSPC", "SPY"])
def test_other_markets_never_fetch_and_remove_old_snapshot(symbol, monkeypatch):
    monkeypatch.setattr(adapter, "_get_json", lambda *a, **k: pytest.fail("Unrelated market fetched"))
    output, config = adapter.prepare_instrument(symbol, "stock", {"_ashare_identity": {"stale": True}})
    assert output == symbol
    assert "_ashare_identity" not in config


def test_crypto_mode_never_enters_stock_universe(monkeypatch):
    monkeypatch.setattr(adapter, "_get_json", lambda *a, **k: pytest.fail("Crypto fetched"))
    assert adapter.prepare_instrument("600519", "crypto", {}) == ("600519", {})


@pytest.mark.parametrize("symbol", ["510300.SS", "159915.SZ", "399001.SZ", "600519.SZ"])
def test_etf_index_and_wrong_exchange_not_enriched_or_rewritten(symbol, official):
    output, config = adapter.prepare_instrument(symbol, "stock", {})
    assert output == symbol
    assert config["_ashare_identity"]["status"] == "not_a_share"
    assert config["_ashare_identity"]["security_type"] is None
    assert config["_ashare_identity"]["english_name"] is None


def test_sh_alias_resolves_same_snapshot(official):
    output, config = adapter.prepare_instrument("600519.sh", "stock", {})
    assert output == "600519.SS"
    assert adapter.identity_for("600519.SH", config)["chinese_short_name"] == "贵州茅台"
    assert len(official) == 1


@pytest.mark.parametrize("symbol", ["920001.BJ", "830799.BJ"])
def test_bse_is_explicit_unknown_without_unverified_requests(symbol, monkeypatch):
    monkeypatch.setattr(adapter, "_get_json", lambda *a, **k: pytest.fail("BSE not supported"))
    output, config = adapter.prepare_instrument(symbol, "stock", {})
    assert output == symbol
    assert config["_ashare_identity"]["status"] == "unavailable"
    assert config["_ashare_identity"]["confidence"] == "unknown"


@pytest.mark.parametrize("symbol", ["920001", "830799", "510300", "123456"])
def test_unverified_bare_code_fails_before_vendor_analysis(symbol, official):
    with pytest.raises(ValueError, match="Cannot uniquely verify"):
        adapter.prepare_instrument(symbol, "stock", {})


def test_duplicate_official_equity_matches_fail_closed(official, monkeypatch):
    monkeypatch.setattr(adapter, "_szse", lambda code, timeout: {"canonical_symbol": f"{code}.SZ", "exchange": "SZSE"})
    with pytest.raises(ValueError, match="Cannot uniquely verify"):
        adapter.prepare_instrument("600519", "stock", {})


def test_failure_on_one_exchange_blocks_bare_and_no_stale_fallback(official, monkeypatch):
    adapter.prepare_instrument("600519", "stock", {})
    monkeypatch.setattr(adapter, "_sse", lambda *a: (_ for _ in ()).throw(requests.Timeout("SECRET")))
    with pytest.raises(ValueError, match="Cannot uniquely verify") as error:
        adapter.prepare_instrument("600519", "stock", {"ashare_identity_cache_ttl": 0})
    assert "SECRET" not in str(error.value)
    _, config = adapter.prepare_instrument("600519.SS", "stock", {"ashare_identity_cache_ttl": 0})
    assert config["_ashare_identity"]["status"] == "unavailable"
    assert config["_ashare_identity"]["chinese_full_name"] is None
    assert "SECRET" not in json.dumps(config)


def test_success_cache_ttl_and_copy_isolation(official, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(adapter.time, "monotonic", lambda: clock[0])
    _, first = adapter.prepare_instrument("600519.SS", "stock", {"ashare_identity_cache_ttl": 10})
    first["_ashare_identity"]["aliases"].clear()
    _, second = adapter.prepare_instrument("600519.SS", "stock", {"ashare_identity_cache_ttl": 10})
    assert second["_ashare_identity"]["aliases"]
    assert len(official) == 1
    clock[0] += 11
    adapter.prepare_instrument("600519.SS", "stock", {"ashare_identity_cache_ttl": 10})
    assert len(official) == 2


def test_bounded_cache(monkeypatch):
    monkeypatch.setattr(adapter, "_sse", lambda *a: None)
    for number in range(adapter._CACHE_LIMIT + 5):
        adapter._lookup("SS", str(number).zfill(6), 1, 30)
    assert len(adapter._CACHE) == adapter._CACHE_LIMIT


def test_provider_failures_are_not_cached(monkeypatch):
    calls = []
    def fail(*args):
        calls.append(args)
        raise requests.Timeout("secret")
    monkeypatch.setattr(adapter, "_sse", fail)
    for _ in range(2):
        adapter.prepare_instrument("600519.SS", "stock", {})
    assert len(calls) == 2
    assert not adapter._CACHE


def test_parser_rejects_cdr_and_missing_schema(monkeypatch):
    monkeypatch.setattr(adapter, "_get_json", lambda *a: {"result": [{"A_STOCK_CODE": "689009", "SEC_TYPE": "科创CDR"}]})
    assert adapter._sse("689009", 1) is None
    monkeypatch.setattr(adapter, "_get_json", lambda *a: {"result": None})
    with pytest.raises(ValueError, match="Unexpected SSE"):
        adapter._sse("600519", 1)
    monkeypatch.setattr(adapter, "_get_json", lambda *a: [{"metadata": {"name": "B股列表", "tabkey": "tab1"}, "data": []}])
    with pytest.raises(ValueError, match="Unexpected SZSE"):
        adapter._szse("000001", 1)


def test_request_timeout_http_error_no_unbounded_retry(monkeypatch):
    calls = []
    def get(*args, **kwargs):
        calls.append((args, kwargs))
        raise requests.Timeout("Sensitive URL")
    monkeypatch.setattr(adapter.requests, "get", get)
    _, config = adapter.prepare_instrument("600519.SS", "stock", {"ashare_identity_timeout": 0.5})
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 0.5
    assert "Sensitive URL" not in json.dumps(config)


@pytest.mark.parametrize("key,value", [("ashare_identity_timeout", 0), ("ashare_identity_timeout", 31),
                                       ("ashare_identity_cache_ttl", -1), ("ashare_identity_cache_ttl", float("nan")),
                                       ("ashare_identity_enabled", "false")])
def test_invalid_adapter_settings(key, value):
    with pytest.raises(ValueError, match=key):
        adapter.prepare_instrument("600519", "stock", {key: value})


def test_disabled_does_not_request_or_guess(monkeypatch):
    monkeypatch.setattr(adapter, "_get_json", lambda *a: pytest.fail("Disabled adapter fetched"))
    assert adapter.prepare_instrument("600519.SH", "stock", {"ashare_identity_enabled": False})[0] == "600519.SS"
    with pytest.raises(ValueError, match="verified exchange"):
        adapter.prepare_instrument("600519", "stock", {"ashare_identity_enabled": False})


def test_snapshot_reuse_query_context_and_cross_market_isolation(official):
    symbol, config = adapter.prepare_instrument("600519", "stock", {})
    calls = len(official)
    adapter.prepare_instrument(symbol, "stock", config)
    assert len(official) == calls
    with run_config(config):
        assert adapter.news_queries(symbol) == ["600519.SS", "贵州茅台 600519", "贵州茅台酒股份有限公司 600519", "Kweichow Moutai Co.,Ltd. 600519"]
        assert adapter.news_queries("NVDA") == ["NVDA"]
    assert adapter.news_queries(symbol) == [symbol]
    rendered = adapter.render_identity(config["_ashare_identity"])
    assert "Current names may differ" in rendered
    assert "Official announcements are not retrieved" in rendered
    assert "independent perspectives" in rendered


def test_signature_observation_time_stable_names_changed(official):
    _, config = adapter.prepare_instrument("600519", "stock", {})
    old = adapter.signature_identity(config)
    config["_ashare_identity"]["retrieved_at"] = "tomorrow"
    assert adapter.signature_identity(config) == old
    config["_ashare_identity"]["english_name"] = "New sourced name"
    assert adapter.signature_identity(config) != old


def test_china_energy_short_full_english_and_queries(monkeypatch):
    payload = json.loads((FIXTURES / "sse_601868.json").read_text())
    monkeypatch.setattr(adapter, "_get_json", lambda *a: deepcopy(payload))
    ticker, config = adapter.prepare_instrument("601868.SH", "stock", {})
    identity = config["_ashare_identity"]
    assert ticker == "601868.SS"
    assert identity["chinese_short_name"] == "中国能建"
    assert identity["chinese_full_name"] == "中国能源建设股份有限公司"
    assert identity["english_name"] == "China Energy Engineering Corporation Limited"
    assert {a["name"] for a in identity["aliases"]} >= {"中国能建", "中国能源建设股份有限公司"}
    rendered = adapter.render_identity(identity)
    assert "中国能建" in rendered and "中国能源建设股份有限公司" in rendered
    with run_config(config):
        assert adapter.news_queries(ticker) == ["601868.SS", "中国能建 601868", "中国能源建设股份有限公司 601868", "China Energy Engineering Corporation Limited 601868"]
    assert identity["retrieval_queries"]["official_announcements"] == {
        "queries": ["中国能建 601868 公告 site:sse.com.cn"], "evidence_retrieved": False}


@pytest.mark.parametrize("changes", [
    {"symbol": "000001.SS"}, {"quoteType": "INDEX"}, {"exchange": "SHH"},
    {"longname": "平安银行", "shortname": "平安银行"}, {"longname": None, "shortname": None},
])
def test_provider_english_rejects_wrong_security_or_non_english(monkeypatch, changes):
    quote = {"symbol": "000001.SZ", "quoteType": "EQUITY", "exchange": "SHZ",
             "longname": "PING AN BANK CO LTD", "shortname": "PING AN BANK"}
    quote.update(changes)
    monkeypatch.setattr(adapter, "_get_json", lambda *args: {"quotes": [quote]})
    assert adapter._provider_english("000001.SZ", 1) is None


def test_szse_preserves_verified_provider_english_as_nonofficial(official, monkeypatch):
    monkeypatch.setattr(adapter, "_provider_english", lambda *args: {
        "name": "PING AN BANK CO LTD", "source": {"provider": "Yahoo Finance", "url": "https://finance.yahoo.com/quote/000001.SZ/"}})
    symbol, config = adapter.prepare_instrument("000001", "stock", {})
    identity = config["_ashare_identity"]
    assert identity["chinese_short_name"] == "平安银行"
    assert identity["english_name"] == "PING AN BANK CO LTD"
    assert identity["english_name_kind"] == "provider_label"
    assert identity["chinese_full_name"] is None
    assert identity["aliases"][-1] == {"name": "PING AN BANK CO LTD", "language": "en", "kind": "provider_label", "source": "Yahoo Finance"}
    assert [source["provider"] for source in identity["sources"]] == ["SZSE", "Yahoo Finance"]
    with run_config(config):
        assert adapter.news_queries(symbol) == ["000001.SZ", "平安银行 000001", "PING AN BANK CO LTD 000001"]


def test_szse_keeps_chinese_identity_when_english_rate_limited(official, monkeypatch):
    def limited(*args):
        raise requests.HTTPError("429 secret header")
    monkeypatch.setattr(adapter, "_provider_english", limited)
    _, config = adapter.prepare_instrument("000001.SZ", "stock", {})
    identity = config["_ashare_identity"]
    assert identity["status"] == "resolved"
    assert identity["chinese_short_name"] == "平安银行"
    assert identity["english_name"] is None
    assert "secret" not in json.dumps(identity)


def test_environment_configuration_available_to_both_cli_modes(monkeypatch):
    from tradingagents.default_config import build_default_config
    monkeypatch.setenv("TRADINGAGENTS_ASHARE_IDENTITY_ENABLED", "false")
    monkeypatch.setenv("TRADINGAGENTS_ASHARE_IDENTITY_TIMEOUT", "2.5")
    monkeypatch.setenv("TRADINGAGENTS_ASHARE_IDENTITY_CACHE_TTL", "60")
    config = build_default_config()
    assert config["ashare_identity_enabled"] is False
    assert config["ashare_identity_timeout"] == 2.5
    assert config["ashare_identity_cache_ttl"] == 60
