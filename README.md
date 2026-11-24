# FMHY Index Drift & GitHub Activity Checker (`fmhy-drift`)

Automated drift auditing for the [FMHY](https://fmhy.net/) index:

1. **Strict Proxy-Only Site Liveness**: Checks every site and mirror in the FMHY index through an HTTP/HTTPS/SOCKS5 proxy (`PROXY_URL`).
   - **Never scans from a GitHub Actions runner or direct host IP**: If `PROXY_URL` is unset, both the GitHub Actions workflow and CLI refuse to scan sites. Before scanning, a preflight check verifies that proxy egress works and does **not** match the host/runner public IP, recording only a truncated SHA-256 fingerprint (never leaking proxy credentials or raw proxy IPs into logs or Discord).
   - Distinguishes genuinely **dead** sites (`404`, `410`, `451` DMCA, `5xx`, Cloudflare `520–530` origin-down, DNS/connection failures), **parked/for-sale** domains, **TLS/SSL certificate** failures, and **WAF/rate-limited** (`403`/`429` Cloudflare challenges) responses.
2. **Automatic Linked GitHub Discovery**:
   - Extracts explicit `[GitHub](https://github.com/owner/repo)` and `*.github.io` repositories from FMHY markdown.
   - Automatically inspects live site homepages for linked `github.com/<owner>/<repo>` repositories (filtering out docs/theme footers like VitePress, MkDocs, Astro, Docusaurus) and highlights newly discovered repos not yet linked in FMHY.
3. **3-Tier GitHub Inactivity & Archival Warnings**:
   - Uses batched GitHub GraphQL queries (50 repositories per request via `GITHUB_TOKEN`, with automatic REST fallback for renamed `301` or deleted `404`/`451` repos) based on real commit/push/release timestamps (ignoring star-triggered `updated_at` timestamps).
   - Default thresholds (configurable via CLI flags, GitHub variables, or `workflow_dispatch` inputs):
     - 🟡 **Tier 1 — Notice**: `>= 90` days (~3 months) without activity
     - 🟠 **Tier 2 — Warning**: `>= 180` days (~6 months) without activity
     - 🔴 **Tier 3 — Critical**: `>= 365` days (1 year) without activity
     - 📦 **Archived / Disabled** & 💀 **Missing (`404`) / DMCA (`451`)** flagged dedicatedly
4. **Discord Webhook + Markdown & JSON Reports**:
   - Generates `fmhy-drift-report.md` and `fmhy-drift-report.json` (uploaded as workflow artifacts and written to GitHub Step Summary).
   - Optionally posts a color-coded summary Embed + attaches `fmhy-drift-report.md` directly to a Discord channel via `DISCORD_WEBHOOK_URL`.

---

## GitHub Actions & Discord Webhook Setup (Zero Local Install)

The workflow in [`.github/workflows/drift-check.yml`](.github/workflows/drift-check.yml) runs every Monday at `06:00 UTC` and supports manual **Run workflow** (`workflow_dispatch`) triggers with custom thresholds and filters.

Configure the following in **Settings → Secrets and variables → Actions**:

| Type | Name | Required | Description |
| :--- | :--- | :--- | :--- |
| **Secret** | `PROXY_URL` | **Required to run** | Proxy URL (`socks5h://user:pass@host:port` or `http://user:pass@host:port`). If unset, the workflow logs a notice and exits without scanning. |
| **Secret** | `DISCORD_WEBHOOK_URL` | Optional | Discord channel webhook URL. Receives a rich summary embed + attached `fmhy-drift-report.md`. |
| **Built-in** | `GITHUB_TOKEN` | Automatic | Provided automatically by GitHub Actions (5,000 GraphQL points/hr, checking 50 repos per query). |
| **Variable** | `WARN_DAYS_1` | Optional | Override default Tier 1 Notice threshold (`90`). |
| **Variable** | `WARN_DAYS_2` | Optional | Override default Tier 2 Warning threshold (`180`). |
| **Variable** | `WARN_DAYS_3` | Optional | Override default Tier 3 Critical threshold (`365`). |

---

## Optional Local CLI Usage

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/melo-foundation/fmhy-drift.git
cd fmhy-drift
uv sync --extra dev
```

Run against the live FMHY index (`https://api.fmhy.net/single-page`):

```bash
PROXY_URL="socks5h://username:password@proxy.example.com:1080" \
GITHUB_TOKEN="ghp_your_token_here" \
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/ID/TOKEN" \
uv run fmhy-drift \
  --warn-days-1 90 \
  --warn-days-2 180 \
  --warn-days-3 365 \
  --markdown-out reports/fmhy-drift-report.md \
  --json-out reports/fmhy-drift-report.json
```

Run tests:

```bash
uv run pytest -v
```
