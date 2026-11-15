import os
import tempfile
import unittest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="github-archive-audit-test-"))
os.environ.setdefault("DOWNLOAD_WORKERS", "1")

from fastapi.testclient import TestClient
import app.main as main


class AuditFixTests(unittest.TestCase):
    def setUp(self):
        self.original_admin_token = main.ADMIN_TOKEN
        self.original_webhook_secret = main.WEBHOOK_SECRET
        main.ADMIN_TOKEN = "audit-test-token"
        main.WEBHOOK_SECRET = ""
        c = main.db()
        for table in ("queue", "files", "versions", "repos"):
            c.execute(f"DELETE FROM {table}")
        c.execute("DELETE FROM groups")
        n = main.now()
        c.execute("INSERT INTO repos(full_name,url,name,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                  ("audit/repo", "https://github.com/audit/repo", "repo", "complete", n, n))
        self.repo_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO versions(repo_id,version,tag_name,kind,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                  (self.repo_id, "v1", "v1", "release", "failed", n, n))
        self.version_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.repo_dir = main.repo_dirs("audit/repo", "v1")[0]
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        file_path = self.repo_dir / "release" / "private.bin"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b"private archive content")
        c.execute("INSERT INTO files(version_id,category,name,relative_path,source_url,status,size) VALUES(?,?,?,?,?,?,?)",
                  (self.version_id, "release", "private.bin", "release/private.bin", "https://example.invalid/file", "failed", 23))
        self.file_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.archive_relative_path = self.repo_dir.relative_to(main.DATA_DIR).as_posix() + "/release/private.bin"
        c.execute("UPDATE repos SET monitoring=0 WHERE id=?", (self.repo_id,))
        c.execute("INSERT INTO queue(file_id,status,queued_at,error) VALUES(?,?,?,?)",
                  (self.file_id, "failed", n, "download failed"))
        c.commit()
        c.close()

    def tearDown(self):
        main.ADMIN_TOKEN = self.original_admin_token
        main.WEBHOOK_SECRET = self.original_webhook_secret

    def test_download_routes_require_admin_token(self):
        with TestClient(main.app) as client:
            self.assertEqual(client.get(f"/download/{self.file_id}").status_code, 401)
            self.assertEqual(client.get("/download/path/" + self.archive_relative_path).status_code, 401)
            by_header = client.get(f"/download/{self.file_id}", headers={"Authorization": "Bearer audit-test-token"})
            self.assertEqual(by_header.status_code, 200)
            self.assertEqual(by_header.content, b"private archive content")
            by_query = client.get(f"/download/{self.file_id}?token=audit-test-token")
            self.assertEqual(by_query.status_code, 200)
            by_path = client.get("/download/path/" + self.archive_relative_path + "?token=audit-test-token")
            self.assertEqual(by_path.status_code, 200)

    def test_webhook_requires_auth_when_no_webhook_secret(self):
        with TestClient(main.app) as client:
            payload = {"repository": {"full_name": "audit/repo"}}
            self.assertEqual(client.post("/api/webhook", json=payload).status_code, 401)
            accepted = client.post("/api/webhook", json=payload,
                                   headers={"Authorization": "Bearer audit-test-token"})
            self.assertEqual(accepted.status_code, 200)

    def test_failed_queue_view_returns_failed_records(self):
        with TestClient(main.app) as client:
            response = client.get("/api/queue?view=failed", headers={"Authorization": "Bearer audit-test-token"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["total"], 1)
            self.assertEqual(response.json()["items"][0]["file_status"], "failed")

    def test_queue_status_is_not_queued_without_active_queue_work(self):
        c = main.db()
        c.execute("UPDATE queue SET status='done' WHERE file_id=?", (self.file_id,))
        c.execute("UPDATE files SET status='complete' WHERE id=?", (self.file_id,))
        c.execute("UPDATE repos SET status='queued' WHERE id=?", (self.repo_id,))
        c.commit()
        c.close()
        import asyncio
        asyncio.run(main.refresh_version_statuses())
        asyncio.run(main.refresh_repo_statuses())
        row = main.db().execute("SELECT status FROM repos WHERE id=?", (self.repo_id,)).fetchone()
        self.assertEqual(row["status"], "complete")

    def test_naive_and_z_timestamps_parse_as_utc(self):
        naive = main.parse_iso_utc("2026-01-01T00:00:00")
        utc_z = main.parse_iso_utc("2026-01-01T00:00:00Z")
        self.assertEqual(naive.utcoffset().total_seconds(), 0)
        self.assertEqual(utc_z.utcoffset().total_seconds(), 0)
        self.assertIsNone(main.parse_iso_utc("not-a-timestamp"))

    def test_frontend_keeps_diagnostics_and_links_failed_files(self):
        js = (main.Path(__file__).parent.parent / "app" / "static" / "app.js").read_text()
        self.assertIn("diagnosticsVisible", js)
        self.assertIn("openFailedQueue()", js)
        self.assertIn("queueView='failed'", js)
        self.assertIn("prevWrap.scrollTop", js)


if __name__ == "__main__":
    unittest.main()
