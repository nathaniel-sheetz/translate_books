# LLM Provider Configuration

This guide covers how to configure LLM providers and models, add new providers (like DeepInfra, Together, Groq), and use the model selection UI in the dashboard.

## Quick Start

1. Copy the example config:
   ```bash
   cp llm_config.example.json llm_config.json
   ```

2. Add your API keys to `.env`:
   ```bash
   ANTHROPIC_API_KEY=sk-ant-...
   OPENAI_API_KEY=sk-...
   DEEPINFRA_API_KEY=...
   ```

3. Start the server. The dashboard dropdowns will show all configured providers, with unavailable ones (missing API key) grayed out.

---

## Config File: `llm_config.json`

The file lives at the project root. It defines which providers and models are available throughout the app. It is gitignored so each user can customize it independently.

### Schema

```json
{
  "default_provider": "anthropic",
  "default_model": "claude-sonnet-4-20250514",
  "providers": [
    {
      "id": "anthropic",
      "name": "Anthropic (Claude)",
      "type": "anthropic",
      "api_key_env_var": "ANTHROPIC_API_KEY",
      "models": [
        {
          "id": "claude-sonnet-4-20250514",
          "name": "Claude Sonnet 4",
          "pricing": { "input": 3.00, "output": 15.00 }
        }
      ]
    }
  ]
}
```

### Field Reference

| Field | Description |
|---|---|
| `default_provider` | Provider ID pre-selected in all dropdowns |
| `default_model` | Model ID pre-selected when the default provider is active |
| `providers[].id` | Unique identifier (used in API calls and internally) |
| `providers[].name` | Display name shown in the UI dropdown |
| `providers[].type` | SDK routing: `"anthropic"` or `"openai-compatible"` |
| `providers[].api_key_env_var` | Name of the environment variable holding the API key |
| `providers[].base_url` | API endpoint URL. `null` for native OpenAI; a URL string for third-party endpoints |
| `providers[].models[].id` | Model identifier passed to the API |
| `providers[].models[].name` | Display name in the model dropdown |
| `providers[].models[].pricing` | `{ "input": X, "output": Y }` per 1M tokens, used for cost estimates |

### Provider Types

There are only two `type` values:

- **`"anthropic"`** -- Uses the `anthropic` Python SDK (`client.messages.create`). Only Anthropic's own API uses this type.
- **`"openai-compatible"`** -- Uses the `openai` Python SDK (`client.chat.completions.create`) with an optional `base_url`. This covers native OpenAI, DeepInfra, Together, Groq, Ollama, and any other service that implements the OpenAI chat completions protocol.

When `base_url` is `null`, the OpenAI SDK uses its default endpoint (`https://api.openai.com/v1`). For third-party providers, set `base_url` to their API endpoint.

---

## Adding a New Provider

To add any OpenAI-compatible provider, add an entry to the `providers` array in `llm_config.json`:

```json
{
  "id": "together",
  "name": "Together AI",
  "type": "openai-compatible",
  "api_key_env_var": "TOGETHER_API_KEY",
  "base_url": "https://api.together.xyz/v1",
  "models": [
    {
      "id": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
      "name": "Llama 3.3 70B Turbo",
      "pricing": { "input": 0.88, "output": 0.88 }
    }
  ]
}
```

Then add the API key to your `.env`:

```bash
TOGETHER_API_KEY=your_key_here
```

No code changes are needed. The new provider will appear in all dashboard dropdowns on the next page load.

### Common Provider Base URLs

| Provider | `base_url` |
|---|---|
| OpenAI (native) | `null` (SDK default) |
| DeepInfra | `https://api.deepinfra.com/v1/openai` |
| Together AI | `https://api.together.xyz/v1` |
| Groq | `https://api.groq.com/openai/v1` |
| Ollama (local) | `http://localhost:11434/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |

---

## Model Selection in the Dashboard

Three places in the dashboard now have LLM provider/model dropdowns:

### Style Guide (Stage 4)

A shared LLM selector appears at the top of the style guide wizard. Two actions use it:

- **Generate Questions via API** (Step 2) -- Calls the LLM to generate additional style questions from your source text. The response is parsed as JSON and rendered as interactive question blocks. Falls back to showing raw text in the paste area if the response isn't valid JSON.

- **Generate Style Guide via API** (Step 3) -- Calls the LLM to generate a complete style guide from your answers. The result appears in the preview area, ready to save.

Both actions sit alongside the existing copy/paste workflow. You can still click "Show Prompt to Copy" and use an external LLM if you prefer.

### Glossary (Stage 5)

A provider/model selector appears in Step 3 ("Bootstrap Translations via LLM"):

- **Generate via API** -- Sends glossary candidates to the LLM for translation. The response is parsed as a JSON array and populates the review table. If the response isn't valid JSON, the raw text is placed in the paste area for manual editing.

The existing copy/paste workflow remains available below.

### Translation (Stage 6)

The batch translate modal shows provider and model dropdowns, now dynamically populated from `llm_config.json` instead of hardcoded. The cost estimate updates when you change the selection.

---

## How It Works Internally

### Config Loading

`src/api_translator.py` loads `llm_config.json` once and caches it in memory. Key functions:

| Function | Purpose |
|---|---|
| `load_llm_config()` | Reads and caches the config file. Falls back to a built-in default (Anthropic + OpenAI) if the file is missing. |
| `get_provider_config(id)` | Looks up a provider by ID. Raises `ValueError` if not found. |
| `get_default_model()` | Returns `default_model` from config. |
| `get_model_pricing(provider, model)` | Returns pricing dict for cost estimation. Falls back to conservative defaults for unknown models. |
| `get_pricing_table()` | Builds the full pricing table from config (backward-compatible with older code). |

### API Dispatch

All LLM calls go through `call_llm()`, which resolves the provider config and calls `_dispatch_llm_call()`. The dispatcher reads the provider's `type` field to choose the SDK:

```
call_llm(prompt, provider, model)
  -> _dispatch_llm_call(prompt, provider, model)
       -> if type == "anthropic":  call_anthropic_api(prompt, model, api_key=...)
       -> if type == "openai-compatible":  call_openai_api(prompt, model, api_key=..., base_url=...)
```

`translate_chunk_realtime()` delegates to `call_llm()` for unified dispatch and retry logic.

### Frontend Config Endpoint

`GET /api/llm-config` serves the config to the frontend JavaScript. For security, it strips `api_key_env_var` from each provider and adds an `available` boolean indicating whether the key is set in the environment.

### Generate Endpoints

Three new endpoints handle direct LLM calls from the dashboard:

| Endpoint | Purpose |
|---|---|
| `POST /api/setup/<id>/questions/generate` | Generate style guide questions via LLM |
| `POST /api/setup/<id>/style-guide/generate` | Generate style guide via LLM |
| `POST /api/setup/<id>/glossary/generate` | Generate glossary translations via LLM |

All accept `{ "provider": "...", "model": "...", ... }` in the request body and return the LLM response. They reuse the same prompt-building functions as the existing copy/paste endpoints.

---

## Batch Translation and Third-Party Providers

The Anthropic and OpenAI batch APIs (`messages.batches` and `/v1/batches`) are provider-specific. Third-party providers generally don't support these.

However, the dashboard's "Batch Translate" feature does **not** use the batch API. It translates chunks sequentially via real-time calls in a background thread, streaming progress via SSE. This works with all providers, including third-party ones.

The file-based batch API (`scripts/translate_api.py --batch`) is only available for Anthropic and OpenAI.

---

## Troubleshooting

**Provider shows "(no API key)" in dropdown:**
The env var specified in `api_key_env_var` is not set. Add it to your `.env` file and restart the server.

**"Unknown provider" error:**
The `provider` value sent from the frontend doesn't match any `id` in `llm_config.json`. Check that the config file is valid JSON and contains the provider.

**Config changes not taking effect:**
The config is cached in memory. Restart the server to reload, or call `load_llm_config(force_reload=True)` programmatically.

**LLM response is not valid JSON (style guide questions / glossary):**
Some models return markdown fences or extra text around JSON. The raw response is placed in the paste area so you can manually clean and parse it.
