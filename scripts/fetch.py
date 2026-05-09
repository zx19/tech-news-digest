#!/usr/bin/env python3
"""Fetch tech news from multiple sources, cross-validate, AI score, output JSON."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta
from urllib.parse import urlparse

import feedparser
import requests


# ========== Sources ==========
RSS_SOURCES = {
    'lobsters': 'https://lobste.rs/rss',
    'infoq_cn': 'https://www.infoq.cn/feed',
    'devto': 'https://dev.to/feed',
    'ruanyf': 'https://www.ruanyifeng.com/blog/atom.xml',
}

MAX_PER_SOURCE = 5
TOP_N = 15


def normalize_url(url: str) -> str:
    u = urlparse(url)
    return f'{u.scheme}://{u.netloc}{u.path}'.rstrip('/').lower()


def normalize_score(source: str, raw_score: int) -> float:
    """Normalize raw scores from different sources to comparable 0-100 scale."""
    if source == 'github':
        return min(raw_score / 150, 60)  # 9000 stars → 60
    if source == 'hackernews':
        return min(raw_score * 0.4, 50)  # 125 points → 50
    if source == 'lobsters':
        return 40
    if source == 'infoq_cn':
        return 35
    if source == 'devto':
        return 30
    if source == 'ruanyf':
        return 45  # 周刊质量高，保底高
    return 10


def fetch_page_description(url: str) -> str:
    """Try to extract meta description from target page."""
    try:
        resp = requests.get(url, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
        resp.raise_for_status()
        text = resp.text
        candidates = []
        # Try og:description first (usually higher quality)
        m = re.search(
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
            text, re.IGNORECASE,
        )
        if m:
            candidates.append(m.group(1))
        # Try meta description
        m = re.search(
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
            text, re.IGNORECASE,
        )
        if m:
            candidates.append(m.group(1))
        # Try twitter:description
        m = re.search(
            r'<meta[^>]+name=["\']twitter:description["\'][^>]+content=["\']([^"\']+)',
            text, re.IGNORECASE,
        )
        if m:
            candidates.append(m.group(1))

        for raw in candidates:
            desc = re.sub(r'\s+', ' ', raw).strip()
            # Filter out garbage descriptions
            if len(desc) < 20:
                continue
            if desc.lower() in {'comments', 'login', 'sign up', 'home', 'menu'}:
                continue
            if re.match(r'^\d+\s+(comments|shares|likes)$', desc, re.IGNORECASE):
                continue
            return desc[:250]
    except Exception:
        pass
    return ''


def fetch_github_trending() -> list[dict]:
    """Fetch trending repos from GitHub Search API (last 7 days)."""
    try:
        since = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        url = (
            'https://api.github.com/search/repositories'
            f'?q=created:>{since}&sort=stars&order=desc&per_page=10'
        )
        resp = requests.get(url, timeout=30, headers={'Accept': 'application/vnd.github.v3+json'})
        resp.raise_for_status()
        items = []
        for item in resp.json().get('items', []):
            stars = item.get('stargazers_count', 0)
            desc = item.get('description') or ''
            items.append({
                'source': 'github',
                'title': f"[GitHub] {item['full_name']}",
                'url': item['html_url'],
                'desc': desc,
                'score': normalize_score('github', stars),
            })
        return items
    except Exception as e:
        print(f'[warn] github trending failed: {e}')
        return []


def fetch_hackernews() -> list[dict]:
    """Fetch HN top stories via official API (score >= 50)."""
    try:
        top_ids = requests.get(
            'https://hacker-news.firebaseio.com/v0/topstories.json',
            timeout=30,
        ).json()[:20]
        items = []
        for hid in top_ids:
            item = requests.get(
                f'https://hacker-news.firebaseio.com/v0/item/{hid}.json',
                timeout=30,
            ).json()
            if not item:
                continue
            score = item.get('score', 0)
            if score < 50:
                continue
            url = item.get('url') or f'https://news.ycombinator.com/item?id={hid}'
            title = item.get('title', '')
            # HN API has no description; try to fetch from target page
            desc = fetch_page_description(url) if url.startswith('http') else ''
            items.append({
                'source': 'hackernews',
                'title': title,
                'url': url,
                'desc': desc,
                'score': normalize_score('hackernews', score),
            })
        return items
    except Exception as e:
        print(f'[warn] hackernews failed: {e}')
        return []


def _clean_rss_summary(text: str) -> str:
    """Remove HTML tags and filter out garbage descriptions."""
    desc = re.sub(r'<[^>]+>', '', text).strip()
    # Filter common garbage from RSS summaries
    if desc.lower() in {'comments', 'login', 'sign up', 'home', 'menu', 'read more'}:
        return ''
    if re.match(r'^\d+\s+(comments?|shares?|likes?)$', desc, re.IGNORECASE):
        return ''
    if len(desc) < 10:
        return ''
    return desc[:300]


def fetch_rss(name: str, url: str) -> list[dict]:
    try:
        fp = feedparser.parse(url)
        items = []
        for entry in getattr(fp, 'entries', [])[:15]:
            summary = _clean_rss_summary(entry.get('summary', ''))
            items.append({
                'source': name,
                'title': entry.get('title', ''),
                'url': entry.link,
                'desc': summary,
                'score': normalize_score(name, 0),
            })
        return items
    except Exception as e:
        print(f'[warn] {name} failed: {e}')
        return []


def fetch_all() -> list[dict]:
    raw: list[dict] = []
    raw.extend(fetch_github_trending())
    raw.extend(fetch_hackernews())
    for name, url in RSS_SOURCES.items():
        raw.extend(fetch_rss(name, url))
    counts = Counter(x['source'] for x in raw)
    print(f'[fetch] counts: {dict(counts)}')
    return raw


def cross_validate(items: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for item in items:
        key = normalize_url(item['url'])
        groups.setdefault(key, []).append(item)

    validated = []
    for url, group in groups.items():
        sources = list({x['source'] for x in group})
        merged = {
            'url': url,
            'title': group[0]['title'],
            'source': sources[0],  # primary source for per-source cap
            'sources': sources,
            'desc': max((x['desc'] for x in group), key=len, default=''),
            'score': max(x['score'] for x in group),  # use max instead of sum
        }
        if len(merged['sources']) >= 2:
            merged['score'] += 20  # cross-validation bonus
        validated.append(merged)
    return validated


def apply_source_cap(items: list[dict], cap: int) -> list[dict]:
    """Keep at most `cap` items per source, preserving overall rank order."""
    source_counts: dict[str, int] = {}
    result = []
    for item in items:
        src = item.get('source', 'unknown')
        if src not in source_counts:
            source_counts[src] = 0
        if source_counts[src] < cap:
            result.append(item)
            source_counts[src] += 1
    return result


def ai_score(items: list[dict]) -> list[dict]:
    api_key = os.getenv('GEMINI_API_KEY', '')
    if not api_key:
        for item in items:
            item['final_score'] = float(item.get('score', 0))
        return sorted(items, key=lambda x: x['final_score'], reverse=True)

    batch = items[:30]
    prompt = (
        'You are a senior tech editor. Rate each news item (0-100) based on:\n'
        '- Technical depth (architecture/algorithms/engineering)\n'
        '- Practical value (can developers apply it?)\n'
        '- Timeliness (important recent release/news)\n\n'
        'Return ONLY a JSON array: [{"index":0,"score":85,"reason":"..."}, ...]\n\n'
        'News list:\n'
        + '\n'.join(f"{i}. {x['title']}" for i, x in enumerate(batch))
    )

    # Retry with exponential backoff for rate limits
    import time
    scores = None
    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(2 ** attempt)
            resp = requests.post(
                'https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent',
                headers={'Content-Type': 'application/json'},
                params={'key': api_key},
                json={'contents': [{'parts': [{'text': prompt}]}]},
                timeout=60,
            )
            resp.raise_for_status()
            text = resp.json()['candidates'][0]['content']['parts'][0]['text']

            match = re.search(r'\[.*\]', text, re.DOTALL)
            if not match:
                raise ValueError('No JSON array found in response')
            scores = json.loads(match.group())
            break
        except Exception as e:
            print(f'[warn] AI scoring attempt {attempt + 1} failed: {e}')
            scores = None

    if scores:
        for s in scores:
            idx = s.get('index', 0)
            if 0 <= idx < len(batch):
                batch[idx]['ai_score'] = s.get('score', 50)
                batch[idx]['ai_reason'] = s.get('reason', '')
    else:
        print('[warn] All AI scoring attempts failed; using heuristic fallback')
        for item in batch:
            item['ai_score'] = 50

    for item in batch:
        base = float(item.get('score', 0))
        ai = float(item.get('ai_score', 50))
        item['final_score'] = base * 0.3 + ai * 0.7

    return sorted(batch, key=lambda x: x.get('final_score', 0), reverse=True)


def classify(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ['python', 'node.js', 'go ', 'rust', 'java ', 'typescript']):
        return '语言/框架'
    if any(k in t for k in ['ai', 'llm', 'gpt', 'claude', 'model', 'gemini', 'deepseek']):
        return 'AI/ML'
    if any(k in t for k in ['security', 'cve', 'vulnerability', 'exploit']):
        return '安全'
    if any(k in t for k in ['database', 'postgres', 'redis', 'mysql', 'sqlite']):
        return '数据库'
    if any(k in t for k in ['kubernetes', 'docker', 'k8s', 'cloud', 'aws', 'azure']):
        return '云原生'
    return '综合'


def main() -> None:
    os.makedirs('data', exist_ok=True)

    raw = fetch_all()
    validated = cross_validate(raw)
    scored = ai_score(validated)
    # Apply per-source cap to enforce diversity
    capped = apply_source_cap(scored, MAX_PER_SOURCE)
    top = capped[:TOP_N]

    digest = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'count': len(top),
        'items': [
            {
                'rank': i + 1,
                'title': x['title'],
                'url': x['url'],
                'desc': x['desc'][:200] if len(x['desc']) > 200 else x['desc'],
                'tag': classify(x['title']),
                'sources': x['sources'],
                'score': round(float(x.get('final_score', 0)), 1),
            }
            for i, x in enumerate(top)
        ],
    }

    date_file = f"data/{digest['date']}.json"
    with open(date_file, 'w', encoding='utf-8') as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)

    with open('data/latest.json', 'w', encoding='utf-8') as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)

    print(f'Digest saved: {date_file} ({len(top)} items)')


if __name__ == '__main__':
    main()
