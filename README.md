# fmhy-drift

Checks the [FMHY](https://fmhy.net) index (`api.fmhy.net/single-page`) for dead sites/DNS and dead, archived, inactive, or vulnerable repositories across GitHub, GitLab, Codeberg/Forgejo/Gitea, SourceHut (`sr.ht`), Bitbucket, Pagure, and SourceForge.

- **Triage & Discord Report UI**: [melo-foundation.github.io/fmhy-drift](https://melo-foundation.github.io/fmhy-drift/) — filter by forge, Dead (`404`/`451`/disabled), Moved/New Repo (including author's active similar-named repos), Severe Vulns (`CRITICAL`/`HIGH`), Dead/Parked DNS (with Wayback + Certificate Transparency last-active dates), or custom `> X` days inactive; move handled batches into local **Done / Pending**; send curated reports to Discord; and sync false positives to [`whitelist.json`](whitelist.json).
- **Multi-forge & OSV check**: Queries GitHub (batched GraphQL), GitLab (`gitlab.com` + self-hosted FOSS instances), Codeberg/Forgejo/Gitea, SourceHut, Bitbucket, Pagure, and SourceForge; scans archived/dead repos and owner/contributor profiles for replacement repos; and checks stale/archived repos + root manifests against [OSV.dev](https://osv.dev) for `HIGH`/`CRITICAL` advisories.
- **Site signals**: Flags malicious URLs (Google Safe Browsing, URLhaus, ThreatFox, OpenPhish), re-registered / drop-caught / expired domains (RDAP), parked DNS, removed or stale store listings, lapsed certificates (CT), dead or moved sites in Wayback / Common Crawl, and sites confirmed down by urlscan.io / Cloudflare, querying only those third-party services.
- **Site liveness & DNS check**: Direct HTTP site scraping only runs when `PROXY_URL` is set (verifying egress IP differs from the runner IP); otherwise, a proxy-free Cloudflare + Google DoH check flags `NXDOMAIN`, bogon/sinkholed, and parked domains and enriches last-active dates via Wayback CDX + CT logs (`crt.sh`) without contacting target hosts.

## GitHub Actions

Runs weekly on Mondays (`06:00 UTC`), on code pushes to `main`, and via `workflow_dispatch`.

| Name | Type | Purpose |
| :--- | :--- | :--- |
| `PROXY_URL` | Secret | HTTP/SOCKS5 proxy (`socks5h://user:pass@host:port`). Required for site checks; if unset, only GitHub checks run. |
| `DISCORD_WEBHOOK_URL` | Secret | Optional automatic summary ping on run completion. |
| `WARN_DAYS_1` / `2` / `3` | Variable | Inactivity thresholds in days (defaults: `90`, `180`, `365`). |
| `STORE_WARN_DAYS_1` / `2` / `3` | Variable | Store / registry listing age thresholds in days (defaults: `365`, `730`, `1095`). |
| `HOLD_DAYS` | Variable | Days a broken site stays in Holding before it shows in Inbox (default: `30`). |
| `SIGNAL_BUDGET_MINUTES` | Variable | Site-signal time budget (default: `30`; `5` for incremental `only_alt_forges` runs). |

### Site-signal secrets

Add each as a repository secret under **Settings → Secrets and variables → Actions**. All are optional.

| Secret | Where to get it | Skipped without it |
| :--- | :--- | :--- |
| `GOOGLE_SAFE_BROWSING_API_KEY` | Google Cloud Console → enable **Safe Browsing API** → **Credentials → API key** | Google Safe Browsing lookups |
| `ABUSECH_AUTH_KEY` | [auth.abuse.ch](https://auth.abuse.ch/) → **Auth-Key** | URLhaus and ThreatFox feeds |
| `URLSCAN_API_KEY` | [urlscan.io](https://urlscan.io/user/profile/) → **Settings & API** → **New API key** | urlscan.io scans of suspect sites |
| `CLOUDFLARE_API_TOKEN` | Cloudflare dashboard → **My Profile → API Tokens** → custom token with **Account → URL Scanner → Edit** | Cloudflare URL Scanner scans |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare dashboard → account home → **Account ID** | Cloudflare URL Scanner scans |
| `CERTSPOTTER_API_KEY` | [sslmate.com/account/api_credentials](https://sslmate.com/account/api_credentials) | Nothing; CT checks use at most 50 CertSpotter queries per run (90 with the key) |

`GITHUB_TOKEN` is provided by Actions; without it locally, FMHY listing dates and re-registration checks are skipped.

The workflow keeps `.cache/signal-cache.json` in the Actions cache. Delete the `signal-cache-*` entries under **Actions → Caches** to force every signal to be re-queried.

### Triage UI site folders

| Folder | URL | Contents |
| :--- | :--- | :--- |
| New | `/new/` | rows new or changed since the previous published report |
| To-do | `/todo/` | dead links on FMHY lines with other working links |
| Back Up | `/recovered/` | reported items no longer flagged |
| Holding | `/holding/` | broken sites down fewer than `HOLD_DAYS` days |
| Reported | `/reported/` | items you marked reported (stored in your browser) |
| Dead / Parked Sites | `/dead-sites/` | `dead`, `parked`, `tls_error` sites |
| Malicious | `/malicious/` | `malicious` |
| Hijack Risk | `/hijack-risk/` | `reregistered`, `dropcatch_registrar`, `domain_expired`, `domain_held` |
| Stores | `/stores/` | `store_removed`, `store_stale` |
| Suspect Sites | `/suspect/` | `cert_lapsed`, `archive_down`, `archive_moved`, `scan_down`, `scan_up` |

## Local Usage

```bash
uv sync
GITHUB_TOKEN="ghp_..." uv run fmhy-drift --github-only
PROXY_URL="socks5h://user:pass@host:1080" GITHUB_TOKEN="ghp_..." uv run fmhy-drift
GITHUB_TOKEN="ghp_..." uv run fmhy-drift --skip-without-proxy --signal-budget-minutes 10
```

| Flag | Env | Default |
| :--- | :--- | :--- |
| `--site-signals` / `--no-site-signals` | `SITE_SIGNALS` | on (ignored with `--github-only`) |
| `--signal-cache PATH` | | `.cache/signal-cache.json` |
| `--signal-budget-minutes N` | `SIGNAL_BUDGET_MINUTES` | `30`; `5` with `--only-alt-forges` |
| `--hold-days N` | `HOLD_DAYS` | `30` |
| `--store-warn-days-1` / `-2` / `-3` | `STORE_WARN_DAYS_1` / `2` / `3` | `365` / `730` / `1095` |

Site-signal API keys are read from the environment variables named in the secrets table above.
