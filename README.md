# FMHY Index Drift & GitHub Activity Checker (`fmhy-drift`)

Automated drift auditing for the [FMHY](https://fmhy.net/) index:

1. **3-Tier GitHub Inactivity & Archival Warnings (Works Standalone Without a Proxy)**:
   - Uses batched GitHub GraphQL queries (50 repositories per request via `GITHUB_TOKEN`, with automatic REST fallback for renamed `301` or deleted `404`/`451` repos) based on real commit/push/release timestamps (ignoring star-triggered `updated_at` timestamps).
   - Default thresholds (configurable via CLI flags, GitHub variables, or `workflow_dispatch` inputs):
     - 🟡 **Tier 1 — Notice**: `>= 90` days (~3 months) without activity
     - 🟠 **Tier 2 — Warning**: `>= 180` days (~6 months) without activity
     - 🔴 **Tier 3 — Critical**: `>= 365` days (1 year) without activity
     - 📦 **Archived / Disabled** & 💀 **Missing (`404`) / DMCA (`451`)** flagged dedicatedly
2. **Multi-Layer Linked GitHub Discovery & Heuristics**:
   - **Direct FMHY Links**: `[GitHub](https://github.com/owner/repo)` (including `/releases`, `/tree`, `/blob`, etc.).
   - **GitHub Pages Heuristics** *(works even in `--github-only` mode)*: Infers `owner/repo` from `https://<owner>.github.io/<repo>` and `owner/<owner>.github.io` from `https://<owner>.github.io/`.
   - **GitHub Raw & CDN Heuristics** *(works even in `--github-only` mode)*: Infers `owner/repo` from userscripts, filterlists, and assets hosted on `raw.githubusercontent.com`, `cdn.jsdelivr.net/gh/<owner>/<repo>@...`, `raw.githack.com`, `cdn.statically.io/gh/`, and `codeload.github.com`.
   - **Live Homepage Scraping** *(when `PROXY_URL` is configured)*: Scans `<a href>`, `<link>`, and SPA hydration JSON (Next.js/Astro/SvelteKit) on live sites for `github.com/<owner>/<repo>`, filtering out docs/theme footers (VitePress, MkDocs, Astro, Docusaurus) and matching against domain/title context.
3. **Optional Proxy-Only Site Liveness**:
   - Checks every site and mirror in the FMHY index strictly through an HTTP/HTTPS/SOCKS5 proxy (`PROXY_URL`).
   - **Never scans sites from a GitHub Actions runner IP**: If `PROXY_URL` is unset (or `--github-only` is enabled), site liveness checks are skipped automatically while GitHub repository checks still run. When `PROXY_URL` is set, an egress preflight check verifies the proxy IP does not match the runner IP and stores only a SHA-256 fingerprint.
4. **Discord Webhook + Markdown & JSON Reports**:
   - Generates `fmhy-drift-report.md` and `fmhy-drift-report.json` (uploaded as workflow artifacts and written to GitHub Step Summary).
   - Optionally posts a color-coded summary Embed + attaches `fmhy-drift-report.md` directly to a Discord channel via `DISCORD_WEBHOOK_URL`.

---

## GitHub Actions & Discord Webhook Setup (Zero Local Install)

The workflow in [`.github/workflows/drift-check.yml`](.github/workflows/drift-check.yml) runs out of the box using GitHub's built-in `GITHUB_TOKEN` (no proxy needed for GitHub checks!).

Configure optional secrets/variables in **Settings → Secrets and variables → Actions**:

| Type | Name | Required | Description |
| :--- | :--- | :--- | :--- |
| **Built-in** | `GITHUB_TOKEN` | Automatic | Provided automatically by GitHub Actions (5,000 GraphQL points/hr, checking 50 repos per query). |
| **Secret** | `DISCORD_WEBHOOK_URL` | Optional | Discord channel webhook URL. Receives a rich summary embed + attached `fmhy-drift-report.md`. |
| **Secret** | `PROXY_URL` | Optional (Required only for site liveness) | Proxy URL (`socks5h://user:pass@host:port` or `http://user:pass@host:port`). If unset, only GitHub repository checks run. |
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

Run GitHub repository checks only (no proxy required):

```bash
GITHUB_TOKEN="ghp_your_token_here" \
uv run fmhy-drift \
  --github-only \
  --warn-days-1 90 \
  --warn-days-2 180 \
  --warn-days-3 365 \
  --markdown-out reports/fmhy-drift-report.md \
  --json-out reports/fmhy-drift-report.json
```

Run full site liveness (via proxy) + GitHub discovery & staleness checks:

```bash
PROXY_URL="socks5h://username:password@proxy.example.com:1080" \
GITHUB_TOKEN="ghp_your_token_here" \
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/ID/TOKEN" \
uv run fmhy-drift \
  --markdown-out reports/fmhy-drift-report.md \
  --json-out reports/fmhy-drift-report.json
```
