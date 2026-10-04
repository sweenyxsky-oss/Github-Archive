# GitHub Archive

A self-hosted GitHub release/tag archiver designed for TrueNAS SCALE.

## What it does

For every tracked repository the service periodically checks GitHub:

1. If a GitHub Release exists, it uses the newest non-draft Release.
2. If there are no usable Releases, it falls back to the newest Git tag.
3. A new version is archived only once.
4. Interrupted downloads are kept as `.part` files and resumed on the next attempt.
5. Every archived version contains:
   - `release/` — all Release assets
   - `source/` — source archive for the exact release/tag
   - `repository-current/` — source archive of the repository default branch at discovery time
6. Older versions are never deleted automatically.
7. The web dashboard provides repository status, version history, GitHub links, and direct local downloads.

## Storage layout

The TrueNAS dataset mounted at `/data` becomes:

```text
/data/
  github_archive.sqlite3
  repos/
    owner/
      repository/
        version/
          release/
          source/
          repository-current/
```

## TrueNAS SCALE

Recommended dataset:

```text
/mnt/dataPool/GitHubArchive
```

The included `truenas.yaml` maps that dataset to `/data` and exposes the dashboard on port `8088`.

In the TrueNAS Custom App YAML editor, paste `truenas.yaml`. Change the host path if your pool/dataset name differs.

### GitHub token

A token is optional for public repositories, but recommended for larger collections because authenticated GitHub API requests have higher rate limits.

Set `GITHUB_TOKEN` in the TrueNAS application environment. Do not commit a real token to this repository.

### Settings

- `CHECK_INTERVAL_MINUTES`: default 360 (6 hours)
- `INCLUDE_PRERELEASES`: default false
- `MAX_DOWNLOAD_RETRIES`: default 4
- `TZ`: default Asia/Riyadh

## Local Docker

```bash
docker compose up -d --build
```

Then open:

```text
http://localhost:8088
```

## GHCR image

The included GitHub Actions workflow publishes:

```text
ghcr.io/danger-mind/github-archive:latest
```

It also publishes Git tags such as `v1.1.0`.

## Important behavior

A Release is preferred over tags. Therefore, if a project has Releases, ordinary tags are not treated as newer versions. For tag-only projects, the latest GitHub tag is used.

The current repository snapshot is downloaded separately for every discovered version, so the archive records what the default branch looked like when that version was detected.

The database is the source of truth for download state. A final file is only marked complete after the download finishes and, where GitHub supplies a SHA-256 digest, the digest is verified.
