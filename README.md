# TinySearch — your own search engine

TinySearch is a learning project that crawls public web pages, stores their text in a local SQLite database, and searches that index using SQLite FTS5's full-text search and BM25 ranking.

**It does not call Google, Bing, Brave, or any other search API.** Your own crawler discovers pages from seed URLs; your own local index stores them; your own search endpoint ranks matches.

## Requirements

- Python 3.10+
- Internet connection to crawl public websites
- SQLite with FTS5 support (normally included with standard Python distributions)

## Run locally

```bash
cd own_search_engine
python -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt
uvicorn app:app --reload
```

Open http://127.0.0.1:8000

## Use it

1. Open **Grow your index**.
2. Add one or more public starting URLs, such as `https://example.com`.
3. Choose a small page limit (try 10–25).
4. Click **Start crawling** and wait for the crawl summary.
5. Search for words that occur on the pages you indexed.

The database is created automatically as `search_index.db`. It remains on your machine between runs. To reset the index, stop the server and delete that file.

## API endpoints

- `GET /api/stats` — number of indexed pages
- `POST /api/crawl` — crawl from seed URLs and index pages
- `GET /api/search?q=...&limit=10` — search the local index
- `GET /api/pages` — recently indexed pages
- `GET /docs` — interactive FastAPI API documentation

Example crawl request:

```json
{
  "seeds": ["https://example.com"],
  "max_pages": 25,
  "same_domain_only": true
}
```

Example search:

```text
http://127.0.0.1:8000/api/search?q=example
```

## How it works

1. **Crawling:** starts from URLs you provide and discovers links in HTML.
2. **Politeness:** checks `robots.txt`, identifies itself with a user agent, and waits between requests.
3. **Parsing:** extracts titles, meta descriptions, and readable page text.
4. **Indexing:** stores documents in SQLite and keeps a full-text index updated with database triggers.
5. **Ranking:** SQLite FTS5's BM25 function ranks documents matching all query terms.
6. **Serving:** FastAPI exposes search and crawl endpoints; the HTML frontend calls them.

## Important limitations

- This is a prototype, not a Google-scale search engine. It only knows pages it has crawled.
- It indexes HTML text, not PDFs, images, videos, JavaScript-rendered content, or the whole internet.
- Search quality is basic: BM25 plus title/description weighting, without link authority, language analysis, spam detection, or machine-learned ranking.
- Crawling may be blocked by a website, and pages may change or disappear.
- Use modest crawl limits, respect site terms and robots.txt, and do not crawl sites you are not allowed to access.
- The crawler has basic public-IP checks, but this project is for local learning, not deployment as a public crawling service. Do not expose its crawl endpoint to the public internet without a security review, quotas, stronger URL validation, and abuse protection.

## Good next steps

1. Persist crawl frontier and revisit pages on a schedule.
2. Add canonical URL handling, duplicate-content detection, and per-host rate limits.
3. Add PageRank / link-based ranking.
4. Add stemming, typo correction, autocomplete, and query highlighting.
5. Move from SQLite to a dedicated index (e.g. your own inverted index or a search library) when the collection grows.
6. Add a sitemap parser and domain-specific crawl policies.
