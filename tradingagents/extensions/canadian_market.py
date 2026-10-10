"""Small Canadian listing adapter; no new vendor, API key or symbol guessing.

Yahoo's .TO/.V symbols identify listings, not issuers or quote currencies. Keep
this policy outside core agents so syncing upstream stays inexpensive.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

_PREFIX = re.compile(r"^(TSX|TSXV):([A-Z0-9][A-Z0-9.-]*)$")
_NATIVE_PREFERRED = re.compile(r"^[A-Z0-9][A-Z0-9-]*\.P[RF]\.[A-Z]+$")
_PREFERRED = re.compile(r"^([A-Z0-9][A-Z0-9-]*)\.PR\.([A-Z]+)$")
_LISTING = re.compile(r"^([A-Z0-9][A-Z0-9-]*)\.(TO|V)$")


def normalize_canadian_symbol(value: str) -> str | None:
    """Accept explicit exchange prefixes/suffixes only; never guess bare RY."""
    value = value.strip().upper()
    if value.startswith(("TSX:", "TSXV:")):
        match = _PREFIX.fullmatch(value)
        if not match:
            raise ValueError("Use TSX:RY / TSXV:RCK or Yahoo RY.TO / RCK.V")
        exchange, code = match.groups()
        suffix = "TO" if exchange == "TSX" else "V"
        # In native preferred notation the final V is a series, not TSXV.
        if not _NATIVE_PREFERRED.fullmatch(code) and code.endswith((".TO", ".V")):
            code, supplied = code.rsplit(".", 1)
            if supplied != suffix:
                raise ValueError("Canadian exchange prefix and suffix disagree")
    elif value.endswith((".TO", ".V")):
        if _NATIVE_PREFERRED.fullmatch(value):
            raise ValueError("Ambiguous preferred-share notation; use an explicit TSX: prefix or Yahoo symbol")
        code, suffix = value.rsplit(".", 1)
    else:
        return None
    preferred = _PREFERRED.fullmatch(code)
    if preferred:
        if suffix != "TO":
            raise ValueError("Native preferred-share notation requires TSX; use the exact Yahoo symbol")
        # Yahoo spells TSX .PR.<series> as -P<series>, e.g. ENB.PR.V -> ENB-PV.TO.
        code = f"{preferred[1]}-P{preferred[2]}"
    elif {"PR", "PF"}.intersection(code.split(".")):
        raise ValueError("Unsupported preferred-share notation; use the exact Yahoo symbol, e.g. ENB-PV.TO")
    # Ordinary class/unit notation (REI.UN, BBD.B, DLR.U) is hyphenated at Yahoo.
    value = f"{code.replace('.', '-')}.{suffix}"
    if not _LISTING.fullmatch(value):
        raise ValueError("Invalid Canadian exchange-listed ticker")
    return value


def canadian_exchange(ticker: str) -> str | None:
    canonical = normalize_canadian_symbol(ticker)
    return ("TSX" if canonical.endswith(".TO") else "TSXV") if canonical else None


def canadian_symbol_key(ticker: str) -> str:
    """Compare run/log keys using the same Canadian aliases as propagation.

    Non-Canadian and unrecognized legacy keys keep their exact spelling. This
    is only a comparison key, not validation or a rewrite of persisted records;
    an invalid ticker still fails normal validation when its analysis runs.
    """
    try:
        # The SDK's date guard parses the raw input first, rejecting malformed
        # prefixes such as TSX:RY+. Such inputs must not hide a valid later cell.
        canonical = normalize_canadian_symbol(ticker)
        # For suffix-form inputs the guard permits a broker qualifier, which
        # normalize_symbol removes before preparing the run (e.g. ry.to+).
        return canonical or normalize_canadian_symbol(ticker.strip().rstrip("+")) or ticker
    except ValueError:
        # One unsupported legacy entry must not block unrelated cells/decisions.
        return ticker


def validate_profile(ticker: str, info: dict) -> dict:
    """Reject an explicitly mismatched listing, even for a cross-listed issuer."""
    if canadian_exchange(ticker) and info.get("symbol"):
        expected = normalize_canadian_symbol(ticker)
        if str(info["symbol"]).strip().upper() != expected:
            raise ValueError(f"Provider profile does not match requested listing {expected}")
    return info


def market_context(ticker: str, identity: Mapping | None = None, *, historical=False) -> str:
    exchange = canadian_exchange(ticker)
    if not exchange:
        return ""
    identity = identity or {}
    context = (
        f" Canadian listing context: {exchange}, Canada; exchange time zone America/Toronto "
        "(daylight-saving aware). Use Canadian exchange sessions and holidays; do not assume "
        "US market hours/holidays or that this is the US cross-listing. Preserve the exact "
        ".TO/.V listing in every tool call, price, news query and portfolio position. Never "
        "fall back to an unsuffixed US namesake. Quote currency must come from the provider: "
        "Canadian listings can trade in CAD or USD; do not infer it from the suffix. "
        "Keep quote currency separate from financial-statement currency; do not mix them "
        "or convert without a sourced FX rate. Prices may be delayed; state source time and "
        "availability instead of claiming real-time data. Consider Bank of Canada policy, "
        "Canadian rates and CAD exposure alongside relevant global risks, rather than only "
        "the Federal Reserve. Canadian disclosures belong to SEDAR+/issuer materials; "
        "SEC filings are not an automatic substitute for this listing. Do not assume US tax rules. "
        "Coverage can be sparse, especially TSXV; missing data is unavailable, not zero. "
        "This adapter covers exchange-listed stocks, ETFs, REITs and other listed funds, "
        "not unlisted NAV-priced mutual funds."
    )
    if not historical:
        for key, label in (("currency", "Provider quote currency"),
                           ("financial_currency", "Provider financial-reporting currency"),
                           ("quote_type", "Provider instrument type")):
            if identity.get(key):
                context += f" {label}: {identity[key]}."
    context += (
        " For an ETF or other exchange-listed fund, analyze mandate, holdings/concentration, "
        "fees, liquidity, tracking, distributions and NAV premium/discount only when sourced; "
        "do not invent operating-company revenue, EPS, debt or corporate statements for a fund. "
        "For REITs, use sourced FFO/AFFO, occupancy and distribution coverage where available; "
        "do not derive them from unrelated corporate metrics. Unknown fund/REIT data stays unavailable."
    )
    return context


def fund_overview(ticker: str, info: dict) -> str | None:
    """Use only provider-returned fund fields; no synthetic company metrics."""
    if not canadian_exchange(ticker) or str(info.get("quoteType", "")).upper() not in {"ETF", "MUTUALFUND"}:
        return None
    fields = [
        ("Name", info.get("longName") or info.get("shortName")),
        ("Provider instrument type", info.get("quoteType")),
        ("Quote currency", info.get("currency")),
        ("Fund family", info.get("fundFamily")),
        ("Category", info.get("category")),
        ("Fund description", info.get("longBusinessSummary")),
        ("NAV per unit (provider quote currency)", info.get("navPrice")),
        ("Total assets (provider value; currency not independently verified)", info.get("totalAssets")),
    ]
    rows = [f"{label}: {value}" for label, value in fields if value is not None]
    return (
        f"# Exchange-listed Fund Overview for {ticker}\n\n" + "\n".join(rows)
        + "\n\nCoverage: present-day provider snapshot, not point-in-time fund disclosures. "
        "Fees, holdings, tracking error and distribution details are unavailable unless "
        "separately sourced. Corporate EPS, revenue and debt ratios are not fund operating metrics. "
        "Do not fabricate missing metrics or compute a NAV premium with mismatched timestamps."
    )


SYMBOL_METHODS = frozenset({
    "get_stock_data", "get_indicators", "get_fundamentals", "get_balance_sheet",
    "get_cashflow", "get_income_statement", "get_news", "get_insider_transactions",
})


def compatible_vendors(method: str, ticker: str, vendors: list[str]) -> list[str]:
    """Only Yahoo's Canadian symbol contract is supported, with no ticker rewrite.

    An explicitly configured unsupported chain stays unavailable: do not silently
    add another vendor or send Yahoo's suffixes to a different symbol namespace.
    """
    if method in SYMBOL_METHODS and canadian_exchange(ticker):
        return [vendor for vendor in vendors if vendor == "yfinance"]
    return vendors


def canadian_today(ticker: str, now: datetime | None = None) -> str | None:
    """The exchange-local calendar date, not a guess at the last trading day."""
    if not canadian_exchange(ticker):
        return None
    zone = ZoneInfo("America/Toronto")
    if now is not None and now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return (now.astimezone(zone) if now is not None else datetime.now(zone)).date().isoformat()
