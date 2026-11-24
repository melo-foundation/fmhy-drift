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

    /* Big Icon Mail Toolbar */
    .tb-toolbar {
      min-height: 50px;
      background: linear-gradient(180deg, #f6f9fd 0%, #e1eaf6 50%, #d2dfef 51%, #dfe9f7 100%);
      border-bottom: 1px solid #a9b9cd;
      box-shadow: inset 0 1px 0 #ffffff;
      display: flex;
      align-items: center;
      padding: 2px 8px;
      gap: 3px;
      flex-wrap: wrap;
    }
    .tb-tool-btn {
      display: inline-flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-width: 54px;
      height: 43px;
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
      width: 21px;
      height: 21px;
      display: block;
    }
    .tb-sep {
      width: 2px;
      height: 34px;
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
      flex-wrap: wrap;
    }
    .tb-filter-group label {
      display: inline-flex;
      align-items: center;
      gap: 3px;
      cursor: pointer;
    }
    .tb-source-select {
      height: 23px;
      padding: 1px 5px;
      font-size: 11px;
      font-weight: 600;
      border: 1px solid #5f82ab;
      background: #ffffff;
      color: #14273d;
    }
    .tb-search-wrap {
      margin-left: auto;
      display: flex;
      align-items: center;
      background: #ffffff;
      border: 1px solid #7f9db9;
      box-shadow: inset 0 1px 2px rgba(0,0,0,0.08);
      height: 23px;
      width: 225px;
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

    /* Classic XUL Quick-Filter / Live Thresholds Bar */
    .tb-threshbar {
      min-height: 26px;
      background: linear-gradient(180deg, #eef3fa 0%, #dde7f4 100%);
      border-bottom: 1px solid #96a8c0;
      box-shadow: inset 0 1px 0 #ffffff;
      display: flex;
      align-items: center;
      padding: 2px 10px;
      gap: 8px;
      font-size: 11px;
      color: #1c2c3f;
      flex-wrap: wrap;
    }
    .tb-thresh-title {
      font-weight: 700;
      color: #20344d;
    }
    .tb-thresh-field {
      display: inline-flex;
      align-items: center;
      gap: 3px;
    }
    .tb-num-input {
      width: 52px;
      height: 20px;
      border: 1px solid #7f9db9;
      box-shadow: inset 0 1px 2px rgba(0,0,0,0.08);
      background: #ffffff;
      font-family: Tahoma, "Segoe UI", sans-serif;
      font-size: 11px;
      padding: 1px 4px;
      color: #111;
      text-align: right;
    }
    .tb-thresh-sep {
      color: #8fa3bd;
      padding: 0 2px;
    }
    .tb-thresh-hint {
      margin-left: auto;
      color: #3c526d;
      font-size: 10.5px;
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
      grid-template-rows: minmax(210px, 53%) minmax(210px, 47%);
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
    .col-star { width: 30px; text-align: center !important; padding: 0 2px !important; }
    .col-clip { width: 30px; text-align: center !important; padding: 0 2px !important; }
    .col-subject { width: 20%; }
    .col-repo { width: 19%; }
    .col-src { width: 11%; }
    .col-web { width: 21%; }
    .col-cat { width: 12%; }
    .col-status { width: 17%; }

    /* Crystal-clear FMHY Explicit vs Heuristic Source Pills */
    .src-pill {
      display: inline-block;
      padding: 0 5px;
      border-radius: 2px;
      font-size: 10.5px;
      font-weight: 600;
      border: 1px solid #1b6e2d;
      background: #dff4e4;
      color: #1b6e2d;
    }
    .src-pill.heur {
      background: #fff4d6;
      border: 1px dashed #b58105;
      color: #6b4900;
    }
    tr.selected .src-pill {
      background: rgba(255,255,255,0.22);
      border-color: rgba(255,255,255,0.75);
      color: #ffffff;
    }

    .star-on { color: #e69c00; font-size: 13px; }
    .star-off { color: #b8c4d4; font-size: 11px; }
    .bold-row td.col-subject, .bold-row td.col-repo { font-weight: 700; }
    .muted-cell { color: #4a5a6e; }
    .row-link {
      color: #0044aa;
      text-decoration: underline;
      cursor: pointer;
    }
    .row-link:hover { color: #0066ff; }
    tr.selected .row-link { color: #ffffff !important; }
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
    .tb-src-banner-ok {
      background: #dff4e4;
      border: 1px solid #1b6e2d;
      color: #145322;
      padding: 4px 7px;
      margin: 4px 0 6px 0;
      font-weight: 600;
      font-size: 11.5px;
    }
    .tb-src-banner-warn {
      background: #fff4d6;
      border: 1px dashed #b58105;
      color: #6b4900;
      padding: 4px 7px;
      margin: 4px 0 6px 0;
      font-weight: 600;
      font-size: 11.5px;
    }
    .tb-raw-line-hdr {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-top: 7px;
      margin-bottom: 3px;
      font-size: 11px;
    }
    .tb-raw-line-box {
      background: #f3f6fb;
      border: 1px solid #9eb0c7;
      box-shadow: inset 0 1px 2px rgba(0,0,0,0.08);
      padding: 4px 6px;
      font-family: Consolas, "Liberation Mono", monospace;
      font-size: 11px;
      color: #1a2533;
      white-space: pre-wrap;
      word-break: break-all;
      max-height: 68px;
      overflow-y: auto;
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
    .tb-muted-note { color: #556880; }
    .tb-action-status { margin-top: 5px; font-size: 11px; color: #224477; }
    .tb-repl-box {
      background: #e9f7ed;
      border: 1px solid #8fd19e;
      padding: 4px 6px;
      margin-top: 6px;
    }
    .tb-repl-title { color: #0b6b24; }

    /* Classic Dark Thunderbird XUL Theme (default) */
    html.dark, html.dark body {
      background: #12161c;
      color: #e2e9f2;
      color-scheme: dark;
    }
    html.dark a { color: #6cb4ff; }
    html.dark a:hover { color: #96cbff; }
    html.dark .tb-window {
      border-color: #344154;
      background: #1b212a;
    }
    html.dark .tb-titlebar {
      background: linear-gradient(180deg, #2c3646 0%, #202835 48%, #19202b 52%, #222b39 100%);
      border-bottom-color: #354357;
      color: #e2ebf7;
      text-shadow: 0 1px 0 rgba(0,0,0,0.65);
    }
    html.dark .tb-win-btn {
      border-color: #485a73;
      background: linear-gradient(180deg, #313d4f 0%, #1f2734 100%);
      color: #d4e0f0;
    }
    html.dark .tb-menubar {
      background: linear-gradient(180deg, #252e3c 0%, #1d2430 100%);
      border-bottom-color: #323f52;
    }
    html.dark .tb-menu-item { color: #dce6f2; }
    html.dark .tb-menu-item:hover {
      background: linear-gradient(180deg, #324055 0%, #253244 100%);
      border-color: #4e6788;
    }
    html.dark .tb-toolbar {
      background: linear-gradient(180deg, #2a3342 0%, #212936 50%, #1b222d 51%, #202835 100%);
      border-bottom-color: #323f52;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.06);
    }
    html.dark .tb-tool-btn { color: #dce6f2; }
    html.dark .tb-tool-btn:hover {
      background: linear-gradient(180deg, rgba(95,135,190,0.25) 0%, rgba(55,85,125,0.28) 100%);
      border-color: #4e698e;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.08);
    }
    html.dark .tb-tool-btn:active {
      background: #171e28;
      border-color: #3f5778;
    }
    html.dark .tb-sep {
      border-left-color: #12171f;
      border-right-color: #364357;
    }
    html.dark .tb-filter-group { color: #d4dfee; }
    html.dark .tb-source-select,
    html.dark .tb-search-wrap,
    html.dark .tb-num-input,
    html.dark .tb-input,
    html.dark .tb-textarea {
      background: #131820;
      border-color: #3e5069;
      color: #e6edf5;
      box-shadow: inset 0 1px 2px rgba(0,0,0,0.35);
    }
    html.dark .tb-search-wrap input { color: #e6edf5; }
    html.dark .tb-threshbar {
      background: linear-gradient(180deg, #222a37 0%, #1b222d 100%);
      border-bottom-color: #323f52;
      box-shadow: none;
      color: #cbd6e6;
    }
    html.dark .tb-thresh-title { color: #9ec3f0; }
    html.dark .tb-thresh-sep { color: #495a73; }
    html.dark .tb-thresh-hint { color: #90a3bd; }
    html.dark .tb-workspace { background: #12161d; }
    html.dark .tb-folder-pane,
    html.dark .tb-thread-pane,
    html.dark .tb-preview-pane {
      background: #1b212a;
      border-color: #344154;
      color: #e2e9f2;
    }
    html.dark .tb-pane-header {
      background: linear-gradient(180deg, #293241 0%, #1e2632 100%);
      border-bottom-color: #344154;
      color: #d6e1f0;
    }
    html.dark .tb-tree-root,
    html.dark .tb-folder-item { color: #dce6f2; }
    html.dark .tb-folder-item:hover { background: #242e3d; }
    html.dark .tb-folder-item.active {
      background: linear-gradient(180deg, #274c7d 0%, #1e3c63 100%);
      border-color: #4978b5;
      color: #ffffff;
    }
    html.dark .tb-cat-divider {
      border-top-color: #2b3545;
      color: #9ec3f0;
    }
    html.dark table.tb-table thead th {
      background: linear-gradient(180deg, #2a3342 0%, #202836 60%, #1b222e 100%);
      border-bottom-color: #38475c;
      border-right-color: #2f3b4d;
      color: #d8e2f0;
    }
    html.dark table.tb-table thead th:hover {
      background: linear-gradient(180deg, #323e52 0%, #263142 100%);
    }
    html.dark table.tb-table tbody tr {
      border-bottom-color: #232b37;
    }
    html.dark table.tb-table tbody tr:nth-child(even) {
      background: #171c24;
    }
    html.dark table.tb-table tbody tr:hover {
      background: #232e3f;
    }
    html.dark table.tb-table tbody tr.ticked:not(.selected) {
      background: #2d2716;
    }
    html.dark table.tb-table tbody tr.selected {
      background: #24559c !important;
      color: #ffffff !important;
    }
    html.dark .src-pill {
      background: #133620;
      border-color: #2ea043;
      color: #7ee787;
    }
    html.dark .src-pill.heur {
      background: #38290c;
      border-color: #bb8009;
      color: #f2cc60;
    }
    html.dark .star-on { color: #f5b427; }
    html.dark .star-off { color: #4d5d73; }
    html.dark .muted-cell, html.dark .tb-muted-note { color: #8fa0b8; }
    html.dark .row-link { color: #6cb4ff; }
    html.dark .row-link:hover { color: #9acdff; }
    html.dark .repl-tag, html.dark .tb-repl-title { color: #56d364; }
    html.dark .tb-msg-header {
      background: linear-gradient(180deg, #242d3b 0%, #1c2430 100%);
      border-bottom-color: #344154;
    }
    html.dark .tb-hdr-label { color: #90a3bd; }
    html.dark .tb-card-heading {
      color: #f0f5fc;
      border-bottom-color: #495b73;
    }
    html.dark .tb-blue-bar {
      background: #255896;
      color: #ffffff;
    }
    html.dark .tb-src-banner-ok {
      background: #12301d;
      border-color: #2ea043;
      color: #7ee787;
    }
    html.dark .tb-src-banner-warn {
      background: #36280d;
      border-color: #bb8009;
      color: #f2cc60;
    }
    html.dark .tb-repl-box {
      background: #122e1c;
      border-color: #2ea043;
      color: #d8f5df;
    }
    html.dark .tb-raw-line-box {
      background: #12161d;
      border-color: #344154;
      color: #cbd6e6;
      box-shadow: inset 0 1px 2px rgba(0,0,0,0.4);
    }
    html.dark .tb-classic-btn {
      border-color: #475973;
      background: linear-gradient(180deg, #2d3747 0%, #212936 100%);
      color: #e2e9f2;
    }
    html.dark .tb-classic-btn:hover {
      background: linear-gradient(180deg, #374458 0%, #283344 100%);
      border-color: #6382ab;
    }
    html.dark .tb-classic-btn.primary { border-color: #4f84c4; }
    html.dark .tb-action-status { color: #79b8ff; }
    html.dark .tb-statusbar {
      background: linear-gradient(180deg, #232b38 0%, #1a212b 100%);
      border-top-color: #344154;
      color: #c8d4e3;
    }
    html.dark .tb-status-panel { border-left-color: #344154; }
    html.dark .tb-pager-btns button {
      border-color: #465770;
      background: #252f3f;
      color: #dce6f2;
    }
  </style>
  <script>
    (function() {
      const saved = localStorage.getItem("fmhy_theme");
      if (saved !== "light") {
        document.documentElement.classList.add("dark");
      }
    })();
  </script>
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
      <a class="tb-menu-item" href="./fmhy-drift-report.md" target="_blank" rel="noopener"><u>F</u>ile (.md)</a>
      <a class="tb-menu-item" href="./fmhy-drift-report.json" target="_blank" rel="noopener"><u>D</u>ata (.json)</a>
      <span class="tb-menu-item" id="menu-tick-page"><u>E</u>dit (Tick Page)</span>
      <span class="tb-menu-item" id="menu-clear-ticks"><u>C</u>lear Ticks</span>
      <span class="tb-menu-item" id="menu-outbox"><u>M</u>essage Outbox</span>
      <span class="tb-menu-item" id="menu-copy-wl"><u>T</u>ools (Edit whitelist.json)</span>
      <span class="tb-menu-item" id="menu-theme-toggle"><u>V</u>iew (☀️ Light / 🌙 Dark)</span>
      <a class="tb-menu-item" id="menu-help-gh" href="https://github.com/melo-foundation/fmhy-drift" target="_blank" rel="noopener"><u>H</u>elp (GitHub)</a>
      <div class="tb-menubar-spacer"></div>
      <span class="tb-muted-note" style="font-size:10.5px;padding-right:6px;">Keys: ↑/↓ Navigate · Space Tick · J Whitelist</span>
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
        <label><input type="checkbox" id="starred-only" /> ★ Starred</label>
        <label><input type="checkbox" id="has-web-only" /> 📎 Has Site/Pages</label>
        <select id="source-filter" class="tb-source-select" title="Filter between repos actually linked in FMHY vs inferred via URL heuristics">
          <option value="">FMHY Link: All</option>
          <option value="explicit">✓ Has Actual GitHub in FMHY Only</option>
          <option value="heuristic">? Guessed / Not in FMHY Only</option>
        </select>
      </div>

      <div class="tb-search-wrap">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
          <circle cx="10.5" cy="10.5" r="6.5" stroke="#68809c" stroke-width="2"/>
          <path d="M15.5 15.5L21 21" stroke="#68809c" stroke-width="2" stroke-linecap="round"/>
        </svg>
        <input type="text" id="search" placeholder="Subject, Repo, or Domain" />
      </div>
    </div>

    <!-- Live Interactive Thresholds & Min-Days Quick Filter Bar -->
    <div class="tb-threshbar">
      <span class="tb-thresh-title">Thresholds (days):</span>
      <label class="tb-thresh-field" title="Notice tier threshold in days (lower below 90 down to 0 to inspect active repos)">
        Notice &gt;= <input type="number" id="thresh-t1" class="tb-num-input" min="0" max="99999" step="1" value="90" />
      </label>
      <label class="tb-thresh-field" title="Warning tier threshold in days">
        Warn &gt;= <input type="number" id="thresh-t2" class="tb-num-input" min="0" max="99999" step="1" value="180" />
      </label>
      <label class="tb-thresh-field" title="Critical tier threshold in days">
        Crit &gt;= <input type="number" id="thresh-t3" class="tb-num-input" min="0" max="99999" step="1" value="365" />
      </label>
      <span class="tb-thresh-sep">|</span>
      <label class="tb-thresh-field" title="Filter current folder immediately to repos with >= X inactive days (e.g. 500 or 730)">
        <strong>Min Age (days):</strong> <input type="number" id="min-days" class="tb-num-input" min="0" max="99999" step="1" value="0" />
      </label>
      <button type="button" class="tb-classic-btn" id="btn-reset-thresh" style="padding:1px 7px;font-size:10.5px;">Reset</button>
      <span class="tb-thresh-hint" id="thresh-summary">Live tier calculation across all checked repos</span>
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
                  <th class="col-src" data-sort="source">FMHY Link?</th>
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
                <div class="tb-detail-row tb-muted-note">Click any row in the list above (or press ↑/↓) to inspect its GitHub repository, FMHY link provenance, literal FMHY markdown line, attached website / GitHub Pages link, and detected replacement link.</div>
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
              <div id="action-status" class="tb-action-status"></div>
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
    function detectRepoSlug() {
      try {
        const host = (window.location.hostname || "").toLowerCase();
        if (host.endsWith(".github.io")) {
          const owner = host.slice(0, -".github.io".length);
          const seg = (window.location.pathname || "").split("/").filter(Boolean)[0];
          if (owner && seg) return `${owner}/${seg}`;
        }
      } catch {}
      return "melo-foundation/fmhy-drift";
    }

    const REPO_SLUG = detectRepoSlug();
    const PAGE_SIZE = 100;

    function normalizeRepoKey(raw) {
      if (!raw || typeof raw !== "string") return "";
      let s = raw.trim();
      if (!s) return "";
      s = s.replace(/^https?:\/\/(?:www\.)?github\.com\//i, "");
      s = s.split(/[?#]/)[0].replace(/^\/+|\/+$/g, "");
      const parts = s.split("/").filter(Boolean);
      if (parts.length < 2) return "";
      const owner = parts[0].toLowerCase();
      const repo = parts[1].replace(/\.git$/i, "").toLowerCase();
      return (owner && repo) ? `${owner}/${repo}` : "";
    }

    function normalizeUrlKey(raw) {
      if (!raw || typeof raw !== "string") return "";
      let s = raw.trim().replace(/[.,;:)>\]"']+$/g, "");
      if (!s) return "";
      try {
        const u = new URL(s);
        if (!/^https?:$/i.test(u.protocol)) return "";
        u.hostname = u.hostname.toLowerCase();
        if ((u.protocol === "https:" && u.port === "443") || (u.protocol === "http:" && u.port === "80")) {
          u.port = "";
        }
        let out = u.toString();
        if (u.pathname !== "/" && out.endsWith("/")) out = out.slice(0, -1);
        return out;
      } catch {
        return s;
      }
    }

    function safeHref(url) {
      if (!url || typeof url !== "string") return "";
      const trimmed = url.trim();
      return /^https?:\/\//i.test(trimmed) ? trimmed : "";
    }

    function safeParseJson(str, fallback) {
      try { return JSON.parse(str); }
      catch { return fallback; }
    }

    const state = {
      items: [],
      categories: [],
      activeFolder: "inbox_all",
      activeCategory: "",
      sortField: "days",
      sortAsc: false,
      page: 0,
      focusedId: null,
      t1: 90,
      t2: 180,
      t3: 365,
      minDays: 0,
      reportDefaults: { t1: 90, t2: 180, t3: 365 },
      thresholdsCustomized: false,
      selectedIds: new Set(safeParseJson(localStorage.getItem("fmhy_selected_ids") || "[]", [])),
      notes: safeParseJson(localStorage.getItem("fmhy_item_notes") || "{}", {}),
      whitelistRepos: new Set(
        safeParseJson(localStorage.getItem("fmhy_wl_repos") || "[]", [])
          .map(normalizeRepoKey)
          .filter(Boolean)
      ),
      whitelistUrls: new Set(
        safeParseJson(localStorage.getItem("fmhy_wl_urls") || "[]", [])
          .map(normalizeUrlKey)
          .filter(Boolean)
      ),
      whitelistNotes: {},
      whitelistDirty: localStorage.getItem("fmhy_wl_dirty") === "1",
    };

    const helpLinkEl = document.getElementById("menu-help-gh");
    if (helpLinkEl) helpLinkEl.href = `https://github.com/${REPO_SLUG}`;
    document.getElementById("webhook-url").value = localStorage.getItem("fmhy_discord_webhook") || "";
    document.getElementById("gh-pat").value = localStorage.getItem("fmhy_gh_pat") || "";

    function getFolders() {
      return [
        { id: "inbox_all", label: "Inbox (All Flagged)", icon: "inbox", tip: "All non-whitelisted dead, archived, dead-site, and inactive (>= Notice threshold) entries" },
        { id: "missing", label: "Dead / 404 Repos", icon: "inbox", tip: "404, DMCA 451, or disabled GitHub repositories" },
        { id: "archived_moved", label: "Archived — Moved", icon: "moved", tip: "Archived repositories with a detected replacement repository link" },
        { id: "archived", label: "All Archived", icon: "folder", tip: "All archived GitHub repositories" },
        { id: "tier3_critical", label: `Inactive >=${state.t3}d`, icon: "folder", tip: `Critical inactivity tier: >= ${state.t3} days since last push/release` },
        { id: "tier2_warning", label: `Inactive >=${state.t2}d`, icon: "folder", tip: `Warning inactivity tier: ${state.t2}–${Math.max(state.t2, state.t3 - 1)} days since last push/release` },
        { id: "tier1_notice", label: `Inactive >=${state.t1}d`, icon: "folder", tip: `Notice inactivity tier: ${state.t1}–${Math.max(state.t1, state.t2 - 1)} days since last push/release` },
        { id: "dead_sites", label: "Dead / Parked Sites", icon: "folder", tip: "Dead, parked, or TLS-failing website URLs" },
        { id: "outbox", label: "Discord Outbox", icon: "outbox", tip: "Entries ticked for curated Discord report" },
        { id: "whitelisted", label: "Whitelisted", icon: "junk", tip: "Ignored / whitelisted repositories and URLs" },
      ];
    }

    function effectiveTier(it) {
      if (it.kind === "site") return "dead_sites";
      if (it.rawTier === "missing" || it.isDisabled) return "missing";
      if (it.rawTier === "archived" || it.isArchived) return "archived";
      if (it.inactiveDays == null) return "tier3_critical";
      if (it.inactiveDays >= state.t3) return "tier3_critical";
      if (it.inactiveDays >= state.t2) return "tier2_warning";
      if (it.inactiveDays >= state.t1) return "tier1_notice";
      return "active";
    }

    function saveLocalState() {
      localStorage.setItem("fmhy_selected_ids", JSON.stringify([...state.selectedIds]));
      localStorage.setItem("fmhy_item_notes", JSON.stringify(state.notes));
      localStorage.setItem("fmhy_wl_repos", JSON.stringify([...state.whitelistRepos]));
      localStorage.setItem("fmhy_wl_urls", JSON.stringify([...state.whitelistUrls]));
      if (state.whitelistDirty) localStorage.setItem("fmhy_wl_dirty", "1");
      else localStorage.removeItem("fmhy_wl_dirty");
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
      if (item.kind === "repo") {
        return state.whitelistRepos.has(item.wlKey) || Boolean(item.origKey && state.whitelistRepos.has(item.origKey));
      }
      return (
        state.whitelistUrls.has(item.wlKey) ||
        Boolean(item.origKey && state.whitelistUrls.has(item.origKey))
      );
    }

    function formatSourceBadge(src) {
      if (!src || src === "explicit_markdown") {
        return {
          short: "✓ In FMHY",
          full: "✅ Directly linked in FMHY listing",
          discordTag: "in FMHY",
          isHeur: false,
        };
      }
      if (src === "github_pages") {
        return {
          short: "? github.io",
          full: "⚠️ NOT in FMHY — guessed via *.github.io heuristic",
          discordTag: "guessed: github.io — not in FMHY",
          isHeur: true,
        };
      }
      if (src === "github_cdn") {
        return {
          short: "? Raw/CDN",
          full: "⚠️ NOT in FMHY — guessed from raw/CDN URL",
          discordTag: "guessed: raw/CDN — not in FMHY",
          isHeur: true,
        };
      }
      if (src === "domain_heuristic") {
        return {
          short: "? Domain",
          full: "⚠️ NOT in FMHY — guessed from subdomain",
          discordTag: "guessed: subdomain — not in FMHY",
          isHeur: true,
        };
      }
      if (src === "discovered_on_site") {
        return {
          short: "? Scraped",
          full: "⚠️ NOT in FMHY — scraped from website HTML",
          discordTag: "scraped from website — not in FMHY",
          isHeur: true,
        };
      }
      return {
        short: `? ${src}`,
        full: `⚠️ NOT in FMHY — ${src}`,
        discordTag: `guessed: ${src} — not in FMHY`,
        isHeur: true,
      };
    }

    function formatAgeParts(days) {
      if (days == null || !Number.isFinite(days) || days < 0 || days >= 999999) return null;
      const d = Math.floor(days);
      const yrs = Math.floor(d / 365);
      const rem = d % 365;
      const mos = Math.min(11, Math.floor(rem / 30));
      const remDays = rem - mos * 30;
      if (yrs >= 1) {
        const short = mos > 0 ? `${yrs}y ${mos}m` : `${yrs}y`;
        return { short, full: `${short} (${d}d)` };
      }
      if (mos >= 1) {
        const short = remDays > 0 ? `${mos}m ${remDays}d` : `${mos}m`;
        return { short, full: `${short} (${d}d)` };
      }
      return { short: `${d}d`, full: `${d}d` };
    }

    function formatAgeHuman(days) {
      const p = formatAgeParts(days);
      return p ? p.full : null;
    }

    function formatAgeShort(days) {
      const p = formatAgeParts(days);
      return p ? p.short : null;
    }

    function escapeHtml(s) {
      return String(s ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    function decodeBase64Utf8(b64) {
      const clean = String(b64 || "").replace(/\s+/g, "");
      const bin = atob(clean);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      return new TextDecoder("utf-8").decode(bytes);
    }

    function applyRemoteWhitelistPayload(wl) {
      if (!wl || typeof wl !== "object") return;
      const remoteRepos = new Set(
        (Array.isArray(wl.repos) ? wl.repos : []).map(normalizeRepoKey).filter(Boolean)
      );
      const remoteUrls = new Set(
        (Array.isArray(wl.urls) ? wl.urls : []).map(normalizeUrlKey).filter(Boolean)
      );
      const remoteNotes = (wl.notes && typeof wl.notes === "object" && !Array.isArray(wl.notes))
        ? wl.notes
        : {};

      if (!state.whitelistDirty) {
        state.whitelistRepos = remoteRepos;
        state.whitelistUrls = remoteUrls;
        state.whitelistNotes = remoteNotes;
        saveLocalState();
      } else {
        state.whitelistNotes = { ...remoteNotes, ...state.whitelistNotes };
      }
    }

    async function loadRemoteWhitelist() {
      const pat = document.getElementById("gh-pat").value.trim();
      const apiHeaders = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
      };
      if (pat) apiHeaders["Authorization"] = `Bearer ${pat}`;

      // 1. Try GitHub Contents API first (no 5-minute raw CDN staleness)
      try {
        const apiResp = await fetch(
          `https://api.github.com/repos/${REPO_SLUG}/contents/whitelist.json`,
          { headers: apiHeaders, cache: "no-store" }
        );
        if (apiResp.ok) {
          const meta = await apiResp.json();
          if (meta && typeof meta.content === "string") {
            const parsed = JSON.parse(decodeBase64Utf8(meta.content));
            applyRemoteWhitelistPayload(parsed);
            return;
          }
        }
      } catch {}

      // 2. Fallback to sibling ./whitelist.json or raw.githubusercontent.com
      const fallbackUrls = [
        "./whitelist.json",
        `https://raw.githubusercontent.com/${REPO_SLUG}/main/whitelist.json?t=${Date.now()}`,
      ];
      for (const u of fallbackUrls) {
        try {
          const r = await fetch(u, { cache: "no-store" });
          if (r.ok) {
            const wl = await r.json();
            applyRemoteWhitelistPayload(wl);
            return;
          }
        } catch {}
      }
    }

    async function loadData() {
      document.getElementById("run-meta").textContent = "Loading whitelist and drift report...";
      try {
        await loadRemoteWhitelist();

        const resp = await fetch(`./fmhy-drift-report.json?t=${Date.now()}`, { cache: "no-store" });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const report = await resp.json();
        ingestReport(report);
      } catch (err) {
        document.getElementById("run-meta").textContent = "Error loading report: " + err.message;
      }
    }

    function syncThresholdInputs() {
      document.getElementById("thresh-t1").value = String(state.t1);
      document.getElementById("thresh-t2").value = String(state.t2);
      document.getElementById("thresh-t3").value = String(state.t3);
      document.getElementById("min-days").value = String(state.minDays);
    }

    function ingestReport(report) {
      if (report.thresholds) {
        state.reportDefaults = {
          t1: report.thresholds.tier1_days ?? 90,
          t2: report.thresholds.tier2_days ?? 180,
          t3: report.thresholds.tier3_days ?? 365,
        };
        if (!state.thresholdsCustomized) {
          state.t1 = state.reportDefaults.t1;
          state.t2 = state.reportDefaults.t2;
          state.t3 = state.reportDefaults.t3;
          syncThresholdInputs();
        }
      }

      const dt = new Date(report.generated_at).toISOString().replace("T", " ").slice(0, 16) + " UTC";
      document.getElementById("run-meta").textContent =
        `Last scan: ${dt}  |  Mode: ${report.mode}  |  Checked: ${report.summary.unique_repos_checked} repos  |  Report defaults: ${state.reportDefaults.t1}d / ${state.reportDefaults.t2}d / ${state.reportDefaults.t3}d`;

      const items = [];
      const seenIds = new Set();
      const cats = new Set();

      for (const entry of report.entries || []) {
        if (entry.category) cats.add(entry.category);
        const catLabel = entry.subcategory ? `${entry.category} > ${entry.subcategory}` : entry.category;
        const rawLine = typeof entry.raw_line === "string" ? entry.raw_line : "";

        for (const rr of entry.github_repos || []) {
          if (rr.tier === "error") continue;
          const wlKey = normalizeRepoKey(rr.full_name) || String(rr.full_name || "").toLowerCase();
          const origKey = normalizeRepoKey(rr.requested_full_name || rr.full_name) || wlKey;
          const id = `repo:${entry.entry_id}:${wlKey}`;
          if (seenIds.has(id)) continue;
          seenIds.add(id);

          const mergedWeb = [...(entry.web_urls || [])];
          if (
            rr.homepage_url &&
            !mergedWeb.some(u => u.replace(/\/$/, "").toLowerCase() === rr.homepage_url.replace(/\/$/, "").toLowerCase())
          ) {
            mergedWeb.push(rr.homepage_url);
          }

          const isMissing = rr.tier === "missing" || Boolean(rr.is_disabled);
          const sortDays = isMissing
            ? 2_000_000 + (rr.inactive_days ?? 999_999)
            : (rr.inactive_days != null ? rr.inactive_days : -1);

          items.push({
            id,
            kind: "repo",
            wlKey,
            origKey,
            requestedFullName: rr.requested_full_name || rr.full_name,
            title: entry.title,
            starred: Boolean(entry.starred),
            category: entry.category,
            catLabel,
            entryId: entry.entry_id,
            rawLine,
            rawTier: rr.tier,
            isArchived: Boolean(rr.is_archived || rr.tier === "archived"),
            isDisabled: Boolean(rr.is_disabled),
            fullName: rr.full_name,
            htmlUrl: rr.html_url,
            source: rr.source || "explicit_markdown",
            inactiveDays: rr.inactive_days != null ? rr.inactive_days : null,
            lastActivityAt: rr.last_activity_at ? rr.last_activity_at.slice(0, 10) : null,
            archivedDays: rr.archived_days != null ? rr.archived_days : null,
            archivedAt: rr.archived_at ? rr.archived_at.slice(0, 10) : null,
            sortDays,
            reason: rr.reason || "",
            replacementUrl: rr.replacement_url || null,
            replacementStatus: rr.replacement_status || null,
            webUrls: mergedWeb,
          });
        }

        for (const sr of entry.sites || []) {
          if (!["dead", "parked", "tls_error"].includes(sr.status)) continue;
          const wlKey = normalizeUrlKey(sr.url) || String(sr.url || "").trim();
          const origKey = String(sr.url || "").trim();
          const id = `site:${entry.entry_id}:${wlKey}`;
          if (seenIds.has(id)) continue;
          seenIds.add(id);

          items.push({
            id,
            kind: "site",
            wlKey,
            origKey,
            title: entry.title,
            starred: Boolean(entry.starred),
            category: entry.category,
            catLabel,
            entryId: entry.entry_id,
            rawLine,
            rawTier: "dead_sites",
            fullName: "—",
            siteUrl: sr.url,
            siteStatus: sr.status,
            inactiveDays: null,
            lastActivityAt: null,
            sortDays: 1_500_000,
            reason: sr.reason || "",
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
        return state.items.filter(it => !isWhitelisted(it) && effectiveTier(it) !== "active");
      }
      return state.items.filter(it => {
        if (isWhitelisted(it)) return false;
        const tier = effectiveTier(it);
        if (folderId === "archived_moved") return it.kind === "repo" && tier === "archived" && Boolean(it.replacementUrl);
        if (folderId === "dead_sites") return it.kind === "site";
        return it.kind === "repo" && tier === folderId;
      });
    }

    function filteredAndSortedItems() {
      const q = document.getElementById("search").value.trim().toLowerCase();
      const starredOnly = document.getElementById("starred-only").checked;
      const hasWebOnly = document.getElementById("has-web-only").checked;
      const srcMode = document.getElementById("source-filter").value;
      const minDays = state.minDays;

      const list = itemsForFolder(state.activeFolder).filter(it => {
        if (state.activeCategory && it.category !== state.activeCategory) return false;
        if (starredOnly && !it.starred) return false;
        if (hasWebOnly && (!it.webUrls || it.webUrls.length === 0)) return false;
        if (srcMode === "explicit" && (it.kind !== "repo" || it.source !== "explicit_markdown")) return false;
        if (srcMode === "heuristic" && (it.kind !== "repo" || it.source === "explicit_markdown")) return false;
        if (minDays > 0 && (it.inactiveDays == null || it.inactiveDays < minDays)) return false;
        if (!q) return true;
        const hay = `${it.title} ${it.catLabel} ${it.fullName || ""} ${it.requestedFullName || ""} ${it.siteUrl || ""} ${it.replacementUrl || ""} ${it.source || ""} ${it.rawLine || ""} ${(it.webUrls || []).join(" ")}`.toLowerCase();
        return hay.includes(q);
      });

      const dir = state.sortAsc ? 1 : -1;
      list.sort((a, b) => {
        let va, vb;
        if (state.sortField === "starred") { va = a.starred ? 1 : 0; vb = b.starred ? 1 : 0; }
        else if (state.sortField === "links") { va = (a.webUrls?.length || 0) + (a.replacementUrl ? 5 : 0); vb = (b.webUrls?.length || 0) + (b.replacementUrl ? 5 : 0); }
        else if (state.sortField === "title") { return dir * a.title.localeCompare(b.title); }
        else if (state.sortField === "repo") { return dir * (a.fullName || "").localeCompare(b.fullName || ""); }
        else if (state.sortField === "source") { return dir * (a.source || "").localeCompare(b.source || ""); }
        else if (state.sortField === "cat") { return dir * a.catLabel.localeCompare(b.catLabel); }
        else if (state.sortField === "web") { va = a.replacementUrl || a.webUrls?.[0] || ""; vb = b.replacementUrl || b.webUrls?.[0] || ""; return dir * va.localeCompare(vb); }
        else {
          va = a.sortDays;
          vb = b.sortDays;
        }
        if (va !== vb) return dir * (va - vb);
        return a.title.localeCompare(b.title);
      });
      return list;
    }

    function renderFolderTree() {
      const folders = getFolders();
      const tree = document.getElementById("folder-tree");
      let html = `<div class="tb-tree-root">▾ ${folderSvg("folder")} <span>Local Drift Folders</span></div>`;

      for (const f of folders) {
        const cnt = itemsForFolder(f.id).length;
        const activeCls = state.activeFolder === f.id ? " active" : "";
        const boldCls = cnt > 0 ? " has-items" : "";
        const countSuffix = cnt > 0 ? ` (${cnt})` : "";
        html += `<div class="tb-folder-item${activeCls}${boldCls}" data-folder="${f.id}" title="${escapeHtml(f.tip || f.label)}">
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

      const activeFolderObj = folders.find(x => x.id === state.activeFolder);
      document.getElementById("window-title").textContent =
        `${activeFolderObj ? activeFolderObj.label : "Drift"} - FMHY Drift Thunderbird`;

      const totalLoadedRepos = state.items.filter(it => it.kind === "repo").length;
      const flaggedCount = itemsForFolder("inbox_all").length;
      const minSuffix = state.minDays > 0 ? ` · Min Age >= ${state.minDays}d active` : "";
      document.getElementById("thresh-summary").textContent =
        `${flaggedCount} flagged / ${totalLoadedRepos} loaded repo rows${minSuffix}`;

      tree.querySelectorAll("[data-folder]").forEach(el => {
        el.onclick = () => {
          state.activeFolder = el.getAttribute("data-folder");
          state.page = 0;
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
      if (it.kind === "site") return `${it.siteStatus}: ${it.reason || "unhealthy"}`;
      const tier = effectiveTier(it);
      const age = formatAgeHuman(it.inactiveDays);
      const pushShort = formatAgeShort(it.inactiveDays);
      const archShort = formatAgeShort(it.archivedDays);

      if (tier === "missing") {
        if (it.isDisabled || it.reason.toLowerCase().includes("disabled")) {
          return age ? `Disabled · last push ${age}` : "Disabled";
        }
        if (it.reason.includes("451")) {
          return pushShort ? `DMCA 451 · last seen alive ${pushShort} ago` : "Dead (DMCA 451)";
        }
        return pushShort ? `404 · last seen alive ${pushShort} ago` : "Dead (404 Gone)";
      }
      if (tier === "archived") {
        if (archShort && pushShort) {
          return `Arch ${archShort} · push ${pushShort}`;
        }
        if (archShort) {
          return `Arch ${archShort}`;
        }
        return pushShort ? `Arch · push ${pushShort}` : "Archived";
      }
      return age ? `${age} ago` : (it.reason || "Empty / unknown age");
    }

    function renderSafeLink(url, labelText, extraClass = "row-link", stopProp = true) {
      const safe = safeHref(url);
      const stopAttr = stopProp ? ` onclick="event.stopPropagation()"` : "";
      const clsAttr = extraClass ? ` class="${extraClass}"` : "";
      if (!safe) {
        return `<span>${escapeHtml(labelText || url || "—")}</span>`;
      }
      return `<a href="${escapeHtml(safe)}" target="_blank" rel="noopener noreferrer"${clsAttr}${stopAttr}>${escapeHtml(labelText)}</a>`;
    }

    function formatWebCell(it) {
      const parts = [];
      if (it.replacementUrl) {
        parts.push(renderSafeLink(it.replacementUrl, `↪ ${shortUrlLabel(it.replacementUrl)}`, "row-link repl-tag", true));
      }
      for (const u of (it.webUrls || [])) {
        const prefix = isPagesUrl(u) ? "Pages:" : "Web:";
        parts.push(renderSafeLink(u, `${prefix} ${shortUrlLabel(u)}`, "row-link", true));
      }
      return parts.join(" · ") || `<span class="muted-cell">—</span>`;
    }

    function updateSortHeaders() {
      document.querySelectorAll("th[data-sort]").forEach(th => {
        if (!th.dataset.baseLabel) {
          th.dataset.baseLabel = th.textContent.replace(/\s*[▲▼]$/, "");
        }
        const field = th.getAttribute("data-sort");
        const isIconCol = field === "starred" || field === "links";
        const arrow = state.sortField === field
          ? (isIconCol ? (state.sortAsc ? "▲" : "▼") : (state.sortAsc ? " ▲" : " ▼"))
          : "";
        th.textContent = th.dataset.baseLabel + arrow;
      });
    }

    function updateStatusBarCounts(showingCount) {
      const dirtyMark = state.whitelistDirty ? " *" : "";
      const wlTotal = state.whitelistRepos.size + state.whitelistUrls.size;
      if (typeof showingCount === "number") {
        document.getElementById("stat-showing").textContent = `Showing: ${showingCount}`;
      }
      document.getElementById("stat-ticked").textContent = `Ticked: ${state.selectedIds.size}`;
      document.getElementById("stat-wl").textContent = `Whitelisted: ${wlTotal}${dirtyMark}`;
      document.getElementById("wl-count").textContent = `${wlTotal} whitelisted${dirtyMark}`;
    }

    function renderTable() {
      updateSortHeaders();
      const all = filteredAndSortedItems();
      const totalPages = Math.max(1, Math.ceil(all.length / PAGE_SIZE));
      if (state.page >= totalPages) state.page = totalPages - 1;
      const slice = all.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE);

      if (slice.length === 0) {
        if (!all.some(x => x.id === state.focusedId)) {
          state.focusedId = null;
        }
      } else if (!slice.some(x => x.id === state.focusedId)) {
        state.focusedId = slice[0].id;
      }

      const tbody = document.getElementById("thread-tbody");
      tbody.innerHTML = "";

      for (const it of slice) {
        const tier = effectiveTier(it);
        const tr = document.createElement("tr");
        tr.dataset.id = it.id;
        const isSel = state.focusedId === it.id;
        const isTicked = state.selectedIds.has(it.id);
        tr.className =
          (isSel ? "selected " : "") +
          (isTicked ? "ticked " : "") +
          ((it.starred || it.replacementUrl || tier === "missing") ? "bold-row" : "");

        const hasClip = (it.webUrls && it.webUrls.length > 0) || Boolean(it.replacementUrl);

        const repoCellHtml = it.kind === "repo"
          ? renderSafeLink(it.htmlUrl, it.fullName, "row-link", true)
          : renderSafeLink(it.siteUrl, shortUrlLabel(it.siteUrl), "row-link", true);

        const srcInfo = it.kind === "repo"
          ? formatSourceBadge(it.source)
          : { short: "✓ In FMHY", full: "✅ Website URL directly in FMHY listing", isHeur: false };
        const srcCellHtml = `<span class="src-pill${srcInfo.isHeur ? " heur" : ""}" title="${escapeHtml(srcInfo.full)}">${escapeHtml(srcInfo.short)}</span>`;

        tr.innerHTML = `
          <td class="col-cb"><input type="checkbox" class="row-cb" ${isTicked ? "checked" : ""} /></td>
          <td class="col-star">${it.starred ? '<span class="star-on">★</span>' : '<span class="star-off">•</span>'}</td>
          <td class="col-clip">${hasClip ? "📎" : ""}</td>
          <td class="col-subject" title="${escapeHtml(it.title)}">${escapeHtml(it.title)}</td>
          <td class="col-repo" title="${escapeHtml(it.fullName || it.siteUrl || "")}">${repoCellHtml}</td>
          <td class="col-src">${srcCellHtml}</td>
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
          tbody.querySelectorAll("tr.selected").forEach(r => r.classList.remove("selected"));
          tr.classList.add("selected");
          renderInspector();
        };

        tr.ondblclick = () => {
          const url = safeHref(it.kind === "repo" ? it.htmlUrl : it.siteUrl);
          if (url) window.open(url, "_blank", "noopener,noreferrer");
        };

        tbody.appendChild(tr);
      }

      document.getElementById("page-label").textContent = `Page ${state.page + 1} / ${totalPages}`;
      document.getElementById("prev-pg").disabled = state.page <= 0;
      document.getElementById("next-pg").disabled = state.page + 1 >= totalPages;
      updateStatusBarCounts(all.length);
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
          `<div class="tb-detail-row tb-muted-note">Select an entry from the folder list above (or clear active search/filters).</div>`;
        noteInput.value = "";
        noteInput.disabled = true;
        return;
      }

      const tier = effectiveTier(it);
      const star = it.starred ? "★ " : "";
      document.getElementById("hdr-title").textContent = star + it.title;
      document.getElementById("hdr-repo").innerHTML = it.kind === "repo"
        ? renderSafeLink(it.htmlUrl, it.fullName, "", false)
        : renderSafeLink(it.siteUrl, it.siteUrl, "", false);
      document.getElementById("hdr-cat").textContent = `${it.catLabel} (${it.entryId})`;

      document.getElementById("hdr-tick-btn").textContent =
        state.selectedIds.has(it.id) ? "✓ Untick" : "✓ Tick for Discord";
      document.getElementById("hdr-wl-btn").textContent =
        isWhitelisted(it) ? "🔥 Unwhitelist" : "🔥 Whitelist";

      document.getElementById("card-heading").textContent = `Card for ${star}${it.title}`;
      document.getElementById("card-tier-pill").textContent = formatStatusCell(it);

      let detailsHtml = "";
      if (it.kind === "repo") {
        const sInfo = formatSourceBadge(it.source);
        const resolvedDiffers = Boolean(
          it.requestedFullName && it.requestedFullName !== it.fullName
        );
        const resolvedLine = resolvedDiffers
          ? `<div style="font-weight:400;margin-top:2px;">Resolved from: <code>${escapeHtml(it.requestedFullName)}</code> &rarr; <code>${escapeHtml(it.fullName)}</code></div>`
          : "";

        if (!sInfo.isHeur) {
          detailsHtml += `<div class="tb-src-banner-ok">✅ GitHub URL is directly in the FMHY listing${resolvedLine}</div>`;
        } else {
          const heurExplain = sInfo.full.replace(/^⚠️\s*NOT in FMHY\s*—\s*/i, "");
          detailsHtml += `<div class="tb-src-banner-warn">⚠️ No GitHub URL in FMHY listing — inferred via heuristic (${escapeHtml(heurExplain)})${resolvedLine}</div>`;
        }
        detailsHtml += `<div class="tb-detail-row"><strong>GitHub Repository:</strong> ${renderSafeLink(it.htmlUrl, it.htmlUrl, "", false)}</div>`;
      } else {
        detailsHtml += `<div class="tb-src-banner-ok">✅ Website URL is directly in the FMHY listing</div>`;
        detailsHtml += `<div class="tb-detail-row"><strong>Target URL:</strong> ${renderSafeLink(it.siteUrl, it.siteUrl, "", false)}</div>`;
      }

      const ageTxt = formatAgeHuman(it.inactiveDays);
      const archAgeTxt = formatAgeHuman(it.archivedDays);
      if (archAgeTxt && it.archivedAt) {
        detailsHtml += `<div class="tb-detail-row"><strong>Archived When:</strong> <strong>${escapeHtml(archAgeTxt)} ago</strong> (${escapeHtml(it.archivedAt)})</div>`;
      }
      if (ageTxt) {
        if (tier === "missing" && !it.isDisabled) {
          const seenDateTxt = it.lastActivityAt ? ` (last seen alive ${escapeHtml(it.lastActivityAt)})` : "";
          detailsHtml += `<div class="tb-detail-row"><strong>Wayback / Last Seen Alive:</strong> <strong>${escapeHtml(ageTxt)} ago</strong>${seenDateTxt}</div>`;
        } else {
          const pushDateTxt = it.lastActivityAt ? ` (last push ${escapeHtml(it.lastActivityAt)})` : "";
          detailsHtml += `<div class="tb-detail-row"><strong>Last Push / Release:</strong> <strong>${escapeHtml(ageTxt)} ago</strong>${pushDateTxt}</div>`;
        }
      }
      detailsHtml += `<div class="tb-detail-row"><strong>Status Detail:</strong> ${escapeHtml(it.reason || "Empty / unknown age")}</div>`;

      if (it.webUrls && it.webUrls.length > 0) {
        const wlinks = it.webUrls.map(u => {
          const tag = isPagesUrl(u) ? "[GitHub Pages]" : "[Website]";
          return `${tag} ${renderSafeLink(u, u, "", false)}`;
        }).join("<br/>");
        detailsHtml += `<div class="tb-detail-row"><strong>Attached Web / Pages:</strong><br/>${wlinks}</div>`;
      } else {
        detailsHtml += `<div class="tb-detail-row"><strong>Attached Web / Pages:</strong> <span class="tb-muted-note">None linked</span></div>`;
      }

      if (it.replacementUrl) {
        const st = it.replacementStatus ? ` (${escapeHtml(it.replacementStatus)})` : "";
        detailsHtml += `<div class="tb-detail-row tb-repl-box">
          <strong class="tb-repl-title">↪ Detected Replacement Repo:</strong>
          <strong>${renderSafeLink(it.replacementUrl, it.replacementUrl, "", false)}</strong>${st}
        </div>`;
      }

      const hasRawLine = Boolean(it.rawLine && it.rawLine.trim());
      detailsHtml += `
        <div class="tb-raw-line-hdr">
          <strong>Literal FMHY Listing Line:</strong>
          ${hasRawLine ? `<button type="button" class="tb-classic-btn" id="btn-copy-raw-line" style="padding:1px 6px;font-size:10.5px;">Copy Line</button>` : ""}
        </div>
        <div class="tb-raw-line-box">${hasRawLine ? escapeHtml(it.rawLine) : `<span class="tb-muted-note" style="font-style:italic;">(Literal raw_line not present in this report JSON — re-run fmhy-drift to include)</span>`}</div>
      `;

      document.getElementById("card-details").innerHTML = detailsHtml;

      const copyLineBtn = document.getElementById("btn-copy-raw-line");
      if (copyLineBtn && hasRawLine) {
        copyLineBtn.onclick = async () => {
          try {
            await navigator.clipboard.writeText(it.rawLine);
            copyLineBtn.textContent = "✓ Copied";
            setStatus("Copied literal FMHY markdown line to clipboard.");
            setTimeout(() => { if (copyLineBtn.isConnected) copyLineBtn.textContent = "Copy Line"; }, 1400);
          } catch (err) {
            setStatus("Clipboard copy failed: " + err.message);
          }
        };
      }

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
        { title: "Dead / 404 / DMCA GitHub Repos", filter: it => it.kind === "repo" && effectiveTier(it) === "missing" },
        { title: "Archived Repos (New Repo Found)", filter: it => it.kind === "repo" && effectiveTier(it) === "archived" && it.replacementUrl },
        { title: "Archived GitHub Repos", filter: it => it.kind === "repo" && effectiveTier(it) === "archived" && !it.replacementUrl },
        { title: "Inactive GitHub Repos", filter: it => it.kind === "repo" && !["missing", "archived"].includes(effectiveTier(it)) },
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
            const tier = effectiveTier(it);
            const srcTag = formatSourceBadge(it.source).discordTag;
            const webPart = (it.webUrls && it.webUrls.length)
              ? " | Web: " + it.webUrls.map(u => `<${u}>`).join(", ")
              : "";
            const replPart = it.replacementUrl
              ? ` | -> New Repo: <${it.replacementUrl}>${it.replacementStatus ? ` (${it.replacementStatus})` : ""}`
              : "";
            const ageStr = formatAgeHuman(it.inactiveDays);
            const archStr = formatAgeHuman(it.archivedDays);
            let detail;
            if (tier === "missing") {
              if (it.isDisabled) {
                detail = ageStr ? `${it.reason || "Disabled"} — last push ${ageStr} ago` : (it.reason || "Disabled");
              } else {
                const seenDate = it.lastActivityAt ? ` (${it.lastActivityAt})` : "";
                detail = ageStr
                  ? `${it.reason || "Dead / 404"} — last seen alive ${ageStr} ago${seenDate}`
                  : (it.reason || "Dead / 404");
              }
            } else if (tier === "archived") {
              const pushDate = it.lastActivityAt ? `, last push ${it.lastActivityAt}` : "";
              detail = (archStr && it.archivedAt)
                ? `archived ${archStr} ago (${it.archivedAt})${ageStr ? `, last push ${ageStr} ago` : ""}${pushDate}`
                : `archived${ageStr ? ` — last push ${ageStr} ago${pushDate}` : ""}`;
            } else {
              const pushDate = it.lastActivityAt ? `, last push ${it.lastActivityAt}` : "";
              detail = ageStr
                ? `${ageStr} without updates${pushDate}`
                : (it.reason || "Empty / unknown age");
            }
            lines.push(`- ${star}**${it.title}** (\`${it.catLabel}\`) — <${it.htmlUrl}> [${srcTag}] (${detail})${webPart}${replPart}${noteSuffix}`);
          } else {
            lines.push(`- ${star}**${it.title}** (\`${it.catLabel}\`) — <${it.siteUrl}> (${it.reason || "unhealthy"})${noteSuffix}`);
          }
        }
      }
      return lines.join("\n");
    }

    function renderDiscordOutbox() {
      document.getElementById("discord-preview").value = buildDiscordMarkdown();
      updateStatusBarCounts();
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
      if (isWhitelisted(it)) {
        targetSet.delete(it.wlKey);
        if (it.origKey) targetSet.delete(it.origKey);
      } else {
        targetSet.add(it.wlKey);
        if (it.origKey && it.origKey !== it.wlKey) targetSet.add(it.origKey);
        state.selectedIds.delete(it.id);
      }
      state.whitelistDirty = true;
      saveLocalState();

      const listAfter = filteredAndSortedItems();
      if (listAfter.length > 0 && idx >= 0) {
        state.focusedId = listAfter[Math.min(idx, listAfter.length - 1)].id;
      } else if (listAfter.length === 0) {
        state.focusedId = null;
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
        renderInspector();
      };
    });

    // Keyboard navigation (Thunderbird shortcuts: Up/Down, Space to tick, J to Junk/Whitelist)
    document.addEventListener("keydown", (e) => {
      const tag = (document.activeElement?.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;

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

    // Toolbar, Filter & Live Threshold bindings
    document.getElementById("btn-reload").onclick = () => loadData();
    document.getElementById("btn-toggle-tick").onclick = () => toggleFocusedTick();
    document.getElementById("hdr-tick-btn").onclick = () => toggleFocusedTick();
    document.getElementById("btn-junk-wl").onclick = () => toggleFocusedWhitelist();
    document.getElementById("hdr-wl-btn").onclick = () => toggleFocusedWhitelist();

    document.getElementById("search").oninput = () => { state.page = 0; renderTable(); renderInspector(); };
    document.getElementById("starred-only").onchange = () => { state.page = 0; renderTable(); renderInspector(); };
    document.getElementById("has-web-only").onchange = () => { state.page = 0; renderTable(); renderInspector(); };
    document.getElementById("source-filter").onchange = () => { state.page = 0; renderTable(); renderInspector(); };

    function parseNonNegInt(val, fallback) {
      const n = parseInt(val, 10);
      return Number.isFinite(n) && n >= 0 ? n : fallback;
    }

    function onThresholdsInput() {
      state.t1 = parseNonNegInt(document.getElementById("thresh-t1").value, 0);
      state.t2 = parseNonNegInt(document.getElementById("thresh-t2").value, 0);
      state.t3 = parseNonNegInt(document.getElementById("thresh-t3").value, 0);
      state.minDays = parseNonNegInt(document.getElementById("min-days").value, 0);
      state.thresholdsCustomized = true;
      state.page = 0;
      renderAll();
    }

    document.getElementById("thresh-t1").oninput = onThresholdsInput;
    document.getElementById("thresh-t2").oninput = onThresholdsInput;
    document.getElementById("thresh-t3").oninput = onThresholdsInput;
    document.getElementById("min-days").oninput = onThresholdsInput;

    document.getElementById("btn-reset-thresh").onclick = () => {
      state.t1 = state.reportDefaults.t1;
      state.t2 = state.reportDefaults.t2;
      state.t3 = state.reportDefaults.t3;
      state.minDays = 0;
      state.thresholdsCustomized = false;
      state.page = 0;
      syncThresholdInputs();
      renderAll();
    };

    document.getElementById("prev-pg").onclick = () => {
      if (state.page > 0) { state.page--; renderTable(); renderInspector(); }
    };
    document.getElementById("next-pg").onclick = () => {
      state.page++; renderTable(); renderInspector();
    };

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
    document.getElementById("menu-theme-toggle").onclick = () => {
      const isDark = document.documentElement.classList.toggle("dark");
      localStorage.setItem("fmhy_theme", isDark ? "dark" : "light");
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

    function splitDiscordChunks(content, maxLen = 1900) {
      const pieces = [];
      for (const rawLine of content.split("\n")) {
        let rem = rawLine;
        while (rem.length > maxLen) {
          let cut = rem.lastIndexOf(" ", maxLen);
          if (cut <= 200) cut = maxLen;
          pieces.push(rem.slice(0, cut));
          rem = rem.slice(cut).trimStart();
        }
        pieces.push(rem);
      }

      const chunks = [];
      let cur = "";
      for (const line of pieces) {
        const cand = cur ? `${cur}\n${line}` : line;
        if (cand.length > maxLen && cur) {
          chunks.push(cur);
          cur = line;
        } else {
          cur = cand;
        }
      }
      if (cur) chunks.push(cur);
      return chunks;
    }

    function sleepMs(ms) {
      return new Promise(resolve => setTimeout(resolve, ms));
    }

    async function sendToDiscord() {
      const webhook = document.getElementById("webhook-url").value.trim();
      const content = document.getElementById("discord-preview").value.trim();
      if (!webhook) { setStatus("Enter a Discord Webhook URL first."); return; }
      if (!content) { setStatus("Tick at least one entry first."); return; }
      localStorage.setItem("fmhy_discord_webhook", webhook);

      const chunks = splitDiscordChunks(content, 1900);
      setStatus(`Sending ${chunks.length} message(s) to Discord...`);
      try {
        for (let i = 0; i < chunks.length; i++) {
          if (i > 0) await sleepMs(350);
          let delivered = false;
          for (let attempt = 0; attempt < 4; attempt++) {
            const r = await fetch(webhook, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ content: chunks[i], flags: 4 }),
            });
            if (r.status === 429 && attempt < 3) {
              let retryAfterSec = NaN;
              try {
                const body = await r.json();
                if (body && typeof body.retry_after === "number") retryAfterSec = body.retry_after;
              } catch {}
              if (!Number.isFinite(retryAfterSec)) {
                const hdr = parseFloat(r.headers.get("Retry-After") || "");
                retryAfterSec = Number.isFinite(hdr) ? hdr : 2.0;
              }
              const waitMs = Math.ceil(Math.min(Math.max(retryAfterSec, 0.35), 30) * 1000);
              setStatus(`Discord HTTP 429 — waiting ${(waitMs / 1000).toFixed(1)}s before retrying chunk ${i + 1}/${chunks.length}...`);
              await sleepMs(waitMs);
              continue;
            }
            if (!r.ok) throw new Error(`Discord HTTP ${r.status}`);
            delivered = true;
            break;
          }
          if (!delivered) throw new Error("Discord rate limit exceeded after retries");
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
      window.open(`https://github.com/${REPO_SLUG}/edit/main/whitelist.json`, "_blank", "noopener,noreferrer");
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
          "X-GitHub-Api-Version": "2022-11-28",
        };
        let sha = undefined;
        const curResp = await fetch(apiUrl, { headers, cache: "no-store" });
        if (curResp.ok) {
          const curData = await curResp.json();
          sha = curData.sha;
        } else if (curResp.status === 401 || curResp.status === 403) {
          let detail = "";
          try {
            const errJson = await curResp.json();
            if (errJson && errJson.message) detail = `: ${errJson.message}`;
          } catch {}
          throw new Error(`GitHub auth error HTTP ${curResp.status}${detail}`);
        } else if (curResp.status !== 404) {
          throw new Error(`GitHub GET whitelist.json failed (HTTP ${curResp.status})`);
        }

        const jsonBytes = new TextEncoder().encode(buildWhitelistJson());
        let binary = "";
        jsonBytes.forEach(b => binary += String.fromCharCode(b));
        const b64 = btoa(binary);

        const putResp = await fetch(apiUrl, {
          method: "PUT",
          cache: "no-store",
          headers: { ...headers, "Content-Type": "application/json" },
          body: JSON.stringify({
            message: "Update whitelist.json via triage UI",
            content: b64,
            ...(sha ? { sha } : {}),
          }),
        });
        if (!putResp.ok) {
          let putDetail = "";
          try {
            const errJson = await putResp.json();
            if (errJson && errJson.message) putDetail = `: ${errJson.message}`;
          } catch {}
          throw new Error(`GitHub API HTTP ${putResp.status}${putDetail}`);
        }
        state.whitelistDirty = false;
        saveLocalState();
        renderAll();
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
