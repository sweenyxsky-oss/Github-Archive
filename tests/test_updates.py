import os
import tempfile
import unittest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="github-archive-test-"))
os.environ.setdefault("DOWNLOAD_WORKERS", "1")

from fastapi.testclient import TestClient
import app.main as main


class UpdateTests(unittest.TestCase):
    def setUp(self):
        c = main.db()
        for t in ("queue", "files", "versions", "repos"):
            c.execute(f"DELETE FROM {t}")
        n = main.now()
        c.execute("INSERT INTO repos(full_name,url,name,status,created_at,updated_at) VALUES('o/r','https://github.com/o/r','r','complete',?,?)", (n, n))
        rid = c.execute("SELECT id FROM repos").fetchone()["id"]
        self.paths = []
        for i in range(1, 5):
            c.execute("INSERT INTO versions(repo_id,version,tag_name,kind,published_at,status,created_at,updated_at) VALUES(?,?,?,?,?,'complete',?,?)",
                      (rid, f"v{i}", f"v{i}", "release", f"2026-0{i}-01T00:00:00Z", n, n))
            vid = c.execute("SELECT id FROM versions WHERE tag_name=?", (f"v{i}",)).fetchone()["id"]
            base = main.repo_dirs("o/r", f"v{i}")[0]
            (base / "release").mkdir(parents=True, exist_ok=True)
            (base / "release" / "a.bin").write_bytes(b"x" * 100)
            self.paths.append(base)
            c.execute("INSERT INTO files(version_id,category,name,relative_path,source_url,status,size) VALUES(?,'release','a.bin','release/a.bin','u','complete',100)", (vid,))
            fid = c.execute("SELECT id FROM files WHERE version_id=?", (vid,)).fetchone()["id"]
            c.execute("INSERT INTO queue(file_id,status,queued_at) VALUES(?,?,?)", (fid, "done" if i < 4 else "queued", n))
        c.commit(); c.close()

    def test_purge_keeps_last_two_and_queue_view(self):
        with TestClient(main.app) as client:
            self.assertEqual(client.get("/api/queue?view=active").json()["total"], 1)
            self.assertEqual(client.get("/api/queue").json()["total"], 4)
            self.assertGreaterEqual(client.get("/api/queue/history").json()["total"], 3)
            r = client.post("/api/releases/purge", json={"keep": 2}).json()
            self.assertEqual(r["purged_versions"], 2)
            self.assertFalse(self.paths[0].exists())
            self.assertFalse(self.paths[1].exists())
            self.assertTrue(self.paths[2].exists())
            self.assertTrue(self.paths[3].exists())
            self.assertEqual(client.get("/api/repos/latest").json()["items"][0]["full_name"], "o/r")
            self.assertIn("created_at", client.get("/api/archive/list?path=repos").json()["entries"][0])
