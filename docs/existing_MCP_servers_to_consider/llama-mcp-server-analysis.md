**Repository:** [ahays248/llama-mcp-server](https://github.com/ahays248/llama-mcp-server)

# llama-mcp-server Analysis Report

## Overview

**llama-mcp-server** is a Model Context Protocol (MCP) server that provides comprehensive integration with **llama-server** (the HTTP server component of llama.cpp). It exposes 19 tools that enable AI assistants and applications to interact with local LLMs running via llama.cpp.

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

## Architecture

### Core Components

1. **`src/client.ts`** - HTTP client for communicating with llama-server REST API
2. **`src/config.ts`** - Configuration loading from environment variables
3. **`src/server.ts`** - MCP server setup that registers all 19 tools
4. **`src/index.ts`** - Entry point that initializes the server
5. **`src/types.ts`** - Shared TypeScript interfaces and types
6. **`src/tools/*.ts`** - Individual tool implementations organized by category

### Configuration

The server reads configuration from environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLAMA_SERVER_URL` | `http://localhost:8080` | URL of the llama-server instance |
| `LLAMA_TIMEOUT` | `30000` (ms) | Request timeout |
| `LLAMA_MODEL_PATH` | (optional) | Default model path for start tool |
| `LLAMA_SERVER_PATH` | `llama-server` | Path to llama-server executable |

---

## All 19 MCP Tools

### 🔧 Server Management Tools (5 tools)

#### 1. `llama_health`
**Purpose:** Check if llama-server is running and healthy.

**Input:** None (empty object)

**Output:**
```typescript
interface HealthResponse {
  status: string;           // "ok" when healthy
  total_slots: number;      // Total available slots
  used_slots: number;       // Currently used slots
}
```

---

#### 2. `llama_props`
**Purpose:** Get or update server default generation settings.

**Input:**
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

**Output:**
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

#### 3. `llama_models`
**Purpose:** List all loaded models on the server.

**Input:** None (empty object)

**Output:**
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

#### 4. `llama_slots`
**Purpose:** Get information about all inference slots (concurrent request handlers).

**Input:** None (empty object)

**Output:**
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

#### 5. `llama_metrics`
**Purpose:** Get server performance metrics.

**Input:** None (empty object)

**Output:**
```typescript
interface MetricsResponse {
  // Performance counters, timing information, etc.
}
```

---

### 🗣️ Inference Tools (5 tools)

#### 6. `llama_complete`
**Purpose:** Generate text completion from a prompt.

**Input:**
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

**Output:**
```typescript
interface CompletionResponse {
  content: string;                   // Generated text
  tokens_predicted: number;
  tokens_evaluated: number;
  // ... timing and usage stats
}
```

---

#### 7. `llama_chat`
**Purpose:** Multi-turn chat completion with message history.

**Input:**
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

**Output:**
```typescript
interface ChatResponse {
  content: string;                   // Assistant response
  model: string;
  // ... usage and timing stats
}
```

---

#### 8. `llama_embed`
**Purpose:** Generate vector embeddings for text.

**Input:**
```typescript
interface EmbedInput {
  content: string;                   // Text to embed
}
```

**Output:**
```typescript
interface EmbedResponse {
  embedding: number[];              // Vector embedding
  // ... metadata
}
```

---

#### 9. `llama_infill`
**Purpose:** Fill in missing code between prefix and suffix (code completion).

**Input:**
```typescript
interface InfillInput {
  input_prefix: string;              // Code before cursor
  input_suffix: string;              // Code after cursor
  max_tokens?: number;               // Maximum tokens (default: 256)
  temperature?: number;              // Temperature (default: 0.7)
  stop?: string[];                   // Stop sequences
}
```

**Output:**
```typescript
interface InfillResponse {
  content: string;                   // Generated code infill
  // ... usage stats
}
```

---

#### 10. `llama_rerank`
**Purpose:** Rerank documents based on relevance to a query.

**Input:**
```typescript
interface RerankInput {
  query: string;                     // Search query
  documents: string[];               // Documents to rerank
}
```

**Output:**
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

### 🔤 Token Manipulation Tools (3 tools)

#### 11. `llama_tokenize`
**Purpose:** Convert text to token IDs.

**Input:**
```typescript
interface TokenizeInput {
  content: string;                   // Text to tokenize
  add_special?: boolean;             // Add BOS/EOS tokens (default: true)
  with_pieces?: boolean;             // Include token strings (default: false)
}
```

**Output:**
```typescript
interface TokenizeResponse {
  tokens: number[];                  // Token IDs
  // pieces?: string[];              // Optional token strings
}
```

---

#### 12. `llama_detokenize`
**Purpose:** Convert token IDs back to text.

**Input:**
```typescript
interface DetokenizeInput {
  tokens: number[];                  // Token IDs to convert
}
```

**Output:**
```typescript
interface DetokenizeResponse {
  content: string;                   // Detokenized text
}
```

---

#### 13. `llama_apply_template`
**Purpose:** Apply a chat template to messages for proper formatting.

**Input:**
```typescript
interface ApplyTemplateInput {
  messages: Array<{
    role: 'system' | 'user' | 'assistant';
    content: string;
  }>;
}
```

**Output:**
```typescript
interface ApplyTemplateResponse {
  content: string;                   // Formatted prompt with template applied
}
```

---

### 📦 Model Management Tools (2 tools)

#### 14. `llama_load_model`
**Purpose:** Load a model into llama-server.

**Input:**
```typescript
interface LoadModelInput {
  model: string;                     // Model name or path to load
}
```

**Output:**
```typescript
interface LoadModelResponse {
  success: boolean;
  // ... loading status
}
```

---

#### 15. `llama_unload_model`
**Purpose:** Unload a model from llama-server.

**Input:**
```typescript
interface UnloadModelInput {
  model: string;                     // Model to unload
}
```

**Output:**
```typescript
interface UnloadModelResponse {
  success: boolean;
  // ... unloading status
}
```

---

### 🎛️ LoRA Adapter Tools (2 tools)

#### 16. `llama_lora_list`
**Purpose:** List all loaded LoRA adapters.

**Input:** None (empty object)

**Output:**
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

#### 17. `llama_lora_set`
**Purpose:** Update LoRA adapter scales (set to 0 to disable).

**Input:**
```typescript
interface LoraSetInput {
  adapters: Array<{
    id: number;                      // Adapter ID
    scale: number;                   // Scale factor (0 to disable)
  }>;
}
```

**Output:**
```typescript
interface LoraSetResponse {
  success: boolean;
  // ... update status
}
```

---

### 🚀 Process Control Tools (2 tools)

#### 18. `llama_start`
**Purpose:** Start a llama-server process with specified parameters.

**Input:**
```typescript
interface StartInput {
  model: string;                     // Path to GGUF model file
  port?: number;                     // Port to listen on (default: 8080)
  ctx_size?: number;                 // Context size (default: 2048)
  n_gpu_layers?: number;             // GPU layers (-1 = all, default: -1)
  threads?: number;                  // CPU threads
}
```

**Output:**
```typescript
interface StartResponse {
  success: boolean;
  pid: number;                       // Process ID
  port: number;                      // Actual port
  // ... process status
}
```

---

#### 19. `llama_stop`
**Purpose:** Stop the running llama-server process.

**Input:** None (empty object)

**Output:**
```typescript
interface StopResponse {
  success: boolean;
  // ... termination status
}
```

---

## How It Works

### Communication Flow

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

### Implementation Details

1. **HTTP Client (`src/client.ts`)**
   - Creates a typed `LlamaClient` interface
   - Handles all HTTP communication with llama-server
   - Methods: `health()`, `props()`, `models()`, `slots()`, `metrics()`, `complete()`, `chat()`, `embed()`, `infill()`, `rerank()`, `tokenize()`, `detokenize()`, `applyTemplate()`, `loadModel()`, `unloadModel()`, `loraList()`, `loraSet()`

2. **Tool Registration (`src/server.ts`)**
   - Uses `@modelcontextprotocol/sdk` to create MCP server
   - Each tool is registered with Zod schemas for input validation
   - Tools are organized by category (server, tokens, inference, models, lora, process)

3. **Process Management (`src/tools/process.ts`)**
   - Uses Node.js `child_process.spawn` to launch llama-server
   - Implements health checking with retry logic (`waitForHealth`)
   - Maintains process state for clean shutdown

4. **Configuration (`src/config.ts`)**
   - Reads from environment variables
   - Validates using Zod schemas
   - Provides sensible defaults

---

## Key Features

### ✅ Strengths
- **Complete API Coverage:** All 19 tools cover the full llama-server API surface
- **Type Safety:** Full TypeScript with Zod validation
- **Process Management:** Can start/stop llama-server directly
- **LoRA Support:** Native LoRA adapter management
- **Standard Compliance:** Follows MCP protocol specification
- **Well-Structured:** Modular codebase with clear separation of concerns

### 📋 Dependencies
- `@modelcontextprotocol/sdk` - MCP protocol implementation
- `zod` - Runtime type validation
- Node.js built-ins (`child_process` for process spawning)

---

## Usage Example

### Starting the Server
```bash
# Set environment variables
export LLAMA_SERVER_URL=http://localhost:8080
export LLAMA_MODEL_PATH=/path/to/model.gguf

# Run the MCP server
npx tsx src/index.ts
```

### Example Tool Call (Chat)
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

## File Structure

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

## Summary

**llama-mcp-server** is a comprehensive, production-ready MCP server that provides full access to llama.cpp's capabilities through the Model Context Protocol. It enables AI assistants to:

- Run local LLM inference (chat, completion, embeddings)
- Manage models and LoRA adapters dynamically
- Monitor server health and performance
- Control the llama-server lifecycle
- Manipulate tokens and templates

The clean architecture, TypeScript typing, and comprehensive tool coverage make it an excellent choice for integrating local LLMs into MCP-compatible workflows.