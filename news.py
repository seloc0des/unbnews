"""
Open WebUI Native Router: /api/v1/news
Serves up-to-date, wire-deduplicated, multi-spectrum news clusters with 2-sentence neutral summaries.
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
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Query
from open_webui.utils.auth import get_verified_user

router = APIRouter()

DEFAULT_SOURCES = [
    {"name": "NPR", "url": "https://feeds.npr.org/1001/rss.xml", "bias": "C"},
    {"name": "BBC World", "url": "https://feeds.bbci.co.uk/news/world/rss.xml", "bias": "C"},
    {"name": "The Guardian", "url": "https://www.theguardian.com/world/rss", "bias": "L"},
    {"name": "Fox News", "url": "https://moxie.foxnews.com/google-publisher/world.xml", "bias": "R"},
    {"name": "CS Monitor", "url": "https://rss.csmonitor.com/feeds/all", "bias": "C"},
    {"name": "The Hill", "url": "https://thehill.com/feed/", "bias": "C"},
]

CACHE_DB_PATH = os.environ.get("NEWS_CACHE_DB", "/app/backend/data/news_feed_cache.db")
REFRESH_TTL_SECS = int(os.environ.get("NEWS_REFRESH_TTL_SECS", "1800"))
LLM_BASE_URL = os.environ.get("NEWS_LLM_BASE_URL", os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434") + "/v1")
LLM_API_KEY = os.environ.get("NEWS_LLM_API_KEY", "ollama")
SUMMARIZER_MODEL = os.environ.get("NEWS_SUMMARIZER_MODEL", "llama3.2:3b")
MAX_SUMMARY_WORDS = int(os.environ.get("NEWS_MAX_SUMMARY_WORDS", "45"))
SEARXNG_URL = os.environ.get("NEWS_SEARXNG_URL", "")

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
    bias: str
    published: str
    shingles: set[str]
    tokens: set[str]


def _clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return re.sub(r"\s+", " ", text).strip()


def _tokenize(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in STOPWORDS}


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
    unique: list[Article] = []
    for art in articles:
        if not any(_jaccard(art.shingles, kept.shingles) >= threshold for kept in unique):
            unique.append(art)
    return unique


def cluster_articles(articles: list[Article], sim_threshold: float = 0.25) -> list[list[Article]]:
    clusters: list[list[Article]] = []
    cluster_tokens: list[set[str]] = []
    for art in articles:
        best_idx, best_sim = -1, 0.0
        for idx, c_toks in enumerate(cluster_tokens):
            sim = _overlap(art.tokens, c_toks)
            if sim > best_sim:
                best_sim, best_idx = sim, idx
        if best_idx >= 0 and best_sim >= sim_threshold:
            clusters[best_idx].append(art)
            cluster_tokens[best_idx] |= art.tokens
        else:
            clusters.append([art])
            cluster_tokens.append(set(art.tokens))
    clusters.sort(key=lambda c: len({a.source for a in c}), reverse=True)
    return clusters


def compute_spectrum(cluster: list[Article]) -> dict[str, Any]:
    counts = {"L": 0, "C": 0, "R": 0}
    seen: set[str] = set()
    for art in cluster:
        if art.source in seen:
            continue
        seen.add(art.source)
        counts[art.bias if art.bias in counts else "C"] += 1
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

        articles.append(
            Article(
                id=hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
                title=title,
                summary=summary[:500],
                url=url,
                source=source_name,
                bias=bias,
                published=pub,
                shingles=_shingles(f"{title}. {summary}"),
                tokens=_tokenize(f"{title} {summary[:240]}"),
            )
        )
    return articles


def _init_db() -> sqlite3.Connection:
    db_dir = os.path.dirname(CACHE_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS feed_cache (id INTEGER PRIMARY KEY CHECK (id = 1), updated_at REAL NOT NULL, stories_json TEXT NOT NULL)"
    )
    conn.commit()
    return conn


def _read_cache() -> Optional[tuple[float, list[dict[str, Any]]]]:
    conn = _init_db()
    try:
        row = conn.execute("SELECT updated_at, stories_json FROM feed_cache WHERE id = 1").fetchone()
        return (float(row[0]), json.loads(row[1])) if row else None
    finally:
        conn.close()


def _write_cache(stories: list[dict[str, Any]]) -> None:
    conn = _init_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO feed_cache (id, updated_at, stories_json) VALUES (1, ?, ?)",
            (time.time(), json.dumps(stories)),
        )
        conn.commit()
    finally:
        conn.close()


async def _fetch_all_sources() -> list[Article]:
    raw_env = os.environ.get("NEWS_SOURCES_JSON", "")
    try:
        sources = json.loads(raw_env) if raw_env else DEFAULT_SOURCES
    except json.JSONDecodeError:
        sources = DEFAULT_SOURCES

    articles: list[Article] = []
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        tasks = [
            client.get(src["url"], headers={"User-Agent": "OpenWebUI-NewsTab/0.2"})
            for src in sources if src.get("url")
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        for src, resp in zip(sources, responses):
            if isinstance(resp, Exception) or resp.status_code != 200:
                continue
            articles.extend(parse_rss_xml(resp.text, src.get("name", "Source"), src.get("bias", "C")))

        if SEARXNG_URL.strip():
            try:
                sx_resp = await client.get(
                    SEARXNG_URL.rstrip("/") + "/search",
                    params={"q": "world top news today", "categories": "news", "format": "json"},
                )
                if sx_resp.status_code == 200:
                    for res in sx_resp.json().get("results", [])[:10]:
                        title = _clean_text(res.get("title", ""))
                        snippet = _clean_text(res.get("content", ""))
                        url = res.get("url", "")
                        if title and url:
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


async def _synthesize_story(client: httpx.AsyncClient, cluster: list[Article]) -> dict[str, str]:
    lead = cluster[0]
    fallback = lead.summary.split(". ")[0].strip()
    if fallback and not fallback.endswith("."):
        fallback += "."
    words = fallback.split()
    if len(words) > MAX_SUMMARY_WORDS:
        fallback = " ".join(words[:MAX_SUMMARY_WORDS]) + "..."

    notes = "\n".join(f"- [{a.source} ({a.bias})]: {a.title}. {a.summary[:220]}" for a in cluster[:5])
    prompt = (
        "Summarize this news cluster neutrally for a quick-scan feed.\n"
        "1. Output valid JSON with keys: headline, brief.\n"
        "2. headline: max 12 words, neutral, factual, no clickbait.\n"
        f"3. brief: EXACTLY 2 short sentences (max {MAX_SUMMARY_WORDS} words total). "
        "Sentence 1 states what happened. Sentence 2 states key context or competing views.\n\n"
        f"Sources:\n{notes}"
    )
    try:
        resp = await client.post(
            f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={
                "model": SUMMARIZER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=15.0,
        )
        if resp.status_code == 200:
            parsed = json.loads(resp.json()["choices"][0]["message"]["content"])
            return {
                "headline": _clean_text(parsed.get("headline", "")) or lead.title,
                "brief": _clean_text(parsed.get("brief", "")) or fallback,
            }
    except Exception:
        pass
    return {"headline": lead.title, "brief": fallback or "Open a source below to read the full article."}


async def build_feed_data() -> list[dict[str, Any]]:
    raw = await _fetch_all_sources()
    deduped = deduplicate_wire_copy(raw)
    clusters = cluster_articles(deduped)[:15]
    async with httpx.AsyncClient(timeout=15.0) as client:
        synths = await asyncio.gather(*[_synthesize_story(client, c) for c in clusters])

    stories: list[dict[str, Any]] = []
    for cluster, synth in zip(clusters, synths):
        spec = compute_spectrum(cluster)
        seen_urls: set[str] = set()
        links = []
        for a in cluster:
            if a.url in seen_urls:
                continue
            seen_urls.add(a.url)
            links.append({"source": a.source, "url": a.url, "bias": a.bias})
            if len(links) >= 4:
                break
        stories.append({
            "headline": synth["headline"],
            "brief": synth["brief"],
            "spectrum": spec["counts"],
            "total_sources": spec["total"],
            "blindspot": spec["blindspot"],
            "links": links,
        })
    _write_cache(stories)
    return stories


@router.get("/feed")
async def get_news_feed(
    refresh: bool = Query(default=False),
    user=Depends(get_verified_user),
):
    cached = None if refresh else _read_cache()
    if cached and (time.time() - cached[0]) < REFRESH_TTL_SECS:
        updated_at, stories = cached
    else:
        stories = await build_feed_data()
        updated_at = time.time()

    age_mins = max(0, int((time.time() - updated_at) // 60))
    return {"updated_at": updated_at, "age_mins": age_mins, "stories": stories}
