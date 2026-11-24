# fmhy-drift

Checks the [FMHY](https://fmhy.net) index (`api.fmhy.net/single-page`) for dead sites and dead, archived, or inactive GitHub repositories.

- **Triage & Discord Report UI**: [melo-foundation.github.io/fmhy-drift](https://melo-foundation.github.io/fmhy-drift/) — filter by Dead (`404`/`451`/disabled), Archived (with auto-detected replacement repo links), Severe Vulns (`CRITICAL`/`HIGH`), Dead/Parked DNS, or custom `> X` days inactive; tick items to send a curated report to a Discord webhook; and save false positives to [`whitelist.json`](whitelist.json).
- **GitHub & OSV check**: Queries linked `github.com` repos (plus repos inferred from `*.github.io`, `raw.githubusercontent.com`, `jsdelivr`, `githack`, and `statically` URLs) via batched GraphQL, scans archived repo descriptions/READMEs for successor links, and checks stale/archived repos + root manifests against [OSV.dev](https://osv.dev) for unpatched `HIGH`/`CRITICAL` advisories.
- **Site liveness & DNS check**: Direct HTTP site scraping only runs when `PROXY_URL` is set (verifying egress IP differs from the runner IP); otherwise, a proxy-free Cloudflare + Google DoH check flags `NXDOMAIN`, sinkholed, and parked domains without contacting target hosts.

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
