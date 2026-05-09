#!/usr/bin/env python3
"""Fetch tech news from multiple sources, cross-validate, AI score, output JSON."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

import feedparser
import requests


# ========== Sources ==========
SOURCES = {
    'hackernews': 'https://hnrss.org/frontpage?points=50',
    'lobsters': 'https://lobste.rs/rss',
    'infoq_cn': 'https://www.infoq.cn/feed',
}


def normalize_url(url: str) -> str:
    u = urlparse(url)
    return f'{u.scheme}://{u.netloc}{u.path}'.rstrip('/').lower()


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
            items.append({
                'source': 'github',
                'title': f"[GitHub] {item['full_name']}",
                'url': item['html_url'],
                'desc': item.get('description') or '',
                'score': item.get('stargazers_count', 0),
            })
        return items
    except Exception as e:
        print(f'[warn] github trending failed: {e}')
        return []


def fetch_rss(name: str, url: str) -> list[dict]:
    try:
        fp = feedparser.parse(url)
        items = []
        for entry in getattr(fp, 'entries', [])[:15]:
            score = 0
            if name == 'hackernews':
                # hnrss includes <hn:score> in some feeds; fallback to comments count
                score = int(getattr(entry, 'hn_score', 0) or getattr(entry, 'comments', 0))
            items.append({
                'source': name,
                'title': entry.get('title', ''),
                'url': entry.link,
                'desc': re.sub(r'<[^>]+>', '', entry.get('summary', ''))[:300],
                'score': score,
            })
        return items
    except Exception as e:
        print(f'[warn] {name} failed: {e}')
        return []


def fetch_all() -> list[dict]:
    raw: list[dict] = []
    raw.extend(fetch_github_trending())
    for name, url in SOURCES.items():
        raw.extend(fetch_rss(name, url))
    return raw


def cross_validate(items: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for item in items:
        key = normalize_url(item['url'])
        groups.setdefault(key, []).append(item)

    validated = []
    for url, group in groups.items():
        merged = {
            'url': url,
            'title': group[0]['title'],
            'sources': list({x['source'] for x in group}),
            'desc': max((x['desc'] for x in group), key=len, default=''),
            'score': sum(x.get('score', 0) for x in group),
        }
        if len(merged['sources']) >= 2:
            merged['score'] += 50
        validated.append(merged)
    return validated


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

    try:
        resp = requests.post(
            'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent',
            headers={'Content-Type': 'application/json'},
            params={'key': api_key},
            json={'contents': [{'parts': [{'text': prompt}]}]},
            timeout=60,
        )
        resp.raise_for_status()
        text = resp.json()['candidates'][0]['content']['parts'][0]['text']

        # Extract JSON array from possible markdown code block
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            raise ValueError('No JSON array found in response')
        scores = json.loads(match.group())

        for s in scores:
            idx = s.get('index', 0)
            if 0 <= idx < len(batch):
                batch[idx]['ai_score'] = s.get('score', 50)
                batch[idx]['ai_reason'] = s.get('reason', '')
    except Exception as e:
        print(f'[warn] AI scoring failed: {e}')
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
    top = scored[:8]

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
