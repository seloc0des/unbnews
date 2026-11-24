"""
title: Unbiased News Desk
author: Open WebUI Prototype
version: 0.2.0
description: Perplexity-style unbiased news feed Pipe for Open WebUI. Fetches multi-spectrum RSS/SearXNG stories, deduplicates syndicated wire copy, clusters events, generates 2-sentence neutral summaries via local/cloud LLM, and renders an Open WebUI-native Rich UI gateway with direct publisher click-out links.
requirements: httpx, pydantic
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import escape
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field


DEFAULT_SOURCES = [
    {"name": "NPR", "url": "https://feeds.npr.org/1001/rss.xml", "bias": "C"},
    {"name": "BBC World", "url": "https://feeds.bbci.co.uk/news/world/rss.xml", "bias": "C"},
    {"name": "The Guardian", "url": "https://www.theguardian.com/world/rss", "bias": "L"},
    {"name": "Fox News", "url": "https://moxie.foxnews.com/google-publisher/world.xml", "bias": "R"},
    {"name": "CS Monitor", "url": "https://rss.csmonitor.com/feeds/all", "bias": "C"},
    {"name": "The Hill", "url": "https://thehill.com/feed/", "bias": "C"},
]

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of",
    "with", "by", "from", "as", "is", "are", "was", "were", "be", "been", "being",
    "that", "this", "it", "its", "over", "after", "amid", "into", "new", "says",
    "say", "said", "will", "would", "could", "should", "about", "more", "than",
}


@dataclass
class Article:
    id: str
    title: str
    summary: str
    url: str
    source: str
    bias: str  # "L", "C", "R"
    published: str
    shingles: set[str]
    tokens: set[str]


def _clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]{3,}", text.lower())
    return {w for w in words if w not in STOPWORDS}


def _shingles(text: str, k: int = 5) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    if len(words) < k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def deduplicate_wire_copy(articles: list[Article], threshold: float = 0.65) -> list[Article]:
    """Collapse near-identical syndicated wire reprints (e.g. AP/Reuters) into one source."""
    unique: list[Article] = []
    for art in articles:
        is_wire_dup = any(_jaccard(art.shingles, kept.shingles) >= threshold for kept in unique)
        if not is_wire_dup:
            unique.append(art)
    return unique


def cluster_articles(articles: list[Article], sim_threshold: float = 0.25) -> list[list[Article]]:
    """Group deduplicated articles covering the same event into multi-source clusters."""
    clusters: list[list[Article]] = []
    cluster_tokens: list[set[str]] = []

    for art in articles:
        best_idx = -1
        best_sim = 0.0
        for idx, c_toks in enumerate(cluster_tokens):
            sim = _overlap(art.tokens, c_toks)
            if sim > best_sim:
                best_sim = sim
                best_idx = idx
        if best_idx >= 0 and best_sim >= sim_threshold:
            clusters[best_idx].append(art)
            cluster_tokens[best_idx] |= art.tokens
        else:
            clusters.append([art])
            cluster_tokens.append(set(art.tokens))

    # Prefer multi-source clusters first, then recency order
    clusters.sort(key=lambda c: len({a.source for a in c}), reverse=True)
    return clusters


def compute_spectrum(cluster: list[Article]) -> dict[str, Any]:
    counts = {"L": 0, "C": 0, "R": 0}
    seen_sources: set[str] = set()
    for art in cluster:
        if art.source in seen_sources:
            continue
        seen_sources.add(art.source)
        b = art.bias if art.bias in counts else "C"
        counts[b] += 1

    total = sum(counts.values())
    blindspot: Optional[str] = None
    if total >= 2:
        if counts["L"] / total >= 0.75 and counts["R"] == 0:
            blindspot = "Right Blindspot"
        elif counts["R"] / total >= 0.75 and counts["L"] == 0:
            blindspot = "Left Blindspot"

    return {"counts": counts, "total": total, "blindspot": blindspot}


def parse_rss_xml(xml_text: str, source_name: str, bias: str) -> list[Article]:
    articles: list[Article] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return articles

    items = root.findall(".//item")
    is_atom = False
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        is_atom = True

    for item in items[:15]:
        if is_atom:
            title = _clean_text(item.findtext("{http://www.w3.org/2005/Atom}title") or "")
            summary = _clean_text(
                item.findtext("{http://www.w3.org/2005/Atom}summary")
                or item.findtext("{http://www.w3.org/2005/Atom}content")
                or ""
            )
            link_el = item.find("{http://www.w3.org/2005/Atom}link")
            url = (link_el.attrib.get("href") if link_el is not None else "") or ""
            pub = _clean_text(item.findtext("{http://www.w3.org/2005/Atom}updated") or "")
        else:
            title = _clean_text(item.findtext("title") or "")
            summary = _clean_text(item.findtext("description") or "")
            url = _clean_text(item.findtext("link") or "")
            pub = _clean_text(item.findtext("pubDate") or "")

        if not title or not url:
            continue

        body_for_dedupe = f"{title}. {summary}"
        art_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        articles.append(
            Article(
                id=art_id,
                title=title,
                summary=summary[:500],
                url=url,
                source=source_name,
                bias=bias,
                published=pub,
                shingles=_shingles(body_for_dedupe),
                tokens=_tokenize(f"{title} {summary[:240]}"),
            )
        )
    return articles


class Pipe:
    class Valves(BaseModel):
        rss_sources_json: str = Field(
            default=json.dumps(DEFAULT_SOURCES),
            description="JSON list of {name, url, bias} objects where bias is L, C, or R.",
        )
        searxng_url: str = Field(
            default="",
            description="Optional SearXNG base URL (e.g. http://searxng:8080) to supplement RSS.",
        )
        llm_base_url: str = Field(
            default="http://localhost:11434/v1",
            description="OpenAI-compatible LLM endpoint (e.g. Ollama) for neutral 2-sentence briefs.",
        )
        llm_api_key: str = Field(
            default="ollama",
            description="API key for the summarizer LLM endpoint.",
        )
        summarizer_model: str = Field(
            default="llama3.2:3b",
            description="Fast local or cloud model used to synthesize neutral 2-sentence briefs.",
        )
        cache_db_path: str = Field(
            default="/tmp/unbiased_news_cache.db",
            description="SQLite path for cached story clusters and summaries.",
        )
        refresh_interval_mins: int = Field(
            default=30,
            description="Minutes before cached feed refreshes automatically.",
        )
        max_summary_words: int = Field(
            default=45,
            description="Maximum word count for each 2-sentence story brief.",
        )

    class UserValves(BaseModel):
        topics: str = Field(
            default="",
            description="Optional comma-separated topic keywords to filter stories (blank = all).",
        )
        blindspot_only: bool = Field(
            default=False,
            description="Show only stories flagged as Left or Right Blindspots.",
        )
        max_stories: int = Field(
            default=9,
            description="Maximum number of story cards to render in the feed.",
        )
        theme: str = Field(
            default="auto",
            description="UI theme override: auto, light, dark, or oled (matches Open WebUI).",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    def pipes(self) -> list[dict[str, str]]:
        return [{"id": "unbiased-news-desk", "name": "Unbiased News Desk"}]

    def _init_db(self) -> sqlite3.Connection:
        db_dir = os.path.dirname(self.valves.cache_db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(self.valves.cache_db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feed_cache (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                updated_at REAL NOT NULL,
                stories_json TEXT NOT NULL
            )
            """
        )
        conn.commit()
        return conn

    def _read_cache(self) -> Optional[tuple[float, list[dict[str, Any]]]]:
        conn = self._init_db()
        try:
            row = conn.execute("SELECT updated_at, stories_json FROM feed_cache WHERE id = 1").fetchone()
            if not row:
                return None
            return float(row[0]), json.loads(row[1])
        finally:
            conn.close()

    def _write_cache(self, stories: list[dict[str, Any]]) -> None:
        conn = self._init_db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO feed_cache (id, updated_at, stories_json) VALUES (1, ?, ?)",
                (time.time(), json.dumps(stories)),
            )
            conn.commit()
        finally:
            conn.close()

    async def _fetch_all_sources(self) -> list[Article]:
        try:
            sources = json.loads(self.valves.rss_sources_json)
        except json.JSONDecodeError:
            sources = DEFAULT_SOURCES

        articles: list[Article] = []
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            tasks = [
                client.get(src["url"], headers={"User-Agent": "OpenWebUI-NewsDesk/0.2"})
                for src in sources
                if src.get("url")
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            for src, resp in zip(sources, responses):
                if isinstance(resp, Exception) or resp.status_code != 200:
                    continue
                articles.extend(parse_rss_xml(resp.text, src.get("name", "Source"), src.get("bias", "C")))

            if self.valves.searxng_url.strip():
                try:
                    sx_url = self.valves.searxng_url.rstrip("/") + "/search"
                    sx_resp = await client.get(
                        sx_url,
                        params={"q": "world top news today", "categories": "news", "format": "json"},
                    )
                    if sx_resp.status_code == 200:
                        for res in sx_resp.json().get("results", [])[:10]:
                            title = _clean_text(res.get("title", ""))
                            snippet = _clean_text(res.get("content", ""))
                            url = res.get("url", "")
                            if not title or not url:
                                continue
                            domain = urlparse(url).netloc.replace("www.", "")
                            articles.append(
                                Article(
                                    id=hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
                                    title=title,
                                    summary=snippet[:500],
                                    url=url,
                                    source=domain or "Web",
                                    bias="C",
                                    published="",
                                    shingles=_shingles(f"{title}. {snippet}"),
                                    tokens=_tokenize(f"{title} {snippet[:240]}"),
                                )
                            )
                except Exception:
                    pass

        return articles

    async def _synthesize_story(self, client: httpx.AsyncClient, cluster: list[Article]) -> dict[str, str]:
        """Generate a neutral headline and strict 2-sentence brief via LLM, with extractive fallback."""
        lead = cluster[0]
        fallback_brief = lead.summary.split(". ")[0].strip()
        if fallback_brief and not fallback_brief.endswith("."):
            fallback_brief += "."
        words = fallback_brief.split()
        if len(words) > self.valves.max_summary_words:
            fallback_brief = " ".join(words[: self.valves.max_summary_words]) + "..."

        source_notes = "\n".join(
            f"- [{a.source} ({a.bias})]: {a.title}. {a.summary[:220]}"
            for a in cluster[:5]
        )
        prompt = (
            "Summarize this news cluster neutrally for a quick-scan feed.\n"
            "Rules:\n"
            "1. Output valid JSON with keys: headline, brief.\n"
            "2. headline: max 12 words, neutral, factual, no clickbait.\n"
            f"3. brief: EXACTLY 2 short sentences (max {self.valves.max_summary_words} words total). "
            "Sentence 1 states what happened. Sentence 2 states the key context or competing view.\n"
            "4. Use plain, unbiased language.\n\n"
            f"Sources:\n{source_notes}"
        )

        try:
            resp = await client.post(
                f"{self.valves.llm_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.valves.llm_api_key}"},
                json={
                    "model": self.valves.summarizer_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                },
                timeout=15.0,
            )
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                headline = _clean_text(parsed.get("headline", "")) or lead.title
                brief = _clean_text(parsed.get("brief", "")) or fallback_brief
                return {"headline": headline, "brief": brief}
        except Exception:
            pass

        return {"headline": lead.title, "brief": fallback_brief or "Click a source below to read the full report."}

    async def _build_feed_data(self) -> list[dict[str, Any]]:
        raw_articles = await self._fetch_all_sources()
        deduped = deduplicate_wire_copy(raw_articles)
        clusters = cluster_articles(deduped)[:14]

        stories: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=15.0) as client:
            synth_tasks = [self._synthesize_story(client, c) for c in clusters]
            synths = await asyncio.gather(*synth_tasks)

        for cluster, synth in zip(clusters, synths):
            spec = compute_spectrum(cluster)
            seen_urls: set[str] = set()
            links: list[dict[str, str]] = []
            for a in cluster:
                if a.url in seen_urls:
                    continue
                seen_urls.add(a.url)
                links.append({"source": a.source, "url": a.url, "bias": a.bias})
                if len(links) >= 4:
                    break

            stories.append(
                {
                    "headline": synth["headline"],
                    "brief": synth["brief"],
                    "spectrum": spec["counts"],
                    "total_sources": spec["total"],
                    "blindspot": spec["blindspot"],
                    "links": links,
                }
            )
        self._write_cache(stories)
        return stories

    def render_rich_ui(
        self,
        stories: list[dict[str, Any]],
        cached_age_mins: int,
        theme_mode: str = "auto",
    ) -> str:
        cards_html = []
        for idx, s in enumerate(stories):
            spec = s["spectrum"]
            total = max(1, s.get("total_sources") or (spec["L"] + spec["C"] + spec["R"]))
            pct_l = round((spec["L"] / total) * 100)
            pct_c = round((spec["C"] / total) * 100)
            pct_r = max(0, 100 - pct_l - pct_c)

            is_balanced = spec["L"] > 0 and spec["R"] > 0
            has_blindspot = bool(s.get("blindspot"))
            card_classes = ["story-card"]
            if idx == 0 and len(stories) > 2:
                card_classes.append("lead-card")

            blindspot_html = (
                f'<span class="badge blindspot">{escape(s["blindspot"])}</span>'
                if has_blindspot
                else (
                    '<span class="badge balanced">Multi-wing</span>'
                    if is_balanced
                    else ""
                )
            )

            link_pills = "".join(
                f'<a class="src-pill" href="{escape(lk["url"])}" target="_blank" rel="noopener noreferrer">'
                f'<span class="dot dot-{escape(lk["bias"])}"></span>'
                f'<span>{escape(lk["source"])}</span>'
                f'<span class="ext">&#8599;</span></a>'
                for lk in s["links"]
            )
            prompt_text = escape(f"Explain the different perspectives on: {s['headline']}", quote=True)

            cards_html.append(
                f"""
                <article class="{' '.join(card_classes)}" data-balanced="{str(is_balanced).lower()}" data-blindspot="{str(has_blindspot).lower()}">
                  <div class="card-meta">
                    <span class="source-count">{total} {"source" if total == 1 else "sources"}</span>
                    {blindspot_html}
                  </div>
                  <h3>{escape(s["headline"])}</h3>
                  <p class="brief">{escape(s["brief"])}</p>
                  <div class="spectrum-bar" title="Coverage spectrum: Left {spec['L']}, Center {spec['C']}, Right {spec['R']}">
                    <span class="seg seg-L" style="width:{pct_l}%"></span>
                    <span class="seg seg-C" style="width:{pct_c}%"></span>
                    <span class="seg seg-R" style="width:{pct_r}%"></span>
                  </div>
                  <div class="spectrum-labels">
                    <span><i class="dot dot-L"></i>L {spec['L']}</span>
                    <span><i class="dot dot-C"></i>C {spec['C']}</span>
                    <span><i class="dot dot-R"></i>R {spec['R']}</span>
                  </div>
                  <div class="card-actions">
                    <div class="links">{link_pills}</div>
                    <button class="ask-pill" type="button" onclick="askAI('{prompt_text}')">Ask AI &#10022;</button>
                  </div>
                </article>
                """
            )

        grid_content = (
            "".join(cards_html)
            if cards_html
            else '<p class="empty">No matching stories found for your current filter.</p>'
        )

        safe_theme = theme_mode if theme_mode in {"auto", "light", "dark", "oled"} else "auto"
        initial_data_theme = "" if safe_theme == "auto" else f'data-theme="{safe_theme}"'

        return f"""<!DOCTYPE html>
<html lang="en" {initial_data_theme}>
<head>
<meta charset="UTF-8">
<style>
  :root {{
    color-scheme: light dark;
    /* Open WebUI Light Surface Palette */
    --bg: #ffffff;
    --card-bg: #f9fafb;
    --card-border: #e5e7eb;
    --fg: #111827;
    --muted: #4b5563;
    --pill-bg: #ffffff;
    --pill-hover: #f3f4f6;
    --accent: #2563eb;
    --accent-soft: rgba(37, 99, 235, 0.1);
    --amber: #d97706;
    --amber-soft: rgba(217, 119, 6, 0.14);
    --emerald: #059669;
    --emerald-soft: rgba(5, 150, 105, 0.12);
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg: #171717;
      --card-bg: #212121;
      --card-border: #303030;
      --fg: #ececec;
      --muted: #a1a1aa;
      --pill-bg: #2a2a2a;
      --pill-hover: #353535;
      --accent: #60a5fa;
      --accent-soft: rgba(96, 165, 250, 0.15);
      --amber: #fbbf24;
      --amber-soft: rgba(251, 191, 36, 0.14);
      --emerald: #34d399;
      --emerald-soft: rgba(52, 211, 153, 0.14);
    }}
  }}
  :root[data-theme="dark"] {{
    --bg: #171717;
    --card-bg: #212121;
    --card-border: #303030;
    --fg: #ececec;
    --muted: #a1a1aa;
    --pill-bg: #2a2a2a;
    --pill-hover: #353535;
    --accent: #60a5fa;
    --accent-soft: rgba(96, 165, 250, 0.15);
    --amber: #fbbf24;
    --amber-soft: rgba(251, 191, 36, 0.14);
    --emerald: #34d399;
    --emerald-soft: rgba(52, 211, 153, 0.14);
  }}
  :root[data-theme="oled"] {{
    --bg: #000000;
    --card-bg: #121212;
    --card-border: #262626;
    --fg: #f4f4f5;
    --muted: #a1a1aa;
    --pill-bg: #1c1c1c;
    --pill-hover: #27272a;
    --accent: #60a5fa;
    --accent-soft: rgba(96, 165, 250, 0.16);
    --amber: #fbbf24;
    --amber-soft: rgba(251, 191, 36, 0.16);
    --emerald: #34d399;
    --emerald-soft: rgba(52, 211, 153, 0.14);
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--fg);
    padding: 14px 16px 18px;
    line-height: 1.5;
    border-radius: 14px;
  }}

  /* Top Bar */
  .topbar {{
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    padding-bottom: 12px;
    margin-bottom: 14px;
    border-bottom: 1px solid var(--card-border);
  }}
  .brand {{
    display: flex;
    align-items: center;
    gap: 10px;
  }}
  .brand h2 {{
    font-size: 15px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }}
  .meta {{
    font-size: 12px;
    color: var(--muted);
  }}
  .controls {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }}
  .filter-btn, .icon-btn {{
    font-size: 12px;
    font-weight: 500;
    padding: 4px 10px;
    border-radius: 999px;
    border: 1px solid var(--card-border);
    background: var(--pill-bg);
    color: var(--muted);
    cursor: pointer;
    transition: background 0.15s, color 0.15s, border-color 0.15s;
  }}
  .filter-btn:hover, .icon-btn:hover {{
    background: var(--pill-hover);
    color: var(--fg);
  }}
  .filter-btn.active {{
    background: var(--fg);
    color: var(--bg);
    border-color: var(--fg);
  }}

  /* Responsive Bento Grid matching Open WebUI chat width */
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 12px;
  }}
  .story-card {{
    display: flex;
    flex-direction: column;
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: 14px;
    padding: 14px 16px;
  }}
  .story-card.lead-card {{
    grid-column: 1 / -1;
    padding: 16px 18px;
  }}
  .story-card.hidden {{
    display: none;
  }}
  .card-meta {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
  }}
  .source-count {{
    font-size: 11.5px;
    font-weight: 500;
    color: var(--muted);
  }}
  .badge {{
    font-size: 11px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 999px;
  }}
  .badge.blindspot {{
    background: var(--amber-soft);
    color: var(--amber);
  }}
  .badge.balanced {{
    background: var(--emerald-soft);
    color: var(--emerald);
  }}
  .story-card h3 {{
    font-size: 15px;
    font-weight: 600;
    line-height: 1.32;
    margin-bottom: 6px;
    letter-spacing: -0.01em;
  }}
  .lead-card h3 {{
    font-size: 17px;
  }}
  .brief {{
    font-size: 13.5px;
    color: var(--muted);
    margin-bottom: 12px;
    flex-grow: 1;
  }}

  /* Proportional 3-Wing Spectrum Bar */
  .spectrum-bar {{
    display: flex;
    height: 4px;
    width: 100%;
    border-radius: 999px;
    overflow: hidden;
    background: var(--card-border);
    margin-bottom: 6px;
  }}
  .seg {{ height: 100%; }}
  .seg-L {{ background: #3b82f6; }}
  .seg-C {{ background: #71717a; }}
  .seg-R {{ background: #f59e0b; }}

  .spectrum-labels {{
    display: flex;
    gap: 12px;
    font-size: 11.5px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    margin-bottom: 12px;
  }}
  .spectrum-labels span {{
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }}
  .dot {{
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    flex-shrink: 0;
  }}
  .dot-L {{ background: #3b82f6; }}
  .dot-C {{ background: #71717a; }}
  .dot-R {{ background: #f59e0b; }}

  /* Publisher Click-Out Pills + Ask AI */
  .card-actions {{
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    padding-top: 10px;
    border-top: 1px solid var(--card-border);
  }}
  .links {{
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
  }}
  .src-pill, .ask-pill {{
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 12px;
    font-weight: 500;
    padding: 4px 9px;
    border-radius: 999px;
    border: 1px solid var(--card-border);
    background: var(--pill-bg);
    color: var(--fg);
    text-decoration: none;
    cursor: pointer;
  }}
  .src-pill:hover, .ask-pill:hover {{
    background: var(--pill-hover);
    border-color: var(--accent);
  }}
  .src-pill .ext {{
    font-size: 11px;
    color: var(--muted);
  }}
  .ask-pill {{
    color: var(--accent);
    background: var(--accent-soft);
    border-color: transparent;
  }}
  .empty {{
    grid-column: 1 / -1;
    padding: 24px 0;
    color: var(--muted);
    font-size: 14px;
  }}
</style>
</head>
<body>
  <div class="topbar">
    <div class="brand">
      <h2>Unbiased News Desk</h2>
      <span class="meta">Updated {cached_age_mins}m ago &middot; {len(stories)} stories</span>
    </div>
    <div class="controls">
      <button class="filter-btn active" type="button" onclick="filterFeed('all', this)">All</button>
      <button class="filter-btn" type="button" onclick="filterFeed('balanced', this)">Multi-wing</button>
      <button class="filter-btn" type="button" onclick="filterFeed('blindspot', this)">Blindspots</button>
      <button class="icon-btn" type="button" onclick="askAI('refresh')" title="Fetch fresh stories">&#8635; Refresh</button>
      <button class="icon-btn" type="button" onclick="cycleTheme()" title="Switch theme (Light / Dark / OLED)">&#9680; Theme</button>
    </div>
  </div>

  <div class="grid" id="feed-grid">
    {grid_content}
  </div>

  <script>
    // Auto-detect Open WebUI parent theme when allowSameOrigin is enabled
    (function syncParentTheme() {{
      if (document.documentElement.hasAttribute('data-theme')) return;
      try {{
        const cls = parent.document.documentElement.classList;
        if (cls.contains('oled')) document.documentElement.setAttribute('data-theme', 'oled');
        else if (cls.contains('dark')) document.documentElement.setAttribute('data-theme', 'dark');
        else if (cls.contains('light')) document.documentElement.setAttribute('data-theme', 'light');
      }} catch (_) {{}}
    }})();

    function cycleTheme() {{
      const order = ['light', 'dark', 'oled'];
      const cur = document.documentElement.getAttribute('data-theme') || 'dark';
      const next = order[(order.indexOf(cur) + 1) % order.length];
      document.documentElement.setAttribute('data-theme', next);
    }}

    function filterFeed(mode, btn) {{
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      if (btn) btn.classList.add('active');
      document.querySelectorAll('.story-card').forEach(card => {{
        const show = mode === 'all'
          || (mode === 'balanced' && card.dataset.balanced === 'true')
          || (mode === 'blindspot' && card.dataset.blindspot === 'true');
        card.classList.toggle('hidden', !show);
      }});
      reportHeight();
    }}

    function askAI(prompt) {{
      parent.postMessage({{ type: 'input:prompt:submit', text: prompt }}, '*');
    }}

    function reportHeight() {{
      const h = document.documentElement.scrollHeight;
      parent.postMessage({{ type: 'iframe:height', height: h }}, '*');
    }}
    window.addEventListener('load', reportHeight);
    new ResizeObserver(reportHeight).observe(document.body);
  </script>
</body>
</html>"""

    async def pipe(
        self,
        body: dict[str, Any],
        __user__: Optional[dict[str, Any]] = None,
        __event_emitter__: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
    ) -> str:
        messages = body.get("messages", [])
        last_msg = (messages[-1].get("content", "") if messages else "").strip()
        lower_msg = last_msg.lower()

        user_valves = (__user__ or {}).get("valves") or self.UserValves()
        if isinstance(user_valves, dict):
            user_valves = self.UserValves(**user_valves)

        async def emit_status(desc: str, done: bool = False) -> None:
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": desc, "done": done}})

        force_refresh = "refresh" in lower_msg
        cached = None if force_refresh else self._read_cache()
        ttl_secs = max(1, self.valves.refresh_interval_mins) * 60

        if cached and (time.time() - cached[0]) < ttl_secs:
            updated_at, stories = cached
            age_mins = max(0, int((time.time() - updated_at) // 60))
        else:
            await emit_status("Fetching multi-spectrum feeds and deduplicating wire copy...")
            stories = await self._build_feed_data()
            age_mins = 0

        filtered = stories
        topics_raw = user_valves.topics.strip()
        if topics_raw:
            kws = [k.strip().lower() for k in topics_raw.split(",") if k.strip()]
            filtered = [
                s for s in filtered
                if any(k in s["headline"].lower() or k in s["brief"].lower() for k in kws)
            ]
        elif lower_msg and lower_msg not in {"news", "feed", "refresh", "today", "headlines"}:
            if len(lower_msg.split()) <= 3:
                filtered = [
                    s for s in filtered
                    if lower_msg in s["headline"].lower() or lower_msg in s["brief"].lower()
                ]

        if user_valves.blindspot_only or "blindspot" in lower_msg:
            filtered = [s for s in filtered if s.get("blindspot")]

        filtered = filtered[: max(1, user_valves.max_stories)]
        html_ui = self.render_rich_ui(filtered, age_mins, theme_mode=user_valves.theme)

        if __event_emitter__:
            await __event_emitter__({"type": "embeds", "data": {"embeds": [html_ui]}})

        await emit_status("Feed ready", done=True)
        return f"Loaded {len(filtered)} stories. Click any publisher pill above to read the full article, or ask a follow-up question."