"""Small adapters at the CLI/graph boundary; no execution or display policy."""

from __future__ import annotations

import json

from tradingagents.reporting import _SECTIONS


def persist_report(store, final_state: dict, settings: dict) -> None:
    """Archive only renderer inputs, including memory_note, never runtime objects.

    Deriving section keys from the existing renderer preserves new report types
    without schema migrations. Message objects, holdings and raw config stay out.
    """
    keys = {"company_of_interest", "trade_date", "final_rating", "memory_note"}
    keys.update(path[0] for _, _, agents in _SECTIONS for _, _, path in agents)
    state = {key: value for key, value in final_state.items() if key in keys}
    store.write_artifact("report_state.json", json.dumps(state, ensure_ascii=False), "application/json")
    store.write_artifact("settings.json", json.dumps(settings, ensure_ascii=False), "application/json")
