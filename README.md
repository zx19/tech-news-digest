# Tech News Digest

Daily curated tech news aggregated from high-quality sources, scored by AI, and delivered to Feishu via OpenClaw.

## Sources

- [Hacker News](https://news.ycombinator.com) (frontpage, score ≥ 50)
- [Lobsters](https://lobste.rs)
- [GitHub Trending](https://github.com/trending) (last 7 days, by stars)
- [InfoQ CN](https://www.infoq.cn)

## Quality Pipeline

1. **Multi-source fetch** → RSS + API
2. **Cross-validation** → same URL appearing in ≥2 sources gets bonus
3. **AI scoring** → Gemini Flash rates technical depth, practicality, timeliness
4. **Deduplication** → URL normalization
5. **Top 8** → highest final score items

## Files

- `scripts/fetch.py` — aggregation + scoring logic
- `.github/workflows/daily.yml` — GitHub Actions cron (08:00 UTC daily)
- `data/latest.json` — latest digest (read by OpenClaw)
- `data/YYYY-MM-DD.json` — historical archives

## OpenClaw Integration

OpenClaw reads `data/latest.json` via GitHub skill and pushes formatted cards to Feishu group daily at 09:30 CST.
