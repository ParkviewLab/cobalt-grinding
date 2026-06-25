# Anthropic Introspection MCP Server — Planning Brief (v4)

## Purpose

A Model Context Protocol server that lets an agent (or its host) query metadata about the Anthropic API and the model it's running on: model capabilities, context-window budget, current rate-limit state, and token-counting for planned requests. It does not proxy chat completions — it's a sidecar for self-knowledge, not inference.

## Background

Before writing this I checked whether Claude Code uses any undocumented endpoints for model introspection. It doesn't. Network captures (mitmproxy) and the March 2026 source-map leak surface plenty of undocumented endpoints — Statsig telemetry at `statsig.anthropic.com/v1/rgstr`, GrowthBook feature flags at `api/eval/sdk-*`, hourly policy-settings polling, Datadog logging, an anti-distillation mechanism — but none of them carry model-capability information. Claude Code learns what its model supports through the documented `GET /v1/models/{id}` endpoint plus hard-coded client knowledge. That confirms the design below: wrap the documented surface, no shortcuts available.

I also did a sweep for existing MCP servers covering this space. The closest candidates are `shin-bot-litellm/litellm-mcp` (wraps a LiteLLM proxy's full OpenAPI as 86 tools — wrong shape, too heavy), Composio's "Anthropic administrator" toolkit (commercial SaaS), and `ahays248/llama-mcp-server` (only the llama.cpp side). None fit. So we build, but with library help where it doesn't introduce risk — see "Implementation libraries" below.

## Threat model note (read before picking dependencies)

This server holds `ANTHROPIC_API_KEY` in its environment. That is exactly the kind of credential a supply-chain attack against an LLM-ecosystem package targets. On March 24, 2026, the LiteLLM PyPI package (versions 1.82.7 and 1.82.8) was compromised by TeamPCP via a chained Trivy → CI/CD credential theft. The malicious versions shipped a credential stealer that harvested SSH keys, cloud creds, K8s configs, `.env` files, and shell history, encrypted them with AES-256-CBC + RSA-4096, and exfiltrated to a lookalike domain (`models.litellm.cloud`, not the official `litellm.ai`). Critically, version 1.82.8 used a `.pth` file that fires on *every* Python interpreter startup — no `import litellm` required — meaning install was sufficient to trigger execution. The malicious versions were live for roughly three hours before PyPI quarantined them, but with ~97 million downloads/month, exposure was wide. The same campaign also hit Trivy, Checkmarx, and KICS upstream.

LiteLLM's current releases are clean and the project is now using cosign for Docker image signing, but the lesson generalizes: any heavy dependency in a process that holds API keys is a soft target. The dependency choice for this server is therefore not just an engineering trade-off — it's a security one. Minimize the surface.

## Scope: in / out

**In scope.** Wrapping these Anthropic API surfaces as MCP tools: `GET /v1/models` (list), `GET /v1/models/{id}` (detail — returns `max_input_tokens`, `max_tokens`, and a `capabilities` object as of late 2025), `POST /v1/messages/count_tokens` (pre-flight token counting), and parsing of `anthropic-ratelimit-*` response headers from a lightweight probe request. Returning the data shaped for agent consumption — small, structured, no echo of full request bodies.

**Out of scope.** Sending real `/v1/messages` requests on the user's behalf — that's the agent's job. No cost calculation beyond what the API returns. No conversation-history tracking — the server is stateless between calls. No streaming. No web search, extended-thinking config, or beta-feature gating — those are request-time params, not introspection. No telemetry/feature-flag mirroring — those endpoints exist but aren't capability surfaces. No multi-provider abstraction — Anthropic-only by design.

## Tools to expose

Names are suggestions; descriptions matter more because they're what the model sees.

`list_models` — Returns available Claude models with `id`, `display_name`, `created_at`, `type`. No params.

`get_model` — Takes `model_id`, returns full metadata: `max_input_tokens`, `max_tokens`, and the `capabilities` object (vision, tools, extended_thinking, etc.). This is the primary self-knowledge tool — most agent introspection needs are answered here.

`count_tokens` — Takes the same shape as a `/v1/messages` request body (`model`, `messages`, optional `system`, `tools`, `thinking`) and returns `input_tokens`. Pass through everything; don't filter fields.

`get_rate_limit_status` — Sends a minimal probe to `/v1/messages` and returns parsed `anthropic-ratelimit-{requests,tokens,input-tokens,output-tokens}-{limit,remaining,reset}`. Document clearly that this *costs a real request* and shouldn't be polled aggressively. A future enhancement: cache headers from any prior real call if the agent can report them in.

`get_context_budget` — Convenience tool: takes `model_id` and an optional `current_input_tokens`, returns max context, used, remaining, and percentage. Pure computation over the `get_model` result.

That's five tools. Keep the surface this small; every tool definition costs context tokens at the agent end.

## A note on the `[1m]` model suffix

Claude Code uses a quirky convention to select the 1M-token context window: append `[1m]` to the model id (e.g., `claude-sonnet-4-5-20250929[1m]`). This is a client-side syntax, not an API-level thing — under the hood it sets the `context-1m-2025-08-07` beta header. Your `count_tokens` and `get_model` tools should accept plain model IDs only and ignore the `[1m]` suffix if passed. Document this so the agent doesn't get confused if someone hands it a Claude-Code-style model string.

## Implementation libraries — recommendation

Given the threat-model note above, prefer the *least* code that gets the job done. Three options, ordered by how I'd rank them now:

**Option C (recommended): bare Anthropic SDK only.** For an Anthropic-only introspection server, the documented Anthropic API already exposes everything the five tools need: `/v1/models` and `/v1/models/{id}` give capabilities and limits; `/v1/messages/count_tokens` gives accurate Claude-specific token counts (same tokenizer the model actually uses); rate-limit headers come back on every call. No registry needed, no cross-provider tokenizer needed, no LiteLLM needed. Dependencies: MCP SDK + Anthropic SDK + a `.env` loader. That's it. Smallest possible attack surface, and the server's outbound traffic goes only to `api.anthropic.com` — which is where the API key was always going to be sent anyway. **This is the right default.**

**Option A′ (acceptable): vendor `model_prices_and_context_window.json` from LiteLLM.** If for some reason you want the registry's richer capability flags (`supports_prompt_caching`, `supports_audio_input`, `supports_response_schema`, etc.) beyond what Anthropic's `/v1/models/{id}` returns, *vendor the JSON file directly* into the repo. It's MIT-licensed, ~500KB, self-contained. Optionally refresh it from `https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json` at startup with a fallback to the bundled copy. **Do not `pip install litellm`** — the JSON file is what you actually want, and the package itself is the part of LiteLLM that got attacked. Vendoring data is fine; importing the library puts you on the same supply-chain risk surface that just got exploited. Same logic applies if you prefer models.dev — fetch their JSON, don't take a runtime dep.

**Option A (do not use): full LiteLLM library import.** Listed for completeness so Claude Code knows why we ruled it out. The convenience (one-call cross-provider tokenization) is real but is overkill for an Anthropic-only sidecar, and the package is a high-value target with a recent confirmed compromise. The credential-stealing payload from March 2026 was specifically designed to harvest the kind of API keys this server holds. Even with pinned versions and `--require-hashes` (which would not have caught the 1.82.8 wheel — its `.pth` file's hash was correctly listed in RECORD), you're carrying transitive risk for a benefit you don't need. Skip.

**For the llama.cpp side**, if it's ever added: hit `/props`, `/slots`, `/v1/models` directly with HTTP. No library buys you anything. `ahays248/llama-mcp-server` is a reference; copy the patterns, don't take it as a dep.

## Key design decisions

**Auth.** `ANTHROPIC_API_KEY` from environment. Don't accept it as a tool parameter — that leaks credentials into agent context. Fail loudly at startup if missing.

**Transport.** Streamable HTTP for remote use, stdio for local Claude Desktop / Code. The official MCP TypeScript or Python SDK handles both with a flag.

**Output shape discipline.** Return small, flat objects. For `get_model`, return only fields the agent will use (`id`, `display_name`, `max_input_tokens`, `max_tokens`, `capabilities`) — drop internal IDs and raw timestamps. For `count_tokens`, return `{input_tokens: N}` and nothing else. Resist the urge to "helpfully" include the full request echo.

**Caching.** Model metadata changes rarely — cache `list_models` and `get_model` for an hour. Token counts and rate-limit status are per-call. Simple in-memory TTL is enough; no Redis.

**Error handling.** Anthropic errors come back in a documented shape (`{error: {type, message}}`). Pass them through verbatim with a clear MCP-side wrapper indicating upstream vs. server-side error. Don't swallow 401s.

**Versioning.** Pin a known-good `anthropic-version` header in code (currently `2023-06-01` is still the standard). Don't expose it as user-configurable in MVP.

**Don't conflate with telemetry.** If the user asks "is this server collecting data on me," the answer should be a clear no. This server makes outbound calls only to `api.anthropic.com` (Option C) or also to `raw.githubusercontent.com` for an optional registry refresh (Option A′), with the user's own API key, and stores nothing.

**Supply-chain hygiene.** Pin every dependency in `requirements.txt` / `package-lock.json` to exact versions. Document upgrade procedure. If using Option A′, ship the vendored JSON's SHA-256 in the README so users can verify it themselves. Consider signing releases (cosign for Docker, sigstore for PyPI) from day one — the LiteLLM team retrofitted this *after* the compromise and it would have helped.

## Deliverables

A single repo with: server source, a README explaining what each tool does and when an agent should call it (this matters more than the code — descriptions drive model behavior), example MCP client config snippets for Claude Desktop and Claude Code, a `.env.example` with `ANTHROPIC_API_KEY`, and basic tests that mock the Anthropic SDK. If using Option A′, also include the vendored JSON with a checksum and a `tools/refresh_registry.sh` script.

License MIT. Dependencies minimal: MCP SDK, Anthropic SDK, a tiny env loader. Optionally: an HTTP client if Option A′ wants startup refresh of the vendored JSON. **No LiteLLM library.** No web framework — the SDK provides transport.

## What to ask the user before starting

Pin these down rather than guessing:

- **Language**: TypeScript or Python? Both are first-class for MCP. The threat-model concern applies to both ecosystems but PyPI has been the more frequently abused channel in 2026, slight edge to TypeScript on supply-chain grounds; not enough to override personal preference.
- **Library choice**: confirm Option C (bare Anthropic SDK) is the intended path. Push back if they reach for full LiteLLM.
- **Registry inclusion**: do they actually need richer capability flags than what `/v1/models/{id}` returns? If unsure, default to no — start with Option C and add Option A′ later if a real need surfaces.
- **Rate-limit approach**: probe-and-discard for `get_rate_limit_status`, or wait until they've added a "report headers from your last call" companion mechanism that avoids the wasted request?
- **count_tokens surface**: full `/v1/messages` schema (more flexible, more surface) or simplified `{model, messages, system?}` (smaller, clearer)?
- **`[1m]` suffix handling**: silently strip it, or reject as invalid?

---

Hand to Claude Code with the instruction to *plan first* — file structure, dependency list, tool schemas — and to surface the six questions above before writing any implementation. If it reaches for `pip install litellm` despite all this, push back hard: the brief explicitly rules it out for documented reasons.
