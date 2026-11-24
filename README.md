# fmhy-drift

Checks the [FMHY](https://fmhy.net) index (`api.fmhy.net/single-page`) for dead sites and inactive or archived GitHub repositories.

- **GitHub check**: Queries linked `github.com` repos (plus repos inferred from `*.github.io`, `raw.githubusercontent.com`, `jsdelivr`, `githack`, and `statically` URLs) via batched GraphQL using `pushedAt`/commit/release dates. Flags repos at `90` / `180` / `365` days of inactivity, plus archived and 404/451 repos.
- **Site liveness check**: Only runs when `PROXY_URL` is set, and verifies egress IP differs from the host/runner IP before scanning. Also scrapes live homepages for unlinked `github.com` repos.

## GitHub Actions

Runs weekly on Mondays (`06:00 UTC`), on push to `main`, and via `workflow_dispatch`.

Optional repository secrets & variables (**Settings → Secrets and variables → Actions**):

| Name | Type | Purpose |
| :--- | :--- | :--- |
| `PROXY_URL` | Secret | HTTP/SOCKS5 proxy (`socks5h://user:pass@host:port`). Required for site checks; if unset, only GitHub checks run. |
| `DISCORD_WEBHOOK_URL` | Secret | Posts summary embed and attaches `fmhy-drift-report.md`. |
| `WARN_DAYS_1` / `2` / `3` | Variable | Inactivity thresholds in days (defaults: `90`, `180`, `365`). |

## Local Usage

```bash
uv sync
GITHUB_TOKEN="ghp_..." uv run fmhy-drift --github-only
PROXY_URL="socks5h://user:pass@host:1080" GITHUB_TOKEN="ghp_..." uv run fmhy-drift
```
