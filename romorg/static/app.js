/* Simple ROM Organiser - single page UI (vanilla JS, no external resources). */
"use strict";

(() => {
  // ------------------------------------------------------------------ utils
  const TOKEN = document.querySelector('meta[name="romorg-token"]').content;
  const $ = (id) => document.getElementById(id);

  /** Create an element. props: {class, text, title, on: {event: fn}, ...attrs}. */
  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "on") for (const [ev, fn] of Object.entries(value)) node.addEventListener(ev, fn);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  const fmt = (n) => (n === null || n === undefined ? "-" : Number(n).toLocaleString());
  function fmtBytes(n) {
    if (!n && n !== 0) return "-";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  }
  function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }
  /** Split "dir/sub/file" into [dir part, file part]. */
  function splitPath(p) {
    const i = p.lastIndexOf("/");
    return i < 0 ? ["", p] : [p.slice(0, i), p.slice(i + 1)];
  }

  // -------------------------------------------------------------------- API
  async function api(method, path, body) {
    const opts = { method, headers: {} };
    if (method === "POST") {
      opts.headers["Content-Type"] = "application/json";
      opts.headers["X-Romorg-Token"] = TOKEN;
      opts.body = JSON.stringify(body || {});
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (err) {
      throw new Error("Cannot reach the app - is it still running?");
    }
    let data = null;
    try { data = await res.json(); } catch (_) { /* empty body */ }
    if (!res.ok) throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
    return data;
  }
  const get = (path) => api("GET", path);
  const post = (path, body) => api("POST", path, body);
  const qs = (params) => new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();

  // ------------------------------------------------------- toasts / banner
  function toast(message, kind = "info", ms = 5000) {
    const node = el("div", { class: `toast ${kind}`, text: message });
    $("toasts").append(node);
    setTimeout(() => node.remove(), ms);
  }
  function showBanner(message) {
    const b = $("banner");
    b.textContent = message || "";
    b.classList.toggle("hidden", !message);
  }

  /** Persistent notice listing what went wrong (failed moves/copies/writes). */
  function showFailures(id, title, failures, describe, extra = []) {
    const box = $(id);
    const list = Array.isArray(failures) ? failures : [];
    const notes = extra.filter(Boolean);
    if (!list.length && !notes.length) { box.classList.add("hidden"); box.replaceChildren(); return; }
    const shown = list.slice(0, 10).map((f) => el("li", { text: describe(f) }));
    if (list.length > shown.length) shown.push(el("li", { text: `... and ${fmt(list.length - shown.length)} more` }));
    box.replaceChildren(
      ...(list.length ? [el("b", { text: title })] : []),
      ...(shown.length ? [el("ul", { class: "failures" }, shown)] : []),
      ...notes.map((n) => el("div", { text: n })),
      el("div", {}, el("button", { class: "btn btn-small btn-ghost", text: "Dismiss", on: { click: () => box.classList.add("hidden") } })));
    box.classList.remove("hidden");
  }

  // ----------------------------------------------------------------- modals
  function openModal(modal) {
    modal.classList.remove("hidden");
    const focusable = modal.querySelector(".btn-primary, button");
    if (focusable) focusable.focus();
  }
  function closeModal(modal) { modal.classList.add("hidden"); }
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    document.querySelectorAll(".modal:not(.hidden)").forEach((m) => {
      if (m.id === "confirm-modal") $("confirm-no").click();
      else closeModal(m);
    });
  });
  document.querySelectorAll(".modal [data-close]").forEach((btn) =>
    btn.addEventListener("click", () => closeModal(btn.closest(".modal"))));

  /** Confirmation dialog. Resolves to true/false. */
  function confirmDialog({ title, body, okText = "OK", danger = false }) {
    return new Promise((resolve) => {
      const modal = $("confirm-modal");
      $("confirm-title").textContent = title;
      const bodyEl = $("confirm-body");
      bodyEl.replaceChildren(body instanceof Node ? body : el("p", { text: body }));
      const yes = $("confirm-yes");
      const no = $("confirm-no");
      yes.textContent = okText;
      yes.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
      const done = (answer) => {
        yes.onclick = no.onclick = null;
        closeModal(modal);
        resolve(answer);
      };
      yes.onclick = () => done(true);
      no.onclick = () => done(false);
      openModal(modal);
      no.focus();
    });
  }

  // ------------------------------------------------------------ paged table
  /**
   * Searchable, paged table backed by a server fetch function.
   * opts: {columns: [{label, cls, render(item)}], fetch({offset, limit, q}), pageSize,
   *        placeholder, emptyText, rowClass(item)}
   */
  class PagedTable {
    constructor(container, opts) {
      this.container = container;
      this.opts = Object.assign({ pageSize: 50, placeholder: "Search...", emptyText: "Nothing to show." }, opts);
      this.offset = 0;
      this.q = "";
      this.seq = 0;
      this.search = el("input", {
        type: "search", class: "input grow", placeholder: this.opts.placeholder, autocomplete: "off",
        on: { input: debounce(() => { this.q = this.search.value.trim(); this.offset = 0; this.load(); }, 250) },
      });
      this.countEl = el("span", { class: "muted" });
      this.body = el("div");
      this.prev = el("button", { class: "btn btn-small", text: "Previous", on: { click: () => this.go(-1) } });
      this.next = el("button", { class: "btn btn-small", text: "Next", on: { click: () => this.go(1) } });
      this.pageInfo = el("span", { class: "muted" });
      container.replaceChildren(
        el("div", { class: "ptable-tools" }, this.search, this.countEl),
        this.body,
        el("div", { class: "pager" }, this.pageInfo, this.prev, this.next),
      );
    }

    go(dir) {
      this.offset = Math.max(0, this.offset + dir * this.opts.pageSize);
      this.load();
    }

    async load() {
      const seq = ++this.seq;
      let data;
      try {
        data = await this.opts.fetch({ offset: this.offset, limit: this.opts.pageSize, q: this.q });
      } catch (err) {
        if (seq === this.seq) this.body.replaceChildren(el("div", { class: "empty error", text: err.message }));
        return;
      }
      if (seq !== this.seq) return; // a newer request superseded this one
      this.render(data);
    }

    render(data) {
      const { total, items } = data;
      if (this.offset > 0 && this.offset >= total) { this.offset = 0; this.load(); return; }
      this.countEl.textContent = `${fmt(total)} item${total === 1 ? "" : "s"}`;
      if (!items.length) {
        this.body.replaceChildren(el("div", { class: "empty", text: this.q ? `No results for "${this.q}".` : this.opts.emptyText }));
      } else {
        const head = el("tr", {}, this.opts.columns.map((c) => el("th", { text: c.label })));
        const rows = items.map((item) => el("tr", { class: this.opts.rowClass ? this.opts.rowClass(item) : null },
          this.opts.columns.map((c) => el("td", { class: c.cls || "" }, c.render(item)))));
        this.body.replaceChildren(el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, head), el("tbody", {}, rows))));
      }
      const end = Math.min(this.offset + items.length, total);
      this.pageInfo.textContent = total ? `${fmt(this.offset + 1)}-${fmt(end)} of ${fmt(total)}` : "";
      this.prev.disabled = this.offset === 0;
      this.next.disabled = end >= total;
    }
  }

  const badge = (status, text = status) => el("span", { class: `badge ${status}`, text });

  // Small coloured chips for a row's name tags (regions, languages, status, bad dump, how it matched).
  const VIA_LABEL = { headerless: "copier header", byteswapped: "byte-swapped" };
  // Header sizes skipped by the scanner: 512 = SNES copier header, 16 = NES iNES header.
  const HEADER_LABEL = { 512: "copier header", 16: "iNES header skipped" };
  // Short names of the library exclusion rules (the long ones live in /api/library/profile).
  const RULE_SHORT = {
    bad_dump: "Bad dump", virus: "Virus", bad_size: "Bad size", pre_release: "Pre-release", prototype: "Prototype",
    demo: "Demo", faked: "Faked", unreleased: "Unreleased", modified: "Modified",
  };
  const ruleShort = (rule) => RULE_SHORT[rule] || rule;
  /** Chip for a bad dump: the exact flag text from the DAT name, e.g. "Bad dump [b corrupt file]". */
  function badChip(tags) {
    const flags = tags.bad_flags || [];
    const text = flags.length ? `Bad dump ${flags.join(" ")}` : "Bad dump";
    return el("span", { class: "tag tag-bad", text, title: flags.length ? `Bad dump flag(s) in the DAT name: ${flags.join(" ")}` : "Marked as a bad dump in the DAT name" });
  }
  function tagChips(tags, via, byteOrder, header) {
    const chips = [];
    if (tags) {
      for (const r of tags.regions || []) chips.push(el("span", { class: "tag tag-region", text: r }));
      if (!tags.languages_implied) for (const l of tags.languages || []) chips.push(el("span", { class: "tag tag-lang", text: l }));
      if (tags.version) chips.push(el("span", { class: "tag", text: tags.version }));
      if (tags.status) chips.push(el("span", { class: "tag tag-status", text: tags.status }));
      if (tags.bad) chips.push(badChip(tags));
      // Other library rules this name hits, with the exact flag: "Modified [m baddump]", "Pre-release (pre-release)".
      for (const e of tags.excluded_by || []) {
        if (e.rule === "bad_dump" && tags.bad) continue; // shown as the bad dump chip
        chips.push(el("span", { class: "tag tag-excl", text: `${ruleShort(e.rule)} ${e.text}`, title: `Left out of a built library: rule "${ruleShort(e.rule)}"` }));
      }
      if (tags.bios) chips.push(el("span", { class: "tag", text: "BIOS" }));
    }
    if (via && via !== "raw") {
      const label = via === "byteswapped" && byteOrder ? `${byteOrder} byte order`
        : via === "headerless" && HEADER_LABEL[header] ? HEADER_LABEL[header] : VIA_LABEL[via] || via;
      chips.push(el("span", { class: "tag tag-via", text: label, title: "Matched after normalising - the file itself is unchanged" }));
    }
    return chips.length ? el("div", { class: "tags" }, chips) : null;
  }

  // How well a Dreamcast disc is known: verified (every track decoded) > identified (data tracks) > raw set.
  const LEVEL_TEXT = {
    verified: "every track, audio included, was decoded and equals Redump",
    identified: "every data track equals Redump; audio tracks have the right length (use Verify fully to check the audio)",
    raw: "an unpacked Redump set (.gdi / .cue + one file per track, or a single .iso) - can be converted to CHD",
  };
  const levelChip = (level, kind) => (level
    ? el("span", { class: `tag tag-level ${level}`, text: level === "raw" ? "raw (convertible)" : level, title: LEVEL_TEXT[level] || level }) : null);

  // ------------------------------------------------------------------ state
  const state = {
    status: null,
    platforms: [],     // from /api/platforms
    platform: null,    // selected platform name
    scan: null,        // {root, platform, layout, dat_names, missing_dats, summary} - of the selected platform
    lastScan: null,    // the server's scan (may be of another platform)
    drafts: {},        // platform -> folder text that is not saved yet
    folderErr: {},     // platform -> why the last save of its folder failed
    folderBusy: {},    // platform -> a save is in flight
    resultDat: "",     // DAT filter for the result tabs
    tagFilter: { region: "", language: "", video: "", flag: "", rule: "" },
    tagExpanded: {},
    updates: null,     // GET /api/updates
    library: {},       // platform -> GET /api/library/profile answer
    libReason: "",     // Build library preview filters
    libStatus: "",
    libWhy: "",        // primary exclusion code filter of the preview
    vanishReason: "",
    libStats: null,    // numbers of the last plan: {reasons, vanish, exclusions}
    gamesHave: "",     // "" | "1" | "0"
    convertFilter: "",
    organiseFilter: "",
    organiseDest: "",
    m3uFilter: "",
    kickFilter: "",
  };

  // ------------------------------------------------------------------- jobs
  const Jobs = {
    handlers: {},
    timer: null,
    running: false,

    track(job) {
      this.render(job);
      this.setRunning(job.status === "running");
      clearTimeout(this.timer);
      if (job.status === "running") this.timer = setTimeout(() => this.poll(), 500);
      else this.finish(job);
    },

    async poll() {
      let job;
      try {
        job = await get("/api/job");
      } catch (err) {
        showBanner(err.message);
        this.timer = setTimeout(() => this.poll(), 2000);
        return;
      }
      showBanner("");
      if (job) this.track(job);
      else this.setRunning(false);
    },

    async start(path, body) {
      try {
        const res = await post(path, body);
        this.track(res.job);
      } catch (err) {
        toast(err.message, "error");
      }
    },

    finish(job) {
      if (this.lastFinished === job.id) return;
      this.lastFinished = job.id;
      if (job.status === "error") toast(`${job.kind} failed: ${job.error}`, "error", 9000);
      if (job.kind === "scan") {
        const failed = job.status === "error";
        $("scan-error").classList.toggle("hidden", !failed);
        $("scan-error-text").textContent = failed ? job.error : "";
      }
      if (job.status === "cancelled") toast(`${job.kind} cancelled`, "info");
      const handler = this.handlers[job.kind];
      if (handler) handler(job);
    },

    setRunning(running) {
      this.running = running;
      document.querySelectorAll("[data-needs-idle]").forEach((b) => { b.disabled = running || b.dataset.blocked === "1"; });
    },

    render(job) {
      document.querySelectorAll(".job").forEach((box) => {
        if (box.dataset.kind !== job.kind) { if (job.status === "running") box.classList.add("hidden"); return; }
        box.classList.remove("hidden", "error", "done");
        if (job.status === "error") box.classList.add("error");
        if (job.status === "done") box.classList.add("done");
        const { done, total, message } = job.progress || {};
        const pct = total > 0 ? Math.min(100, (100 * done) / total) : null;
        const isBytes = total > 1e6 && /download|updat/i.test(message || "");
        let countText = "";
        if (total > 0) countText = isBytes ? `${fmtBytes(done)} / ${fmtBytes(total)}` : `${fmt(done)} / ${fmt(total)}`;
        let msg = message || "";
        if (job.status === "done") msg = "Finished";
        if (job.status === "error") msg = `Error: ${job.error}`;
        if (job.status === "cancelled") msg = "Cancelled";

        const bar = el("div", { class: "progress-bar" });
        const progress = el("div", { class: "progress" }, bar);
        if (job.status === "running" && pct === null) progress.classList.add("indeterminate");
        else bar.style.width = `${job.status === "done" ? 100 : pct || 0}%`;

        const head = el("div", { class: "job-head" },
          el("span", { class: "job-msg", text: msg, title: msg }),
          el("span", { class: "job-count", text: countText + (pct !== null && job.status === "running" ? `  (${pct.toFixed(0)}%)` : "") }));
        if (job.status === "running" && job.cancellable) {
          head.append(el("button", {
            class: "btn btn-small", text: "Cancel",
            on: { click: async (e) => { e.target.disabled = true; try { await post("/api/job/cancel"); } catch (err) { toast(err.message, "error"); } } },
          }));
        } else if (job.status !== "running") {
          head.append(el("button", { class: "btn btn-small btn-ghost", text: "Hide", on: { click: () => box.classList.add("hidden") } }));
        }
        box.replaceChildren(head, progress);
      });
    },
  };


  const UNMATCHED = "_unmatched";
  const SUPERSEDED = "_superseded";
  const EXCLUDED = "_excluded";
  const INCOMPLETE = "_incomplete";
  const DUPLICATES = "_duplicates";
  const CONVERTED = "_converted_originals";
  const shortDat = (name) => {
    // "Commodore Amiga - Games - [ADF]" -> "Games - [ADF]" when the platform prefix matches.
    const prefix = state.platform ? `${state.platform} - ` : "";
    return prefix && name.startsWith(prefix) ? name.slice(prefix.length) : name;
  };
  const currentPlatform = () => state.platforms.find((p) => p.name === state.platform) || null;
  const pctText = (pct) => `${pct.toFixed(pct >= 10 || pct === 0 ? 1 : 2)}%`;
  const isFlat = (p) => !!p && p.layout === "flat";
  const sourceLabel = (source) => (source === "nointro" ? "No-Intro" : source === "whdload" ? "WHDLoad"
    : source === "redump" ? "Redump" : "TOSEC");
  const isGameFolder = (p) => !!p && p.layout === "game_folder";   // disc systems (Dreamcast, PlayStation, PlayStation 2): one folder per game
  const discOf = (p) => (p && p.disc) || null;                         // {key, label, gd, iso, playlists, iso_mode, ...}
  const discPlaylists = (p) => !!p && isGameFolder(p) && (!discOf(p) || discOf(p).playlists !== false);
  const rawKinds = (p) => { const d = discOf(p); return d && d.iso ? ".cue / .iso" : d && !d.gd ? ".cue" : ".gdi / .cue"; };
  const hasKickstart = (p) => !!(p && (p.has_kickstart !== undefined ? p.has_kickstart : p.kickstart_dat));
  const folderOf = (p) => (p ? (p.name in state.drafts ? state.drafts[p.name] : p.folder || "") : "");

  // ------------------------------------------- 1. Systems, folders + DATs
  async function loadStatus() {
    try {
      state.status = await get("/api/status");
      showBanner("");
    } catch (err) {
      showBanner(err.message);
      return;
    }
    const s = state.status;
    $("app-version").textContent = `v${s.version}`;
    Updates.apply(s.updates);
    $("dats-dir").textContent = s.data_dir ? `Data folder: ${s.data_dir}` : "";
    $("kick-native-browse-btn").classList.toggle("hidden", !s.dialog_available);
  }

  // ------------------------------------------------ DAT updates (automatic)
  const Updates = {
    timer: null,
    wasRunning: false,

    async load() {
      let u;
      try { u = await get("/api/updates"); } catch (_) { return; }
      this.apply(u);
    },

    apply(u) {
      const was = this.wasRunning;
      state.updates = u;
      renderUpdates(u);
      clearTimeout(this.timer);
      this.wasRunning = !!(u && u.running);
      if (this.wasRunning) this.timer = setTimeout(() => this.load(), 1500);
      else if (was) this.finished(u); // an update just ended: new DATs may be installed
    },

    async finished(u) {
      await loadStatus();
      await loadPlatforms();
      if (u && u.scan_stale) toast("DATs updated - rescan to use them.", "info", 8000);
    },

    async check() {
      const u = state.updates;
      try {
        if (u && u.running) {
          await post("/api/updates/cancel");
          toast("Cancelling the update... the installed DATs stay as they are.", "info");
        } else {
          const res = await post("/api/updates/check", {});
          if (!res.started) toast("An update is already running.", "info");
          this.apply(res.updates);
          return;
        }
      } catch (err) { toast(err.message, "error"); }
      this.load();
    },
  };

  const pad2 = (n) => String(n).padStart(2, "0");
  /** "14:02" for today, "2026-10-01 14:02" otherwise. */
  function checkedText(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    const time = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
    return d.toDateString() === new Date().toDateString() ? time
      : `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${time}`;
  }

  /** The single status line, progress bar, offline note and error box of the updates bar. */
  function renderUpdates(u) {
    if (!u) return;
    const tosec = u.tosec || {}, ni = u.nointro || {}, whd = u.whdload || {};
    const parts = [
      tosec.installed ? `TOSEC ${tosec.installed}` : "TOSEC not installed",
      ni.installed ? `No-Intro ${ni.installed}` : "No-Intro not installed",
      whd.installed ? `WHDLoad ${whd.installed}` : "WHDLoad not installed",
    ];
    if (u.last_checked) parts.push(`checked ${checkedText(u.last_checked)}`);
    else if (!u.enabled) parts.push("automatic updates are off");
    const labels = { checking: "Checking for updates...", downloading: "Updating DATs...", installing: "Installing DATs..." };
    $("updates-line").textContent = (u.running ? `${labels[u.state] || "Updating..."}  ` : "") + parts.join(" · ");
    const btn = $("updates-btn");
    btn.textContent = u.running ? "Cancel update" : "Check for updates";
    btn.disabled = !u.enabled && !u.running;
    btn.title = u.enabled || u.running ? "Check TOSEC, No-Intro and WHDLoad for newer DATs and install them" : "Automatic updates are switched off in this run";

    const pr = u.progress || {};
    const bar = $("updates-progress-bar");
    $("updates-progress").classList.toggle("hidden", !u.running);
    $("updates-progress").classList.toggle("indeterminate", !!u.running && !(pr.total > 0));
    bar.style.width = pr.total > 0 ? `${Math.min(100, (100 * pr.done) / pr.total)}%` : "0%";
    const bytes = pr.total > 1e6;
    const count = pr.total > 0 ? (bytes ? `${fmtBytes(pr.done)} / ${fmtBytes(pr.total)}` : `${fmt(pr.done)} / ${fmt(pr.total)}`) : "";
    $("updates-progress-text").textContent = [pr.message, count].filter(Boolean).join("  -  ");
    $("updates-progress-text").classList.toggle("hidden", !u.running || !$("updates-progress-text").textContent);

    const err = u.error;
    $("updates-error").classList.toggle("hidden", !err);
    $("updates-error-text").textContent = err ? (err.message || err.code || "Update failed") : "";
    const note = err ? "" : u.scan_stale ? "DATs updated - rescan to use them."
      : u.offline ? (u.notice || "Offline - using the installed DATs.") : (u.notice || "");
    $("updates-note").textContent = note;
    $("updates-note").classList.toggle("hidden", !note);
    $("updates-note").classList.toggle("stale", !!u.scan_stale && !err);
  }

  async function loadPlatforms() {
    try {
      state.platforms = await get("/api/platforms");
    } catch (err) {
      state.platforms = [];
      $("systems-body").replaceChildren(el("tr", {}, el("td", { colspan: "4", class: "muted", text: `Could not load systems: ${err.message}` })));
      return;
    }
    const names = state.platforms.map((p) => p.name);
    if (!names.includes(state.platform)) {
      const s = state.status || {};
      state.platform = [s.scan && s.scan.platform, s.last_platform, s.default_platform].find((n) => names.includes(n)) || names[0] || null;
    }
    $("platform-select").replaceChildren(...state.platforms.map((p) =>
      el("option", { value: p.name, selected: p.name === state.platform, text: p.complete ? p.name : `${p.name} (DATs missing)` })));
    renderSystems();
    renderPlatform();
  }

  function datCell(p) {
    const present = p.dats.filter((d) => d.present);
    const version = present.length ? present[0].version : null;
    if (p.dats.length === 1) {
      return present.length ? el("span", { class: "mono", text: version || "present" }) : badge("missing");
    }
    return el("div", {},
      present.length === p.dats.length ? el("span", { class: "mono", text: version || "" }) : badge("missing", `${present.length} of ${p.dats.length}`),
      el("div", { class: "sub", text: `${p.dats.length} DATs` }));
  }

  /** One row per system: source, DAT state, folder field + browse, saved/unsaved state, Scan.
   *  A folder is saved the moment it is chosen (Browse / Folders), when the field loses focus or Enter is
   *  pressed, and before a scan - there is no separate Save button to forget. */
  function renderSystems() {
    const dialog = !!(state.status && state.status.dialog_available);
    const rows = state.platforms.map((p) => {
      const input = el("input", {
        type: "text", class: "input grow mono", value: folderOf(p), spellcheck: "false", autocomplete: "off",
        placeholder: `/run/media/deck/<SD>/roms/${p.folder_hint || "..."}`, "aria-label": `${p.name} folder`,
        "data-platform": p.name,
      });
      const stateChip = el("span", { class: "folder-state", "data-folder-state": "" });
      const clear = el("button", { class: "btn btn-small", text: "Clear", title: "Forget this folder" });
      const scan = el("button", { class: "btn btn-small btn-primary", text: "Scan", "data-needs-idle": "" });
      const paint = () => {
        const value = input.value.trim();
        const saved = p.folder || "";
        let kind, text, title = "";
        if (state.folderBusy[p.name]) { kind = "saving"; text = "Saving..."; }
        else if (state.folderErr[p.name]) { kind = "error"; text = "Not saved"; title = state.folderErr[p.name]; }
        else if (value !== saved) { kind = "unsaved"; text = "Not saved"; title = "Saved when you leave the field, press Enter or Scan"; }
        else if (value) { kind = "saved"; text = "Saved"; title = "This folder is remembered"; }
        else { kind = "empty"; text = ""; }
        stateChip.className = `folder-state ${kind}`;
        stateChip.textContent = text;
        stateChip.title = title;
        stateChip.dataset.folderState = kind;
        clear.disabled = !value && !saved;
      };
      const sync = () => {
        const value = input.value.trim();
        if (value === (p.folder || "")) delete state.drafts[p.name]; else state.drafts[p.name] = input.value;
        delete state.folderErr[p.name];
        paint();
        scan.dataset.blocked = value ? "0" : "1";
        scan.disabled = Jobs.running || !value;
        if (p.name === state.platform) renderScanTarget();
      };
      input.addEventListener("input", sync);
      // `change` = the field lost focus / Enter after an edit, and what Browse / Folders dispatch
      input.addEventListener("change", async () => { sync(); await commitFolder(p); paint(); });
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") scanPlatform(p.name); });
      clear.addEventListener("click", async () => {
        input.value = "";
        sync();
        await commitFolder(p);
        paint();
      });
      scan.addEventListener("click", () => scanPlatform(p.name));
      p.paintFolder = paint;
      const title = `Choose the ${p.name} folder`;
      const native = dialog ? el("button", { class: "btn btn-small", text: "Browse...", on: { click: (e) => nativeBrowse(e.currentTarget, input, title) } }) : null;
      const folders = el("button", { class: "btn btn-small", text: "Folders...", on: { click: () => FolderBrowser.open(input, title) } });
      const row = el("tr", {
        class: `system-row ${p.name === state.platform ? "selected" : ""}`,
        on: { click: (e) => { if (!e.target.closest("button, input")) selectPlatform(p.name); } },
      },
      el("td", { class: "wrap" },
        el("div", { class: "system-name" }, el("span", { text: p.name })),
        el("div", { class: "sub" }, el("span", { class: `badge src-${p.source}`, text: sourceLabel(p.source) }),
          p.extensions && p.extensions.length ? ` ${p.extensions.join(" ")}` : "")),
      el("td", {}, datCell(p)),
      el("td", { class: "folder-cell" }, el("div", { class: "folder-row" }, input, native, folders, clear, stateChip)),
      el("td", {}, scan));
      sync();
      return row;
    });
    $("systems-body").replaceChildren(...(rows.length ? rows : [el("tr", {}, el("td", { colspan: "4", class: "muted", text: "No systems defined." }))]));
    Jobs.setRunning(Jobs.running);
  }

  /** Save the folder text of a system now (serialised per system; the newest text always wins).
   *  Resolves true when the folder is saved (or was already), false when the server refused it. */
  const folderQueue = {};
  function commitFolder(p, quiet = false) {
    const run = async () => {
      const path = folderOf(p).trim();
      if (path === (p.folder || "")) { delete state.drafts[p.name]; return true; }
      state.folderBusy[p.name] = true;
      if (p.paintFolder) p.paintFolder();
      try {
        const res = await post("/api/folders", { platform: p.name, path });
        p.folder = (res.folders || {})[p.name] || null;
        const live = state.platforms.find((x) => x.name === p.name);   // the list may have been reloaded meanwhile
        if (live && live !== p) { live.folder = p.folder; if (!(p.name in state.drafts) || folderOf(p).trim() === path) renderSystems(); }
        if (folderOf(p).trim() === path) delete state.drafts[p.name];
        delete state.folderErr[p.name];
        if (state.status) state.status.folders = res.folders || {};
        if (!quiet) toast(p.folder ? `Saved folder for ${p.name}` : `Forgot the folder for ${p.name}`, "ok");
        renderScanTarget();
        return true;
      } catch (err) {
        state.folderErr[p.name] = err.message;
        toast(`${p.name}: folder not saved - ${err.message}`, "error", 8000);
        return false;
      } finally {
        delete state.folderBusy[p.name];
        if (p.paintFolder) p.paintFolder();
      }
    };
    folderQueue[p.name] = (folderQueue[p.name] || Promise.resolve()).then(run, run);
    return folderQueue[p.name];
  }

  function renderPlatform() {
    const p = currentPlatform();
    $("platform-details-title").textContent = p ? `DAT files for ${p.name} (${sourceLabel(p.source)})` : "DAT files";
    const rows = p ? p.dats.map((d) => el("tr", {},
      el("td", { class: "wrap" }, el("div", { text: d.name }),
        el("div", { class: "sub", text: [d.m3u ? "M3U playlists" : "", d.kickstart ? "Kickstart source" : ""].filter(Boolean).join(" · ") })),
      el("td", { class: "mono", text: d.version || "-" }),
      el("td", { class: "mono wrap", text: d.folder ? `${d.folder}/` : "(console folder)" }),
      el("td", {}, badge(d.present ? "ok" : "missing")))) : [];
    $("platform-dats").replaceChildren(...(rows.length ? rows : [el("tr", {}, el("td", { colspan: "4", class: "muted", text: "No system selected." }))]));

    const missing = p ? p.dats.filter((d) => !d.present) : [];
    const notice = $("dats-missing");
    notice.classList.toggle("hidden", !missing.length);
    const updating = !!(state.updates && state.updates.running);
    notice.replaceChildren(
      el("b", { text: `${missing.length} of ${p ? p.dats.length : 0} DATs for ${p ? p.name : "this system"} are not installed yet. ` }),
      updating ? "They are being downloaded now (see the update line above)."
        : "They are downloaded automatically - just press Scan.");

    // Organise step: what goes where for this layout.
    const gameFolder = isGameFolder(p);
    const flat = isFlat(p);
    $("organise-title").textContent = gameFolder ? "Organise (one folder per game)"
      : flat ? "Organise (rename in the console folder)" : "Organise (into DAT folders)";
    $("organise-intro").textContent = gameFolder
      ? "Every game gets its own folder named exactly like the Redump entry, holding <name>.chd. Save states, saves and other files "
        + "that start with the CHD's old name (.zip .md5 .state .srm ...) are renamed with it, so they stay linked; anything else in "
        + "the folder keeps its name and moves along. A loose .chd in the system folder gets a folder of its own. A CHD that matches "
        + "no Redump entry moves with its whole folder into _unmatched/. Nothing is overwritten or deleted; every apply writes an undo log."
      : p && p.source === "whdload"
      ? "Matched .lha archives get their exact WHDLoad database name (for example 1000ccTurbo_v1.0.lha) and are kept flat in the system "
        + "folder (files in subfolders are moved up). The archives are never opened - they are matched by the hash of the whole file. "
        + "Files that match nothing go to _unmatched/ (keeping their subfolders); a Kickstarts/ folder is left alone. "
        + "Nothing is overwritten, and every apply writes an undo log in the folder."
      : flat
      ? "Matched files get their exact No-Intro name and are kept flat in the console folder (files in subfolders are moved up; "
        + "archives are named after the game). Files that match nothing go to _unmatched/ (keeping their subfolders); frontend "
        + "media folders such as media/ or images/ are left alone. Nothing is overwritten, and every apply writes an undo log in the folder."
      : "Matched files are moved into one folder per DAT and given their exact TOSEC name; files that match nothing go to "
        + "_unmatched/ (keeping their subfolders). Nothing is overwritten, and every apply writes an undo log in the platform folder.";
    const hasM3uLayout = !!(p && p.m3u_dats && p.m3u_dats.length) || gameFolder;
    $("organise-layout").textContent = !p ? "" : [
      flat ? "<console folder>/" : "<platform folder>/",
      ...(gameFolder ? ["  <Redump name>/<Redump name>.chd   (+ .zip .md5 .state .srm ... named alike)",
        discPlaylists(p) ? "  <Redump name> (Disc 2)/...         (multi-disc games: one folder per disc + a playlist next to disc 1)"
          : "  <Redump name> (Disc 2)/...         (multi-disc games: one folder per disc, no playlist)"] : []),
      ...(gameFolder ? [] : flat ? [p.source === "whdload" ? "  <WHDLoad name>.lha   (the exact database file name)" : "  <No-Intro name>.<ext>   (or .zip / .7z named after the game)"]
        : p.dats.map((d) => `  ${d.folder}/`)),
      ...reserved(hasM3uLayout && !gameFolder, !!p.convertible, p.protected_dirs || [])].join("\n");
    $("organise-latest-only").checked = !!(p && p.latest_only);
    $("m3u-dats").textContent = p && p.m3u_dats.length ? `Playlists are made for: ${p.m3u_dats.map(shortDat).join(", ")}` : "";
    const hasM3u = !!(p && p.m3u_dats && p.m3u_dats.length);
    $("m3u-block").classList.toggle("hidden", !hasM3u);
    $("lib-labels-wrap").classList.toggle("hidden", !hasM3u && !discPlaylists(p));
    $("lib-savedisk-wrap").classList.toggle("hidden", !hasM3u);
    $("library-layout").textContent = $("organise-layout").textContent;
    $("library-title").textContent = "Build library";
    $("library-intro").replaceChildren(
      "One action tidies the whole folder: files are renamed and sorted, and unwanted, older and duplicate files are set aside in their own folders next to the games (",
      el("code", { text: `${EXCLUDED}/` }), ", ", el("code", { text: `${SUPERSEDED}/` }), ", ",
      ...(hasM3u && !gameFolder ? [el("code", { text: `${INCOMPLETE}/` }), ", "] : []),
      el("code", { text: `${DUPLICATES}/` }), "; never deleted; files that match nothing go to ", el("code", { text: `${UNMATCHED}/` }), ")",
      hasM3u ? ", and playlists are written for complete multi-disk games."
        : discPlaylists(p) ? ", and an .m3u playlist is written for every multi-disc game (all discs of the chosen release stay together; set-aside games move with their whole folder)."
        : gameFolder ? " (the discs of a multi-disc game stay together; no playlists are written for this system - its emulator does not use them)." : ".",
      " Preview first; ", el("b", { text: "Undo last" }), hasM3u ? " reverts everything, playlists included." : " reverts everything.");
    loadLibraryProfile();
    renderSteps();
    renderScanTarget();
    updateActionState();
  }

  /** Show only the steps the selected system uses and number them 1..n. */
  function renderSteps() {
    const p = currentPlatform();
    const visible = {
      "step-convert": !!(p && p.convertible),
      "step-kickstart": hasKickstart(p),
    };
    let n = 0;
    document.querySelectorAll("main > section.step").forEach((section) => {
      const show = visible[section.id] !== false;
      section.classList.toggle("hidden", !show);
      const link = document.querySelector(`#stepnav a[href="#${section.id}"]`);
      if (link) link.classList.toggle("hidden", !show);
      if (!show) return;
      n += 1;
      const num = section.querySelector(".step-num");
      if (num) num.textContent = String(n);
      if (link) link.textContent = `${n} ${link.dataset.label}`;
    });
    if (visible["step-kickstart"] && !kickDirsLoaded) loadKickDirs();
  }

  function selectPlatform(name) {
    if (!name || name === state.platform) return;
    state.platform = name;
    state.resultDat = "";
    state.tagFilter = { region: "", language: "", video: "", flag: "", rule: "" };
    state.gamesHave = "";
    state.libReason = "";
    state.libStatus = "";
    // plan filters belong to the previous system (its destinations / statuses)
    state.organiseFilter = "";
    state.organiseDest = "";
    state.convertFilter = "";
    state.m3uFilter = "";
    state.kickFilter = "";
    kickDirsLoaded = false;                       // each system has its own Kickstart destination
    $("lib-labels").checked = !isGameFolder(currentPlatform());   // |Disc N labels are PUAE syntax: off for disc-system playlists
    setKickDest(currentPlatform());
    $("platform-select").value = name;
    // A finished job box (e.g. the last scan) belongs to the previous system.
    if (!Jobs.running) document.querySelectorAll(".job").forEach((b) => b.classList.add("hidden"));
    $("scan-error").classList.add("hidden");
    renderSystems();
    renderPlatform();
    applyScan();
  }

  /** In-app folder browser; writes the chosen folder into a target input element. */
  const FolderBrowser = {
    current: null,
    parent: null,
    target: null,

    async open(target, title = "Choose a folder") {
      this.target = target;
      $("folder-modal-title").textContent = title;
      openModal($("folder-modal"));
      await this.list(target.value.trim(), true);
    },

    async list(path, fallbackHome = false) {
      const dirs = $("fb-dirs");
      try {
        const data = await get(`/api/fs/list?${qs({ path, hidden: $("fb-hidden").checked ? "1" : "" })}`);
        this.render(data);
      } catch (err) {
        if (fallbackHome && path) return this.list("", false);
        dirs.replaceChildren(el("li", { class: "empty-item", text: err.message }));
      }
    },

    render(data) {
      this.current = data.path;
      this.parent = data.parent;
      $("fb-path").textContent = data.path;
      $("fb-up").disabled = !data.parent;
      $("fb-places").replaceChildren(...data.places.map((p) =>
        el("button", { class: "btn", text: p.name, title: p.path, on: { click: () => this.list(p.path) } })));
      const items = data.dirs.map((d) => el("li", {
        tabindex: "0", text: d.name,
        on: {
          click: () => this.list(d.path),
          keydown: (e) => { if (e.key === "Enter") this.list(d.path); },
        },
      }));
      $("fb-dirs").replaceChildren(...(items.length ? items : [el("li", { class: "empty-item", text: "No subfolders here." })]));
      $("fb-dirs").scrollTop = 0;
    },

    choose() {
      if (!this.current || !this.target) return;
      this.target.value = this.current;
      closeModal($("folder-modal"));
      this.target.dispatchEvent(new Event("input"));
      this.target.dispatchEvent(new Event("change"));   // a system folder is saved right away
    },
  };

  async function nativeBrowse(btn, target, title) {
    btn.disabled = true;
    try {
      const res = await post("/api/fs/pick", { start: target.value.trim(), title });
      if (res.path) { target.value = res.path; target.dispatchEvent(new Event("input")); target.dispatchEvent(new Event("change")); }
      else if (res.timeout) toast("The folder dialog did not answer - use \"Folders...\" instead.", "info", 8000);
    } catch (err) {
      toast(err.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  // ------------------------------------------------------------- 2. Scan
  function renderScanTarget() {
    const p = currentPlatform();
    const folder = folderOf(p).trim();
    $("scan-folder").textContent = folder || "No folder yet - choose it in step 1.";
    $("scan-folder").title = folder;
    $("scan-folder").classList.toggle("muted", !folder);
    $("scan-btn").dataset.blocked = p && folder ? "0" : "1";
    Jobs.setRunning(Jobs.running);
  }

  function scanPlatform(name) {
    selectPlatform(name);
    startScan();
  }

  async function startScan() {
    const p = currentPlatform();
    if (!p) { toast("Choose a system first", "error"); return; }
    const path = folderOf(p).trim();
    if (!path) { toast(`Choose the ${p.name} folder first`, "error"); return; }
    if (!(await commitFolder(p, true))) return;   // remember the folder first (a bad path says why)
    Jobs.start("/api/scan", { path, platform: p.name });
  }

  Jobs.handlers.scan = async (job) => {
    if (job.status !== "done") return;
    await loadPlatforms(); // the folder is remembered by the scan
    await refreshScan();
  };

  /** Pull the current scan from /api/status and re-render everything that depends on it. */
  async function refreshScan() {
    await loadStatus();
    applyScan();
  }

  /** Use the server's scan only when it is of the selected system. */
  function applyScan() {
    const s = state.status || {};
    state.lastScan = s.scan || null;
    state.scan = state.lastScan && state.lastScan.platform === state.platform ? state.lastScan : null;
    if (state.scan && state.resultDat && !state.scan.dat_names.includes(state.resultDat)) state.resultDat = "";
    renderScan();
    renderLibraryIntro();
    renderOrganiseIntro();
    renderConvertIntro();
    renderM3UIntro();
    renderKickIntro();
    updateActionState();
  }

  function card(value, label, cls = "") {
    return el("div", { class: `card ${cls}` }, el("div", { class: "value", text: value }), el("div", { class: "label", text: label }));
  }

  function progressBar(pct) {
    const bar = el("div", { class: "progress-bar" });
    bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    return el("div", { class: "progress" }, bar);
  }

  let activeTab = "";
  const DAT_FILTER_TABS = new Set(["matched", "missing", "games"]);
  const TAG_TABS = DAT_FILTER_TABS;
  const byGame = (s) => s && s.count_by === "game";

  function renderScan() {
    const scan = state.scan;
    $("scan-empty").classList.toggle("hidden", !!scan);
    $("scan-output").classList.toggle("hidden", !scan);
    const last = state.lastScan;
    $("scan-empty").textContent = last && !scan
      ? `The last scan was of ${last.platform}. Press "Scan folder" to check ${state.platform}.`
      : "Choose a folder for the system in step 1, then press Scan.";
    if (!scan) { $("scan-info").textContent = ""; return; }
    const s = scan.summary;
    const games = byGame(s);
    $("scan-info").textContent = `Results for ${scan.platform}  -  ${scan.root}`;
    const missingDats = scan.missing_dats || [];
    $("scan-missing-dats").classList.toggle("hidden", !missingDats.length);
    $("scan-missing-dats").textContent = missingDats.length
      ? `Not checked (DAT not downloaded): ${missingDats.join(", ")}` : "";

    renderDcBar(s);
    const total = games ? first(s.games_total, s.dat_total) : s.dat_total;
    const have = games ? first(s.games_have, s.have) : s.have;
    const missing = games ? first(s.games_missing, s.missing) : s.missing;
    const pct = total ? (100 * have) / total : 0;
    const via = s.matched_via || {};
    const normalised = (via.headerless || 0) + (via.byteswapped || 0);
    $("summary-cards").replaceChildren(
      card(fmt(have), `${games ? "Games you have" : "Have"} (of ${fmt(total)})`, "ok"),
      card(fmt(missing), games ? "Games missing" : "Missing", "bad"),
      card(pctText(pct), "Complete", "info"),
      card(fmt(s.matched_files), "Matched files", "ok"),
      card(fmt(s.unmatched_files), "Unmatched files", s.unmatched_files ? "warn" : ""),
      ...(s.correctly_placed !== undefined ? [card(fmt(s.correctly_placed), isFlat(currentPlatform()) ? "In place (named correctly)" : "In place (named + in DAT folder)", "ok")] : []),
      card(fmt(s.duplicates), "Duplicates", s.duplicates ? "warn" : ""),
      ...(normalised ? [card(fmt(normalised), "Matched with header / byte-swapped", "info")] : []),
      ...(s.chd_files !== undefined ? [
        card(fmt(s.verified), "CHDs verified (every track)", s.verified ? "ok" : ""),
        card(fmt(s.identified), "CHDs identified (data tracks)", s.identified ? "info" : ""),
        card(fmt(s.raw), "Raw sets (convertible to CHD)", s.raw ? "warn" : "")]
        : s.convertible ? [card(fmt(s.convertible), "Convertible to No-Intro format", "info")] : []),
      ...(s.unsupported ? [card(fmt(s.unsupported), "Unsupported archives", "warn")] : []),
      ...(s.errors ? [card(fmt(s.errors), "Read errors", "bad")] : []),
      el("div", { class: "complete-bar" }, progressBar(pct)),
    );

    // Per-DAT cards (only worth showing with several DATs): click one to filter the tables.
    const perDat = s.per_dat || {};
    const multi = scan.dat_names.length > 1;
    $("dat-cards-title").classList.toggle("hidden", !multi);
    $("dat-cards").classList.toggle("hidden", !multi);
    $("dat-cards").replaceChildren(...(multi ? scan.dat_names : []).map((name) => {
      const d = perDat[name] || {};
      const dGames = d.count_by === "game";
      const dTotal = dGames ? first(d.games_total, d.dat_total) : d.dat_total;
      const dHave = dGames ? first(d.games_have, d.have) : d.have;
      const dpct = dTotal ? (100 * (dHave || 0)) / dTotal : 0;
      const active = state.resultDat === name;
      return el("button", {
        class: `dat-card ${active ? "active" : ""}`, title: active ? "Show all DATs" : `Show only ${name}`,
        on: { click: () => { state.resultDat = active ? "" : name; if (!DAT_FILTER_TABS.has(activeTab)) activeTab = "matched"; renderScan(); } },
      },
      el("div", { class: "dat-card-name", text: shortDat(name) }),
      el("div", { class: "dat-card-main" },
        el("span", { class: "value", text: fmt(dHave) }),
        el("span", { class: "muted", text: ` / ${fmt(dTotal)}  (${pctText(dpct)})` })),
      progressBar(dpct),
      el("div", { class: "dat-card-stats" },
        el("span", { text: `Missing ${fmt(dGames ? first(d.games_missing, d.missing) : d.missing)}` }),
        el("span", { text: `Files ${fmt(d.matched_files)}` }),
        d.correctly_placed !== undefined ? el("span", { text: `In place ${fmt(d.correctly_placed)}` }) : null));
    }));

    const tabs = [];
    if (games) tabs.push(["games", "Games", total]);
    tabs.push(["matched", "Matched files", s.matched_files], ["missing", games ? "Missing games" : "Missing", missing],
      ["unmatched", "Unmatched", s.unmatched_files]);
    if (s.unsupported) tabs.push(["unsupported", "Unsupported", s.unsupported]);
    if (s.errors) tabs.push(["errors", "Errors", s.errors]);
    if (!tabs.some((t) => t[0] === activeTab)) activeTab = tabs[0][0];
    $("result-tabs").replaceChildren(...tabs.map(([key, label, count]) => el("button", {
      class: `tab ${key === activeTab ? "active" : ""}`, role: "tab", "aria-selected": key === activeTab ? "true" : "false",
      on: { click: () => { activeTab = key; renderScan(); } },
    }, label, el("span", { class: "count", text: `(${fmt(count)})` }))));

    const container = el("div");
    const filters = [];
    if (DAT_FILTER_TABS.has(activeTab) && multi) {
      const select = el("select", {
        class: "input select", "aria-label": "Filter by DAT",
        on: { change: (e) => { state.resultDat = e.target.value; renderScan(); } },
      },
      el("option", { value: "", text: "All DATs", selected: !state.resultDat }),
      ...scan.dat_names.map((n) => el("option", { value: n, text: shortDat(n), selected: n === state.resultDat })));
      filters.push(el("div", { class: "row" }, el("span", { class: "label-inline", text: "DAT" }), select));
    }
    let table = null;
    const reload = () => { if (table) { table.offset = 0; table.load(); } };
    if (activeTab === "games") {
      const counts = { 1: have, 0: missing };
      const chipBox = el("div", { class: "chips" });
      const drawHave = () => chipBox.replaceChildren(...[["", "All games", total], ["1", "Have", counts[1]], ["0", "Missing", counts[0]]].map(([key, label, n]) =>
        el("button", {
          class: `chip ${key === "1" ? "chip-have" : key === "0" ? "chip-missing" : ""} ${state.gamesHave === key ? "active" : ""}`,
          text: `${label} (${fmt(n)})`, on: { click: () => { state.gamesHave = key; drawHave(); reload(); } },
        })));
      drawHave();
      filters.push(chipBox);
    }
    const tagBar = TAG_TABS.has(activeTab) ? el("div", { class: "tag-filters" }) : null;
    $("result-table").replaceChildren(...[...filters, tagBar, container].filter(Boolean));
    table = new PagedTable(container, resultTableOptions(activeTab, tagBar, reload));
    table.load();
  }

  /** Name tag chips plus the Dreamcast level chip in ONE chip row (the tag row alone for other systems). */
  function gameChips(tags, level) {
    const base = tagChips(tags);
    if (!level) return base;
    return el("div", { class: "tags" }, ...(base ? Array.from(base.childNodes) : []), levelChip(level));
  }

  /** Sega Dreamcast: which engine reads the CHDs, and the Verify fully button. */
  function renderDcBar(s) {
    const dc = s.chd_files !== undefined;
    $("dc-bar").classList.toggle("hidden", !dc);
    if (!dc) return;
    $("dc-engine-line").textContent = s.engine === "chdman"
      ? `CHDs were read with chdman (${s.chdman}) - every track was checked, so they are verified.`
      : "CHDs were read with the built-in reader (no chdman needed): data tracks are hashed, audio is checked by length "
        + "until you press Verify fully." + (s.identified ? ` ${fmt(s.identified)} CHD(s) are still only identified.` : "")
        + (s.needs_chdman ? ` ${fmt(s.needs_chdman)} CHD(s) use a compression the built-in reader cannot decode (zstd) - they are left in place; install chdman to identify them.` : "");
    const tempText = s.temp_text || (s.temp && s.temp.text) || "";
    $("dc-temp-line").textContent = tempText;
    $("dc-temp-line").classList.toggle("hidden", !tempText);
    $("dc-verify-btn").dataset.blocked = s.identified ? "0" : "1";
    $("dc-verify-btn").title = s.identified ? "Decode EVERY track of the identified CHDs (audio included) and compare it with Redump - can take a while"
      : "Every CHD is already verified";
    Jobs.setRunning(Jobs.running);
  }

  async function verifyFully() {
    const s = (state.scan && state.scan.summary) || {};
    if (!s.identified) { toast("Every CHD is already verified.", "ok"); return; }
    const body = el("div", {},
      el("p", { text: `Decode every track - audio included - of ${fmt(s.identified)} CHD${s.identified === 1 ? "" : "s"} and compare it with Redump?` }),
      el("ul", {},
        el("li", { text: "Nothing is changed in your folder; the result is remembered, so this is done once per file." }),
        el("li", { text: "With chdman installed each disc is extracted to scratch space (about its full size) and deleted again: in RAM when that fits with a safe reserve, otherwise in the app\u2019s cache folder - never in your game folder. If neither has room the built-in reader is used." }),
        el("li", { text: "Without chdman the built-in reader decodes data at roughly 30-45 MB/s per CD/DVD (a 8 GB PlayStation 2 DVD takes about 3-4 minutes; several files run side by side) and FLAC audio at about 1 MB/s (a disc with 400 MB of audio takes minutes). You can cancel at any time." })));
    if (!(await confirmDialog({ title: "Verify fully", body, okText: "Verify" }))) return;
    Jobs.start("/api/dc/verify", {});
  }

  Jobs.handlers.verify = async (job) => {
    if ((job.status === "done" || job.status === "cancelled") && job.result) {
      const r = job.result;
      const failed = Array.isArray(r.failed) ? r.failed : [];
      toast(`Verified ${fmt(r.verified || 0)} CHD(s)${failed.length ? `, ${fmt(failed.length)} do not match Redump` : ""}`, failed.length ? "error" : "ok", 8000);
      showFailures("dc-failures", "Does not match Redump:", failed, (f) => `${f.file}: ${f.error}`,
        [r.rescan_error ? `The folder could not be re-scanned (${r.rescan_error}) - scan it again.` : ""]);
    }
    await refreshScan();
  };

  const first = (...vals) => vals.find((v) => v !== undefined && v !== null);
  const TAG_FACETS = [["region", "regions", "Region"], ["language", "languages", "Language"],
    ["video", "video", "Video"], ["flag", "flags", "Tag"], ["rule", "rules", "Library rules"]];
  /** Chip text of a facet value ("bad" is the bad-dump flag, rules show their short name). */
  const facetLabel = (key, value) => (key === "rule" ? ruleShort(value) : key === "flag" && value === "bad" ? "Bad dump" : value);
  const TAG_SHOWN = 8;

  /** Filter chips built from the server's facets (filters only change what is shown). */
  function renderTagBar(box, facets, reload) {
    if (!box) return;
    const rows = [];
    for (const [key, facetKey, label] of TAG_FACETS) {
      let entries = Object.entries((facets || {})[facetKey] || {});
      // "Bad dump" would show twice (tag row and rule row) when both filter the same rows
      if (key === "rule" && state.tagFilter.rule !== "bad_dump"
          && ((facets || {}).rules || {}).bad_dump !== undefined
          && ((facets || {}).rules || {}).bad_dump === ((facets || {}).flags || {}).bad) {
        entries = entries.filter(([v]) => v !== "bad_dump");
      }
      const current = state.tagFilter[key];
      if (!entries.length && !current) continue;
      const expanded = !!state.tagExpanded[key];
      let shown = expanded ? entries : entries.slice(0, TAG_SHOWN);
      if (current && !shown.some(([v]) => v === current)) shown = shown.concat([[current, (facets[facetKey] || {})[current] || 0]]);
      const pick = (value) => { state.tagFilter[key] = state.tagFilter[key] === value ? "" : value; reload(); };
      const chips = [el("button", { class: `chip chip-sm ${current ? "" : "active"}`, text: "Any", on: { click: () => pick("") } }),
        ...shown.map(([value, n]) => el("button", {
          class: `chip chip-sm ${current === value ? "active" : ""} ${key === "flag" && value === "bad" ? "chip-bad" : ""}`,
          text: `${facetLabel(key, value)} (${fmt(n)})`, title: key === "rule" ? "Rows that a library rule would set aside" : null,
          on: { click: () => pick(value) },
        }))];
      if (entries.length > TAG_SHOWN) {
        chips.push(el("button", {
          class: "chip chip-sm chip-more", text: expanded ? "Fewer" : `More (${fmt(entries.length - TAG_SHOWN)})`,
          on: { click: () => { state.tagExpanded[key] = !expanded; renderTagBar(box, facets, reload); } },
        }));
      }
      rows.push(el("div", { class: "tag-filter-row" }, el("span", { class: "tag-filter-label", text: label }), el("div", { class: "chips" }, chips)));
    }
    box.replaceChildren(...rows);
    box.classList.toggle("hidden", !rows.length);
  }

  function fileCell(path) {
    const [dir, name] = splitPath(path);
    return el("div", {}, el("div", { text: name }), dir ? el("div", { class: "sub", text: dir }) : null);
  }

  function resultTableOptions(kind, tagBar, reload) {
    const dat = DAT_FILTER_TABS.has(kind) ? state.resultDat : "";
    const fetchKind = async ({ offset, limit, q }) => {
      const have = kind === "games" ? state.gamesHave : "";
      const tagParams = TAG_TABS.has(kind) ? state.tagFilter : {};
      const data = await get(`/api/scan/results?${qs({ kind, offset, limit, q, dat, have, ...tagParams })}`);
      if (tagBar) renderTagBar(tagBar, data.facets, reload);
      return data;
    };
    const multi = state.scan && state.scan.dat_names.length > 1;
    switch (kind) {
      case "games":
        return {
          fetch: fetchKind, placeholder: "Search games or file names...", emptyText: "No games in this category.",
          rowClass: (i) => (i.have ? "row-have" : "row-missing"),
          columns: [
            { label: "", render: (i) => badge(i.have ? "ok" : "missing", i.have ? "have" : "missing") },
            {
              label: "Game", cls: "wrap", render: (i) => el("div", {},
                el("div", { class: "game-name", text: i.name }),
                gameChips(i.tags, i.level),
                multi && !dat ? el("div", { class: "sub", text: shortDat(i.dat || "") }) : null),
            },
            {
              label: "Your file(s)", cls: "wrap", render: (i) => (i.files && i.files.length
                ? el("div", {}, ...i.files.slice(0, 3).map((f) => el("div", { class: "mono-path small", text: f })),
                  i.files.length > 3 ? el("div", { class: "sub", text: `+${i.files.length - 3} more` }) : null)
                : el("span", { class: "muted", text: "-" })),
            },
          ],
        };
      case "matched":
        return {
          fetch: fetchKind, placeholder: "Search matched files or DAT names...", emptyText: "No files matched.",
          columns: [
            { label: "Local file", cls: "wrap", render: (i) => fileCell(i.file) },
            {
              label: "DAT entry", cls: "wrap", render: (i) => el("div", {},
                el("div", { text: i.game || i.roms[0] || "" }),
                i.level ? el("div", { class: "tags" }, levelChip(i.level), i.engine ? el("span", { class: "tag", text: i.engine === "files" ? "files" : `via ${i.engine}` }) : null) : null,
                tagChips(i.tags, i.level ? "" : i.via, i.byte_order, i.header),
                multi && !dat ? el("div", { class: "sub", text: shortDat(i.dat) }) : null,
                i.roms.length > 1 ? el("div", { class: "sub", text: `+${i.roms.length - 1} other name(s) with identical content` }) : null,
                i.other_dats && i.other_dats.length ? el("div", { class: "sub", text: `also in: ${i.other_dats.map(shortDat).join(", ")}` }) : null),
            },
            {
              label: "Place", render: (i) => {
                if (i.placed_ok && i.named_ok) return badge("ok");
                return el("span", { class: "badge move", text: i.placed_ok ? "rename" : "move" });
              },
            },
            { label: "Size", cls: "num", render: (i) => fmtBytes(i.size) },
          ],
        };
      case "missing":
        return {
          fetch: fetchKind, placeholder: "Search missing entries...", emptyText: "Nothing missing - complete set!",
          rowClass: () => "row-missing",
          columns: [
            {
              label: "DAT entry", cls: "wrap", render: (i) => el("div", {}, el("div", { text: i.set_name || i.name }),
                tagChips(i.tags),
                multi && !dat ? el("div", { class: "sub", text: shortDat(i.dat || "") }) : null),
            },
            { label: "Size", cls: "num", render: (i) => fmtBytes(i.size) },
            { label: "CRC32", cls: "mono", render: (i) => i.crc },
          ],
        };
      case "unmatched":
        return {
          fetch: fetchKind, placeholder: "Search unmatched files...", emptyText: "Every file matched a DAT.",
          columns: [
            { label: "Local file", cls: "wrap", render: (i) => fileCell(i.file) },
            ...(isGameFolder(currentPlatform()) ? [{ label: "Why", cls: "wrap", render: (i) => el("span", { class: "muted", text: i.reason || "" }) }] : []),
            { label: "Size", cls: "num", render: (i) => fmtBytes(i.size) },
            ...(isGameFolder(currentPlatform()) ? [] : [{ label: "CRC32", cls: "mono", render: (i) => i.crc || "" }]),
          ],
        };
      case "unsupported":
        return {
          fetch: fetchKind, emptyText: "None.",
          columns: [{ label: "Archive (install 7-Zip to read .7z / .rar)", cls: "wrap", render: (i) => fileCell(i.file) }],
        };
      default:
        return {
          fetch: fetchKind, emptyText: "No errors.",
          columns: [
            { label: "File", cls: "wrap", render: (i) => fileCell(i.file) },
            { label: "Error", cls: "wrap", render: (i) => i.error },
          ],
        };
    }
  }

  // ---------------------------------------------------- 3. Build library
  // The reserved folders: all direct children of the platform folder (the layout sketch lists them).
  const reserved = (hasM3u, convertible, protectedDirs = []) => [
    [UNMATCHED, "only files that matched nothing"],
    [EXCLUDED, "bad dumps, betas, demos, language/flag exclusions (library rules)"],
    [SUPERSEDED, "older versions / worse variants"],
    ...(hasM3u ? [[INCOMPLETE, "multi-disk games with missing disks"]] : []),
    [DUPLICATES, "extra copies of the same ROM"],
    ...(convertible ? [[CONVERTED, "originals kept by Convert"]] : []),
    ...protectedDirs.map((d) => [d, "your files - left alone by every step"]),
  ].map(([name, text]) => `  ${(name + "/").padEnd(23)}${text}`);
  const REASON_LABEL = { kept: "Kept", excluded: "Excluded", superseded: "Superseded", incomplete: "Incomplete",
    duplicate: "Duplicates", unmatched: "Unmatched", playlist: "Playlists" };
  const REASON_ORDER = ["kept", "excluded", "superseded", "incomplete", "duplicate", "unmatched", "playlist"];
  const REASON_BADGE = { kept: "ok", excluded: "bad", superseded: "info", incomplete: "warn", duplicate: "warn",
    unmatched: "skip", playlist: "move" };
  let libTable = null;
  let vanishTable = null;
  let libPlan = null;

  const libOptions = () => ({
    move_unmatched: $("lib-move-unmatched").checked, labels: $("lib-labels").checked, savedisk: $("lib-savedisk").checked,
  });

  async function loadLibraryProfile() {
    const name = state.platform;
    if (!name) return;
    try {
      state.library[name] = await get(`/api/library/profile?${qs({ platform: name })}`);
    } catch (err) {
      state.library[name] = null;
      $("library-rules").replaceChildren(el("div", { class: "muted", text: `Library rules are not available: ${err.message}` }));
      return;
    }
    if (name === state.platform) renderLibraryRules();
  }

  // Everything below is rendered from the server's rule catalog (library.rule_catalog / profile_info): no rule list lives here.
  const GROUP_TITLE = { exclude: "Exclude", keep_flag: "Keep these dump types" };
  const catalogOf = (info) => info.catalog || [];
  const optionEntries = (info) => catalogOf(info).filter((e) => e.kind === "option" && e.id !== "languages" && e.id !== "region_priority"
    && (info.available || {})[e.id] !== false);

  /** Human label of an exclusion reason code ("bad_dump", "flag_cr", "language") from the catalog. */
  function reasonLabel(code) {
    const info = state.library[state.platform];
    const entries = info ? catalogOf(info) : [];
    if (code === "language") return "Language";
    if (code.startsWith("flag_")) {
      const f = entries.find((e) => e.kind === "keep_flag" && e.id === code.slice(5));
      return f ? `${plainLabel(f)} off` : code;
    }
    const r = entries.find((e) => e.kind === "exclude" && e.id === code);
    return r ? plainLabel(r) : (RULE_SHORT[code] || code);
  }

  const langName = (info, code) => ((info.language_names || {})[code]) || code;

  async function saveProfile(changes) {
    const name = state.platform;
    const before = state.library[name];
    try {
      state.library[name] = await post("/api/library/profile", { platform: name, ...changes });
      const p = currentPlatform();
      if (p) { p.library = state.library[name].profile; p.latest_only = !!p.library.latest_only; }
      $("organise-latest-only").checked = !!(p && p.latest_only);
      profileChanged();
    } catch (err) {
      toast(`Could not save the library rules: ${err.message}`, "error");
      state.library[name] = await get(`/api/library/profile?${qs({ platform: name })}`).catch(() => before);
    }
    renderLibraryRules();
  }

  /** Move item i of a list by dir (-1 up, +1 down); returns a new array. */
  function moved(list, i, dir) {
    const out = [...list], j = i + dir;
    if (j < 0 || j >= out.length) return out;
    [out[i], out[j]] = [out[j], out[i]];
    return out;
  }

  const tokenChips = (tokens) => (tokens && tokens.length
    ? el("span", { class: "toks" }, tokens.map((t) => el("code", { class: "tok", text: t }))) : null);

  /** The catalog label without its trailing "[b]" style tokens (the chips next to it show the exact tokens). */
  const plainLabel = (entry) => (entry.tokens && entry.tokens.length ? entry.label.replace(/(\s*\[[^\]]*\])+\s*$/, "") || entry.label : entry.label);

  /** One rule row: checkbox, label, the exact tokens as chips, a live count and a one-line description. */
  function ruleRow(entry, checked, onChange, countKey, countWhen = true) {
    return el("label", { class: "check rule-check", title: entry.description || entry.label },
      el("input", { type: "checkbox", checked, "data-rule": entry.id, on: { change: (e) => onChange(e.target.checked) } }),
      el("span", { class: "rule-text" },
        el("span", { class: "rule-line" }, el("span", { class: "rule-label", text: plainLabel(entry) }), tokenChips(entry.tokens),
          countKey ? el("span", { class: "rule-count", "data-count": countKey, "data-when": checked === countWhen ? "on" : "off" }) : null),
        el("span", { class: "sub", text: entry.description || "" })));
  }

  function languageSection(info) {
    const prof = info.profile, avail = info.available_languages || [];
    const codes = avail.map((l) => l.code);
    const known = [...avail, ...prof.languages.filter((c) => !codes.includes(c)).map((c) => ({ code: c, name: langName(info, c), count: null, games: null }))];
    const set = (list) => saveProfile({ languages: list });
    const boxes = known.map((l) => el("label", { class: "check lang-check", title: `${l.name}${l.games ? ` - ${fmt(l.games)} titles` : ""}` },
      el("input", {
        type: "checkbox", checked: prof.languages.includes(l.code), "data-lang": l.code,
        on: { change: (e) => set(e.target.checked ? [...prof.languages, l.code] : prof.languages.filter((c) => c !== l.code)) },
      }),
      el("span", {}, l.name, l.count !== null ? el("span", { class: "sub inline", text: ` ${fmt(l.count)}` }) : null)));
    const order = prof.languages.length > 1 ? el("div", { class: "lang-order" },
      el("span", { class: "muted small", text: "Preferred first:" }),
      ...prof.languages.map((c, i) => el("span", { class: "lang-pill" },
        el("span", { text: `${i + 1}. ${langName(info, c)}` }),
        el("button", { class: "btn btn-icon", text: "▲", title: "Prefer more", "aria-label": `Move ${langName(info, c)} up`, disabled: i === 0,
          on: { click: () => set(moved(prof.languages, i, -1)) } }),
        el("button", { class: "btn btn-icon", text: "▼", title: "Prefer less", "aria-label": `Move ${langName(info, c)} down`, disabled: i === prof.languages.length - 1,
          on: { click: () => set(moved(prof.languages, i, 1)) } })))) : null;
    return el("div", { class: "rules-group", id: "rules-languages" },
      el("div", { class: "rules-head", text: "Languages" }),
      el("div", { class: "sub note", text: info.style === "whdload"
        ? ((catalogOf(info).find((e) => e.id === "languages") || {}).description || "No language tag means English.")
        : "No language or country tag means English/neutral (TOSEC convention); (de-en) counts as English; [tr en] counts as an English translation." }),
      known.length ? el("div", { class: "lang-grid" }, boxes) : el("div", { class: "muted small", text: "Languages appear here once the DAT is installed." }),
      order,
      el("div", { class: "sub", text: prof.languages.length ? "Titles with no version in a ticked language are left out." : "Nothing ticked: every language stays." }),
      el("div", { class: "rule-count vanish-note", "data-vanish": "language" }));
  }

  function regionSection(info) {
    const prof = info.profile, list = info.regions || [];
    return el("div", { class: "rules-group", id: "rules-regions" },
      el("div", { class: "rules-head", text: "Region priority" }),
      el("div", { class: "sub note", text: (catalogOf(info).find((e) => e.id === "region_priority") || {}).description || "Best region first." }),
      el("ol", { class: "region-list" }, list.map((r, i) => el("li", { class: i < 4 ? "prio" : "" },
        el("span", { class: "region-n", text: String(i + 1) }), el("span", { class: "region-name", text: r }),
        el("button", { class: "btn btn-icon", text: "▲", title: "Prefer more", "aria-label": `Move ${r} up`, disabled: i === 0,
          on: { click: () => saveProfile({ region_priority: moved(list, i, -1) }) } }),
        el("button", { class: "btn btn-icon", text: "▼", title: "Prefer less", "aria-label": `Move ${r} down`, disabled: i === list.length - 1,
          on: { click: () => saveProfile({ region_priority: moved(list, i, 1) }) } })))));
  }

  /** The Library rules panel, rendered from ``info.catalog``. */
  function renderLibraryRules() {
    const info = state.library[state.platform];
    if (!info) return;
    const prof = info.profile, avail = info.available || {}, scopes = info.scopes || {};
    const scope = (dats) => (dats && dats.length ? `applies to: ${dats.map(shortDat).join(", ")}` : "");
    const cat = catalogOf(info);
    const groups = [];

    const excl = cat.filter((e) => e.kind === "exclude");
    if (excl.length) {
      groups.push(el("div", { class: "rules-group", id: "rules-exclude" },
        el("div", { class: "rules-head", text: `${GROUP_TITLE.exclude} (moved to _excluded/)` }),
        el("div", { class: "rules-grid" }, excl.map((e) => ruleRow(e, prof.exclude.includes(e.id),
          (on) => saveProfile({ exclude: on ? [...prof.exclude, e.id] : prof.exclude.filter((x) => x !== e.id) }), `excluded_${e.id}`))),
        el("div", { class: "sub", text: scope(scopes.exclude_dats) })));
    }
    const flags = cat.filter((e) => e.kind === "keep_flag");
    if (flags.length && avail.keep_flags !== false) {
      const cr = flags.find((e) => e.id === "cr");
      groups.push(el("div", { class: "rules-group", id: "rules-flags" },
        el("div", { class: "rules-head", text: GROUP_TITLE.keep_flag }),
        el("div", { class: "sub note", text: `Untick a type to leave out every variant that carries it. ${scope(scopes.best_variant_dats)}` }),
        el("div", { class: "rules-grid" }, flags.map((e) => ruleRow(e, prof.keep_flags.includes(e.id),
          (on) => saveProfile({ keep_flags: on ? [...prof.keep_flags, e.id] : prof.keep_flags.filter((x) => x !== e.id) }), `excluded_flag_${e.id}`, false))),
        cr && !prof.keep_flags.includes("cr") ? el("div", { class: "notice cr-warning", id: "cr-warning" },
          el("b", { text: "Cracks are off. " }),
          "Many games only exist as cracked dumps, so they will disappear from your library. ",
          el("span", { "data-vanish": "flag_cr" }), " ",
          el("button", { class: "btn btn-small", text: "Keep cracks again", on: { click: () => saveProfile({ keep_flags: [...prof.keep_flags, "cr"] }) } })) : null));
    }
    const opts = optionEntries(info);
    if (opts.length || info.ranking) {
      const datsOf = { latest_only: scopes.latest_dats, best_variant: scopes.best_variant_dats, complete_only: scopes.complete_dats,
        borrow_editions: scopes.best_variant_dats };
      groups.push(el("div", { class: "rules-group", id: "rules-options" },
        el("div", { class: "rules-head", text: "Options" }),
        el("div", { class: "rules-grid" }, opts.map((e) => {
          const row = ruleRow(e, !!prof[e.field], (on) => saveProfile({ [e.field]: on }), null);
          row.querySelector("input").dataset.opt = e.field;
          const sc = scope(datsOf[e.id]);
          if (sc) row.querySelector(".rule-text").append(el("span", { class: "sub", text: sc }));
          return row;
        })),
        info.ranking ? el("div", { class: "sub ranking", id: "rules-ranking", text: info.ranking }) : null));
    }
    if (avail.languages && cat.some((e) => e.id === "languages")) groups.push(languageSection(info));
    if (avail.region_priority && cat.some((e) => e.id === "region_priority")) groups.push(regionSection(info));
    if (!groups.length) groups.push(el("div", { class: "muted", text: "No library rules apply to this system." }));
    $("library-rules").replaceChildren(...groups);
    const on = excl.filter((r) => prof.exclude.includes(r.id)).length;
    $("library-rules-summary").textContent = ` - ${on} of ${excl.length} exclusion rules on`
      + (avail.languages ? `, languages: ${prof.languages.length ? prof.languages.join(", ") : "all"}` : "");
    $("library-rules-note").textContent = JSON.stringify(prof) === JSON.stringify(info.defaults) ? "Default rules" : "Custom rules";
    updateRuleCounts();
  }

  /** Fill the live numbers of the rules panel from the last plan (``libStats``). */
  function updateRuleCounts() {
    const st = state.libStats;
    for (const node of document.querySelectorAll("#library-rules [data-count]")) {
      const n = st ? (st.reasons || {})[node.dataset.count] : null;
      const show = st && node.dataset.when === "on" && n;
      node.textContent = show ? `${fmt(n)} file${n === 1 ? "" : "s"}` : "";
      node.classList.toggle("hidden", !show);
    }
    for (const node of document.querySelectorAll("#library-rules [data-vanish]")) {
      const code = node.dataset.vanish;
      const n = st && st.vanish ? ((st.vanish.by_code || {})[code] || (st.vanish.by_reason || {})[code] || 0) : null;
      const what = code === "language" ? "have no version in the ticked languages" : "exist only with that dump type";
      node.textContent = st ? (n ? `${fmt(n)} title${n === 1 ? "" : "s"} ${what} (see "Games that vanish" in the preview).` : "")
        : (code === "language" ? "" : "Press Preview library to see how many titles this affects.");
    }
  }

  /** Background refresh of the plan numbers (rules panel counts) after a scan or a rules change. */
  let statsSeq = 0;
  const refreshLibraryStats = debounce(async () => {
    if (!state.scan) { state.libStats = null; updateRuleCounts(); return; }
    const seq = ++statsSeq;
    try {
      const data = await post("/api/library/plan", { ...libOptions(), limit: 1 });
      if (seq !== statsSeq) return;
      state.libStats = { reasons: data.reasons || {}, vanish: data.vanish || {}, exclusions: data.exclusions || {} };
    } catch (err) {
      if (seq === statsSeq) state.libStats = null;
    }
    updateRuleCounts();
  }, 500);

  /** The profile changed: every cached preview is out of date. */
  function profileChanged() {
    libTable = null;
    vanishTable = null;
    libPlan = null;
    state.libStats = null;
    state.libWhy = "";
    updateRuleCounts();
    refreshLibraryStats();
    $("lib-output").classList.add("hidden");
    $("lib-empty").textContent = state.scan ? "Rules changed - press \"Preview library\" to see what would happen." : "Run a scan first.";
    $("lib-empty").classList.remove("hidden");
    organiseTable = null;
    convertTable = null;
    $("organise-output").classList.add("hidden");
    $("convert-output").classList.add("hidden");
    updateActionState();
  }

  function renderLibraryIntro() {
    $("lib-empty").textContent = state.scan ? "Press \"Preview library\" to see what would happen." : "Run a scan first.";
    $("lib-empty").classList.remove("hidden");
    $("lib-output").classList.add("hidden");
    libTable = null;
    vanishTable = null;
    libPlan = null;
    refreshUndo();
    refreshLibraryStats();
  }

  function renderLibraryCards(plan) {
    const r = plan.reasons || {}, pl = plan.playlists || {}, vanish = plan.vanish || {};
    $("lib-cards").replaceChildren(
      card(fmt(r.kept), "Kept", "ok"),
      card(fmt((r.renamed || 0) + (r.moved || 0)), `Renamed / moved (${fmt(r.renamed || 0)} / ${fmt(r.moved || 0)})`, "info"),
      card(fmt(r.excluded), "Excluded", r.excluded ? "warn" : ""),
      card(fmt(r.superseded), "Superseded", r.superseded ? "warn" : ""),
      ...(isGameFolder(currentPlatform()) ? [] : [card(fmt(r.incomplete), "Incomplete", r.incomplete ? "warn" : "")]),
      card(fmt(r.duplicates), "Duplicates", r.duplicates ? "warn" : ""),
      ...(r.unmatched ? [card(fmt(r.unmatched), `Unmatched → ${UNMATCHED}/`, "warn")] : []),
      ...(pl.write || pl.remove || pl.ok ? [card(fmt(pl.write), "Playlists to write", pl.write ? "info" : ""),
        card(fmt(pl.remove), "Playlists to remove", pl.remove ? "warn" : "")] : []),
      ...(r.borrowed_sets ? [card(fmt(r.borrowed_sets), `Sets completed with borrowed disks (${fmt(r.borrowed_disks || 0)} disk${r.borrowed_disks === 1 ? "" : "s"})`, "info")] : []),
      ...(r.conflict || pl.conflict ? [card(fmt((r.conflict || 0) + (pl.conflict || 0)), "Conflicts (skipped)", "bad")] : []),
      ...(vanish.titles ? [card(fmt(vanish.titles), "Games that vanish", "warn")] : []),
    );
    state.libStats = { reasons: r, vanish, exclusions: plan.exclusions || {} };
    updateRuleCounts();
    // excluded files per reason (language, flag_*, rules): click one to list just those rows
    const why = {};
    for (const [k, n] of Object.entries(r)) if (k.startsWith("excluded_") && n) why[k.slice(9)] = n;
    const whyBox = $("lib-why-filters");
    whyBox.classList.toggle("hidden", !Object.keys(why).length);
    const infoNow = state.library[state.platform];
    const whyOrder = infoNow ? [...catalogOf(infoNow).filter((e) => e.kind === "exclude").map((e) => e.id),
      ...catalogOf(infoNow).filter((e) => e.kind === "keep_flag").map((e) => `flag_${e.id}`), "language"] : [];
    filterChips(whyBox, why, state.libWhy, whyOrder, (key) => { state.libWhy = key; if (key) state.libReason = "excluded"; libTable.offset = 0; libTable.load(); },
      reasonLabel, "All exclusions");
    renderVanishBox(plan);
    $("lib-uptodate").classList.toggle("hidden", !plan.empty);
    const sets = plan.incomplete_sets || [];
    $("lib-incomplete-box").classList.toggle("hidden", !sets.length);
    if (sets.length && sets.length <= 15) $("lib-incomplete-box").open = true;
    const total = plan.incomplete_total || sets.length;
    $("lib-incomplete-title").textContent = isGameFolder(currentPlatform())
      ? `Multi-disc games with a disc missing (${fmt(total)}) - kept where they are, no playlist`
      : `Incomplete sets (${fmt(total)}) - disks missing, set aside as _incomplete`;
    $("lib-incomplete").replaceChildren(...sets.map((x) => el("li", {},
      el("span", { text: x.name }), " ",
      el("span", { class: "tag tag-status", text: `missing ${isGameFolder(currentPlatform()) ? "disc" : "disk"} ${x.missing.join(", ")} of ${x.total}` }),
      el("span", { class: "sub", text: ` have ${x.present.length ? x.present.join(", ") : "none"} - ${shortDat(x.dat)}` }))),
    ...(total > sets.length ? [el("li", { class: "muted", text: `... and ${fmt(total - sets.length)} more` })] : []));
  }

  /** Reason / note cell of one preview row. */
  function libraryNote(i) {
    if (i.item === "playlist") {
      return el("div", {}, el("span", { class: "muted", text: i.reason || `${i.disks} disk${i.disks === 1 ? "" : "s"}` }),
        ...(i.notes || []).map((n) => el("div", { class: "borrow-note small" }, el("span", { class: "tag tag-status", text: "borrowed" }), " ", n)));
    }
    const bits = [];
    if (i.code === "excluded") {
      bits.push(el("span", { class: "tag tag-bad", text: i.flags_text || i.reason }));
      for (const c of i.reasons || []) bits.push(el("span", { class: "tag tag-excl", text: reasonLabel(c) }));
    }
    if (i.code === "incomplete" && i.missing && i.missing.length) bits.push(el("span", { class: "tag tag-status", text: `missing disk ${i.missing.join(", ")}` }));
    if (i.code === "duplicate" && i.keeper) bits.push(el("span", { class: "sub", text: `keeping ${i.keeper}` }));
    if (i.code === "superseded" && i.superseded_by) bits.push(el("span", { class: "sub", text: `better: ${i.superseded_by}` }));
    return el("div", {}, ...bits, i.reason ? el("div", { class: "muted small", text: i.reason }) : null);
  }

  // ---- "Games that vanish": titles none of whose variants stay
  function vanishText(reason) {
    if (reason === "language") return "No version in your selected languages";
    if (reason.startsWith("flag_")) {
      const f = ((state.library[state.platform] || {}).catalog || []).find((e) => e.kind === "keep_flag" && e.id === reason.slice(5));
      return `Only available as ${f ? plainLabel(f).toLowerCase() : reason.slice(5)} variants, but those are off`;
    }
    if (reason === "incomplete") return "Only incomplete multi-disk sets";
    return `Every version is excluded: ${reasonLabel(reason)}`;
  }

  function renderVanishBox(plan) {
    const v = plan.vanish || {};
    const box = $("lib-vanish-box");
    box.classList.toggle("hidden", !v.titles);
    if (!v.titles) return;
    $("lib-vanish-title").textContent = `Games that vanish (${fmt(v.titles)}) - no version is kept`;
    const reasons = v.by_reason || {};
    filterChips($("lib-vanish-filters"), reasons, state.vanishReason, ["language", "flag_cr", "flag_h", "flag_t", "flag_a", "flag_f", "flag_tr"],
      (key) => { state.vanishReason = key; renderVanishBox(plan); if (vanishTable) { vanishTable.offset = 0; vanishTable.load(); } },
      (k) => (k === "language" ? "No version in selected languages" : vanishText(k)), "All reasons");
    if (box.open && !vanishTable) showVanishTable();
  }

  function renderVanishHints(data) {
    const info = state.library[state.platform];
    const hints = [];
    if (info && (data.by_reason || {}).language) {
      const have = new Set(info.profile.languages);
      const langs = Object.entries(data.by_language || {}).filter(([c]) => !have.has(c)).sort((a, b) => b[1] - a[1]).slice(0, 4);
      if (langs.length) {
        hints.push(el("span", { class: "muted small", text: "Quick fix - keep these by ticking a language:" }),
          ...langs.map(([c, n]) => el("button", { class: "btn btn-small", text: `+ ${langName(info, c)} (${fmt(n)})`,
            title: `Tick ${langName(info, c)}: ${fmt(n)} of these titles come back`, on: { click: () => saveProfile({ languages: [...info.profile.languages, c] }) } })));
      }
    }
    for (const flag of ["cr", "h", "t", "a", "f", "tr"]) {
      if (info && (data.by_reason || {})[`flag_${flag}`] && !info.profile.keep_flags.includes(flag)) {
        const e = catalogOf(info).find((x) => x.id === flag && x.kind === "keep_flag");
        hints.push(el("button", { class: "btn btn-small", text: `Keep ${e ? plainLabel(e).toLowerCase() : flag} again (${fmt(data.by_reason[`flag_${flag}`])})`,
          on: { click: () => saveProfile({ keep_flags: [...info.profile.keep_flags, flag] }) } }));
      }
    }
    $("lib-vanish-hints").replaceChildren(...hints);
  }

  function showVanishTable() {
    vanishTable = new PagedTable($("lib-vanish-table"), {
      placeholder: "Search titles...", emptyText: "Nothing in this category.", pageSize: 25,
      fetch: async ({ offset, limit, q }) => {
        const data = await post("/api/library/vanished", { ...libOptions(), reason: state.vanishReason, offset, limit, q });
        renderVanishHints(data);
        return data;
      },
      columns: [
        { label: "Title", cls: "wrap", render: (i) => el("div", {}, el("div", { text: i.title }), el("div", { class: "sub mono-path", text: i.name }))},
        { label: "Why it vanishes", cls: "wrap", render: (i) => el("div", {}, el("div", { text: vanishText(i.reason) }),
          el("div", { class: "tags" }, (i.codes || []).map((c) => el("span", { class: "tag tag-excl", text: reasonLabel(c) })),
            (i.languages || []).map((l) => el("span", { class: "tag tag-lang", text: l })))) },
        { label: "Variants", render: (i) => String(i.variants) },
      ],
    });
    vanishTable.load();
  }

  async function showLibraryPlan() {
    $("lib-empty").classList.add("hidden");
    $("lib-output").classList.remove("hidden");
    if (!libTable) {
      vanishTable = null;
      libTable = new PagedTable($("lib-table"), {
        placeholder: "Search file names, folders or playlists...",
        emptyText: "Nothing in this category.",
        fetch: async ({ offset, limit, q }) => {
          const data = await post("/api/library/plan", {
            ...libOptions(), reason: state.libReason, status: state.libStatus, why: state.libWhy, offset, limit, q,
          });
          libPlan = data;
          renderLibraryCards(data);
          const box = $("lib-warnings");
          const notes = [...(data.warnings || [])];
          if (data.missing_dats && data.missing_dats.length) notes.push(`Not checked (DAT not installed): ${data.missing_dats.map(shortDat).join(", ")}.`);
          box.classList.toggle("hidden", !notes.length);
          box.replaceChildren(...notes.map((n) => el("div", { text: n })));
          const reload = () => { libTable.offset = 0; libTable.load(); };
          const r = data.reasons || {}, pl = data.playlists || {};
          const rc = { kept: r.kept, excluded: r.excluded, superseded: r.superseded, incomplete: r.incomplete,
            duplicate: r.duplicates, unmatched: r.unmatched, playlist: (pl.write || 0) + (pl.ok || 0) + (pl.remove || 0) + (pl.conflict || 0) };
          filterChips($("lib-reason-filters"), Object.fromEntries(Object.entries(rc).filter(([, n]) => n)), state.libReason, REASON_ORDER,
            (key) => { state.libReason = key; if (key !== "excluded") state.libWhy = ""; reload(); }, (k) => REASON_LABEL[k] || k, "All reasons");
          filterChips($("lib-status-filters"), data.counts || {}, state.libStatus, ["move", "rename", "delete", "conflict", "skip", "ok"],
            (key) => { state.libStatus = key; reload(); }, (k) => k, "All statuses");
          $("lib-apply-btn").dataset.blocked = data.empty ? "1" : "0";
          Jobs.setRunning(Jobs.running);
          return data;
        },
        columns: [
          { label: "Status", render: (i) => badge(i.status) },
          { label: "Reason", render: (i) => badge(REASON_BADGE[i.category] || "skip", REASON_LABEL[i.category] || i.category) },
          {
            label: "Change (relative to the system folder)", cls: "wrap", render: (i) => {
              if (i.item === "playlist") {
                return el("div", {}, el("div", { class: "rename-to mono-path", text: i.path }),
                  i.lines && i.lines.length ? el("details", {}, el("summary", { text: `${i.disks} disk${i.disks === 1 ? "" : "s"}` }),
                    el("pre", { class: "m3u-lines", text: i.lines.join("\n") })) : null);
              }
              const changed = i.from !== i.to;
              return el("div", {},
                el("div", { class: changed ? "rename-from mono-path" : "rename-to mono-path", text: i.from }),
                changed ? el("div", {}, el("span", { class: "rename-arrow", text: "→ " }), el("span", { class: "rename-to mono-path", text: i.to })) : null);
            },
          },
          { label: "Why", cls: "wrap", render: libraryNote },
        ],
      });
    }
    await libTable.load();
  }

  async function applyLibrary() {
    let plan;
    try {
      plan = await post("/api/library/plan", { ...libOptions(), limit: 1 });
    } catch (err) { toast(err.message, "error"); return; }
    if (plan.empty) { toast("Nothing to do - the library is already built.", "ok"); return; }
    const r = plan.reasons || {}, pl = plan.playlists || {};
    const warnings = plan.warnings || [];
    const line = (n, text) => (n ? el("li", { text: `${fmt(n)} ${text}` }) : null);
    const body = el("div", {},
      ...warnings.map((w) => el("p", { class: "notice", text: w })),
      el("p", { text: `Build the library inside ${state.scan.root}:` }),
      el("ul", {},
        line((r.renamed || 0) + (r.moved || 0), `file(s) renamed / moved to their place (${fmt(r.kept)} kept in total)`),
        line(r.excluded, `excluded file(s) → ${EXCLUDED}/`),
        line(r.superseded, `superseded file(s) → ${SUPERSEDED}/`),
        line(r.incomplete, `file(s) of incomplete sets → ${INCOMPLETE}/`),
        line(r.duplicates, `duplicate(s) → ${DUPLICATES}/`),
        line(r.unmatched, `unmatched file(s) → ${UNMATCHED}/`),
        line((plan.vanish || {}).titles, "title(s) have no kept version (see \"Games that vanish\")"),
        line(pl.write, "playlist(s) written"),
        line(pl.remove, "outdated playlist(s) made by this app removed")),
      el("ul", {},
        el("li", { text: "Nothing is deleted and existing files are never overwritten." }),
        el("li", { text: "One undo log is saved as it goes - \"Undo last\" reverts the moves and removes / restores the playlists." }),
        el("li", { text: "The folder is re-scanned afterwards." })));
    if (!(await confirmDialog({ title: "Build library", body, okText: `Build library (${fmt(plan.actionable)} changes)` }))) return;
    if (warnings.length && !(await confirmDialog({
      title: "Are you sure?",
      body: `${warnings.join(" ")} Moving files out of other systems' folders would break them for your emulators.`,
      okText: "Yes, build in this folder", danger: true,
    }))) return;
    Jobs.start("/api/library/apply", libOptions());
  }

  async function undoLibrary() {
    await refreshUndo();
    const log = undoLogs[0];
    if (!log) { toast("No undo log found in this folder.", "info"); return; }
    const when = log.mtime ? new Date(log.mtime * 1000).toLocaleString() : log.name;
    const what = log.count !== null && log.count !== undefined ? `${fmt(log.count)} change(s)` : "the changes";
    const ok = await confirmDialog({
      title: "Undo last build",
      body: `Revert ${what} made on ${when}, playlists included? Files that were moved or changed since are skipped.`,
      okText: "Undo", danger: true,
    });
    if (ok) Jobs.start("/api/library/undo", { log: log.log });
  }

  Jobs.handlers.library = async (job) => {
    if ((job.status === "done" || job.status === "cancelled") && job.result) {
      const r = job.result;
      const isUndo = r.action === "undo";
      const failed = Array.isArray(r.failed) ? r.failed : [];
      const n = (v) => (Array.isArray(v) ? v.length : v || 0);
      const text = isUndo
        ? `Reverted ${fmt(n(r.restored))} file(s)${r.created_removed ? `, removed ${fmt(r.created_removed)} playlist/converted file(s)` : ""}`
        : `Moved ${fmt(n(r.moved))} file(s), wrote ${fmt(n(r.playlists_written))} playlist(s)`;
      toast(text + (failed.length ? `, ${fmt(failed.length)} failed` : ""), failed.length || r.error ? "error" : "ok", 8000);
      const skipped = Array.isArray(r.skipped) ? r.skipped : [];
      showFailures("lib-failures", isUndo ? "Could not restore:" : "Could not move / write:",
        failed.concat(skipped.filter((x) => x.reason !== "already back in place")),
        (f) => `${f.src || f.path || ""}${f.dst ? ` → ${f.dst}` : ""}: ${f.error || f.reason || "failed"}`,
        [r.error ? `Stopped: ${r.error}` : "",
          r.remaining ? `${fmt(r.remaining)} move(s) are kept in the undo log - fix the cause and press "Undo last" again.` : "",
          r.rescan_error ? `The folder could not be re-scanned (${r.rescan_error}) - scan it again.` : ""]);
    }
    const wasOpen = !$("lib-output").classList.contains("hidden");
    await refreshScan();
    if (wasOpen && state.scan) { libTable = null; vanishTable = null; await showLibraryPlan(); }
  };

  // --------------------------------------------------------- 3b. Organise (advanced)
  let organiseTable = null;
  let organisePlan = null; // last plan response (counts, by_dest, ...)
  const ACTIONABLE = ["move", "rename", "delete"];
  const moveUnmatched = () => $("organise-move-unmatched").checked;
  const latestOnly = () => $("organise-latest-only").checked;

  function renderOrganiseWarnings(plan) {
    const notes = [...(plan.warnings || [])];
    if (plan.missing_dats && plan.missing_dats.length) {
      notes.push(`Not checked (DAT not downloaded): ${plan.missing_dats.map(shortDat).join(", ")} - files in those folders are left where they are.`);
    }
    const box = $("organise-warnings");
    box.classList.toggle("hidden", !notes.length);
    box.replaceChildren(...notes.map((n) => el("div", { text: n })));
  }
  const actionableCount = (counts) => ACTIONABLE.reduce((n, k) => n + (counts[k] || 0), 0);

  function renderOrganiseIntro() {
    const has = !!state.scan;
    $("organise-empty").textContent = has ? "Press \"Preview changes\" to see what would move." : "Run a scan first.";
    $("organise-empty").classList.remove("hidden");
    $("organise-output").classList.add("hidden");
    organiseTable = null;
    organisePlan = null;
    refreshUndo();
  }

  function filterChips(container, counts, current, order, onPick, labelFor = (k) => k, allLabel = "All") {
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const chip = (key, label, n) => el("button", {
      class: `chip ${current === key ? "active" : ""}`, text: `${label} (${fmt(n)})`,
      on: { click: () => onPick(key) },
    });
    const keys = order.filter((k) => counts[k]).concat(Object.keys(counts).filter((k) => !order.includes(k)));
    container.replaceChildren(chip("", allLabel, total), ...keys.map((k) => chip(k, labelFor(k), counts[k])));
  }

  const destLabel = (dest) => (dest === "." ? (isFlat(currentPlatform()) ? "(console folder)" : "(platform folder)")
    : `${dest}/`);

  function renderOrganiseCards(plan) {
    const c = plan.counts || {};
    $("organise-cards").replaceChildren(
      card(fmt(plan.actionable !== undefined ? plan.actionable : actionableCount(c)), "To move / rename", "info"),
      card(fmt(plan.to_unmatched || 0), `Going to ${UNMATCHED}/`, plan.to_unmatched ? "warn" : ""),
      ...(plan.latest_only ? [card(fmt(plan.to_superseded || 0), "Older versions → _superseded/", plan.to_superseded ? "info" : "")] : []),
      card(fmt(c.ok || 0), "Already in place", "ok"),
      card(fmt(c.duplicate || 0), "Duplicates (left alone)", c.duplicate ? "warn" : ""),
      card(fmt(c.conflict || 0), "Conflicts (skipped)", c.conflict ? "bad" : ""),
      ...(c.delete ? [card(fmt(c.delete), "Old playlists to remove", "warn")] : []),
      ...(c.skip ? [card(fmt(c.skip), "Skipped / left in place", "")] : []),
    );
  }

  async function showOrganisePlan() {
    $("organise-empty").classList.add("hidden");
    $("organise-output").classList.remove("hidden");
    if (!organiseTable) {
      organiseTable = new PagedTable($("organise-table"), {
        placeholder: "Search file names or folders...",
        emptyText: "Nothing in this category.",
        fetch: async ({ offset, limit, q }) => {
          const data = await post("/api/organise/plan", {
            status: state.organiseFilter, dest: state.organiseDest, offset, limit, q, move_unmatched: moveUnmatched(),
            latest_only: latestOnly(),
          });
          organisePlan = data;
          renderOrganiseCards(data);
          renderOrganiseWarnings(data);
          const reload = () => { organiseTable.offset = 0; organiseTable.load(); };
          filterChips($("organise-filters"), data.counts || {}, state.organiseFilter,
            ["move", "rename", "delete", "conflict", "duplicate", "skip", "ok"],
            (key) => { state.organiseFilter = key; reload(); }, (k) => k, "All statuses");
          // The root folder's key is "" on the server; "." here so it differs from "All".
          const byDest = Object.fromEntries(Object.entries(data.by_dest || {}).map(([k, v]) => [k === "" ? "." : k, v]));
          $("organise-dest-filters").classList.toggle("hidden", Object.keys(byDest).length < 2 && !state.organiseDest);
          filterChips($("organise-dest-filters"), byDest, state.organiseDest, [],
            (key) => { state.organiseDest = key; reload(); }, destLabel, "All destinations");
          $("apply-btn").dataset.blocked = actionableCount(data.counts || {}) ? "0" : "1";
          Jobs.setRunning(Jobs.running);
          return data;
        },
        columns: [
          { label: "Status", render: (i) => badge(i.status) },
          {
            label: `Change (from → to, relative to the ${isFlat(currentPlatform()) ? "console" : "platform"} folder)`, cls: "wrap", render: (i) => {
              const changed = i.from !== i.to;
              return el("div", {},
                el("div", { class: changed ? "rename-from mono-path" : "rename-to mono-path", text: i.from }),
                changed ? el("div", {}, el("span", { class: "rename-arrow", text: "→ " }), el("span", { class: "rename-to mono-path", text: i.to })) : null,
                i.superseded_by ? el("div", { class: "tags" }, el("span", { class: "tag tag-status", text: "older version" })) : null);
            },
          },
          { label: "Note", cls: "wrap", render: (i) => el("span", { class: "muted", text: i.reason }) },
        ],
      });
    }
    await organiseTable.load();
  }

  async function applyOrganise() {
    let plan;
    try {
      plan = await post("/api/organise/plan", { limit: 1, move_unmatched: moveUnmatched(), latest_only: latestOnly() });
    } catch (err) { toast(err.message, "error"); return; }
    const n = plan.actionable !== undefined ? plan.actionable : actionableCount(plan.counts || {});
    if (!n) { toast("Nothing to do - every file is already in its place.", "ok"); return; }
    const byDest = plan.by_dest || {};
    const del = (plan.counts || {}).delete || 0;
    const warnings = plan.warnings || [];
    const body = el("div", {},
      ...warnings.map((w) => el("p", { class: "notice", text: w })),
      el("p", { text: `Change ${fmt(n)} file${n === 1 ? "" : "s"} inside ${state.scan.root}:` }),
      el("ul", {}, Object.entries(byDest).map(([dest, count]) =>
        el("li", { text: `${fmt(count)} → ${destLabel(dest === "" ? "." : dest)}` }))),
      el("ul", {},
        plan.to_superseded ? el("li", { text: `${fmt(plan.to_superseded)} older version(s) go to ${plan.superseded_dir || SUPERSEDED}/ (the newest one you have stays).` }) : null,
        el("li", { text: "Existing files are never overwritten (conflicts are skipped)." }),
        del ? el("li", { text: `${fmt(del)} old playlist(s) made by this app are removed - write the M3U playlists again afterwards.` }) : null,
        el("li", { text: "Folders emptied by the moves are removed." }),
        el("li", { text: "An undo log is saved in the folder as it goes - use \"Undo last\" to revert." }),
        el("li", { text: "The folder is re-scanned afterwards." })));
    if (!(await confirmDialog({ title: "Organise files", body, okText: `Apply ${fmt(n)} changes` }))) return;
    if (warnings.length && !(await confirmDialog({
      title: "Are you sure?",
      body: `${warnings.join(" ")} Moving files out of other systems' folders would break them for your emulators.`,
      okText: "Yes, organise this folder", danger: true,
    }))) return;
    Jobs.start("/api/organise/apply", { move_unmatched: moveUnmatched(), latest_only: latestOnly() });
  }

  let undoLogs = [];
  async function refreshUndo() {
    undoLogs = [];
    if (state.scan) {
      try { undoLogs = (await get("/api/organise/undo-logs")).logs || []; } catch (_) { undoLogs = []; }
    }
    for (const id of ["undo-btn", "convert-undo-btn", "lib-undo-btn"]) {
      const btn = $(id);
      btn.dataset.blocked = undoLogs.length ? "0" : "1";
      btn.title = undoLogs.length ? `Undo ${undoLogs[0].name}` : "No undo logs in this folder";
    }
    Jobs.setRunning(Jobs.running);
  }

  async function undoLast() {
    await refreshUndo();
    const log = undoLogs[0];
    if (!log) { toast("No undo log found in this folder.", "info"); return; }
    const when = log.mtime ? new Date(log.mtime * 1000).toLocaleString() : log.name;
    const what = log.count !== null && log.count !== undefined ? `${fmt(log.count)} change(s)` : "the changes";
    const ok = await confirmDialog({
      title: "Undo last organise / convert",
      body: `Revert ${what} made on ${when}? Files that were moved or recreated since are skipped.`,
      okText: "Undo", danger: true,
    });
    if (ok) Jobs.start("/api/organise/undo", { log: log.log });
  }

  Jobs.handlers.organise = async (job) => {
    if ((job.status === "done" || job.status === "cancelled") && job.result) {
      const r = job.result;
      const isUndo = r.action === "undo" || (r.action === undefined && "restored" in r);
      const failed = Array.isArray(r.failed) ? r.failed : [];
      const first = (...vals) => vals.find((v) => v !== undefined);
      const done = isUndo ? r.restored : first(r.moved, r.renamed);
      const count = Array.isArray(done) ? done.length : done || 0;
      const created = isUndo && r.created_removed ? `, removed ${fmt(r.created_removed)} converted file(s)` : "";
      toast(`${isUndo ? "Reverted" : "Moved"} ${fmt(count)} file(s)${created}${failed.length ? `, ${fmt(failed.length)} failed` : ""}`,
        failed.length || r.error ? "error" : "ok", 8000);
      const skipped = Array.isArray(r.skipped) ? r.skipped : [];
      showFailures("organise-failures", isUndo ? "Could not restore:" : "Could not move:",
        failed.concat(skipped.filter((x) => x.reason !== "already back in place")),
        (f) => `${f.src}${f.dst ? ` → ${f.dst}` : ""}: ${f.error || f.reason || "failed"}`,
        [r.error ? `Stopped: ${r.error}` : "",
          r.remaining ? `${fmt(r.remaining)} move(s) are kept in the undo log - fix the cause and press "Undo last" again.` : "",
          r.rescan_error ? `The folder could not be re-scanned (${r.rescan_error}) - scan it again.` : ""]);
    }
    const wasOpen = !$("organise-output").classList.contains("hidden");
    await refreshScan();
    if (wasOpen && state.scan) await showOrganisePlan();
  };

  // ----------------------------------------------------------- 4. Convert
  let convertTable = null;
  const VIA_TEXT = { headerless: "remove 512-byte copier header", byteswapped: "byte-swap to big-endian .z64", chdman: "chdman createcd, then checked against Redump" };
  const MODE_TEXT = { createcd: "chdman createcd, then checked against Redump", createdvd: "chdman createdvd, then checked against Redump" };

  const CHD_INTRO = "Turns an unpacked Redump set (a .gdi or .cue with one .bin / .raw file per track, or a single .iso) into a CHD with chdman (createcd for CD images, createdvd for a PlayStation 2 .iso). "
    + "The new CHD is checked track by track against Redump BEFORE anything of your set is touched; only if every track matches is it placed in the "
    + "game folder (named like the Redump entry) and the raw files are moved to _converted_originals/ - nothing is deleted, and Undo last removes "
    + "the CHD and puts the raw files back. The new CHD is written next to its final place (needs the disc size free there); the check decodes it in scratch space (RAM or the app's cache folder, never in your game folder).";
  let chdmanInfo = null;

  async function loadChdman(refresh = false) {
    try {
      chdmanInfo = await get(`/api/chdman${refresh ? "?refresh=1" : ""}`);
    } catch (err) { chdmanInfo = null; }
    renderChdman();
  }

  function renderChdman() {
    const box = $("chdman-box");
    const dc = isGameFolder(currentPlatform());
    box.classList.toggle("hidden", !dc || !chdmanInfo);
    if (!dc || !chdmanInfo) return;
    const found = !!chdmanInfo.found;
    box.classList.toggle("missing", !found);
    $("chdman-line").textContent = found ? `chdman found: ${chdmanInfo.label}${chdmanInfo.kind === "flatpak" ? "" : ""}`
      : "chdman not found - converting is disabled (scanning and verifying still work with the built-in reader).";
    const steps = $("chdman-steps");
    steps.classList.toggle("hidden", found);
    steps.replaceChildren(...(found ? [] : (chdmanInfo.steps || []).map((t) => el("li", { text: t }))));
    if (document.activeElement !== $("chdman-path")) $("chdman-path").value = chdmanInfo.override || "";
    $("chdman-engine").value = chdmanInfo.engine || "auto";
  }

  async function saveChdman(body) {
    try {
      chdmanInfo = await post("/api/chdman", body);
      renderChdman();
      if (chdmanInfo.warning) toast(chdmanInfo.warning, "error", 8000);
      else toast(chdmanInfo.found ? `chdman: ${chdmanInfo.label}` : "Saved", "ok");
      if (convertTable) { convertTable.offset = 0; convertTable.load(); }
    } catch (err) { toast(err.message, "error", 8000); }
  }

  function renderConvertIntro() {
    const dc = isGameFolder(currentPlatform());
    $("convert-title").textContent = dc ? "Convert raw Redump sets to CHD (optional, needs chdman)" : "Convert to No-Intro format (optional)";
    if (dc) $("convert-intro").textContent = CHD_INTRO;
    if (dc) loadChdman(); else $("chdman-box").classList.add("hidden");
    $("convert-empty").textContent = state.scan ? "Press \"Preview conversions\" to see which files can be converted." : "Run a scan first.";
    $("convert-empty").classList.remove("hidden");
    $("convert-output").classList.add("hidden");
    convertTable = null;
  }

  function renderConvertCards(data) {
    const c = data.counts || {};
    $("convert-cards").replaceChildren(
      card(fmt(c.convert || 0), isGameFolder(currentPlatform()) ? "Raw sets to convert to CHD" : "To convert", "info"),
      card(fmt(c.conflict || 0), "Conflicts (skipped)", c.conflict ? "bad" : ""),
      card(fmt(c.skip || 0), "Skipped", ""));
  }

  async function showConvertPlan() {
    $("convert-empty").classList.add("hidden");
    $("convert-output").classList.remove("hidden");
    if (!convertTable) {
      convertTable = new PagedTable($("convert-table"), {
        placeholder: "Search file names...",
        emptyText: isGameFolder(currentPlatform()) ? "No raw Redump sets found in this folder." : "Nothing to convert - every matched file is already in No-Intro format.",
        fetch: async ({ offset, limit, q }) => {
          const data = await post("/api/convert/plan", { status: state.convertFilter, offset, limit, q, latest_only: latestOnly() });
          renderConvertCards(data);
          if (data.chdman) { chdmanInfo = { ...(chdmanInfo || {}), ...data.chdman }; renderChdman(); }
          filterChips($("convert-filters"), data.counts || {}, state.convertFilter, ["convert", "conflict", "skip"],
            (key) => { state.convertFilter = key; convertTable.offset = 0; convertTable.load(); });
          $("convert-apply-btn").dataset.blocked = (data.counts || {}).convert && !(data.chdman && !data.chdman.found) ? "0" : "1";
          Jobs.setRunning(Jobs.running);
          return data;
        },
        columns: [
          { label: "Status", render: (i) => badge(i.status) },
          {
            label: "Change (relative to the console folder)", cls: "wrap", render: (i) => el("div", {},
              el("div", { class: "rename-from mono-path", text: i.from }),
              el("div", {}, el("span", { class: "rename-arrow", text: "→ " }), el("span", { class: "rename-to mono-path", text: i.to })),
              i.status === "convert" ? el("div", { class: "sub mono-path", text: `original kept as ${i.original_to}` }) : null),
          },
          { label: "How", cls: "wrap", render: (i) => el("span", { class: "muted", text: MODE_TEXT[i.mode] || VIA_TEXT[i.via] || i.via || "-" }) },
          { label: "Note", cls: "wrap", render: (i) => el("span", { class: "muted", text: i.reason }) },
        ],
      });
    }
    await convertTable.load();
  }

  async function applyConvert() {
    let data;
    try {
      data = await post("/api/convert/plan", { limit: 1, latest_only: latestOnly() });
    } catch (err) { toast(err.message, "error"); return; }
    const n = (data.counts || {}).convert || 0;
    if (!n) { toast("Nothing to convert.", "ok"); return; }
    if (data.chdman && !data.chdman.found) { toast("chdman was not found - see the instructions above.", "error", 8000); return; }
    const body = isGameFolder(currentPlatform()) ? el("div", {},
      el("p", { text: `Convert ${fmt(n)} raw set${n === 1 ? "" : "s"} inside ${state.scan.root} to CHD (chdman ${data.chdman ? data.chdman.label : ""})?` }),
      el("ul", {},
        el("li", { text: "The new CHD is decoded again and compared with every Redump track before it is kept; on any mismatch nothing changes." }),
        el("li", { text: `The raw files are moved to ${data.originals_dir || CONVERTED}/ - nothing is deleted.` }),
        el("li", { text: "The new CHD is written next to its final place as a .part file (about the raw size must be free there) and renamed only after the check; the check itself uses scratch space outside your game folder." }),
        el("li", { text: "An undo log is saved as it goes - \"Undo last\" removes the CHD and puts the raw files back." }),
        el("li", { text: "The folder is re-scanned afterwards. You can cancel; the raw files stay untouched." })))
    : el("div", {},
      el("p", { text: `Convert ${fmt(n)} file${n === 1 ? "" : "s"} inside ${state.scan.root} into No-Intro format?` }),
      el("ul", {},
        el("li", { text: "A clean copy is written under the exact DAT name and checked against the DAT before it is kept." }),
        el("li", { text: `The original is moved to ${data.originals_dir || CONVERTED}/ - nothing is deleted.` }),
        el("li", { text: "An undo log is saved as it goes - \"Undo last\" removes the copies and puts the originals back." }),
        el("li", { text: "The folder is re-scanned afterwards." })));
    if (!(await confirmDialog({ title: isGameFolder(currentPlatform()) ? "Convert to CHD" : "Convert to No-Intro format", body, okText: `Convert ${fmt(n)}` }))) return;
    Jobs.start("/api/convert/apply", { latest_only: latestOnly() });
  }

  Jobs.handlers.convert = async (job) => {
    if ((job.status === "done" || job.status === "cancelled") && job.result) {
      const r = job.result;
      const failed = Array.isArray(r.failed) ? r.failed : [];
      const converted = Array.isArray(r.converted) ? r.converted.length : r.converted || 0;
      toast(`Converted ${fmt(converted)} file(s)${failed.length ? `, ${fmt(failed.length)} failed` : ""}${r.cancelled ? " (cancelled)" : ""}`,
        failed.length || r.error ? "error" : "ok", 8000);
      showFailures("convert-failures", "Could not convert:", failed,
        (f) => `${f.src}${f.dst ? ` → ${f.dst}` : ""}: ${f.error || "failed"}`,
        [r.error ? `Stopped: ${r.error}` : "",
          r.rescan_error ? `The folder could not be re-scanned (${r.rescan_error}) - scan it again.` : ""]);
    }
    const wasOpen = !$("convert-output").classList.contains("hidden");
    await refreshScan();
    if (wasOpen && state.scan) await showConvertPlan();
  };

  // -------------------------------------------------------------- 5. M3U
  let m3uTable = null;
  const m3uOptions = () => ({ savedisk: $("m3u-savedisk").checked, labels: $("m3u-labels").checked });

  function renderM3UIntro() {
    $("m3u-empty").textContent = state.scan ? "Press \"Preview playlists\" to find multi-disk sets." : "Run a scan first.";
    $("m3u-empty").classList.remove("hidden");
    $("m3u-output").classList.add("hidden");
    m3uTable = null;
  }

  async function showM3UPlan() {
    $("m3u-empty").classList.add("hidden");
    $("m3u-output").classList.remove("hidden");
    if (!m3uTable) {
      m3uTable = new PagedTable($("m3u-table"), {
        pageSize: 25,
        placeholder: "Search playlists...",
        emptyText: "No multi-disk sets found among the matched files.",
        fetch: async ({ offset, limit, q }) => {
          const data = await post("/api/m3u/plan", { ...m3uOptions(), status: state.m3uFilter, offset, limit, q });
          const counts = data.counts || {};
          filterChips($("m3u-filters"), counts, state.m3uFilter, ["write", "stale", "ok", "incomplete", "conflict"],
            (key) => { state.m3uFilter = key; m3uTable.offset = 0; m3uTable.load(); });
          $("m3u-apply-btn").dataset.blocked = counts.write || counts.stale ? "0" : "1";
          Jobs.setRunning(Jobs.running);
          return data;
        },
        columns: [
          { label: "Status", render: (i) => badge(i.status) },
          {
            label: "Playlist", cls: "wrap", render: (i) => el("div", {},
              el("div", { text: i.name }),
              i.dir && i.dir !== "." ? el("div", { class: "sub", text: `in ${i.dir}` }) : null),
          },
          {
            label: "Disks", cls: "wrap", render: (i) => {
              if (!i.lines.length) return el("span", { class: "muted", text: "-" });
              return el("details", {},
                el("summary", { text: `${i.disks} disk${i.disks === 1 ? "" : "s"}` }),
                el("pre", { class: "m3u-lines", text: i.lines.join("\n") }));
            },
          },
          { label: "Note", cls: "wrap", render: (i) => el("span", { class: "muted", text: i.reason }) },
        ],
      });
    }
    await m3uTable.load();
  }

  async function writeM3Us() {
    let counts;
    try {
      counts = (await post("/api/m3u/plan", { ...m3uOptions(), limit: 1 })).counts || {};
    } catch (err) { toast(err.message, "error"); return; }
    const n = counts.write || 0;
    const stale = counts.stale || 0;
    if (!n && !stale) { toast("No new playlists to write.", "ok"); return; }
    const ok = await confirmDialog({
      title: "Write M3U playlists",
      body: `Write ${fmt(n)} playlist${n === 1 ? "" : "s"} next to the disk images?`
        + (stale ? ` ${fmt(stale)} outdated playlist(s) made by this app are removed.` : "")
        + " Playlists not created by this app are left alone.",
      okText: n ? `Write ${fmt(n)}` : `Remove ${fmt(stale)}`,
    });
    if (ok) Jobs.start("/api/m3u/apply", m3uOptions());
  }

  Jobs.handlers.m3u = async (job) => {
    if (job.status === "done" && job.result) {
      const r = job.result;
      const written = r.written !== undefined ? (Array.isArray(r.written) ? r.written.length : r.written) : null;
      const failed = Array.isArray(r.failed) ? r.failed : [];
      toast((written !== null ? `Wrote ${fmt(written)} playlist(s)` : "Playlists written")
        + (r.removed ? `, removed ${fmt(r.removed)} outdated` : "")
        + (failed.length ? `, ${fmt(failed.length)} failed` : ""), failed.length ? "error" : "ok", 8000);
      showFailures("m3u-failures", "Could not write:", failed, (f) => `${f.path}: ${f.error}`);
    }
    m3uTable = null;
    if (state.scan) await showM3UPlan();
  };

  // -------------------------------------------------------- 6. Kickstarts
  let kickTable = null;
  let kickDirsLoaded = false;

  /** The Kickstart destination field belongs to the selected system (each has its own saved folder). */
  function setKickDest(p) {
    $("kick-dest").value = (p && p.kickstart_dest) || "";
    kickTable = null;
    $("kick-output").classList.add("hidden");
    $("kick-failures").classList.add("hidden");
  }

  const kickFolderMode = (p) => !!(p && p.kickstart_folder);
  /** The Kickstart step can run: a folder-source system needs its folder, a DAT-source one a scan of itself. */
  const kickReady = (p) => (kickFolderMode(p) ? !!folderOf(p).trim() : !!state.scan);

  function renderKickIntro() {
    const p = currentPlatform();
    const folderMode = kickFolderMode(p);
    $("kick-intro").replaceChildren(...(folderMode ? [
      "Optional. Copies the Kickstart ROMs you put into the ", el("code", { text: `${p.kickstart_folder}/` }),
      " folder inside this system's folder (matched by MD5 against the ",
      el("a", { href: "https://docs.libretro.com/library/puae/", target: "_blank", rel: "noopener noreferrer", text: "PUAE BIOS list" }),
      ") into RetroArch's system / BIOS folder under the file names PUAE expects. PUAE needs these for WHDLoad. ",
      "That folder is never scanned, moved or organised by the other steps. Files are copied, never moved, and existing files are never overwritten.",
    ] : [
      "Optional. Copies the Kickstart ROMs you have (matched by MD5 against the ",
      el("a", { href: "https://docs.libretro.com/library/puae/", target: "_blank", rel: "noopener noreferrer", text: "PUAE BIOS list" }),
      ") into RetroArch's system / BIOS folder under the file names PUAE expects. Files are copied, never moved, and existing files are never overwritten.",
    ]));
    $("kick-empty").textContent = kickReady(p) ? "Pick a destination, then press \"Preview\"."
      : folderMode ? `Choose the ${p ? p.name : "system"} folder in step 1 first.` : "Run a scan first.";
    $("kick-empty").classList.remove("hidden");
    $("kick-output").classList.add("hidden");
    $("kick-source").textContent = "";
    kickTable = null;
    if (!kickDirsLoaded && hasKickstart(p)) loadKickDirs();
  }

  async function loadKickDirs() {
    let data;
    const forPlatform = state.platform;
    try {
      data = await get(`/api/kickstart/dirs?${qs({ platform: forPlatform })}`);
    } catch (err) {
      $("kick-dirs").replaceChildren(el("div", { class: "muted small", text: `Could not detect RetroArch folders: ${err.message}` }));
      return;
    }
    if (forPlatform !== state.platform) return;   // the user switched system meanwhile
    kickDirsLoaded = true;
    const dirs = data.dirs || [];
    const input = $("kick-dest");
    if (!input.value.trim()) {
      const firstExisting = dirs.find((d) => d.exists);
      input.value = data.last || (firstExisting ? firstExisting.path : "");
    }
    renderKickDirs(dirs);
    updateActionState(); // the destination may have just been filled in
  }

  function renderKickDirs(dirs) {
    const chosen = $("kick-dest").value.trim();
    $("kick-dirs").replaceChildren(...(dirs.length ? dirs.map((d) => el("button", {
      class: `dest-option ${d.path === chosen ? "active" : ""}`, title: d.path,
      on: { click: () => { $("kick-dest").value = d.path; $("kick-dest").dispatchEvent(new Event("input")); $("kick-dest").dispatchEvent(new Event("change")); renderKickDirs(dirs); } },
    },
    el("div", { class: "dest-label" }, el("span", { text: d.label || "Folder" }), " ", badge(d.exists ? "ok" : "missing")),
    el("div", { class: "sub mono", text: d.path }))) : [el("div", { class: "muted small", text: "No RetroArch / EmuDeck / RetroDECK folders found - type or browse to your BIOS folder." })]));
    $("kick-dirs").dataset.dirs = JSON.stringify(dirs);
  }

  /** Remember the destination of the selected system as soon as it is chosen (not only after a copy). */
  async function saveKickDest() {
    const p = currentPlatform();
    const dest = $("kick-dest").value.trim();
    if (!p || !dest || !hasKickstart(p) || dest === p.kickstart_dest) return;
    try {
      const res = await post("/api/kickstart/dest", { platform: p.name, dest });
      p.kickstart_dest = res.dest;
      toast(`Saved the Kickstart folder for ${p.name}`, "ok");
    } catch (err) {
      toast(`Kickstart folder not saved - ${err.message}`, "error");
    }
  }

  async function showKickPlan() {
    const dest = $("kick-dest").value.trim();
    if (!dest) { toast("Choose a destination folder first", "error"); return; }
    $("kick-empty").classList.add("hidden");
    $("kick-output").classList.remove("hidden");
    const platform = state.platform;
    kickTable = new PagedTable($("kick-table"), {
      placeholder: "Search Kickstarts...",
      emptyText: "Nothing in this category.",
      fetch: async ({ offset, limit, q }) => {
        const data = await post("/api/kickstart/plan", { platform, dest, status: state.kickFilter, offset, limit, q });
        const counts = data.counts || {};
        filterChips($("kick-filters"), counts, state.kickFilter, ["copy", "ok", "conflict", "missing", "unmatched"],
          (key) => { state.kickFilter = key; kickTable.offset = 0; kickTable.load(); });
        $("kick-apply-btn").dataset.blocked = counts.copy ? "0" : "1";
        $("kick-source").textContent = data.source === "folder"
          ? (data.source_exists ? `Kickstart ROMs are read from ${data.source_dir}`
            : `The folder ${data.source_dir} does not exist yet - create it and drop your Kickstart ROMs into it.`)
          : "";
        Jobs.setRunning(Jobs.running);
        return data;
      },
      columns: [
        { label: "Status", render: (i) => badge(i.status) },
        {
          label: "PUAE file", cls: "wrap", render: (i) => el("div", {},
            el("div", { class: "mono", text: i.file }),
            i.description ? el("div", { class: "sub", text: i.description }) : null),
        },
        { label: "From (your files)", cls: "wrap", render: (i) => (i.source ? fileCell(i.source) : el("span", { class: "muted", text: "-" })) },
        { label: "Note", cls: "wrap", render: (i) => el("span", { class: "muted", text: i.reason }) },
      ],
    });
    await kickTable.load();
  }

  async function applyKick() {
    const dest = $("kick-dest").value.trim();
    if (!dest) { toast("Choose a destination folder first", "error"); return; }
    let data;
    try {
      data = await post("/api/kickstart/plan", { platform: state.platform, dest, limit: 1 });
    } catch (err) { toast(err.message, "error"); return; }
    const n = (data.counts || {}).copy || 0;
    if (!n) { toast("Nothing to copy - press Preview to see why.", "ok"); return; }
    const ok = await confirmDialog({
      title: "Copy Kickstarts",
      body: `Copy ${fmt(n)} Kickstart file${n === 1 ? "" : "s"} into ${data.dest}${data.dest_exists ? "" : " (the folder will be created)"}? Existing files are not overwritten.`,
      okText: `Copy ${fmt(n)}`,
    });
    if (ok) Jobs.start("/api/kickstart/apply", { platform: state.platform, dest });
  }

  Jobs.handlers.kickstart = async (job) => {
    if (job.status === "done" && job.result) {
      const r = job.result;
      const copied = Array.isArray(r.copied) ? r.copied.length : r.copied;
      const failed = Array.isArray(r.failed) ? r.failed.length : r.failed || 0;
      toast(`Copied ${fmt(copied || 0)} Kickstart file(s)${failed ? `, ${fmt(failed)} failed` : ""}`, failed ? "error" : "ok", 8000);
      showFailures("kick-failures", "Could not copy:", Array.isArray(r.failed) ? r.failed : [],
        (f) => `${f.target}: ${f.error}`);
      const p = state.platforms.find((x) => x.name === r.platform);
      if (p && r.dest) p.kickstart_dest = r.dest;
    }
    if (kickReady(currentPlatform()) && $("kick-dest").value.trim()) await showKickPlan();
  };

  // ----------------------------------------------------------- shared UI
  function updateActionState() {
    const hasScan = !!state.scan;
    const canScan = !!state.platform && !!folderOf(currentPlatform()).trim();
    $("scan-btn").dataset.blocked = canScan ? "0" : "1";
    for (const id of ["plan-btn", "m3u-plan-btn", "convert-plan-btn", "lib-plan-btn"]) $(id).dataset.blocked = hasScan ? "0" : "1";
    if (!hasScan) for (const id of ["undo-btn", "convert-undo-btn", "lib-undo-btn"]) $(id).dataset.blocked = "1";
    if (!hasScan || libTable === null) $("lib-apply-btn").dataset.blocked = hasScan ? "0" : "1";
    if (!hasScan || convertTable === null) $("convert-apply-btn").dataset.blocked = hasScan ? "0" : "1";
    // Apply buttons are enabled once a scan exists (until a preview shows there is
    // nothing to do); the confirmation step re-checks the counts anyway.
    if (!hasScan || organiseTable === null) $("apply-btn").dataset.blocked = hasScan ? "0" : "1";
    if (!hasScan || m3uTable === null) $("m3u-apply-btn").dataset.blocked = hasScan ? "0" : "1";
    const hasDest = !!$("kick-dest").value.trim();
    const kickOk = kickReady(currentPlatform());
    $("kick-plan-btn").dataset.blocked = kickOk && hasDest ? "0" : "1";
    if (!kickOk || !hasDest || kickTable === null) $("kick-apply-btn").dataset.blocked = kickOk && hasDest ? "0" : "1";
    Jobs.setRunning(Jobs.running);
  }

  async function quit() {
    const ok = await confirmDialog({ title: "Quit", body: "Stop Simple ROM Organiser? You can close this tab afterwards.", okText: "Quit", danger: true });
    if (!ok) return;
    try {
      await post("/api/quit");
      clearTimeout(Jobs.timer);
      document.querySelector("main").replaceChildren(el("div", { class: "empty", text: "Simple ROM Organiser has stopped. You can close this tab." }));
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function bind() {
    for (const id of ["scan-btn", "plan-btn", "apply-btn", "undo-btn", "lib-plan-btn", "lib-apply-btn", "lib-undo-btn",
      "convert-plan-btn", "convert-apply-btn", "convert-undo-btn", "m3u-plan-btn", "m3u-apply-btn",
      "kick-plan-btn", "kick-apply-btn", "dc-verify-btn"]) {
      $(id).setAttribute("data-needs-idle", "");
    }
    $("updates-btn").addEventListener("click", () => Updates.check());
    $("updates-retry").addEventListener("click", () => Updates.check());
    $("scan-retry").addEventListener("click", () => { $("scan-error").classList.add("hidden"); startScan(); });
    window.addEventListener("focus", () => { if (!Updates.wasRunning) Updates.load(); });
    $("lib-plan-btn").addEventListener("click", showLibraryPlan);
    $("lib-vanish-box").addEventListener("toggle", () => { if ($("lib-vanish-box").open && !vanishTable && libPlan) showVanishTable(); });
    $("lib-apply-btn").addEventListener("click", applyLibrary);
    $("lib-undo-btn").addEventListener("click", undoLibrary);
    $("library-reset").addEventListener("click", async () => {
      try {
        state.library[state.platform] = await post("/api/library/profile", { platform: state.platform, reset: true });
        const p = currentPlatform();
        if (p) { p.library = state.library[state.platform].profile; p.latest_only = !!p.library.latest_only; }
        $("organise-latest-only").checked = !!(p && p.latest_only);
        profileChanged();
        renderLibraryRules();
      } catch (err) { toast(err.message, "error"); }
    });
    for (const id of ["lib-move-unmatched", "lib-labels", "lib-savedisk"]) {
      $(id).addEventListener("change", () => { if (libTable) { libTable.offset = 0; libTable.load(); } });
    }
    $("platform-select").addEventListener("change", (e) => selectPlatform(e.target.value));
    $("fb-up").addEventListener("click", () => FolderBrowser.parent && FolderBrowser.list(FolderBrowser.parent));
    $("fb-hidden").addEventListener("change", () => FolderBrowser.current && FolderBrowser.list(FolderBrowser.current));
    $("fb-choose").addEventListener("click", () => FolderBrowser.choose());
    $("scan-btn").addEventListener("click", startScan);
    $("plan-btn").addEventListener("click", showOrganisePlan);
    $("apply-btn").addEventListener("click", applyOrganise);
    $("undo-btn").addEventListener("click", undoLast);
    $("m3u-plan-btn").addEventListener("click", showM3UPlan);
    $("m3u-apply-btn").addEventListener("click", writeM3Us);
    $("organise-move-unmatched").addEventListener("change", () => {
      if (organiseTable) { organiseTable.offset = 0; organiseTable.load(); }
    });
    $("organise-latest-only").addEventListener("change", async () => {
      const p = currentPlatform();
      const value = latestOnly();
      if (p) {
        p.latest_only = value;
        try { // remembered right away, so a scan / DAT download in between keeps it
          await post("/api/platforms/options", { platform: p.name, latest_only: value });
        } catch (err) { toast(`Could not save "Latest version only": ${err.message}`, "error"); }
      }
      loadLibraryProfile();
      libTable = null; vanishTable = null; libPlan = null; $("lib-output").classList.add("hidden"); $("lib-empty").classList.remove("hidden");
      if (organiseTable) { organiseTable.offset = 0; organiseTable.load(); }
      if (convertTable) { convertTable.offset = 0; convertTable.load(); }
    });
    $("dc-verify-btn").addEventListener("click", verifyFully);
    $("chdman-save").addEventListener("click", () => saveChdman({ path: $("chdman-path").value.trim() }));
    $("chdman-refresh").addEventListener("click", () => loadChdman(true));
    $("chdman-engine").addEventListener("change", (e) => saveChdman({ engine: e.target.value }));
    $("convert-plan-btn").addEventListener("click", showConvertPlan);
    $("convert-apply-btn").addEventListener("click", applyConvert);
    $("convert-undo-btn").addEventListener("click", undoLast);
    for (const id of ["m3u-labels", "m3u-savedisk"]) {
      $(id).addEventListener("change", () => { if (m3uTable) { m3uTable.offset = 0; m3uTable.load(); } });
    }
    $("kick-dest").addEventListener("input", debounce(() => {
      kickTable = null;
      $("kick-output").classList.add("hidden");
      $("kick-empty").classList.remove("hidden");
      try { renderKickDirs(JSON.parse($("kick-dirs").dataset.dirs || "[]")); } catch (_) { /* ignore */ }
      updateActionState();
    }, 150));
    $("kick-dest").addEventListener("change", saveKickDest);
    $("kick-dest").addEventListener("keydown", (e) => { if (e.key === "Enter" && kickReady(currentPlatform())) showKickPlan(); });
    $("kick-browse-btn").addEventListener("click", () => FolderBrowser.open($("kick-dest"), "Choose the RetroArch system / BIOS folder"));
    $("kick-native-browse-btn").addEventListener("click", (e) => nativeBrowse(e.currentTarget, $("kick-dest"), "Choose the RetroArch system / BIOS folder"));
    $("kick-plan-btn").addEventListener("click", showKickPlan);
    $("kick-apply-btn").addEventListener("click", applyKick);
    $("quit-btn").addEventListener("click", quit);
  }

  async function init() {
    bind();
    await loadStatus();
    const s = state.status || {};
    state.platform = (s.scan && s.scan.platform) || s.last_platform || s.default_platform || null;
    await loadPlatforms();
    setKickDest(currentPlatform());
    await refreshScan();
    // Resume tracking a job that was started before a page reload.
    try {
      const job = await get("/api/job");
      if (job && job.status === "running") Jobs.track(job);
    } catch (_) { /* ignore */ }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
