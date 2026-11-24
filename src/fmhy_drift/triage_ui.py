"""Static HTML/JS Triage & Discord Report Builder UI styled after Mozilla Thunderbird 2.0 (2008)."""

from __future__ import annotations

TRIAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Inbox - FMHY Drift Thunderbird</title>
  <style>
    * { box-sizing: border-box; }
    html, body {
      margin: 0;
      padding: 0;
      height: 100%;
      overflow: hidden;
      font-family: Tahoma, "Segoe UI", "DejaVu Sans", Verdana, sans-serif;
      font-size: 11.5px;
      color: #111;
      background: #cfdbe9;
      user-select: none;
    }
    a { color: #0044aa; text-decoration: underline; }
    a:hover { color: #0066ee; }

    /* Outer Vista/XP XUL Window Frame */
    .tb-window {
      display: flex;
      flex-direction: column;
      height: 100vh;
      width: 100vw;
      border: 1px solid #6982a3;
      background: #e4ebf5;
    }

    /* Classic Title Bar */
    .tb-titlebar {
      height: 25px;
      background: linear-gradient(180deg, #e7f0fb 0%, #c6d9f1 48%, #b3cae8 52%, #d2e2f6 100%);
      border-bottom: 1px solid #8ba2c1;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 8px;
      font-size: 12px;
      color: #1c2b3d;
      text-shadow: 0 1px 0 rgba(255,255,255,0.8);
    }
    .tb-titlebar-left {
      display: flex;
      align-items: center;
      gap: 6px;
      font-weight: 600;
    }
    .tb-win-controls {
      display: flex;
      gap: 3px;
    }
    .tb-win-btn {
      width: 22px;
      height: 16px;
      border: 1px solid #7d93b2;
      border-radius: 2px;
      background: linear-gradient(180deg, #f7faff 0%, #ccd9ec 100%);
      font-size: 10px;
      line-height: 14px;
      text-align: center;
      color: #2b3e57;
    }

    /* Menu Bar */
    .tb-menubar {
      height: 22px;
      background: linear-gradient(180deg, #f3f6fb 0%, #dfe7f3 100%);
      border-bottom: 1px solid #b6c3d6;
      display: flex;
      align-items: center;
      padding: 0 6px;
      gap: 2px;
    }
    .tb-menu-item {
      padding: 2px 7px;
      border: 1px solid transparent;
      border-radius: 2px;
      cursor: pointer;
      color: #111;
      text-decoration: none;
    }
    .tb-menu-item:hover {
      background: linear-gradient(180deg, #fafcff 0%, #d4e3f8 100%);
      border-color: #8da9ce;
    }
    .tb-menu-item u { text-decoration: underline; }
    .tb-menubar-spacer { flex: 1; }
    .tb-throbber {
      width: 16px;
      height: 16px;
      opacity: 0.7;
    }

    /* Big Icon Mail Toolbar */
    .tb-toolbar {
      min-height: 52px;
      background: linear-gradient(180deg, #f6f9fd 0%, #e1eaf6 50%, #d2dfef 51%, #dfe9f7 100%);
      border-bottom: 1px solid #9cacbf;
      box-shadow: inset 0 1px 0 #ffffff;
      display: flex;
      align-items: center;
      padding: 3px 8px;
      gap: 3px;
      flex-wrap: wrap;
    }
    .tb-tool-btn {
      display: inline-flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-width: 54px;
      height: 44px;
      padding: 2px 7px;
      border: 1px solid transparent;
      border-radius: 3px;
      background: transparent;
      cursor: pointer;
      color: #182330;
      font-family: inherit;
      font-size: 11px;
      gap: 2px;
    }
    .tb-tool-btn:hover {
      background: linear-gradient(180deg, rgba(255,255,255,0.85) 0%, rgba(196,216,242,0.7) 100%);
      border-color: #8aa5c7;
      box-shadow: inset 0 1px 0 #fff;
    }
    .tb-tool-btn:active {
      background: linear-gradient(180deg, #c5d7ee 0%, #dde9f8 100%);
      border-color: #6f8cb2;
    }
    .tb-tool-btn svg {
      width: 22px;
      height: 22px;
      display: block;
    }
    .tb-sep {
      width: 2px;
      height: 36px;
      margin: 0 4px;
      border-left: 1px solid #a8b7cc;
      border-right: 1px solid #f7faff;
    }
    .tb-filter-group {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 0 6px;
      font-size: 11px;
      color: #1f2e40;
    }
    .tb-filter-group label {
      display: inline-flex;
      align-items: center;
      gap: 3px;
      cursor: pointer;
    }
    .tb-search-wrap {
      margin-left: auto;
      display: flex;
      align-items: center;
      background: #ffffff;
      border: 1px solid #7f9db9;
      box-shadow: inset 0 1px 2px rgba(0,0,0,0.08);
      height: 23px;
      width: 235px;
      padding: 0 5px;
      gap: 4px;
    }
    .tb-search-wrap input {
      border: none;
      outline: none;
      width: 100%;
      font-family: inherit;
      font-size: 11.5px;
      background: transparent;
      color: #111;
    }

    /* Main 3-Pane Workspace */
    .tb-workspace {
      flex: 1;
      display: grid;
      grid-template-columns: 235px minmax(0, 1fr);
      gap: 4px;
      padding: 4px;
      min-height: 0;
      background: #d7e1ef;
    }

    /* Left Folder Tree Pane */
    .tb-folder-pane {
      background: #ffffff;
      border: 1px solid #8b9db5;
      display: flex;
      flex-direction: column;
      min-height: 0;
    }
    .tb-pane-header {
      height: 21px;
      background: linear-gradient(180deg, #fbfcfe 0%, #e2eaf5 55%, #d3dfee 100%);
      border-bottom: 1px solid #9eb0c7;
      padding: 0 7px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 11px;
      font-weight: 600;
      color: #223247;
    }
    .tb-folder-tree {
      flex: 1;
      overflow-y: auto;
      padding: 4px 3px;
    }
    .tb-tree-root {
      font-weight: 700;
      padding: 3px 4px;
      display: flex;
      align-items: center;
      gap: 5px;
      color: #111;
    }
    .tb-folder-item {
      padding: 2px 6px 2px 18px;
      display: flex;
      align-items: center;
      gap: 5px;
      cursor: pointer;
      border: 1px solid transparent;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      color: #111;
    }
    .tb-folder-item:hover {
      background: #eef4fc;
    }
    .tb-folder-item.active {
      background: linear-gradient(180deg, #dceafc 0%, #c1daf8 100%);
      border-color: #7da2ce;
    }
    .tb-folder-item.has-items {
      font-weight: 700;
    }
    .tb-folder-icon {
      width: 14px;
      height: 14px;
      flex-shrink: 0;
    }
    .tb-cat-divider {
      margin-top: 8px;
      padding: 3px 4px;
      font-weight: 700;
      border-top: 1px solid #e1e7f0;
      color: #223247;
      display: flex;
      align-items: center;
      gap: 5px;
    }

    /* Right Split: Thread Table (Top) + Message/Outbox Preview (Bottom) */
    .tb-right-split {
      display: grid;
      grid-template-rows: minmax(220px, 54%) minmax(210px, 46%);
      gap: 4px;
      min-height: 0;
      min-width: 0;
    }

    /* Top-Right Thread Table */
    .tb-thread-pane {
      background: #ffffff;
      border: 1px solid #8b9db5;
      display: flex;
      flex-direction: column;
      min-height: 0;
      min-width: 0;
    }
    .tb-table-wrap {
      flex: 1;
      overflow: auto;
    }
    table.tb-table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 11.5px;
    }
    table.tb-table thead th {
      position: sticky;
      top: 0;
      z-index: 2;
      height: 21px;
      background: linear-gradient(180deg, #ffffff 0%, #e4ebf5 55%, #d3dfee 100%);
      border-bottom: 1px solid #96a8c0;
      border-right: 1px solid #bbc8da;
      padding: 0 5px;
      text-align: left;
      font-weight: 400;
      color: #182330;
      cursor: pointer;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    table.tb-table thead th:hover {
      background: linear-gradient(180deg, #ffffff 0%, #eaf2fd 55%, #dae8fa 100%);
    }
    table.tb-table tbody tr {
      height: 21px;
      border-bottom: 1px solid #edf1f7;
      cursor: default;
    }
    table.tb-table tbody tr:nth-child(even) {
      background: #f6f9fd;
    }
    table.tb-table tbody tr:hover {
      background: #eaf3fd;
    }
    table.tb-table tbody tr.selected {
      background: #316ac5 !important;
      color: #ffffff !important;
    }
    table.tb-table tbody tr.selected a,
    table.tb-table tbody tr.selected .muted-cell {
      color: #ffffff !important;
    }
    table.tb-table tbody tr.ticked:not(.selected) {
      background: #fff9e5;
    }
    table.tb-table td {
      padding: 1px 5px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      vertical-align: middle;
    }
    .col-cb { width: 26px; text-align: center !important; }
    .col-star { width: 24px; text-align: center !important; }
    .col-clip { width: 24px; text-align: center !important; }
    .col-subject { width: 25%; }
    .col-repo { width: 21%; }
    .col-web { width: 25%; }
    .col-cat { width: 14%; }
    .col-status { width: 15%; }

    .star-on { color: #e69c00; font-size: 13px; }
    .star-off { color: #b8c4d4; font-size: 11px; }
    .bold-row td.col-subject, .bold-row td.col-repo { font-weight: 700; }
    .muted-cell { color: #4a5a6e; }
    .repl-tag {
      color: #0b6b24;
      font-weight: 700;
    }
    tr.selected .repl-tag { color: #b6ffcc !important; }

    /* Bottom-Right Message & Address-Book-Card Preview Pane */
    .tb-preview-pane {
      background: #ffffff;
      border: 1px solid #8b9db5;
      display: flex;
      flex-direction: column;
      min-height: 0;
      user-select: text;
    }
    .tb-msg-header {
      background: linear-gradient(180deg, #f2f6fc 0%, #e1eaf6 100%);
      border-bottom: 1px solid #a9bbd2;
      padding: 5px 10px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-size: 11.5px;
    }
    .tb-msg-header-fields {
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      align-items: center;
      min-width: 0;
    }
    .tb-hdr-label {
      color: #4f6177;
      font-weight: 700;
      margin-right: 3px;
    }
    .tb-preview-body {
      flex: 1;
      overflow-y: auto;
      padding: 10px 14px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(320px, 44%);
      gap: 16px;
    }
    @media (max-width: 980px) {
      .tb-workspace { grid-template-columns: 190px minmax(0, 1fr); }
      .tb-preview-body { grid-template-columns: 1fr; }
    }
    .tb-card-heading {
      margin: 0 0 6px 0;
      font-size: 15px;
      font-weight: 700;
      color: #000;
      border-bottom: 2px solid #111;
      padding-bottom: 2px;
    }
    /* Classic Thunderbird Address Book Blue Section Bars (#3b88d8) */
    .tb-blue-bar {
      background: #3b88d8;
      color: #ffffff;
      font-weight: 700;
      font-size: 11.5px;
      padding: 2px 7px;
      margin: 6px 0 6px 0;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .tb-detail-row {
      margin: 4px 0;
      font-size: 11.5px;
      line-height: 1.45;
      word-break: break-word;
    }
    .tb-input, .tb-textarea {
      border: 1px solid #7f9db9;
      background: #ffffff;
      font-family: Tahoma, "Segoe UI", sans-serif;
      font-size: 11.5px;
      padding: 3px 6px;
      color: #111;
    }
    .tb-textarea {
      width: 100%;
      min-height: 92px;
      font-family: Consolas, "Liberation Mono", monospace;
      font-size: 11px;
      resize: vertical;
    }
    .tb-classic-btn {
      border: 1px solid #7f9db9;
      border-radius: 2px;
      background: linear-gradient(180deg, #fcfdff 0%, #dfe8f4 100%);
      padding: 2px 9px;
      font-family: inherit;
      font-size: 11px;
      color: #111;
      cursor: pointer;
    }
    .tb-classic-btn:hover {
      background: linear-gradient(180deg, #ffffff 0%, #cde0f7 100%);
      border-color: #567db0;
    }
    .tb-classic-btn.primary {
      font-weight: 700;
      border-color: #3b6ea5;
    }
    .tb-btn-row {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      margin-top: 5px;
      align-items: center;
    }

    /* Classic Sunken XUL Status Bar */
    .tb-statusbar {
      height: 22px;
      background: linear-gradient(180deg, #edf1f7 0%, #dde4ef 100%);
      border-top: 1px solid #9eb0c7;
      display: flex;
      align-items: center;
      padding: 0 6px;
      font-size: 11px;
      color: #1f2d3d;
      gap: 6px;
    }
    .tb-status-main {
      flex: 1;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .tb-status-panel {
      border-left: 1px solid #b3c1d4;
      padding: 0 8px;
      white-space: nowrap;
    }
    .tb-pager-btns button {
      border: 1px solid #8fa3bd;
      background: #f4f7fc;
      font-size: 10.5px;
      padding: 0 6px;
      cursor: pointer;
    }
    .tb-pager-btns button:disabled {
      opacity: 0.45;
      cursor: default;
    }
  </style>
</head>
<body>
  <div class="tb-window">
    <!-- Titlebar -->
    <div class="tb-titlebar">
      <div class="tb-titlebar-left">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none">
          <circle cx="12" cy="12" r="10" fill="#2b6cb0" stroke="#1a365d" stroke-width="1.5"/>
          <path d="M5 11l7 5 7-5M5 8h14v9H5z" fill="#fff" opacity="0.92"/>
        </svg>
        <span id="window-title">Inbox (All Flagged) - FMHY Drift Thunderbird</span>
      </div>
      <div class="tb-win-controls">
        <span class="tb-win-btn">_</span>
        <span class="tb-win-btn">□</span>
        <span class="tb-win-btn">×</span>
      </div>
    </div>

    <!-- Menu bar -->
    <div class="tb-menubar">
      <a class="tb-menu-item" href="./fmhy-drift-report.md" target="_blank"><u>F</u>ile (.md)</a>
      <a class="tb-menu-item" href="./fmhy-drift-report.json" target="_blank"><u>D</u>ata (.json)</a>
      <span class="tb-menu-item" id="menu-tick-page"><u>E</u>dit (Tick Page)</span>
      <span class="tb-menu-item" id="menu-clear-ticks"><u>C</u>lear Ticks</span>
      <span class="tb-menu-item" id="menu-outbox"><u>M</u>essage Outbox</span>
      <span class="tb-menu-item" id="menu-copy-wl"><u>T</u>ools (Edit whitelist.json)</span>
      <a class="tb-menu-item" href="https://github.com/melo-foundation/fmhy-drift" target="_blank"><u>H</u>elp (GitHub)</a>
      <div class="tb-menubar-spacer"></div>
      <span style="color:#556880;font-size:10.5px;padding-right:6px;">Keys: ↑/↓ Navigate · Space Tick · J Whitelist</span>
    </div>

    <!-- Big-Icon XUL Toolbar -->
    <div class="tb-toolbar">
      <button type="button" class="tb-tool-btn" id="btn-reload" title="Reload fmhy-drift-report.json">
        <svg viewBox="0 0 24 24"><path fill="#2b7de9" d="M12 4V1L8 5l4 4V6c3.3 0 6 2.7 6 6 0 1-.25 1.97-.7 2.8l1.46 1.46A7.93 7.93 0 0020 12c0-4.42-3.58-8-8-8zm-6 8c0-1 .25-1.97.7-2.8L5.24 7.74A7.93 7.93 0 004 12c0 4.42 3.58 8 8 8v3l4-4-4-4v3c-3.3 0-6-2.7-6-6z"/></svg>
        <span>Get Drift</span>
      </button>

      <button type="button" class="tb-tool-btn" id="btn-send-discord" title="Send ticked report to Discord Webhook">
        <svg viewBox="0 0 24 24"><rect x="2" y="5" width="20" height="14" rx="2" fill="#f7d774" stroke="#9b7414" stroke-width="1.4"/><path d="M3 6l9 7 9-7" fill="none" stroke="#9b7414" stroke-width="1.4"/><path d="M15 13l6 4v-8z" fill="#3b88d8" opacity="0.25"/></svg>
        <span>Send Report</span>
      </button>

      <button type="button" class="tb-tool-btn" id="btn-copy-md" title="Copy Discord Markdown to clipboard">
        <svg viewBox="0 0 24 24"><rect x="6" y="3" width="12" height="16" rx="1" fill="#fff" stroke="#5b708b" stroke-width="1.5"/><path d="M9 7h6M9 10h6M9 13h4" stroke="#3b88d8" stroke-width="1.5"/><rect x="4" y="6" width="12" height="15" rx="1" fill="none" stroke="#8a9db5" stroke-width="1.2"/></svg>
        <span>Copy Report</span>
      </button>

      <div class="tb-sep"></div>

      <button type="button" class="tb-tool-btn" id="btn-toggle-tick" title="Tick / Untick selected row (Space)">
        <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="#eaf6ec" stroke="#2d8a42" stroke-width="1.6"/><path d="M8 12.5l2.7 2.7L16.5 9" fill="none" stroke="#237a38" stroke-width="2.2" stroke-linecap="round"/></svg>
        <span>Tag / Tick</span>
      </button>

      <button type="button" class="tb-tool-btn" id="btn-junk-wl" title="Whitelist / Unwhitelist selected item (Key: J)">
        <svg viewBox="0 0 24 24"><path d="M13.5 2s.8 3.2-1.5 5.4c-1.8 1.7-2.2 3.3-1.2 4.8-1.8-.6-2.8-2.3-2.5-4.3C5.7 9.8 5 12.8 6.2 15.8 7.5 19 10.5 21 13.3 21c3.6 0 6.2-2.7 6.2-6.3 0-4.2-2.8-6.4-2.8-6.4s.2 2.2-1.4 3.1c.6-3.1-.5-6.7-1.8-9.4z" fill="#f2791a" stroke="#a64300" stroke-width="1.2"/></svg>
        <span>Whitelist</span>
      </button>

      <button type="button" class="tb-tool-btn" id="btn-save-wl" title="Commit whitelist.json to GitHub">
        <svg viewBox="0 0 24 24"><path d="M5 3h11l3 3v15H5V3z" fill="#4f83cc" stroke="#24477a" stroke-width="1.4"/><rect x="8" y="3" width="7" height="6" fill="#e8eff9"/><rect x="7" y="13" width="10" height="7" fill="#fff"/></svg>
        <span>Save List</span>
      </button>

      <div class="tb-sep"></div>

      <div class="tb-filter-group">
        <label><input type="checkbox" id="starred-only" /> ★ Starred Only</label>
        <label><input type="checkbox" id="has-web-only" /> 📎 Has Site/Pages</label>
      </div>

      <div class="tb-search-wrap">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
          <circle cx="10.5" cy="10.5" r="6.5" stroke="#68809c" stroke-width="2"/>
          <path d="M15.5 15.5L21 21" stroke="#68809c" stroke-width="2" stroke-linecap="round"/>
        </svg>
        <input type="text" id="search" placeholder="Subject, Repo, or Domain" />
      </div>
    </div>

    <!-- 3-Pane Workspace -->
    <div class="tb-workspace">
      <!-- Left Folder Tree -->
      <aside class="tb-folder-pane">
        <div class="tb-pane-header">
          <span>All Folders</span>
          <span style="color:#5c708a;">◀ ▶</span>
        </div>
        <div class="tb-folder-tree" id="folder-tree"></div>
      </aside>

      <!-- Right Split -->
      <div class="tb-right-split">
        <!-- Top-Right Tabular Thread List -->
        <section class="tb-thread-pane">
          <div class="tb-table-wrap">
            <table class="tb-table">
              <thead>
                <tr>
                  <th class="col-cb" title="Tick for Discord Report">✓</th>
                  <th class="col-star" data-sort="starred" title="FMHY Starred">★</th>
                  <th class="col-clip" data-sort="links" title="Has Website / Pages / Replacement">📎</th>
                  <th class="col-subject" data-sort="title">Entry / Title</th>
                  <th class="col-repo" data-sort="repo">Repository</th>
                  <th class="col-web" data-sort="web">Website / Pages / New Repo</th>
                  <th class="col-cat" data-sort="cat">Category</th>
                  <th class="col-status" data-sort="days">Status / Age ▼</th>
                </tr>
              </thead>
              <tbody id="thread-tbody"></tbody>
            </table>
          </div>
        </section>

        <!-- Bottom-Right Thunderbird Envelope Header + Address-Book-Style Card -->
        <section class="tb-preview-pane">
          <div class="tb-msg-header" id="msg-header">
            <div class="tb-msg-header-fields">
              <span><span class="tb-hdr-label">Entry:</span> <strong id="hdr-title">Select an entry above</strong></span>
              <span><span class="tb-hdr-label">Repo:</span> <span id="hdr-repo">—</span></span>
              <span><span class="tb-hdr-label">Category:</span> <span id="hdr-cat">—</span></span>
            </div>
            <div class="tb-btn-row" style="margin:0;">
              <button type="button" class="tb-classic-btn" id="hdr-tick-btn">✓ Tick for Discord</button>
              <button type="button" class="tb-classic-btn" id="hdr-wl-btn">🔥 Whitelist</button>
            </div>
          </div>

          <div class="tb-preview-body">
            <!-- Left Column: Selected Item Card -->
            <div id="inspector-card">
              <div class="tb-card-heading" id="card-heading">No Entry Selected</div>

              <div class="tb-blue-bar">
                <span>Links, Website &amp; Replacement</span>
                <span id="card-tier-pill"></span>
              </div>
              <div id="card-details">
                <div class="tb-detail-row" style="color:#556880;">Click any row in the list above (or press ↑/↓) to inspect its GitHub repository, attached website / GitHub Pages link, and detected replacement link.</div>
              </div>

              <div class="tb-blue-bar" style="margin-top:10px;">
                <span>Note for Curated Report</span>
              </div>
              <input type="text" id="card-note-input" class="tb-input" style="width:100%;" placeholder="Optional note attached when ticked (e.g. 'replace with active fork', 'site also dead')..." disabled />
            </div>

            <!-- Right Column: Discord Outbox & Whitelist Sync -->
            <div>
              <div class="tb-blue-bar" style="margin-top:0;">
                <span>Discord Report Builder (Outbox)</span>
                <span id="selected-count">0 ticked</span>
              </div>
              <textarea id="discord-preview" class="tb-textarea" placeholder="Tick entries in the list (or press Space) to build a report for Discord..."></textarea>
              <div class="tb-btn-row">
                <button type="button" class="tb-classic-btn primary" id="copy-md-btn">Copy Markdown</button>
                <button type="button" class="tb-classic-btn" id="download-md-btn">Save .md</button>
                <input type="password" id="webhook-url" class="tb-input" style="flex:1;min-width:140px;" placeholder="Discord Webhook URL..." />
                <button type="button" class="tb-classic-btn primary" id="send-discord-btn">Send to Discord</button>
              </div>

              <div class="tb-blue-bar" style="margin-top:10px;">
                <span>Whitelist Sync (whitelist.json)</span>
                <span id="wl-count">0 whitelisted</span>
              </div>
              <div class="tb-btn-row">
                <input type="password" id="gh-pat" class="tb-input" style="flex:1;min-width:140px;" placeholder="Optional GitHub PAT for 1-click commit..." />
                <button type="button" class="tb-classic-btn primary" id="save-gh-btn">Commit whitelist.json</button>
                <button type="button" class="tb-classic-btn" id="copy-wl-btn">Copy &amp; Edit on GitHub</button>
              </div>
              <div id="action-status" style="margin-top:5px;font-size:11px;color:#224477;"></div>
            </div>
          </div>
        </section>
      </div>
    </div>

    <!-- Classic Sunken Status Bar -->
    <div class="tb-statusbar">
      <div class="tb-status-main" id="run-meta">Connecting to FMHY Drift index...</div>
      <div class="tb-status-panel tb-pager-btns">
        <button type="button" id="prev-pg">◀ Prev</button>
        <span id="page-label">Page 1 / 1</span>
        <button type="button" id="next-pg">Next ▶</button>
      </div>
      <div class="tb-status-panel" id="stat-showing">Showing: 0</div>
      <div class="tb-status-panel" id="stat-ticked">Ticked: 0</div>
      <div class="tb-status-panel" id="stat-wl">Whitelisted: 0</div>
    </div>
  </div>

  <script>
    const REPO_SLUG = "melo-foundation/fmhy-drift";
    const PAGE_SIZE = 100;

    const FOLDERS = [
      { id: "inbox_all", label: "Inbox (All Flagged)", icon: "inbox" },
      { id: "missing", label: "Dead / 404 Repos", icon: "inbox" },
      { id: "archived_moved", label: "Archived — Moved", icon: "moved" },
      { id: "archived", label: "All Archived", icon: "folder" },
      { id: "tier3_critical", label: "Inactive 1y+", icon: "folder" },
      { id: "tier2_warning", label: "Inactive 6m+", icon: "folder" },
      { id: "tier1_notice", label: "Inactive 3m+", icon: "folder" },
      { id: "dead_sites", label: "Dead / Parked Sites", icon: "folder" },
      { id: "outbox", label: "Discord Outbox", icon: "outbox" },
      { id: "whitelisted", label: "Whitelisted", icon: "junk" },
    ];

    const state = {
      items: [],
      categories: [],
      activeFolder: "inbox_all",
      activeCategory: "",
      sortField: "days",
      sortAsc: false,
      page: 0,
      focusedId: null,
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

    function setStatus(msg) {
      document.getElementById("action-status").textContent = msg;
    }

    function folderSvg(type) {
      if (type === "inbox") {
        return `<svg class="tb-folder-icon" viewBox="0 0 16 16"><path d="M2 5h12l1 6v3H1v-3l1-6z" fill="#7aa6da" stroke="#2f5687"/><path d="M5 10h2a1 1 0 002 0h2" fill="none" stroke="#fff"/></svg>`;
      }
      if (type === "moved") {
        return `<svg class="tb-folder-icon" viewBox="0 0 16 16"><path d="M1 3h5l1.5 2H15v9H1V3z" fill="#f3cf68" stroke="#9e7a1c"/><path d="M6 9.5h5m0 0L9 7.5m2 2L9 11.5" stroke="#186a2c" stroke-width="1.5" stroke-linecap="round"/></svg>`;
      }
      if (type === "outbox") {
        return `<svg class="tb-folder-icon" viewBox="0 0 16 16"><path d="M2 6h12v8H2z" fill="#9dc3ee" stroke="#365f91"/><path d="M8 2l4 4H9.5v4h-3V6H4l4-4z" fill="#2d8a42"/></svg>`;
      }
      if (type === "junk") {
        return `<svg class="tb-folder-icon" viewBox="0 0 16 16"><path d="M9 1.5s.6 2.2-1 3.7c-1.2 1.1-1.4 2.2-.8 3.2-1.2-.4-1.8-1.5-1.6-2.8C4 6.8 3.5 8.8 4.3 10.8 5.2 13 7.1 14.5 9 14.5c2.4 0 4.1-1.8 4.1-4.2 0-2.8-1.9-4.3-1.9-4.3s.1 1.5-.9 2.1c.4-2.1-.3-4.5-1.3-6.6z" fill="#f2791a" stroke="#9d3f00" stroke-width="0.9"/></svg>`;
      }
      return `<svg class="tb-folder-icon" viewBox="0 0 16 16"><path d="M1 3h5l1.5 2H15v9H1V3z" fill="#f4d47c" stroke="#a68121"/></svg>`;
    }

    function isPagesUrl(url) {
      try { return new URL(url).hostname.toLowerCase().endsWith(".github.io"); }
      catch { return false; }
    }

    function shortUrlLabel(url) {
      try {
        const u = new URL(url);
        const p = u.pathname && u.pathname !== "/" ? u.pathname.replace(/\/$/, "") : "";
        return u.hostname.replace(/^www\./, "") + (p.length <= 22 ? p : p.slice(0, 20) + "…");
      } catch { return url; }
    }

    function isWhitelisted(item) {
      if (item.kind === "repo") return state.whitelistRepos.has(item.wlKey);
      return state.whitelistUrls.has(item.wlKey);
    }

    function formatAgeHuman(days) {
      if (days == null || days < 0 || days >= 999999) return null;
      const yrs = Math.floor(days / 365);
      const mos = Math.floor((days % 365) / 30);
      const remDays = days % 30;
      if (yrs >= 1) {
        return (mos > 0 ? `${yrs}y ${mos}m` : `${yrs}y`) + ` (${days}d)`;
      }
      if (mos >= 1) {
        return (remDays > 0 ? `${mos}m ${remDays}d` : `${mos}m`) + ` (${days}d)`;
      }
      return `${days}d`;
    }

    function escapeHtml(s) {
      return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    async function loadData() {
      document.getElementById("run-meta").textContent = "Loading whitelist and drift report...";
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

        const resp = await fetch(`./fmhy-drift-report.json?t=${Date.now()}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const report = await resp.json();
        ingestReport(report);
      } catch (err) {
        document.getElementById("run-meta").textContent = "Error loading report: " + err.message;
      }
    }

    function ingestReport(report) {
      const dt = new Date(report.generated_at).toISOString().replace("T", " ").slice(0, 16) + " UTC";
      document.getElementById("run-meta").textContent =
        `Last scan: ${dt}  |  Mode: ${report.mode}  |  Checked: ${report.summary.unique_repos_checked} repos  |  Thresholds: ${report.thresholds.tier1_days}d / ${report.thresholds.tier2_days}d / ${report.thresholds.tier3_days}d`;

      const items = [];
      const cats = new Set();

      for (const entry of report.entries || []) {
        cats.add(entry.category);
        const catLabel = entry.subcategory ? `${entry.category} > ${entry.subcategory}` : entry.category;

        for (const rr of entry.github_repos || []) {
          if (rr.tier === "active") continue;
          const wlKey = rr.full_name.toLowerCase();
          const mergedWeb = [...(entry.web_urls || [])];
          if (rr.homepage_url && !mergedWeb.some(u => u.replace(/\/$/, "").toLowerCase() === rr.homepage_url.replace(/\/$/, "").toLowerCase())) {
            mergedWeb.push(rr.homepage_url);
          }
          items.push({
            id: `repo:${entry.entry_id}:${wlKey}`,
            kind: "repo",
            wlKey,
            title: entry.title,
            starred: Boolean(entry.starred),
            category: entry.category,
            catLabel,
            entryId: entry.entry_id,
            tier: rr.tier,
            fullName: rr.full_name,
            htmlUrl: rr.html_url,
            inactiveDays: rr.inactive_days,
            lastActivityAt: rr.last_activity_at ? rr.last_activity_at.slice(0, 10) : null,
            sortDays: rr.inactive_days != null ? rr.inactive_days : 1000000,
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
            starred: Boolean(entry.starred),
            category: entry.category,
            catLabel,
            entryId: entry.entry_id,
            tier: "dead_sites",
            fullName: "—",
            siteUrl: sr.url,
            siteStatus: sr.status,
            inactiveDays: null,
            lastActivityAt: null,
            sortDays: 999999,
            reason: sr.reason,
            replacementUrl: null,
            replacementStatus: null,
            webUrls: [sr.url],
          });
        }
      }

      state.items = items;
      state.categories = [...cats].sort();
      renderAll();
    }

    function itemsForFolder(folderId) {
      if (folderId === "whitelisted") {
        return state.items.filter(it => isWhitelisted(it));
      }
      if (folderId === "outbox") {
        return state.items.filter(it => state.selectedIds.has(it.id));
      }
      if (folderId === "inbox_all") {
        return state.items.filter(it => !isWhitelisted(it));
      }
      return state.items.filter(it => {
        if (isWhitelisted(it)) return false;
        if (folderId === "archived_moved") return it.kind === "repo" && it.tier === "archived" && Boolean(it.replacementUrl);
        if (folderId === "dead_sites") return it.kind === "site";
        return it.kind === "repo" && it.tier === folderId;
      });
    }

    function filteredAndSortedItems() {
      const q = document.getElementById("search").value.trim().toLowerCase();
      const starredOnly = document.getElementById("starred-only").checked;
      const hasWebOnly = document.getElementById("has-web-only").checked;

      const list = itemsForFolder(state.activeFolder).filter(it => {
        if (state.activeCategory && it.category !== state.activeCategory) return false;
        if (starredOnly && !it.starred) return false;
        if (hasWebOnly && (!it.webUrls || it.webUrls.length === 0)) return false;
        if (!q) return true;
        const hay = `${it.title} ${it.catLabel} ${it.fullName || ""} ${it.siteUrl || ""} ${it.replacementUrl || ""} ${(it.webUrls || []).join(" ")}`.toLowerCase();
        return hay.includes(q);
      });

      const dir = state.sortAsc ? 1 : -1;
      list.sort((a, b) => {
        let va, vb;
        if (state.sortField === "starred") { va = a.starred ? 1 : 0; vb = b.starred ? 1 : 0; }
        else if (state.sortField === "links") { va = (a.webUrls?.length || 0) + (a.replacementUrl ? 5 : 0); vb = (b.webUrls?.length || 0) + (b.replacementUrl ? 5 : 0); }
        else if (state.sortField === "title") { return dir * a.title.localeCompare(b.title); }
        else if (state.sortField === "repo") { return dir * (a.fullName || "").localeCompare(b.fullName || ""); }
        else if (state.sortField === "cat") { return dir * a.catLabel.localeCompare(b.catLabel); }
        else if (state.sortField === "web") { va = a.replacementUrl || a.webUrls?.[0] || ""; vb = b.replacementUrl || b.webUrls?.[0] || ""; return dir * va.localeCompare(vb); }
        else {
          // Default ("days"): strictly longest-unupdated / dead at the top!
          va = a.sortDays;
          vb = b.sortDays;
        }
        if (va !== vb) return dir * (va - vb);
        return a.title.localeCompare(b.title);
      });
      return list;
    }

    function renderFolderTree() {
      const tree = document.getElementById("folder-tree");
      let html = `<div class="tb-tree-root">▾ ${folderSvg("folder")} <span>Local Drift Folders</span></div>`;

      for (const f of FOLDERS) {
        const cnt = itemsForFolder(f.id).length;
        const activeCls = state.activeFolder === f.id ? " active" : "";
        const boldCls = cnt > 0 ? " has-items" : "";
        const countSuffix = cnt > 0 ? ` (${cnt})` : "";
        html += `<div class="tb-folder-item${activeCls}${boldCls}" data-folder="${f.id}">
          ${folderSvg(f.icon)}
          <span>${escapeHtml(f.label)}${countSuffix}</span>
        </div>`;
      }

      html += `<div class="tb-cat-divider">▾ ${folderSvg("folder")} <span>Category Filter</span></div>`;
      const allCatActive = state.activeCategory === "" ? " active has-items" : "";
      html += `<div class="tb-folder-item${allCatActive}" data-cat="">
        ${folderSvg("folder")} <span>All Categories</span>
      </div>`;
      for (const c of state.categories) {
        const cActive = state.activeCategory === c ? " active has-items" : "";
        html += `<div class="tb-folder-item${cActive}" data-cat="${escapeHtml(c)}">
          ${folderSvg("folder")} <span>${escapeHtml(c)}</span>
        </div>`;
      }

      tree.innerHTML = html;

      tree.querySelectorAll("[data-folder]").forEach(el => {
        el.onclick = () => {
          state.activeFolder = el.getAttribute("data-folder");
          state.page = 0;
          const folderObj = FOLDERS.find(x => x.id === state.activeFolder);
          document.getElementById("window-title").textContent =
            `${folderObj ? folderObj.label : "Drift"} - FMHY Drift Thunderbird`;
          renderAll();
        };
      });

      tree.querySelectorAll("[data-cat]").forEach(el => {
        el.onclick = () => {
          state.activeCategory = el.getAttribute("data-cat");
          state.page = 0;
          renderAll();
        };
      });
    }

    function formatStatusCell(it) {
      const age = formatAgeHuman(it.inactiveDays);
      if (it.kind === "site") return `${it.siteStatus}: ${it.reason}`;
      if (it.tier === "missing") {
        if (age) return `Disabled · ${age}`;
        if (it.reason.includes("451")) return "Dead (DMCA 451)";
        return "Dead (404 Gone)";
      }
      if (it.tier === "archived") {
        return age ? `Archived · ${age}` : "Archived";
      }
      return age ? `${age} ago` : `${it.inactiveDays}d ago`;
    }

    function formatWebCell(it) {
      const parts = [];
      if (it.replacementUrl) {
        parts.push(`<span class="repl-tag">↪ ${escapeHtml(shortUrlLabel(it.replacementUrl))}</span>`);
      }
      for (const u of (it.webUrls || [])) {
        const prefix = isPagesUrl(u) ? "Pages:" : "Web:";
        parts.push(`${prefix} ${escapeHtml(shortUrlLabel(u))}`);
      }
      return parts.join(" · ") || `<span class="muted-cell">—</span>`;
    }

    function renderTable() {
      const all = filteredAndSortedItems();
      const totalPages = Math.max(1, Math.ceil(all.length / PAGE_SIZE));
      if (state.page >= totalPages) state.page = totalPages - 1;
      const slice = all.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE);

      if (!state.focusedId && slice.length > 0) {
        state.focusedId = slice[0].id;
      }

      const tbody = document.getElementById("thread-tbody");
      tbody.innerHTML = "";

      for (const it of slice) {
        const tr = document.createElement("tr");
        const isSel = state.focusedId === it.id;
        const isTicked = state.selectedIds.has(it.id);
        tr.className =
          (isSel ? "selected " : "") +
          (isTicked ? "ticked " : "") +
          ((it.starred || it.replacementUrl || it.tier === "missing") ? "bold-row" : "");

        const hasClip = (it.webUrls && it.webUrls.length > 0) || Boolean(it.replacementUrl);

        tr.innerHTML = `
          <td class="col-cb"><input type="checkbox" class="row-cb" ${isTicked ? "checked" : ""} /></td>
          <td class="col-star">${it.starred ? '<span class="star-on">★</span>' : '<span class="star-off">•</span>'}</td>
          <td class="col-clip">${hasClip ? "📎" : ""}</td>
          <td class="col-subject" title="${escapeHtml(it.title)}">${escapeHtml(it.title)}</td>
          <td class="col-repo" title="${escapeHtml(it.fullName || it.siteUrl || "")}">${escapeHtml(it.kind === "repo" ? it.fullName : shortUrlLabel(it.siteUrl))}</td>
          <td class="col-web">${formatWebCell(it)}</td>
          <td class="col-cat muted-cell" title="${escapeHtml(it.catLabel)}">${escapeHtml(it.catLabel)}</td>
          <td class="col-status" title="${escapeHtml(it.reason)}">${escapeHtml(formatStatusCell(it))}</td>
        `;

        const cb = tr.querySelector(".row-cb");
        cb.onclick = (e) => {
          e.stopPropagation();
          if (cb.checked) state.selectedIds.add(it.id);
          else state.selectedIds.delete(it.id);
          state.focusedId = it.id;
          saveLocalState();
          renderAll();
        };

        tr.onclick = () => {
          state.focusedId = it.id;
          renderTable();
          renderInspector();
        };

        tr.ondblclick = () => {
          const url = it.kind === "repo" ? it.htmlUrl : it.siteUrl;
          if (url) window.open(url, "_blank", "noopener");
        };

        tbody.appendChild(tr);
      }

      document.getElementById("page-label").textContent = `Page ${state.page + 1} / ${totalPages}`;
      document.getElementById("prev-pg").disabled = state.page <= 0;
      document.getElementById("next-pg").disabled = state.page + 1 >= totalPages;
      document.getElementById("stat-showing").textContent = `Showing: ${all.length}`;
      document.getElementById("stat-ticked").textContent = `Ticked: ${state.selectedIds.size}`;
      document.getElementById("stat-wl").textContent =
        `Whitelisted: ${state.whitelistRepos.size + state.whitelistUrls.size}`;
    }

    function renderInspector() {
      const it = state.items.find(x => x.id === state.focusedId);
      const noteInput = document.getElementById("card-note-input");

      if (!it) {
        document.getElementById("hdr-title").textContent = "No entry selected";
        document.getElementById("hdr-repo").textContent = "—";
        document.getElementById("hdr-cat").textContent = "—";
        document.getElementById("card-heading").textContent = "No Entry Selected";
        document.getElementById("card-tier-pill").textContent = "";
        document.getElementById("card-details").innerHTML =
          `<div class="tb-detail-row" style="color:#556880;">Select an entry from the folder list above.</div>`;
        noteInput.value = "";
        noteInput.disabled = true;
        return;
      }

      const star = it.starred ? "★ " : "";
      document.getElementById("hdr-title").textContent = star + it.title;
      document.getElementById("hdr-repo").innerHTML = it.kind === "repo"
        ? `<a href="${escapeHtml(it.htmlUrl)}" target="_blank" rel="noopener">${escapeHtml(it.fullName)}</a>`
        : `<a href="${escapeHtml(it.siteUrl)}" target="_blank" rel="noopener">${escapeHtml(it.siteUrl)}</a>`;
      document.getElementById("hdr-cat").textContent = `${it.catLabel} (${it.entryId})`;

      document.getElementById("hdr-tick-btn").textContent =
        state.selectedIds.has(it.id) ? "✓ Untick" : "✓ Tick for Discord";
      document.getElementById("hdr-wl-btn").textContent =
        isWhitelisted(it) ? "🔥 Unwhitelist" : "🔥 Whitelist";

      document.getElementById("card-heading").textContent = `Card for ${star}${it.title}`;
      document.getElementById("card-tier-pill").textContent = formatStatusCell(it);

      let detailsHtml = "";
      if (it.kind === "repo") {
        detailsHtml += `<div class="tb-detail-row"><strong>GitHub Repository:</strong> <a href="${escapeHtml(it.htmlUrl)}" target="_blank" rel="noopener">${escapeHtml(it.htmlUrl)}</a></div>`;
      } else {
        detailsHtml += `<div class="tb-detail-row"><strong>Target URL:</strong> <a href="${escapeHtml(it.siteUrl)}" target="_blank" rel="noopener">${escapeHtml(it.siteUrl)}</a></div>`;
      }
      const ageTxt = formatAgeHuman(it.inactiveDays);
      const lastDateTxt = it.lastActivityAt ? ` (last push ${escapeHtml(it.lastActivityAt)})` : "";
      if (ageTxt) {
        detailsHtml += `<div class="tb-detail-row"><strong>Time Since Last Update:</strong> <strong>${escapeHtml(ageTxt)}</strong>${lastDateTxt}</div>`;
      }
      detailsHtml += `<div class="tb-detail-row"><strong>Status Detail:</strong> ${escapeHtml(it.reason)}</div>`;

      if (it.webUrls && it.webUrls.length > 0) {
        const wlinks = it.webUrls.map(u => {
          const tag = isPagesUrl(u) ? "[GitHub Pages]" : "[Website]";
          return `${tag} <a href="${escapeHtml(u)}" target="_blank" rel="noopener">${escapeHtml(u)}</a>`;
        }).join("<br/>");
        detailsHtml += `<div class="tb-detail-row"><strong>Attached Web / Pages:</strong><br/>${wlinks}</div>`;
      } else {
        detailsHtml += `<div class="tb-detail-row"><strong>Attached Web / Pages:</strong> <span style="color:#666;">None linked</span></div>`;
      }

      if (it.replacementUrl) {
        const st = it.replacementStatus ? ` (${escapeHtml(it.replacementStatus)})` : "";
        detailsHtml += `<div class="tb-detail-row" style="background:#e9f7ed;border:1px solid #8fd19e;padding:4px 6px;margin-top:6px;">
          <strong style="color:#0b6b24;">↪ Detected Replacement Repo:</strong>
          <a href="${escapeHtml(it.replacementUrl)}" target="_blank" rel="noopener"><strong>${escapeHtml(it.replacementUrl)}</strong></a>${st}
        </div>`;
      }

      document.getElementById("card-details").innerHTML = detailsHtml;

      noteInput.disabled = false;
      noteInput.value = state.notes[it.id] || "";
      noteInput.oninput = () => {
        if (noteInput.value.trim()) state.notes[it.id] = noteInput.value;
        else delete state.notes[it.id];
        saveLocalState();
        if (state.selectedIds.has(it.id)) renderDiscordOutbox();
      };
    }

    function buildDiscordMarkdown() {
      const selected = state.items
        .filter(it => state.selectedIds.has(it.id))
        .sort((a, b) => b.sortDays - a.sortDays);
      document.getElementById("selected-count").textContent = `${selected.length} ticked`;
      if (selected.length === 0) return "";

      const groups = [
        { title: "Dead / 404 / DMCA GitHub Repos", filter: it => it.kind === "repo" && it.tier === "missing" },
        { title: "Archived Repos (New Repo Found)", filter: it => it.kind === "repo" && it.tier === "archived" && it.replacementUrl },
        { title: "Archived GitHub Repos", filter: it => it.kind === "repo" && it.tier === "archived" && !it.replacementUrl },
        { title: "Inactive GitHub Repos", filter: it => it.kind === "repo" && !["missing", "archived"].includes(it.tier) },
        { title: "Dead / Parked Websites", filter: it => it.kind === "site" },
      ];

      const lines = ["## FMHY Drift Report"];
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
              ? ` | -> New Repo: <${it.replacementUrl}>${it.replacementStatus ? ` (${it.replacementStatus})` : ""}`
              : "";
            const ageStr = formatAgeHuman(it.inactiveDays);
            const datePart = it.lastActivityAt ? `, last push ${it.lastActivityAt}` : "";
            const detail = it.tier === "missing"
              ? (ageStr ? `${it.reason} — ${ageStr} without updates` : it.reason)
              : (it.tier === "archived"
                  ? `archived${ageStr ? ` — ${ageStr} without updates${datePart}` : ""}`
                  : `${ageStr || (it.inactiveDays + "d")} without updates${datePart}`);
            lines.push(`- ${star}**${it.title}** (\`${it.catLabel}\`) — <${it.htmlUrl}> (${detail})${webPart}${replPart}${noteSuffix}`);
          } else {
            lines.push(`- ${star}**${it.title}** (\`${it.catLabel}\`) — <${it.siteUrl}> (${it.reason})${noteSuffix}`);
          }
        }
      }
      return lines.join("\n");
    }

    function renderDiscordOutbox() {
      document.getElementById("discord-preview").value = buildDiscordMarkdown();
      document.getElementById("wl-count").textContent =
        `${state.whitelistRepos.size + state.whitelistUrls.size} whitelisted`;
    }

    function renderAll() {
      renderFolderTree();
      renderTable();
      renderInspector();
      renderDiscordOutbox();
    }

    function toggleFocusedTick() {
      if (!state.focusedId) return;
      if (state.selectedIds.has(state.focusedId)) state.selectedIds.delete(state.focusedId);
      else state.selectedIds.add(state.focusedId);
      saveLocalState();
      renderAll();
    }

    function toggleFocusedWhitelist() {
      const it = state.items.find(x => x.id === state.focusedId);
      if (!it) return;
      const listBefore = filteredAndSortedItems();
      const idx = listBefore.findIndex(x => x.id === it.id);

      const targetSet = it.kind === "repo" ? state.whitelistRepos : state.whitelistUrls;
      if (isWhitelisted(it)) targetSet.delete(it.wlKey);
      else {
        targetSet.add(it.wlKey);
        state.selectedIds.delete(it.id);
      }
      saveLocalState();

      const listAfter = filteredAndSortedItems();
      if (listAfter.length > 0 && idx >= 0) {
        state.focusedId = listAfter[Math.min(idx, listAfter.length - 1)].id;
      }
      renderAll();
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

    // Column sorting
    document.querySelectorAll("th[data-sort]").forEach(th => {
      th.onclick = () => {
        const field = th.getAttribute("data-sort");
        if (state.sortField === field) state.sortAsc = !state.sortAsc;
        else { state.sortField = field; state.sortAsc = field === "title" || field === "repo" || field === "cat"; }
        renderTable();
      };
    });

    // Keyboard navigation (Thunderbird shortcuts: Up/Down, Space to tick, J to Junk/Whitelist)
    document.addEventListener("keydown", (e) => {
      const tag = (document.activeElement?.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea") return;

      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const all = filteredAndSortedItems();
        const slice = all.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE);
        if (!slice.length) return;
        const idx = slice.findIndex(x => x.id === state.focusedId);
        const nextIdx = e.key === "ArrowDown"
          ? Math.min(slice.length - 1, idx + 1)
          : Math.max(0, idx - 1);
        state.focusedId = slice[nextIdx].id;
        renderTable();
        renderInspector();
      } else if (e.key === " ") {
        e.preventDefault();
        toggleFocusedTick();
      } else if (e.key === "j" || e.key === "J") {
        e.preventDefault();
        toggleFocusedWhitelist();
      }
    });

    // Toolbar & Menu bindings
    document.getElementById("btn-reload").onclick = () => loadData();
    document.getElementById("btn-toggle-tick").onclick = () => toggleFocusedTick();
    document.getElementById("hdr-tick-btn").onclick = () => toggleFocusedTick();
    document.getElementById("btn-junk-wl").onclick = () => toggleFocusedWhitelist();
    document.getElementById("hdr-wl-btn").onclick = () => toggleFocusedWhitelist();

    document.getElementById("search").oninput = () => { state.page = 0; renderTable(); };
    document.getElementById("starred-only").onchange = () => { state.page = 0; renderTable(); };
    document.getElementById("has-web-only").onchange = () => { state.page = 0; renderTable(); };

    document.getElementById("prev-pg").onclick = () => { if (state.page > 0) { state.page--; renderTable(); } };
    document.getElementById("next-pg").onclick = () => { state.page++; renderTable(); };

    document.getElementById("menu-tick-page").onclick = () => {
      const all = filteredAndSortedItems();
      all.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE).forEach(it => state.selectedIds.add(it.id));
      saveLocalState();
      renderAll();
    };
    document.getElementById("menu-clear-ticks").onclick = () => {
      state.selectedIds.clear();
      saveLocalState();
      renderAll();
    };
    document.getElementById("menu-outbox").onclick = () => {
      state.activeFolder = "outbox";
      state.page = 0;
      renderAll();
    };

    async function copyReport() {
      const txt = document.getElementById("discord-preview").value;
      if (!txt) { setStatus("Nothing ticked yet."); return; }
      await navigator.clipboard.writeText(txt);
      setStatus("Copied Discord Markdown to clipboard.");
    }
    document.getElementById("copy-md-btn").onclick = copyReport;
    document.getElementById("btn-copy-md").onclick = copyReport;

    document.getElementById("download-md-btn").onclick = () => {
      const txt = document.getElementById("discord-preview").value;
      if (!txt) return;
      const blob = new Blob([txt], { type: "text/markdown;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "fmhy-curated-drift.md";
      a.click();
    };

    async function sendToDiscord() {
      const webhook = document.getElementById("webhook-url").value.trim();
      const content = document.getElementById("discord-preview").value.trim();
      if (!webhook) { setStatus("Enter a Discord Webhook URL first."); return; }
      if (!content) { setStatus("Tick at least one entry first."); return; }
      localStorage.setItem("fmhy_discord_webhook", webhook);

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

      setStatus(`Sending ${chunks.length} message(s) to Discord...`);
      try {
        for (const chunk of chunks) {
          const r = await fetch(webhook, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ content: chunk, flags: 4 }),
          });
          if (!r.ok) throw new Error(`Discord HTTP ${r.status}`);
        }
        setStatus(`Sent ${chunks.length} message(s) to Discord.`);
      } catch (err) {
        setStatus(`Discord send failed: ${err.message}`);
      }
    }
    document.getElementById("send-discord-btn").onclick = sendToDiscord;
    document.getElementById("btn-send-discord").onclick = sendToDiscord;

    async function openWhitelistEditor() {
      await navigator.clipboard.writeText(buildWhitelistJson());
      setStatus("Copied whitelist.json to clipboard — opening GitHub editor...");
      window.open(`https://github.com/${REPO_SLUG}/edit/main/whitelist.json`, "_blank");
    }
    document.getElementById("copy-wl-btn").onclick = openWhitelistEditor;
    document.getElementById("menu-copy-wl").onclick = openWhitelistEditor;

    async function saveWhitelistToGithub() {
      const pat = document.getElementById("gh-pat").value.trim();
      if (!pat) {
        await openWhitelistEditor();
        return;
      }
      localStorage.setItem("fmhy_gh_pat", pat);
      setStatus("Committing whitelist.json to GitHub...");
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
        setStatus("Committed whitelist.json to main.");
      } catch (err) {
        setStatus(`Commit failed: ${err.message}`);
      }
    }
    document.getElementById("save-gh-btn").onclick = saveWhitelistToGithub;
    document.getElementById("btn-save-wl").onclick = saveWhitelistToGithub;

    loadData();
  </script>
</body>
</html>
"""
