<!--
SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# MCP servers to consider

## Context

This notebook weighs MCP servers that CoGrind might adopt or build as children, beyond the four its default configuration starts (described in [`architecture.md`](architecture.md#the-children)). It is the detailed backing of an entry in [`in-flight_ideas.md`](in-flight_ideas.md), and until Toolsmith exists it is where such candidates are recorded. Nothing here is decided: each idea is a question, to be promoted or dropped on its own.

It gathers two earlier documents, each kept in its own words under the candidate it concerns: a planning brief for a server that would let an agent query the Anthropic API about its own model (version 4 of the brief, first recorded on 2026-06-13), and an analysis of `ahays248/llama-mcp-server`, a server for llama.cpp (first recorded on 2026-05-05). Both are kept with three changes: their dashes are replaced by other punctuation, their bold type is removed, and their headings are set lower; the analysis's headings also lose their emoji, and the code blocks of both are unchanged. The candidates' state was checked on 2026-09-27.

## An introspection server for the Anthropic API

The brief below plans a server, not yet built, that would let an agent, or its host, ask about the Anthropic API and the model it runs on. It serves the northstar's second thing to remember, that agents need to know about the model they are using; what an agent would do with such knowledge is in [`agent_self_knowledge_ideas.md`](agent_self_knowledge_ideas.md). The brief was written to be handed to Claude Code, and its first person is its author's.

### Anthropic Introspection MCP Server: Planning Brief (v4)

#### Purpose

A Model Context Protocol server that lets an agent (or its host) query metadata about the Anthropic API and the model it's running on: model capabilities, context-window budget, current rate-limit state, and token-counting for planned requests. It does not proxy chat completions; it's a sidecar for self-knowledge, not inference.

#### Background

Before writing this I checked whether Claude Code uses any undocumented endpoints for model introspection. It doesn't. Network captures (mitmproxy) and the March 2026 source-map leak surface plenty of undocumented endpoints (Statsig telemetry at `statsig.anthropic.com/v1/rgstr`, GrowthBook feature flags at `api/eval/sdk-*`, hourly policy-settings polling, Datadog logging, an anti-distillation mechanism), but none of them carry model-capability information. Claude Code learns what its model supports through the documented `GET /v1/models/{id}` endpoint plus hard-coded client knowledge. That confirms the design below: wrap the documented surface, no shortcuts available.

I also did a sweep for existing MCP servers covering this space. The closest candidates are `shin-bot-litellm/litellm-mcp` (wraps a LiteLLM proxy's full OpenAPI as 86 tools: wrong shape, too heavy), Composio's "Anthropic administrator" toolkit (commercial SaaS), and `ahays248/llama-mcp-server` (only the llama.cpp side). None fit. So we build, but with library help where it doesn't introduce risk; see "Implementation libraries" below.

#### Threat model note (read before picking dependencies)

This server holds `ANTHROPIC_API_KEY` in its environment. That is exactly the kind of credential a supply-chain attack against an LLM-ecosystem package targets. On March 24, 2026, the LiteLLM PyPI package (versions 1.82.7 and 1.82.8) was compromised by TeamPCP via a chained Trivy → CI/CD credential theft. The malicious versions shipped a credential stealer that harvested SSH keys, cloud creds, K8s configs, `.env` files, and shell history, encrypted them with AES-256-CBC + RSA-4096, and exfiltrated to a lookalike domain (`models.litellm.cloud`, not the official `litellm.ai`). Critically, version 1.82.8 used a `.pth` file that fires on *every* Python interpreter startup (no `import litellm` required), meaning install was sufficient to trigger execution. The malicious versions were live for roughly three hours before PyPI quarantined them, but with ~97 million downloads/month, exposure was wide. The same campaign also hit Trivy, Checkmarx, and KICS upstream.

LiteLLM's current releases are clean and the project is now using cosign for Docker image signing, but the lesson generalizes: any heavy dependency in a process that holds API keys is a soft target. The dependency choice for this server is therefore not just an engineering trade-off; it's a security one. Minimize the surface.

#### Scope: in / out

In scope. Wrapping these Anthropic API surfaces as MCP tools: `GET /v1/models` (list), `GET /v1/models/{id}` (detail: returns `max_input_tokens`, `max_tokens`, and a `capabilities` object as of late 2025), `POST /v1/messages/count_tokens` (pre-flight token counting), and parsing of `anthropic-ratelimit-*` response headers from a lightweight probe request. Returning the data shaped for agent consumption: small, structured, no echo of full request bodies.

Out of scope. Sending real `/v1/messages` requests on the user's behalf; that's the agent's job. No cost calculation beyond what the API returns. No conversation-history tracking; the server is stateless between calls. No streaming. No web search, extended-thinking config, or beta-feature gating; those are request-time params, not introspection. No telemetry/feature-flag mirroring; those endpoints exist but aren't capability surfaces. No multi-provider abstraction: Anthropic-only by design.

#### Tools to expose

Names are suggestions; descriptions matter more because they're what the model sees.

`list_models`: Returns available Claude models with `id`, `display_name`, `created_at`, `type`. No params.

`get_model`: Takes `model_id`, returns full metadata: `max_input_tokens`, `max_tokens`, and the `capabilities` object (vision, tools, extended_thinking, etc.). This is the primary self-knowledge tool: most agent introspection needs are answered here.

`count_tokens`: Takes the same shape as a `/v1/messages` request body (`model`, `messages`, optional `system`, `tools`, `thinking`) and returns `input_tokens`. Pass through everything; don't filter fields.

`get_rate_limit_status`: Sends a minimal probe to `/v1/messages` and returns parsed `anthropic-ratelimit-{requests,tokens,input-tokens,output-tokens}-{limit,remaining,reset}`. Document clearly that this *costs a real request* and shouldn't be polled aggressively. A future enhancement: cache headers from any prior real call if the agent can report them in.

`get_context_budget` (convenience tool): takes `model_id` and an optional `current_input_tokens`, returns max context, used, remaining, and percentage. Pure computation over the `get_model` result.

That's five tools. Keep the surface this small; every tool definition costs context tokens at the agent end.

#### A note on the `[1m]` model suffix

Claude Code uses a quirky convention to select the 1M-token context window: append `[1m]` to the model id (e.g., `claude-sonnet-4-5-20250929[1m]`). This is a client-side syntax, not an API-level thing; under the hood it sets the `context-1m-2025-08-07` beta header. Your `count_tokens` and `get_model` tools should accept plain model IDs only and ignore the `[1m]` suffix if passed. Document this so the agent doesn't get confused if someone hands it a Claude-Code-style model string.

#### Implementation libraries: recommendation

Given the threat-model note above, prefer the *least* code that gets the job done. Three options, ordered by how I'd rank them now:

Option C (recommended): bare Anthropic SDK only. For an Anthropic-only introspection server, the documented Anthropic API already exposes everything the five tools need: `/v1/models` and `/v1/models/{id}` give capabilities and limits; `/v1/messages/count_tokens` gives accurate Claude-specific token counts (same tokenizer the model actually uses); rate-limit headers come back on every call. No registry needed, no cross-provider tokenizer needed, no LiteLLM needed. Dependencies: MCP SDK + Anthropic SDK + a `.env` loader. That's it. Smallest possible attack surface, and the server's outbound traffic goes only to `api.anthropic.com`, which is where the API key was always going to be sent anyway. This is the right default.

Option A′ (acceptable): vendor `model_prices_and_context_window.json` from LiteLLM. If for some reason you want the registry's richer capability flags (`supports_prompt_caching`, `supports_audio_input`, `supports_response_schema`, etc.) beyond what Anthropic's `/v1/models/{id}` returns, *vendor the JSON file directly* into the repo. It's MIT-licensed, ~500KB, self-contained. Optionally refresh it from `https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json` at startup with a fallback to the bundled copy. Do not `pip install litellm`: the JSON file is what you actually want, and the package itself is the part of LiteLLM that got attacked. Vendoring data is fine; importing the library puts you on the same supply-chain risk surface that just got exploited. Same logic applies if you prefer models.dev: fetch their JSON, don't take a runtime dep.

Option A (do not use): full LiteLLM library import. Listed for completeness so Claude Code knows why we ruled it out. The convenience (one-call cross-provider tokenization) is real but is overkill for an Anthropic-only sidecar, and the package is a high-value target with a recent confirmed compromise. The credential-stealing payload from March 2026 was specifically designed to harvest the kind of API keys this server holds. Even with pinned versions and `--require-hashes` (which would not have caught the 1.82.8 wheel: its `.pth` file's hash was correctly listed in RECORD), you're carrying transitive risk for a benefit you don't need. Skip.

For the llama.cpp side, if it's ever added: hit `/props`, `/slots`, `/v1/models` directly with HTTP. No library buys you anything. `ahays248/llama-mcp-server` is a reference; copy the patterns, don't take it as a dep.

#### Key design decisions

Auth. `ANTHROPIC_API_KEY` from environment. Don't accept it as a tool parameter; that leaks credentials into agent context. Fail loudly at startup if missing.

Transport. Streamable HTTP for remote use, stdio for local Claude Desktop / Code. The official MCP TypeScript or Python SDK handles both with a flag.

Output shape discipline. Return small, flat objects. For `get_model`, return only fields the agent will use (`id`, `display_name`, `max_input_tokens`, `max_tokens`, `capabilities`); drop internal IDs and raw timestamps. For `count_tokens`, return `{input_tokens: N}` and nothing else. Resist the urge to "helpfully" include the full request echo.

Caching. Model metadata changes rarely: cache `list_models` and `get_model` for an hour. Token counts and rate-limit status are per-call. Simple in-memory TTL is enough; no Redis.

Error handling. Anthropic errors come back in a documented shape (`{error: {type, message}}`). Pass them through verbatim with a clear MCP-side wrapper indicating upstream vs. server-side error. Don't swallow 401s.

Versioning. Pin a known-good `anthropic-version` header in code (currently `2023-06-01` is still the standard). Don't expose it as user-configurable in MVP.

Don't conflate with telemetry. If the user asks "is this server collecting data on me," the answer should be a clear no. This server makes outbound calls only to `api.anthropic.com` (Option C) or also to `raw.githubusercontent.com` for an optional registry refresh (Option A′), with the user's own API key, and stores nothing.

Supply-chain hygiene. Pin every dependency in `requirements.txt` / `package-lock.json` to exact versions. Document upgrade procedure. If using Option A′, ship the vendored JSON's SHA-256 in the README so users can verify it themselves. Consider signing releases (cosign for Docker, sigstore for PyPI) from day one; the LiteLLM team retrofitted this *after* the compromise and it would have helped.

#### Deliverables

A single repo with: server source, a README explaining what each tool does and when an agent should call it (this matters more than the code: descriptions drive model behavior), example MCP client config snippets for Claude Desktop and Claude Code, a `.env.example` with `ANTHROPIC_API_KEY`, and basic tests that mock the Anthropic SDK. If using Option A′, also include the vendored JSON with a checksum and a `tools/refresh_registry.sh` script.

License MIT. Dependencies minimal: MCP SDK, Anthropic SDK, a tiny env loader. Optionally: an HTTP client if Option A′ wants startup refresh of the vendored JSON. No LiteLLM library. No web framework; the SDK provides transport.

#### What to ask the user before starting

Pin these down rather than guessing:

- Language: TypeScript or Python? Both are first-class for MCP. The threat-model concern applies to both ecosystems but PyPI has been the more frequently abused channel in 2026, slight edge to TypeScript on supply-chain grounds; not enough to override personal preference.
- Library choice: confirm Option C (bare Anthropic SDK) is the intended path. Push back if they reach for full LiteLLM.
- Registry inclusion: do they actually need richer capability flags than what `/v1/models/{id}` returns? If unsure, default to no: start with Option C and add Option A′ later if a real need surfaces.
- Rate-limit approach: probe-and-discard for `get_rate_limit_status`, or wait until they've added a "report headers from your last call" companion mechanism that avoids the wasted request?
- count_tokens surface: full `/v1/messages` schema (more flexible, more surface) or simplified `{model, messages, system?}` (smaller, clearer)?
- `[1m]` suffix handling: silently strip it, or reject as invalid?

---

Hand to Claude Code with the instruction to *plan first* (file structure, dependency list, tool schemas) and to surface the six questions above before writing any implementation. If it reaches for `pip install litellm` despite all this, push back hard: the brief explicitly rules it out for documented reasons.

### For CoGrind

Should such a server be one of CoGrind's children, and should its agents' self-knowledge come from the Anthropic API alone, from llama.cpp's endpoints when a local model is used (below), or from both behind one surface?

## The llama.cpp side

For a model served locally by llama.cpp, the equivalent facts come from `llama-server`'s own HTTP endpoints (`/props`, `/slots` and `/v1/models`), which the brief would call directly. `ahays248/llama-mcp-server` was analysed on 2026-05-05 as a reference for those patterns, not as a dependency, and the analysis follows in its own words. The repository no longer exists (it returned 404 when checked on 2026-09-27), so its code cannot be consulted; the endpoints it wrapped remain `llama-server`'s own.

### llama-mcp-server Analysis Report

Repository: [ahays248/llama-mcp-server](https://github.com/ahays248/llama-mcp-server)

#### Overview

llama-mcp-server is a Model Context Protocol (MCP) server that provides comprehensive integration with llama-server (the HTTP server component of llama.cpp). It exposes 19 tools that enable AI assistants and applications to interact with local LLMs running via llama.cpp.

The server acts as a bridge between MCP-compatible clients (like Claude Desktop, Cursor, Zed, etc.) and llama.cpp, providing a standardized interface for:
- Text completion and chat
- Embedding generation
- Code infilling
- Document reranking
- Token manipulation
- Model management
- LoRA adapter control
- Server health monitoring and metrics
- Process management (start/stop llama-server)

---

#### Architecture

##### Core Components

1. `src/client.ts` - HTTP client for communicating with llama-server REST API
2. `src/config.ts` - Configuration loading from environment variables
3. `src/server.ts` - MCP server setup that registers all 19 tools
4. `src/index.ts` - Entry point that initializes the server
5. `src/types.ts` - Shared TypeScript interfaces and types
6. `src/tools/*.ts` - Individual tool implementations organized by category

##### Configuration

The server reads configuration from environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLAMA_SERVER_URL` | `http://localhost:8080` | URL of the llama-server instance |
| `LLAMA_TIMEOUT` | `30000` (ms) | Request timeout |
| `LLAMA_MODEL_PATH` | (optional) | Default model path for start tool |
| `LLAMA_SERVER_PATH` | `llama-server` | Path to llama-server executable |

---

#### All 19 MCP Tools

##### Server Management Tools (5 tools)

###### 1. `llama_health`
Purpose: Check if llama-server is running and healthy.

Input: None (empty object)

Output:
```typescript
interface HealthResponse {
  status: string;           // "ok" when healthy
  total_slots: number;      // Total available slots
  used_slots: number;       // Currently used slots
}
```

---

###### 2. `llama_props`
Purpose: Get or update server default generation settings.

Input:
```typescript
interface PropsInput {
  default_generation_settings?: {
    temperature?: number;    // Sampling temperature (0-2)
    top_p?: number;          // Nucleus sampling threshold
    top_k?: number;          // Top-k sampling
    // ... additional settings via passthrough
  };
}
```

Output:
```typescript
interface PropsResponse {
  default_generation_settings: {
    temperature: number;
    top_p: number;
    top_k: number;
    // ... other generation settings
  };
}
```

---

###### 3. `llama_models`
Purpose: List all loaded models on the server.

Input: None (empty object)

Output:
```typescript
interface ModelsResponse {
  models: ModelInfo[];
}

interface ModelInfo {
  name: string;
  loading: boolean;
  size: number;
  // ... additional model metadata
}
```

---

###### 4. `llama_slots`
Purpose: Get information about all inference slots (concurrent request handlers).

Input: None (empty object)

Output:
```typescript
type SlotsResponse = SlotInfo[];

interface SlotInfo {
  id: number;
  state: string;
  model: string;
  // ... slot status information
}
```

---

###### 5. `llama_metrics`
Purpose: Get server performance metrics.

Input: None (empty object)

Output:
```typescript
interface MetricsResponse {
  // Performance counters, timing information, etc.
}
```

---

##### Inference Tools (5 tools)

###### 6. `llama_complete`
Purpose: Generate text completion from a prompt.

Input:
```typescript
interface CompleteInput {
  prompt: string;                    // The prompt to complete
  max_tokens?: number;               // Maximum tokens to generate (default: 256)
  temperature?: number;              // Sampling temperature (0-2, default: 0.7)
  top_p?: number;                    // Nucleus sampling threshold (default: 0.9)
  top_k?: number;                    // Top-k sampling (default: 40)
  stop?: string[];                   // Stop sequences
  seed?: number;                     // Random seed for reproducibility
}
```

Output:
```typescript
interface CompletionResponse {
  content: string;                   // Generated text
  tokens_predicted: number;
  tokens_evaluated: number;
  // ... timing and usage stats
}
```

---

###### 7. `llama_chat`
Purpose: Multi-turn chat completion with message history.

Input:
```typescript
interface ChatInput {
  messages: ChatMessage[];           // Chat messages
  max_tokens?: number;               // Maximum tokens (default: 256)
  temperature?: number;              // Temperature (default: 0.7)
  top_p?: number;                    // Top-p (default: 0.9)
  stop?: string[];                   // Stop sequences
  seed?: number;                     // Random seed
}

interface ChatMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
}
```

Output:
```typescript
interface ChatResponse {
  content: string;                   // Assistant response
  model: string;
  // ... usage and timing stats
}
```

---

###### 8. `llama_embed`
Purpose: Generate vector embeddings for text.

Input:
```typescript
interface EmbedInput {
  content: string;                   // Text to embed
}
```

Output:
```typescript
interface EmbedResponse {
  embedding: number[];              // Vector embedding
  // ... metadata
}
```

---

###### 9. `llama_infill`
Purpose: Fill in missing code between prefix and suffix (code completion).

Input:
```typescript
interface InfillInput {
  input_prefix: string;              // Code before cursor
  input_suffix: string;              // Code after cursor
  max_tokens?: number;               // Maximum tokens (default: 256)
  temperature?: number;              // Temperature (default: 0.7)
  stop?: string[];                   // Stop sequences
}
```

Output:
```typescript
interface InfillResponse {
  content: string;                   // Generated code infill
  // ... usage stats
}
```

---

###### 10. `llama_rerank`
Purpose: Rerank documents based on relevance to a query.

Input:
```typescript
interface RerankInput {
  query: string;                     // Search query
  documents: string[];               // Documents to rerank
}
```

Output:
```typescript
interface RerankResponse {
  results: RerankResult[];
}

interface RerankResult {
  index: number;                     // Original document index
  relevance_score: number;           // Relevance score
}
```

---

##### Token Manipulation Tools (3 tools)

###### 11. `llama_tokenize`
Purpose: Convert text to token IDs.

Input:
```typescript
interface TokenizeInput {
  content: string;                   // Text to tokenize
  add_special?: boolean;             // Add BOS/EOS tokens (default: true)
  with_pieces?: boolean;             // Include token strings (default: false)
}
```

Output:
```typescript
interface TokenizeResponse {
  tokens: number[];                  // Token IDs
  // pieces?: string[];              // Optional token strings
}
```

---

###### 12. `llama_detokenize`
Purpose: Convert token IDs back to text.

Input:
```typescript
interface DetokenizeInput {
  tokens: number[];                  // Token IDs to convert
}
```

Output:
```typescript
interface DetokenizeResponse {
  content: string;                   // Detokenized text
}
```

---

###### 13. `llama_apply_template`
Purpose: Apply a chat template to messages for proper formatting.

Input:
```typescript
interface ApplyTemplateInput {
  messages: Array<{
    role: 'system' | 'user' | 'assistant';
    content: string;
  }>;
}
```

Output:
```typescript
interface ApplyTemplateResponse {
  content: string;                   // Formatted prompt with template applied
}
```

---

##### Model Management Tools (2 tools)

###### 14. `llama_load_model`
Purpose: Load a model into llama-server.

Input:
```typescript
interface LoadModelInput {
  model: string;                     // Model name or path to load
}
```

Output:
```typescript
interface LoadModelResponse {
  success: boolean;
  // ... loading status
}
```

---

###### 15. `llama_unload_model`
Purpose: Unload a model from llama-server.

Input:
```typescript
interface UnloadModelInput {
  model: string;                     // Model to unload
}
```

Output:
```typescript
interface UnloadModelResponse {
  success: boolean;
  // ... unloading status
}
```

---

##### LoRA Adapter Tools (2 tools)

###### 16. `llama_lora_list`
Purpose: List all loaded LoRA adapters.

Input: None (empty object)

Output:
```typescript
interface LoraListResponse {
  adapters: LoraAdapter[];
}

interface LoraAdapter {
  id: number;
  name: string;
  scale: number;
  // ... adapter metadata
}
```

---

###### 17. `llama_lora_set`
Purpose: Update LoRA adapter scales (set to 0 to disable).

Input:
```typescript
interface LoraSetInput {
  adapters: Array<{
    id: number;                      // Adapter ID
    scale: number;                   // Scale factor (0 to disable)
  }>;
}
```

Output:
```typescript
interface LoraSetResponse {
  success: boolean;
  // ... update status
}
```

---

##### Process Control Tools (2 tools)

###### 18. `llama_start`
Purpose: Start a llama-server process with specified parameters.

Input:
```typescript
interface StartInput {
  model: string;                     // Path to GGUF model file
  port?: number;                     // Port to listen on (default: 8080)
  ctx_size?: number;                 // Context size (default: 2048)
  n_gpu_layers?: number;             // GPU layers (-1 = all, default: -1)
  threads?: number;                  // CPU threads
}
```

Output:
```typescript
interface StartResponse {
  success: boolean;
  pid: number;                       // Process ID
  port: number;                      // Actual port
  // ... process status
}
```

---

###### 19. `llama_stop`
Purpose: Stop the running llama-server process.

Input: None (empty object)

Output:
```typescript
interface StopResponse {
  success: boolean;
  // ... termination status
}
```

---

#### How It Works

##### Communication Flow

```
MCP Client (Claude, Cursor, etc.)
         ↓
    llama-mcp-server (this project)
         ↓
    HTTP REST API calls
         ↓
    llama-server (llama.cpp HTTP server)
         ↓
    llama.cpp inference engine
```

##### Implementation Details

1. HTTP Client (`src/client.ts`)
   - Creates a typed `LlamaClient` interface
   - Handles all HTTP communication with llama-server
   - Methods: `health()`, `props()`, `models()`, `slots()`, `metrics()`, `complete()`, `chat()`, `embed()`, `infill()`, `rerank()`, `tokenize()`, `detokenize()`, `applyTemplate()`, `loadModel()`, `unloadModel()`, `loraList()`, `loraSet()`

2. Tool Registration (`src/server.ts`)
   - Uses `@modelcontextprotocol/sdk` to create MCP server
   - Each tool is registered with Zod schemas for input validation
   - Tools are organized by category (server, tokens, inference, models, lora, process)

3. Process Management (`src/tools/process.ts`)
   - Uses Node.js `child_process.spawn` to launch llama-server
   - Implements health checking with retry logic (`waitForHealth`)
   - Maintains process state for clean shutdown

4. Configuration (`src/config.ts`)
   - Reads from environment variables
   - Validates using Zod schemas
   - Provides sensible defaults

---

#### Key Features

##### Strengths
- Complete API Coverage: All 19 tools cover the full llama-server API surface
- Type Safety: Full TypeScript with Zod validation
- Process Management: Can start/stop llama-server directly
- LoRA Support: Native LoRA adapter management
- Standard Compliance: Follows MCP protocol specification
- Well-Structured: Modular codebase with clear separation of concerns

##### Dependencies
- `@modelcontextprotocol/sdk` - MCP protocol implementation
- `zod` - Runtime type validation
- Node.js built-ins (`child_process` for process spawning)

---

#### Usage Example

##### Starting the Server
```bash
# Set environment variables
export LLAMA_SERVER_URL=http://localhost:8080
export LLAMA_MODEL_PATH=/path/to/model.gguf

# Run the MCP server
npx tsx src/index.ts
```

##### Example Tool Call (Chat)
```json
{
  "tool": "llama_chat",
  "arguments": {
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "What is the capital of France?"}
    ],
    "max_tokens": 100,
    "temperature": 0.7
  }
}
```

---

#### File Structure

```
src/
├── index.ts          # Entry point
├── server.ts         # MCP server setup (registers all tools)
├── client.ts         # HTTP client for llama-server
├── config.ts         # Configuration loader
├── types.ts          # Shared TypeScript types
└── tools/
    ├── server.ts     # Health, props, models, slots, metrics tools
    ├── inference.ts  # Complete, chat, embed, infill, rerank tools
    ├── tokens.ts     # Tokenize, detokenize, apply_template tools
    ├── models.ts     # Load/unload model tools
    ├── lora.ts       # LoRA list/set tools
    └── process.ts    # Start/stop process tools
```

---

#### Summary

llama-mcp-server is a comprehensive, production-ready MCP server that provides full access to llama.cpp's capabilities through the Model Context Protocol. It enables AI assistants to:

- Run local LLM inference (chat, completion, embeddings)
- Manage models and LoRA adapters dynamically
- Monitor server health and performance
- Control the llama-server lifecycle
- Manipulate tokens and templates

The clean architecture, TypeScript typing, and comprehensive tool coverage make it an excellent choice for integrating local LLMs into MCP-compatible workflows.

## Candidates weighed

| Candidate | What it is | Verdict recorded | State on 2026-09-27 |
|---|---|---|---|
| `shin-bot-litellm/litellm-mcp` | wraps the whole OpenAPI of a LiteLLM proxy as 86 tools | wrong shape, too heavy | not found on GitHub (404), the account included |
| Composio's "Anthropic administrator" toolkit | a commercial hosted service | set aside | not checked |
| `ahays248/llama-mcp-server` | MCP server for llama.cpp's `llama-server` | the llama.cpp side only; a reference, not a dependency | not found on GitHub (404); the account exists |
| LiteLLM's `model_prices_and_context_window.json` | data file of model prices and capabilities | acceptable as vendored data, never as the library | the raw URL above resolves (HTTP 200) |
