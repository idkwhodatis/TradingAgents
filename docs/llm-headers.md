# Custom LLM HTTP headers

OpenAI, OpenAI-compatible providers (including local servers), Anthropic and
Azure accept custom HTTP headers through the standard configuration path.
These are transport headers, not `extra_body`/model parameters.

```dotenv
TRADINGAGENTS_LLM_HEADERS='{"User-Agent":"TradingAgents/0.6.0","X-Custom-Header":"value"}'
```

Or, before constructing `TradingAgentsGraph`:

```python
config["llm_headers"] = {
    "User-Agent": "TradingAgents/0.6.0",
    "X-Custom-Header": "value",
}
```

The mapping reaches both deep- and quick-thinking clients as the SDK's
`default_headers`, including synchronous and asynchronous requests and retries.
Direct `create_llm_client` callers can pass `default_headers={...}`.

Unset, `None`, an empty string or `{}` preserves existing provider defaults.
A configured mapping replaces the environment-provided mapping; they are not
implicitly merged. Values must be strings. Invalid JSON, non-object JSON,
duplicate names (case-insensitive), invalid names and control/non-ASCII
characters are rejected. Validation errors do not include header values.
Google and Bedrock adapters do not implement this option; a nonempty mapping
raises an explicit error rather than silently ignoring the headers.

Use `User-Agent` with that spelling when overriding an SDK's default. For a
session header, reuse the same value for one conversation and its retries;
use a different value for an independent conversation. Treat headers as
potential credentials: keep real values in an untracked `.env` or a secret
manager, not in source code or committed examples.

## Different providers per model tier

Shared `llm_headers` belong to `llm_provider`. A quick or deep tier using a
different provider never inherits them. Set `quick_think_llm_headers` or
`deep_think_llm_headers` explicitly for that tier (environment variables
`TRADINGAGENTS_QUICK_THINK_LLM_HEADERS` and `TRADINGAGENTS_DEEP_THINK_LLM_HEADERS`).
These accept the same JSON object format. An empty object disables inherited
headers for a tier on the shared provider. Do not put secrets in report content.
