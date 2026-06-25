> A captured design Q&A on model self-knowledge for agents (referenced from
> [`northstar.md`](northstar.md)): what an agent benefits from knowing about
> the model it runs on, and how much of that is observable from llama.cpp vs.
> the Anthropic API.

-----------------------------------------------
besides n_ctx, what else would be useful for an agent to know about the model being used?
-----------------------------------------------

Good question, and it gets at something real about how agents fail in practice — most of the catastrophic failures aren't reasoning failures, they're "the agent didn't know X about itself" failures.

Here's what I'd actually want an agent to have access to, organized by what it lets the agent do better.

**Capability awareness** — what the model can even do. Whether the model supports tool calling at all (and the calling style: native function calling, XML tags, JSON-only). Whether it supports parallel tool calls or only one at a time. Whether vision/audio modalities are loaded. Whether structured output (JSON schema, grammar-constrained) is available. An agent that doesn't know these will either try things that silently fail or refuse to try things it could have done. In llama-server's `/props`, the `chat_template_caps` and `modalities` fields cover some of this.

**Context budget, in a real-time sense.** `n_ctx` is just the ceiling. What an agent actually needs is "how much room do I have *right now*" — current token count, headroom remaining, and whether context shifting / SWA is on (which changes whether overflow means truncation or graceful sliding). Without that, agents either over-pack and get truncated mid-thought, or under-pack and waste budget. Token count for a planned action ("if I dump this 50KB file into context, will I survive?") is the single most useful signal an agent can have.

**Knowledge cutoff and current date.** Agents constantly make confident claims about "current" state of things — package versions, APIs, people in roles — that are months or years stale. Knowing the cutoff lets the agent decide *for itself* when to reach for a search tool versus answer from memory. Llama-server has `--chat-template-kwargs` and the `{{current_date}}` template variable for this; some models bake the cutoff into the system prompt automatically.

**Identity and lineage.** Model name, version, parameter count, quantization. Sounds vain but matters: a 4B model and a 70B model should make different decisions about when to delegate vs. attempt, when to ask for clarification vs. push through, how much to trust their own reasoning on edge cases. A well-calibrated small model that knows it's small can be more useful than a poorly calibrated big one.

**Reasoning configuration.** Whether thinking/reasoning mode is on, what the budget is (`--reasoning-budget`), what format thoughts come back in (`reasoning_content` field vs inline tags). Agents that emit thinking traces but get them stripped, or expect to think and don't get the budget, behave erratically.

**Sampling parameters in effect.** Temperature, top_p, repetition penalties, whether mirostat is on. An agent at temperature 0 should commit to its first plan; an agent at 0.9 should expect variance and maybe self-check more. Knowing this changes appropriate strategies for things like "try N times and take the best."

**Chat template constraints.** Whether the template supports system messages at all (some don't), whether it supports tool messages and in what shape, whether assistant prefilling works, whether multiple system messages get merged or rejected. Agents that try to inject mid-conversation system instructions on a template that flattens them will silently lose those instructions.

**Tool-calling format quirks.** Some models emit tool calls reliably only with specific Jinja templates, some need `--jinja` plus a specific override. Some emit JSON inside `<tool_call>` tags, some use OpenAI's structured field. Some support parallel calls in theory but degrade badly past 2-3. The agent benefits from knowing which family it's in.

**Speed and cost characteristics.** Tokens/sec for prompt processing vs generation, whether speculative decoding is on, whether KV cache reuse is enabled. Agents making cost/latency tradeoffs ("should I make 5 small calls or 1 big one?") need this. Llama-server's `timings` block gives historical numbers.

**Slot/concurrency state.** In llama-server specifically: how many slots, whether others are busy, whether prompt cache reuse will hit (via `cache_n` in timings). An agent doing parallel sub-tasks should know if it's actually getting parallelism or just queuing.

If I had to pick the three that move the needle most: **live token budget**, **tool-calling capability and format**, and **knowledge cutoff + current date**. The rest is optimization; those three prevent whole categories of failure.

Worth noting that very little of this is currently exposed cleanly to the model itself in most setups — it sits in the host's head, not the agent's prompt. The usual fix is for the host to inject a small "you are running on X with Y context, today is Z, you have these tools" preamble, which is crude but mostly works.


-----------------------------------------------
how much of that can we get from llama.cpp and how much of that can we get from Anthropic servers when we are using sonnet and opus?
-----------------------------------------------

Let me check Anthropic's API docs before answering — this is exactly the kind of question where my memory could be stale.The two answer surface very different slices, partly because they're different beasts — llama-server is a stateful local server you own, Anthropic's API is stateless and multi-tenant. Going through the list:

**Live token budget.** llama-server wins here. `/props` gives `n_ctx`, `/slots` gives per-slot state with `n_ctx` and processing flags, and every chat completion response includes a `timings` block with `cache_n`, `prompt_n`, `predicted_n` — letting you compute used and remaining in real time. There's also a `truncated` flag when the context overflowed. Anthropic gives you the post-hoc count in the response `usage` object (`input_tokens`, `output_tokens`, plus `cache_creation_input_tokens` / `cache_read_input_tokens`) and a pre-flight `/v1/messages/count_tokens` that takes the same request shape and returns just the input token count. No "current session state" concept because each request stands alone; you track budget yourself by summing across turns.

**Capability awareness.** llama-server's `/props` returns `modalities` (vision flag) and `chat_template_caps`. Tool use depends on `--jinja` and the model's template — discoverable but not always cleanly. Anthropic's Models API (`GET /v1/models`, `GET /v1/models/{id}`) returns a `capabilities` object plus `max_input_tokens` and `max_tokens` per model, documented per-model. Tool use, vision, and extended thinking are first-class and uniformly described.

**Knowledge cutoff & current date.** Neither exposes cutoff in the API response itself, but Anthropic publishes per-model "reliable knowledge cutoff" dates in the model docs that an agent's host can hardcode against the model id. llama-server doesn't expose this — you have to know what model you loaded. For current date, llama-server has the `{{current_date}}`, `{{current_time}}`, `{{current_timestamp}}` template variables; Anthropic expects you to inject it in your system prompt.

**Identity & lineage.** llama-server's `/v1/models` returns a single entry with rich metadata in `meta`: `n_vocab`, `n_ctx_train`, `n_embd`, `n_params`, `size` (file size in bytes). `/props` adds `model_path`, `build_info`, full `chat_template`. You see the entire stack down to quantization. Anthropic's `/v1/models/{id}` gives model id, display name, type, snapshot date — clean but minimal. No params, no quantization (they don't want you to know).

**Reasoning configuration.** Roughly comparable. llama-server: `--reasoning-format` and `--reasoning-budget` settings reflected in `/props`; responses include `reasoning_content`. Anthropic: a `thinking: {type: "enabled", budget_tokens: N}` object in the request, and response content blocks of type `thinking` (with a signature for verification). Anthropic's is more structured; llama.cpp's is more flexible.

**Sampling params.** llama-server gives you the entire `default_generation_settings` block via `/props` and per-slot overrides via `/slots` — temperature, top_p, top_k, mirostat, DRY, XTC, the works. Anthropic just echoes back what you sent (or rather, doesn't — the response doesn't include the input sampling params at all). You only know what you set yourself; Anthropic exposes `temperature`, `top_p`, `top_k`, `stop_sequences` and that's it.

**Chat template constraints.** llama-server lets you read the actual Jinja template source out of `/props.chat_template` and the parsed caps. Anthropic abstracts this away entirely — you send messages in their schema, they handle templating opaquely. That's a feature, not a bug, for most users.

**Tool-calling format quirks.** This is where Anthropic's managed surface really helps. One uniform format across all Claude models: `tool_use` content blocks emitted by the model, `tool_result` content blocks fed back in, `parallel_tool_use` standardized. With llama-server you're at the mercy of the model's training and the Jinja template — formats vary, `--jinja` flag matters, sometimes you need a chat-template override.

**Speed & cost.** llama-server gives you tokens/sec via the `timings` block (`prompt_per_second`, `predicted_per_second`) and `/metrics` for Prometheus-style aggregates. No money cost since it's local. Anthropic gives token counts in `usage` for cost calculation, and `service_tier` in the response indicating which tier processed the request, but no per-request latency breakdown — you measure that yourself with wall-clock. Pricing is documented per-model.

**Concurrency / rate state.** llama-server's `/slots` is the gold standard for a self-hosted server. Anthropic returns rate limit info via response headers: `anthropic-ratelimit-requests-remaining`, `-tokens-remaining`, `-input-tokens-remaining`, `-output-tokens-remaining`, plus retry-after when you 429. Different abstraction — slot accounting vs. quota accounting — but Anthropic's headers tell an agent exactly when to back off.

The shape of the difference: llama-server is more transparent about *the model and its mechanics* (full template, full sampling state, full cache state, parameter counts), while Anthropic is more transparent about *the model's contract* (capabilities object, max input/output, structured tool/thinking schema, rate limit budget). For an agent, the Anthropic side is more useful for planning ("can this model use vision? how many tokens fit? how close am I to my quota?"); the llama-server side is more useful for diagnosis and tuning ("why did this hit context? what sampler order is in effect? is my prompt cache being reused?").

The one thing neither exposes well to the model itself, in either case, is real-time context occupancy — both still expect the host to track that and tell the model when relevant.