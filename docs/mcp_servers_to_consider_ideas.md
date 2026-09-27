<!--
SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# MCP servers to consider

## Context

This notebook weighs MCP servers that CoGrind might adopt or build as children, beyond the four it runs (described in [`architecture.md`](architecture.md#the-children)). It is the detailed backing of an entry in [`in-flight_ideas.md`](in-flight_ideas.md), and until Toolsmith exists it is where such candidates are recorded. Nothing here is decided: each idea is a question, to be promoted or dropped on its own.

It gathers two earlier documents: a planning brief for a server that would let an agent query the Anthropic API about its own model (version 4 of the brief, first recorded on 2026-06-13), and an analysis of `ahays248/llama-mcp-server`, a server for llama.cpp (first recorded on 2026-05-05). The candidates' state was checked on 2026-09-27.

## An introspection server for the Anthropic API

### What it would do

A server that lets an agent, or its host, ask about the Anthropic API and the model it runs on: the model's capabilities, the budget of its context window, the current state of the rate limits, and the token count of a planned request. It would not proxy completions: it is a sidecar for self-knowledge, not for inference. It serves the northstar's second thing to remember, that agents need to know about the model they are using.

The brief found that Claude Code learns what its model supports from the documented `GET /v1/models/{id}` endpoint and from knowledge built into the client; the undocumented endpoints seen in network captures carry telemetry, feature flags and policy settings, not capabilities. So the server would wrap the documented API, with no shortcut available.

### Scope

In scope, each wrapped as a tool: `GET /v1/models` (the list), `GET /v1/models/{id}` (the detail, which returns `max_input_tokens`, `max_tokens` and a `capabilities` object), `POST /v1/messages/count_tokens` (counting before sending), and the `anthropic-ratelimit-*` headers read from a light probe request, all returned in small shapes made for an agent. Out of scope: sending real messages on the user's behalf, which is the agent's job; any cost calculation beyond what the API returns; tracking conversations, since the server is stateless between calls; streaming; request-time parameters (web search, extended thinking, beta features), which are not introspection; mirroring telemetry or feature flags; and other providers, the server being Anthropic's alone by design.

### Tools

Five, the names being suggestions, since the descriptions are what the model reads:

| Tool | Takes | Returns |
|---|---|---|
| `list_models` | nothing | each model's `id`, `display_name`, `created_at` and `type` |
| `get_model` | `model_id` | `max_input_tokens`, `max_tokens` and the `capabilities` object; the main tool of self-knowledge |
| `count_tokens` | the body of a `/v1/messages` request (`model`, `messages`, optionally `system`, `tools`, `thinking`), passed through whole | `input_tokens` |
| `get_rate_limit_status` | nothing | the `limit`, `remaining` and `reset` values for requests, tokens, input tokens and output tokens; each call costs a real request, so it is not for polling |
| `get_context_budget` | `model_id`, optionally `current_input_tokens` | the maximum context, the part used, the part remaining and the percentage, computed from `get_model` |

The surface stays this small because every tool's definition costs context at the agent's end. Claude Code selects the context window of a million tokens by appending `[1m]` to a model's id, a syntax of the client that sets the beta header `context-1m-2025-08-07`; the tools would take plain model ids.

### Dependencies and threat model

The server would hold `ANTHROPIC_API_KEY`, exactly the credential that a supply-chain attack on a package of the language-model ecosystem targets. The case the brief records: on 2026-03-24 versions 1.82.7 and 1.82.8 of the LiteLLM package on PyPI carried a credential stealer (reached through a chained compromise of Trivy and of CI credentials), which collected SSH keys, cloud credentials, Kubernetes configuration, `.env` files and shell history and sent them to a lookalike domain; 1.82.8 ran from a `.pth` file at every start of the interpreter, so installing it was enough, and the versions were live for about three hours. Any heavy dependency of a process that holds keys is therefore a target, and the choice of dependencies is a matter of security as well as engineering.

The options the brief ranked:

- Recommended: the Anthropic SDK alone, with the MCP SDK and an environment loader. The documented API gives everything the five tools need, with Claude's own token counts, and the server's traffic goes only to `api.anthropic.com`, where the key goes anyway.
- Acceptable: vendoring LiteLLM's `model_prices_and_context_window.json` (MIT-licensed, about 500 KB) for capability flags richer than the API's, refreshed at start from `https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json` with the bundled copy as the fallback. The data file is what is wanted; the package is the part that was attacked, so it is not installed. The same holds for models.dev's data.
- Rejected: importing the LiteLLM library. Its convenience, token counting across providers, is more than an Anthropic-only sidecar needs, and pinned versions with `--require-hashes` would not have caught the 1.82.8 wheel, whose `.pth` file was correctly listed in its record.

### Design points

The key comes from the environment, never as a tool parameter, which would leak it into the agent's context, and the server fails at start without it. It serves streamable HTTP for remote use and stdio for Claude Desktop and Claude Code. Its outputs are small and flat: `get_model` returns only the fields an agent uses, and `count_tokens` returns `{input_tokens}` alone. Model metadata is cached in memory for an hour; token counts and rate limits are per call. The API's errors (`{error: {type, message}}`) pass through verbatim, wrapped to show whether the error came from upstream or from the server, and a 401 is never swallowed. The `anthropic-version` header is pinned in code (`2023-06-01`) and not configurable at first. The server collects no data: its only outbound calls are to `api.anthropic.com`, and to `raw.githubusercontent.com` for the optional refresh, with the user's own key, and it stores nothing. Every dependency is pinned to an exact version, with a documented procedure for upgrades; with the vendored file, its SHA-256 is published in the README; and releases are signed from the start (cosign for images, sigstore for PyPI).

The deliverable would be one repository: the server, a README explaining what each tool does and when an agent should call it (the descriptions drive the model's behaviour more than the code does), configuration snippets for Claude Desktop and Claude Code, an `.env.example`, and tests that mock the Anthropic SDK; MIT-licensed, with minimal dependencies and no web framework, the SDK providing the transport.

### Open questions

The brief asked these to be settled before any implementation:

- Language: TypeScript or Python. Both are first-class for MCP; PyPI was the more abused channel in 2026, a slight edge to TypeScript on supply-chain grounds, not enough to override preference.
- Whether the recommended option, the Anthropic SDK alone, is the path taken.
- Whether capability flags richer than those of `/v1/models/{id}` are needed; if unsure, no, with the vendored registry added only when a real need appears.
- For `get_rate_limit_status`, a probe whose answer is discarded, or a companion mechanism by which an agent reports the headers of its last real call and so avoids the wasted request.
- For `count_tokens`, the whole schema of `/v1/messages` (more flexible, more surface) or a reduced `{model, messages, system?}` (smaller, clearer).
- A model id ending in `[1m]`: strip the suffix silently, or reject the id.

And for CoGrind itself: should such a server be one of its children, and should the agents' self-knowledge come from the Anthropic API alone, from llama.cpp's endpoints when a local model is used (below), or from both behind one surface?

## The llama.cpp side

For a model served locally by llama.cpp, the equivalent facts come from `llama-server`'s own HTTP endpoints, `/props`, `/slots` and `/v1/models`, which the brief would call directly, no library adding anything. `ahays248/llama-mcp-server` was analysed as a reference for those patterns, not as a dependency.

As analysed on 2026-05-05, it was a TypeScript MCP server, built on `@modelcontextprotocol/sdk` with inputs validated by `zod` schemas, that bridged MCP clients to `llama-server`'s REST API with 19 tools: server management (`llama_health`, `llama_props`, `llama_models`, `llama_slots`, `llama_metrics`); inference (`llama_complete`, `llama_chat`, `llama_embed`, `llama_infill`, `llama_rerank`); tokens (`llama_tokenize`, `llama_detokenize`, `llama_apply_template`); models (`llama_load_model`, `llama_unload_model`); LoRA adapters (`llama_lora_list`, `llama_lora_set`); and process control (`llama_start` and `llama_stop`, which spawned `llama-server` and waited for its health check). It was configured by `LLAMA_SERVER_URL` (default `http://localhost:8080`), `LLAMA_TIMEOUT` (30,000 ms), `LLAMA_MODEL_PATH` and `LLAMA_SERVER_PATH` (default `llama-server`). For introspection, the relevant tools were those that wrapped `/props`, `/slots`, `/v1/models` and `/metrics`. The repository no longer exists, so its code cannot be consulted; the endpoints it wrapped remain `llama-server`'s own.

## Candidates weighed

| Candidate | What it is | Verdict recorded | State on 2026-09-27 |
|---|---|---|---|
| `shin-bot-litellm/litellm-mcp` | wraps the whole OpenAPI of a LiteLLM proxy as 86 tools | wrong shape, too heavy | not found on GitHub (404), the account included |
| Composio's "Anthropic administrator" toolkit | a commercial hosted service | set aside | not checked |
| `ahays248/llama-mcp-server` | MCP server for llama.cpp's `llama-server` | the llama.cpp side only; a reference, not a dependency | not found on GitHub (404); the account exists |
| LiteLLM's `model_prices_and_context_window.json` | data file of model prices and capabilities | acceptable as vendored data, never as the library | the raw URL above resolves (HTTP 200) |
