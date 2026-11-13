from __future__ import annotations

import ipaddress
import re
import socket
import sqlite3
import time
from collections import deque
from contextlib import contextmanager
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_DIR = __import__("pathlib").Path(__file__).resolve().parent
DB_PATH = APP_DIR / "search_index.db"
USER_AGENT = "TinySearchBot/0.1 (+local learning project)"
REQUEST_TIMEOUT = 10
MAX_HTML_BYTES = 2_000_000

app = FastAPI(title="TinySearch", description="A small, self-built web crawler and search index.")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                crawled_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
                url UNINDEXED, title, description, body,
                content='pages', content_rowid='id',
                tokenize='unicode61 remove_diacritics 2'
            )
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
              INSERT INTO pages_fts(rowid, url, title, description, body)
              VALUES (new.id, new.url, new.title, new.description, new.body);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
              INSERT INTO pages_fts(pages_fts, rowid, url, title, description, body)
              VALUES ('delete', old.id, old.url, old.title, old.description, old.body);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
              INSERT INTO pages_fts(pages_fts, rowid, url, title, description, body)
              VALUES ('delete', old.id, old.url, old.title, old.description, old.body);
              INSERT INTO pages_fts(rowid, url, title, description, body)
              VALUES (new.id, new.url, new.title, new.description, new.body);
            END
        """)

init_db()

def normalize_url(url: str) -> str | None:
    url, _ = urldefrag(url.strip())
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    if p.username or p.password:
        return None
    # Drop common tracking fragments/parameters are intentionally not rewritten.
    return url

def public_host(hostname: str) -> bool:
    """Best-effort block for localhost and private/reserved IP destinations."""
    try:
        addresses = {ipaddress.ip_address(hostname)}
    except ValueError:
        try:
            infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            addresses = {ipaddress.ip_address(info[4][0]) for info in infos}
        except (socket.gaierror, ValueError):
            return False
    return bool(addresses) and all(ip.is_global for ip in addresses)

def get_robots_parser(origin: str) -> RobotFileParser:
    robots_url = origin.rstrip("/") + "/robots.txt"
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        r = requests.get(
            robots_url, headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT, allow_redirects=True
        )
        if r.status_code == 200:
            parser.parse(r.text.splitlines())
        elif r.status_code in (401, 403):
            parser.parse(["User-agent: *", "Disallow: /"])
        else:
            parser.parse(["User-agent: *", "Disallow:"])
    except requests.RequestException:
        # Be conservative when robots.txt cannot be fetched.
        parser.parse(["User-agent: *", "Disallow: /"])
    return parser

def extract_page(html: str):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    description = meta.get("content", "").strip() if meta else ""
    body = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    return title[:500], description[:1500], body[:100_000], soup

def save_page(url: str, title: str, description: str, body: str):
    with db() as conn:
        conn.execute("""
            INSERT INTO pages(url, title, description, body, crawled_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
              title=excluded.title,
              description=excluded.description,
              body=excluded.body,
              crawled_at=excluded.crawled_at
        """, (url, title, description, body, time.time()))

class CrawlRequest(BaseModel):
    seeds: list[str] = Field(default_factory=list, max_length=20)
    max_pages: int = Field(default=25, ge=1, le=200)
    same_domain_only: bool = True

class SearchRequest(BaseModel):
    q: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=10, ge=1, le=50)

@app.get("/")
def home():
    return FileResponse(APP_DIR / "static" / "index.html")

@app.get("/api/stats")
def stats():
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    return {"indexed_pages": count, "engine": "TinySearch", "index": "SQLite FTS5"}

@app.post("/api/crawl")
def crawl(request: CrawlRequest):
    seeds = []
    for raw in request.seeds:
        url = normalize_url(raw)
        if not url:
            raise HTTPException(status_code=400, detail=f"Invalid URL: {raw}")
        seeds.append(url)
    if not seeds:
        raise HTTPException(status_code=400, detail="Add at least one starting URL.")

    seed_hosts = {urlparse(u).hostname for u in seeds}
    queue = deque(seeds)
    queued = set(seeds)
    visited = set()
    robots_cache = {}
    indexed = 0
    skipped = 0
    errors = []

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})

    while queue and indexed < request.max_pages:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        parsed = urlparse(url)
        host = parsed.hostname
        if not host or not public_host(host):
            skipped += 1
            continue

        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in robots_cache:
            robots_cache[origin] = get_robots_parser(origin)
        if not robots_cache[origin].can_fetch(USER_AGENT, url):
            skipped += 1
            continue

        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True, stream=True)
            content_type = response.headers.get("content-type", "").lower()
            if response.status_code != 200 or "text/html" not in content_type:
                skipped += 1
                response.close()
                continue
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=16384):
                total += len(chunk)
                if total > MAX_HTML_BYTES:
                    break
                chunks.append(chunk)
            encoding = response.encoding or "utf-8"
            html = b"".join(chunks).decode(encoding, errors="replace")
            final_url = normalize_url(response.url) or url
            response.close()
            title, description, body, soup = extract_page(html)
            if not body:
                skipped += 1
                continue
            save_page(final_url, title or final_url, description, body)
            indexed += 1

            for a in soup.find_all("a", href=True):
                next_url = normalize_url(urljoin(final_url, a["href"]))
                if not next_url or next_url in queued or next_url in visited:
                    continue
                next_host = urlparse(next_url).hostname
                if request.same_domain_only and next_host not in seed_hosts:
                    continue
                # Stay on the same hostname by default, not just the same registrable domain.
                if request.same_domain_only and next_host != host:
                    continue
                queued.add(next_url)
                queue.append(next_url)
            time.sleep(0.35)  # polite delay between pages
        except (requests.RequestException, UnicodeError, OSError) as exc:
            errors.append({"url": url, "error": str(exc)[:180]})
            skipped += 1

    return {
        "message": "Crawl finished",
        "indexed_this_run": indexed,
        "skipped": skipped,
        "visited": len(visited),
        "queued_remaining": len(queue),
        "errors": errors[:10],
        "note": "This is a small learning crawler, not a full-web index. Respect each site's terms and robots.txt."
    }

@app.get("/api/search")
def search(q: str, limit: int = 10):
    q = q.strip()
    if not q:
        raise HTTPException(status_code=400, detail="Enter a search query.")
    limit = max(1, min(int(limit), 50))
    # Turn a user query into quoted FTS terms to avoid accidental FTS syntax errors.
    terms = re.findall(r"[\w-]+", q, flags=re.UNICODE)
    if not terms:
        return {"query": q, "results": [], "total": 0}
    match_query = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
    with db() as conn:
        try:
            rows = conn.execute("""
                SELECT p.url, p.title, p.description, p.body,
                       bm25(pages_fts, 0.0, 5.0, 2.0, 1.0) AS score
                FROM pages_fts
                JOIN pages p ON p.id = pages_fts.rowid
                WHERE pages_fts MATCH ?
                ORDER BY score
                LIMIT ?
            """, (match_query, limit)).fetchall()
        except sqlite3.OperationalError:
            rows = []
    results = []
    for row in rows:
        body = row["body"] or ""
        lower_body = body.lower()
        pos = lower_body.find(terms[0].lower())
        start = max(0, pos - 90) if pos >= 0 else 0
        snippet = body[start:start + 240].strip()
        if start > 0:
            snippet = "…" + snippet
        if start + 240 < len(body):
            snippet += "…"
        results.append({
            "url": row["url"],
            "title": row["title"] or row["url"],
            "description": row["description"],
            "snippet": row["description"] or snippet or row["url"],
        })
    return {"query": q, "results": results, "total": len(results)}

@app.get("/api/pages")
def pages(limit: int = 20):
    limit = max(1, min(int(limit), 100))
    with db() as conn:
        rows = conn.execute(
            "SELECT url, title, crawled_at FROM pages ORDER BY crawled_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return {"pages": [dict(r) for r in rows]}
