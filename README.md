# Unbiased News Desk for Open WebUI

A zero-fork Open WebUI Pipe Function that delivers a Perplexity-style unbiased news feed with 2-sentence neutral summaries, wire-copy deduplication, Left/Center/Right spectrum bars, Blindspot detection, and direct publisher click-out links.

## Quick Install (Open WebUI)

1. Open **Open WebUI -> Workspace -> Functions -> + New Function**.
2. Paste the contents of `unbiased_news_pipe.py` (or import it) and click **Save**.
3. Toggle the function **On** and open its **Valves** (gear icon):
   - `llm_base_url`: Your Ollama or OpenAI-compatible URL (e.g. `http://host.docker.internal:11434/v1` if Open WebUI is in Docker).
   - `summarizer_model`: A fast local model such as `llama3.2:3b` or `qwen2.5:3b`.
   - `searxng_url` (optional): Your SearXNG URL (e.g. `http://searxng:8080`) to supplement RSS feeds.
   - `rss_sources_json`: Customize your RSS outlets and `L` / `C` / `R` bias tags (see `sources.example.json`).
4. Start a new chat and select **Unbiased News Desk** from the model picker.
5. Send `news` (or any topic keyword, `refresh`, or `blindspot`).
