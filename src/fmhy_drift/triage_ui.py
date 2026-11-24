"""Static HTML/JS Triage & Discord Report Builder UI generator (deployed to GitHub Pages)."""

from __future__ import annotations

TRIAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>FMHY Drift Triage</title>
  <style>
    :root {
      --bg: #0d1117;
      --panel: #161b22;
      --panel-alt: #1c2128;
      --border: #30363d;
      --text: #e6edf3;
      --muted: #8b949e;
      --accent: #58a6ff;
      --danger: #f85149;
      --warn: #d29922;
      --purple: #bc8cff;
      --ok: #3fb950;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      font-size: 14px;
      line-height: 1.45;
    }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    header {
      padding: 14px 20px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
    }
    header h1 { margin: 0; font-size: 17px; font-weight: 600; }
    .meta { color: var(--muted); font-size: 12px; }
    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 400px;
      min-height: calc(100vh - 56px);
    }
    @media (max-width: 1080px) {
      .layout { grid-template-columns: 1fr; }
    }
    .main-pane { padding: 16px 20px; min-width: 0; }
    .side-pane {
      border-left: 1px solid var(--border);
      background: var(--panel);
      padding: 16px;
      position: sticky;
      top: 0;
      height: 100vh;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    @media (max-width: 1080px) {
      .side-pane { position: static; height: auto; border-left: none; border-top: 1px solid var(--border); }
    }
    .tabs {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 12px;
    }
    .tab-btn {
      background: var(--panel);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px 10px;
      font-size: 13px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .tab-btn:hover { border-color: var(--muted); }
    .tab-btn.active {
      background: #1f6feb26;
      border-color: var(--accent);
      color: #fff;
      font-weight: 600;
    }
    .count-pill {
      background: #30363d;
      border-radius: 10px;
      padding: 1px 7px;
      font-size: 11px;
      color: var(--text);
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      margin-bottom: 12px;
      background: var(--panel);
      padding: 10px;
      border: 1px solid var(--border);
      border-radius: 6px;
    }
    input[type="text"], input[type="password"], select, textarea {
      background: var(--bg);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px 9px;
      font-size: 13px;
    }
    input[type="text"]:focus, input[type="password"]:focus, select:focus, textarea:focus {
      outline: none;
      border-color: var(--accent);
    }
    .search-input { flex: 1; min-width: 190px; }
    button {
      background: var(--panel-alt);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px 10px;
      font-size: 12.5px;
      cursor: pointer;
    }
    button:hover { border-color: var(--muted); }
    button.primary {
      background: #238636;
      border-color: #2ea043;
      color: #fff;
      font-weight: 600;
    }
    button.primary:hover { background: #2ea043; }
    button.discord-btn {
      background: #5865f2;
      border-color: #6d78f9;
      color: #fff;
      font-weight: 600;
    }
    button.discord-btn:hover { background: #4752c4; }
    .item-list {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .row {
      display: grid;
      grid-template-columns: 26px minmax(0, 1fr) auto;
      gap: 10px;
      align-items: start;
      padding: 10px 12px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 6px;
    }
    .row.checked {
      border-color: var(--accent);
      background: #1f6feb14;
    }
    .row input[type="checkbox"] {
      width: 16px;
      height: 16px;
      margin-top: 3px;
      cursor: pointer;
    }
    .row-title {
      font-weight: 600;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px;
    }
    .cat-tag {
      font-weight: 400;
      font-size: 12px;
      color: var(--muted);
    }
    .links-line {
      margin-top: 4px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      font-size: 12.5px;
    }
    .badge {
      display: inline-block;
      padding: 1px 7px;
      border-radius: 4px;
      font-size: 11.5px;
      font-weight: 600;
      border: 1px solid transparent;
    }
    .badge-dead { background: #f8514922; color: #ff7b72; border-color: #f8514955; }
    .badge-arch { background: #bc8cff22; color: #d2a8ff; border-color: #bc8cff55; }
    .badge-repl { background: #3fb95022; color: #56d364; border-color: #3fb95066; }
    .badge-warn { background: #d2992222; color: #e3b341; border-color: #d2992255; }
    .badge-web  { background: #58a6ff1a; color: #79c0ff; border-color: #58a6ff44; }
    .badge-pages { background: #388bfd26; color: #a5d6ff; border-color: #58a6ff66; }
    .note-input {
      margin-top: 6px;
      width: 100%;
      max-width: 440px;
      padding: 4px 7px;
      font-size: 12px;
    }
    .side-section {
      border: 1px solid var(--border);
      background: var(--bg);
      border-radius: 6px;
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .side-section h2 {
      margin: 0;
      font-size: 13.5px;
      font-weight: 600;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    textarea.preview {
      width: 100%;
      min-height: 220px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      resize: vertical;
    }
    .btn-row { display: flex; flex-wrap: wrap; gap: 6px; }
    .status-msg { font-size: 12px; color: var(--muted); min-height: 16px; }
    .pager {
      margin-top: 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      color: var(--muted);
      font-size: 12.5px;
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>FMHY Drift &amp; GitHub Staleness Triage</h1>
      <div class="meta" id="run-meta">Loading report...</div>
    </div>
    <div class="btn-row">
      <a href="./fmhy-drift-report.md" target="_blank"><button type="button">Raw .md</button></a>
      <a href="./fmhy-drift-report.json" target="_blank"><button type="button">Raw .json</button></a>
      <a id="repo-link" href="https://github.com/melo-foundation/fmhy-drift" target="_blank"><button type="button">GitHub Repo</button></a>
    </div>
  </header>

  <div class="layout">
    <main class="main-pane">
      <div class="tabs" id="tabs"></div>

      <div class="toolbar">
        <input type="text" id="search" class="search-input" placeholder="Filter title, owner/repo, domain, replacement..." />
        <select id="cat-filter">
          <option value="">All Categories</option>
        </select>
        <label style="display:inline-flex;align-items:center;gap:4px;cursor:pointer;font-size:12.5px;">
          <input type="checkbox" id="starred-only" /> ⭐ Starred
        </label>
        <label style="display:inline-flex;align-items:center;gap:4px;cursor:pointer;font-size:12.5px;">
          <input type="checkbox" id="has-web-only" /> Has Site/Pages
        </label>
        <button type="button" id="tick-page-btn">Tick Page</button>
        <button type="button" id="clear-ticks-btn">Clear All Ticks</button>
      </div>

      <div class="item-list" id="item-list"></div>
      <div class="pager" id="pager"></div>
    </main>

    <aside class="side-pane">
      <section class="side-section">
        <h2>
          <span>Discord Report Builder</span>
          <span class="count-pill" id="selected-count">0 ticked</span>
        </h2>
        <textarea id="discord-preview" class="preview" placeholder="Tick entries on the left to build a clean report for Discord..."></textarea>
        <div class="btn-row">
          <button type="button" id="copy-md-btn" class="primary">Copy Markdown</button>
          <button type="button" id="download-md-btn">Download .md</button>
        </div>
        <input type="password" id="webhook-url" placeholder="Discord Webhook URL (saved in browser only)" />
        <div class="btn-row">
          <button type="button" id="send-discord-btn" class="discord-btn">Send Ticked Report to Discord</button>
        </div>
        <div class="status-msg" id="discord-status"></div>
      </section>

      <section class="side-section">
        <h2>
          <span>Whitelist (`whitelist.json`)</span>
          <span class="count-pill" id="wl-count">0 items</span>
        </h2>
        <div class="meta">
          Whitelisted repos/URLs are hidden immediately here and skipped on future scans once saved to `whitelist.json`.
        </div>
        <input type="password" id="gh-pat" placeholder="Optional GitHub PAT (repo contents:write) for 1-click save" />
        <div class="btn-row">
          <button type="button" id="save-gh-btn" class="primary">Commit whitelist.json</button>
          <button type="button" id="copy-wl-btn">Copy JSON &amp; Edit on GitHub</button>
        </div>
        <div class="status-msg" id="wl-status"></div>
      </section>
    </aside>
  </div>

  <script>
    const REPO_SLUG = "melo-foundation/fmhy-drift";
    const PAGE_SIZE = 75;

    const TABS = [
      { id: "missing", label: "💀 Dead / 404 Repos" },
      { id: "archived_moved", label: "🔀 Archived (New Repo Found)" },
      { id: "archived", label: "📦 All Archived" },
      { id: "tier3_critical", label: "🔴 1y+ Inactive" },
      { id: "tier2_warning", label: "🟠 6m+ Inactive" },
      { id: "tier1_notice", label: "🟡 3m+ Inactive" },
      { id: "dead_sites", label: "🌐 Dead / Parked Sites" },
      { id: "whitelisted", label: "🛡️ Whitelisted" },
    ];

    const state = {
      items: [],
      activeTab: "missing",
      page: 0,
      selectedIds: new Set(JSON.parse(localStorage.getItem("fmhy_selected_ids") || "[]")),
      notes: JSON.parse(localStorage.getItem("fmhy_item_notes") || "{}"),
      whitelistRepos: new Set(JSON.parse(localStorage.getItem("fmhy_wl_repos") || "[]")),
      whitelistUrls: new Set(JSON.parse(localStorage.getItem("fmhy_wl_urls") || "[]")),
      whitelistNotes: {},
    };

    document.getElementById("webhook-url").value = localStorage.getItem("fmhy_discord_webhook") || "";
    document.getElementById("gh-pat").value = localStorage.getItem("fmhy_gh_pat") || "";

    function saveLocalState() {
      localStorage.setItem("fmhy_selected_ids", JSON.stringify([...state.selectedIds]));
      localStorage.setItem("fmhy_item_notes", JSON.stringify(state.notes));
      localStorage.setItem("fmhy_wl_repos", JSON.stringify([...state.whitelistRepos]));
      localStorage.setItem("fmhy_wl_urls", JSON.stringify([...state.whitelistUrls]));
    }

    function isPagesUrl(url) {
      try { return new URL(url).hostname.toLowerCase().endsWith(".github.io"); }
      catch { return false; }
    }

    function shortHost(url) {
      try {
        const u = new URL(url);
        const p = u.pathname && u.pathname !== "/" ? u.pathname.replace(/\/$/, "") : "";
        return u.hostname.replace(/^www\./, "") + (p.length <= 24 ? p : p.slice(0, 22) + "…");
      } catch { return url; }
    }

    function isWhitelisted(item) {
      if (item.kind === "repo") return state.whitelistRepos.has(item.wlKey);
      return state.whitelistUrls.has(item.wlKey);
    }

    async function loadData() {
      try {
        const wlUrls = [
          `https://raw.githubusercontent.com/${REPO_SLUG}/main/whitelist.json?t=${Date.now()}`,
          "./whitelist.json"
        ];
        for (const u of wlUrls) {
          try {
            const r = await fetch(u);
            if (r.ok) {
              const wl = await r.json();
              (wl.repos || []).forEach(x => state.whitelistRepos.add(String(x).toLowerCase()));
              (wl.urls || []).forEach(x => state.whitelistUrls.add(String(x)));
              state.whitelistNotes = wl.notes || {};
              break;
            }
          } catch {}
        }

        const resp = await fetch("./fmhy-drift-report.json");
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const report = await resp.json();
        ingestReport(report);
      } catch (err) {
        document.getElementById("run-meta").textContent = "Failed to load fmhy-drift-report.json: " + err.message;
      }
    }

    function ingestReport(report) {
      const dt = new Date(report.generated_at).toISOString().replace("T", " ").slice(0, 19) + " UTC";
      document.getElementById("run-meta").textContent =
        `Generated: ${dt} · Mode: ${report.mode} · Unique Repos: ${report.summary.unique_repos_checked} · Thresholds: ${report.thresholds.tier1_days}d / ${report.thresholds.tier2_days}d / ${report.thresholds.tier3_days}d`;

      const items = [];
      const cats = new Set();

      for (const entry of report.entries || []) {
        cats.add(entry.category);
        const catLabel = entry.subcategory ? `${entry.category} > ${entry.subcategory}` : entry.category;

        for (const rr of entry.github_repos || []) {
          if (rr.tier === "active") continue;
          const wlKey = rr.full_name.toLowerCase();
          const webUrls = entry.web_urls && entry.web_urls.length ? entry.web_urls : [];
          const mergedWeb = [...webUrls];
          if (rr.homepage_url && !mergedWeb.some(u => u.replace(/\/$/, "").toLowerCase() === rr.homepage_url.replace(/\/$/, "").toLowerCase())) {
            mergedWeb.push(rr.homepage_url);
          }
          items.push({
            id: `repo:${entry.entry_id}:${wlKey}`,
            kind: "repo",
            wlKey,
            title: entry.title,
            starred: entry.starred,
            category: entry.category,
            catLabel,
            entryId: entry.entry_id,
            tier: rr.tier,
            fullName: rr.full_name,
            htmlUrl: rr.html_url,
            inactiveDays: rr.inactive_days,
            reason: rr.reason,
            replacementUrl: rr.replacement_url || null,
            replacementStatus: rr.replacement_status || null,
            webUrls: mergedWeb,
          });
        }

        for (const sr of entry.sites || []) {
          if (!["dead", "parked", "tls_error"].includes(sr.status)) continue;
          items.push({
            id: `site:${entry.entry_id}:${sr.url}`,
            kind: "site",
            wlKey: sr.url,
            title: entry.title,
            starred: entry.starred,
            category: entry.category,
            catLabel,
            entryId: entry.entry_id,
            tier: "dead_sites",
            siteUrl: sr.url,
            siteStatus: sr.status,
            reason: sr.reason,
            webUrls: [sr.url],
          });
        }
      }

      // Sort: starred first, then replacement present, then most inactive days descending
      items.sort((a, b) => {
        if (a.starred !== b.starred) return a.starred ? -1 : 1;
        if (Boolean(a.replacementUrl) !== Boolean(b.replacementUrl)) return a.replacementUrl ? -1 : 1;
        return (b.inactiveDays || 0) - (a.inactiveDays || 0);
      });

      state.items = items;

      const catSelect = document.getElementById("cat-filter");
      [...cats].sort().forEach(c => {
        const opt = document.createElement("option");
        opt.value = c;
        opt.textContent = c;
        catSelect.appendChild(opt);
      });

      // Pick first non-empty tab automatically if default is empty
      if (itemsForTab("missing").length === 0 && itemsForTab("archived_moved").length > 0) {
        state.activeTab = "archived_moved";
      }
      renderAll();
    }

    function itemsForTab(tabId) {
      if (tabId === "whitelisted") {
        return state.items.filter(it => isWhitelisted(it));
      }
      return state.items.filter(it => {
        if (isWhitelisted(it)) return false;
        if (tabId === "archived_moved") return it.kind === "repo" && it.tier === "archived" && Boolean(it.replacementUrl);
        if (tabId === "dead_sites") return it.kind === "site";
        return it.kind === "repo" && it.tier === tabId;
      });
    }

    function filteredItems() {
      const q = document.getElementById("search").value.trim().toLowerCase();
      const cat = document.getElementById("cat-filter").value;
      const starredOnly = document.getElementById("starred-only").checked;
      const hasWebOnly = document.getElementById("has-web-only").checked;

      return itemsForTab(state.activeTab).filter(it => {
        if (cat && it.category !== cat) return false;
        if (starredOnly && !it.starred) return false;
        if (hasWebOnly && (!it.webUrls || it.webUrls.length === 0)) return false;
        if (!q) return true;
        const hay = `${it.title} ${it.catLabel} ${it.fullName || ""} ${it.siteUrl || ""} ${it.replacementUrl || ""} ${(it.webUrls || []).join(" ")}`.toLowerCase();
        return hay.includes(q);
      });
    }

    function renderTabs() {
      const container = document.getElementById("tabs");
      container.innerHTML = "";
      for (const t of TABS) {
        const count = itemsForTab(t.id).length;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "tab-btn" + (state.activeTab === t.id ? " active" : "");
        btn.innerHTML = `<span>${t.label}</span><span class="count-pill">${count}</span>`;
        btn.onclick = () => {
          state.activeTab = t.id;
          state.page = 0;
          renderAll();
        };
        container.appendChild(btn);
      }
    }

    function escapeHtml(s) {
      return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    function renderList() {
      const all = filteredItems();
      const totalPages = Math.max(1, Math.ceil(all.length / PAGE_SIZE));
      if (state.page >= totalPages) state.page = totalPages - 1;
      const slice = all.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE);

      const listEl = document.getElementById("item-list");
      listEl.innerHTML = "";

      if (slice.length === 0) {
        listEl.innerHTML = `<div class="meta" style="padding:18px;">No matching items in this tab.</div>`;
      }

      for (const it of slice) {
        const row = document.createElement("div");
        const checked = state.selectedIds.has(it.id);
        row.className = "row" + (checked ? " checked" : "");

        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = checked;
        cb.onchange = () => {
          if (cb.checked) state.selectedIds.add(it.id);
          else state.selectedIds.delete(it.id);
          saveLocalState();
          row.className = "row" + (cb.checked ? " checked" : "");
          renderDiscordPreview();
        };

        const body = document.createElement("div");
        const star = it.starred ? "⭐ " : "";
        let statusBadge = "";
        if (it.kind === "site") {
          statusBadge = `<span class="badge badge-dead">${escapeHtml(it.siteStatus)}: ${escapeHtml(it.reason)}</span>`;
        } else if (it.tier === "missing") {
          statusBadge = `<span class="badge badge-dead">${escapeHtml(it.reason)}</span>`;
        } else if (it.tier === "archived") {
          const daysTxt = it.inactiveDays != null ? ` · ${it.inactiveDays}d` : "";
          statusBadge = `<span class="badge badge-arch">Archived${daysTxt}</span>`;
        } else {
          statusBadge = `<span class="badge badge-warn">${it.inactiveDays}d inactive</span>`;
        }

        let linksHtml = "";
        if (it.kind === "repo") {
          linksHtml += `<a href="${escapeHtml(it.htmlUrl)}" target="_blank" rel="noopener"><strong>${escapeHtml(it.fullName)}</strong></a> ${statusBadge}`;
        } else {
          linksHtml += `<a href="${escapeHtml(it.siteUrl)}" target="_blank" rel="noopener"><strong>${escapeHtml(it.siteUrl)}</strong></a> ${statusBadge}`;
        }

        if (it.webUrls && it.webUrls.length > 0 && it.kind === "repo") {
          for (const wu of it.webUrls) {
            const isPg = isPagesUrl(wu);
            const cls = isPg ? "badge-pages" : "badge-web";
            const prefix = isPg ? "Pages:" : "Site:";
            linksHtml += ` <a class="badge ${cls}" href="${escapeHtml(wu)}" target="_blank" rel="noopener">${prefix} ${escapeHtml(shortHost(wu))}</a>`;
          }
        }

        if (it.replacementUrl) {
          const st = it.replacementStatus ? ` (${escapeHtml(it.replacementStatus)})` : "";
          linksHtml += ` <a class="badge badge-repl" href="${escapeHtml(it.replacementUrl)}" target="_blank" rel="noopener">↪ New Repo: ${escapeHtml(shortHost(it.replacementUrl))}${st}</a>`;
        }

        const noteVal = state.notes[it.id] || "";
        body.innerHTML = `
          <div class="row-title">
            <span>${star}${escapeHtml(it.title)}</span>
            <span class="cat-tag">${escapeHtml(it.catLabel)}</span>
          </div>
          <div class="links-line">${linksHtml}</div>
        `;

        const noteInput = document.createElement("input");
        noteInput.type = "text";
        noteInput.className = "note-input";
        noteInput.placeholder = "Optional note for Discord report (e.g. replace with new fork, site also 404)...";
        noteInput.value = noteVal;
        noteInput.oninput = () => {
          if (noteInput.value.trim()) state.notes[it.id] = noteInput.value;
          else delete state.notes[it.id];
          saveLocalState();
          if (state.selectedIds.has(it.id)) renderDiscordPreview();
        };
        body.appendChild(noteInput);

        const actions = document.createElement("div");
        const wlBtn = document.createElement("button");
        wlBtn.type = "button";
        const currentlyWl = isWhitelisted(it);
        wlBtn.textContent = currentlyWl ? "Unwhitelist" : "Whitelist";
        wlBtn.onclick = () => {
          const targetSet = it.kind === "repo" ? state.whitelistRepos : state.whitelistUrls;
          if (currentlyWl) targetSet.delete(it.wlKey);
          else {
            targetSet.add(it.wlKey);
            state.selectedIds.delete(it.id);
          }
          saveLocalState();
          renderAll();
        };
        actions.appendChild(wlBtn);

        row.appendChild(cb);
        row.appendChild(body);
        row.appendChild(actions);
        listEl.appendChild(row);
      }

      const pagerEl = document.getElementById("pager");
      pagerEl.innerHTML = `
        <span>Showing ${slice.length} of ${all.length} items (Page ${state.page + 1} / ${totalPages})</span>
        <div class="btn-row">
          <button type="button" id="prev-pg" ${state.page <= 0 ? "disabled" : ""}>Prev</button>
          <button type="button" id="next-pg" ${state.page + 1 >= totalPages ? "disabled" : ""}>Next</button>
        </div>
      `;
      document.getElementById("prev-pg").onclick = () => { if (state.page > 0) { state.page--; renderList(); } };
      document.getElementById("next-pg").onclick = () => { if (state.page + 1 < totalPages) { state.page++; renderList(); } };
    }

    function buildDiscordMarkdown() {
      const selected = state.items.filter(it => state.selectedIds.has(it.id));
      document.getElementById("selected-count").textContent = `${selected.length} ticked`;
      if (selected.length === 0) return "";

      const groups = [
        { title: "💀 Dead / Deleted / DMCA'd GitHub Repos", filter: it => it.kind === "repo" && it.tier === "missing" },
        { title: "🔀 Archived Repos With Replacement", filter: it => it.kind === "repo" && it.tier === "archived" && it.replacementUrl },
        { title: "📦 Archived GitHub Repos", filter: it => it.kind === "repo" && it.tier === "archived" && !it.replacementUrl },
        { title: "⏳ Inactive GitHub Repos", filter: it => it.kind === "repo" && !["missing", "archived"].includes(it.tier) },
        { title: "🌐 Dead / Parked Websites", filter: it => it.kind === "site" },
      ];

      const lines = ["## FMHY Curated Drift Report"];
      for (const g of groups) {
        const bucket = selected.filter(g.filter);
        if (!bucket.length) continue;
        lines.push("", `### ${g.title}`);
        for (const it of bucket) {
          const star = it.starred ? "⭐ " : "";
          const note = (state.notes[it.id] || "").trim();
          const noteSuffix = note ? ` — *${note}*` : "";
          if (it.kind === "repo") {
            const webPart = (it.webUrls && it.webUrls.length)
              ? " | Web: " + it.webUrls.map(u => `<${u}>`).join(", ")
              : "";
            const replPart = it.replacementUrl
              ? ` | ➡️ Replacement: <${it.replacementUrl}>${it.replacementStatus ? ` (${it.replacementStatus})` : ""}`
              : "";
            const detail = it.tier === "missing"
              ? it.reason
              : (it.tier === "archived"
                  ? `archived${it.inactiveDays != null ? `, ${it.inactiveDays}d` : ""}`
                  : `${it.inactiveDays}d inactive`);
            lines.push(`- ${star}**${it.title}** (\`${it.catLabel}\`) — <${it.htmlUrl}> (${detail})${webPart}${replPart}${noteSuffix}`);
          } else {
            lines.push(`- ${star}**${it.title}** (\`${it.catLabel}\`) — <${it.siteUrl}> (${it.reason})${noteSuffix}`);
          }
        }
      }
      return lines.join("\n");
    }

    function renderDiscordPreview() {
      document.getElementById("discord-preview").value = buildDiscordMarkdown();
      document.getElementById("wl-count").textContent =
        `${state.whitelistRepos.size + state.whitelistUrls.size} items`;
    }

    function renderAll() {
      renderTabs();
      renderList();
      renderDiscordPreview();
    }

    function buildWhitelistJson() {
      return JSON.stringify(
        {
          repos: [...state.whitelistRepos].sort(),
          urls: [...state.whitelistUrls].sort(),
          notes: state.whitelistNotes,
        },
        null,
        2
      ) + "\n";
    }

    document.getElementById("search").oninput = () => { state.page = 0; renderList(); };
    document.getElementById("cat-filter").onchange = () => { state.page = 0; renderList(); };
    document.getElementById("starred-only").onchange = () => { state.page = 0; renderList(); };
    document.getElementById("has-web-only").onchange = () => { state.page = 0; renderList(); };

    document.getElementById("tick-page-btn").onclick = () => {
      const all = filteredItems();
      const slice = all.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE);
      slice.forEach(it => state.selectedIds.add(it.id));
      saveLocalState();
      renderAll();
    };

    document.getElementById("clear-ticks-btn").onclick = () => {
      state.selectedIds.clear();
      saveLocalState();
      renderAll();
    };

    document.getElementById("copy-md-btn").onclick = async () => {
      const txt = document.getElementById("discord-preview").value;
      if (!txt) return;
      await navigator.clipboard.writeText(txt);
      document.getElementById("discord-status").textContent = "Copied Discord Markdown to clipboard.";
    };

    document.getElementById("download-md-btn").onclick = () => {
      const txt = document.getElementById("discord-preview").value;
      if (!txt) return;
      const blob = new Blob([txt], { type: "text/markdown;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "fmhy-curated-drift.md";
      a.click();
    };

    document.getElementById("send-discord-btn").onclick = async () => {
      const statusEl = document.getElementById("discord-status");
      const webhook = document.getElementById("webhook-url").value.trim();
      const content = document.getElementById("discord-preview").value.trim();
      if (!webhook) { statusEl.textContent = "Enter a Discord Webhook URL first."; return; }
      if (!content) { statusEl.textContent = "Tick at least one entry first."; return; }
      localStorage.setItem("fmhy_discord_webhook", webhook);

      // Split cleanly on line boundaries under Discord's 2000-character limit
      const chunks = [];
      let cur = "";
      for (const line of content.split("\n")) {
        if ((cur + "\n" + line).length > 1920 && cur) {
          chunks.push(cur);
          cur = line;
        } else {
          cur = cur ? (cur + "\n" + line) : line;
        }
      }
      if (cur) chunks.push(cur);

      statusEl.textContent = `Sending ${chunks.length} message(s) to Discord...`;
      try {
        for (const chunk of chunks) {
          const r = await fetch(webhook, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ content: chunk, flags: 4 }), // flags=4 suppresses link embed spam!
          });
          if (!r.ok) throw new Error(`Discord HTTP ${r.status}`);
        }
        statusEl.textContent = `Sent ${chunks.length} message(s) to Discord!`;
      } catch (err) {
        statusEl.textContent = `Discord send failed: ${err.message}`;
      }
    };

    document.getElementById("copy-wl-btn").onclick = async () => {
      const jsonTxt = buildWhitelistJson();
      await navigator.clipboard.writeText(jsonTxt);
      document.getElementById("wl-status").textContent = "Copied whitelist.json! Opening GitHub web editor...";
      window.open(`https://github.com/${REPO_SLUG}/edit/main/whitelist.json`, "_blank");
    };

    document.getElementById("save-gh-btn").onclick = async () => {
      const statusEl = document.getElementById("wl-status");
      const pat = document.getElementById("gh-pat").value.trim();
      if (!pat) {
        statusEl.textContent = "Enter a GitHub PAT, or click 'Copy JSON & Edit on GitHub'.";
        return;
      }
      localStorage.setItem("fmhy_gh_pat", pat);
      statusEl.textContent = "Committing whitelist.json to GitHub...";
      try {
        const apiUrl = `https://api.github.com/repos/${REPO_SLUG}/contents/whitelist.json`;
        const headers = {
          "Accept": "application/vnd.github+json",
          "Authorization": `Bearer ${pat}`,
        };
        let sha = undefined;
        const curResp = await fetch(apiUrl, { headers });
        if (curResp.ok) {
          const curData = await curResp.json();
          sha = curData.sha;
        }
        const jsonBytes = new TextEncoder().encode(buildWhitelistJson());
        let binary = "";
        jsonBytes.forEach(b => binary += String.fromCharCode(b));
        const b64 = btoa(binary);

        const putResp = await fetch(apiUrl, {
          method: "PUT",
          headers: { ...headers, "Content-Type": "application/json" },
          body: JSON.stringify({
            message: "Update whitelist.json via triage UI",
            content: b64,
            ...(sha ? { sha } : {}),
          }),
        });
        if (!putResp.ok) throw new Error(`GitHub API HTTP ${putResp.status}`);
        statusEl.textContent = "Saved whitelist.json to main!";
      } catch (err) {
        statusEl.textContent = `Commit failed: ${err.message}`;
      }
    };

    loadData();
  </script>
</body>
</html>
"""
