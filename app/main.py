import asyncio
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

DATA_DIR = Path(os.getenv("DATA_DIR", "/data")).resolve()
DB_PATH = DATA_DIR / "github_archive.sqlite3"
REPOS_DIR = DATA_DIR / "repos"
INTERVAL_MIN = max(1, int(os.getenv("CHECK_INTERVAL_MINUTES", "360")))
INCLUDE_PRERELEASES = os.getenv("INCLUDE_PRERELEASES", "false").lower() == "true"
MAX_RETRIES = max(1, int(os.getenv("MAX_DOWNLOAD_RETRIES", "4")))
TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

app = FastAPI(title="GitHub Archive", version="1.1.0")
check_lock = asyncio.Lock()
background_task = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 full_name TEXT NOT NULL UNIQUE,
 url TEXT NOT NULL,
 name TEXT NOT NULL,
 default_branch TEXT,
 latest_version TEXT,
 latest_kind TEXT,
 latest_date TEXT,
 latest_url TEXT,
 status TEXT NOT NULL DEFAULT 'never',
 error TEXT,
 last_checked_at TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
 version TEXT NOT NULL,
 tag_name TEXT NOT NULL,
 kind TEXT NOT NULL,
 published_at TEXT,
 html_url TEXT,
 target_sha TEXT,
 status TEXT NOT NULL DEFAULT 'pending',
 error TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(repo_id, tag_name)
);
CREATE TABLE IF NOT EXISTS files (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
 category TEXT NOT NULL,
 name TEXT NOT NULL,
 relative_path TEXT NOT NULL,
 source_url TEXT NOT NULL,
 expected_size INTEGER,
 sha256 TEXT,
 status TEXT NOT NULL DEFAULT 'pending',
 size INTEGER DEFAULT 0,
 downloaded_at TEXT,
 error TEXT,
 UNIQUE(version_id, relative_path)
);
CREATE INDEX IF NOT EXISTS idx_versions_repo ON versions(repo_id);
CREATE INDEX IF NOT EXISTS idx_files_version ON files(version_id);
"""

def now():
    return datetime.now(timezone.utc).isoformat()

def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(SCHEMA)
    return c

def rowdict(row):
    return dict(row) if row else None

def safe_name(s):
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("._")
    return s or "unnamed"

def parse_repo_url(url):
    p = urlparse(url.strip())
    if p.scheme not in ("http", "https") or p.netloc.lower() != "github.com":
        raise ValueError("Repository URL must be a github.com URL")
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 2:
        raise ValueError("Repository URL must look like https://github.com/owner/repository")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid GitHub repository name")
    return owner + "/" + repo

class RepoIn(BaseModel):
    url: str

def gh_headers(auth=True):
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-archive/1.1.0",
    }
    if auth and TOKEN:
        h["Authorization"] = "Bearer " + TOKEN
    return h

async def gh_json(client, url):
    r = await client.get(url, headers=gh_headers())
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub API {r.status_code}: {r.text[:500]}")
    return r.json()

async def discover(repo):
    api = f"https://api.github.com/repos/{repo}"
    async with httpx.AsyncClient(timeout=45) as client:
        meta = await gh_json(client, api)
        releases = await gh_json(client, api + "/releases?per_page=30")
        chosen = None
        for rel in releases:
            if rel.get("draft"):
                continue
            if rel.get("prerelease") and not INCLUDE_PRERELEASES:
                continue
            chosen = {
                "version": rel["tag_name"],
                "tag_name": rel["tag_name"],
                "kind": "release",
                "published_at": rel.get("published_at") or rel.get("created_at"),
                "html_url": rel["html_url"],
                "sha": None,
                "assets": rel.get("assets", []),
            }
            break
        if chosen is None:
            tags = await gh_json(client, api + "/tags?per_page=1")
            if not tags:
                return meta, None
            tag = tags[0]
            sha = tag["commit"]["sha"]
            commit = await gh_json(client, api + "/commits/" + sha)
            chosen = {
                "version": tag["name"],
                "tag_name": tag["name"],
                "kind": "tag",
                "published_at": commit.get("commit", {}).get("committer", {}).get("date"),
                "html_url": f"https://github.com/{repo}/releases/tag/{quote(tag['name'], safe='')}",
                "sha": sha,
                "assets": [],
            }
        chosen["default_branch"] = meta["default_branch"]
        return meta, chosen

def upsert_version(c, repo_row, info):
    t = now()
    c.execute(
        """INSERT INTO versions(repo_id,version,tag_name,kind,published_at,html_url,target_sha,status,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,'pending',?,?)
        ON CONFLICT(repo_id,tag_name) DO UPDATE SET
          version=excluded.version, kind=excluded.kind, published_at=excluded.published_at,
          html_url=excluded.html_url, target_sha=excluded.target_sha, updated_at=excluded.updated_at""",
        (repo_row["id"], info["version"], info["tag_name"], info["kind"], info["published_at"],
         info["html_url"], info["sha"], t, t),
    )
    return c.execute("SELECT * FROM versions WHERE repo_id=? AND tag_name=?", (repo_row["id"], info["tag_name"])).fetchone()

def upsert_file(c, version_id, category, name, relative_path, source_url, expected_size=None, sha256=None):
    c.execute(
        """INSERT INTO files(version_id,category,name,relative_path,source_url,expected_size,sha256,status)
        VALUES(?,?,?,?,?,?,?,'pending')
        ON CONFLICT(version_id,relative_path) DO UPDATE SET
          source_url=excluded.source_url, expected_size=excluded.expected_size,
          sha256=excluded.sha256""",
        (version_id, category, name, relative_path, source_url, expected_size, sha256),
    )

async def create_manifest(repo_row, version_row, info):
    c = db()
    version_dir = REPOS_DIR / safe_name(repo_row["full_name"].split("/")[0]) / safe_name(repo_row["name"]) / safe_name(version_row["version"])
    release_dir = version_dir / "release"
    source_dir = version_dir / "source"
    current_dir = version_dir / "repository-current"
    for d in (release_dir, source_dir, current_dir):
        d.mkdir(parents=True, exist_ok=True)

    for asset in info["assets"]:
        name = safe_name(asset["name"])
        upsert_file(c, version_row["id"], "release", name, str(Path("release") / name),
                    asset["browser_download_url"], asset.get("size"), asset.get("digest"))
    exact_name = f"{safe_name(repo_row['name'])}-{safe_name(version_row['tag_name'])}-source.zip"
    current_name = f"{safe_name(repo_row['name'])}-current-{safe_name(info['default_branch'])}.zip"
    upsert_file(c, version_row["id"], "source", exact_name, str(Path("source") / exact_name),
                f"https://api.github.com/repos/{repo_row['full_name']}/zipball/{quote(version_row['tag_name'], safe='')}")
    upsert_file(c, version_row["id"], "repository-current", current_name, str(Path("repository-current") / current_name),
                f"https://api.github.com/repos/{repo_row['full_name']}/zipball/{quote(info['default_branch'], safe='')}")
    c.commit()
    c.close()
    return version_dir

async def download_one(file_row, version_dir):
    rel = Path(file_row["relative_path"])
    target = (version_dir / rel).resolve()
    if not str(target).startswith(str(version_dir.resolve()) + os.sep):
        raise RuntimeError("Unsafe archive path")
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")

    c = db()
    c.execute("UPDATE files SET status='downloading', error=NULL WHERE id=?", (file_row["id"],))
    c.commit()
    c.close()

    expected = file_row["expected_size"]
    digest = file_row["sha256"]
    if target.exists() and target.is_file() and (expected is None or target.stat().st_size == expected):
        c = db()
        c.execute("UPDATE files SET status='complete',size=?,downloaded_at=?,error=NULL WHERE id=?",
                  (target.stat().st_size, now(), file_row["id"]))
        c.commit(); c.close()
        return

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            start = part.stat().st_size if part.exists() else 0
            headers = gh_headers()
            if start:
                headers["Range"] = f"bytes={start}-"
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0),
                                         follow_redirects=False) as client:
                r = await client.get(file_row["source_url"], headers=headers)
                if r.status_code in (301,302,303,307,308):
                    location = r.headers.get("location")
                    if not location:
                        raise RuntimeError("GitHub returned a redirect without Location")
                    h2 = {"User-Agent": "github-archive/1.1.0"}
                    if start:
                        h2["Range"] = f"bytes={start}-"
                    r = await client.get(location, headers=h2, follow_redirects=True)
                if start and r.status_code == 200:
                    start = 0
                    part.unlink(missing_ok=True)
                if r.status_code == 416:
                    part.unlink(missing_ok=True)
                    continue
                if r.status_code >= 400:
                    raise RuntimeError(f"Download HTTP {r.status_code}: {r.text[:300]}")

                mode = "ab" if start and r.status_code == 206 else "wb"
                with part.open(mode) as f:
                    async for chunk in r.aiter_bytes(1024 * 1024):
                        if chunk:
                            f.write(chunk)
                size = part.stat().st_size
                if expected is not None and size != expected:
                    raise RuntimeError(f"Size mismatch: got {size}, expected {expected}")
                if digest and digest.startswith("sha256:"):
                    h = hashlib.sha256()
                    with part.open("rb") as f:
                        for block in iter(lambda: f.read(1024 * 1024), b""):
                            h.update(block)
                    if h.hexdigest().lower() != digest.split(":", 1)[1].lower():
                        raise RuntimeError("SHA-256 digest mismatch")
                part.replace(target)
                c = db()
                c.execute("UPDATE files SET status='complete',size=?,downloaded_at=?,error=NULL WHERE id=?",
                          (size, now(), file_row["id"]))
                c.commit(); c.close()
                return
        except Exception as e:
            if attempt == MAX_RETRIES:
                c = db()
                c.execute("UPDATE files SET status='failed',error=? WHERE id=?", (str(e), file_row["id"]))
                c.commit(); c.close()
                raise
            await asyncio.sleep(min(30, 2 ** attempt))

async def archive_version(repo_row, version_row, info):
    version_dir = await create_manifest(repo_row, version_row, info)
    c = db()
    files = c.execute("SELECT * FROM files WHERE version_id=? ORDER BY id", (version_row["id"],)).fetchall()
    c.close()
    errors = []
    for f in files:
        try:
            await download_one(f, version_dir)
        except Exception as e:
            errors.append(f"{f['name']}: {e}")
    c = db()
    status = "complete" if not errors else "failed"
    c.execute("UPDATE versions SET status=?,error=?,updated_at=? WHERE id=?",
              (status, "\n".join(errors)[:4000] if errors else None, now(), version_row["id"]))
    c.commit(); c.close()
    if errors:
        raise RuntimeError("; ".join(errors))

async def check_repo(repo_id):
    async with check_lock:
        c = db()
        repo = c.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()
        c.close()
        if not repo:
            raise HTTPException(404, "Repository not found")
        try:
            meta, info = await discover(repo["full_name"])
            c = db()
            c.execute("""UPDATE repos SET name=?,default_branch=?,status='checking',error=NULL,updated_at=? WHERE id=?""",
                      (meta["name"], meta["default_branch"], now(), repo_id))
            c.commit()
            if info is None:
                c.execute("""UPDATE repos SET status='no_release',latest_version=NULL,latest_kind=NULL,
                    latest_date=NULL,latest_url=NULL,last_checked_at=?,updated_at=? WHERE id=?""",
                          (now(), now(), repo_id))
                c.commit(); c.close()
                return {"status":"no_release"}
            vr = upsert_version(c, dict(c.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()), info)
            c.commit(); c.close()
            c = db()
            pending = c.execute("SELECT * FROM versions WHERE repo_id=? AND status!='complete' ORDER BY id", (repo_id,)).fetchall()
            c.close()
            for v in pending:
                await archive_version(dict(repo), v, info if v["tag_name"] == info["tag_name"] else {
                    "default_branch": meta["default_branch"], "assets": []
                })
            c = db()
            c.execute("""UPDATE repos SET latest_version=?,latest_kind=?,latest_date=?,latest_url=?,
                status='complete',last_checked_at=?,error=NULL,updated_at=? WHERE id=?""",
                      (info["version"], info["kind"], info["published_at"], info["html_url"], now(), now(), repo_id))
            c.commit(); c.close()
            return {"status":"complete","version":info["version"],"kind":info["kind"]}
        except Exception as e:
            c = db()
            c.execute("UPDATE repos SET status='failed',error=?,last_checked_at=?,updated_at=? WHERE id=?",
                      (str(e)[:4000], now(), now(), repo_id))
            c.commit(); c.close()
            raise

async def scheduler():
    await asyncio.sleep(5)
    while True:
        try:
            c = db()
            rows = c.execute("SELECT * FROM repos").fetchall()
            c.close()
            t = datetime.now(timezone.utc)
            for r in rows:
                due = True
                if r["last_checked_at"]:
                    try:
                        last = datetime.fromisoformat(r["last_checked_at"])
                        due = (t - last).total_seconds() >= INTERVAL_MIN * 60
                    except ValueError:
                        pass
                if due:
                    try:
                        await check_repo(r["id"])
                    except Exception:
                        pass
        except Exception:
            pass
        await asyncio.sleep(60)

@app.on_event("startup")
async def startup():
    global background_task
    db().close()
    background_task = asyncio.create_task(scheduler())

@app.on_event("shutdown")
async def shutdown():
    if background_task:
        background_task.cancel()

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8"))

@app.get("/health")
async def health():
    return {"ok": True, "version": "1.1.0"}

@app.get("/api/settings")
async def settings():
    return {"check_interval_minutes": INTERVAL_MIN, "include_prereleases": INCLUDE_PRERELEASES, "token_configured": bool(TOKEN)}

@app.get("/api/repos")
async def list_repos():
    c = db()
    rows = c.execute("SELECT * FROM repos ORDER BY name COLLATE NOCASE").fetchall()
    c.close()
    return [rowdict(r) for r in rows]

@app.post("/api/repos")
async def add_repo(body: RepoIn):
    try:
        full = parse_repo_url(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    c = db()
    existing = c.execute("SELECT id FROM repos WHERE full_name=?", (full,)).fetchone()
    if existing:
        c.close()
        raise HTTPException(409, "Repository is already tracked")
    t = now()
    c.execute("INSERT INTO repos(full_name,url,name,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
              (full, f"https://github.com/{full}", full.split("/",1)[1], "never", t, t))
    rid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.commit(); c.close()
    asyncio.create_task(check_repo(rid))
    return {"id": rid, "full_name": full}

@app.delete("/api/repos/{repo_id}")
async def delete_repo(repo_id: int):
    c = db()
    r = c.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()
    if not r:
        c.close(); raise HTTPException(404, "Repository not found")
    c.execute("DELETE FROM repos WHERE id=?", (repo_id,))
    c.commit(); c.close()
    return {"ok": True}

@app.post("/api/repos/{repo_id}/check")
async def manual_check(repo_id: int):
    return await check_repo(repo_id)

@app.post("/api/check-all")
async def check_all():
    c = db(); ids = [r["id"] for r in c.execute("SELECT id FROM repos").fetchall()]; c.close()
    results = []
    for rid in ids:
        try:
            results.append(await check_repo(rid))
        except Exception as e:
            results.append({"status":"failed","error":str(e)})
    return results

@app.get("/api/repos/{repo_id}/versions")
async def versions(repo_id: int):
    c = db()
    if not c.execute("SELECT 1 FROM repos WHERE id=?", (repo_id,)).fetchone():
        c.close(); raise HTTPException(404, "Repository not found")
    rows = c.execute("SELECT * FROM versions WHERE repo_id=? ORDER BY id DESC", (repo_id,)).fetchall()
    c.close()
    return [rowdict(r) for r in rows]

@app.get("/api/versions/{version_id}")
async def version_detail(version_id: int):
    c = db()
    v = c.execute("SELECT v.*,r.full_name,r.name AS repo_name FROM versions v JOIN repos r ON r.id=v.repo_id WHERE v.id=?",
                  (version_id,)).fetchone()
    if not v:
        c.close(); raise HTTPException(404, "Version not found")
    fs = c.execute("SELECT * FROM files WHERE version_id=? ORDER BY category,name", (version_id,)).fetchall()
    c.close()
    return {"version":rowdict(v), "files":[rowdict(x) for x in fs]}

@app.get("/download/{file_id}")
async def download(file_id: int):
    c = db()
    f = c.execute("""SELECT f.*,v.repo_id,v.version,r.full_name,r.name AS repo_name
                    FROM files f JOIN versions v ON v.id=f.version_id JOIN repos r ON r.id=v.repo_id
                    WHERE f.id=?""", (file_id,)).fetchone()
    c.close()
    if not f:
        raise HTTPException(404, "File not found")
    base = REPOS_DIR / safe_name(f["full_name"].split("/")[0]) / safe_name(f["repo_name"]) / safe_name(f["version"])
    path = (base / f["relative_path"]).resolve()
    if not str(path).startswith(str(base.resolve()) + os.sep) or not path.is_file():
        raise HTTPException(404, "Archived file is not available")
    return FileResponse(path, filename=path.name)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
