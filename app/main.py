import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

DATA_DIR = Path(os.getenv("DATA_DIR", "/data")).resolve()
DB_PATH = DATA_DIR / "github_archive.sqlite3"
REPOS_DIR = DATA_DIR / "repos"
DEFAULT_INTERVAL = max(1, int(os.getenv("CHECK_INTERVAL_MINUTES", "360")))
DEFAULT_PRERELEASES = os.getenv("INCLUDE_PRERELEASES", "false").lower() == "true"
MAX_RETRIES = max(1, int(os.getenv("MAX_DOWNLOAD_RETRIES", "4")))
WORKERS = max(1, int(os.getenv("DOWNLOAD_WORKERS", "2")))
TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

app = FastAPI(title="GitHub Archive", version="2.0.0")
check_lock = asyncio.Lock()
background_task = None
worker_tasks = []
queue_event = asyncio.Event()

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS groups (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 name TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repos (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 full_name TEXT NOT NULL UNIQUE, url TEXT NOT NULL, name TEXT NOT NULL,
 default_branch TEXT, latest_version TEXT, latest_kind TEXT, latest_date TEXT, latest_url TEXT,
 status TEXT NOT NULL DEFAULT 'never', error TEXT, last_checked_at TEXT,
 group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
 policy TEXT NOT NULL DEFAULT '{"releases":true,"assets":true,"source":true,"current":true,"tags":true,"artifacts":false,"commits":false,"prereleases":false}',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
 version TEXT NOT NULL, tag_name TEXT NOT NULL, kind TEXT NOT NULL, published_at TEXT,
 html_url TEXT, target_sha TEXT, status TEXT NOT NULL DEFAULT 'pending', error TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(repo_id, tag_name)
);
CREATE TABLE IF NOT EXISTS files (
 id INTEGER PRIMARY KEY AUTOINCREMENT, version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
 category TEXT NOT NULL, name TEXT NOT NULL, relative_path TEXT NOT NULL, source_url TEXT NOT NULL,
 expected_size INTEGER, sha256 TEXT, status TEXT NOT NULL DEFAULT 'pending', size INTEGER DEFAULT 0,
 downloaded_at TEXT, error TEXT, verify_status TEXT DEFAULT 'unverified',
 UNIQUE(version_id, relative_path)
);
CREATE TABLE IF NOT EXISTS queue (
 id INTEGER PRIMARY KEY AUTOINCREMENT, file_id INTEGER NOT NULL UNIQUE REFERENCES files(id) ON DELETE CASCADE,
 status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
 queued_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS idx_versions_repo ON versions(repo_id);
CREATE INDEX IF NOT EXISTS idx_files_version ON files(version_id);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);
"""

def now(): return datetime.now(timezone.utc).isoformat()
def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True); REPOS_DIR.mkdir(parents=True, exist_ok=True)
    c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); c.executescript(SCHEMA)
    cols={r["name"] for r in c.execute("PRAGMA table_info(repos)").fetchall()}
    if "monitoring" not in cols: c.execute("ALTER TABLE repos ADD COLUMN monitoring INTEGER NOT NULL DEFAULT 1")
    defaults={"check_interval_minutes":str(DEFAULT_INTERVAL),"include_prereleases":str(DEFAULT_PRERELEASES).lower()}
    for k,v in defaults.items(): c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,v))
    c.commit(); return c
def rd(x): return dict(x) if x else None
def safe_name(s): return re.sub(r"[^A-Za-z0-9._-]+","_",s).strip("._") or "unnamed"
def parse_repo_url(url):
    p=urlparse(url.strip())
    if p.scheme not in ("http","https") or p.netloc.lower()!="github.com": raise ValueError("Repository URL must be a github.com URL")
    parts=[x for x in p.path.split("/") if x]
    if len(parts)<2: raise ValueError("Repository URL must look like https://github.com/owner/repository")
    owner,repo=parts[0],parts[1].removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+",owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+",repo): raise ValueError("Invalid GitHub repository name")
    return owner+"/"+repo
def settings_map(c):
    return {r["key"]:r["value"] for r in c.execute("SELECT key,value FROM settings")}
def get_bool(c,k,default=False): return settings_map(c).get(k,str(default).lower()).lower()=="true"
def get_int(c,k,default): return max(1,int(settings_map(c).get(k,str(default))))
def gh_headers(auth=True):
    h={"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28","User-Agent":"github-archive/2.0.0"}
    if auth and TOKEN: h["Authorization"]="Bearer "+TOKEN
    return h
async def gh_json(client,url):
    last=None
    for attempt in range(4):
        try:
            r=await client.get(url,headers=gh_headers())
            if r.status_code < 400: return r.json()
            last=RuntimeError(f"GitHub API {r.status_code}: {r.text[:500]}")
            if r.status_code not in (429,500,502,503,504): raise last
            retry_after=r.headers.get("Retry-After")
            delay=float(retry_after) if retry_after and retry_after.replace(".","",1).isdigit() else min(8,2**attempt)
            await asyncio.sleep(delay)
        except (httpx.TimeoutException,httpx.NetworkError) as e:
            last=e
            if attempt==3: raise
            await asyncio.sleep(min(8,2**attempt))
    raise last or RuntimeError("GitHub API request failed")

async def resolve_tag_sha(client,repo,tag_name):
    ref=await gh_json(client,f"https://api.github.com/repos/{repo}/git/ref/tags/{quote(tag_name,safe='')}")
    obj=ref.get("object") or {}
    sha=obj.get("sha")
    if not sha: raise RuntimeError(f"GitHub tag {tag_name} has no target SHA")
    if obj.get("type")=="tag":
        tag=await gh_json(client,f"https://api.github.com/repos/{repo}/git/tags/{sha}")
        sha=(tag.get("object") or {}).get("sha") or sha
    return sha

async def discover(repo, policy):
    api=f"https://api.github.com/repos/{repo}"
    async with httpx.AsyncClient(timeout=45) as client:
        meta=await gh_json(client,api)
        releases=await gh_json(client,api+"/releases?per_page=100&page=1")
        chosen=None
        tag_info=None
        for rel in releases:
            if not policy.get("releases",True): break
            if rel.get("draft") or (rel.get("prerelease") and not policy.get("prereleases",False)): continue
            sha=await resolve_tag_sha(client,repo,rel["tag_name"])
            chosen={"version":rel["tag_name"],"tag_name":rel["tag_name"],"kind":"release",
                    "published_at":rel.get("published_at") or rel.get("created_at"),
                    "html_url":rel["html_url"],"sha":sha,"assets":rel.get("assets",[])}
            break
        mode=policy.get("mode","release_fallback")
        want_tag=mode in ("tags_only","both") or chosen is None
        if want_tag and policy.get("tags",True):
            tags=await gh_json(client,api+"/tags?per_page=1&page=1")
            if tags:
                tag=tags[0]
                sha=await resolve_tag_sha(client,repo,tag["name"])
                commit=await gh_json(client,api+"/commits/"+sha)
                tag_info={"version":tag["name"],"tag_name":tag["name"],"kind":"tag",
                          "published_at":commit.get("commit",{}).get("committer",{}).get("date"),
                          "html_url":f"https://github.com/{repo}/releases/tag/{quote(tag['name'],safe='')}",
                          "sha":sha,"assets":[]}
                if mode=="tags_only" or chosen is None: chosen=tag_info
        if chosen is None: return meta,None
        chosen["default_branch"]=meta["default_branch"]
        if tag_info: tag_info["default_branch"]=meta["default_branch"]
        return meta,(chosen,tag_info)

def policy_for(repo):
    try: return json.loads(repo["policy"] or "{}")
    except Exception: return {"releases":True,"assets":True,"source":True,"current":True,"tags":True}
def repo_dirs(full,version):
    owner,name=full.split("/",1); base=REPOS_DIR/safe_name(owner)/safe_name(name)/safe_name(version)
    return base,base/"release",base/"source",base/"repository-current",base/"actions",base/"commits"

def upsert_version(c,repo,info):
    t=now()
    c.execute("""INSERT INTO versions(repo_id,version,tag_name,kind,published_at,html_url,target_sha,status,created_at,updated_at)
                 VALUES(?,?,?,?,?,?,?,'pending',?,?)
                 ON CONFLICT(repo_id,tag_name) DO UPDATE SET version=excluded.version,kind=excluded.kind,published_at=excluded.published_at,
                 html_url=excluded.html_url,target_sha=excluded.target_sha,updated_at=excluded.updated_at""",
              (repo["id"],info["version"],info["tag_name"],info["kind"],info["published_at"],info["html_url"],info["sha"],t,t))
    return c.execute("SELECT * FROM versions WHERE repo_id=? AND tag_name=?",(repo["id"],info["tag_name"])).fetchone()
def upsert_file(c,vid,cat,name,rel,url,size=None,digest=None):
    c.execute("""INSERT INTO files(version_id,category,name,relative_path,source_url,expected_size,sha256,status)
                 VALUES(?,?,?,?,?,?,?,'pending')
                 ON CONFLICT(version_id,relative_path) DO UPDATE SET source_url=excluded.source_url,expected_size=excluded.expected_size,sha256=excluded.sha256""",
              (vid,cat,name,rel,url,size,digest))
def enqueue(c,fid):
    c.execute("INSERT OR IGNORE INTO queue(file_id,status,queued_at) VALUES(?,'queued',?)",(fid,now()))

async def list_actions_artifacts(repo, head_sha):
    if not head_sha: return []
    artifacts=[]
    async with httpx.AsyncClient(timeout=60) as client:
        page=1
        while True:
            data=await gh_json(client,f"https://api.github.com/repos/{repo}/actions/artifacts?per_page=100&page={page}")
            items=data.get("artifacts",[])
            if not items: break
            for a in items:
                run=a.get("workflow_run") or {}
                if a.get("expired") or run.get("head_sha")!=head_sha: continue
                artifacts.append(a)
            if len(items)<100: break
            page+=1
    return artifacts

async def create_manifest(repo,version,info,include_current=True):
    policy=policy_for(repo); base,release,source,current,actions,commits=repo_dirs(repo["full_name"],version["version"])
    for d in (release,source,current,actions,commits): d.mkdir(parents=True,exist_ok=True)
    c=db()
    if policy.get("assets",True) and version["kind"]=="release":
        for a in info.get("assets",[]):
            name=safe_name(a["name"]); upsert_file(c,version["id"],"release",name,str(Path("release")/name),a["browser_download_url"],a.get("size"),a.get("digest"))
    if policy.get("source",True):
        n=f"{safe_name(repo['name'])}-{safe_name(version['tag_name'])}-source.zip"
        upsert_file(c,version["id"],"source",n,str(Path("source")/n),f"https://api.github.com/repos/{repo['full_name']}/zipball/{quote(version['tag_name'],safe='')}")
    if include_current and policy.get("current",True):
        n=f"{safe_name(repo['name'])}-current-{safe_name(info['default_branch'])}.zip"
        upsert_file(c,version["id"],"repository-current",n,str(Path("repository-current")/n),f"https://api.github.com/repos/{repo['full_name']}/zipball/{quote(info['default_branch'],safe='')}")
    if policy.get("artifacts",False) and version.get("target_sha"):
        for a in await list_actions_artifacts(repo["full_name"],version["target_sha"]):
            name=f"{safe_name(a.get('name') or 'artifact')}-{a.get('id')}.zip"
            upsert_file(c,version["id"],"actions",name,str(Path("actions")/name),
                        a["archive_download_url"],a.get("size_in_bytes"),a.get("digest"))

    if policy.get("commits",False) and version.get("target_sha"):
        sha=version["target_sha"]
        name=f"commit-{safe_name(sha)}.zip"
        upsert_file(c,version["id"],"commits",name,str(Path("commits")/name),
                    f"https://api.github.com/repos/{repo['full_name']}/zipball/{quote(sha,safe='')}")

    rows=c.execute("SELECT * FROM files WHERE version_id=?",(version["id"],)).fetchall()
    for f in rows: enqueue(c,f["id"])
    c.commit()
    meta={"repository":repo["full_name"],"version":version["version"],"tag":version["tag_name"],"kind":version["kind"],"target_sha":version["target_sha"],
          "default_branch":info["default_branch"],"published_at":version["published_at"],"release_url":version["html_url"],
          "archived_at":now(),"assets":[{"name":a.get("name"),"size":a.get("size"),"digest":a.get("digest"),"url":a.get("browser_download_url")} for a in info.get("assets",[])]}
    (base/"metadata.json").write_text(json.dumps(meta,indent=2),encoding="utf-8")
    c.close(); return base

async def download_one(f):
    file_id=f["file_id"]
    base=repo_dirs(f["full_name"],f["version"])[0]
    target=(base/Path(f["relative_path"])).resolve()
    if not str(target).startswith(str(base.resolve())+os.sep):
        raise RuntimeError("Invalid archive path")
    part=target.with_name(target.name+".part")
    target.parent.mkdir(parents=True,exist_ok=True)
    expected=f["expected_size"]; digest=f["sha256"]
    try:
        for attempt in range(1,MAX_RETRIES+1):
            try:
                start=part.stat().st_size if part.exists() else 0
                headers=gh_headers()
                if start: headers["Range"]=f"bytes={start}-"
                async with httpx.AsyncClient(timeout=httpx.Timeout(120,connect=30),follow_redirects=False) as client:
                    async with client.stream("GET",f["source_url"],headers=headers) as r:
                        if r.status_code in (301,302,303,307,308):
                            loc=r.headers.get("location")
                            if not loc: raise RuntimeError("Redirect without Location")
                            h={"User-Agent":"github-archive/2.0.0"}
                            if start: h["Range"]=f"bytes={start}-"
                            async with client.stream("GET",loc,headers=h,follow_redirects=True) as rr:
                                if start and rr.status_code==200: start=0; part.unlink(missing_ok=True)
                                if rr.status_code==416: part.unlink(missing_ok=True); continue
                                if rr.status_code>=400: raise RuntimeError(f"Download HTTP {rr.status_code}")
                                with part.open("ab" if start and rr.status_code==206 else "wb") as out:
                                    async for chunk in rr.aiter_bytes(1024*1024): out.write(chunk)
                        else:
                            if start and r.status_code==200: start=0; part.unlink(missing_ok=True)
                            if r.status_code==416: part.unlink(missing_ok=True); continue
                            if r.status_code>=400: raise RuntimeError(f"Download HTTP {r.status_code}")
                            with part.open("ab" if start and r.status_code==206 else "wb") as out:
                                async for chunk in r.aiter_bytes(1024*1024): out.write(chunk)
                size=part.stat().st_size
                if expected is not None and size!=expected: raise RuntimeError(f"Size mismatch: got {size}, expected {expected}")
                h=hashlib.sha256()
                with part.open("rb") as inp:
                    for block in iter(lambda:inp.read(1024*1024),b""): h.update(block)
                actual=h.hexdigest()
                if digest and digest.startswith("sha256:") and actual.lower()!=digest.split(":",1)[1].lower():
                    raise RuntimeError("SHA-256 digest mismatch")
                part.replace(target)
                c=db()
                c.execute("UPDATE files SET status='complete',size=?,downloaded_at=?,error=NULL,verify_status=? WHERE id=?",
                          (size,now(),"verified" if digest else "verified-local",file_id))
                c.execute("UPDATE queue SET status='done',finished_at=?,error=NULL WHERE id=?",
                          (now(),f["queue_id"]))
                c.commit(); c.close()
                return
            except Exception:
                if attempt==MAX_RETRIES: raise
                await asyncio.sleep(min(30,2**attempt))
    except Exception as e:
        c=db()
        c.execute("UPDATE files SET status='failed',error=?,verify_status='failed' WHERE id=?",(str(e)[:2000],file_id))
        c.execute("UPDATE queue SET status='failed',finished_at=?,error=? WHERE id=?",(now(),str(e)[:2000],f["queue_id"]))
        c.commit(); c.close()
        raise


async def file_repo(fid):
    c=db(); r=c.execute("SELECT f.*,v.version,v.repo_id,r.full_name,r.name AS repo_name FROM files f JOIN versions v ON v.id=f.version_id JOIN repos r ON r.id=v.repo_id WHERE f.id=?",(fid,)).fetchone(); c.close()
    if not r: raise RuntimeError("File not found")
    return r

def claim_next_queue_item():
    c=db()
    try:
        c.execute("BEGIN IMMEDIATE")
        row=c.execute("""SELECT q.id queue_id,q.file_id,q.attempts,
                                f.version_id,f.category,f.name,f.relative_path,f.source_url,
                                f.expected_size,f.sha256,f.status file_status,
                                v.version,r.full_name
                         FROM queue q
                         JOIN files f ON f.id=q.file_id
                         JOIN versions v ON v.id=f.version_id
                         JOIN repos r ON r.id=v.repo_id
                         WHERE q.status='queued'
                         ORDER BY q.id LIMIT 1""").fetchone()
        if not row:
            c.commit()
            return None
        changed=c.execute("""UPDATE queue SET status='running',started_at=?,attempts=attempts+1,error=NULL
                             WHERE id=? AND status='queued'""",(now(),row["queue_id"])).rowcount
        if changed != 1:
            c.rollback()
            return None
        c.execute("UPDATE files SET status='downloading',error=NULL WHERE id=?",(row["file_id"],))
        c.commit()
        return dict(row)
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

async def worker():
    while True:
        try:
            q=claim_next_queue_item()
            if not q:
                try: await asyncio.wait_for(queue_event.wait(),timeout=10)
                except asyncio.TimeoutError: pass
                queue_event.clear()
                continue
            try: await download_one(q)
            except Exception: pass
        except Exception:
            await asyncio.sleep(2)

async def archive_version(repo,version,info):
    await create_manifest(repo,version,info)
    # Version remains pending until its queue files are complete.
    c=db(); total=c.execute("SELECT COUNT(*) n FROM files WHERE version_id=?",(version["id"],)).fetchone()["n"]; done=c.execute("SELECT COUNT(*) n FROM files WHERE version_id=? AND status='complete'",(version["id"],)).fetchone()["n"]
    if total==0: c.execute("UPDATE versions SET status='complete',error=NULL,updated_at=? WHERE id=?",(now(),version["id"]))
    elif done==total: c.execute("UPDATE versions SET status='complete',error=NULL,updated_at=? WHERE id=?",(now(),version["id"]))
    else: c.execute("UPDATE versions SET status='queued',updated_at=? WHERE id=?",(now(),version["id"]))
    c.commit(); c.close(); queue_event.set()

async def list_user_repos(username):
    username=username.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9-]+",username):
        raise ValueError("Invalid GitHub username")
    repos=[]
    async with httpx.AsyncClient(timeout=45) as client:
        page=1
        while True:
            items=await gh_json(client,f"https://api.github.com/users/{quote(username,safe='')}/repos?per_page=100&page={page}&type=owner")
            if not items: break
            repos.extend(items)
            if len(items)<100: break
            page+=1
    return repos

async def list_repo_releases(full_name, include_prereleases=True):
    releases=[]
    async with httpx.AsyncClient(timeout=60) as client:
        page=1
        while True:
            items=await gh_json(client,f"https://api.github.com/repos/{full_name}/releases?per_page=100&page={page}")
            if not items: break
            for rel in items:
                if rel.get("draft"): continue
                if rel.get("prerelease") and not include_prereleases: continue
                releases.append({
                    "version":rel["tag_name"],"tag_name":rel["tag_name"],"kind":"release",
                    "published_at":rel.get("published_at") or rel.get("created_at"),
                    "html_url":rel["html_url"],"sha":None,"assets":rel.get("assets",[])
                })
            if len(items)<100: break
            page+=1
    return releases

async def import_repo_history(repo_id):
    c=db(); repo=c.execute("SELECT * FROM repos WHERE id=?",(repo_id,)).fetchone(); c.close()
    if not repo: return
    try:
        meta,_=await discover(repo["full_name"],{"prereleases":True})
        releases=await list_repo_releases(repo["full_name"],True)
        c=db()
        c.execute("UPDATE repos SET name=?,default_branch=?,status='importing',error=NULL,updated_at=? WHERE id=?",
                  (meta["name"],meta["default_branch"],now(),repo_id)); c.commit()
        repo_now=dict(c.execute("SELECT * FROM repos WHERE id=?",(repo_id,)).fetchone())
        c.close()
        for info in reversed(releases):
            info["default_branch"]=meta["default_branch"]
            c=db(); v=upsert_version(c,repo_now,info); c.commit(); known=c.execute(
                "SELECT * FROM versions WHERE repo_id=? AND tag_name=?",(repo_id,info["tag_name"])).fetchone(); c.close()
            if known["status"]!="complete":
                await create_manifest(repo_now,known,info,include_current=False)
        if releases:
            latest=releases[0]
            latest["default_branch"]=meta["default_branch"]
            c=db(); lv=c.execute("SELECT * FROM versions WHERE repo_id=? AND tag_name=?",(repo_id,latest["tag_name"])).fetchone(); c.close()
            if lv: await create_manifest(repo_now,lv,latest,include_current=True)
        c=db()
        c.execute("UPDATE repos SET status='queued',latest_version=?,latest_kind='release',latest_date=?,latest_url=?,last_checked_at=?,updated_at=? WHERE id=?",
                  ((releases[0]["version"] if releases else None),
                   (releases[0]["published_at"] if releases else None),
                   (releases[0]["html_url"] if releases else None),now(),now(),repo_id))
        c.commit(); c.close(); queue_event.set()
        if not releases:
            await check_repo(repo_id)
    except Exception as e:
        c=db(); c.execute("UPDATE repos SET status='failed',error=?,updated_at=? WHERE id=?",(str(e)[:4000],now(),repo_id)); c.commit(); c.close()

async def import_user(username, archive_all=True):
    try:
        items=await list_user_repos(username)
    except Exception:
        raise
    results=[]
    for item in items:
        full=item.get("full_name")
        if not full: continue
        c=db(); existing=c.execute("SELECT id FROM repos WHERE full_name=?",(full,)).fetchone()
        if existing:
            rid=existing["id"]
        else:
            t=now()
            c.execute("INSERT INTO repos(full_name,url,name,default_branch,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                      (full,f"https://github.com/{full}",item.get("name") or full.split("/",1)[1],
                       item.get("default_branch"),"never",t,t))
            rid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.commit()
        c.close()
        if archive_all:
            asyncio.create_task(import_repo_history(rid))
        else:
            asyncio.create_task(check_repo(rid))
        results.append({"id":rid,"full_name":full})
    return results

async def check_repo(repo_id):
    async with check_lock:
        c=db(); repo=c.execute("SELECT * FROM repos WHERE id=?",(repo_id,)).fetchone(); c.close()
        if not repo: raise HTTPException(404,"Repository not found")
        try:
            policy=policy_for(repo); meta,discovered=await discover(repo["full_name"],policy)
            info,tag_info=discovered if discovered else (None,None)
            c=db()
            c.execute("UPDATE repos SET name=?,default_branch=?,status='checking',error=NULL,updated_at=? WHERE id=?",
                      (meta["name"],meta["default_branch"],now(),repo_id))
            c.commit()
            if info is None:
                c.execute("UPDATE repos SET status='no_version',last_checked_at=?,updated_at=? WHERE id=?",
                          (now(),now(),repo_id))
                c.commit(); c.close()
                return {"status":"no_version"}
            repo_now=dict(c.execute("SELECT * FROM repos WHERE id=?",(repo_id,)).fetchone())
            c.close()

            c=db()
            v=upsert_version(c,repo_now,info)
            c.commit()
            known=c.execute("SELECT * FROM versions WHERE repo_id=? AND tag_name=?",(repo_id,info["tag_name"])).fetchone()
            c.close()
            if known["status"]!="complete":
                await create_manifest(repo_now,known,info)

            if policy.get("mode")=="both" and tag_info and tag_info["tag_name"]!=info["tag_name"]:
                c=db()
                tv=upsert_version(c,repo_now,tag_info)
                c.commit()
                known_tag=c.execute("SELECT * FROM versions WHERE repo_id=? AND tag_name=?",(repo_id,tag_info["tag_name"])).fetchone()
                c.close()
                if known_tag["status"]!="complete":
                    await create_manifest(repo_now,known_tag,tag_info)

            c=db()
            c.execute("UPDATE repos SET latest_version=?,latest_kind=?,latest_date=?,latest_url=?,last_checked_at=?,status='queued',error=NULL,updated_at=? WHERE id=?",
                      (info["version"],info["kind"],info["published_at"],info["html_url"],now(),now(),repo_id))
            c.commit(); c.close(); queue_event.set()
            return {"status":"queued","version":info["version"],"kind":info["kind"]}
        except Exception as e:
            c=db(); c.execute("UPDATE repos SET status='failed',error=?,last_checked_at=?,updated_at=? WHERE id=?",(str(e)[:4000],now(),now(),repo_id)); c.commit(); c.close(); raise

async def refresh_version_statuses():
    c=db()
    rows=c.execute("SELECT id FROM versions WHERE status IN ('queued','pending','failed')").fetchall()
    for r in rows:
        total=c.execute("SELECT COUNT(*) n FROM files WHERE version_id=?",(r["id"],)).fetchone()["n"]
        done=c.execute("SELECT COUNT(*) n FROM files WHERE version_id=? AND status='complete'",(r["id"],)).fetchone()["n"]
        failed=c.execute("SELECT COUNT(*) n FROM files WHERE version_id=? AND status='failed'",(r["id"],)).fetchone()["n"]
        status="complete" if done==total else ("failed" if failed else "queued")
        c.execute("UPDATE versions SET status=?,updated_at=? WHERE id=?",(status,now(),r["id"]))
    c.commit(); c.close()

async def scheduler():
    await asyncio.sleep(5)
    while True:
        try:
            c=db(); rows=c.execute("SELECT * FROM repos WHERE monitoring=1").fetchall(); interval=get_int(c,"check_interval_minutes",DEFAULT_INTERVAL); c.close()
            t=datetime.now(timezone.utc)
            for r in rows:
                due=True
                if r["last_checked_at"]:
                    try: due=(t-datetime.fromisoformat(r["last_checked_at"])).total_seconds()>=interval*60
                    except ValueError: pass
                if due:
                    try: await check_repo(r["id"])
                    except Exception: pass
            await refresh_version_statuses()
        except Exception: pass
        await asyncio.sleep(60)

class RepoIn(BaseModel): url:str; archive_history:bool=False
class UserIn(BaseModel): username:str; archive_all:bool=True
class MonitoringIn(BaseModel): enabled:bool
class PolicyIn(BaseModel): policy:dict
class GroupIn(BaseModel): name:str
class SettingsIn(BaseModel): check_interval_minutes:int|None=None; include_prereleases:bool|None=None

def recover_interrupted_queue():
    c=db()
    c.execute("""UPDATE queue SET status='queued',started_at=NULL,error=COALESCE(error,'Recovered after service restart')
                WHERE status='running'""")
    c.execute("""UPDATE files SET status='pending',error=COALESCE(error,'Download resumed after service restart')
                WHERE status='downloading'""")
    c.commit()
    c.close()

@app.on_event("startup")
async def startup():
    global background_task,worker_tasks
    db().close()
    recover_interrupted_queue()
    worker_tasks=[asyncio.create_task(worker()) for _ in range(WORKERS)]
    background_task=asyncio.create_task(scheduler())
@app.on_event("shutdown")
async def shutdown():
    if background_task: background_task.cancel()
    for t in worker_tasks: t.cancel()

@app.get("/",response_class=HTMLResponse)
async def index(): return HTMLResponse((Path(__file__).parent/"static"/"index.html").read_text(encoding="utf-8"))
@app.get("/health")
async def health(): return {"ok":True,"version":"2.0.0","workers":WORKERS}

@app.get("/api/settings")
async def settings():
    c=db(); s=settings_map(c); c.close()
    return {"check_interval_minutes":int(s.get("check_interval_minutes",DEFAULT_INTERVAL)),"include_prereleases":s.get("include_prereleases","false")=="true","token_configured":bool(TOKEN),"webhook_configured":bool(WEBHOOK_SECRET),"workers":WORKERS}

@app.put("/api/settings")
async def update_settings(body:SettingsIn):
    c=db()
    if body.check_interval_minutes is not None: c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('check_interval_minutes',?)",(str(max(1,body.check_interval_minutes)),))
    if body.include_prereleases is not None: c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('include_prereleases',?)",(str(body.include_prereleases).lower(),))
    c.commit(); s=settings_map(c); c.close(); return {"ok":True,**s}

@app.get("/api/groups")
async def groups():
    c=db(); rows=c.execute("SELECT g.*,COUNT(r.id) repo_count FROM groups g LEFT JOIN repos r ON r.group_id=g.id GROUP BY g.id ORDER BY g.name COLLATE NOCASE").fetchall(); c.close(); return [rd(x) for x in rows]
@app.post("/api/groups")
async def add_group(body:GroupIn):
    n=body.name.strip()
    if not n: raise HTTPException(400,"Group name is required")
    c=db()
    try: c.execute("INSERT INTO groups(name,created_at) VALUES(?,?)",(n,now())); c.commit()
    except sqlite3.IntegrityError: c.close(); raise HTTPException(409,"Group already exists")
    row=c.execute("SELECT * FROM groups WHERE name=?",(n,)).fetchone(); c.close(); return rd(row)
@app.delete("/api/groups/{gid}")
async def delete_group(gid:int):
    c=db(); c.execute("DELETE FROM groups WHERE id=?",(gid,)); c.commit(); c.close(); return {"ok":True}

@app.put("/api/repos/{repo_id}/monitoring")
async def set_monitoring(repo_id:int, body:MonitoringIn):
    c=db()
    if not c.execute("SELECT 1 FROM repos WHERE id=?",(repo_id,)).fetchone(): c.close(); raise HTTPException(404,"Repository not found")
    c.execute("UPDATE repos SET monitoring=?,updated_at=? WHERE id=?",(1 if body.enabled else 0,now(),repo_id)); c.commit(); row=c.execute("SELECT monitoring FROM repos WHERE id=?",(repo_id,)).fetchone(); c.close()
    return {"ok":True,"monitoring":bool(row["monitoring"])}

@app.get("/api/repos")
async def list_repos():
    c=db(); rows=c.execute("""SELECT r.*,g.name group_name,
      (SELECT COUNT(*) FROM versions v WHERE v.repo_id=r.id) version_count,
      (SELECT COALESCE(SUM(f.size),0) FROM files f JOIN versions v ON v.id=f.version_id WHERE v.repo_id=r.id AND f.status='complete') archive_size,
      (SELECT COUNT(*) FROM files f JOIN versions v ON v.id=f.version_id WHERE v.repo_id=r.id AND f.status='failed') failed_files
      FROM repos r LEFT JOIN groups g ON g.id=r.group_id ORDER BY r.name COLLATE NOCASE""").fetchall(); c.close(); return [rd(x) for x in rows]

@app.post("/api/repos")
async def add_repo(body:RepoIn):
    try: full=parse_repo_url(body.url)
    except ValueError as e: raise HTTPException(400,str(e))
    c=db()
    if c.execute("SELECT id FROM repos WHERE full_name=?",(full,)).fetchone(): c.close(); raise HTTPException(409,"Repository is already tracked")
    t=now(); c.execute("INSERT INTO repos(full_name,url,name,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",(full,f"https://github.com/{full}",full.split("/",1)[1],"never",t,t))
    rid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.commit(); c.close()
    if body.archive_history: asyncio.create_task(import_repo_history(rid))
    else: asyncio.create_task(check_repo(rid))
    return {"id":rid,"full_name":full,"archive_history":body.archive_history}

@app.post("/api/users/import")
async def import_user_endpoint(body:UserIn):
    username=body.username.strip().lstrip("@")
    if not username: raise HTTPException(400,"GitHub username is required")
    try:
        results=await import_user(username,body.archive_all)
    except Exception as e:
        raise HTTPException(400,str(e))
    return {"username":username,"count":len(results),"repositories":results}

@app.delete("/api/repos/{repo_id}")
async def delete_repo(repo_id:int):
    c=db(); r=c.execute("SELECT * FROM repos WHERE id=?",(repo_id,)).fetchone()
    if not r: c.close(); raise HTTPException(404,"Repository not found")
    c.execute("DELETE FROM repos WHERE id=?",(repo_id,)); c.commit(); c.close(); return {"ok":True}

@app.put("/api/repos/{repo_id}/policy")
async def set_policy(repo_id:int,body:PolicyIn):
    c=db()
    if not c.execute("SELECT 1 FROM repos WHERE id=?",(repo_id,)).fetchone(): c.close(); raise HTTPException(404,"Repository not found")
    c.execute("UPDATE repos SET policy=?,updated_at=? WHERE id=?",(json.dumps(body.policy,separators=(",",":")),now(),repo_id)); c.commit(); c.close(); return {"ok":True,"policy":body.policy}

@app.put("/api/repos/{repo_id}/group/{gid}")
async def set_group(repo_id:int,gid:int):
    c=db()
    if not c.execute("SELECT 1 FROM repos WHERE id=?",(repo_id,)).fetchone(): c.close(); raise HTTPException(404,"Repository not found")
    if not c.execute("SELECT 1 FROM groups WHERE id=?",(gid,)).fetchone(): c.close(); raise HTTPException(404,"Group not found")
    c.execute("UPDATE repos SET group_id=?,updated_at=? WHERE id=?",(gid,now(),repo_id)); c.commit(); c.close(); return {"ok":True}

@app.delete("/api/repos/{repo_id}/group")
async def clear_group(repo_id:int):
    c=db(); c.execute("UPDATE repos SET group_id=NULL,updated_at=? WHERE id=?",(now(),repo_id)); c.commit(); c.close(); return {"ok":True}

@app.post("/api/repos/{repo_id}/check")
async def manual_check(repo_id:int): return await check_repo(repo_id)
@app.post("/api/check-all")
async def check_all():
    c=db(); ids=[r["id"] for r in c.execute("SELECT id FROM repos").fetchall()]; c.close(); results=[]
    for rid in ids:
        try: results.append(await check_repo(rid))
        except Exception as e: results.append({"status":"failed","error":str(e)})
    return results

@app.get("/api/repos/{repo_id}/versions")
async def versions(repo_id:int):
    c=db(); rows=c.execute("SELECT * FROM versions WHERE repo_id=? ORDER BY id DESC",(repo_id,)).fetchall(); c.close(); return [rd(x) for x in rows]

@app.get("/api/versions/{version_id}")
async def version_detail(version_id:int):
    c=db(); v=c.execute("SELECT v.*,r.full_name,r.name repo_name FROM versions v JOIN repos r ON r.id=v.repo_id WHERE v.id=?",(version_id,)).fetchone()
    if not v: c.close(); raise HTTPException(404,"Version not found")
    fs=c.execute("SELECT * FROM files WHERE version_id=? ORDER BY category,name",(version_id,)).fetchall(); c.close()
    return {"version":rd(v),"files":[rd(x) for x in fs]}

@app.get("/api/queue")
async def queue():
    c=db(); rows=c.execute("""SELECT q.*,f.name,f.category,f.size,f.status file_status,v.version,r.full_name
      FROM queue q JOIN files f ON f.id=q.file_id JOIN versions v ON v.id=f.version_id JOIN repos r ON r.id=v.repo_id
      ORDER BY q.id DESC LIMIT 500""").fetchall(); c.close(); return [rd(x) for x in rows]

@app.post("/api/queue/{qid}/retry")
async def retry_queue(qid:int):
    c=db(); q=c.execute("SELECT * FROM queue WHERE id=?",(qid,)).fetchone()
    if not q: c.close(); raise HTTPException(404,"Queue item not found")
    c.execute("UPDATE queue SET status='queued',error=NULL,finished_at=NULL WHERE id=?",(qid,)); c.execute("UPDATE files SET status='pending',error=NULL WHERE id=?",(q["file_id"],)); c.commit(); c.close(); queue_event.set(); return {"ok":True}

@app.get("/api/storage")
async def storage():
    c=db(); repo_count=c.execute("SELECT COUNT(*) n FROM repos").fetchone()["n"]; versions=c.execute("SELECT COUNT(*) n FROM versions").fetchone()["n"]
    files=c.execute("SELECT COUNT(*) n FROM files WHERE status='complete'").fetchone()["n"]; size=c.execute("SELECT COALESCE(SUM(size),0) n FROM files WHERE status='complete'").fetchone()["n"]; c.close()
    st=os.statvfs(DATA_DIR); return {"archive_bytes":size,"free_bytes":st.f_bavail*st.f_frsize,"total_bytes":st.f_blocks*st.f_frsize,"repos":repo_count,"versions":versions,"files":files}

@app.get("/api/storage/repos")
async def storage_repos():
    c=db(); rows=c.execute("""SELECT r.full_name,r.name,COALESCE(SUM(f.size),0) size,COUNT(DISTINCT v.id) versions
      FROM repos r LEFT JOIN versions v ON v.repo_id=r.id LEFT JOIN files f ON f.version_id=v.id AND f.status='complete'
      GROUP BY r.id ORDER BY size DESC""").fetchall(); c.close(); return [rd(x) for x in rows]

@app.get("/api/verify")
async def verify_all():
    c=db()
    fs=c.execute("""SELECT f.*,v.version,r.full_name
                   FROM files f
                   JOIN versions v ON v.id=f.version_id
                   JOIN repos r ON r.id=v.repo_id
                   WHERE f.status='complete'""").fetchall()
    c.close()
    results=[]
    ok=0
    for f in fs:
        base=repo_dirs(f["full_name"],f["version"])[0].resolve()
        p=(base/f["relative_path"]).resolve()
        safe=False
        try:
            p.relative_to(base)
            safe=True
        except ValueError:
            pass
        if not safe or not p.is_file() or p.is_symlink():
            c=db()
            c.execute("UPDATE files SET verify_status='failed',error=? WHERE id=?",
                      ("missing or invalid archived file",f["id"]))
            c.commit(); c.close()
            results.append({"id":f["id"],"ok":False,"error":"missing or invalid archived file"})
            continue
        h=hashlib.sha256()
        with p.open("rb") as x:
            for data in iter(lambda:x.read(1024*1024),b""): h.update(data)
        actual=h.hexdigest()
        if f["sha256"] and f["sha256"].startswith("sha256:"):
            good=actual.lower()==f["sha256"].split(":",1)[1].lower()
            verify_status="verified" if good else "failed"
        else:
            good=True
            verify_status="verified-local"
        c=db()
        c.execute("UPDATE files SET verify_status=?,error=NULL WHERE id=?",
                  (verify_status if good else "failed",f["id"]))
        c.commit(); c.close()
        results.append({"id":f["id"],"ok":good,"sha256":actual})
        ok+=1 if good else 0
    return {"checked":len(results),"ok":ok,"failed":len(results)-ok,"results":results}

@app.get("/api/search")
async def search(q:str=""):
    q="%"+q.strip()+"%"
    c=db(); rows=c.execute("""SELECT r.id repo_id,r.full_name,v.id version_id,v.version,f.id file_id,f.name,f.category
      FROM repos r LEFT JOIN versions v ON v.repo_id=r.id LEFT JOIN files f ON f.version_id=v.id
      WHERE r.full_name LIKE ? OR v.version LIKE ? OR f.name LIKE ? ORDER BY r.full_name,v.id DESC LIMIT 500""",(q,q,q)).fetchall(); c.close(); return [rd(x) for x in rows]

@app.get("/api/archive/tree")
async def archive_tree():
    def walk(p):
        out=[]
        if not p.exists(): return out
        for x in sorted(p.iterdir(),key=lambda z:(not z.is_dir(),z.name.lower())):
            out.append({"name":x.name,"path":str(x.relative_to(DATA_DIR)),"directory":x.is_dir(),"size":x.stat().st_size if x.is_file() else None})
        return out
    return {"root":"repos","entries":walk(REPOS_DIR)}

@app.get("/api/archive/list")
async def archive_list(path:str=""):
    target=(DATA_DIR/path).resolve()
    try:
        target.relative_to(REPOS_DIR.resolve())
    except ValueError:
        raise HTTPException(400,"Invalid archive path")
    if not target.is_dir(): raise HTTPException(400,"Invalid archive path")
    return {"path":str(target.relative_to(DATA_DIR)),"entries":[{"name":x.name,"path":str(x.relative_to(DATA_DIR)),"directory":x.is_dir(),"size":x.stat().st_size if x.is_file() else None} for x in sorted(target.iterdir(),key=lambda z:(not z.is_dir(),z.name.lower()))]}

@app.get("/download/{file_id}")
async def download(file_id:int):
    f=await file_repo(file_id); base=repo_dirs(f["full_name"],f["version"])[0]; p=(base/f["relative_path"]).resolve()
    if not str(p).startswith(str(base.resolve())+os.sep) or not p.is_file(): raise HTTPException(404,"Archived file is not available")
    return FileResponse(p,filename=p.name)

@app.post("/api/webhook")
async def webhook(request:Request):
    body=await request.body()
    if WEBHOOK_SECRET:
        sig=request.headers.get("x-hub-signature-256","")
        expected="sha256="+hmac.new(WEBHOOK_SECRET.encode(),body,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig,expected): raise HTTPException(401,"Invalid webhook signature")
    try: payload=json.loads(body)
    except Exception: raise HTTPException(400,"Invalid JSON")
    event=request.headers.get("x-github-event","").lower()
    if event not in ("release","create","ping",""):
        return {"ok":True,"queued":False,"event":event}
    if event=="create" and payload.get("ref_type")!="tag":
        return {"ok":True,"queued":False,"event":event}
    repo=payload.get("repository",{}).get("full_name")
    if not repo: return {"ok":True,"queued":False}
    c=db(); r=c.execute("SELECT id,monitoring FROM repos WHERE full_name=?",(repo,)).fetchone(); c.close()
    if r and r["monitoring"]: asyncio.create_task(check_repo(r["id"])); return {"ok":True,"queued":True,"repository":repo}
    if r: return {"ok":True,"queued":False,"repository":repo,"monitoring":False}
    return {"ok":True,"queued":False,"repository":repo}

@app.get("/api/activity")
async def activity():
    c=db(); rows=c.execute("""SELECT 'version' type,v.updated_at timestamp,r.full_name,v.version,v.status,v.error
      FROM versions v JOIN repos r ON r.id=v.repo_id
      UNION ALL SELECT 'file' type,f.downloaded_at timestamp,r.full_name,f.name,f.status,f.error
      FROM files f JOIN versions v ON v.id=f.version_id JOIN repos r ON r.id=v.repo_id
      WHERE f.downloaded_at IS NOT NULL ORDER BY timestamp DESC LIMIT 100""").fetchall(); c.close(); return [rd(x) for x in rows]
