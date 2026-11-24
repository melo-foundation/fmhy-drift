# fmhy-drift

Checks the [FMHY](https://fmhy.net) index (`api.fmhy.net/single-page`) for dead sites/DNS and dead, archived, inactive, or vulnerable repositories across GitHub, GitLab, Codeberg/Forgejo/Gitea, SourceHut (`sr.ht`), Bitbucket, Pagure, and SourceForge.

- **Triage & Discord Report UI**: [melo-foundation.github.io/fmhy-drift](https://melo-foundation.github.io/fmhy-drift/) — filter by forge, Dead (`404`/`451`/disabled), Moved/New Repo (including author's active similar-named repos), Severe Vulns (`CRITICAL`/`HIGH`), Dead/Parked DNS (with Wayback + Certificate Transparency last-active dates), or custom `> X` days inactive; move handled batches into local **Done / Pending**; send curated reports to Discord; and sync false positives to [`whitelist.json`](whitelist.json).
- **Multi-forge & OSV check**: Queries GitHub (batched GraphQL), GitLab (`gitlab.com` + self-hosted FOSS instances), Codeberg/Forgejo/Gitea, SourceHut, Bitbucket, Pagure, and SourceForge; scans archived/dead repos and owner/contributor profiles for replacement repos; and checks stale/archived repos + root manifests against [OSV.dev](https://osv.dev) for `HIGH`/`CRITICAL` advisories.
- **Site liveness & DNS check**: Direct HTTP site scraping only runs when `PROXY_URL` is set (verifying egress IP differs from the runner IP); otherwise, a proxy-free Cloudflare + Google DoH check flags `NXDOMAIN`, bogon/sinkholed, and parked domains and enriches last-active dates via Wayback CDX + CT logs (`crt.sh`) without contacting target hosts.

## GitHub Actions

Runs weekly on Mondays (`06:00 UTC`), on code pushes to `main`, and via `workflow_dispatch`.

| Name | Type | Purpose |
| :--- | :--- | :--- |
| `PROXY_URL` | Secret | HTTP/SOCKS5 proxy (`socks5h://user:pass@host:port`). Required for site checks; if unset, only GitHub checks run. |
| `DISCORD_WEBHOOK_URL` | Secret | Optional automatic summary ping on run completion. |
| `WARN_DAYS_1` / `2` / `3` | Variable | Inactivity thresholds in days (defaults: `90`, `180`, `365`). |

## Local Usage

```bash
uv sync
GITHUB_TOKEN="ghp_..." uv run fmhy-drift --github-only
PROXY_URL="socks5h://user:pass@host:1080" GITHUB_TOKEN="ghp_..." uv run fmhy-drift
```
