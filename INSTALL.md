# Open WebUI Native News Tab (`/news`)

This package adds a **clickable News tab** in Open WebUI's left sidebar that opens a dedicated `/news` page showing up-to-date, wire-deduplicated, multi-spectrum news summaries with direct publisher click-out links.

## Files Included
- `backend/open_webui/routers/news.py` — FastAPI `/api/v1/news/feed` endpoint (RSS + optional SearXNG, 5-gram wire deduplication, clustering, L/C/R spectrum & Blindspot scoring, SQLite cache, and local Ollama summarization).
- `src/routes/(app)/news/+page.svelte` — Dedicated SvelteKit `/news` frontend page styled with Open WebUI's native Tailwind classes (Light / Dark / OLED).
- `Sidebar.news-snippet.svelte` — Clickable `<a href="/news">` sidebar tab with newspaper icon.
- `apply_patch.py` — Installer script for an `open-webui` git checkout.

## Install & Build
```bash
git clone https://github.com/open-webui/open-webui.git
python3 apply_patch.py ./open-webui
cd open-webui
docker build -t open-webui-news .
```

## Optional Environment Variables
- `NEWS_LLM_BASE_URL` (default: `http://host.docker.internal:11434/v1`)
- `NEWS_SUMMARIZER_MODEL` (default: `llama3.2:3b`)
- `NEWS_SEARXNG_URL` (optional SearXNG URL)
- `NEWS_REFRESH_TTL_SECS` (default: `1800`)
