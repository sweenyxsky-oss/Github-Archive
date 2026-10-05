import os
import tempfile
import unittest

from fastapi.testclient import TestClient

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="github-archive-test-")
os.environ["DOWNLOAD_WORKERS"] = "1"

import app.main as main


class CoreTests(unittest.TestCase):
    def test_repo_url_validation(self):
        self.assertEqual(main.parse_repo_url("https://github.com/octocat/Hello-World"), "octocat/Hello-World")
        self.assertEqual(main.parse_repo_url("https://github.com/octocat/Hello-World.git"), "octocat/Hello-World")
        for value in ("https://github.com/octocat/Hello-World/issues", "https://example.com/octocat/Hello-World", "github.com/octocat/Hello-World"):
            with self.assertRaises(ValueError):
                main.parse_repo_url(value)

    def test_frontend_static_and_empty_repos_api(self):
        with TestClient(main.app) as client:
            static = client.get("/static/app.js")
            self.assertEqual(static.status_code, 200)
            self.assertIn("async function init()", static.text)
            repos = client.get("/api/repos")
            self.assertEqual(repos.status_code, 200)
            self.assertEqual(repos.json(), [])

    def test_safe_name(self):
        self.assertEqual(main.safe_name("hello world"), "hello_world")
        self.assertEqual(main.safe_name("a/b:c"), "a_b_c")

    def test_version_paths_do_not_collide(self):
        a=main.repo_dirs("octocat/test","release/foo")[0]
        b=main.repo_dirs("octocat/test","release_foo")[0]
        self.assertNotEqual(a,b)

    def test_changed_tag_sha_invalidates_version(self):
        c = main.db()
        t = main.now()
        c.execute("INSERT INTO repos(full_name,url,name,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                  ("octocat/mutable", "https://github.com/octocat/mutable", "mutable", "queued", t, t))
        rid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        repo = c.execute("SELECT * FROM repos WHERE id=?", (rid,)).fetchone()
        v = main.upsert_version(c, repo, {"version":"v1","tag_name":"v1","kind":"tag","published_at":t,"html_url":"https://github.com/octocat/mutable/releases/tag/v1","sha":"aaa"})
        c.execute("UPDATE versions SET status='complete' WHERE id=?", (v["id"],))
        c.commit()
        v2 = main.upsert_version(c, repo, {"version":"v1","tag_name":"v1","kind":"tag","published_at":t,"html_url":"https://github.com/octocat/mutable/releases/tag/v1","sha":"bbb"})
        c.commit()
        self.assertEqual(v2["target_sha"], "bbb")
        self.assertEqual(v2["status"], "pending")
        c.close()

    def test_queue_claim_is_atomic_and_uses_file_id(self):
        c = main.db()
        t = main.now()
        c.execute(
            "INSERT INTO repos(full_name,url,name,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("octocat/test", "https://github.com/octocat/test", "test", "queued", t, t),
        )
        rid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute(
            "INSERT INTO versions(repo_id,version,tag_name,kind,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (rid, "v1", "v1", "tag", "queued", t, t),
        )
        vid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute(
            "INSERT INTO files(version_id,category,name,relative_path,source_url,status) VALUES(?,?,?,?,?,?)",
            (vid, "source", "x.zip", "source/x.zip", "https://example.invalid/x.zip", "pending"),
        )
        fid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO queue(file_id,status,queued_at) VALUES(?,?,?)", (fid, "queued", t))
        qid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
        c.close()

        item = main.claim_next_queue_item()
        self.assertIsNotNone(item)
        self.assertEqual(item["queue_id"], qid)
        self.assertEqual(item["file_id"], fid)
        self.assertEqual(item["file_status"], "pending")
        self.assertIsNone(main.claim_next_queue_item())

        c = main.db()
        self.assertEqual(c.execute("SELECT status FROM queue WHERE id=?", (qid,)).fetchone()["status"], "running")
        self.assertEqual(c.execute("SELECT status FROM files WHERE id=?", (fid,)).fetchone()["status"], "downloading")
        c.close()

        main.recover_interrupted_queue()
        c = main.db()
        self.assertEqual(c.execute("SELECT status FROM queue WHERE id=?", (qid,)).fetchone()["status"], "queued")
        self.assertEqual(c.execute("SELECT status FROM files WHERE id=?", (fid,)).fetchone()["status"], "pending")
        c.close()


if __name__ == "__main__":
    unittest.main()
