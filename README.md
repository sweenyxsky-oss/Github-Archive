# GitHub Archive

A self-hosted GitHub release/tag archiver designed for TrueNAS SCALE.

## v2 features

- Automatic release/tag detection with **Release + tag fallback**, **Tags only**, or **Both releases and tags** modes.
- Persistent SQLite-backed download queue with configurable workers.
- Streaming downloads with resumable `.part` files and retries.
- SHA-256 verification and GitHub digest verification when supplied.
- Per-version `metadata.json` records repository, tag, commit, branch, release and asset metadata.
- Release assets, exact tag/release source and default-branch current source.
- Optional GitHub Actions artifact archiving.
- Optional commit snapshot archiving.
- Repository archive policies configurable from the dashboard.
- Repository groups/categories.
- Storage dashboard with free/used capacity and largest repositories.
- Integrity page with **Verify Everything**.
- Archive browser and direct local downloads.
- **What's New?** activity dashboard.
- Search and repository health/status.
- Configurable checking interval from the web UI.
- GitHub webhook endpoint for near-real-time checks, with polling fallback.
- JSON REST API for automation and external integrations.
- Older versions are append-only and are never automatically deleted.

## Storage layout

```
/data/
  github_archive.sqlite3
  repos/
    owner/
      repository/
        version/
          metadata.json
          release/
          source/
          repository-current/
          actions/
          commits/
```

## TrueNAS SCALE

Recommended dataset:

`/mnt/dataPool/GitHubArchive`

The included `truenas.yaml` maps that dataset to `/data` and exposes the dashboard on port **8088**.

Environment variables:

- `GITHUB_TOKEN` — optional for public repositories; recommended for larger collections.
- `CHECK_INTERVAL_MINUTES` — initial default, 360 minutes.
- `INCLUDE_PRERELEASES` — initial default, false.
- `MAX_DOWNLOAD_RETRIES` — default 4.
- `DOWNLOAD_WORKERS` — concurrent download workers, default 2.
- `WEBHOOK_SECRET` — optional HMAC secret for GitHub webhook verification.
- `TZ` — default Asia/Riyadh.

The interval and prerelease setting can also be changed in the dashboard after installation.

### GitHub webhook

Create a repository webhook pointing to:

`http://YOUR-TRUENAS-HOST:8088/api/webhook`

Use **application/json**. Recommended events are **Release** and **Create** (for tag creation). Set the same secret in `WEBHOOK_SECRET`. If the service is not reachable from GitHub, use the normal scheduled polling.

### Local Docker

```bash
docker compose up -d --build
```

Open `http://localhost:8088`.

### GHCR

The GitHub Actions workflow publishes:

`ghcr.io/sweenyxsky-oss/github-archive:latest`

and version tags such as `v2.0.0`.

## API

Examples:

- `GET /api/repos`
- `POST /api/check-all`
- `GET /api/repos/{id}/versions`
- `GET /api/queue`
- `POST /api/queue/{id}/retry`
- `GET /api/storage`
- `GET /api/storage/repos`
- `GET /api/verify`
- `GET /api/search?q=...`
- `GET /api/activity`
- `GET /api/groups`
- `GET /api/settings`
- `PUT /api/settings`
- `POST /api/webhook`

## Important behavior

A release is preferred in **Release + tag fallback** mode. **Tags only** ignores releases. **Both releases and tags** archives the newest usable release and the newest tag when they differ.

For every archived version, the service can preserve release assets, exact source and a snapshot of the default branch. Optional Actions artifacts and commit snapshots can be enabled per repository.

The database is the source of truth for download state. A file is marked complete only after its final download is present, its expected size matches when known, and SHA-256 verification succeeds. Interrupted downloads remain as `.part` files and resume automatically.
