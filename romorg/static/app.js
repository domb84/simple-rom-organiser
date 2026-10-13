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
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  }
  /** 45s, 2m 05s, 1h 05m */
  function fmtDuration(sec) {
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return `${sec}s`;
    const m = Math.floor(sec / 60), s = sec % 60;
    if (m < 60) return `${m}m ${String(s).padStart(2, "0")}s`;
    return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
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
    if (!res.ok) {
      const err = new Error((data && data.error) || `${res.status} ${res.statusText}`);
      if (data && data.code) err.code = data.code;
      throw err;
    }
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

  // ------------------------------------------------------------ remembered view settings
  /** Per-system view settings (sort, filters), a global density flag and per-table hidden columns, in localStorage.
   *  Everything is optional: with storage blocked the app simply starts from its defaults. */
  const Prefs = {
    KEY: "romorg.prefs.v1",
    SYSTEM_FIELDS: ["gamesSort", "libSort", "vanishSort", "gamesHave", "gamesRated", "libReason", "libStatus", "tagFilter", "kindSort"],
    data: null,
    read() {
      if (this.data) return this.data;
      try { this.data = JSON.parse(localStorage.getItem(this.KEY) || "{}") || {}; } catch (_) { this.data = {}; }
      if (typeof this.data !== "object") this.data = {};
      return this.data;
    },
    write() {
      try { localStorage.setItem(this.KEY, JSON.stringify(this.data)); } catch (_) { /* private window / blocked */ }
    },
    /** Remember the current sort and filters of the selected system. */
    save() {
      const d = this.read();
      if (!state.platform) return;
      d.systems = d.systems || {};
      d.systems[state.platform] = Object.fromEntries(this.SYSTEM_FIELDS.map((k) => [k, state[k]]));
      this.write();
    },
    /** Put the remembered sort and filters of ``name`` back into the state (unknown / wrong-typed values are ignored). */
    restore(name) {
      const saved = ((this.read().systems || {})[name]) || {};
      for (const k of this.SYSTEM_FIELDS) {
        if (saved[k] === undefined || typeof saved[k] !== typeof state[k]) continue;
        state[k] = saved[k];
      }
    },
    hiddenColumns(table) { return new Set(((this.read().columns || {})[table]) || []); },
    setHiddenColumns(table, set) {
      const d = this.read();
      d.columns = d.columns || {};
      d.columns[table] = [...set];
      this.write();
    },
    get dense() { return !!this.read().dense; },
    set dense(on) { this.read().dense = !!on; this.write(); },
  };

  // ------------------------------------------------------------ paged table
  /**
   * Searchable, paged table backed by a server fetch function.
   * opts: {id, columns: [{label, cls, render(item), sortKey, sortFirst, when(data), always}], fetch({offset, limit, q}),
   *        pageSize, placeholder, emptyText, rowClass(item),
   *        sort: {get, set}                       header clicks cycle a column's sort (sortKey name|rating|year|size)
   *        detail: {kind, ref(item), extra(item)} per-row "Details" panel (a note + the checksums)
   *        select: {id(item), can(item), actions: [{label, run(items)}]}   tick rows, act on them}
   * ``id`` names the table for the remembered hidden columns (the "Columns" menu).
   */
  class PagedTable {
    static n = 0;
    constructor(container, opts) {
      this.container = container;
      this.opts = Object.assign({ pageSize: 50, placeholder: "Search...", emptyText: "Nothing to show." }, opts);
      this.offset = 0;
      this.q = "";
      this.seq = 0;
      this.data = null;
      this.selected = new Map();
      this.hidden = this.opts.id ? Prefs.hiddenColumns(this.opts.id) : new Set();
      this.search = el("input", {
        type: "search", class: "input grow", placeholder: this.opts.placeholder, autocomplete: "off", "aria-label": this.opts.placeholder,
        on: { input: debounce(() => { this.q = this.search.value.trim(); this.offset = 0; this.load(); }, 250) },
      });
      this.countEl = el("span", { class: "muted" });
      this.body = el("div");
      this.prev = el("button", { class: "btn btn-small", text: "Previous", on: { click: () => this.go(-1) } });
      this.next = el("button", { class: "btn btn-small", text: "Next", on: { click: () => this.go(1) } });
      this.pageInfo = el("span", { class: "muted" });
      this.selBar = el("div", { class: "sel-bar hidden", role: "status" });
      const tools = [this.search, this.countEl];
      if (this.opts.detail) {
        // global switch: every visible row shows its details and checksums (the page is then fetched with them)
        this.allBox = el("input", { type: "checkbox", id: `show-checksums-${++PagedTable.n}`, checked: state.showChecksums ? "" : null });
        this.allBox.checked = !!state.showChecksums;
        this.allBox.addEventListener("change", () => { state.showChecksums = this.allBox.checked; this.load(); });
        tools.push(el("label", { class: "check", title: "Show the details and DAT checksums of every game below - and those of your own file when it matched" },
          this.allBox, "Show checksums"));
      }
      this.colMenu = el("details", { class: "col-menu hidden" }, el("summary", { class: "btn btn-small", text: "Columns" }), el("div", { class: "col-menu-pop" }));
      tools.push(this.colMenu);
      container.replaceChildren(
        el("div", { class: "ptable-tools" }, ...tools),
        this.selBar,
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
      Prefs.save();
      try {
        data = await this.opts.fetch({ offset: this.offset, limit: this.opts.pageSize, q: this.q,
          checksums: !!(this.opts.detail && state.showChecksums) });
      } catch (err) {
        if (seq === this.seq) this.body.replaceChildren(el("div", { class: "empty error", text: err.message }));
        return;
      }
      if (seq !== this.seq) return; // a newer request superseded this one
      this.render(data);
    }

    /** The columns to show: not hidden by the user and not switched off by their own ``when(data)``. */
    visibleColumns() {
      return this.opts.columns.filter((c) => !this.hidden.has(c.label) && (!c.when || c.when(this.data || {})));
    }

    drawColumnMenu() {
      const optional = this.opts.columns.filter((c) => c.label && !c.always && (!c.when || c.when(this.data || {})));
      this.colMenu.classList.toggle("hidden", !this.opts.id || optional.length < 2);
      this.colMenu.querySelector(".col-menu-pop").replaceChildren(...optional.map((c) => el("label", { class: "check" },
        el("input", { type: "checkbox", checked: this.hidden.has(c.label) ? null : "",
          on: { change: (e) => {
            if (e.target.checked) this.hidden.delete(c.label); else this.hidden.add(c.label);
            Prefs.setHiddenColumns(this.opts.id, this.hidden);
            this.render(this.data);
          } } }), c.label)));
    }

    drawSelection() {
      const sel = this.opts.select;
      if (!sel) return;
      const n = this.selected.size;
      this.selBar.classList.toggle("hidden", !n);
      this.selBar.replaceChildren(
        el("strong", { text: `${fmt(n)} selected` }),
        ...sel.actions.map((a) => el("button", { class: `btn btn-small ${a.cls || ""}`, text: a.label, title: a.title || "",
          on: { click: async () => { await a.run([...this.selected.values()]); this.selected.clear(); this.render(this.data); } } })),
        el("button", { class: "btn btn-small", text: "Clear", on: { click: () => { this.selected.clear(); this.render(this.data); } } }));
    }

    render(data) {
      this.data = data;
      const { total, items } = data;
      if (this.offset > 0 && this.offset >= total) { this.offset = 0; this.load(); return; }
      this.countEl.textContent = `${fmt(total)} item${total === 1 ? "" : "s"}`;
      this.drawColumnMenu();
      if (!items.length) {
        this.body.replaceChildren(el("div", { class: "empty", text: this.q ? `No results for "${this.q}".` : this.opts.emptyText }));
      } else {
        const detail = this.opts.detail;
        const select = this.opts.select;
        const cols = this.visibleColumns();
        const sort = this.opts.sort;
        const cur = sort ? sort.get() : "";
        const selectable = (item) => !select.can || select.can(item);
        const headBox = select ? el("input", { type: "checkbox", "aria-label": "Select every row of this page",
          on: { change: (e) => {
            for (const it of items.filter(selectable)) { if (e.target.checked) this.selected.set(select.id(it), it); else this.selected.delete(select.id(it)); }
            this.render(this.data);
          } } }) : null;
        if (headBox) headBox.checked = items.filter(selectable).length > 0 && items.filter(selectable).every((it) => this.selected.has(select.id(it)));
        const head = el("tr", {}, select ? el("th", { class: "sel-col" }, headBox) : null, cols.map((c) => {
          if (!c.sortKey || !sort) return el("th", { text: c.label });
          const first = c.sortFirst || "asc";
          const [key, dir] = cur === "rating" ? ["rating", "desc"] : cur.split("_");
          const on = key === c.sortKey;
          const next = !on ? `${c.sortKey}_${first}` : dir === first ? `${c.sortKey}_${first === "asc" ? "desc" : "asc"}` : "";
          return el("th", { "aria-sort": on ? (dir === "asc" ? "ascending" : "descending") : "none" },
            el("button", { class: "th-sort", type: "button",
              title: `Sort by ${c.sortKey}${on ? (next ? "" : " (click to clear)") : ""}`,
              text: `${c.label}${on ? (dir === "asc" ? " ▲" : " ▼") : ""}`,
              on: { click: () => { sort.set(next); this.offset = 0; this.load(); } } }));
        }), detail ? el("th", { class: "cs-col", text: "Details" }) : null);
        const rows = [];
        const span = cols.length + (select ? 1 : 0) + 1;
        for (const item of items) {
          let pick = null;
          if (select) {
            pick = el("td", { class: "sel-col" }, selectable(item) ? el("input", { type: "checkbox", "aria-label": "Select this row",
              checked: this.selected.has(select.id(item)) ? "" : null,
              on: { change: (e) => {
                if (e.target.checked) this.selected.set(select.id(item), item); else this.selected.delete(select.id(item));
                this.drawSelection();
              } } }) : null);
          }
          const tr = el("tr", { class: this.opts.rowClass ? this.opts.rowClass(item) : null },
            pick, cols.map((c) => el("td", { class: c.cls || "" }, c.render(item))));
          rows.push(tr);
          if (detail) rows.push(...this.detailRows(item, tr, detail, span));
        }
        this.body.replaceChildren(el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, head), el("tbody", {}, rows))));
      }
      this.drawSelection();
      const end = Math.min(this.offset + items.length, total);
      this.pageInfo.textContent = total ? `${fmt(this.offset + 1)}-${fmt(end)} of ${fmt(total)}` : "";
      this.prev.disabled = this.offset === 0;
      this.next.disabled = end >= total;
    }

    /** The "Details" toggle cell of a row (appended to it) and the full-width panel row below it. */
    detailRows(item, tr, detail, span) {
      const ref = detail.ref ? detail.ref(item) : { kind: detail.kind, id: item.id };
      const extra = detail.extra ? detail.extra(item) : null;
      if (!ref && !extra) { tr.append(el("td", { class: "cs-col" })); return []; }
      const panel = el("td", { colspan: String(span), class: "cs-cell" });
      const row = el("tr", { class: "detail-row hidden" }, panel);
      const btn = el("button", { class: "btn btn-small cs-toggle", "aria-expanded": "false", text: "Show",
        title: "Why this row is where it is, and the DAT checksums with those of your matching file" });
      const set = async (open) => {
        btn.setAttribute("aria-expanded", open ? "true" : "false");
        btn.textContent = open ? "Hide" : "Show";
        row.classList.toggle("hidden", !open);
        if (open && !panel.firstChild) {
          const sums = el("div", { class: "cs-sums" });
          panel.replaceChildren(extra, sums);
          if (!ref) return;
          sums.replaceChildren(el("span", { class: "muted", text: "Loading checksums..." }));
          try {
            const payload = item.checksums || await get(`/api/scan/checksums?${qs({ kind: ref.kind, id: ref.id })}`);
            item.checksums = payload;
            sums.replaceChildren(checksumPanel(payload));
          } catch (err) {
            sums.replaceChildren(el("span", { class: "error-text", text: err.message }));
          }
        }
      };
      btn.addEventListener("click", () => set(btn.getAttribute("aria-expanded") !== "true"));
      tr.append(el("td", { class: "cs-col" }, btn));
      if (state.showChecksums) set(true);
      return [row];
    }
  }

  const badge = (status, text = status) => el("span", { class: `badge ${status}`, text });

  // Small coloured chips for a row's name tags (regions, languages, status, bad dump, how it matched).
  const VIA_LABEL = { headerless: "copier header", byteswapped: "byte-swapped", container: "rvz" };
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
    libDefaults: null, // GET /api/library/defaults answer (your defaults, Settings page)
    libReason: "",     // Build library preview filters
    libStatus: "",
    libWhy: "",        // primary exclusion code filter of the preview
    vanishReason: "",
    libStats: null,    // numbers of the last plan: {reasons, vanish, exclusions}
    gamesHave: "",     // "" | "1" | "0"
    gamesRated: "",    // "" | "1" (rated) | "0" (unrated)  -  Browse > Games
    gamesSaves: "",    // "" | "1" (only the games with saves)  -  Browse > Games, only with a RetroArch config
    libSaves: "",      // "" | with | affected  -  the Library preview's saves filter
    gamesSort: "",     // "" | rating_desc | rating_asc | name_asc | name_desc
    libSort: "",       // the same, for the Library preview
    vanishSort: "",    // ... and for "Games that vanish"
    kindSort: {},      // Browse: result kind -> sort of the matched / unmatched / missing tabs
    convertFilter: "",
    organiseFilter: "",
    organiseDest: "",
    m3uFilter: "",
    tab: "files",     // the open tab of the system page
    tiles: [],         // the tiles of the system pages (Settings > Tiles), from /api/status
    scanFailed: {},    // platform -> error text of its last failed scan
    showChecksums: false,   // Browse: checksum rows expanded for every visible row
  };

  // ------------------------------------------------------------------- jobs
  const Jobs = {
    handlers: {},
    timer: null,
    running: false,

    track(job) {
      this.last = job;
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
      // one start at a time: a double click must not ask for two jobs (the second would only answer "a job is running")
      if (this.starting) return;
      this.starting = true;
      try {
        const res = await post(path, body);
        this.track(res.job);
      } catch (err) {
        toast(err.message, "error");
      } finally {
        this.starting = false;
      }
    },

    finish(job) {
      if (this.lastFinished === job.id) return;
      this.lastFinished = job.id;
      if (job.status === "error") toast(`${job.kind} failed: ${job.error}`, "error", 9000);
      if (job.kind === "scan") {
        const failed = job.status === "error";
        if (!job.platform || job.platform === state.platform) {
          $("scan-error").classList.toggle("hidden", !failed);
          $("scan-error-text").textContent = failed ? job.error : "";
        }
        state.scanFailed[job.platform || ""] = failed ? job.error : "";
      }
      if (job.status === "cancelled") {
        const scanning = job.kind === "scan";
        toast(scanning ? "Scan stopped. What was read is kept: scanning again carries on from there." : `${job.kind} cancelled`, "info", scanning ? 7000 : undefined);
      }
      const handler = this.handlers[job.kind];
      if (handler) handler(job);
    },

    setRunning(running) {
      this.running = running;
      document.querySelectorAll("[data-needs-idle]").forEach((b) => {
        // blocked: nothing to act on; busy: its preview is being calculated; stale: its preview is out of date
        b.disabled = running || b.dataset.blocked === "1" || b.dataset.busy === "1" || b.dataset.stale === "1";
      });
    },

    /** The one global job bar in the header, plus the live progress of the system card the job belongs to. */
    render(job) {
      const box = $("job-bar");
      box.classList.remove("hidden", "error", "done");
      if (job.status === "error") box.classList.add("error");
      if (job.status === "done") box.classList.add("done");
      const { done, total, message } = job.progress || {};
      const pct = total > 0 ? Math.min(100, (100 * done) / total) : null;
      const isBytes = total > 1e6 && /download|updat/i.test(message || "");
      let countText = "";
      // a job of one step reports a fraction of 1: "0 / 1" says nothing, the percentage does
      if (total > 1) countText = isBytes ? `${fmtBytes(done)} / ${fmtBytes(total)}` : `${fmt(Math.floor(done))} / ${fmt(total)}`;
      let msg = message || "";
      if (job.status === "done") msg = "Finished";
      if (job.status === "error") msg = `Error: ${job.error}`;
      if (job.status === "cancelled") msg = "Cancelled";
      const what = `${JOB_LABEL[job.kind] || job.kind}${job.platform ? ` · ${job.platform}` : ""}`;

      const bar = el("div", { class: "progress-bar" });
      const progress = el("div", { class: "progress" }, bar);
      if (job.status === "running" && pct === null) progress.classList.add("indeterminate");
      else bar.style.width = `${job.status === "done" ? 100 : pct || 0}%`;

      const head = el("div", { class: "job-head" },
        el("b", { class: "job-what", text: what }),
        el("span", { class: "job-msg", text: msg, title: msg }),
        el("span", { class: "job-count", text: countText + (pct !== null && job.status === "running" ? `  (${fmtPct(pct)})` : "") }));
      if (job.status === "running" && job.cancellable) {
        head.append(el("button", {
          class: "btn btn-small", text: "Cancel",
          on: { click: async (e) => { e.target.disabled = true; try { await post("/api/job/cancel"); } catch (err) { toast(err.message, "error"); } } },
        }));
      } else if (job.status !== "running") {
        head.append(el("button", { class: "btn btn-small btn-ghost", text: "Hide", on: { click: () => box.classList.add("hidden") } }));
      }
      // time and data: elapsed, an estimate of what is left, and how much data the job has dealt with so far
      const elapsed = Math.max(0, (job.finished || Date.now() / 1000) - job.started);
      const bits = [`${job.status === "running" ? "running" : "took"} ${fmtDuration(elapsed)}`];
      if (job.status === "running" && pct !== null && pct >= 1 && elapsed >= 4) bits.push(`about ${fmtDuration((elapsed * (100 - pct)) / pct)} left`);
      if (job.bytes > 0) {
        bits.push(`${fmtBytes(job.bytes)} ${/^(scan|collection|verify)$/.test(job.kind) ? "scanned" : "processed"}`);
        if (job.status === "running" && elapsed >= 4) bits.push(`${fmtBytes(job.bytes / elapsed)}/s`);
      }
      const timing = el("div", { class: "job-time muted small", text: bits.join(" · ") });
      box.replaceChildren(head, progress, timing);
      renderCardJob(job, msg, countText, pct);
      clearTimeout(this.hideTimer);
      if (job.status === "done" || job.status === "cancelled") this.hideTimer = setTimeout(() => box.classList.add("hidden"), 8000);
    },
  };

  /** "0.4%" for a sliver, "37%" otherwise: a scan of hundreds of gigabytes is below 1 % for minutes. */
  const fmtPct = (pct) => `${pct < 10 ? pct.toFixed(1) : pct.toFixed(0)}%`;
  const JOB_LABEL = { scan: "Scan", verify: "Verify", library: "Library", organise: "Organise", convert: "Convert",
    m3u: "Playlists", retroarch: "RetroArch", switch: "Switch scan", switchdb: "Switch title database", switchverify: "Switch checksums" };


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
  /** Where a system's DATs come from, for the screen: two systems have no DAT of checksums, their catalogue is made from a list. */
  const platformSource = (p) => (p.name === "Nintendo Switch" ? "titledb" : p.name === "Nintendo Wii U" ? "GameTDB" : sourceLabel(p.source));
  const isGameFolder = (p) => !!p && p.layout === "game_folder";   // disc systems (Dreamcast, PlayStation, PlayStation 2): one folder per game
  const discOf = (p) => (p && p.disc) || null;                         // {key, label, gd, iso, playlists, iso_mode, ...}
  const discPlaylists = (p) => !!p && isGameFolder(p) && (!discOf(p) || discOf(p).playlists !== false);
  const folderOf = (p) => (p ? (p.name in state.drafts ? state.drafts[p.name] : p.folder || "") : "");

  // ------------------------------------------- status, systems and folders
  async function loadStatus() {
    try {
      state.status = await get("/api/status");
      showBanner("");
    } catch (err) {
      showBanner(err.message);
      return;
    }
    const s = state.status;
    if (Array.isArray(s.tiles) && s.tiles.join() !== state.tiles.join()) { state.tiles = s.tiles; Totals.shown[""] = ""; if (inSystem()) { renderTotals(); loadInsights(); } }
    $("app-version").textContent = `v${s.version}`;
    Updates.apply(s.updates);
    $("dats-dir").textContent = s.data_dir ? `Data folder: ${s.data_dir}` : "";
    if (!state.exportLoaded) { state.exportLoaded = true; loadExportSettings(); }      // once: later loads must not undo an edit
    paintExamplePaths();
  }

  /** The example paths in the empty folder fields, written the way this platform writes paths. The page's own
   *  placeholders are the Linux ones (~/Emulation/...); a Windows server gets drive-letter examples instead. */
  function paintExamplePaths() {
    if (serverOs() !== "windows") return;
    $("lib-export-dest").placeholder = "Folder to build the library in, e.g. D:\\Emulation\\library";
    $("ra-shared-base").placeholder = "The folder that holds them, e.g. D:\\Emulation\\assets";
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
      renderRatingsStatus();
      clearTimeout(this.timer);
      this.wasRunning = !!(u && u.running);
      if (this.wasRunning) this.timer = setTimeout(() => this.load(), 1500);
      else if (was) this.finished(u); // an update just ended: new DATs may be installed
    },

    async finished(u) {
      await loadStatus();
      await loadPlatforms();
      loadTotals();                      // new DATs: the library totals follow them
      Previews.retryPending();           // a preview that waited for the ratings data runs again
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
    const tosec = u.tosec || {}, ni = u.nointro || {}, whd = u.whdload || {}, red = u.redump || {};
    const parts = [
      tosec.installed ? `TOSEC ${tosec.installed}` : "TOSEC not installed",
      ni.installed ? `No-Intro ${ni.installed}` : "No-Intro not installed",
      whd.installed ? `WHDLoad ${whd.installed}` : "WHDLoad not installed",
      red.installed ? `Redump ${red.installed}` : "Redump not installed",
    ];
    const rat = u.ratings || {};
    if (rat.installed) parts.push(`Ratings ${rat.installed}`);
    else if (rat.wanted) parts.push("Ratings not installed");
    if (u.last_checked) parts.push(`checked ${checkedText(u.last_checked)}`);
    else if (!u.enabled) parts.push("automatic updates are off");
    const labels = { checking: "Checking for updates...", downloading: "Updating DATs...", installing: "Installing DATs..." };
    const ratingsRun = u.running && (u.progress || {}).source === "ratings";
    $("updates-line").textContent = (u.running ? `${ratingsRun ? "Updating the ratings..." : (labels[u.state] || "Updating...")}  ` : "") + parts.join(" · ");
    const btn = $("updates-btn");
    btn.textContent = u.running ? "Cancel update" : "Check for updates";
    btn.disabled = !u.enabled && !u.running;
    btn.title = u.enabled || u.running ? "Check TOSEC, No-Intro, WHDLoad, Redump and the ratings for newer versions and install them" : "Automatic updates are switched off in this run";
    if (Databases.open) Databases.load();

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

  /** The Databases page: every list the app uses, with its address, the systems that use it and its state. */
  const DB_STATUS = { up_to_date: ["ok", "up to date"], update_available: ["move", "update available"], updating: ["move", "updating"],
    absent: ["missing", "not installed"], missing: ["missing", "not installed"], unknown: ["skip", "not checked this session"], error: ["conflict", "check failed"],
    bundled: ["ok", "ships with the app"] };
  /** The BIOS, firmware and key files page: the rule, the lists the checksums come from, what the scans found. The check of the
   *  emulators' own files (``RetroArch``) lives on this page too. */
  const Bios = {
    async show() {
      try {
        const d = await get("/api/bios");
        $("bios-rule").replaceChildren(el("span", { text: `Your default: ${d.keep_default ? "keep them" : "treat them like any other file"}. ` }),
          d.overridden.length ? el("span", { text: `Overridden for: ${d.overridden.join(", ")}.` }) : el("span", { class: "muted", text: "No system overrides it." }));
        $("bios-sources").replaceChildren(...d.sources.map((x) => el("tr", {},
          el("td", { text: x.name }), el("td", { class: "num", text: x.version || "-" }),
          el("td", { class: "num", text: x.installed ? fmt(x.entries) : "not installed" }), el("td", { class: "wrap small", text: x.note }))));
        $("bios-found").replaceChildren(d.found.length
          ? el("div", { class: "table-wrap" }, el("table", {},
            el("thead", {}, el("tr", {}, ["System", "Files", "Last scan"].map((h) => el("th", { text: h })))),
            el("tbody", {}, d.found.map((f) => el("tr", {},
              el("td", {}, el("a", { href: Route.build(f.slug, "files", { show: "unmatched" }), text: f.platform })),
              el("td", { class: "num", text: fmt(f.files) }), el("td", { class: "num muted", text: f.at.replace("T", " ") }))))))
          : el("div", { class: "muted", text: "None yet: scan a system, and its BIOS files are listed here." }));
      } catch (err) { toast(err.message, "error"); }
    },
  };

  const Databases = {
    rows: [],
    open: false,

    async show() {
      this.open = true;
      await this.load();
    },

    async load() {
      try { this.rows = (await get("/api/databases")).databases; } catch (err) { toast(err.message, "error"); return; }
      this.render();
    },

    render() {
      const status = (st) => { const [cls, text] = DB_STATUS[st] || ["skip", st || "-"]; return el("span", { class: `badge ${cls}`, text }); };
      const groups = [...new Set(this.rows.map((r) => r.group))];
      $("databases-body").replaceChildren(...groups.map((g) => el("section", { class: "panel" },
        el("h2", { text: g }),
        el("div", { class: "table-wrap" }, el("table", {},
          el("thead", {}, el("tr", {}, ["Source", "Database", "Used by", "Installed", "Newest known", "Status", "Last checked"].map((h) => el("th", { text: h })))),
          el("tbody", {}, this.rows.filter((r) => r.group === g).map((r) => el("tr", {},
            el("td", { text: r.source }),
            el("td", { class: "wrap" }, el("a", { href: r.url, target: "_blank", rel: "noopener noreferrer", text: r.name }),
              el("div", { class: "sub mono-path", text: r.url }), r.note ? el("div", { class: "sub", text: r.note }) : null),
            el("td", { class: "wrap small", text: r.used_by.length > 4 ? `${r.used_by.length} systems` : r.used_by.join(", ") || "-", title: r.used_by.join(", ") }),
            el("td", { class: "num", text: r.installed || "not installed" }), el("td", { class: "num", text: r.latest || "-" }),
            el("td", {}, status(r.status)), el("td", { class: "num muted", text: r.checked_at ? checkedText(r.checked_at) : (r.status === "bundled" ? "-" : "never") }))))))))
        .concat([el("div", { class: "muted small", text: "Versions are the date in each DAT. \"Newest known\" is what the last check found online; No-Intro and WHDLoad are compared by content, so they show the installed date once they are current. The Nintendo Wii U and Switch lists are made here from the names and IDs below them: no checksum list exists for those systems." })]));
    },
  };

  // ------------------------------------------------------------- routing
  // Hash routes (no server support needed; back / forward / reload just work):
  //   #/                                  home: one card per system
  //   #/system/<slug>/<tab>[?show=...]    <tab> = files | settings   (the addresses of the old tabs open the files)
  const TABS = ["files", "settings"];
  // what the lists of the old Browse tab were called -> the "Show" filter of the files
  const OLD_VIEWS = { games: "", matched: "have", missing: "missing", unmatched: "unmatched", saves: "saves" };
  const slugOf = (p) => p.slug || String(p.name).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  const Route = {
    /** "#/system/snes/browse?view=missing&have=0" -> {view: "system", slug, tab, params}; anything else -> home. */
    parse(hash) {
      const text = String(hash || "").replace(/^#/, "");
      const [path, query = ""] = text.split("?");
      const parts = path.split("/").filter(Boolean);
      if (parts[0] === "system" && parts[1]) {
        const tab = TABS.includes(parts[2]) ? parts[2] : "files";
        return { view: "system", slug: decodeURIComponent(parts[1]), tab, params: Object.fromEntries(new URLSearchParams(query)) };
      }
      if (parts[0] === "retroarch") return { view: "retroarch", slug: "", tab: "", params: {} };
      if (parts[0] === "chd") return { view: "chd", slug: "", tab: "", params: {} };
      if (parts[0] === "settings") return { view: "settings", slug: "", tab: "", params: {} };
      if (parts[0] === "databases") return { view: "databases", slug: "", tab: "", params: {} };
      if (parts[0] === "bios") return { view: "bios", slug: "", tab: "", params: {} };
      if (parts[0] === "switch") return { view: "system", slug: "nintendo-switch", tab: "files", params: {} };
      if (parts[0] === "ps2") return { view: "system", slug: "sony-playstation-2", tab: "files", params: {} };
      if (parts[0] === "wii") return { view: "system", slug: "nintendo-wii", tab: "files", params: {} };
      if (parts[0] === "wiiu") return { view: "system", slug: "nintendo-wii-u", tab: "files", params: {} };
      return { view: "home", slug: "", tab: "", params: {} };
    },
    build(slug, tab = "files", params = {}) {
      const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();
      return `#/system/${encodeURIComponent(slug)}/${tab}${q ? `?${q}` : ""}`;
    },
  };
  const inSystem = () => !!state.route && state.route.view === "system";
  const platformBySlug = (slug) => state.platforms.find((p) => slugOf(p) === slug) || null;
  const goto = (hash) => { if (location.hash === hash) applyRoute(); else location.hash = hash; };

  async function loadPlatforms() {
    try {
      state.platforms = await get("/api/platforms");
    } catch (err) {
      state.platforms = [];
      $("home-groups").replaceChildren(el("div", { class: "empty error", text: `Could not load systems: ${err.message}` }));
      return;
    }
    const names = state.platforms.map((p) => p.name);
    if (!names.includes(state.platform)) {
      const s = state.status || {};
      state.platform = [s.scan && s.scan.platform, s.last_platform, s.default_platform].find((n) => names.includes(n)) || names[0] || null;
    }
    renderHome();
    if (inSystem()) { renderSystemHead(); renderPlatform(); }
  }

  // --------------------------------------------------------- home: system cards
  /** Home groups come from what a system is (no system names here): disc systems, cartridge consoles, computers. */
  const GROUPS = [
    ["computers", "Computers", (p) => !isGameFolder(p) && p.source !== "nointro" && p.source !== "redump"],
    ["consoles", "Cartridge consoles", (p) => !isGameFolder(p) && (p.source === "nointro" || p.name === "Nintendo Switch")],
    ["discs", "Disc systems", (p) => isGameFolder(p) || (p.source === "redump" && p.name !== "Nintendo Switch")],
  ];

  const groupOf = (p) => (GROUPS.find((g) => g[2](p)) || GROUPS[0])[0];
  const datVersion = (p) => {
    const present = p.dats.filter((d) => d.present);
    return present.length ? present[0].version : null;
  };

  /** DAT status text of a system: installed version, or missing / updating. */
  function datStatus(p) {
    const updating = !!(state.updates && state.updates.running);
    const present = p.dats.filter((d) => d.present);
    if (!p.dats.length || present.length === 0) return { kind: "missing", text: updating ? "DAT updating..." : "DAT missing" };
    if (present.length < p.dats.length) {
      return { kind: "missing", text: updating ? "DAT updating..." : `${present.length} of ${p.dats.length} DATs installed` };
    }
    const v = datVersion(p);
    return { kind: "ok", text: v ? `DAT ${String(v).split(" ")[0]}` : "DAT installed", title: v ? `DAT version ${v}` : "" };
  }

  const scanTime = (rec) => (rec && rec.at ? checkedText(rec.at) : "");

  /** The sidebar: a few fixed links above; below, the systems by group, each with a status dot and the % owned. */
  function sideItem(p) {
    const rec = p.last_scan;
    const dat = datStatus(p);
    const stale = !!(rec && rec.folder && p.folder && rec.folder !== p.folder);
    const have = rec ? rec.have : 0, total = rec ? rec.total : 0;
    const pct = rec ? (rec.pct !== undefined ? rec.pct : (total ? (100 * have) / total : 0)) : null;
    const kind = dat.kind === "missing" ? "warn" : rec && !stale ? "ok" : "off";
    const saves = rec && rec.saves && rec.saves.files ? rec.saves : null;
    const mine = [...new Set((p.rule_overrides || []).map(ruleName))];
    const tip = [p.name, p.folder || "No folder set", dat.text,
      rec ? `${fmt(have)} of ${fmt(total)} (${pctText(pct)}), scanned ${scanTime(rec)}${stale ? " (folder changed since)" : ""}` : "Not scanned yet",
      saves ? `${nPlural(saves.files, "save or save state", "saves and save states")} in RetroArch` : "",
      mine.length ? `Its own library rules: ${mine.join(", ")}` : "",
      ].filter(Boolean).join("\n");
    return el("a", { class: "nav-item", href: Route.build(slugOf(p), state.route && state.route.view === "system" ? state.tab || "files" : "files"),
      "data-platform": p.name, "data-kind": kind, title: tip,
      "aria-current": inSystem() && state.platform === p.name ? "true" : null },
      el("span", { class: `dot ${kind}` }),
      el("span", { class: "nav-name", text: p.name }),
      el("span", { class: "nav-meta" },
        mine.length ? el("span", { class: "nav-override", "data-overrides": String(mine.length), text: "Custom rules", title: `Its own library rules: ${mine.join(", ")}`,
          "aria-label": `${nPlural(mine.length, "library rule", "library rules")} set for this system only` }) : null,
        saves ? el("span", { class: "nav-saves num", text: `${fmt(saves.files)} saves`, title: "Saves and save states found for this system" }) : null,
        el("span", { class: "nav-pct num", text: pct === null ? "" : `${Math.round(pct)}%` })),
      el("span", { class: "nav-job hidden" }, el("span", { class: "progress" }, el("span", { class: "progress-bar" }))));
  }

  function renderHome() {
    const box = $("home-groups");
    if (!state.platforms.length) { box.replaceChildren(el("div", { class: "empty", text: "No systems defined." })); return; }
    const q = $("side-q").value.trim().toLowerCase();
    const groups = GROUPS.map(([key, title]) => {
      const list = state.platforms.filter((p) => groupOf(p) === key && (!q || p.name.toLowerCase().includes(q)));
      const items = list.map(sideItem);
      return items.length ? el("section", { class: "side-group", "aria-label": title },
        el("h2", { class: "side-title", text: title }), ...items) : null;
    }).filter(Boolean);
    box.replaceChildren(...(groups.length ? groups : [el("div", { class: "muted small side-none", text: "No system matches." })]));
    $("retroarch-link").setAttribute("aria-current", state.route && state.route.view === "retroarch" ? "true" : "false");
    $("chd-link").setAttribute("aria-current", state.route && state.route.view === "chd" ? "true" : "false");
    $("settings-link").setAttribute("aria-current", state.route && state.route.view === "settings" ? "true" : "false");
    $("databases-link").setAttribute("aria-current", state.route && state.route.view === "databases" ? "true" : "false");
    $("bios-link").setAttribute("aria-current", state.route && state.route.view === "bios" ? "true" : "false");
    Jobs.setRunning(Jobs.running);
    if (Jobs.last) renderCardJob(Jobs.last);
  }

  /** Narrow windows show the system list on demand; wide ones keep it (the choice is remembered). */
  const wideLayout = () => window.matchMedia("(min-width: 901px)").matches;
  function setSide(open, remember = false) {
    $("shell").dataset.side = open ? "open" : "closed";
    $("side-toggle").setAttribute("aria-expanded", open ? "true" : "false");
    $("side-toggle").textContent = wideLayout() ? (open ? "\u2039 Hide list" : "Systems \u203A") : (open ? "Hide systems" : "\u2039 Systems");
    if (remember && wideLayout()) { try { localStorage.setItem("romorg.side", open ? "open" : "closed"); } catch (_) { /* optional */ } }
  }
  function initSide() {
    let saved = null;
    try { saved = localStorage.getItem("romorg.side"); } catch (_) { /* optional */ }
    setSide(wideLayout() ? saved !== "closed" : false);
    $("side-toggle").addEventListener("click", () => setSide($("shell").dataset.side !== "open", true));
    $("side-q").addEventListener("input", () => renderHome());
    window.matchMedia("(min-width: 901px)").addEventListener("change", () => setSide(wideLayout() ? saved !== "closed" : false));
  }

  /** Live progress of the running job under its system in the list (the other items stay as they are). */
  function renderCardJob(job, msg, countText, pct) {
    if (!job) return;
    if (msg === undefined) {
      const pr = job.progress || {};
      msg = pr.message || "";
      pct = pr.total > 0 ? Math.min(100, (100 * pr.done) / pr.total) : null;
    }
    document.querySelectorAll(".nav-item[data-platform]").forEach((item) => {
      const box = item.querySelector(".nav-job");
      const mine = job.status === "running" && !!job.platform && item.dataset.platform === job.platform;
      item.classList.toggle("running", mine);
      box.classList.toggle("hidden", !mine);
      if (!mine) {                                          // give the item its own tooltip back
        if (item.dataset.tip !== undefined) { item.title = item.dataset.tip; delete item.dataset.tip; }
        return;
      }
      if (item.dataset.tip === undefined) item.dataset.tip = item.title;
      // Only the bar here: what the job is doing is written once, in the job bar at the top (this one has it as a tooltip).
      // A big scan is a tiny fraction for a long time, so a known fraction never draws thinner than a sliver; an unknown one
      // (the job has not counted its work yet) leaves the width to the stylesheet so the sliding animation shows.
      const known = pct !== null && pct !== undefined;
      box.querySelector(".progress").classList.toggle("indeterminate", !known);
      const fill = box.querySelector(".progress-bar");
      if (known) fill.style.width = `${Math.max(2, Math.min(100, pct))}%`;
      else fill.style.removeProperty("width");
      item.title = `${JOB_LABEL[job.kind] || job.kind}: ${msg}${known ? ` (${fmtPct(pct)})` : ""}`;
    });
  }

  // ----------------------------------------------- system page header + folder field
  /** The platform the server runs on ("windows", "linux", "darwin", ...), from /api/status. */
  function serverOs() { return (state.status && state.status.os) || ""; }

  /** An example ROM folder in the form this platform uses (a Steam Deck SD card on Linux). */
  function folderExample(hint) {
    const os = serverOs();
    if (os === "windows") return `D:\\Emulation\\roms\\${hint}`;
    if (os === "darwin") return `/Volumes/<drive>/roms/${hint}`;
    return `/run/media/deck/<SD>/roms/${hint}`;
  }

  function renderSystemHead() {
    const p = currentPlatform();
    if (!p) return;
    $("sys-title").textContent = p.name;
    $("sys-source").textContent = platformSource(p);
    $("sys-source").className = `badge src-${p.source}`;
    $("sys-sub").textContent = p.extensions && p.extensions.length ? p.extensions.join(" ") : "";
    Switch.load();
    const example = folderExample(p.folder_hint || "...");
    $("folder-example").textContent = example;
    $("folder-input").placeholder = example;
    $("folder-input").setAttribute("aria-label", `${p.name} folder`);
    if (document.activeElement !== $("folder-input")) $("folder-input").value = folderOf(p);
    paintFolder();
    renderTabs();
  }

  /** Saved / Not saved chip, Clear and Scan state of the folder field. */
  function paintFolder() {
    const p = currentPlatform();
    if (!p) return;
    const value = $("folder-input").value.trim();
    const saved = p.folder || "";
    let kind, text, title = "";
    if (state.folderBusy[p.name]) { kind = "saving"; text = "Saving..."; }
    else if (state.folderErr[p.name]) { kind = "error"; text = "Not saved"; title = state.folderErr[p.name]; }
    else if (value !== saved) { kind = "unsaved"; text = "Not saved"; title = "Saved when you leave the field, press Enter or Scan"; }
    else if (value) { kind = "saved"; text = "Saved"; title = "This folder is remembered"; }
    else { kind = "empty"; text = ""; }
    const chip = $("folder-state");
    chip.className = `folder-state ${kind}`;
    chip.textContent = text;
    chip.title = title;
    chip.dataset.folderState = kind;
    $("folder-clear-btn").disabled = !value && !saved;
    $("folder-panel").classList.toggle("attn", !value && !saved);
    $("folder-help").classList.toggle("hidden", false);
    $("scan-btn").dataset.blocked = value ? "0" : "1";
    Jobs.setRunning(Jobs.running);
  }

  function syncFolder() {
    const p = currentPlatform();
    if (!p) return;
    const input = $("folder-input");
    const value = input.value.trim();
    if (value === (p.folder || "")) delete state.drafts[p.name]; else state.drafts[p.name] = input.value;
    delete state.folderErr[p.name];
    paintFolder();
  }

  /** Save the folder text of a system now (serialised per system; the newest text always wins).
   *  Resolves true when the folder is saved (or was already), false when the server refused it. */
  const folderQueue = {};
  function commitFolder(p, quiet = false) {
    const run = async () => {
      const path = folderOf(p).trim();
      if (path === (p.folder || "")) { delete state.drafts[p.name]; return true; }
      state.folderBusy[p.name] = true;
      if (p.name === state.platform) paintFolder();
      try {
        const res = await post("/api/folders", { platform: p.name, path });
        p.folder = (res.folders || {})[p.name] || null;
        const live = state.platforms.find((x) => x.name === p.name);   // the list may have been reloaded meanwhile
        if (live && live !== p) live.folder = p.folder;
        if (folderOf(p).trim() === path) delete state.drafts[p.name];
        delete state.folderErr[p.name];
        if (state.status) state.status.folders = res.folders || {};
        if (!quiet) toast(p.folder ? `Saved folder for ${p.name}` : `Forgot the folder for ${p.name}`, "ok");
        renderHome();
        renderScanTarget();
        return true;
      } catch (err) {
        state.folderErr[p.name] = err.message;
        toast(`${p.name}: folder not saved - ${err.message}`, "error", 8000);
        return false;
      } finally {
        delete state.folderBusy[p.name];
        if (p.name === state.platform) paintFolder();
      }
    };
    folderQueue[p.name] = (folderQueue[p.name] || Promise.resolve()).then(run, run);
    return folderQueue[p.name];
  }

  /** "Folder layout after building": what is in the system folder. What the rules leave out is there only while there is no archive
   *  folder (otherwise it lies in the archive folder, shown on the Library tab). */
  function renderLibraryLayout() {
    const p = currentPlatform();
    const gameFolder = isGameFolder(p);
    const flat = isFlat(p);
    const hasM3uLayout = !!(p && p.m3u_dats && p.m3u_dats.length) || gameFolder;
    const archived = archiveOn();
    $("library-layout").textContent = !p ? "" : [
      flat ? "<console folder>/" : "<platform folder>/",
      ...(gameFolder ? ["  <Redump name>/<Redump name>.chd   (+ .zip .md5 .state .srm ... named alike)",
        discPlaylists(p) ? "  <Redump name> (Disc 2)/...         (multi-disc games: one folder per disc + a playlist next to disc 1)"
          : "  <Redump name> (Disc 2)/...         (multi-disc games: one folder per disc, no playlist)"] : []),
      ...(gameFolder ? [] : flat ? [p.source === "whdload" ? "  <WHDLoad name>.lha   (the exact database file name)" : "  <No-Intro name>.<ext>   (or .zip / .7z named after the game)"]
        : p.dats.map((d) => `  ${d.folder}/`)),
      ...reserved(hasM3uLayout && !gameFolder, !!p.convertible, p.protected_dirs || [], archived)].join("\n");
  }

  function renderPlatform() {
    const p = currentPlatform();
    state.rendered = p ? p.name : null;
    $("platform-details-title").textContent = p ? `DAT files for ${p.name} (${platformSource(p)})` : "DAT files";
    const rows = p ? p.dats.map((d) => el("tr", {},
      el("td", { class: "wrap" }, el("div", { text: d.name }),
        el("div", { class: "sub", text: [d.m3u ? "M3U playlists" : ""].filter(Boolean).join(" · ") })),
      el("td", { class: "mono", text: d.version || "-" }),
      el("td", { class: "mono wrap", text: d.folder ? `${d.folder}/` : "(console folder)" }),
      el("td", {}, badge(d.present ? "ok" : "missing")))) : [];
    $("platform-dats").replaceChildren(...(rows.length ? rows : [el("tr", {}, el("td", { colspan: "4", class: "muted", text: "No system selected." }))]));

    const missing = p ? p.dats.filter((d) => !d.present) : [];
    const notice = $("dats-missing");
    notice.classList.toggle("hidden", !missing.length);
    const updating = !!(state.updates && state.updates.running);
    notice.replaceChildren(
      el("b", { text: missing.length === (p ? p.dats.length : 0) ? `The DAT for ${p ? p.name : "this system"} is not installed yet. ` : `${missing.length} of ${p ? p.dats.length : 0} DATs for ${p ? p.name : "this system"} are not installed yet. ` }),
      updating ? "It is being downloaded now (see the update line at the top)."
        : "It is downloaded automatically - just press Scan, or use Check for updates at the top.");
    const ds = p ? datStatus(p) : { kind: "missing", text: "" };
    $("dat-status-line").replaceChildren(
      el("span", { class: `dat-chip ${ds.kind}`, text: ds.text }),
      p ? el("span", { class: "muted small", text: ` ${p.dats.length} DAT${p.dats.length === 1 ? "" : "s"} · ${platformSource(p)}` }) : null);

    // Library: what goes where for this layout.
    renderLibraryLayout();
    syncAsideDir();
    const hasM3u = !!(p && p.m3u_dats && p.m3u_dats.length);
    $("lib-labels-wrap").classList.toggle("hidden", !hasM3u && !discPlaylists(p));
    $("lib-savedisk-wrap").classList.toggle("hidden", !hasM3u);
    loadLibraryProfile();
    renderSystemHead();
    renderScanTarget();
    updateActionState();
  }

  // ------------------------------------------------------------------ tabs
  /** Mark the open tab. */
  function renderTabs() {
    for (const tab of TABS) {
      const on = tab === state.tab;
      const btn = $(`tabbtn-${tab}`);
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
      btn.tabIndex = on ? 0 : -1;
      $(`tab-${tab}`).classList.toggle("hidden", !on);
    }
  }

  /** Open a tab of the system page (the route already says which). Heavy parts load only now. */
  function showTab(tab) {
    const p = currentPlatform();
    state.tab = tab;
    renderTabs();

    if (tab === "files") {
      loadTotals();
      renderLibraryGate();
      Previews.resume("lib");
      Files.show();
    } else if (statsStale) {
      refreshLibraryStats();              // (the numbers next to the rules)
    }
  }

  /** Apply the address: home, or a system with its tab (and Browse filters). */
  function applyRoute() {
    state.route = Route.parse(location.hash);
    const r = state.route;
    if (r.view === "home" && state.platforms.length && wideLayout()) {     // a wide window always shows a system
      history.replaceState(null, "", Route.build(slugOf(currentPlatform() || state.platforms[0]), "files"));
      state.route = Route.parse(location.hash);
      return applyRoute();
    }
    if (!wideLayout()) setSide(r.view === "home");
    $("view-retroarch").classList.toggle("hidden", r.view !== "retroarch");
    $("view-chd").classList.toggle("hidden", r.view !== "chd");
    $("view-settings").classList.toggle("hidden", r.view !== "settings");      // (every page is shown or hidden before any of them returns)
    $("view-databases").classList.toggle("hidden", r.view !== "databases");
    $("view-bios").classList.toggle("hidden", r.view !== "bios");
    Databases.open = r.view === "databases";
    if (r.view === "databases") {
      $("view-home").classList.add("hidden");
      $("view-system").classList.add("hidden");
      document.title = "Databases - Simple ROM Organiser";
      state.viewWas = "databases";
      Databases.show();
      renderHome();
      window.scrollTo(0, 0);
      return;
    }
    if (r.view === "bios") {
      $("view-home").classList.add("hidden");
      $("view-system").classList.add("hidden");
      document.title = "BIOS and firmware - Simple ROM Organiser";
      state.viewWas = "bios";
      Bios.show();
      RetroArch.show();
      renderHome();
      window.scrollTo(0, 0);
      return;
    }
    if (r.view === "settings") {
      $("view-home").classList.add("hidden");
      $("view-system").classList.add("hidden");
      document.title = "Settings - Simple ROM Organiser";
      state.viewWas = "settings";
      Settings.show();
      renderHome();
      window.scrollTo(0, 0);
      return;
    }
    if (r.view === "chd") {
      $("view-home").classList.add("hidden");
      $("view-system").classList.add("hidden");
      document.title = "Disc images - Simple ROM Organiser";
      state.viewWas = "chd";
      loadChdman();
      renderHome();
      window.scrollTo(0, 0);
      return;
    }
    if (r.view === "retroarch") {
      $("view-home").classList.add("hidden");
      $("view-system").classList.add("hidden");
      document.title = "Emulators and saves - Simple ROM Organiser";
      state.viewWas = "retroarch";
      Emu.show();
      RetroArch.show();
      renderHome();
      window.scrollTo(0, 0);
      return;
    }
    const home = r.view === "home" || !state.platforms.length;
    let p = home ? null : platformBySlug(r.slug);
    if (!home && !p) { if (state.platforms.length) history.replaceState(null, "", "#/"); state.route = Route.parse("#/"); p = null; }
    $("view-home").classList.toggle("hidden", !!p);
    $("view-system").classList.toggle("hidden", !p);
    $("shell").classList.toggle("in-system", !!p);
    if (!p) { document.title = "Simple ROM Organiser"; state.viewWas = "home"; renderHome(); window.scrollTo(0, 0); return; }
    document.title = `${p.name} - Simple ROM Organiser`;
    if (p.name !== state.platform) selectPlatform(p.name);
    else if (state.rendered !== p.name || state.viewWas !== "system") { renderPlatform(); applyScan(); }
    Files.applyParams(r.params);
    state.viewWas = "system";
    renderSystemHead();
    showTab(r.tab);
    renderHome();                                  // the open system is marked in the list
    window.scrollTo(0, 0);
    if (r.tab !== state.tab) history.replaceState(null, "", Route.build(slugOf(p), state.tab));
  }

  /** Switch tabs from a tab button: through the address, so Back / Forward / reload behave. */
  function openTab(tab) {
    const p = currentPlatform();
    if (p) goto(Route.build(slugOf(p), tab));
  }

  function selectPlatform(name) {
    if (!name || name === state.platform) return;
    state.platform = name;
    state.vanishSort = "";
    state.kindSort = {};
    Files.reset();
    Prefs.restore(name);
    // plan filters belong to the previous system (its destinations / statuses)
    state.organiseFilter = "";
    state.organiseDest = "";
    state.convertFilter = "";
    state.m3uFilter = "";
    $("lib-labels").checked = !isGameFolder(currentPlatform());   // |Disc N labels are PUAE syntax: off for disc-system playlists
    $("folder-input").value = folderOf(currentPlatform());
    $("scan-error").classList.toggle("hidden", !state.scanFailed[name]);
    $("scan-error-text").textContent = state.scanFailed[name] || "";
    renderPlatform();
    applyScan();
    syncAsideDir();
    if (!state.scan) restoreScan(name);
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

  // ------------------------------------------------------------- scan + overview
  function renderScanTarget() {
    paintFolder();
  }

  async function startScan(force = false) {
    const p = currentPlatform();
    if (!p) { toast("Choose a system first", "error"); return; }
    const path = folderOf(p).trim();
    if (!path) { toast(`Choose the ${p.name} folder first`, "error"); return; }
    if (!(await commitFolder(p, true))) return;   // remember the folder first (a bad path says why)
    if (force && !(await confirmDialog({ title: "Recalculate every checksum", okText: "Recalculate",
      body: el("p", { text: "Every file of this system is read again and its checksums are calculated again. A normal rescan reads only new and changed files, which is almost always enough. This can take a long time." }) }))) return;
    Jobs.start("/api/scan", { path, platform: p.name, ...(force ? { force: true } : {}) });
  }

  /** The scan buttons: "Rescan" once there is a scan (it reads new and changed files only), and the way to read everything again. */
  function renderScanButtons() {
    const scanned = !!state.scan;
    $("scan-btn").textContent = scanned ? "Rescan folder" : "Scan folder";
    $("scan-btn").title = scanned ? "Reads new and changed files only: what was read before is remembered (also after a stopped scan)" : "";
    $("scan-force-btn").classList.toggle("hidden", !scanned);
  }

  /** Coming back to a system whose scan is still kept by the server: no new scan. */
  async function restoreScan(name) {
    try {
      const r = await post("/api/scan/select", { platform: name });
      if (r.selected && state.platform === name) await refreshScan();
    } catch (_) { /* the Scan button is still there */ }
  }

  Jobs.handlers.scan = async (job) => {
    if (job.status !== "done") { renderHome(); return; }
    await loadPlatforms(); // the folder is remembered by the scan, and so is its summary (the cards)
    await refreshScan();
  };

  /** Pull the current scan from /api/status and re-render everything that depends on it. */
  async function refreshScan() {
    const [, platforms] = await Promise.all([loadStatus(), get("/api/platforms").catch(() => null)]);
    if (platforms) state.platforms = platforms;      // else keep the old list
    renderHome();
    applyScan();
  }

  /** Use the server's scan only when it is of the selected system. */
  function applyScan() {
    const s = state.status || {};
    state.lastScan = s.scan || null;
    state.scan = state.lastScan && state.lastScan.platform === state.platform ? state.lastScan : null;
    renderScanButtons();
    Previews.onScan();                // a preview of the same system stays (marked out of date), others are dropped
    renderScanOverview();
    renderLibraryIntro();
    renderLibraryGate();
    refreshUndo();
    renderLibraryConvert();
    updateActionState();
    loadTotals();
    if (inSystem() && state.tab === "files") { Previews.resume("lib"); Files.show(); }
    renderSavesBox();
  }

  /** A summary card; with ``link`` it opens the files with the matching filter (deep link). */
  function card(value, label, cls = "", link = null) {
    const body = [el("div", { class: "value", text: value }), el("div", { class: "label", text: label })];
    const p = currentPlatform();
    if (link && p) {
      return el("a", { class: `card card-link ${cls}`, href: Route.build(slugOf(p), "files", { show: OLD_VIEWS[link.view] || "" }),
        title: "Show these files" }, ...body);
    }
    return el("div", { class: `card ${cls}` }, ...body);
  }

  function progressBar(pct) {
    const bar = el("div", { class: "progress-bar" });
    bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    return el("div", { class: "progress" }, bar);
  }

  /** Arrow keys / Home / End move between the tabs of a tablist (roving focus, activates on arrow). */
  function tabKeys(list) {
    list.addEventListener("keydown", (e) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
      const tabs = Array.from(list.querySelectorAll('[role="tab"]')).filter((t) => !t.classList.contains("hidden"));
      const i = tabs.indexOf(document.activeElement);
      if (i < 0) return;
      let j = e.key === "ArrowRight" ? (i + 1) % tabs.length : e.key === "ArrowLeft" ? (i - 1 + tabs.length) % tabs.length
        : e.key === "Home" ? 0 : tabs.length - 1;
      e.preventDefault();
      tabs[j].focus();
      tabs[j].click();
      // the click re-renders the tabs of a result list: keep the focus on the same one
      const again = Array.from(list.querySelectorAll('[role="tab"]')).filter((t) => !t.classList.contains("hidden"))[j];
      if (again) again.focus();
    });
  }

  const byGame = (s) => s && s.count_by === "game";

  /** have / missing / total / percent of a scan summary (games for No-Intro and Redump, ROMs otherwise). */
  function summaryNumbers(s) {
    const games = byGame(s);
    const total = games ? first(s.games_total, s.dat_total) : s.dat_total;
    const have = games ? first(s.games_have, s.have) : s.have;
    const missing = games ? first(s.games_missing, s.missing) : s.missing;
    return { games, total, have, missing, pct: total ? (100 * have) / total : 0 };
  }

  /** The scan results of the library page: summary cards (links into Browse) and the per-DAT cards. */
  function renderScanOverview() {
    const scan = state.scan;
    const p = currentPlatform();
    renderTotals();
    $("scan-empty").classList.toggle("hidden", !!scan);
    $("scan-output").classList.toggle("hidden", !scan);
    if (!scan) {
      const rec = p && p.last_scan;
      const folder = folderOf(p).trim();
      $("scan-empty").replaceChildren(...(!folder
        ? [el("b", { text: "No folder set yet." }), " Choose the folder of this system above - then press ", el("b", { text: "Scan folder" }), "."]
        : rec ? [el("b", { text: "Last scan " }), scanTime(rec) + ": ",
          `${fmt(rec.have)} of ${fmt(rec.total)} (${pctText(rec.pct || 0)}), ${fmt(rec.missing)} missing. `,
          "The list is not kept when the app restarts - press ", el("b", { text: "Scan folder" }), " to browse the results and build the library."]
        : [el("b", { text: "Not scanned yet." }), " Press ", el("b", { text: "Scan folder" }), " to match this folder against the DAT."]));
      $("scan-info").textContent = "";
      renderDcBar({});
      return;
    }
    const s = scan.summary;
    const { games, total, have, missing, pct } = summaryNumbers(s);
    $("scan-info").textContent = `Results for ${scan.platform}  -  ${scan.root}`;
    const missingDats = scan.missing_dats || [];
    $("scan-missing-dats").classList.toggle("hidden", !missingDats.length);
    $("scan-missing-dats").textContent = missingDats.length
      ? `Not checked (DAT not downloaded): ${missingDats.join(", ")}` : "";

    renderDcBar(s);
    renderSavesCard();
    const via = s.matched_via || {};
    const normalised = (via.headerless || 0) + (via.byteswapped || 0);
    const L = (view, extra = {}) => ({ view, ...extra });
    $("summary-cards").replaceChildren(
      card(fmt(have), `${games ? "Games you have" : "Have"} (of ${fmt(total)})`, "ok", games ? L("games", { have: "1" }) : L("matched")),
      card(fmt(missing), games ? "Games missing" : "Missing", "bad", L("missing")),
      card(pctText(pct), "Complete", "info", games ? L("games") : L("matched")),
      card(fmt(s.matched_files), "Matched files", "ok", L("matched")),
      card(fmt(s.unmatched_files), "Unmatched files", s.unmatched_files ? "warn" : "", L("unmatched")),
      ...(s.correctly_placed !== undefined ? [card(fmt(s.correctly_placed), isFlat(currentPlatform()) ? "In place (named correctly)" : "In place (named + in DAT folder)", "ok", L("matched"))] : []),
      ...(s.checks ? [card(fmt(s.checks.damaged), "Damaged files (checksums)", s.checks.damaged ? "bad" : "ok", L("matched"))] : []),
      card(fmt(s.duplicates), "Duplicates", s.duplicates ? "warn" : "", L("matched")),
      ...(normalised ? [card(fmt(normalised), "Matched with header / byte-swapped", "info", L("matched"))] : []),
      ...(s.chd_files !== undefined ? [
        card(fmt(s.verified), "CHDs verified (every track)", s.verified ? "ok" : "", L("matched")),
        card(fmt(s.identified), "CHDs identified (data tracks)", s.identified ? "info" : "", L("matched")),
        card(fmt(s.raw), "Raw sets (convertible to CHD)", s.raw ? "warn" : "", L("matched"))]
        : s.convertible ? [card(fmt(s.convertible), "Convertible to No-Intro format", "info", L("matched"))] : []),
      ...(s.unsupported ? [card(fmt(s.unsupported), "Unsupported archives", "warn", L("unsupported"))] : []),
      card(fmt(s.bios_files || 0), "BIOS / firmware files found", s.bios_files ? "info" : "", L("unmatched")),
      ...(s.errors ? [card(fmt(s.errors), "Read errors", "bad", L("errors"))] : []),
      el("div", { class: "complete-bar" }, progressBar(pct)),
    );

    // Per-DAT cards (only worth showing with several DATs): open the Browse tab filtered to that DAT.
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
      return el("a", {
        class: "dat-card", title: `Browse only ${name}`,
        href: Route.build(slugOf(p), "files", { show: "have" }),
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
  }

  // ---- RetroArch saves (Amendment 31). Every function below is reached only when the server sent saves data, which it does
  // only when a RetroArch config is known: without one nothing about saves is drawn.
  const nPlural = (n, one, many) => `${fmt(n)} ${n === 1 ? one : many}`;

  /** `1 save · 6 states` (empty when there are none). */
  function savesText(c) {
    const bits = [];
    if (c && c.saves) bits.push(nPlural(c.saves, "save", "saves"));
    if (c && c.states) bits.push(nPlural(c.states, "state", "states"));
    return bits.join(" \u00b7 ");
  }

  /** The Saves cell of a Browse row: the total (saves + states), the breakdown under it, and the title's total when it differs. */
  function savesCell(c) {
    if (!c || (!c.total && !c.title_total)) return el("span", { class: "muted", text: "-" });
    return el("div", { class: "saves-cell", title: c.total ? savesText(c) : "No saves for this edition" },
      el("div", { class: "saves-total", text: c.total ? fmt(c.total) : "0" }),
      c.total ? el("div", { class: "sub", text: savesText(c) }) : null,
      c.title_total && c.title_total !== c.total ? el("div", { class: "sub", text: `all editions: ${fmt(c.title_total)}` }) : null);
  }

  /** Overview of a system: how many saves there are and which titles they belong to; what a pending build would do. */
  function renderSavesCard() {
    const box = $("ov-saves");
    const t = state.scan && state.scan.saves;
    box.classList.toggle("hidden", !t);
    if (!t) { box.replaceChildren(); return; }
    const p = currentPlatform();
    const line = t.sets
      ? `${nPlural(t.files, "save file", "save files")} for ${nPlural(t.sets, "game", "games")} (${fmt(t.rom_sets)} with a ROM here, ${fmt(t.dat_sets)} without, ${fmt(t.unmatched_sets)} unmatched${t.guessed_sets ? `, of which ${fmt(t.guessed_sets)} told by their title` : ""})`
      : "No save files found for this system.";
    const plan = libPlan && libPlan.saves;
    const will = [];
    if (plan) {
      if (plan.follow && plan.rename && plan.rename.files) will.push(`${plan.elsewhere ? "copy" : "rename"} ${nPlural(plan.rename.files, "file", "files")} with their ROMs`);
      if (plan.ps2 && plan.ps2.saves) will.push(`convert ${nPlural(plan.ps2.saves, "disc-console save", "disc-console saves")} to the better version`);
      if (plan.carry && plan.carry.files) will.push(`copy ${nPlural(plan.carry.files, "save", "saves")} to the better version`);
      if (plan.kept) will.push(`keep ${nPlural(plan.kept, "game", "games")} the rules would archive`);
      if (plan.archive && plan.archive.files) will.push(`archive ${nPlural(plan.archive.files, "file", "files")} with their ROMs`);
      if (plan.leave && plan.leave.files) will.push(`leave ${nPlural(plan.leave.files, "file", "files")} of archived games where they are`);
    }
    box.replaceChildren(
      el("h2", { id: "ov-saves-title", text: `Saves (${(t.sources && t.sources.length ? t.sources : ["RetroArch"]).join(", ")})` }),
      el("div", { class: "saves-line", id: "ov-saves-line", text: line }),
      t.cards ? el("div", { class: "sub", id: "ov-saves-cards", text: `${nPlural(t.cards, "memory card", "memory cards")} hold${t.cards === 1 ? "s" : ""} ${nPlural(t.card_games, "save", "saves")} of games (listed under Browse > Saves).` }) : null,
      t.sets ? el("div", { class: "sub", text: `${savesText(t)}${t.screenshots ? ` \u00b7 ${nPlural(t.screenshots, "state screenshot", "state screenshots")} (not counted)` : ""} \u00b7 ${fmtBytes(t.bytes)}` }) : null,
      plan ? el("div", { class: "sub", id: "ov-saves-plan", text: will.length ? `A build with the current rules would ${will.join(", ")}.` : "A build with the current rules changes none of them." })
        : el("div", { class: "sub", text: "The list on this page shows what a build would do with them." }),
      t.sets && p ? el("div", { class: "sub", text: "Which game each belongs to is listed at the foot of the Files page." }) : null);
  }

  /** The RetroArch choice changed (or a config appeared): the saves columns, the saves option and the Saves card follow. */
  async function refreshSavesAwareness() {
    for (const k of Object.keys(state.library)) delete state.library[k];
    await refreshScan();
    await loadLibraryProfile();
  }

  /** "Scan first" explanation with a Scan button, shared by the Library and Browse tabs. */
  function scanGate(box, what) {
    const p = currentPlatform();
    const folder = folderOf(p).trim();
    const stale = p && p.last_scan;
    box.replaceChildren(
      el("p", {}, el("b", { text: "Scan first. " }),
        folder ? `${what} needs the scan results of ${p ? p.name : "this system"}.${stale ? " The results are not kept when the app restarts." : ""}`
          : `Choose the folder of ${p ? p.name : "this system"} in its Settings, then scan it.`),
      folder
        ? el("button", { class: "btn btn-primary", text: "Scan folder", "data-needs-idle": "", on: { click: () => startScan(false) } })
        : el("button", { class: "btn btn-primary", text: "Open Settings", on: { click: () => openTab("settings") } }));
    Jobs.setRunning(Jobs.running);
  }

  function renderLibraryGate() {
    const has = !!state.scan;
    $("lib-gate").classList.toggle("hidden", has);
    $("lib-body").classList.toggle("hidden", !has);
    if (!has) scanGate($("lib-gate"), "Building the library");
  }



  /** The Browse > Games rating column / chips only exist for systems the ratings cover (the server marks them). */
  function ratingsAvailable() {
    const info = state.library[state.platform];
    return !!(info && (info.available || {}).ratings);
  }

  /** A small 0-10 bar. The width is set through the style object: the page's CSP refuses inline style attributes. */
  function ratingBar(rating) {
    const fill = el("i");
    fill.style.width = `${Math.max(0, Math.min(10, Number(rating))) * 10}%`;
    return el("span", { class: "rbar", "aria-hidden": "true" }, fill);
  }

  /** "8.4 · 123 votes" or a dash. */
  function ratingCell(i) {
    if (i.rating === null || i.rating === undefined) return el("span", { class: "muted", text: "\u2014", title: "No rating found" });
    return el("span", { class: "rating-cell", title: i.rating_match ? `LaunchBox match: ${i.rating_match}` : "" },
      el("b", { text: Number(i.rating).toFixed(1) }), ratingBar(i.rating),
      el("span", { class: "muted small", text: ` \u00B7 ${fmt(i.votes)} vote${i.votes === 1 ? "" : "s"}` }));
  }

  /** Name tag chips plus the Dreamcast level chip in ONE chip row (the tag row alone for other systems). */
  function gameChips(tags, level) {
    const base = tagChips(tags);
    if (!level) return base;
    return el("div", { class: "tags" }, ...(base ? Array.from(base.childNodes) : []), levelChip(level));
  }

  /** A disc system's scan: remember how fast the CHDs were decoded (shown on the Disc images page). */
  function renderDcBar(s) {
    if (s && (s.engine_text || state.dcSpeed)) state.dcSpeed = s.engine_text || state.dcSpeed;
  }

  const first = (...vals) => vals.find((v) => v !== undefined && v !== null);

  // ------------------------------------------------ checksums (Browse tab)
  // The server sends what the scan already knows (DAT hashes, the hashes of the local file that matched,
  // per-track hashes of discs); "-" means "not known" - nothing is ever guessed or computed here.
  const SOURCE_NAME = { nointro: "No-Intro", tosec: "TOSEC", redump: "Redump", whdload: "WHDLoad" };
  const HASH_LABEL = { crc32: "CRC32", md5: "MD5", sha1: "SHA-1" };
  const HASH_KEYS = ["crc32", "md5", "sha1"];

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const area = el("textarea", { class: "copy-area", "aria-hidden": "true" });
      area.value = text;
      document.body.append(area);
      area.select();
      try { document.execCommand("copy"); } catch (__) { /* ignore */ }
      area.remove();
    }
    toast("Copied", "ok", 1500);
  }

  /** One hash: monospace, click to copy; a check mark when it equals the other side, a cross when it differs. */
  /** A name or a checksum you can copy with one press (to search for the file): ``label`` in front, the text, the copy mark. */
  function copyChip(text, label = "", cls = "") {
    return el("button", { class: `hash-btn copy-chip ${cls}`, type: "button", title: `${label || "Name"} - click to copy`, "aria-label": `Copy ${label || "name"} ${text}`,
      on: { click: (ev) => { ev.stopPropagation(); copyText(text); } } },
    label ? el("span", { class: "muted", text: `${label} ` }) : null, cls === "name" ? el("span", { text }) : el("code", { text }),
    el("span", { class: "hash-copy", text: "⧉", "aria-hidden": "true" }));
  }

  function hashCell(key, value, eq, whyUnknown, state_ = "") {
    if (!value) {
      return el("td", { class: "cs-hash" }, el("span", { class: "hash-none", text: "-", title: whyUnknown || "not known", "aria-label": `${HASH_LABEL[key]} not known` }));
    }
    const mark = eq === true ? el("span", { class: "hash-mark ok", text: "✓", title: "equal" })
      : eq === false ? el("span", { class: "hash-mark bad", text: "≠", title: "differs" }) : null;
    return el("td", { class: "cs-hash" }, el("button", {
      class: `hash-btn ${eq === true ? "eq" : eq === false ? "ne" : ""}`, type: "button",
      title: `${HASH_LABEL[key]}${state_ ? ` (${state_})` : ""} - click to copy`, "aria-label": `Copy ${HASH_LABEL[key]} ${value}`,
      on: { click: () => copyText(value) },
    }, mark, el("code", { text: value }), el("span", { class: "hash-copy", text: "⧉", "aria-hidden": "true" })));
  }

  const NO_MD5 = "MD5 is not computed while scanning";
  /** Why a local hash is unknown: depends on the hash and on whether the file is inside an archive. */
  function whyLocalUnknown(key, local) {
    if (key === "md5") return NO_MD5;
    if (local && local.archive) return "inside an archive only the CRC32 is stored - the other hashes are not known without extracting";
    return "not known";
  }

  /** A row of the checksum table. ``hashes``: {crc32, md5, sha1}; ``equal``: same keys, true / false / null. */
  function csRow(label, cls, nameNode, hashes, equal, why = () => "not known", state_ = "") {
    return el("tr", { class: `cs-row ${cls}` },
      el("th", { scope: "row", class: "cs-src", text: label }),
      el("td", { class: "cs-name" }, nameNode),
      ...HASH_KEYS.map((k) => hashCell(k, hashes && hashes[k], equal ? equal[k] : null, why(k), state_)));
  }

  function nameBlock(name, size, extra, dirOverride) {
    const [dir0, base] = dirOverride !== undefined ? [dirOverride, String(name || "")] : splitPath(String(name || ""));
    return el("div", {},
      base ? el("div", { class: "cs-file" }, copyChip(base, "", "name")) : el("div", { class: "cs-file", text: "(unnamed)" }),
      dir0 ? el("div", { class: "sub mono-path", text: dir0 }) : null,
      el("div", { class: "sub", text: size || size === 0 ? `${fmt(size)} bytes` : "size not known" }), extra || null);
  }

  /** DAT row(s) and local row(s) of a single-file payload (also used for every disk of a multi-disk set). */
  function fileRows(payload, dat, locals, labels) {
    const rows = [];
    for (const d of dat || []) rows.push(csRow(labels.dat, "cs-dat", nameBlock(d.name, d.size), d, null, () => "this DAT does not list it"));
    for (const l of locals || []) {
      const chips = [];
      if (l.archive) chips.push(el("span", { class: "tag", text: "inside an archive" }));
      if (l.via && l.via !== "raw") chips.push(el("span", { class: "tag tag-via", text: l.via === "headerless" ? "header skipped" : l.via === "byteswapped" ? "byte-swapped" : l.via }));
      const extra = chips.length ? el("div", { class: "tags" }, chips) : null;
      const nm = l.member ? nameBlock(l.member, l.size, extra, `in ${l.file.split("::")[0]}`) : nameBlock(l.file, l.size, extra);
      if (l.normalised) {
        rows.push(csRow(`${labels.local} (as stored)`, "cs-local differs", nm, l.raw, null, (k) => whyLocalUnknown(k, l)));
        rows.push(csRow(`${labels.local} (normalised)`, "cs-local", nameBlock(l.via_text || "normalised content", l.normalised.size, null, ""), l.normalised, l.equal, (k) => whyLocalUnknown(k, l), l.via));
      } else {
        rows.push(csRow(labels.local, "cs-local", nm, l.raw, l.equal || null, (k) => whyLocalUnknown(k, l)));
      }
    }
    return rows;
  }

  function csTable(rows) {
    const cols = el("colgroup", {}, el("col", { class: "c-src" }), el("col", { class: "c-file" }),
      el("col", { class: "c-crc" }), el("col", { class: "c-md5" }), el("col", { class: "c-sha" }));
    return el("div", { class: "table-wrap cs-wrap" }, el("table", { class: "cs-table" }, cols,
      el("thead", {}, el("tr", {}, el("th", { text: "" }), el("th", { text: "File" }), ...HASH_KEYS.map((k) => el("th", { text: HASH_LABEL[k] })))),
      el("tbody", {}, rows)));
  }

  /** Per-track DAT vs local hashes of a disc (CHD or raw set). */
  function discRows(payload, labels, matched) {
    const rows = [];
    for (const t of payload.tracks || []) {
      const lbl = el("tr", { class: "cs-track" }, el("td", { colspan: "5" },
        el("b", { text: `Track ${t.number}` }), ` ${t.type ? `· ${t.type} ` : ""}· ${fmt(t.size)} bytes`,
        t.state === "length" ? el("span", { class: "tag tag-status", text: "length only" }) : null,
        t.state === "header" ? el("span", { class: "tag", text: "from the CHD header" }) : null));
      rows.push(lbl);
      if (t.dat && t.dat.name) rows.push(csRow(labels.dat, "cs-dat", nameBlock(t.dat.name, t.dat.size), t.dat, null));
      if (!matched || !t.local) continue;
      if (t.state === "length") {
        rows.push(el("tr", { class: "cs-row cs-local" }, el("th", { scope: "row", class: "cs-src", text: labels.local }),
          el("td", { class: "cs-name" }, el("div", { class: "cs-file", text: "audio track: length matches" })),
          el("td", { colspan: "3", class: "muted" }, "length only - not decoded yet (the Verify tool hashes every track)")));
      } else {
        rows.push(csRow(labels.local, "cs-local", el("div", { class: "cs-file", text: payload.kind === "disc_unmatched" ? "track hashes" : "decoded track" }),
          t.local, t.equal, (k) => (k === "md5" && t.state === "header" ? "the CHD header holds only the SHA-1" : "not known"), t.state === "header" ? "from the CHD header, not decoded" : ""));
      }
    }
    return rows;
  }

  /** The expanded checksum area of one Browse row. */
  function checksumPanel(payload) {
    const src = SOURCE_NAME[payload.source] || "DAT";
    const labels = { dat: `DAT (${src})`, local: payload.kind === "unmatched" || payload.kind === "disc_unmatched" ? "Your file – no game match" : "Your file" };
    const box = el("div", { class: "cs" });
    const notes = [];
    if (payload.kind === "disc" || payload.kind === "disc_unmatched") {
      box.append(csTable(discRows(payload, labels, payload.kind !== "missing" && !!(payload.local && payload.local.length))));
      const f = (payload.local || [])[0];
      if (f && f.chd_sha1) {
        box.append(el("div", { class: "cs-note" }, `${f.kind === "raw" ? "Sheet" : "CHD"} ${f.file}: header SHA-1 `, el("code", { text: f.chd_sha1 })));
      }
      if (!(payload.local && payload.local.length)) box.append(el("div", { class: "cs-note muted", text: payload.kind === "disc_unmatched" ? "This disc matched nothing in the DAT; only the hashes read from it are shown." : "You do not have this disc - only the DAT checksums are shown." }));
    } else {
      const rows = fileRows(payload, payload.dat, payload.local, labels);
      if (!rows.length) rows.push(el("tr", {}, el("td", { colspan: "5", class: "muted", text: "No checksums available for this row." })));
      box.append(csTable(rows));
      if (payload.kind === "missing") notes.push("You do not have this one - only the DAT checksums are shown.");
      if (payload.bios) notes.push(`These checksums are those of a BIOS / firmware file: ${payload.bios}.`);
      if (payload.also_named) notes.push(`${payload.also_named} more DAT entr${payload.also_named === 1 ? "y has" : "ies have"} identical content.`);
      if (payload.more_files) notes.push(`${payload.more_files} more matching file(s) not listed.`);
      for (const l of payload.local || []) {
        if (l.via_text) notes.push(`${l.file}: matched after normalising - ${l.via_text}. The file itself is unchanged, so its own hashes differ from the DAT by design; the normalised hashes equal the DAT.`);
        if (l.archive) notes.push("Inside an archive only the CRC32 (and size) are stored, so MD5 and SHA-1 show \"-\" instead of a guess.");
      }
      if ((payload.local || []).some((l) => !l.archive)) notes.push(NO_MD5 + ", so MD5 shows \"-\" for your file.");
      if (payload.disks) {
        const d = payload.disks;
        box.append(el("h4", { class: "cs-sub", text: `Disks of this set: ${d.name} (${d.total} disks${d.complete ? "" : ", incomplete"})` }));
        const drows = [];
        for (const disk of d.disks) {
          drows.push(el("tr", { class: "cs-track" }, el("td", { colspan: "5" }, el("b", { text: `Disk ${disk.number}` }),
            disk.missing ? el("span", { class: "tag tag-bad", text: "missing" }) : null, disk.this ? el("span", { class: "tag", text: "this row" }) : null)));
          if (!disk.missing) drows.push(...fileRows(payload, [disk.dat], [disk.local], labels));
        }
        box.append(csTable(drows));
      }
    }
    if (notes.length) box.append(el("ul", { class: "cs-notes" }, Array.from(new Set(notes)).map((n) => el("li", { text: n }))));
    return box;
  }

  function fileCell(path) {
    const [dir, name] = splitPath(path);
    return el("div", {}, el("div", { text: name }), dir ? el("div", { class: "sub", text: dir }) : null);
  }


  /** The saves the emulator keeps for the system (they lie in its own folders, not in the ROM folder) and the game each belongs to. */
  let savesTable = null;
  function renderSavesBox() {
    const box = $("saves-box"), t = state.scan && state.scan.saves;
    box.classList.toggle("hidden", !(t && t.sets));
    savesTable = null;
    $("saves-table").replaceChildren();
    if (!(t && t.sets)) return;
    $("saves-box-title").textContent = `Saves in the emulator's folders (${nPlural(t.files, "file", "files")} for ${nPlural(t.sets, "game", "games")})`;
    if (box.open) showSavesTable();
  }

  function showSavesTable() {
    if (savesTable || !state.scan) return;
    const chips = $("saves-filter");
    const drawChips = () => chips.replaceChildren(...[["", "All saves"], ["1", "Games I have a ROM for"], ["0", "No ROM here"]].map(([v, text]) => el("button", {
      class: `chip chip-sm ${state.savesHave === v ? "active" : ""}`, type: "button", "data-value": v, text,
      on: { click: () => { if (state.savesHave !== v) { state.savesHave = v; drawChips(); savesTable.offset = 0; savesTable.load(); } } } })));
    state.savesHave = state.savesHave || "";
    drawChips();
    savesTable = new PagedTable($("saves-table"), {
      id: "browse-saves", placeholder: "Search titles or save names...", emptyText: "No save files found for this system.",
      sort: { get: () => state.kindSort.saves || "", set: (v) => { state.kindSort = { ...state.kindSort, saves: v }; } },
      fetch: ({ offset, limit, q }) => get(`/api/scan/results?${qs({ kind: "saves", offset, limit, q, have: state.savesHave || undefined, sort: state.kindSort.saves || "" })}`),
      columns: [
        {
          label: "Title", cls: "wrap", sortKey: "name", sortFirst: "asc", always: true, render: (i) => el("div", {},
        el("div", { class: "game-name", text: i.title }),
        i.game && i.game !== i.title ? el("div", { class: "sub", text: i.game }) : null,
        i.name !== i.title ? el("div", { class: "sub mono-path", text: i.name }) : null,
        i.guess ? el("div", { class: "sub" }, badge("info", "looks like"), ` ${i.guess}`) : null,
        ...(i.cards || []).map((c) => el("div", { class: "sub card-games" }, el("span", { class: "muted", text: `${c.label}: ` }),
          c.games.join("; ")))),
        },
        {
          label: "Matched to", cls: "wrap", render: (i) => (i.match === "rom" ? badge("ok", "ROM here")
        : i.match === "dat" ? badge("info", "title, no ROM here") : i.guess ? badge("info", "title only") : badge("skip", "unmatched")),
        },
        { label: "Saves", sortKey: "files", sortFirst: "desc", cls: "num", render: (i) => el("div", {}, el("div", { class: "saves-total", text: fmt(i.files) }),
          el("div", { class: "sub", text: savesText(i) + (i.screenshots ? ` \u00b7 ${nPlural(i.screenshots, "screenshot", "screenshots")}` : "") })) },
        { label: "From", render: (i) => (i.source && i.source !== "retroarch" ? i.label : i.cores.length ? i.cores.join(", ") : "-") },
      ],
    });
    savesTable.load();
  }

  // ----------------------------------------------- previews: the Recalculate affordance
  // Every "Preview ..." button follows one pattern: first press = calculate; afterwards the same button says
  // "Recalculate preview" (with a refresh icon), shows when / how many, and the preview is marked OUT OF DATE
  // (banner, dimmed, Recalculate highlighted, Build / Apply disabled) as soon as the rules, the options or the
  // folder change. States: none | busy | ready | stale | error.
  const REFRESH_SVG = '<svg viewBox="0 0 24 24" width="18" height="18" focusable="false" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.6-5.9M20 4v5h-5" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const clockText = (d) => `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  const sumCounts = (c) => Object.values(c || {}).reduce((a, b) => a + (Number(b) || 0), 0);

  const Previews = {
    items: {},
    HINT: "Recalculates with your current rules and folder contents",
    // id -> button / apply button / output box / noun / how to count / how to drop it
    CONFIG: {
      lib: { btn: "lib-plan-btn", apply: "lib-apply-btn", output: "lib-output", live: "lib-live", auto: true, noun: "files", count: (d) => (d.files !== undefined ? d.files : sumCounts(d.counts)), run: () => showLibraryPlan(), table: () => null, reset: () => resetLibraryPreview() },
    },

    /** Wire the buttons once: icon + label, a status line with the hint (aria-live) and the out-of-date banner. */
    bindAll() {
      for (const [id, cfg] of Object.entries(this.CONFIG)) {
        const btn = $(cfg.btn);
        const out = $(cfg.output);
        const first = btn.textContent.trim();
        const icon = el("span", { class: "pv-icon", "aria-hidden": "true" });
        const label = el("span", { class: "pv-label", text: first });
        btn.replaceChildren(icon, label);
        btn.classList.add("pv-btn");
        const status = el("span", { class: "pv-status", role: "status", "aria-live": "polite" });
        const hint = el("span", { class: "pv-hint hidden", text: this.HINT });
        const meta = el("div", { class: "pv-meta" }, status, hint);
        btn.parentElement.append(meta);
        const bannerText = el("span", { class: "pv-banner-text" });
        const banner = el("div", { class: "pv-banner hidden", role: "status" }, bannerText,
          el("button", { class: "btn btn-primary btn-small", type: "button", text: "Recalculate", "data-needs-idle": "",
            on: { click: () => this.click(id) } }));
        out.prepend(banner);
        if (cfg.auto) { btn.classList.add("hidden"); hint.classList.add("hidden"); }      // (it recalculates by itself: the button stays for the code only)
        if (cfg.live) $(cfg.live).addEventListener("click", () => { if (this.items[id].state === "error") this.click(id); });
        this.items[id] = { id, cfg, btn, icon, label, first, status, hint, banner, bannerText, out, apply: $(cfg.apply), live: cfg.live ? $(cfg.live) : null, timer: 0,
          state: "none", at: null, count: null, platform: null, scanId: null, startScan: null, force: false, dirty: "", why: "", err: "" };
        btn.addEventListener("click", () => this.click(id));
      }
    },

    has(id) { const it = this.items[id]; return !!it && it.state !== "none"; },

    /** The user pressed Preview / Recalculate (or the banner button): always asks the server for a fresh plan. */
    click(id) {
      const it = this.items[id];
      if (!it) return;
      if (it.state !== "none") it.force = true;
      const table = it.cfg.table();
      if (table) table.offset = 0;
      it.cfg.run();
    },

    busy(id) {
      const it = this.items[id];
      if (!it) return;
      it.state = "busy";
      it.dirty = "";
      it.err = "";
      it.platform = state.platform;
      it.startScan = state.scan ? state.scan.id : null;
      this.paint(it);
    },

    /** Wrap a PagedTable fetch: asks for a fresh plan when Recalculate was pressed, reports ready / error. */
    wrap(id, fn) {
      return async (args) => {
        const it = this.items[id];
        const refresh = it.force;
        it.force = false;
        const fresh = it.state !== "ready";       // paging / filtering a ready preview is not a new calculation
        try {
          const data = await fn({ ...args, refresh: refresh || undefined });
          this.ready(id, data, fresh || refresh);
          return data;
        } catch (err) {
          this.fail(id, err);
          throw err;
        }
      };
    },

    ready(id, data, fresh = true) {
      const it = this.items[id];
      if (!it) return;
      if (fresh) {
        it.at = new Date();
        it.count = it.cfg.count(data || {});
        it.platform = state.platform;
        it.scanId = state.scan ? state.scan.id : null;
        // something changed while it was being calculated: it is already out of date
        const changed = it.dirty || (it.startScan !== null && it.startScan !== it.scanId ? "The folder was scanned again since this preview" : "");
        it.state = changed ? "stale" : "ready";
        it.why = changed;
        it.dirty = "";
      } else if (it.state === "stale" || it.state === "error" || it.state === "busy") {
        it.state = it.dirty ? "stale" : "ready";
      }
      it.err = "";
      this.paint(it);
      if (it.state === "stale") this.autoSchedule(id);          // changed again while it was calculated
    },

    fail(id, err) {
      const it = this.items[id];
      if (!it) return;
      it.state = "error";
      it.err = (err && err.message) || "failed";
      it.pendingRatings = !!(err && err.code === "ratings_pending");   // runs again by itself when the data arrives
      this.paint(it);
    },

    /** The ratings data finished installing: previews that were waiting for it run again. */
    retryPending() {
      for (const [id, it] of Object.entries(this.items)) {
        if (it.state === "error" && it.pendingRatings) { it.pendingRatings = false; this.click(id); }
      }
    },

    /** The preview no longer matches the rules / options / folder: keep it on screen, flagged. */
    stale(id, why) {
      const it = this.items[id];
      if (!it) return;
      if (it.state === "busy") { it.dirty = why; return; }
      if (it.state === "error" && it.cfg.auto) { this.autoSchedule(id); return; }       // what failed may work with the new rules: try again
      if (it.state !== "ready" && it.state !== "stale") return;
      it.state = "stale";
      it.why = why;
      this.paint(it);
      this.autoSchedule(id);
    },

    /** An automatic preview recalculates by itself shortly after the last change (a quick run of changes makes one calculation). */
    autoSchedule(id) {
      const it = this.items[id];
      if (!it || !it.cfg.auto) return;
      clearTimeout(it.timer);
      it.timer = setTimeout(() => this.autoRun(id), 500);
    },

    autoRun(id) {
      const it = this.items[id];
      if (!it || it.state === "busy" || it.state === "ready") return;
      if (!inSystem() || state.tab !== "files" || !state.scan) return;
      if (it.state === "stale" && it.platform !== state.platform) return;
      this.click(id);
    },

    /** The library page was opened (or a scan arrived): a missing or out-of-date preview is calculated now. */
    resume(id) {
      const it = this.items[id];
      if (it && state.scan && (it.state === "none" || it.state === "stale")) this.autoSchedule(id);
    },

    staleAll(why, ids = Object.keys(this.items)) { for (const id of ids) this.stale(id, why); },

    clear(id) {
      const it = this.items[id];
      if (!it) return;
      it.state = "none";
      it.dirty = "";
      it.at = null;
      this.paint(it);
    },

    /** After every scan: a preview of the scanned system stays (out of date), any other goes. */
    onScan() {
      for (const [id, it] of Object.entries(this.items)) {
        if (it.state === "none") continue;
        if (!state.scan || it.platform !== state.platform) { it.cfg.reset(); this.clear(id); continue; }
        const sid = state.scan.id;
        if (it.state === "busy") { if (it.startScan !== sid) it.dirty = "The folder was scanned again since this preview"; continue; }
        if (it.scanId !== sid) this.stale(id, "The folder was scanned again since this preview");
      }
    },

    paint(it) {
      const busy = it.state === "busy", stale = it.state === "stale", err = it.state === "error";
      const has = it.state !== "none";
      it.btn.dataset.busy = busy ? "1" : "0";
      it.btn.classList.toggle("btn-primary", stale || err);
      it.btn.classList.toggle("pv-attn", stale);
      it.btn.classList.toggle("pv-busy", busy);
      it.btn.setAttribute("aria-busy", busy ? "true" : "false");
      it.icon.className = `pv-icon${busy ? " pv-spin" : ""}`;
      it.icon.innerHTML = !busy && has ? REFRESH_SVG : "";
      it.label.textContent = busy ? "Calculating..." : err ? "Try again" : has ? "Recalculate preview" : it.first;
      it.btn.title = has && !busy ? this.HINT : "";
      let text = "";
      if (busy) text = "Calculating...";
      else if (err) text = `Could not calculate: ${it.err}`;
      else if (has && it.at) {
        const n = it.count === null || it.count === undefined ? "" : ` · ${fmt(it.count)} ${it.count === 1 ? it.cfg.noun.replace(/s$/, "") : it.cfg.noun}`;
        text = `${stale ? "Out of date - calculated" : "Calculated"} ${clockText(it.at)}${n}`;
      }
      it.status.textContent = text;
      it.status.className = `pv-status ${it.state}`;
      it.hint.classList.toggle("hidden", !has || busy || it.cfg.auto);
      it.banner.classList.toggle("hidden", !stale || it.cfg.auto);
      if (it.live) {
        const working = busy || stale;
        it.live.textContent = working ? (it.at ? "Updating the list..." : "Calculating the list...")
          : err ? `Could not update the list: ${it.err} - press here to try again` : has && it.at ? `The list is up to date (${clockText(it.at)}${it.count === null || it.count === undefined ? "" : `, ${fmt(it.count)} ${it.cfg.noun}`})` : "";
        it.live.className = `live ${working ? "busy" : err ? "bad" : has ? "ok" : ""}`;
      }
      it.bannerText.textContent = stale ? `${it.why || "Rules changed since this preview"} — recalculate to see the current result.` : "";
      // while an out-of-date preview is being recalculated (or that failed) it stays dimmed and Build / Apply stay disabled
      const lock = stale || ((busy || err) && it.at !== null);
      it.out.classList.toggle("pv-stale", lock);
      it.out.setAttribute("aria-busy", busy ? "true" : "false");
      if (it.apply) {
        it.apply.dataset.stale = lock ? "1" : "0";
        it.apply.title = lock ? "The list is being updated - building waits for it" : "";
      }
      Jobs.setRunning(Jobs.running);
    },
  };

  // ------------------------------------------- Library page: totals and tiles (Amendment 17)
  // "With your library rules: have N of M games": M = games the rules keep for the WHOLE DAT, N = those the user has.
  // The server computes it in the background (GET /api/library/totals); this block polls while it calculates.
  const Totals = { seq: 0, timer: null, data: {}, shown: {}, upgrades: {}, upgradesOpen: {} };
  const scheduleTotals = debounce(() => loadTotals(), 600);

  async function loadTotals() {
    const p = currentPlatform();
    if (!p || !inSystem()) return;
    const name = p.name;
    const seq = ++Totals.seq;
    clearTimeout(Totals.timer);
    let data;
    try {
      data = await get(`/api/library/totals?${qs({ platform: name })}`);
    } catch (err) {
      data = { available: true, error: err.message, calculating: false };
    }
    if (seq !== Totals.seq) return;
    Totals.data[name] = data;
    renderTotals();
    renderRatingsStatus();
    if (!data.calculating && !data.ratings_pending) { loadInsights(); loadList(name); }
    if (data.ratings_pending) Updates.load();          // the server asked for the data: show its progress
    if (data.calculating || data.ratings_pending) Totals.timer = setTimeout(loadTotals, data.ratings_pending ? 1500 : 900);
  }

  function totalsLine(cls, text) { return el("div", { class: `totals-line ${cls}`, text }); }

  /** The lists behind the hint lines of the totals: ``upgrades`` (owned, but not the preferred version: with the checksums of the preferred
   *  one), ``excluded`` (owned, left out by your rules) and ``incomplete`` (multi-disk games with disks missing). One is open at a time. */
  async function toggleList(name, kind) {
    const open = Totals.upgradesOpen[name] === kind;
    Totals.upgradesOpen[name] = open ? "" : kind;
    if (!open) Totals.upgrades[name] = null;
    renderTotals();
    if (!open) loadList(name);
  }

  /** Read the open list again: when it is opened, and whenever the totals it belongs to were calculated again (other rules, a scan). */
  async function loadList(name) {
    const kind = Totals.upgradesOpen[name];
    if (!kind) return;
    clearTimeout(Totals.listTimer);
    let doc;
    try {
      doc = await get(`/api/library/upgrades?${qs({ platform: name, kind })}`);
    } catch (err) {
      doc = { ready: true, items: [], error: err.message };
    }
    if (Totals.upgradesOpen[name] !== kind) return;           // closed, or another list opened, meanwhile
    if (!doc.ready) { Totals.listTimer = setTimeout(() => loadList(name), 900); if (Totals.upgrades[name]) return; }
    Totals.upgrades[name] = doc;
    renderTotals();
  }

  function upgradesTable(doc, kind = "upgrades") {
    if (!doc) return el("div", { class: "totals-wait" }, el("span", { class: "spinner", "aria-hidden": "true" }), " looking for them...");
    if (doc.error) return totalsLine("error-text", `Could not list them: ${doc.error}`);
    if (!doc.ready) return totalsLine("muted", "The numbers are still being calculated - try again in a moment.");
    if (!doc.items.length) return totalsLine("muted", "None.");
    const have = (it) => el("td", {}, ...it.have.map((n) => el("div", { class: "small", text: n })));
    if (kind !== "upgrades") {
      const rows = doc.items.map((it) => el("tr", {}, el("td", { text: it.title }), have(it), el("td", { class: "small", text: it.why || "" })));
      return el("div", { class: "upgrades table-wrap", "data-upgrades": "list" },
        el("table", {}, el("thead", {}, el("tr", {}, el("th", { text: "Game" }), el("th", { text: "You have" }),
          el("th", { text: kind === "excluded" ? "Left out because" : "What is missing" }))), el("tbody", {}, ...rows)));
    }
    const hash = (label, v) => v ? el("div", {}, copyChip(v, label)) : null;
    const version = (b) => el("div", {}, el("div", {}, copyChip(b.name, "", "name")),
      ...b.files.flatMap((f) => (f.crc || f.md5 || f.sha1)
        ? [b.files.length > 1 ? el("div", {}, copyChip(f.name, "", "name"), el("span", { class: "small muted", text: ` ${fmtBytes(f.size)}` })) : null,
          hash("CRC32", f.crc), hash("MD5", f.md5), hash("SHA-1", f.sha1)]
        : [el("div", { class: "small muted", text: "No checksum: this catalogue identifies the game by its ID." })]));
    const rows = doc.items.map((it) => el("tr", {}, el("td", { text: it.title }), have(it), el("td", {}, ...it.better.map(version))));
    return el("div", { class: "upgrades table-wrap", "data-upgrades": "list" },
      el("table", {}, el("thead", {}, el("tr", {}, el("th", { text: "Game" }), el("th", { text: "You have" }), el("th", { text: "Preferred version (press a name or a checksum to copy it)" }))),
        el("tbody", {}, ...rows)));
  }

  // ---- The tiles at the top of a system page (Settings > Tiles chooses and orders them; ``tiles.CATALOG`` lists them).
  // Each renderer gets what the page already has and returns {tone, value, unit, bar, sub, links, wait} - or null to leave the tile out.
  const Insights = { data: {}, key: {}, timer: null };

  /** The numbers of the tiles that need a calculation of their own: asked for only when one of them is shown. */
  async function loadInsights() {
    const p = currentPlatform();
    if (!p || !inSystem()) return;
    const want = (state.tiles || []).filter((t) => CALC_TILES.has(t));
    if (!want.length) return;
    const name = p.name;
    let doc;
    try { doc = await get(`/api/library/insights?${qs({ platform: name, want: want.join(",") })}`); } catch (_) { doc = { ready: true }; }
    Insights.data[name] = doc;
    clearTimeout(Insights.timer);
    if (!doc.ready) Insights.timer = setTimeout(loadInsights, 1500);
    renderTotals();
  }
  const CALC_TILES = new Set(["space", "best_missing", "avg_rating", "regions", "convert"]);

  const tileWait = (text) => ({ tone: "plain", wait: text });
  const pctOf = (a, b) => (b ? (100 * a) / b : 0);

  const TILES = {
    have_all: { label: "Have, all entries", render: (c) => {
      if (c.scan) {
        const { games, total, have, missing, pct } = summaryNumbers(c.scan.summary);
        return { tone: "ok", value: fmt(have), unit: `of ${fmt(total)} ${games ? "games" : "ROMs"}`, bar: pct, sub: `${pctText(pct)}, missing ${fmt(missing)}` };
      }
      const rec = c.p.last_scan;
      if (rec) return { tone: "ok", value: fmt(rec.have), unit: `of ${fmt(rec.total)}`, bar: rec.pct || 0, sub: `${pctText(rec.pct || 0)}, last scan ${scanTime(rec)}` };
      return { tone: "ok", value: "-", sub: "Not scanned yet" };
    } },
    have_rules: { label: "Have, with your rules", render: (c) => {
      const d = c.data;
      if (!d) return tileWait("calculating...");
      if (d.available === false) return { tone: "after", value: "-", sub: "The database of this system is not installed yet" };
      if (d.ratings_pending) return tileWait("waiting for the ratings data");
      if (d.error) return { tone: "after", value: "-", sub: `Could not calculate: ${d.error}` };
      if (d.target_games === null || d.target_games === undefined) return tileWait(d.calculating ? "calculating..." : "");
      const m = d.target_games, n = d.have_games;
      const stale = d.calculating ? (d.stale ? "recalculating..." : "calculating...") : "";
      if (n === null || n === undefined) return { tone: "after", value: fmt(m), unit: "games in the library", sub: "Scan to see how many you have", stale };
      const pct = d.percent !== null && d.percent !== undefined ? d.percent : pctOf(n, m);
      return { tone: "after", value: fmt(n), unit: `of ${fmt(m)} games`, bar: pct, sub: `${pctText(pct)}, missing ${fmt(m - n)}`, stale };
    } },
    rated_all: { label: "Rated, all entries", render: (c) => {
      const r = c.data && c.data.rated_all;
      if (!r || !r.games) return null;
      const pct = pctOf(r.rated, r.games);
      return { tone: "ok", value: pctText(pct), unit: `${fmt(r.rated)} of ${fmt(r.games)} rated`, bar: pct, sub: "enough votes to count" };
    } },
    rated_rules: { label: "Rated, with your rules", render: (c) => {
      const r = c.data && !c.data.ratings_pending && !c.data.error ? c.data.rated_target : null;
      if (!r || !r.games) return null;
      const pct = pctOf(r.rated, r.games);
      return { tone: "after", value: pctText(pct), unit: `${fmt(r.rated)} of ${fmt(r.games)} rated`, bar: pct, sub: "of the games your rules keep" };
    } },
    needs_look: { label: "Needs a look", render: (c) => {
      const d = c.data;
      if (!d || d.have_games === null || d.have_games === undefined) return null;
      const items = [["upgrades", d.not_preferred, "not the preferred version"], ["excluded", d.owned_but_excluded, "left out by your rules"],
        ["incomplete", d.owned_incomplete, "incomplete"]].filter(([, n]) => n);
      const total = items.reduce((a, [, n]) => a + n, 0);
      if (!total) return { tone: "ok", value: "0", sub: "everything you own fits your rules" };
      return { tone: "warn", value: fmt(total), unit: total === 1 ? "game" : "games",
        links: items.map(([kind, n, text]) => ({ kind, text: `${fmt(n)} ${text}` })) };
    } },
    files_to_change: { label: "Files to change", render: (c) => {
      const plan = c.plan;
      if (!plan || !c.scan) return null;
      const r = plan.reasons || {};
      const moves = (plan.counts && (plan.counts.move || 0) + (plan.counts.rename || 0) + (plan.counts.delete || 0)) || 0;
      const parts = [[r.excluded, "excluded"], [r.superseded, "superseded"], [r.duplicates, "duplicates"], [r.incomplete, "incomplete"],
        [r.saves, "saves"], [r.unmatched, "unmatched"], [(r.renamed || 0) + (r.moved || 0), "renamed or moved"]].filter(([n]) => n);
      return { tone: "plain", value: fmt(moves), unit: moves === 1 ? "file" : "files",
        sub: parts.length ? parts.slice(0, 4).map(([n, t]) => `${fmt(n)} ${t}`).join(", ") : "nothing to do" };
    } },
    unmatched: { label: "Unmatched files", render: (c) => {
      const n = c.scan && c.scan.summary.unmatched_files;
      return n ? { tone: "warn", value: fmt(n), unit: n === 1 ? "file" : "files", sub: "match nothing in the database", href: Route.build(slugOf(c.p), "files", { show: "unmatched" }) } : null;
    } },
    bios: { label: "BIOS and firmware", render: (c) => {
      const n = c.scan && c.scan.summary.bios_files;
      return n ? { tone: "info", value: fmt(n), unit: n === 1 ? "file" : "files", sub: "found among your files" } : null;
    } },
    saves: { label: "Saves", render: (c) => {
      const s = c.scan && c.scan.saves;
      return s && s.files ? { tone: "plain", value: fmt(s.files), unit: s.files === 1 ? "save or state" : "saves and states", sub: "found for this system" } : null;
    } },
    database: { label: "Database", render: (c) => {
      const ds = datStatus(c.p);
      const first = (c.p.dats || []).find((d) => d.version);
      return { tone: ds.kind === "missing" ? "warn" : "plain", value: first ? first.version : "-", sub: ds.text };
    } },
    last_scan: { label: "Last scan", render: (c) => {
      const rec = c.p.last_scan;
      if (!rec) return { tone: "plain", value: "never", sub: "press Scan folder" };
      const stale = rec.folder && c.p.folder && rec.folder !== c.p.folder;
      return { tone: "plain", value: scanTime(rec), sub: stale ? "the folder has changed since" : `${fmt(rec.have)} of ${fmt(rec.total)} found` };
    } },
    space: { label: "Space", render: (c) => {
      const i = c.insights && c.insights.space;
      if (!i) return c.insights && c.insights.ready ? null : tileWait("calculating...");
      if (i.have === null || i.have === undefined) return { tone: "plain", value: fmtBytes(i.target), sub: "the library your rules describe" };
      return { tone: "plain", value: fmtBytes(i.have), unit: `of ${fmtBytes(i.target)}`, bar: pctOf(i.have, i.target), sub: "your games against the whole library (database sizes)" };
    } },
    best_missing: { label: "Best you are missing", render: (c) => {
      if (!c.insights || !c.insights.ready) return tileWait("calculating...");
      const b = c.insights.best_missing;
      return b ? { tone: "info", value: b.title, small: true, sub: `rated ${b.rating.toFixed(1)} (${fmt(b.votes)} votes), not in your folder` } : null;
    } },
    avg_rating: { label: "Average rating", render: (c) => {
      const a = c.insights && c.insights.avg_rating;
      if (!a) return c.insights && c.insights.ready ? null : tileWait("calculating...");
      if (a.owned === null && a.target === null) return null;
      return { tone: "plain", value: a.owned !== null ? a.owned.toFixed(1) : "-", unit: "of 10", sub: a.target !== null ? `the whole library: ${a.target.toFixed(1)}` : "" };
    } },
    regions: { label: "Regions", render: (c) => {
      const r = c.insights && c.insights.regions;
      if (!r) return c.insights && c.insights.ready ? null : tileWait("calculating...");
      if (!r.length) return null;
      return { tone: "plain", value: r[0].region, unit: `${r[0].pct}%`, sub: r.slice(1, 4).map((x) => `${x.region} ${x.pct}%`).join(", ") };
    } },
    convert: { label: "Convert", render: (c) => {
      const v = c.insights && c.insights.convert;
      return v && v.games ? { tone: "plain", value: fmt(v.games), unit: v.games === 1 ? "game" : "games", sub: "could be converted to CHD" } : null;
    } },
  };

  function tileNode(id, spec) {
    const body = [];
    if (spec.wait !== undefined && spec.value === undefined) {
      body.push(el("div", { class: "totals-wait" }, el("span", { class: "spinner", "aria-hidden": "true" }), ` ${spec.wait}`));
    } else {
      body.push(el("div", { class: `tile-value${spec.small ? " tile-small" : ""}` }, el("span", { class: "value", text: spec.value }),
        spec.unit ? el("span", { class: "muted", text: ` ${spec.unit}` }) : null));
      if (spec.bar !== undefined && spec.bar !== null) body.push(progressBar(spec.bar));
      if (spec.sub) body.push(el("div", { class: "tile-sub muted", text: spec.sub }));
      if (spec.stale) body.push(el("div", { class: "totals-wait" }, el("span", { class: "spinner", "aria-hidden": "true" }), ` ${spec.stale}`));
      for (const l of spec.links || []) {
        body.push(el("button", { class: `linklike tile-link${Totals.upgradesOpen[currentPlatform().name] === l.kind ? " on" : ""}`, type: "button", "data-upgrades": l.kind,
          text: l.text, title: "Show the games", on: { click: () => toggleList(currentPlatform().name, l.kind) } }));
      }
    }
    const tag = spec.href ? "a" : "section";
    return el(tag, { class: `tile tile-${spec.tone || "plain"}`, "data-tile": id, ...(spec.href ? { href: spec.href } : {}) },
      el("h3", { text: TILES[id].label }), ...body);
  }

  function renderTotals() {
    const p = currentPlatform();
    const box = $("sys-tiles");
    if (!p) { box.replaceChildren(); return; }
    renderRulesLine(p);
    const name = p.name;
    const ctx = { p, scan: state.scan, data: Totals.data[name] || null, plan: libPlan && state.scan ? libPlan : null, insights: Insights.data[name] || null };
    const key = JSON.stringify([name, (state.tiles || []).join(), state.scan && state.scan.id, p.last_scan && p.last_scan.at, ctx.data, ctx.insights,
      ctx.plan && [ctx.plan.reasons, ctx.plan.counts], Totals.upgradesOpen[name] || "", (p.dats || []).map((d) => d.version)]);
    if (key === Totals.shown[""]) { renderTileList(p); return; }
    Totals.shown[""] = key;
    const nodes = [];
    for (const id of state.tiles || []) {
      if (!TILES[id]) continue;
      const spec = TILES[id].render(ctx);
      if (spec) nodes.push(tileNode(id, spec));
    }
    box.replaceChildren(...nodes);
    box.classList.toggle("hidden", !nodes.length);
    box.classList.toggle("tile-stale", !!(ctx.data && ctx.data.stale));
    renderTileList(p);
  }

  /** The list behind a "Needs a look" link, under the tiles. */
  function renderTileList(p) {
    let kind = Totals.upgradesOpen[p.name];
    const d = Totals.data[p.name];
    if (kind && d && !d.calculating && { upgrades: d.not_preferred, excluded: d.owned_but_excluded, incomplete: d.owned_incomplete }[kind] === 0) {
      kind = Totals.upgradesOpen[p.name] = "";              // the rules changed and nothing is left to list
    }
    $("tile-lists").replaceChildren(...(kind ? [upgradesTable(Totals.upgrades[p.name], kind)] : []));
  }

  /** "Library rules: your defaults" or "Custom: region priority" with what the rules do, in the bar above the list. */
  function renderRulesLine(p) {
    const mine = [...new Set((p.rule_overrides || []).map(ruleName))];
    const sum = $("library-rules-summary").textContent;
    $("totals-rules-line").replaceChildren(...[
      mine.length ? el("span", { class: "nav-override", text: "Custom", title: "Rules this system overrides" }) : null,
      mine.length ? el("b", { text: ` ${mine.join(", ")}` }) : el("b", { text: "Your default rules" }),
      mine.length ? el("span", { class: "muted", text: " (the rest follow your defaults)" }) : null,
      " ", el("a", { class: "small", href: Route.build(slugOf(p), "settings"), text: "Change" }),
      sum ? el("div", { class: "muted small", text: sum }) : null].filter(Boolean));
  }

  // ---------------------------------------------------- 3. Build library
  // The reserved folders: all direct children of the platform folder (the layout sketch lists them).
  const reserved = (hasM3u, convertible, protectedDirs = [], archived = false) => [
    ...(archived ? [] : [
      [UNMATCHED, "only files that matched nothing"],
      [EXCLUDED, "bad dumps, betas, demos, language/flag exclusions (library rules)"],
      [SUPERSEDED, "older versions / worse variants"],
      ...(hasM3u ? [[INCOMPLETE, "multi-disk games with missing disks"]] : []),
      [DUPLICATES, "extra copies of the same ROM"]]),
    ...(convertible ? [[CONVERTED, "originals kept by Convert"]] : []),
    ...protectedDirs.map((d) => [d, "your files - left alone by every step"]),
  ].map(([name, text]) => `  ${(name + "/").padEnd(23)}${text}`);
  const REASON_LABEL = { kept: "Kept", bios: "BIOS / firmware", excluded: "Excluded", superseded: "Superseded", incomplete: "Incomplete",
    duplicate: "Duplicates", saves: "Saves", unmatched: "Unmatched", playlist: "Playlists" };
  const REASON_ORDER = ["kept", "bios", "excluded", "superseded", "incomplete", "duplicate", "saves", "unmatched", "playlist"];
  const REASON_BADGE = { kept: "ok", bios: "info", excluded: "bad", superseded: "info", incomplete: "warn", duplicate: "warn",
    saves: "info", unmatched: "skip", playlist: "move" };
  let vanishTable = null;
  let vanishPlanId = "";         // the plan the vanish list on screen belongs to
  let libPlan = null;

  const libOptions = () => ({
    labels: $("lib-labels").checked, savedisk: $("lib-savedisk").checked,
  });

  // "Where to build": in the scanned folder (moves) or in another folder (the source is only read)
  const exportActive = () => $("lib-where-other").checked && !!$("lib-export-dest").value.trim();
  const exportOptions = () => (exportActive()
    ? { export_to: $("lib-export-dest").value.trim(), export_mode: $("lib-export-mode").value, export_sidecars: $("lib-export-sidecars").checked, export_sync: $("lib-export-sync").checked }
    : {});
  // "Keep this folder tidy": a build in place moves what it sets aside to another folder
  const asideActive = () => !$("lib-where-other").checked && $("lib-aside-on").checked && !!$("lib-aside-dir").value.trim();
  const asideOptions = () => (asideActive() ? { aside_to: $("lib-aside-dir").value.trim() } : {});
  const buildOptions = () => ({ ...libOptions(), ...exportOptions(), ...asideOptions() });
  /** The archive folder for every system: the one set in Settings (there is no default: with none, nothing is archived). */
  const archiveBase = () => (state.status && state.status.archive && state.status.archive.dir) || "";
  /** The archive folder of one system: its own when it has one, else the base. */
  const archiveFor = (p) => (p && state.status && state.status.archive && state.status.archive.overrides[p.name]) || archiveBase();
  /** Archiving exists for a system only when an archive folder is set (for every system, or for it alone). */
  const archiveAvailable = (p) => !!archiveFor(p);
  const archiveOn = () => !$("lib-where-other").checked && $("lib-aside-on").checked && archiveAvailable(currentPlatform());
  /** Show the archive options, or the note that there is no archive folder. */
  function renderAsideVisibility() {
    const avail = archiveAvailable(currentPlatform()), other = $("lib-where-other").checked;
    $("lib-aside-opts").classList.toggle("hidden", other || !avail);
    $("lib-aside-row").classList.toggle("hidden", other || !avail || !$("lib-aside-on").checked);
    $("lib-aside-none").classList.toggle("hidden", other || avail);
  }
  /** Put the archive folder of the open system into its field. */
  function syncAsideDir() {
    const p = currentPlatform();
    if (!p) return;
    $("lib-aside-dir").value = archiveFor(p);
    renderAsideVisibility();
    renderAsideWhere();
    renderLibraryLayout();
  }
  /** The field was edited: a folder other than the base is this system's own, the base itself clears it. */
  async function archiveFieldChanged() {
    const p = currentPlatform();
    if (!p) return;
    const typed = $("lib-aside-dir").value.trim();
    const own = typed && typed !== archiveBase() ? typed : "";
    try {
      state.status.archive = await post("/api/settings/archive", { platform: p.name, dir: own });
    } catch (err) { toast(err.message, "error"); }
    if (Previews.has("lib")) resetLibraryPreview();       // (before the field shows the new folder: nothing is waited for after it)
    syncAsideDir();
  }
  /** Says where the archived files of this system will go, as a list: <archive folder>/<this system's folder name>/... */
  function renderAsideWhere() {
    const box = $("lib-aside-where");
    const p = currentPlatform();
    const dir = $("lib-aside-dir").value.trim().replace(/[\\/]+$/, "");
    const folder = String(folderOf(p) || "").replace(/[\\/]+$/, "");
    const show = archiveOn() && !!dir && !!folder;
    box.classList.toggle("hidden", !show);
    if (!show) return;
    const sep = dir.includes("\\") && !dir.includes("/") ? "\\" : "/";
    const own = `${dir}${sep}${folder.split(/[\\/]/).pop()}${sep}`;
    const hasM3u = !!(p.m3u_dats && p.m3u_dats.length);
    const mine = !!(state.status.archive && state.status.archive.overrides[p.name]);
    box.replaceChildren(
      el("div", { class: "archive-source" }, mine ? "This system's own archive folder. " : "The archive folder for every system (Settings). ",
        mine ? el("button", { class: "btn btn-small btn-ghost", id: "lib-aside-reset", text: "Use the common folder",
          on: { click: () => { $("lib-aside-dir").value = archiveBase(); archiveFieldChanged(); } } }) : null),
      el("div", { class: "archive-head" }, "Files the rules leave out go to ", el("code", { text: own }), ":"),
      el("ul", { class: "archive-list" }, [
        [EXCLUDED, "bad dumps, betas, demos, language and flag exclusions"],
        [SUPERSEDED, "older versions and worse variants"],
        ...(hasM3u ? [[INCOMPLETE, "multi-disk games with missing disks"]] : []),
        [DUPLICATES, "extra copies of the same ROM"],
        [UNMATCHED, "files that match no database"],
      ].map(([name, why]) => el("li", {}, el("code", { text: name }), el("span", { class: "muted", text: ` ${why}` })))));
  }
  function asideChanged() {
    renderAsideVisibility();
    if ($("lib-aside-on").checked) syncAsideDir();
    renderAsideWhere();
    renderLibraryLayout();
    saveExportSettings();
    if (Previews.has("lib")) resetLibraryPreview();
  }
  const saveExportSettings = debounce(async () => {
    try {
      await post("/api/library/export/settings", { enabled: $("lib-where-other").checked, dest: $("lib-export-dest").value.trim(),
        mode: $("lib-export-mode").value, sidecars: $("lib-export-sidecars").checked, sync: $("lib-export-sync").checked,
        aside: $("lib-aside-on").checked });
    } catch (_) { /* a convenience only */ }
  }, 300);
  function exportSettingsChanged() {
    renderAsideWhere();
    renderAsideVisibility();
    if ($("lib-export-mode").value === "move") $("lib-export-sync").checked = false;
    $("lib-export-sync").disabled = $("lib-export-mode").value === "move";
    $("lib-export-opts").classList.toggle("hidden", !$("lib-where-other").checked);
    $("lib-apply-btn").textContent = $("lib-where-other").checked ? "Build library in destination" : "Build library";
    $("lib-undo-btn").textContent = $("lib-where-other").checked ? "Undo last build" : "Undo last";
    saveExportSettings();
    if (Previews.has("lib")) resetLibraryPreview();
    refreshUndo();
    updateActionState();
  }
  function loadExportSettings() {
    const e = (state.status && state.status.library_export) || {};
    $("lib-where-other").checked = !!e.enabled;
    $("lib-where-here").checked = !e.enabled;
    $("lib-export-dest").value = e.dest || "";
    $("lib-export-mode").value = e.mode || "copy";
    $("lib-export-sidecars").checked = !!e.sidecars;
    $("lib-aside-on").checked = e.aside !== false;
    syncAsideDir();
    renderAsideVisibility();
    $("lib-export-sync").checked = !!e.sync && e.mode !== "move";
    $("lib-export-sync").disabled = e.mode === "move";
    $("lib-export-opts").classList.toggle("hidden", !e.enabled);
    $("lib-apply-btn").textContent = e.enabled ? "Build library in destination" : "Build library";
    $("lib-undo-btn").textContent = e.enabled ? "Undo last build" : "Undo last";
  }

  async function loadLibraryProfile() {
    const name = state.platform;
    if (!name) return;
    const hadRatings = ratingsAvailable();
    try {
      state.library[name] = await get(`/api/library/profile?${qs({ platform: name })}`);
    } catch (err) {
      state.library[name] = null;
      $("library-rules").replaceChildren(el("div", { class: "muted", text: `Library rules are not available: ${err.message}` }));
      return;
    }
    if (name !== state.platform) return;
    renderLibraryRules();
    if (!hadRatings && ratingsAvailable() && inSystem() && state.tab === "files") Files.reload();      // (the rating filter and the ratings shown)
  }

  // Everything below is rendered from the server's rule catalog (library.rule_catalog / profile_info): no rule list lives here.
  const GROUP_TITLE = { exclude: "Exclude", keep_flag: "Keep these dump types" };
  const catalogOf = (info) => info.catalog || [];
  const optionEntries = (info) => catalogOf(info).filter((e) => e.kind === "option" && e.id !== "languages" && e.id !== "region_priority"
    && !e.group && (info.available || {})[e.id] !== false);
  const ratingEntries = (info) => ((info.available || {}).ratings ? catalogOf(info).filter((e) => e.group === "ratings") : []);
  /** A rating filter is set: one of the group's ``filter`` entries (minimum rating / top N) has a value. */
  const ratingActive = (info) => !!info && ratingEntries(info).some((e) => e.filter && info.profile[e.field] !== null && info.profile[e.field] !== undefined);

  /** Human label of an exclusion reason code ("bad_dump", "flag_cr", "language") from the catalog. */
  function reasonLabel(code) {
    const info = state.library[state.platform];
    const entries = info ? catalogOf(info) : [];
    if (code === "language") return "Language";
    if (info && info.reason_labels && info.reason_labels[code]) { const t = info.reason_labels[code]; return t.charAt(0).toUpperCase() + t.slice(1); }
    if (code.startsWith("flag_")) {
      const f = entries.find((e) => e.kind === "keep_flag" && e.id === code.slice(5));
      return f ? `${plainLabel(f)} off` : code;
    }
    const r = entries.find((e) => e.kind === "exclude" && e.id === code);
    return r ? plainLabel(r) : (RULE_SHORT[code] || code);
  }

  const langName = (info, code) => ((info.language_names || {})[code]) || code;

  /** Remember a profile the server returned (for platform ``name``) and tell the rest of the UI. */
  function adoptProfile(name, info) {
    state.library[name] = info;
    const own = state.platforms.find((x) => x.name === name);
    if (own && info.inherit) {
      const before = (own.rule_overrides || []).join();
      own.rule_overrides = info.inherit.overridden;
      if (before !== own.rule_overrides.join()) renderHome();           // the marker in the list
    }
    if (name !== state.platform) return;
    const p = currentPlatform();
    if (p) { p.library = info.profile; p.latest_only = !!p.library.latest_only; }
    profileChanged();
  }

  async function saveProfile(changes) {
    const name = state.platform;
    const before = state.library[name];
    try {
      adoptProfile(name, await post("/api/library/profile", { platform: name, ...changes }));
    } catch (err) {
      toast(`Could not save the library rules: ${err.message}`, "error");
      state.library[name] = await get(`/api/library/profile?${qs({ platform: name })}`).catch(() => before);
    }
    renderLibraryRules();
  }

  // ---- The rules panel is drawn twice from the same catalog: on a system's Library tab (each rule follows your defaults until it
  // is overridden there) and on the Settings page (your defaults themselves). ``rctx`` is the panel being drawn or used.
  /** After your defaults changed: every system's own answer is out of date, and so are the previews. */
  function defaultsChanged() {
    for (const k of Object.keys(state.library)) delete state.library[k];
    Previews.staleAll("Your library defaults changed since this preview", ["lib"]);
    state.libStats = null;
    scheduleTotals();
    loadPlatforms().then(() => { if (inSystem()) loadLibraryProfile(); });
  }

  async function saveDefaults(changes) {
    try {
      state.libDefaults = await post("/api/library/defaults", changes);
      defaultsChanged();
    } catch (err) {
      toast(`Could not save your defaults: ${err.message}`, "error");
      state.libDefaults = await get("/api/library/defaults").catch(() => state.libDefaults);
    }
    renderDefaultsRules();
  }

  const platformRules = {
    key: () => state.platform, overrides: true, host: () => $("library-rules"),
    info: (key = state.platform) => state.library[key],
    post: (body, key = state.platform) => post("/api/library/profile", { platform: key, ...body }),
    adopt: (info, key = state.platform) => adoptProfile(key, info),
    save: saveProfile, rerender: () => renderLibraryRules(),
  };
  const defaultsRules = {
    key: () => "*", overrides: false, host: () => $("set-library-rules"),
    info: () => state.libDefaults,
    post: (body) => post("/api/library/defaults", body),
    adopt: (info) => { state.libDefaults = info; defaultsChanged(); },
    save: saveDefaults, rerender: () => renderDefaultsRules(),
  };
  let rctx = platformRules;
  const rulesCtx = (key) => (key === "*" ? defaultsRules : platformRules);

  const RULE_NAMES = { exclude: "Exclusions", latest_only: "Latest version only", best_variant: "Best variant", complete_only: "Complete games only",
    languages: "Languages", keep_flags: "Dump types", rescue_only_dump: "Keep the only dump", region_priority: "Region priority", region_ties: "Region priority",
    one_per_game: "One per game", borrow_other_editions: "Borrow other editions", keep_other_language: "Keep other languages",
    min_rating: "Minimum rating", top_n: "Top N", min_votes: "Minimum votes", keep_unrated: "Keep unrated games", rank_scope: "Ranking",
    saved_games: "Saves", keep_bios: "BIOS and firmware", keep_extras: "Files with a game" };
  const ruleName = (field) => RULE_NAMES[field] || field;

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
    const set = (list) => rctx.save({ languages: list });
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
    return el("div", { class: "rules-group", id: rctx.overrides ? "rules-languages" : null },
      el("div", { class: "rules-head", text: "Languages" }),
      el("div", { class: "sub note", text: info.style === "whdload"
        ? ((catalogOf(info).find((e) => e.id === "languages") || {}).description || "No language tag means English.")
        : "No language or country tag means English/neutral (TOSEC convention); (de-en) counts as English; [tr en] counts as an English translation." }),
      known.length ? el("div", { class: "lang-grid" }, boxes) : el("div", { class: "muted small", text: "Languages appear here once the DAT is installed." }),
      order,
      el("div", { class: "sub", text: prof.languages.length ? "Titles with no version in a ticked language are left out." : "Nothing ticked: every language stays." }),
      el("div", { class: "rule-count vanish-note", "data-vanish": "language" }));
  }

  // --------------------------------------------------------------------------------------------------
  // Reorderable list (used by the region priority). Mouse + touch drag through Pointer Events on a handle
  // (touch-action: none on the handle only, so the rest of the row still scrolls the list), keyboard pick-up
  // (Space/Enter, arrows, Space/Enter to drop, Escape to cancel), and Top / up / down buttons as a fallback.
  // ``items`` is the FULL order; a filter only hides rows. ``onCommit(order, before)`` runs once per completed move.
  function sortableList({ items, label, noun = "item", prioCount = 0, prioLabel = "Prioritised", restLabel = "Everything else", ties = null, checks = null, maxChecks = Infinity, labels = null, descs = null, onLimit = null, onCommit }) {
    let order = [...items];
    let on = new Set(checks || []);       // (checks only when ``checks`` is given) the rows that are ticked: their order is the order that counts
    let tied = new Set(ties || []);       // (ties only when ``ties`` is given) rows that rank the same as the row above them
    const rows = new Map();
    let filter = "";
    let grab = null;            // keyboard pick-up: { name, before }
    let drag = null;            // pointer drag: { id, name, row, ghost, before, offY, x, y, raf }
    let current = order[0] || "";   // the row that is in the tab order (roving tabindex)
    let flip = false;

    const live = el("div", { class: "sr-only", role: "status", "aria-live": "polite" });
    const announce = (msg) => { flip = !flip; live.textContent = msg + (flip ? " " : ""); };
    const list = el("ol", { class: "sortable-list", "aria-label": label });
    const divider = el("li", { class: "sortable-divider", role: "presentation", "aria-hidden": "true" },
      el("span", { text: `▲ ${prioLabel}` }), el("span", { text: `${restLabel} ▼` }));
    const count = el("span", { class: "sortable-count muted small" });
    const empty = el("li", { class: "sortable-empty muted hidden", role: "presentation", text: `No ${noun} matches.` });   // inside the list: no layout shift
    const input = el("input", { type: "search", class: "input sortable-filter", placeholder: `Find a ${noun}…`, "aria-label": `Find a ${noun}`,
      autocomplete: "off", spellcheck: "false" });
    const hint = el("div", { class: "sub sortable-hint",
      text: "Drag the ⋮⋮ handle, or focus a row and press Space, use the arrow keys, then Space to drop (Escape cancels). “Top” moves it to the first place." });
    const root = el("div", { class: "sortable" },
      el("div", { class: "sortable-bar" }, input, count), hint, list, live);

    const visible = (name) => !rows.get(name).hidden;
    const visibleNames = () => order.filter(visible);

    const labelOf = (n) => (labels && labels[n]) || n;

    function buildRow(name) {
      const act = (how) => () => commit(name, how);
      const btn = (cls, text, title, aria, how) => el("button", { type: "button", class: `btn btn-icon ${cls}`, text, title, "aria-label": aria,
        on: { click: act(how) } });
      const row = el("li", { class: "sortable-row", "data-name": name, tabindex: "-1" },
        el("span", { class: "drag-handle", "data-drag-handle": "", "aria-hidden": "true", title: `Drag to reorder ${name}`, text: "⋮⋮" }),
        ...(checks ? [el("input", { type: "checkbox", class: "row-check", "aria-label": `Show ${labelOf(name)}`, checked: on.has(name), on: { change: (ev) => toggleCheck(name, ev.target) } })] : []),
        el("span", { class: "region-n" }),
        el("span", { class: "region-name", title: descs && descs[name] || null }, labelOf(name), descs && descs[name] ? el("span", { class: "row-desc muted small", text: descs[name] }) : null),
        ...(ties ? [el("button", { type: "button", class: "btn btn-icon row-tie", text: "=", title: `${name} is as good as the region above it (both count as best)`,
          "aria-label": `${name} is as good as the region above it`, "aria-pressed": "false", on: { click: () => toggleTie(name) } })] : []),
        btn("row-top", "Top", "Move to top", `Move ${name} to top`, "top"),
        btn("row-up", "▲", "Prefer more", `Move ${name} up`, "up"),
        btn("row-down", "▼", "Prefer less", `Move ${name} down`, "down"));
      return row;
    }

    /** Decorations that depend on the order: numbers, priority highlight, divider, disabled buttons, tab stops. */
    function refresh() {
      const vis = visibleNames();
      if (!visible(current) && vis.length) current = vis[0];
      let tier = 0;
      order.forEach((name, i) => {
        const row = rows.get(name);
        if (i > 0 && !(ties && tied.has(name))) tier++;
        row.querySelector(".region-n").textContent = checks ? (on.has(name) ? String([...order].filter((x) => on.has(x)).indexOf(name) + 1) : "") : String(ties ? tier + 1 : i + 1);
        if (checks) row.classList.toggle("prio", on.has(name));
        if (ties) {
          const tie = row.querySelector(".row-tie");
          tie.disabled = i === 0;
          tie.setAttribute("aria-pressed", String(i > 0 && tied.has(name)));
          row.classList.toggle("tied", i > 0 && tied.has(name));
        }
        row.classList.toggle("prio", i < prioCount);
        row.setAttribute("aria-label", `${name}, position ${i + 1} of ${order.length}`);
        const vi = vis.indexOf(name);
        const set = (cls, off) => { row.querySelector(cls).disabled = off; };
        set(".row-top", i === 0);
        set(".row-up", vi <= 0);
        set(".row-down", vi < 0 || vi === vis.length - 1);
        row.tabIndex = name === current ? 0 : -1;
        for (const b of row.querySelectorAll("button")) b.tabIndex = name === current ? 0 : -1;
      });
      // the divider sits after the prioCount-th row (hidden while a filter is active: rows are not contiguous then)
      const rowEls = order.map((n) => rows.get(n));
      if (prioCount > 0 && prioCount < rowEls.length && !filter) {
        rowEls[prioCount - 1].after(divider);
        divider.hidden = false;
      } else {
        divider.hidden = true;
      }
      const shown = vis.length;
      count.textContent = filter ? `${shown} of ${order.length} shown` : `${order.length} ${noun}s`;
      empty.classList.toggle("hidden", shown > 0);
    }

    /** Put the DOM rows in ``next`` order (re-focusing whatever had focus) and refresh the decorations. */
    function applyOrder(next) {
      const active = document.activeElement;
      order = [...next];
      order.forEach((name, i) => {
        const row = rows.get(name);
        const want = list.querySelectorAll(":scope > .sortable-row")[i];
        if (want !== row) list.insertBefore(row, want || null);
      });
      refresh();
      if (active && active !== document.body && list.contains(active) && document.activeElement !== active) {
        const row = active.closest(".sortable-row");
        (active.disabled && row ? row : active).focus({ preventScroll: true });
      }
    }

    /** ``name`` placed before ``beforeName`` (null: after the last visible row; hidden rows keep their places). */
    function placed(name, beforeName) {
      const out = order.filter((x) => x !== name);
      if (beforeName) { out.splice(out.indexOf(beforeName), 0, name); return out; }
      let last = -1;
      out.forEach((x, i) => { if (visible(x)) last = i; });
      out.splice(last + 1, 0, name);
      return out;
    }

    function reveal(row) {
      const lr = list.getBoundingClientRect(), r = row.getBoundingClientRect();
      const top = lr.top + list.clientTop, bottom = top + list.clientHeight;      // inside the border
      if (r.top < top) list.scrollTop -= top - r.top;
      else if (r.bottom > bottom) list.scrollTop += r.bottom - bottom;
    }

    function flash(name) {
      const row = rows.get(name);
      row.classList.remove("flash");
      void row.offsetWidth;
      row.classList.add("flash");
      setTimeout(() => row.classList.remove("flash"), 1400);
    }

    const sameOrder = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);

    /** One finished move: the new order is already shown; tell the owner once. */
    function finish(before, name, how) {
      if (sameOrder(before, order)) return;
      const pos = order.indexOf(name) + 1;
      announce(`${name} ${how} position ${pos} of ${order.length}.`);
      flash(name);
      onCommit([...order], before, checks ? [...on] : [...tied]);
    }

    /** A row's box ticked or cleared (at most ``maxChecks`` ticked). */
    function toggleCheck(name, box) {
      if (box.checked && on.size >= maxChecks) { box.checked = false; if (onLimit) onLimit(); return; }
      if (box.checked) on.add(name); else on.delete(name);
      refresh();
      announce(`${labelOf(name)} ${on.has(name) ? "is shown" : "is not shown"}.`);
      onCommit([...order], [...order], [...on]);
    }

    /** "Same as above" on or off for one row. */
    function toggleTie(name) {
      if (order.indexOf(name) < 1) return;
      if (tied.has(name)) tied.delete(name); else tied.add(name);
      refresh();
      announce(tied.has(name) ? `${name} now ranks the same as the region above it.` : `${name} now ranks after the region above it.`);
      onCommit([...order], [...order], [...tied]);
    }

    /** The up / down / top buttons. */
    function commit(name, how) {
      const before = [...order];
      const vis = visibleNames(), vi = vis.indexOf(name);
      if (how === "top") applyOrder([name, ...order.filter((x) => x !== name)]);
      else if (how === "up" && vi > 0) applyOrder(placed(name, vis[vi - 1]));
      else if (how === "down" && vi >= 0 && vi < vis.length - 1) applyOrder(placed(name, vis[vi + 2] || null));
      reveal(rows.get(name));
      finish(before, name, how === "top" ? "moved to the top, now" : "moved to");
    }

    // ---- keyboard pick-up
    function cancelGrab(restore = true) {
      if (!grab) return;
      const g = grab;
      grab = null;
      rows.get(g.name).classList.remove("grabbed");
      if (restore) { applyOrder(g.before); announce(`Move cancelled. ${g.name} is back at position ${order.indexOf(g.name) + 1}.`); }
    }
    function dropGrab() {
      const g = grab;
      if (!g) return;
      grab = null;
      rows.get(g.name).classList.remove("grabbed");
      if (sameOrder(g.before, order)) announce(`${g.name} dropped, unchanged.`);
      else finish(g.before, g.name, "dropped at");
    }
    function stepGrab(name, kind) {
      const vis = visibleNames(), vi = vis.indexOf(name);
      let next = null;
      if (kind === "up" && vi > 0) next = placed(name, vis[vi - 1]);
      else if (kind === "down" && vi < vis.length - 1) next = placed(name, vis[vi + 2] || null);
      else if (kind === "home" && vi > 0) next = placed(name, vis[0]);
      else if (kind === "end" && vi < vis.length - 1) next = placed(name, null);
      if (!next) return;
      applyOrder(next);
      const row = rows.get(name);
      row.focus({ preventScroll: true });
      reveal(row);
      announce(`${name}, position ${order.indexOf(name) + 1} of ${order.length}.`);
    }
    function focusRow(name) {
      if (!name) return;
      current = name;
      refresh();
      const row = rows.get(name);
      row.focus({ preventScroll: true });
      reveal(row);
    }
    list.addEventListener("focusin", (e) => {
      const row = e.target.closest && e.target.closest(".sortable-row");
      if (row && row.dataset.name !== current && !drag) { current = row.dataset.name; refresh(); }
    });
    list.addEventListener("keydown", (e) => {
      const row = e.target.closest && e.target.closest(".sortable-row");
      if (!row || e.target !== row || drag || e.altKey || e.ctrlKey || e.metaKey) return;
      const name = row.dataset.name;
      const key = e.key;
      const vis = visibleNames(), vi = vis.indexOf(name);
      if (grab && grab.name === name) {
        if (key === "ArrowUp" || key === "ArrowDown") stepGrab(name, key === "ArrowUp" ? "up" : "down");
        else if (key === "Home" || key === "End") stepGrab(name, key.toLowerCase());
        else if (key === " " || key === "Enter") dropGrab();
        else if (key === "Escape") { cancelGrab(); e.stopPropagation(); }
        else if (key === "Tab") { dropGrab(); return; }
        else return;
        e.preventDefault();
        return;
      }
      if (key === " " || key === "Enter") {
        grab = { name, before: [...order] };
        row.classList.add("grabbed");
        announce(`${name} picked up, position ${order.indexOf(name) + 1} of ${order.length}. Use the arrow keys to move, Space to drop, Escape to cancel.`);
      } else if (key === "ArrowUp") focusRow(vis[Math.max(0, vi - 1)]);
      else if (key === "ArrowDown") focusRow(vis[Math.min(vis.length - 1, vi + 1)]);
      else if (key === "Home") focusRow(vis[0]);
      else if (key === "End") focusRow(vis[vis.length - 1]);
      else return;
      e.preventDefault();
    });
    list.addEventListener("focusout", (e) => {
      // focus moved elsewhere on the page: a held row is dropped where it is
      if (grab && e.target === rows.get(grab.name) && e.relatedTarget && !list.contains(e.relatedTarget)) dropGrab();
    });

    // ---- pointer drag (mouse and touch)
    function dropTarget(y) {
      for (const name of visibleNames()) {
        if (name === drag.name) continue;
        const r = rows.get(name).getBoundingClientRect();
        if (y < r.top + r.height / 2) return name;
      }
      return null;
    }
    function dragUpdate() {
      if (!drag) return;
      const lr = list.getBoundingClientRect();
      const top = Math.min(Math.max(drag.y - drag.offY, lr.top - drag.h / 2), lr.bottom - drag.h / 2);
      drag.ghost.style.top = `${top}px`;
      const target = dropTarget(drag.y);
      const next = placed(drag.name, target);
      if (!sameOrder(next, order)) applyOrder(next);
      // the ghost shows the position it would get if dropped now
      const pos = order.indexOf(drag.name);
      drag.ghost.querySelector(".region-n").textContent = String(pos + 1);
      drag.ghost.classList.toggle("prio", pos < prioCount);
    }
    function dragTick() {
      if (!drag) return;
      const lr = list.getBoundingClientRect();
      const zone = Math.min(64, lr.height / 4);
      let dy = 0;
      if (drag.y < lr.top + zone) dy = -Math.ceil(Math.min(1, (lr.top + zone - drag.y) / zone) * 18);
      else if (drag.y > lr.bottom - zone) dy = Math.ceil(Math.min(1, (drag.y - (lr.bottom - zone)) / zone) * 18);
      if (dy) {
        const was = list.scrollTop;
        list.scrollTop += dy;
        if (list.scrollTop !== was) dragUpdate();
      }
      drag.raf = requestAnimationFrame(dragTick);
    }
    function dragEnd(commitIt) {
      const d = drag;
      if (!d) return;
      drag = null;
      cancelAnimationFrame(d.raf);
      window.removeEventListener("pointermove", onPointerMove, true);
      window.removeEventListener("pointerup", onPointerUp, true);
      window.removeEventListener("pointercancel", onPointerCancel, true);
      window.removeEventListener("keydown", onDragKey, true);
      try { list.releasePointerCapture(d.id); } catch (err) { /* already released */ }
      d.ghost.remove();
      d.row.classList.remove("dragging");
      list.classList.remove("is-dragging");
      if (commitIt) finish(d.before, d.name, "dropped at");
      else { applyOrder(d.before); announce(`Move cancelled. ${d.name} is back at position ${order.indexOf(d.name) + 1}.`); }
    }
    function onPointerMove(e) {
      if (!drag || e.pointerId !== drag.id) return;
      e.preventDefault();
      drag.x = e.clientX; drag.y = e.clientY;
      dragUpdate();
    }
    function onPointerUp(e) { if (drag && e.pointerId === drag.id) dragEnd(true); }
    function onPointerCancel(e) { if (drag && e.pointerId === drag.id) dragEnd(false); }
    function onDragKey(e) { if (drag && e.key === "Escape") { e.preventDefault(); e.stopPropagation(); dragEnd(false); } }

    list.addEventListener("pointerdown", (e) => {
      const handle = e.target.closest && e.target.closest("[data-drag-handle]");
      if (!handle || drag || (e.pointerType === "mouse" && e.button !== 0) || e.isPrimary === false) return;
      const row = handle.closest(".sortable-row");
      const name = row.dataset.name;
      e.preventDefault();
      cancelGrab(false);
      const rect = row.getBoundingClientRect();
      const ghost = row.cloneNode(true);
      ghost.classList.add("drag-ghost");
      for (const attr of ["tabindex", "data-name", "aria-label"]) ghost.removeAttribute(attr);
      ghost.setAttribute("aria-hidden", "true");
      ghost.style.cssText = `left:${rect.left}px;top:${rect.top}px;width:${rect.width}px;height:${rect.height}px`;
      document.body.append(ghost);
      row.classList.add("dragging");
      list.classList.add("is-dragging");
      try { list.setPointerCapture(e.pointerId); } catch (err) { /* the window listeners still see it */ }
      drag = { id: e.pointerId, name, row, ghost, before: [...order], offY: e.clientY - rect.top, h: rect.height, x: e.clientX, y: e.clientY, raf: 0 };
      window.addEventListener("pointermove", onPointerMove, true);
      window.addEventListener("pointerup", onPointerUp, true);
      window.addEventListener("pointercancel", onPointerCancel, true);
      window.addEventListener("keydown", onDragKey, true);
      announce(`Dragging ${name}.`);
      drag.raf = requestAnimationFrame(dragTick);
    });
    list.addEventListener("contextmenu", (e) => { if (e.target.closest && e.target.closest("[data-drag-handle]")) e.preventDefault(); });

    // ---- filter
    function applyFilter() {
      filter = input.value.trim().toLowerCase();
      for (const name of order) {
        const row = rows.get(name);
        const hit = !filter || labelOf(name).toLowerCase().includes(filter);
        row.hidden = !hit;
        row.classList.toggle("match", !!filter && hit);
      }
      refresh();
      list.scrollTop = 0;
    }
    input.addEventListener("input", applyFilter);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === "ArrowDown") { e.preventDefault(); focusRow(visibleNames()[0]); }
      else if (e.key === "Escape" && input.value) { input.value = ""; applyFilter(); e.stopPropagation(); }
    });

    function build() {
      rows.clear();
      list.replaceChildren(...order.map((n) => { const r = buildRow(n); rows.set(n, r); return r; }), divider, empty);
      refresh();
    }
    build();

    return {
      root, list, input,
      /** Replace the whole list (also the rollback after a failed save). */
      setItems(next, nextTies) {
        cancelGrab(false);
        if (drag) dragEnd(false);
        if (ties && nextTies) { tied = new Set(nextTies); refresh(); }
        if (checks && nextTies) {
          on = new Set(nextTies);
          for (const [n, row] of rows) row.querySelector(".row-check").checked = on.has(n);
          refresh();
        }
        if (sameOrder(next, order)) return;
        if (sameOrder([...next].sort(), [...order].sort())) applyOrder(next);
        else { order = [...next]; build(); applyFilter(); }
      },
      order: () => [...order],
    };
  }

  // ---- Region priority (No-Intro / Redump systems): one debounced save per completed move, optimistic with rollback.
  const regionSave = { timer: 0, pending: {}, confirmed: {}, busy: false, ui: null, uiPlatform: "" };

  async function flushRegionSave() {
    if (regionSave.busy) return;
    const name = Object.keys(regionSave.pending)[0];
    if (!name) return;
    const { order, ties } = regionSave.pending[name];
    delete regionSave.pending[name];
    regionSave.busy = true;
    try {
      const ctx = rulesCtx(name);
      const resp = await ctx.post({ region_priority: order, region_ties: ties }, name);
      regionSave.confirmed[name] = { order: [...(resp.regions || order)], ties: [...((resp.profile || {}).region_ties || ties)] };
      const newer = regionSave.pending[name];
      if (newer) { resp.regions = [...newer.order]; resp.profile = { ...resp.profile, region_ties: [...newer.ties] }; }           // a later move is already shown: keep it on screen
      ctx.adopt(resp, name);
      if (name === state.platform && ctx.overrides) renderRulesSummary(resp);
    } catch (err) {
      toast(`Could not save the region priority: ${err.message}`, "error");
      delete regionSave.pending[name];
      const info = rulesCtx(name).info(name);
      const back = regionSave.confirmed[name];
      if (info && back) {
        info.regions = [...back.order];
        info.profile = { ...info.profile, region_ties: [...back.ties] };
        if ((name === "*" || name === state.platform) && regionSave.ui && regionSave.uiPlatform === name) regionSave.ui.setItems(back.order, back.ties);
        if (name === state.platform) renderRulesSummary(info);
      }
    } finally {
      regionSave.busy = false;
      if (Object.keys(regionSave.pending).length) flushRegionSave();
    }
  }

  const regionState = (info) => ({ order: [...(info.regions || [])], ties: [...((info.profile || {}).region_ties || [])] });

  function commitRegionOrder(name, order, ties) {
    const info = rulesCtx(name).info(name);
    if (!info) return;
    if (!regionSave.confirmed[name]) regionSave.confirmed[name] = regionState(info);
    info.regions = [...order];                          // optimistic
    info.profile = { ...info.profile, region_ties: [...ties] };
    regionSave.pending[name] = { order: [...order], ties: [...ties] };
    if (name !== "*") renderRulesSummary(info);
    clearTimeout(regionSave.timer);
    regionSave.timer = setTimeout(flushRegionSave, 250);
  }

  function regionSection(info) {
    const name = rctx.key();
    if (!regionSave.pending[name] && !regionSave.busy) regionSave.confirmed[name] = regionState(info);
    const oldList = rctx.host().querySelector(".rules-region .sortable-list");
    const keepScroll = oldList ? oldList.scrollTop : 0;
    const ui = sortableList({
      items: info.regions || [], label: "Region priority, best region first", noun: "region",
      ties: (info.profile || {}).region_ties || [],
      onCommit: (order, before, ties) => commitRegionOrder(name, order, ties),
    });
    regionSave.ui = ui;
    regionSave.uiPlatform = name;
    const section = el("div", { class: "rules-group rules-region", id: rctx.overrides ? "rules-regions" : null },
      el("div", { class: "rules-head", text: "Region priority" }),
      el("div", { class: "sub note", text: `${(catalogOf(info).find((e) => e.id === "region_priority") || {}).description || "Best region first."} The whole list counts, in this order; regions you have not moved stay in alphabetical order after the ones you placed. Press = on a region to make it as good as the one above: with Europe and USA both first, a version from either is a best match.` }),
      ui.root);
    requestAnimationFrame(() => { ui.list.scrollTop = keepScroll; });
    return section;
  }

  /** The one-line summary of the rules panel and its "Default / Custom rules" note. */
  function renderRulesSummary(info) {
    const prof = info.profile, avail = info.available || {};
    const cat = catalogOf(info);
    const excl = cat.filter((e) => e.kind === "exclude");
    const flags = cat.filter((e) => e.kind === "keep_flag");
    const opts = optionEntries(info);
    const on = excl.filter((r) => prof.exclude.includes(r.id)).length;
    const bits = [];
    if (excl.length) bits.push(`${on} of ${excl.length} exclusions`);
    if (avail.languages) bits.push(prof.languages.length ? prof.languages.map((c) => langName(info, c)).join(", ") : "all languages");
    if (info.ranking_short) bits.push(info.ranking_short);
    else if (avail.region_priority && (info.regions || []).length) {
      const ties = new Set((prof.region_ties || []));
      const top = [info.regions[0]];
      for (const r of info.regions.slice(1)) { if (ties.has(r)) top.push(r); else break; }
      bits.push(top.length > 1 ? `${top.join(" = ")} first` : `${info.regions.slice(0, 2).join(" > ")} first`);
    }
    if (flags.length && avail.keep_flags !== false) {
      const off = flags.filter((f) => !prof.keep_flags.includes(f.id)).map((f) => plainLabel(f).toLowerCase());
      if (off.length) bits.push(`no ${off.join(", ")}`);
    }
    for (const e of opts) if (prof[e.field]) bits.push(plainLabel(e).toLowerCase());
    const rated = ratingActive(info);
    for (const e of ratingEntries(info)) {
      const v = prof[e.field];
      if (!e.summary || v === null || v === undefined || v === false || (e.summary_when_filter && !rated)) continue;
      bits.push(e.summary.replace("{v}", String(v)));
    }
    if (rated && prof.rank_scope === "owned" && prof.top_n) bits.push("ranked among your games");
    if (prof.saved_games && prof.saved_games !== "keep") bits.push(prof.saved_games === "archive" ? "saves archived with their games" : prof.saved_games === "carry" || prof.saved_games === "convert" ? "saves copied to the better version" : "saves left alone");
    $("library-rules-summary").textContent = bits.join(" \u00B7 ");
    const p = currentPlatform();
    if (p && inSystem()) renderRulesLine(p);
  }

  // ---- What happens to the saves of a game a rule replaces or archives (Amendment 31): one group for the Library rules and
  // (it exists only when a save source is set up for the system: the profile then has ``saved_games``).
  const SAVED_CHOICES = [
    { value: "keep", label: "Keep both ROMs",
      text: "The ROM the rules would replace or archive stays as well, so the saves still have their game. Its saves are renamed if the ROM is renamed." },
    { value: "archive", label: "Archive the saves with the ROM",
      text: "The ROM is replaced or archived as the rules say, and its saves and save states move to the archive with it (into a _saves folder). Undo brings them back. Not while RetroArch is running." },
    { value: "leave", label: "Leave the saves where they are",
      text: "The ROM is replaced or archived as the rules say; its saves are not touched." },
    { value: "carry", label: "Copy the save to the better version",
      text: "When a better version of a game you have saves for arrives, the old ROM is replaced and its save games (memory cards, battery saves; not save states) are copied to the new ROM's name, if the new ROM has no save yet. Where the save would not work in the new version (a PlayStation, GameCube or Dreamcast save of another region) both ROMs stay. The old save is left where it is. Undo removes the copy." },
    { value: "convert", label: "Copy the save, converting its region (PlayStation, PlayStation 2)",
      text: "As above, and a PlayStation save is rewritten for the other region's disc, so a USA save works in the European release and the other way round (the card's file name for the save and its checksum are changed; nothing else). Both discs must be readable (a .chd or .iso), and the save may still not work in a game that keeps region-specific data. Experimental: the old save is never touched, and Undo removes the converted copy. A PlayStation 2 save is moved to the other release's game code the same way: on a folder card (PCSX2) the save's folder is copied under the new name; in a card image (.ps2) the names are rewritten in place (a copy of the image is kept for Undo). Not while PCSX2 is running. Other systems copy as above." },
  ];

  /** The group of the rules panels: what happens to a game the rules would archive when RetroArch has saves for it, and the
   *  switch that renames saves with their ROMs (the same stored setting as on the RetroArch page). */
  function savesGroup(current, onChange, where) {
    const choice = SAVED_CHOICES.find((c) => c.value === current) || SAVED_CHOICES[0];
    const select = el("select", { class: "input select", id: `${where}-saved-games`, "data-field": "saved_games", "aria-label": "When a rule replaces or archives a game you have saves for",
      on: { change: (ev) => onChange(ev.target.value) } },
    ...SAVED_CHOICES.map((c) => el("option", { value: c.value, text: c.label, selected: c.value === choice.value })));
    const follow = el("input", { type: "checkbox", id: `${where}-saves-follow`, "data-saves-follow": "1", checked: state.raFollow !== false,
      on: { change: async (ev) => {
        const box = ev.target;
        try { const i = await post("/api/retroarch/follow", { follow: box.checked }); state.raFollow = i.follow; } catch (err) { box.checked = !box.checked; toast(err.message, "error"); }
        syncFollowBoxes();
      } } });
    if (state.raFollow === undefined) {
      state.raFollow = true;
      get("/api/retroarch").then((i) => { state.raFollow = i.follow; syncFollowBoxes(); }).catch(() => {});
    }
    const found = state.scan && state.scan.saves ? state.scan.saves : null;
    const renames = !found || found.renames !== false;                  // (only RetroArch's saves are named after the ROM)
    const names = found && found.sources && found.sources.length ? found.sources.join(", ") : "RetroArch";
    return el("div", { class: "rules-group", id: `${where}-saves-group` },
      el("div", { class: "rules-head", text: `Saves (${names})` }),
      el("label", { class: "rating-label" }, el("span", { class: "rule-label", text: "When a rule replaces or archives a game you have saves for: " }), select),
      el("div", { class: "sub note", id: `${where}-saved-games-note`, text: choice.text }),
      renames ? el("label", { class: "check rule-check", title: "The same setting as on the RetroArch page" }, follow,
        el("span", { class: "rule-text" }, el("span", { class: "rule-line" }, el("span", { class: "rule-label", text: "Rename saves with their ROMs" })),
          el("span", { class: "sub", text: "When a build gives a ROM its database name, its saves and save states get the new name too (a build in another folder copies them). Each core's folder stays separate; nothing is overwritten." }))) : null,
      el("div", { class: "sub note", text: "This is the default for every game; on the Library tab you can choose differently for a single game. Building into another folder archives nothing here, so saves are left alone there; the setting still decides which games are copied." }));
  }

  function syncFollowBoxes() {
    for (const b of document.querySelectorAll("input[data-saves-follow]")) b.checked = state.raFollow !== false;
    const page = document.getElementById("ra-follow");
    if (page) page.checked = state.raFollow !== false;
  }

  /** The preview's saves cards: from the plan (counted before the build, nothing is looked at afterwards). */
  function savesCards(s, apply) {
    if (!s || !s.found) return [];
    const out = [];
    if (s.kept) out.push(card(fmt(s.kept), "Kept: you have saves for them", "ok"));
    if (s.follow && s.rename && (s.rename.files || s.rename.conflicts)) {
      const verb = s.elsewhere ? (apply ? "copied" : "to copy") : (apply ? "renamed" : "to rename");
      out.push(card(fmt(s.rename.files), `Save files ${verb} with their ROMs (${fmt(s.rename.games)} game${s.rename.games === 1 ? "" : "s"})`, s.rename.files ? "info" : ""));
      if (s.rename.conflicts) out.push(card(fmt(s.rename.conflicts), "Save conflicts (left alone)", "bad"));
    }
    if (!s.elsewhere && s.carry && (s.carry.files || s.carry.conflicts)) {
      out.push(card(fmt(s.carry.files), `Save files ${apply ? "copied" : "to copy"} to the better version (${fmt(s.carry.games)} game${s.carry.games === 1 ? "" : "s"}${s.carry.converted ? `, ${fmt(s.carry.converted)} converted to the other region` : ""})`, s.carry.files ? "info" : ""));
      if (s.carry.conflicts) out.push(card(fmt(s.carry.conflicts), "Better versions that already have a save (left alone)", "warn"));
    }
    if (!s.elsewhere && s.ps2 && (s.ps2.saves || s.ps2.conflicts)) {
      out.push(card(fmt(s.ps2.saves), `PlayStation 2 ${s.ps2.saves === 1 ? "save" : "saves"} ${apply ? "converted" : "to convert"} to the better version's region`, s.ps2.saves ? "info" : ""));
      if (s.ps2.conflicts) out.push(card(fmt(s.ps2.conflicts), "Saves the card already has under the new game code (left alone)", "warn"));
    }
    if (!s.elsewhere && s.archive && s.archive.files)
      out.push(card(fmt(s.archive.files), `Save files ${apply ? "archived" : "to archive"} with their games (${fmt(s.archive.games)} game${s.archive.games === 1 ? "" : "s"})`, "warn"));
    if (!s.elsewhere && s.archive && (s.archive.running || []).length)
      out.push(card(s.archive.running.join(", "), `running: close ${s.archive.running.length > 1 ? "them" : "it"}, or its saves stay where they are`, "bad"));
    if (!s.elsewhere && s.leave && s.leave.files)
      out.push(card(fmt(s.leave.files), `Save files left where they are (${fmt(s.leave.games)} archived game${s.leave.games === 1 ? "" : "s"})`, "info"));
    return out;
  }

  /** The Library rules panel, rendered from ``info.catalog``. */
  /** One rule (or a list that is one rule) of the panel. On a system's tab it carries the Override checkbox: until it is ticked the
   *  rule shows your default, greyed and inert. Rules the system cannot use are not drawn. On the Settings page it is the content as it is. */
  const unitStats = { total: 0, over: 0 };
  function unit(fields, content, label = "") {
    const info = rctx.info();
    if (info && info.inherit && !fields.some((f) => info.inherit.applicable.includes(f))) return null;
    unitStats.total += 1;
    if (!rctx.overrides) return content;
    const on = fields.some((f) => info.inherit.overridden.includes(f));
    if (on) unitStats.over += 1;
    const name = label || [...new Set(fields.map(ruleName))].join(", ");
    const set = (value) => rctx.post({ override: { fields, on: value } }).then((r) => { rctx.adopt(r); rctx.rerender(); })
      .catch((err) => { toast(`Could not change the override: ${err.message}`, "error"); rctx.rerender(); });
    const body = el("div", { class: "ov-body" }, content);
    if (!on) body.setAttribute("inert", "");
    return el("div", { class: `ov-unit ${on ? "on" : "off"}`, "data-fields": fields.join(",") },
      el("label", { class: "ov-toggle", title: on ? `${name} is set for this system only` : `Tick to set ${name} for this system only` },
        el("input", { type: "checkbox", checked: on, "data-override": fields.join(","), "aria-label": `Override ${name}`, on: { change: (e) => set(e.target.checked) } }),
        el("span", { text: "Override" })),
      el("div", { class: "ov-main" },
        on ? el("div", { class: "ov-note" }, el("span", { class: "ov-badge", text: "this system only" }),
          el("button", { class: "linklike", type: "button", text: "use the default again", on: { click: () => set(false) } })) : null,
        body));
  }

  /** The line under the rules: how many follow your defaults, and the Reset all button (a system's tab only). */
  function renderOverrideStatus(info) {
    const note = $("library-rules-note"), reset = $("library-reset");
    const n = unitStats.over, total = unitStats.total;
    note.textContent = n ? `${n} ${n === 1 ? "rule is" : "rules are"} overridden for this system; the other ${total - n} follow your defaults.`
      : `Using your defaults for all ${total} rules.`;
    note.dataset.overridden = String(n);
    reset.disabled = !n;
  }

  function renderRules() {
    const info = rctx.info();
    if (!info) return;
    unitStats.total = 0; unitStats.over = 0;
    const prof = info.profile, avail = info.available || {}, scopes = info.scopes || {};
    const scope = (dats) => (dats && dats.length && rctx.overrides ? `applies to: ${dats.map(shortDat).join(", ")}` : "");
    const cat = catalogOf(info);
    const groups = [];

    const excl = cat.filter((e) => e.kind === "exclude");
    if (excl.length) {
      groups.push(unit(["exclude"], el("div", { class: "rules-group", id: "rules-exclude" },
        el("div", { class: "rules-head", text: `${GROUP_TITLE.exclude} (moved to _excluded/)` }),
        el("div", { class: "rules-grid" }, excl.map((e) => ruleRow(e, prof.exclude.includes(e.id),
          (on) => rctx.save({ exclude: on ? [...prof.exclude, e.id] : prof.exclude.filter((x) => x !== e.id) }), `excluded_${e.id}`))),
        el("div", { class: "sub", text: scope(scopes.exclude_dats) })), "Exclusions"));
    }
    const flags = cat.filter((e) => e.kind === "keep_flag");
    if (flags.length && avail.keep_flags !== false) {
      const cr = flags.find((e) => e.id === "cr");
      groups.push(unit(["keep_flags"], el("div", { class: "rules-group", id: "rules-flags" },
        el("div", { class: "rules-head", text: GROUP_TITLE.keep_flag }),
        el("div", { class: "sub note", text: `Untick a type to leave out every variant that carries it. ${scope(scopes.best_variant_dats)}` }),
        el("div", { class: "rules-grid" }, flags.map((e) => ruleRow(e, prof.keep_flags.includes(e.id),
          (on) => rctx.save({ keep_flags: on ? [...prof.keep_flags, e.id] : prof.keep_flags.filter((x) => x !== e.id) }), `excluded_flag_${e.id}`, false))),
        cr && !prof.keep_flags.includes("cr") ? el("div", { class: "notice cr-warning", id: "cr-warning" },
          el("b", { text: "Cracks are off. " }),
          "Many games only exist as cracked dumps, so they will disappear from your library. ",
          el("span", { "data-vanish": "flag_cr" }), " ",
          el("button", { class: "btn btn-small", text: "Keep cracks again", on: { click: () => rctx.save({ keep_flags: [...prof.keep_flags, "cr"] }) } })) : null), "Dump types"));
    }
    const opts = optionEntries(info);
    if (opts.length || info.ranking) {
      const datsOf = { latest_only: scopes.latest_dats, best_variant: scopes.best_variant_dats, complete_only: scopes.complete_dats,
        borrow_editions: scopes.best_variant_dats };
      groups.push(el("div", { class: "rules-group", id: "rules-options" },
        el("div", { class: "rules-head", text: "Options" }),
        el("div", { class: "rules-grid" }, opts.map((e) => {
          const row = ruleRow(e, !!prof[e.field], (on) => rctx.save({ [e.field]: on }), null);
          row.querySelector("input").dataset.opt = e.field;
          const sc = scope(datsOf[e.id]);
          if (sc) row.querySelector(".rule-text").append(el("span", { class: "sub", text: sc }));
          return unit([e.field], row, plainLabel(e));
        }).filter(Boolean)),
        info.ranking ? el("div", { class: "sub ranking", id: "rules-ranking", text: info.ranking }) : null));
    }
    if (avail.languages && cat.some((e) => e.id === "languages")) groups.push(unit(["languages"], languageSection(info)));
    if (avail.region_priority && cat.some((e) => e.id === "region_priority")) groups.push(unit(["region_priority", "region_ties"], regionSection(info)));
    if (ratingEntries(info).length) groups.push(ratingsSection(info));
    if (prof.saved_games !== undefined) groups.push(unit(["saved_games"], savesGroup(prof.saved_games, (v) => rctx.save({ saved_games: v }), rctx.overrides ? "lib" : "set"), "Saves"));
    const drawn = groups.filter(Boolean);
    if (!drawn.length) drawn.push(el("div", { class: "muted", text: "No library rules apply to this system." }));
    rctx.host().replaceChildren(...drawn);
  }

  /** A system's Library rules (each follows your defaults until it is overridden). */
  function renderLibraryRules() {
    rctx = platformRules;
    const info = rctx.info();
    if (!info) return;
    renderRules();
    renderRulesSummary(info);
    renderOverrideStatus(info);
    updateRuleCounts();
    renderRatingsStatus();
  }

  /** Your defaults, on the Settings page. */
  function renderDefaultsRules() {
    rctx = defaultsRules;
    if (!state.libDefaults) { $("set-library-rules").replaceChildren(el("div", { class: "muted", text: "Loading..." })); return; }
    renderRules();
    const changed = (state.libDefaults.changed || []).length;
    $("set-library-note").textContent = changed ? `${changed} ${changed === 1 ? "rule differs" : "rules differ"} from the app's own settings.` : "These are the app's own settings.";
    $("set-library-reset").disabled = !changed;
  }

  // ---- Ratings group (Amendment 18): rendered from the catalog entries with group "ratings"; no rule list here.
  const ratingsOf = () => (state.updates && state.updates.ratings) || null;

  function ratingsSection(info) {
    const prof = info.profile;
    const save = (e, raw) => {
      const text = String(raw).trim();
      let v = text === "" ? null : Number(text);
      if (e.kind === "number" && v !== null && Number.isNaN(v)) { toast(`${e.label} needs a number`, "error"); rctx.rerender(); return; }
      if (v === null && e.default !== null && e.default !== undefined) v = e.default;
      if (v === prof[e.field]) return;
      rctx.save({ [e.field]: v });
    };
    const rows = ratingEntries(info).map((e) => unit([e.field], (() => {
      if (e.kind === "option") {
        return ruleRow(e, !!prof[e.field], (on) => rctx.save({ [e.field]: on }), null);
      }
      let control;
      if (e.kind === "choice") {
        control = el("select", { class: "input select", "data-field": e.field, "aria-label": e.label,
          on: { change: (ev) => rctx.save({ [e.field]: ev.target.value }) } },
        ...(e.choices || []).map((c) => el("option", { value: c.value, text: c.label, selected: prof[e.field] === c.value })));
      } else {
        const input = el("input", { type: "number", class: "input rating-input", "data-field": e.field, "aria-label": e.label,
          min: String(e.min), max: String(e.max), step: String(e.step), inputmode: e.integer ? "numeric" : "decimal",
          placeholder: e.default === null ? "off" : String(e.default) });
        input.value = prof[e.field] === null || prof[e.field] === undefined ? "" : String(prof[e.field]);
        input.addEventListener("change", () => save(e, input.value));
        input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") input.blur(); });
        if (e.filter) input.addEventListener("input", () => updateRatingsHint(info));
        control = el("span", { class: "rating-field" }, input, e.unit ? el("span", { class: "muted small", text: ` ${e.unit}` }) : null);
      }
      return el("div", { class: "rating-row", title: e.description || e.label },
        el("label", { class: "rating-label" }, el("span", { class: "rule-label", text: e.label }), control),
        el("span", { class: "sub", text: e.description || "" }));
    })()));
    return el("div", { class: "rules-group", id: rctx.overrides ? "rules-ratings" : null },
      el("div", { class: "rules-head", text: "Ratings" }),
      rctx.overrides ? el("div", { class: "sub note", id: "ratings-credit" }) : null,
      rctx.overrides ? el("div", { class: "ratings-status", id: "ratings-status", role: "status", "aria-live": "polite" }) : null,
      el("div", { class: "rating-rows" }, rows.filter(Boolean)),
      rctx.overrides ? el("div", { class: "sub", id: "ratings-hint" }) : null,
      el("div", { class: "notice notice-warn ratings-note", id: rctx.overrides ? "ratings-note" : null,
        text: "Games with no rating are excluded while a rating filter is set (tick Keep unrated games to keep them)." }));
  }

  /** "≈ N of M target games rated ≥ x" from the totals' coverage histogram (no server round trip). */
  function updateRatingsHint(info) {
    const box = document.getElementById("ratings-hint");
    if (!box) return;
    const data = Totals.data[state.platform];
    const cov = data && data.rating_coverage;
    const hintEntry = ratingEntries(info).find((e) => e.hint === "coverage_ge");
    const input = hintEntry ? document.querySelector(`#rules-ratings input[data-field="${hintEntry.field}"]`) : null;
    const x = input && input.value !== "" ? Number(input.value) : null;
    if (!cov || x === null || Number.isNaN(x) || x <= 0) { box.textContent = ""; return; }
    const k = Math.min(20, Math.max(1, Math.ceil(x * 2)));
    box.textContent = `\u2248 ${fmt(cov.ge[k - 1])} of ${fmt(cov.games)} target games are rated \u2265 ${x}`;
  }

  /** Credit line, coverage ("Ratings found for X of Y target games"), data date, download progress / button. */
  function renderRatingsStatus() {
    const info = state.library[state.platform];
    const box = document.getElementById("ratings-status");
    const r = ratingsOf();
    const credit = document.getElementById("ratings-credit");
    if (credit) {
      credit.replaceChildren(r && r.credit_url
        ? el("a", { href: r.credit_url, target: "_blank", rel: "noopener noreferrer", text: r.credit || "Ratings: LaunchBox Games Database community ratings" })
        : document.createTextNode("Ratings: LaunchBox Games Database community ratings"));
    }
    renderRatingsBanner();
    if (!box || !info) return;
    const parts = [];
    const data = Totals.data[state.platform];
    const cov = data && data.rating_coverage;
    const running = state.updates && state.updates.running && (state.updates.progress || {}).source === "ratings";
    if (running) {
      const pr = state.updates.progress, pct = pr.total > 0 ? ` (${Math.min(100, Math.round((100 * pr.done) / pr.total))}%)` : "";
      parts.push(el("span", {}, el("span", { class: "spinner", "aria-hidden": "true" }), ` ${pr.message || "Downloading ratings"}${pct}`));
    } else if (!r || !r.installed) {
      parts.push(el("span", { class: "muted", text: "The ratings data is not installed yet. " }),
        r && r.error ? el("span", { class: "error-text", text: `${r.error} ` }) : null);
    } else if (cov) {
      const pct = cov.games ? Math.round((100 * cov.rated) / cov.games) : 0;
      parts.push(el("span", { text: `Ratings found for ${fmt(cov.rated)} of ${fmt(cov.games)} target games (${pct}%) \u00B7 LaunchBox data from ${r.installed}` }));
    } else {
      parts.push(el("span", { class: "muted", text: `LaunchBox data from ${r.installed}` }));
    }
    parts.push(el("button", { class: "btn btn-small", type: "button", id: "ratings-download",
      text: r && r.installed ? "Update ratings" : "Download ratings", disabled: running || (state.updates && !state.updates.enabled) || null,
      title: "Download the LaunchBox Games Database (about 108 MB) and build a small local index; the download is discarded afterwards",
      on: { click: () => downloadRatings() } }));
    box.replaceChildren(...parts.filter(Boolean));
    updateRatingsHint(info);
  }

  async function downloadRatings() {
    try {
      const res = await post("/api/ratings/download", {});
      if (!res.started) toast("An update is already running (or updates are switched off).", "info");
      Updates.apply(res.updates);
    } catch (err) { toast(err.message, "error"); }
  }

  /** Library tab: a rating filter is set but the data is missing / being fetched: say so, with progress. */
  function renderRatingsBanner() {
    const box = $("lib-ratings-banner");
    if (!box) return;
    const info = state.library[state.platform];
    const r = ratingsOf();
    const need = ratingActive(info) && (!r || !r.installed);
    box.classList.toggle("hidden", !need);
    if (!need) return;
    const u = state.updates || {};
    const pr = u.progress || {};
    const live = u.running && pr.source === "ratings";
    const pct = live && pr.total > 0 ? ` (${Math.min(100, Math.round((100 * pr.done) / pr.total))}%)` : "";
    box.replaceChildren(...[
      live ? el("span", { class: "spinner", "aria-hidden": "true" }) : null,
      el("span", { text: live ? ` ${pr.message || "Downloading the ratings"}${pct}. Preview and Build wait for it.`
        : r && r.error ? `The ratings data could not be installed: ${r.error}` : "The ratings data is not installed yet - Preview and Build wait for it." }),
      !live ? el("button", { class: "btn btn-small", type: "button", text: "Download ratings", disabled: (!u.enabled) || null,
        title: u.enabled ? "" : "Automatic downloads are switched off in this run", on: { click: () => downloadRatings() } }) : null,
    ].filter(Boolean));
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
        : (code === "language" ? "" : "The list below says how many titles this affects once it is calculated.");
    }
  }

  /** Background refresh of the plan numbers (rules panel counts) after a scan or a rules change. */
  let statsSeq = 0;
  let statsStale = true;       // the rule counts are fetched when the Library tab is open (not on every scan)
  const refreshLibraryStats = debounce(async () => {
    if (!state.scan) { state.libStats = null; updateRuleCounts(); return; }
    if (!inSystem() || state.tab !== "settings") { statsStale = true; return; }
    statsStale = false;
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
    state.libStats = null;
    updateRuleCounts();
    refreshLibraryStats();
    // The previews stay on screen but are marked out of date (Recalculate is the highlighted action, Build / Apply
    // are disabled until it ran); the library totals on the Overview follow after a short pause.
    Previews.staleAll("Rules changed since this preview", ["lib"]);
    scheduleTotals();
    updateActionState();
  }

  /** No preview (yet, or it belongs to another system): the empty prompt. */
  function resetLibraryPreview() {
    $("lib-empty").textContent = state.scan ? "Working out the files..." : "Scan the folder to see its files.";
    $("lib-empty").classList.remove("hidden");
    $("lib-output").classList.add("hidden");
    vanishTable = null;
    libPlan = null;
    if (state.scan) renderSavesCard();
  }

  function renderLibraryIntro() {
    if (!Previews.has("lib")) resetLibraryPreview();
    refreshUndo();
    refreshLibraryStats();
  }

  function renderLibraryCards(plan) {
    const r = plan.reasons || {}, pl = plan.playlists || {}, vanish = plan.vanish || {}, cats = plan.categories || {};
    // the cards count what this build moves; files a previous build already archived are named next to it
    const aside = (label, moving, all) => ((all || 0) > (moving || 0) ? `${label} now (${fmt(all - (moving || 0))} already archived)` : label);
    const ex = plan.export;
    const asd = plan.aside;
    $("lib-export-note").textContent = ex ? `Library goes to ${ex.dest}. ${(ex.notes || []).join(" ")}` : "";
    $("lib-cards").replaceChildren(
      ...(ex ? [card(fmt((ex.counts.copy || 0)), `To copy (${fmtBytes(ex.bytes_copy)})`, ex.counts.copy ? "info" : ""),
        ...(ex.counts.move ? [card(fmt(ex.counts.move), ex.bytes_linked ? "To move (same drive: instant)" : `To move (${fmtBytes(ex.bytes_copy)} written)`, "warn")] : []),
        card(fmt(ex.counts.exists || 0), "Already in the destination", "ok"),
        ...(ex.counts.replace ? [card(fmt(ex.counts.replace), "To replace (source changed)", "info")] : []),
        ...(ex.counts.remove ? [card(fmt(ex.counts.remove), "To remove (no longer kept)", "warn")] : []),
        ...(ex.counts.kept_edited ? [card(fmt(ex.counts.kept_edited), "Edited by you: stay", "warn")] : []),
        ...(ex.counts.conflict ? [card(fmt(ex.counts.conflict), "Conflicts (left alone)", "bad")] : []),
        ...(ex.enough_space ? [] : [card(fmtBytes(ex.free || 0), "Free space is not enough", "bad")])] : []),
      card(fmt(r.kept), "Kept", "ok"),
      card(fmt(r.bios || 0), "BIOS / firmware files kept", r.bios ? "info" : ""),
      ...(asd ? [card(fmt(asd.coming + asd.existing), "To move out to the archive folder", "info")] : []),
      ...(plan.convert ? [card(fmt(plan.convert.count), plan.convert.kind === "chd" ? "Raw discs to convert to CHD first" : "Dumps to clean up first", plan.convert.count ? "info" : "ok")] : []),
      card(fmt((r.renamed || 0) + (r.moved || 0)), `Renamed / moved (${fmt(r.renamed || 0)} / ${fmt(r.moved || 0)})`, "info"),
      card(fmt(r.excluded), aside("Excluded", r.excluded, cats.excluded), r.excluded ? "warn" : ""),
      card(fmt(r.superseded), aside("Superseded", r.superseded, cats.superseded), r.superseded ? "warn" : ""),
      ...(isGameFolder(currentPlatform()) ? [] : [card(fmt(r.incomplete), aside("Incomplete", r.incomplete, cats.incomplete), r.incomplete ? "warn" : "")]),
      card(fmt(r.duplicates), aside("Duplicates", r.duplicates, cats.duplicate), r.duplicates ? "warn" : ""),
      ...(r.saves ? [card(fmt(r.saves), "Saves found here → _saves/<game>/", "info")] : []),
      ...(r.unmatched ? [card(fmt(r.unmatched), `Unmatched → ${UNMATCHED}/`, "warn")] : []),
      ...(pl.write || pl.remove || pl.ok ? [card(fmt(pl.write), "Playlists to write", pl.write ? "info" : ""),
        card(fmt(pl.remove), "Playlists to remove", pl.remove ? "warn" : "")] : []),
      ...savesCards(plan.saves, false),
      ...(r.borrowed_sets ? [card(fmt(r.borrowed_sets), `Sets completed with borrowed disks (${fmt(r.borrowed_disks || 0)} disk${r.borrowed_disks === 1 ? "" : "s"})`, "info")] : []),
      ...(r.conflict || pl.conflict ? [card(fmt((r.conflict || 0) + (pl.conflict || 0)), "Conflicts (skipped)", "bad")] : []),
      ...(vanish.titles ? [card(fmt(vanish.titles), "Games that vanish", "warn")] : []),
      ...(plan.rating && plan.rating.games ? [card(fmt(plan.rating.excluded), `Left out by rating (of ${fmt(plan.rating.games)} games; ${fmt(plan.rating.unrated)} unrated)`, plan.rating.excluded ? "warn" : "")] : []),
    );
    state.libStats = { reasons: r, vanish, exclusions: plan.exclusions || {} };
    updateRuleCounts();
    renderVanishBox(plan);
    $("lib-uptodate").classList.toggle("hidden", !plan.empty);
    const sets = plan.incomplete_sets || [];
    $("lib-incomplete-box").classList.toggle("hidden", !sets.length);
    if (sets.length && sets.length <= 15) $("lib-incomplete-box").open = true;
    const total = plan.incomplete_total || sets.length;
    $("lib-incomplete-title").textContent = isGameFolder(currentPlatform())
      ? `Multi-disc games with a disc missing (${fmt(total)}) - kept where they are, no playlist`
      : `Incomplete sets (${fmt(total)}) - disks missing, archived as _incomplete`;
    $("lib-incomplete").replaceChildren(...sets.map((x) => el("li", {},
      el("span", { text: x.name }), " ",
      el("span", { class: "tag tag-status", text: `missing ${isGameFolder(currentPlatform()) ? "disc" : "disk"} ${x.missing.join(", ")} of ${x.total}` }),
      el("span", { class: "sub", text: ` have ${x.present.length ? x.present.join(", ") : "none"} - ${shortDat(x.dat)}` }))),
    ...(total > sets.length ? [el("li", { class: "muted", text: `... and ${fmt(total - sets.length)} more` })] : []));
  }

  /** The version the rules prefer to this file's, when you own another one of the game: ``{title, better}`` of the upgrades list, or null.
   *  The list is asked for once per set of numbers (rules, databases, scan) and kept. */
  const Better = {
    docs: {},
    async of(i) {
      const p = currentPlatform();
      const d = Totals.data[p.name];
      const key = d ? `${d.profile_signature}|${d.computed_at}|${(state.scan && state.scan.finished) || ""}` : "";
      let hit = this.docs[p.name];
      if (!hit || hit.key !== key) {
        hit = this.docs[p.name] = { key, doc: null };
        try { hit.doc = await get(`/api/library/upgrades?${qs({ platform: p.name })}`); } catch (_) { hit.doc = null; }
        if (hit.doc && !hit.doc.ready) { hit.doc = null; this.docs[p.name] = null; return null; }
      }
      if (!hit.doc) return null;
      const names = [i.rom_name, i.game_ref && i.game_ref.game, i.name].filter(Boolean);
      const bare = (n) => n.replace(/\.[A-Za-z0-9]{1,4}$/, "");
      return hit.doc.items.find((it) => it.have.some((h) => names.some((n) => n === h || bare(n) === h || n === bare(h)))) || null;
    },
  };

  /** Reason / note cell of one preview row. */
  /** The "why" paragraph of a Library row's details: the rule that decided, the version that won, the rating. */
  function libraryWhyPanel(i) {
    if (i.item !== "file") return null;
    const lines = [];
    const cat = i.category;
    const names = (i.reasons || []).map(reasonLabel);
    if ((i.reasons || []).includes("override_exclude")) lines.push("You chose to always exclude this game.");
    else if (cat === "kept") lines.push(i.reason && i.reason.includes("always keep") ? "You chose to always keep this game."
      : i.reason && i.reason.includes("you have saves") ? "Kept because RetroArch has saves or save states for it and the choice for its saves is \"Keep both ROMs\" (Library rules, or the choice for this game)."
        : "Kept.");
    else if (cat === "excluded") lines.push(`Excluded by: ${names.join("; ") || i.reason}.${i.flags_text ? ` Flags: ${i.flags_text}.` : ""}`);
    else if (cat === "superseded") lines.push(`A better version of the same game wins${i.superseded_by ? `: ${i.superseded_by}` : ""}. ${i.reason || ""}`);
    else if (cat === "incomplete") lines.push(`Part of an incomplete multi-disk set${i.missing && i.missing.length ? ` (missing disk ${i.missing.join(", ")})` : ""}.`);
    else if (cat === "duplicate") lines.push(`The same content as ${i.keeper || "another file"}, which is the copy kept.`);
    else if (cat === "unmatched") lines.push("This file matches nothing in the DATs.");
    else if (i.reason) lines.push(i.reason);
    const t = i.tags;
    const facts = [];
    if (i.rating !== null && i.rating !== undefined) facts.push(`rated ${Number(i.rating).toFixed(1)} from ${fmt(i.votes)} vote${i.votes === 1 ? "" : "s"}`);
    if (t && t.regions && t.regions.length) facts.push(`region ${t.regions.join(", ")}`);
    if (t && t.languages && t.languages.length) facts.push(`language ${t.languages.join(", ")}${t.languages_implied ? " (assumed)" : ""}`);
    if (t && t.version) facts.push(`version ${t.version}`);
    if (i.year) facts.push(`released ${i.year}`);
    if (i.status === "move" || i.status === "rename") facts.push(`will be ${i.kind === "rename" ? "renamed" : "moved"} to ${i.to}`);
    return el("div", { class: "why-panel" }, ...lines.map((l) => el("div", { text: l })),
      facts.length ? el("div", { class: "muted small", text: facts.join(" - ") }) : null);
  }

  const SAVED_OPTIONS = [["", "Use the default"], ["keep", "Keep both ROMs"], ["archive", "Archive the saves with the ROM"], ["leave", "Leave the saves"], ["carry", "Copy the save to the better version"], ["convert", "Copy the save, converting its region (PlayStation, PlayStation 2)"]];
  const SAVED_EFFECT = { rename: ["info", "renamed with the ROM"], keep: ["ok", "ROM kept for them"], archive: ["warn", "archived with the ROM"], leave: ["skip", "left where they are"], carry: ["info", "save copied to the better version"], convert: ["info", "save copied to the better version"] };


  /** The Saves cell of a Library row: how many, what the build does with them, and the choice for this game. */
  function libSavesCell(i) {
    const s = i.saves || null;
    if (!s) return el("span", { class: "muted", text: "" });
    const eff = SAVED_EFFECT[s.effect];
    const info = state.library[state.platform];
    const own = ((info && info.profile.saved_overrides) || []).find((o) => o[0] === s.dat && o[1] === s.game);
    const select = s.game ? el("select", { class: "input select saves-choice", "data-game": s.game, "aria-label": `If a rule replaces ${s.game}: what happens to its saves`,
      title: "If a rule replaces or archives this game: what happens to its saves. Only this game; the default is in the Library rules.",
      on: { change: (e) => setSavedChoice(s, e.target.value) } },
    ...SAVED_OPTIONS.map(([v, label]) => el("option", { value: v, text: label, selected: v === (own ? own[2] : "") }))) : null;
    return el("div", { class: "saves-cell" },
      el("div", {}, el("span", { class: "saves-total", text: fmt(s.total) }), " ", el("span", { class: "sub", text: savesText(s) })),
      eff ? el("div", {}, badge(eff[0], eff[1])) : null,
      select);
  }

  /** One game's own choice for its saves (stored in the library profile). */
  async function setSavedChoice(s, choice) {
    try {
      const info = await post("/api/library/saved", { platform: state.platform, choice: choice || "default", games: [{ dat: s.dat, game: s.game }] });
      adoptProfile(state.platform, info);          // the preview is marked out of date: Recalculate applies it
      toast(`${s.game}: ${choice ? SAVED_OPTIONS.find((o) => o[0] === choice)[1].toLowerCase() : "back to the default"} - the list is updating`, "ok", 6000);
    } catch (err) { toast(err.message, "error"); }
  }

  /** "Always keep / exclude / back to the rules" for the ticked Library rows (one choice per game, not per file). */
  async function setOverrides(items, action) {
    const seen = new Map();
    for (const i of items) if (i.game_ref) seen.set(`${i.game_ref.dat}\t${i.game_ref.game}`, i.game_ref);
    if (!seen.size) return;
    try {
      const info = await post("/api/library/override", { platform: state.platform, action, games: [...seen.values()] });
      adoptProfile(state.platform, info);        // the preview is marked out of date: Recalculate applies it
      toast(action === "clear" ? `${fmt(seen.size)} game(s) back under the rules` : `${fmt(seen.size)} game(s) will always be ${action === "keep" ? "kept" : "excluded"} - the list is updating`, "ok", 6000);
    } catch (err) { toast(err.message, "error"); }
  }

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
    const inf = state.library[state.platform];
    if (inf && (inf.rating_codes || []).includes(reason)) return `Left out by the rating filter: ${reasonLabel(reason)}`;
    return `Every version is excluded: ${reasonLabel(reason)}`;
  }

  function renderVanishBox(plan) {
    const v = plan.vanish || {};
    const box = $("lib-vanish-box");
    box.classList.toggle("hidden", !v.titles);
    if (!v.titles) return;
    $("lib-vanish-title").textContent = `Games that vanish (${fmt(v.titles)}) - no version is kept`;
    const reasons = v.by_reason || {};
    filterChips($("lib-vanish-filters"), reasons, state.vanishReason, ["rating_unrated", "rating_low", "rating_not_top", "language", "flag_cr", "flag_h", "flag_t", "flag_a", "flag_f", "flag_tr"],
      (key) => { state.vanishReason = key; renderVanishBox(plan); if (vanishTable) { vanishTable.offset = 0; vanishTable.load(); } },
      (k) => (k === "language" ? "No version in selected languages" : vanishText(k)), "All reasons");
    if (box.open && !vanishTable) showVanishTable();
    else if (vanishTable && vanishPlanId !== plan.plan_id) { vanishTable.offset = 0; vanishTable.load(); }      // the list was calculated again: so is this one
    vanishPlanId = plan.plan_id;
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
    if (info && (data.by_reason || {}).rating_unrated && !info.profile.keep_unrated) {
      hints.push(el("button", { class: "btn btn-small", text: `Keep unrated games (${fmt(data.by_reason.rating_unrated)})`,
        title: "Tick Keep unrated games: games with no usable rating stay", on: { click: () => saveProfile({ keep_unrated: true }) } }));
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
      placeholder: "Search titles...", emptyText: "Nothing in this category.", pageSize: 25, id: "vanish",
      sort: { get: () => state.vanishSort, set: (v) => { state.vanishSort = v; } },
      fetch: async ({ offset, limit, q }) => {
        const data = await post("/api/library/vanished", { ...libOptions(), reason: state.vanishReason, sort: state.vanishSort, offset, limit, q });
        renderVanishHints(data);
        return data;
      },
      columns: [
        { label: "Title", cls: "wrap", sortKey: "name", sortFirst: "asc", always: true, render: (i) => el("div", {}, el("div", { text: i.title }), el("div", { class: "sub mono-path", text: i.name }))},
        { label: "Why it vanishes", cls: "wrap", render: (i) => el("div", {}, el("div", { text: vanishText(i.reason) }),
          i.hint ? el("div", { class: "sub", text: `${i.detail ? i.detail + " - " : ""}${i.hint}` }) : null,
          el("div", { class: "tags" }, (i.codes || []).map((c) => el("span", { class: "tag tag-excl", text: reasonLabel(c) })),
            (i.languages || []).map((l) => el("span", { class: "tag tag-lang", text: l })))) },
        { label: "Variants", render: (i) => String(i.variants) },
      ],
    });
    vanishTable.load();
  }

  /** Ask for the plan of a build with the current rules (its numbers, its warnings, what it would do), then draw the files from it. */
  async function showLibraryPlan() {
    Previews.busy("lib");
    $("lib-empty").classList.add("hidden");
    $("lib-output").classList.remove("hidden");
    const load = Previews.wrap("lib", async ({ refresh }) => {
      const data = await post("/api/library/plan", { ...buildOptions(), limit: 1, refresh });
      libPlan = data;
      renderTotals();
      renderLibraryCards(data);
      renderSavesCard();
      const box = $("lib-warnings");
      const notes = [...(data.warnings || [])];
      if (data.missing_dats && data.missing_dats.length) notes.push(`Not checked (DAT not installed): ${data.missing_dats.map(shortDat).join(", ")}.`);
      box.classList.toggle("hidden", !notes.length);
      box.replaceChildren(...notes.map((n) => el("div", { text: n })));
      $("lib-apply-btn").dataset.blocked = data.empty && !(data.aside && data.aside.existing) && !(data.convert && data.convert.count) ? "1" : "0";
      Jobs.setRunning(Jobs.running);
      return data;
    });
    try { await load({}); } catch (_) { return; }            // (the status line says what went wrong)
    Files.reload();
  }

  async function applyLibraryExport() {
    let plan;
    try {
      plan = await post("/api/library/plan", { ...buildOptions(), limit: 1 });
    } catch (err) { toast(err.message, "error"); return; }
    const ex = plan.export;
    if (plan.empty) { toast("Nothing to do - the destination already has the library.", "ok"); return; }
    if (!ex.enough_space) { toast(`Not enough free space on the destination (${fmtBytes(ex.bytes_copy)} needed, ${fmtBytes(ex.free || 0)} free).`, "error", 10000); return; }
    const line = (n, text) => (n ? el("li", { text: `${fmt(n)} ${text}` }) : null);
    const body = el("div", {},
      ...(ex.notes || []).map((w) => el("p", { class: "notice", text: w })),
      el("p", { text: `Build the library in ${ex.dest} from ${state.scan.root}:` }),
      el("ul", {},
        line(ex.counts.copy, `file(s) copied (${fmtBytes(ex.bytes_copy)})`),
        line(ex.counts.move, "file(s) MOVED out of the source folder"),
        line(ex.counts.exists, "file(s) already there (skipped)"),
        line(ex.counts.conflict, "file(s) skipped: a different file is already at that name"),
        line(ex.counts.replace, "file(s) copied again because the source changed"),
        line(ex.counts.remove, "file(s) REMOVED from the destination: the rules no longer keep them"),
        line(ex.counts.kept_edited, "file(s) you edited stay where they are"),
        line(ex.playlists, "playlist(s) written")),
      el("ul", {},
        el("li", { text: ex.counts.move ? "Moved files are no longer in the source folder. Undo moves them back." : "The source folder is not changed: its files stay where they are." }),
        el("li", { text: "Only the files your rules keep are built; excluded, superseded and unmatched files stay in the source." }),
        el("li", { text: "\"Undo last build\" removes what this build added (nothing else)." })));
    if (!(await confirmDialog({ title: ex.sync ? "Build and sync library" : "Build library in another folder", body, okText: `Build (${fmt(plan.actionable)} items)` }))) return;
    if (ex.mass_removal && !(await confirmDialog({
      title: "Remove most of the library?",
      body: `${(ex.notes || []).join(" ")} ${fmt(ex.counts.remove)} files would be removed. Only do this if you changed the rules on purpose and the source is complete.`,
      okText: "Yes, remove them", danger: true }))) return;
    Jobs.start("/api/library/apply", { ...buildOptions(), plan_id: plan.plan_id, allow_mass_removal: ex.mass_removal ? true : undefined });
  }

  async function undoLibraryExport() {
    const dest = $("lib-export-dest").value.trim();
    let runs;
    try { runs = (await get(`/api/library/export/runs?${qs({ dest })}`)).runs || []; } catch (err) { toast(err.message, "error"); return; }
    const run = runs[0];
    if (!run) { toast("No library build is recorded in the destination.", "info"); return; }
    const ok = await confirmDialog({
      title: "Undo last build",
      body: `Remove the ${fmt(run.files)} file(s) added to ${dest} on ${run.started.replace("T", " ")}? Files you changed since are left in place. The source is not touched.`,
      okText: "Undo", danger: true,
    });
    if (!ok) return;
    try {
      const r = await post("/api/library/export/undo", { dest, run: run.id, follow: state.exportFollow || undefined });
      state.exportFollow = "";
      toast(`Removed ${fmt(r.removed)} file(s)${r.restored ? `, put back ${fmt(r.restored)}` : ""}${r.skipped.length ? `, ${fmt(r.skipped.length)} left (changed or not restorable)` : ""}`, r.skipped.length ? "error" : "ok", 8000);
      if (libPlan) showLibraryPlan();
    } catch (err) { toast(err.message, "error"); }
  }

  async function applyLibrary() {
    if (exportActive()) return applyLibraryExport();
    if ($("lib-where-other").checked) { toast("Choose the folder to build the library in first.", "error"); return; }
    let plan;
    try {
      plan = await post("/api/library/plan", { ...libOptions(), ...asideOptions(), limit: 1 });
    } catch (err) { toast(err.message, "error"); return; }
    if (plan.empty && !(plan.aside && plan.aside.existing) && !(plan.convert && plan.convert.count)) { toast("Nothing to do - the library is already built.", "ok"); return; }
    const r = plan.reasons || {}, pl = plan.playlists || {};
    const warnings = plan.warnings || [];
    const line = (n, text) => (n ? el("li", { text: `${fmt(n)} ${text}` }) : null);
    const into = plan.aside ? " in the archive folder" : "";           // (or in the system's own folder while there is no archive)
    const body = el("div", {},
      ...warnings.map((w) => el("p", { class: "notice", text: w })),
      el("p", { text: `Build the library inside ${state.scan.root}:` }),
      el("ul", {},
        line((r.renamed || 0) + (r.moved || 0), `file(s) renamed / moved to their place (${fmt(r.kept)} kept in total)`),
        line(r.excluded, `excluded file(s) → ${EXCLUDED}/${into}`),
        line(r.superseded, `superseded file(s) → ${SUPERSEDED}/${into}`),
        line(r.incomplete, `file(s) of incomplete sets → ${INCOMPLETE}/${into}`),
        line(r.duplicates, `duplicate(s) → ${DUPLICATES}/${into}`),
        line(r.saves, `save file(s) found in this folder → _saves/<game>/${into}`),
        line(r.unmatched, `unmatched file(s) → ${UNMATCHED}/${into} (they match nothing in the DATs; subfolders are kept)`),
        line((plan.vanish || {}).titles, "title(s) have no kept version (see \"Games that vanish\")"),
        line(pl.write, "playlist(s) written"),
        line(pl.remove, "outdated playlist(s) made by this app removed"),
        ...savesLines(plan.saves)),
      el("ul", {},
        el("li", { text: "Nothing is deleted and existing files are never overwritten." }),
        ...(plan.aside ? [el("li", { text: `What is archived (${fmt(plan.aside.coming + plan.aside.existing)} file(s)) is then moved out of this folder to ${plan.aside.path}.` })] : []),
      ...(plan.convert && plan.convert.count ? [el("li", { text: plan.convert.kind === "chd"
        ? `${fmt(plan.convert.count)} raw disc set(s) are converted to CHD first (checked against Redump; this can take a long time). The library counts above are those before the conversion.`
        : `${fmt(plan.convert.count)} dump(s) are cleaned up first. The library counts above are those before the clean-up.` })] : []),
        el("li", { text: "One undo log is saved as it goes - \"Undo last\" reverts the moves and removes / restores the playlists." }),
        el("li", { text: "The folder is re-scanned afterwards." })));
    if (!(await confirmDialog({ title: "Build library", body, okText: `Build library (${fmt(plan.actionable)} changes)` }))) return;
    if (warnings.length && !(await confirmDialog({
      title: "Are you sure?",
      body: `${warnings.join(" ")} Moving files out of other systems' folders would break them for your emulators.`,
      okText: "Yes, build in this folder", danger: true,
    }))) return;
    Jobs.start("/api/library/apply", { ...libOptions(), ...asideOptions(), plan_id: plan.plan_id });   // the server refuses a plan of other rules
  }

  /** The saves lines of a confirmation (only what the plan says will happen). */
  function savesLines(s) {
    if (!s || !s.found) return [];
    const out = [];
    if (s.kept) out.push(el("li", { text: `${fmt(s.kept)} game(s) the rules would archive are kept because you have saves for them.` }));
    if (s.follow && s.rename && s.rename.files) out.push(el("li", { text: `${fmt(s.rename.files)} save file(s) are ${s.elsewhere ? "copied" : "renamed"} with their ROMs.` }));
    if (s.rename && s.rename.conflicts) out.push(el("li", { text: `${fmt(s.rename.conflicts)} save file(s) cannot be renamed: a file with the new name is there. They stay as they are.` }));
    if (!s.elsewhere && s.ps2 && s.ps2.saves) out.push(el("li", { text: `${fmt(s.ps2.saves)} PlayStation 2 save(s) are converted to the better version's game code (a card image is rewritten; the old image is kept for Undo).` }));
    if (!s.elsewhere && s.carry && s.carry.files) out.push(el("li", { text: `${fmt(s.carry.files)} save file(s) are copied to the better version of ${fmt(s.carry.games)} game(s)${s.carry.converted ? `, ${fmt(s.carry.converted)} of them converted to the other region` : ""}. The old saves stay.` }));
    if (!s.elsewhere && s.archive && s.archive.files)
      out.push(el("li", { text: `${fmt(s.archive.files)} save file(s) of ${fmt(s.archive.games)} archived game(s) move to ${s.archive.to}${s.running ? " - not now: RetroArch is running, so they stay" : ""}.` }));
    if (!s.elsewhere && s.leave && s.leave.files)
      out.push(el("li", { text: `${fmt(s.leave.files)} save file(s) of ${fmt(s.leave.games)} archived game(s) stay where they are.` }));
    return out;
  }

  async function undoLibrary() {
    if ($("lib-where-other").checked) {
      if (!$("lib-export-dest").value.trim()) { toast("Choose the destination folder first.", "error"); return; }
      return undoLibraryExport();
    }
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
      const isExport = r.action === "library_export";
      if (isExport && r.saves && r.saves.journal) state.exportFollow = r.saves.journal;       // (undo removes the copied saves too)
      const failed = Array.isArray(r.failed) ? r.failed : [];
      const n = (v) => (Array.isArray(v) ? v.length : v || 0);
      const text = isUndo
        ? `Reverted ${fmt(n(r.restored))} file(s)${r.created_removed ? `, removed ${fmt(r.created_removed)} playlist/converted file(s)` : ""}`
        : isExport
          ? `Built ${fmt(r.created || 0)} file(s) in ${r.dest} (${fmt(r.copied || 0)} copied, ${fmt(r.moved || 0)} moved), ${fmt(r.playlists || 0)} playlist(s)${r.removed ? `, removed ${fmt(r.removed)}` : ""}${r.cancelled ? " - cancelled" : ""}`
          : `Moved ${fmt(n(r.moved))} file(s), wrote ${fmt(n(r.playlists_written))} playlist(s)`;
      const sv = r.saves || {};
      const saveText = sv.carried ? `; ${fmt(sv.carried)} save file(s) copied to the better version` + ((sv.followed || 0) ? `, ${fmt(sv.followed)} renamed with their ROMs` : "") : (sv.followed || sv.copied) ? `; ${fmt((sv.followed || 0) + (sv.copied || 0))} save file(s) ${sv.copied ? "copied to" : "renamed to"} the new names` : (r.action === "undo" && sv.restored ? `; ${fmt(sv.restored)} save file(s) renamed back` : "");
      const p2 = r.ps2_saves;
      const sa = r.saves_archived;
      const archText = sa ? (sa.skipped_running ? `; ${(sa.running || ["RetroArch"]).join(", ")} ${(sa.running || [1]).length > 1 ? "are" : "is"} running: ${sa.moved ? "some" : "the"} saves of the archived games were not moved` : `; ${fmt(sa.moved)} save file(s) archived with their games`) : "";
      const asideText = r.aside ? `; ${fmt(r.aside.moved)} archived file(s) moved out to ${r.aside.path}` : (r.action === "undo" && r.aside_restored ? `; ${fmt(r.aside_restored)} archived file(s) brought back` : "");
      const convText = r.converted ? `; ${fmt(r.converted.count)} converted first${r.converted.failed.length ? ` (${fmt(r.converted.failed.length)} could not be converted)` : ""}` : (r.action === "undo" && r.converted_back ? "; the conversion was undone too" : "");
      const ps2Text = p2 ? (p2.skipped_running ? "; PCSX2 is running: its saves were not converted" : p2.saves ? `; ${fmt(p2.saves)} PlayStation 2 save(s) converted to the better version` : "") : "";
      toast(text + saveText + ps2Text + archText + asideText + convText + (failed.length ? `, ${fmt(failed.length)} failed` : ""), failed.length || r.error ? "error" : "ok", 8000);
      const skipped = Array.isArray(r.skipped) ? r.skipped : [];
      showFailures("lib-failures", isUndo ? "Could not restore:" : "Could not move / write:",
        failed.concat(skipped.filter((x) => x.reason !== "already back in place")),
        (f) => `${f.src || f.path || ""}${f.dst ? ` → ${f.dst}` : ""}: ${f.error || f.reason || "failed"}`,
        [r.error ? `Stopped: ${r.error}` : "",
          r.remaining ? `${fmt(r.remaining)} move(s) are kept in the undo log - fix the cause and press "Undo last" again.` : "",
          r.rescan_error ? `The folder could not be re-scanned (${r.rescan_error}) - scan it again.` : ""]);
    }
    const wasOpen = !$("lib-output").classList.contains("hidden");
    if (job.result && job.result.action === "library_export") {      // the source did not change: no re-scan
      if (wasOpen && state.scan && libPlan) await showLibraryPlan();
      return;
    }
    await refreshScan();
    if (wasOpen && state.scan) { vanishTable = null; await showLibraryPlan(); }
  };

  function filterChips(container, counts, current, order, onPick, labelFor = (k) => k, allLabel = "All") {
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const chip = (key, label, n) => el("button", {
      class: `chip ${current === key ? "active" : ""}`, text: `${label} (${fmt(n)})`,
      on: { click: () => onPick(key) },
    });
    const keys = order.filter((k) => counts[k]).concat(Object.keys(counts).filter((k) => !order.includes(k)));
    container.replaceChildren(chip("", allLabel, total), ...keys.map((k) => chip(k, labelFor(k), counts[k])));
  }

  let undoLogs = [];
  let undoFetch = null;      // the request in flight: every tab intro asks at once when a page opens
  async function refreshUndo() {
    let logs = [];
    if (state.scan) {
      undoFetch = undoFetch || get("/api/organise/undo-logs").finally(() => { undoFetch = null; });
      try { logs = (await undoFetch).logs || []; } catch (_) { logs = []; }
    }
    undoLogs = logs;
    for (const id of ["lib-undo-btn"]) {
      const btn = $(id);
      btn.dataset.blocked = undoLogs.length || (id === "lib-undo-btn" && $("lib-where-other").checked && state.scan) ? "0" : "1";
      btn.title = undoLogs.length ? `Undo ${undoLogs[0].name}` : "No undo logs in this folder";
    }
    Jobs.setRunning(Jobs.running);
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
      showFailures("lib-failures", isUndo ? "Could not restore:" : "Could not move:",
        failed.concat(skipped.filter((x) => x.reason !== "already back in place")),
        (f) => `${f.src}${f.dst ? ` → ${f.dst}` : ""}: ${f.error || f.reason || "failed"}`,
        [r.error ? `Stopped: ${r.error}` : "",
          r.remaining ? `${fmt(r.remaining)} move(s) are kept in the undo log - fix the cause and press "Undo last" again.` : "",
          r.rescan_error ? `The folder could not be re-scanned (${r.rescan_error}) - scan it again.` : ""]);
    }
    await refreshScan();
  };

  // ------------------------------------------------------------------ Disc images (CHD): the settings every disc system shares
  let chdmanInfo = null;

  async function loadChdman(refresh = false) {
    try {
      chdmanInfo = await get(`/api/chdman${refresh ? "?refresh=1" : ""}`);
    } catch (err) { chdmanInfo = null; }
    renderChdman();
  }

  /** How CD audio is decoded: libFLAC, libsndfile standing in for it, or the slow built-in decoder. */
  function flacDecodingText(flac) {
    if (!flac) return "";
    if (flac.native && flac.library === "libsndfile") return " Audio decoding: libsndfile (libFLAC was not found).";
    if (flac.native) return " Audio decoding: native libFLAC.";
    return " Audio decoding: built-in Python FLAC (slow) - libFLAC was not found.";
  }

  function renderChdman() {
    if (!chdmanInfo) return;
    const found = !!chdmanInfo.found;
    const notes = (chdmanInfo.notes || []).join(" ");
    $("chdman-line").textContent = (found ? `chdman found: ${chdmanInfo.label}${chdmanInfo.bundled ? " - shipped with the app" : ""} (optional: the app reads and writes CHDs by itself).`
      : "chdman not found - it is not needed: the app reads and writes CHDs by itself.")
      + (notes ? ` (${notes})` : "")
      + flacDecodingText(chdmanInfo.flac)
      + (chdmanInfo.workers ? ` Decode processes: ${chdmanInfo.workers}.` : "");
    const steps = $("chdman-steps");
    const wantsChdman = chdmanInfo.writer === "chdman" || chdmanInfo.engine === "chdman";
    steps.classList.toggle("hidden", found || !wantsChdman);       // how to install it: only when it was asked for
    steps.replaceChildren(...(found || !wantsChdman ? [] : (chdmanInfo.steps || []).map((t) => el("li", { text: t }))));
    if (document.activeElement !== $("chdman-path")) $("chdman-path").value = chdmanInfo.override || "";
    $("chdman-path").placeholder = chdmanInfo.os === "windows" ? "C:\\...\\chdman.exe (optional override)" : "/path/to/chdman (optional override)";
    // without libFLAC the built-in writer stores audio tracks with LZMA: valid and verified, but larger
    $("chd-flac-note").classList.toggle("hidden", chdmanInfo.flac_encoder !== false || chdmanInfo.writer === "chdman");
    $("chdman-engine").value = chdmanInfo.engine || "auto";
    $("chd-writer").value = chdmanInfo.writer || "auto";
    const zstdOption = $("chd-preset").querySelector('option[value="zstd"]');
    zstdOption.disabled = !chdmanInfo.zstd_writer || chdmanInfo.writer === "chdman";
    $("chd-preset").value = zstdOption.disabled ? "default" : (chdmanInfo.preset || "default");
    $("chd-preset-note").classList.toggle("hidden", $("chd-preset").value !== "zstd");
    $("chd-verify-scan").checked = !!chdmanInfo.verify_scan;
    const speed = state.dcSpeed || "";
    $("chd-speed-line").textContent = speed ? `Last decode: ${speed}` : "";
    $("chd-speed-line").classList.toggle("hidden", !speed);
  }

  async function saveChdman(body) {
    try {
      chdmanInfo = await post("/api/chdman", body);
      renderChdman();
      if (chdmanInfo.warning) toast(chdmanInfo.warning, "error", 8000);
      else toast("Saved", "ok");
    } catch (err) { toast(err.message, "error", 8000); }
  }

  // ------------------------------------------------- the Library option "convert first" (raw discs to CHD / clean up dumps)
  function renderLibraryConvert() {
    const p = currentPlatform();
    const on = !!(p && p.convertible);
    $("lib-convert-row").classList.toggle("hidden", !on);
    if (!on) return;
    $("lib-convert-label").textContent = isGameFolder(p)
      ? "Convert raw disc sets (.gdi / .cue + tracks, or an .iso) to CHD first. Each CHD is checked against Redump before the raw files move to _converted_originals/ (nothing is deleted); this takes a while. Settings: Disc images (CHD)."
      : "Clean up dumps first: remove SNES copier headers and fix the byte order of N64 dumps, so the files equal the database's. The originals move to _converted_originals/ (nothing is deleted).";
    $("lib-convert").checked = !!p.convert_on_build;
  }

  async function saveConvertOption() {
    const p = currentPlatform();
    if (!p) return;
    const value = $("lib-convert").checked;
    try {
      await post("/api/platforms/options", { platform: p.name, convert: value });
      p.convert_on_build = value;
    } catch (err) { toast(`Could not save the option: ${err.message}`, "error"); $("lib-convert").checked = !value; return; }
    if (Previews.has("lib")) resetLibraryPreview();
  }

  // ------------------------------------------------------------------ RetroArch: saves and config (v0.2)

  // ------------------------------------------------------------------ the emulators whose saves are looked at (besides RetroArch)
  const Emu = {
    list: [],

    async show() {
      try { this.list = (await get("/api/emulators")).emulators; } catch (err) { toast(err.message, "error"); return; }
      this.render();
    },

    render() {
      $("emu-list").replaceChildren(...this.list.map((e) => {
        const input = el("input", { type: "text", class: "input grow mono", id: `emu-${e.key}-folder`, value: e.folder, spellcheck: "false", autocomplete: "off",
          placeholder: e.what, "aria-label": `${e.label}: ${e.what}`, disabled: e.enabled ? null : true,
          on: { change: () => this.save(e.key, { folder: input.value.trim() }) } });
        const note = !e.enabled ? "Switched off: its saves are ignored."
          : e.found ? (e.auto ? "Found on this machine and selected." : "Your folder.")
          : e.folder ? "That folder does not exist." : "Not found: choose its folder, or leave it if you do not use it.";
        return el("div", { class: "emu-row", "data-emulator": e.key },
          el("div", { class: "row" },
            el("label", { class: "check" }, el("input", { type: "checkbox", id: `emu-${e.key}-on`, checked: e.enabled ? true : null,
              on: { change: (ev) => this.save(e.key, { enabled: ev.target.checked }) } }), el("b", { text: e.label })),
            el("span", { class: "muted small", text: e.platforms.join(", ") })),
          el("div", { class: "row" }, input,
            el("button", { class: "btn", id: `emu-${e.key}-browse`, text: "Folders...", disabled: e.enabled ? null : true,
              on: { click: () => FolderBrowser.open(input, `Choose ${e.label}'s folder`) } })),
          el("div", { class: "muted small", id: `emu-${e.key}-note`, text: note }));
      }));
    },

    async save(key, change) {
      try {
        this.list = (await post("/api/emulators/config", { source: key, ...change })).emulators;
        this.render();
        refreshSavesAwareness();                           // (the systems' saves cards, columns and rules follow)
      } catch (err) { toast(err.message, "error"); }
    },
  };

  const RetroArch = {
    info: null,
    loaded: false,
    last: null,

    async show() {
      await this.load();
      this.render();
      if (this.info && this.info.settings && !this.sharedShown) { this.sharedShown = true; this.sharedCheck(); }
    },

    async load() {
      try { this.info = await get("/api/retroarch"); } catch (err) { toast(err.message, "error"); return; }
      this.loaded = true;
    },

    render() {
      const info = this.info;
      if (!info) return;
      $("ra-none").classList.toggle("hidden", info.installs.length > 0);
      $("ra-install").replaceChildren(...info.installs.map((i) => el("option", { value: i.cfg, text: `${i.label} - ${i.cfg}`, selected: i.cfg === info.selected })));
      $("ra-install").disabled = !info.installs.length;
      $("ra-running").classList.toggle("hidden", !info.running);
      $("ra-change-panel").classList.toggle("hidden", !info.settings);
      $("ra-follow-panel").classList.toggle("hidden", !info.settings);
      $("ra-shared-panel").classList.toggle("hidden", !info.settings);
      $("ra-bios-panel").classList.toggle("hidden", !info.settings);
      $("bios-no-ra").classList.toggle("hidden", !!info.settings);
      $("ra-current-panel").classList.toggle("hidden", !info.settings);
      if (!info.settings) return;
      const s = info.settings;
      const where = (k) => s[k] || "(RetroArch's default: next to the games)";
      const yes = (v) => (v ? "yes" : "no");
      $("ra-current").replaceChildren(
        ...[["Save files", where("savefile_path")], ["Save states", where("savestate_path")],
          ["One folder per core (saves / states)", `${yes(s.sort_savefiles_enable)} / ${yes(s.sort_savestates_enable)}`],
          ["Saves next to the games (saves / states)", `${yes(s.savefiles_in_content_dir)} / ${yes(s.savestates_in_content_dir)}`],
          ["BIOS / system files", where("system_path")], ["Games folder RetroArch opens at", s.content_path || "-"],
          ["Config file", s.cfg]]
          .map(([k, v]) => el("tr", {}, el("th", { text: k }), el("td", { class: "mono", text: v }))));
      const ov = $("ra-overrides");
      ov.classList.toggle("hidden", !info.overrides.length);
      ov.replaceChildren(el("div", { text: "These override files change the save folders for one core or game, so the setting here does not apply to them:" }),
        ...info.overrides.slice(0, 8).map((o) => el("div", { class: "mono small", text: o })));
      if (!this.filled) {
        this.filled = true;
        $("ra-save-dir").value = s.savefile_path || "";
        $("ra-state-dir").value = s.savestate_path || "";
        $("ra-sort-saves").checked = !!s.sort_savefiles_enable;
        $("ra-sort-states").checked = !!s.sort_savestates_enable;
      }
      $("ra-follow").checked = info.follow;
      state.raFollow = info.follow;
      $("ra-follow-panel").classList.remove("hidden");
      $("ra-bios-panel").classList.remove("hidden");
      if (!$("ra-bios-platform").options.length) {
        $("ra-bios-platform").replaceChildren(
          el("option", { value: "", text: "All systems that have a ROM folder" }), el("option", { value: "*all", text: "Every installed core" }),
          ...state.platforms.map((p) => el("option", { value: p.name, text: p.name })));
      }
      $("ra-backup").checked = info.backup.enabled;
      if (document.activeElement !== $("ra-backup-dir")) $("ra-backup-dir").value = info.backup.dir;
      $("ra-undo-btn").dataset.blocked = info.undo.length ? "0" : "1";
      Jobs.setRunning(Jobs.running);
    },

    body() {
      return { save_dir: $("ra-save-dir").value.trim(), state_dir: $("ra-state-dir").value.trim() || $("ra-save-dir").value.trim(),
        sort_saves: $("ra-sort-saves").checked, sort_states: $("ra-sort-states").checked, backup: $("ra-backup").checked };
    },

    async select(body) {
      try { this.info = await post("/api/retroarch/select", body); this.filled = false; this.render(); } catch (err) { toast(err.message, "error"); return; }
      await refreshSavesAwareness();
    },

    async plan() {
      try {
        const p = await post("/api/retroarch/plan", this.body());
        this.last = p;
        const c = p.counts;
        $("ra-output").classList.remove("hidden");
        $("ra-cards").replaceChildren(
          card(fmt(c.move + c.needs_core), "Files to move", c.move + c.needs_core ? "info" : "ok"),
          card(fmtBytes(p.bytes), "Size", ""),
          ...(c.ok ? [card(fmt(c.ok), "Already in place", "ok")] : []),
          ...(c.conflict ? [card(fmt(c.conflict), "In the way (left alone)", "bad")] : []),
          ...(c.needs_core ? [card(fmt(c.needs_core), "Need their core's folder", "warn")] : []));
        const notes = [...p.notes, ...(p.running ? ["RetroArch is running. Close it before applying."] : []),
          ...(this.body().backup && p.free !== null && p.bytes > p.free ? ["The backup folder does not have enough free space."] : [])];
        $("ra-notes").classList.toggle("hidden", !notes.length);
        $("ra-notes").replaceChildren(...notes.map((n) => el("div", { text: n })));
        const cfgRows = Object.entries(p.cfg_changes).map(([k, v]) => el("tr", {}, el("td", { text: "retroarch.cfg" }),
          el("td", { class: "mono", text: k }), el("td", { class: "mono", text: String(v) }), el("td", { class: "small muted", text: "setting" })));
        $("ra-table").replaceChildren(...cfgRows, ...p.items.map((i) => el("tr", {}, el("td", { text: i.kind === "state" ? "state" : "save" }),
          el("td", { class: "mono small", text: i.from }), el("td", { class: "mono small", text: i.to }), el("td", { class: "small muted", text: i.note || i.status }))));
        return p;
      } catch (err) { toast(err.message, "error"); return null; }
    },

    async apply() {
      const p = await this.plan();
      if (!p) return;
      if (p.empty) { toast("Nothing to change: the files and the config already match.", "ok"); return; }
      if (p.running) { toast("Close RetroArch first.", "error"); return; }
      const c = p.counts;
      const ok = await confirmDialog({
        title: "Move saves and update RetroArch",
        body: el("div", {},
          el("p", { text: `${fmt(c.move + c.needs_core)} file(s) (${fmtBytes(p.bytes)}) move to ${p.new.save}${p.new.state !== p.new.save ? ` and ${p.new.state}` : ""}.` }),
          el("ul", {},
            el("li", { text: this.body().backup ? "A zip backup of those files is made first." : "No backup: the files are only moved (Undo can move them back)." }),
            el("li", { text: "Nothing is overwritten. Folders left empty are removed." }),
            el("li", { text: "retroarch.cfg is backed up, then its save settings are changed." }),
            ...(c.needs_core ? [el("li", { text: `${fmt(c.needs_core)} file(s) have no core folder yet; RetroArch will not find them until they are in one.` })] : []))),
        okText: "Move and update" });
      if (!ok) return;
      await post("/api/retroarch/select", { backup: $("ra-backup").checked, backup_dir: $("ra-backup-dir").value.trim() }).catch(() => null);
      this.filled = false;
      Jobs.start("/api/retroarch/apply", this.body());
    },

    async sharedCheck(base) {
      try {
        const r = await post("/api/retroarch/shared", base ? { base } : {});
        if (!$("ra-shared-base").value.trim() || base === undefined) $("ra-shared-base").value = r.base;
        const LABEL = { ok: "already used", set: "points elsewhere", unset: "not set (RetroArch's own folder)", none: "no such folder here" };
        $("ra-shared-table").replaceChildren(...r.rows.map((x) => el("tr", {},
          el("td", {}, el("input", { type: "checkbox", "data-key": x.key, checked: x.status === "unset", disabled: !x.want || x.status === "ok",
            "aria-label": `Use the shared folder for ${x.label}` })),
          el("td", { text: x.label }), el("td", { class: "mono small", text: x.current_path || x.current || "-" }),
          el("td", { class: "mono small", text: x.want || "-" }),
          el("td", {}, badge(x.status === "ok" ? "ok" : x.status === "unset" ? "warn" : x.status === "set" ? "info" : "skip", LABEL[x.status]),
            x.status === "unset" && x.want_has_files && !x.current_has_files ? el("span", { class: "small muted", text: "  has files, unused" }) : null))));
        $("ra-shared-note").textContent = r.rows.some((x) => x.status === "unset" && x.want_has_files)
          ? "Ticked: folders that have files but that RetroArch is not using yet." : "";
        return r;
      } catch (err) { toast(err.message, "error"); return null; }
    },

    async sharedApply() {
      const keys = [...document.querySelectorAll("#ra-shared-table input[type=checkbox]:checked")].map((c) => c.dataset.key);
      if (!keys.length) { toast("Tick the folders to use.", "info"); return; }
      if (this.info && this.info.running) { toast("Close RetroArch first.", "error"); return; }
      if (!(await confirmDialog({ title: "Use shared folders", body: `Change ${keys.length} setting(s) in retroarch.cfg to the shared folders? The config is backed up first, files are not moved, and Undo restores the old settings.`, okText: "Change settings" }))) return;
      try {
        const r = await post("/api/retroarch/shared/apply", { base: $("ra-shared-base").value.trim(), keys });
        toast(`Changed ${r.changed.length} setting(s)`, "ok", 6000);
        await this.show();
        this.sharedCheck($("ra-shared-base").value.trim());
      } catch (err) { toast(err.message, "error"); }
    },

    biosBody() {
      return { platform: $("ra-bios-platform").value, search_dir: $("ra-bios-search").value.trim() };
    },

    biosCheck() {
      $("ra-bios-where").textContent = "Looking... this reads every ROM folder once and can take a minute for a large collection.";
      $("ra-bios-out").classList.remove("hidden");
      Jobs.start("/api/retroarch/bios/scan", this.biosBody());
    },

    showBios(r) {
      this.bios = r;
      const c = { ...r.counts };
      const keys = (r.keys && r.keys.rows) || [];
      c.found += keys.filter((k) => k.status === "found").length;
      this.biosFound = c.found;
      $("ra-bios-out").classList.remove("hidden");
      $("ra-bios-cards").replaceChildren(
        card(fmt(r.cores.length), "Cores", "info"), card(fmt(c.ok + c.present), "In place", "ok"),
        ...(c.wrong ? [card(fmt(c.wrong), "Wrong checksum", "bad")] : []),
        ...(c.found ? [card(fmt(c.found), "Found, not placed yet", "info")] : []),
        card(fmt(c.missing), "Missing", c.missing ? "warn" : ""),
        card(fmt(r.required_missing), "Required, not in place", r.required_missing ? "bad" : "ok"),
        ...(keys.length ? [card(fmt(keys.filter((k) => k.required && k.status !== "ok").length), "Key files not in place", keys.some((k) => k.required && k.status !== "ok") ? "bad" : "ok")] : []));
      $("ra-bios-where").textContent = `Cores of ${r.scope}. System folder: ${r.system_dir}. Searched ${r.searched.length} folder${r.searched.length === 1 ? "" : "s"}${r.searched.length ? `: ${r.searched.join(" \u00B7 ")}` : ""}.${r.complete ? "" : " The search stopped early (time limit): files found by checksum under another name may be missing from the list."}`;
      const LABEL = { ok: "verified", present: "there (no checksum to compare)", wrong: "there, wrong checksum", found: "found - not in place", missing: "missing" };
      const firmware = r.cores.flatMap((core) => core.firmware.map((f) => el("tr", {},
        el("td", { text: core.core, title: (core.serves || []).join(", ") }), el("td", { class: "mono small", text: f.path }), el("td", { text: f.optional ? "optional" : "required" }),
        el("td", {}, badge(f.status === "ok" || f.status === "present" ? "ok" : f.status === "found" ? "info" : f.status === "wrong" ? "bad" : f.optional ? "skip" : "warn", LABEL[f.status])),
        el("td", { class: "mono small", text: f.source || "" }))));
      const KEY_LABEL = { ok: "there", invalid: "there, but not a usable key file", found: "found - not in place (copied, never moved)", missing: "missing" };
      const keyRows = keys.map((k) => el("tr", {},
        el("td", { text: k.label, title: `The key file ${k.platform} needs` }), el("td", { class: "mono small", text: k.target }), el("td", { text: k.required ? "required" : "optional" }),
        el("td", {}, badge(k.status === "ok" ? "ok" : k.status === "found" ? "info" : k.status === "invalid" ? "bad" : k.required ? "warn" : "skip", KEY_LABEL[k.status])),
        el("td", { class: "mono small", text: k.source || "" })));
      $("ra-bios-table").replaceChildren(...firmware, ...keyRows);
    },

    async biosApply() {
      const r = this.bios;
      if (!r) { toast("Press Check first to see what can be placed.", "info"); return; }
      if (!this.biosFound) { toast("No file to place: nothing missing was found in the folders searched.", "info"); return; }
      const mode = $("ra-bios-mode").value;
      if (!(await confirmDialog({ title: "Place BIOS files", body: `${mode === "copy" ? "Copy" : "Move"} ${fmt(r.counts.found)} firmware file(s) into ${r.system_dir}${(r.keys && r.keys.rows.some((k) => k.status === "found")) ? ", and copy the key files found into the emulators' folders (key files are always copied)" : ""}? Nothing is overwritten; Undo puts the moved files back.`, okText: mode === "copy" ? "Copy" : "Move" }))) return;
      Jobs.start("/api/retroarch/bios/apply", { ...this.biosBody(), mode });
    },

    async undo() {
      const u = (this.info && this.info.undo) || [];
      if (!u.length) { toast("Nothing to undo.", "info"); return; }
      const what = { follow: "Give the saves their old names back (or remove the copies)?", bios: "Move the BIOS files back where they came from?",
        config: "Restore RetroArch's config from before? Close RetroArch first." }[u[0].kind]
        || "Move the saves back and restore RetroArch's config from before? Close RetroArch first.";
      if (!(await confirmDialog({ title: "Undo last change", body: `${fmt(u[0].files)} file(s). ${what}`, okText: "Undo", danger: true }))) return;
      try {
        const r = await post("/api/retroarch/undo", { journal: u[0].journal });
        toast(`Moved ${fmt(r.restored)} file(s) back${r.cfg_restored ? " and restored the config" : ""}${r.skipped.length ? `; ${fmt(r.skipped.length)} left` : ""}`, r.skipped.length ? "error" : "ok", 8000);
        this.filled = false;
        await this.show();
      } catch (err) { toast(err.message, "error"); }
    },

    bind() {
      $("ra-install").addEventListener("change", () => this.select({ cfg: $("ra-install").value }));
      $("ra-custom-add").addEventListener("click", () => {
        const v = $("ra-custom").value.trim();
        if (v) this.select({ custom: v }); else toast("Enter the RetroArch folder, or the path of its retroarch.cfg, first.", "info");
      });
      $("ra-custom-browse").addEventListener("click", () => FolderBrowser.open($("ra-custom"), "Choose the RetroArch folder"));
      $("ra-save-browse").addEventListener("click", () => FolderBrowser.open($("ra-save-dir"), "Choose the folder for save files"));
      $("ra-state-browse").addEventListener("click", () => FolderBrowser.open($("ra-state-dir"), "Choose the folder for save states"));
      $("ra-backup-browse").addEventListener("click", () => FolderBrowser.open($("ra-backup-dir"), "Choose the backup folder"));
      $("ra-follow").addEventListener("change", async () => { try { this.info = await post("/api/retroarch/follow", { follow: $("ra-follow").checked }); state.raFollow = this.info.follow; syncFollowBoxes(); } catch (err) { toast(err.message, "error"); } });
      $("ra-shared-check").addEventListener("click", () => this.sharedCheck($("ra-shared-base").value.trim()));
      $("ra-shared-apply").addEventListener("click", () => this.sharedApply());
      $("ra-shared-browse").addEventListener("click", () => FolderBrowser.open($("ra-shared-base"), "Choose the folder that holds the shared assets"));
      $("ra-bios-check").addEventListener("click", () => this.biosCheck());
      $("ra-bios-apply").addEventListener("click", () => this.biosApply());
      $("ra-bios-browse").addEventListener("click", () => FolderBrowser.open($("ra-bios-search"), "Choose a folder to look for BIOS files in"));
      $("ra-plan-btn").addEventListener("click", () => this.plan());
      $("ra-apply-btn").addEventListener("click", () => this.apply());
      $("ra-undo-btn").addEventListener("click", () => this.undo());
    },
  };

  Jobs.handlers.retroarch = async (job) => {
    if ((job.status === "done" || job.status === "cancelled") && job.result) {
      const r = job.result;
      if (r.action === "retroarch_bios_check") { RetroArch.showBios(r); return; }
      if (r.action === "retroarch_bios") {
        toast(`Placed ${fmt(r.placed)} file(s)${r.failed.length ? `, ${fmt(r.failed.length)} failed` : ""}`, r.failed.length ? "error" : "ok", 8000);
        showFailures("ra-failures", "Could not place:", r.failed, (f) => `${f.path}: ${f.error}`);
        await RetroArch.load();
        RetroArch.render();
        RetroArch.bios = null;
        RetroArch.biosCheck();
        return;
      }
      toast(`Moved ${fmt(r.moved)} file(s)${r.cfg_backup ? " and updated retroarch.cfg" : ""}${r.failed.length ? `, ${fmt(r.failed.length)} failed` : ""}`, r.failed.length ? "error" : "ok", 9000);
      showFailures("ra-failures", "Could not move:", r.failed, (f) => `${f.path}: ${f.error}`,
        [r.failed.length ? "The RetroArch config was not changed." : ""]);
    }
    RetroArch.filled = false;
    await RetroArch.show();
    $("ra-output").classList.add("hidden");
  };

  // ------------------------------------------------------------------ Nintendo Switch: games and the saves of Eden and Ryujinx
  /** The Switch's option "also check every file's checksums when scanning" (its own stored setting). */
  const Switch = {
    async load() {
      const p = currentPlatform();
      const row = $("sw-verify-row");
      row.classList.toggle("hidden", !(p && p.name === "Nintendo Switch"));
      if (row.classList.contains("hidden")) return;
      try { $("sw-verify-scan").checked = !!(await get("/api/switch")).config.verify_scan; } catch (err) { toast(err.message, "error"); }
    },

    bind() {
      $("sw-verify-scan").addEventListener("change", async (e) => {
        const box = e.target;
        try {
          await post("/api/switch/config", { verify_scan: box.checked });
          toast(box.checked ? "The next scan checks every file's checksums" : "Scans no longer check the checksums", "ok");
        } catch (err) { box.checked = !box.checked; toast(err.message, "error"); }
      });
    },
  };

  // ------------------------------------------------------------------ the files of a system: one tree, with filters
  // What the system's ROM folder and its part of the archive hold after a build with the current rules (a preview: nothing is
  // moved here). A folder is asked for when it is opened (GET /api/tree: one level, with the totals below it and the numbers of
  // every filter's choices), so a library of tens of thousands of files opens at once. A file opens into what is known about it.
  const TREE_STATES = { same: ["", "unchanged"], renamed: ["move", "renamed"], moved: ["move", "moved"], new: ["ok", "new"],
    existing: ["", "already archived"], ignored: ["skip", "not looked at"], incoming: ["warn", "set aside"], copy: ["info", "copy"],
    saves: ["info", "saves"], missing: ["missing", "missing"], saveonly: ["info", "saves only"] };
  const FILES_SHOW = [["", "Everything"], ["have", "Games you have"], ["missing", "Missing"], ["bios", "BIOS and firmware"], ["unmatched", "Unmatched"], ["saves", "Saves"]];
  const FILES_STATES = ["renamed", "moved", "new", "incoming", "copy", "saves", "existing", "ignored"];
  const FILES_MORE = 10;          // region / language choices shown before "More"
  const Files = {
    side: "library",
    f: { show: "have", state: "", region: "", language: "", rated: "", q: "" },
    open: { library: new Set(), archive: new Set() },      // the folders that are open: they open again when the tree is drawn again
    more: {},
    seq: 0,
    upTimer: 0,
    upTries: 0,
    shown: "",

    reset() {
      this.side = "library";
      this.f = { show: "have", state: "", region: "", language: "", rated: "", q: "" };
      this.open = { library: new Set(), archive: new Set() };
      this.shown = "";
      $("files-q").value = "";
    },

    /** The filters of an address (``?show=missing``): a link from a tile or a card. */
    applyParams(params) {
      if (!params) return;
      const show = params.show !== undefined ? params.show : (params.view !== undefined ? (OLD_VIEWS[params.view] || "") : undefined);
      if (show !== undefined && FILES_SHOW.some(([k]) => k === show) && show !== this.f.show) { this.f.show = show; this.shown = ""; }
      if (params.side === "archive" || params.side === "library") { if (params.side !== this.side) this.shown = ""; this.side = params.side; }
    },

    /** Draw the tree when the page is opened, unless what is on screen is still right. */
    show() {
      const p = currentPlatform();
      if (!p || !state.scan || !libPlan) return;
      const key = `${p.name}|${libPlan.plan_id}|${this.side}|${JSON.stringify(this.f)}`;
      if (key !== this.shown) this.reload();
    },

    params(path, offset) {
      return qs({ platform: state.platform, side: this.side, path, offset: offset || undefined, ...this.f });
    },

    /** Ask for the top of the tree again (the filters, the side or the plan changed) and draw it, the open folders open again. */
    async reload(again) {
      const p = currentPlatform();
      if (!p || !state.scan || !libPlan) return;
      if (!again) this.upTries = 0;
      const seq = ++this.seq;
      const box = $("files-tree");
      this.shown = `${p.name}|${libPlan.plan_id}|${this.side}|${JSON.stringify(this.f)}`;
      box.classList.add("tree-busy");
      let doc;
      try {
        doc = await get(`/api/tree?${this.params("", 0)}`);
      } catch (err) {
        if (seq !== this.seq) return;
        box.classList.remove("tree-busy");
        box.replaceChildren(el("div", { class: "error-text", text: `Could not work out the files: ${err.message}` }));
        this.shown = "";
        return;
      }
      if (seq !== this.seq) return;                      // a later change is already on its way
      box.classList.remove("tree-busy");
      if (!doc.ready) { box.replaceChildren(el("div", { class: "muted", text: doc.reason || "Not available." })); return; }
      this.drawSides(doc);
      this.drawChips(doc.facets || {});
      const t = doc.totals || { files: 0, bytes: 0, changed: 0, missing: 0 };
      $("files-where").textContent = doc.none ? "" : doc.root;
      $("files-totals").textContent = doc.none
        ? "No archive folder is set (Settings > Archive folder): what the rules set aside stays in the ROM folder, in _excluded, _superseded ..."
        : `${fmt(t.files)} ${t.files === 1 ? "file" : "files"} · ${fmtBytes(t.bytes)}${t.missing ? ` · ${fmt(t.missing)} missing` : ""}`
          + `${t.changed ? ` · ${fmt(t.changed)} changed by the build` : ""}${this.filtered() ? " (with these filters)" : ""}`;
      box.replaceChildren(...this.rows(doc, ""));
      if (!box.childElementCount) box.replaceChildren(el("div", { class: "muted", text: this.filtered() ? "Nothing matches these filters." : "Nothing here." }));
      clearTimeout(this.upTimer);                        // the arrows need the totals: ask again until they are worked out
      if (doc.upgrades_pending && ++this.upTries < 12) this.upTimer = setTimeout(() => { if (seq === this.seq) this.reload(true); }, 1500);
      else if (!doc.upgrades_pending) this.upTries = 0;
    },

    filtered() { return Object.entries(this.f).some(([k, v]) => v && !(k === "show" && v === "have")); },

    set(key, value) {
      this.f[key] = this.f[key] === value ? "" : value;
      this.reload();
    },

    /** ROM folder | Archive. */
    drawSides(doc) {
      const n = doc.sides || {};
      $("files-sides").replaceChildren(...[["library", "ROM folder"], ["archive", "Archive"]].map(([side, label]) => el("button", {
        class: `tab ${side === this.side ? "active" : ""}`, role: "tab", type: "button", "data-side": side,
        "aria-selected": side === this.side ? "true" : "false", tabindex: side === this.side ? "0" : "-1",
        on: { click: () => { if (this.side !== side) { this.side = side; this.reload(); } } } },
      label, el("span", { class: "count", text: `(${fmt(n[side] || 0)})` }))));
    },

    /** The filters, each choice with the number of files it would leave. */
    drawChips(facets) {
      const row = (label, key, choices, any) => {
        if (!choices.length) return null;
        const chips = [];
        if (any) chips.push(el("button", { class: `chip chip-sm ${this.f[key] ? "" : "active"}`, type: "button", text: any, on: { click: () => { if (this.f[key]) this.set(key, this.f[key]); } } }));
        for (const [value, text, n] of choices) {
          chips.push(el("button", { class: `chip chip-sm ${this.f[key] === value ? "active" : ""}`, type: "button", "data-filter": key, "data-value": value,
            text: `${text} (${fmt(n)})`, on: { click: () => this.set(key, value) } }));
        }
        return el("div", { class: "tag-filter-row" }, el("span", { class: "tag-filter-label", text: label }), el("div", { class: "chips" }, chips));
      };
      const top = (key, counts) => {
        let list = Object.entries(counts || {}).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
        const all = list.length;
        if (!this.more[key]) list = list.slice(0, FILES_MORE);
        if (this.f[key] && !list.some(([v]) => v === this.f[key])) list.push([this.f[key], (counts || {})[this.f[key]] || 0]);
        const out = list.map(([v, n]) => [v, v, n]);
        const extra = all > FILES_MORE ? el("button", { class: "chip chip-sm chip-more", type: "button", text: this.more[key] ? "Fewer" : `More (${fmt(all - FILES_MORE)})`,
          on: { click: () => { this.more[key] = !this.more[key]; this.drawChips(facets); } } }) : null;
        return [out, extra];
      };
      const show = facets.show || {};
      const showRow = el("div", { class: "tag-filter-row" }, el("span", { class: "tag-filter-label", text: "Show" }), el("div", { class: "chips" },
        FILES_SHOW.filter(([k]) => !k || show[k] || this.f.show === k).map(([k, text]) => el("button", { class: `chip chip-sm ${this.f.show === k ? "active" : ""}`, type: "button",
          "data-filter": "show", "data-value": k, text: `${text} (${fmt(show[k] || 0)})`, on: { click: () => { if (this.f.show !== k) { this.f.show = k; this.reload(); } } } }))));
      const st = facets.state || {};
      const states = FILES_STATES.filter((k) => st[k] || this.f.state === k).map((k) => [k, TREE_STATES[k][1], st[k] || 0]);
      const [regions, moreR] = top("region", facets.region);
      const [langs, moreL] = top("language", facets.language);
      const rated = facets.rated || {};
      const regionRow = row("Region", "region", regions, "Any");
      const langRow = row("Language", "language", langs, "Any");
      if (regionRow && moreR) regionRow.lastChild.append(moreR);
      if (langRow && moreL) langRow.lastChild.append(moreL);
      $("files-chips").replaceChildren(...[showRow, row("The build", "state", states, "Anything"), regionRow, langRow,
        ratingsAvailable() && (rated["1"] || rated["0"]) ? row("Rating", "rated", [["1", "Rated", rated["1"] || 0], ["0", "Unrated", rated["0"] || 0]], "Rated or not") : null,
      ].filter(Boolean));
      $("files-clear").classList.toggle("hidden", !this.filtered());
    },

    /** The rows of one folder: its sub-folders, then its files. */
    rows(doc, path) {
      const rows = [];
      if (!doc.offset) for (const d of doc.dirs) rows.push(this.folderNode(d, path ? `${path}/${d.name}` : d.name));
      for (const f of doc.files) rows.push(this.fileNode(f));
      const shown = doc.offset + doc.files.length;
      if (shown < doc.total_files) {
        rows.push(el("button", { class: "btn btn-small btn-ghost tree-more", type: "button", text: `Show more (${fmt(doc.total_files - shown)} more)`,
          on: { click: async (ev) => {
            const btn = ev.currentTarget;
            btn.disabled = true;
            try {
              const more = await get(`/api/tree?${this.params(path, shown)}`);
              btn.replaceWith(...this.rows(more, path));
            } catch (err) { btn.disabled = false; toast(err.message, "error"); }
          } } }));
      }
      return rows;
    },

    /** A folder row with its (lazily loaded) children. */
    folderNode(info, path) {
      const kids = el("div", { class: "tree-kids hidden", role: "group" });
      const caret = el("span", { class: "tree-caret", "aria-hidden": "true", text: "▸" });
      const meta = `${info.files ? `${fmt(info.files)} ${info.files === 1 ? "file" : "files"} · ${fmtBytes(info.bytes)}` : ""}`
        + `${info.missing ? `${info.files ? " · " : ""}${fmt(info.missing)} missing` : ""}${info.changed ? ` · ${fmt(info.changed)} changed` : ""}`;
      const head = el("button", { class: "tree-row tree-dir", type: "button", role: "treeitem", "aria-expanded": "false", "data-tree-path": path },
        caret, el("span", { class: "tree-name", text: info.name }),
        info.upgrades ? el("span", { class: "tree-upgrade", title: `${fmt(info.upgrades)} ${info.upgrades === 1 ? "game" : "games"} below ${info.upgrades === 1 ? "has" : "have"} a better version`,
          "aria-label": "Better versions exist below", text: `⬆ ${fmt(info.upgrades)}` }) : null,
        el("span", { class: "tree-meta muted small", text: meta }));
      const open = this.open[this.side];
      let loaded = false;
      const set = async (on) => {
        kids.classList.toggle("hidden", !on);
        head.setAttribute("aria-expanded", on ? "true" : "false");
        caret.textContent = on ? "▾" : "▸";
        if (on) open.add(path); else open.delete(path);
        if (!on || loaded) return;
        loaded = true;
        kids.replaceChildren(el("div", { class: "totals-wait" }, el("span", { class: "spinner", "aria-hidden": "true" }), " loading..."));
        try {
          const doc = await get(`/api/tree?${this.params(path, 0)}`);
          kids.replaceChildren(...this.rows(doc, path));
        } catch (err) {
          loaded = false;
          open.delete(path);
          kids.replaceChildren(el("div", { class: "error-text small", text: `Could not load: ${err.message}` }));
        }
      };
      head.addEventListener("click", () => set(kids.classList.contains("hidden")));
      if (open.has(path)) set(true);
      return el("div", { class: "tree-node" }, head, kids);
    },

    /** A file row; one that belongs to a row of the build list (a game's file, a missing game) opens into what is known about it. */
    fileNode(f) {
      const [cls, label] = TREE_STATES[f.state] || ["", f.state];
      const i = f.row;
      const plain = f.state === "same";
      const tags = i && i.tags ? tagChips(i.tags) : null;
      const caret = el("span", { class: "tree-caret", "aria-hidden": "true", text: i ? "▸" : "" });
      const cells = [caret, el("span", { class: `tree-name ${f.state === "missing" ? "" : "mono-path"}`, text: f.name }), tags,
        i && i.bios ? badge("info", "BIOS") : null,
        f.upgrade ? el("span", { class: "tree-upgrade", title: "A better version of this game exists (open the file to see it)", "aria-label": "A better version exists", text: "⬆" }) : null,
        plain ? null : badge(cls || "skip", label),
        i && i.saves && i.saves.total ? el("span", { class: "tree-saves small", title: "Saves and save states in the emulator's folders", text: savesText(i.saves) }) : null,
        f.state === "missing" || f.state === "saveonly" ? null : el("span", { class: "tree-meta muted small", text: fmtBytes(f.size) })];
      if (!i) {
        return el("div", { class: `tree-row tree-file state-${f.state}`, role: "treeitem", "data-state": f.state, title: f.from ? `from ${f.from}` : "" }, ...cells);
      }
      const panel = el("div", { class: "tree-detail hidden" });
      const head = el("button", { class: `tree-row tree-file state-${f.state}`, type: "button", role: "treeitem", "aria-expanded": "false", "data-state": f.state,
        title: f.from ? `from ${f.from}` : "" }, ...cells);
      head.addEventListener("click", () => {
        const on = panel.classList.contains("hidden");
        panel.classList.toggle("hidden", !on);
        head.setAttribute("aria-expanded", on ? "true" : "false");
        caret.textContent = on ? "▾" : "▸";
        if (on && !panel.firstChild) this.detail(panel, f);
      });
      return el("div", { class: "tree-node" }, head, panel);
    },

    /** What is known about a file: its game, why the rules keep or drop it, what the build does, its saves, the checksums, your choice. */
    async detail(panel, f) {
      const i = f.row;
      const line = (label, ...content) => (content.some(Boolean) ? [el("dt", { text: label }), el("dd", {}, ...content.filter(Boolean))] : []);
      const game = i.item === "game" ? i.name : (i.rom_name || (i.game_ref && i.game_ref.game) || "");
      const build = f.state === "saveonly" ? "No game file here: these are only its saves." : f.state === "missing" ? "Not in your folder."
        : f.state === "same" ? "Leaves it where it is."
          : f.state === "incoming" ? `Sets it aside: it goes to the archive${f.from ? ` from ${f.from}` : ""}.`
            : `${TREE_STATES[f.state] ? TREE_STATES[f.state][1] : f.state}${f.from ? ` (from ${f.from})` : ""}.`;
      const sums = el("div", { class: "cs-sums" });
      const actions = i.game_ref && i.item === "file" ? el("div", { class: "row" },
        el("button", { class: "btn btn-small", type: "button", text: "Always keep", title: "Keep this game whatever the rules say", on: { click: () => setOverrides([i], "keep") } }),
        el("button", { class: "btn btn-small", type: "button", text: "Always exclude", title: "Set this game aside whatever the rules say", on: { click: () => setOverrides([i], "exclude") } }),
        el("button", { class: "btn btn-small btn-ghost", type: "button", text: "Back to the rules", title: "Remove your choice for this game", on: { click: () => setOverrides([i], "clear") } })) : null;
      panel.replaceChildren(el("dl", { class: "tree-facts" },
        ...line("Game", game ? copyChip(game, "", "name") : null, i.title && i.title !== game ? el("span", { class: "muted small", text: ` ${i.title}` }) : null,
          i.dat ? el("div", { class: "sub", text: shortDat(i.dat) }) : null),
        ...line("Rating", ratingsAvailable() && i.rating !== undefined ? ratingCell(i) : null),
        ...line("The rules", i.item === "file" ? libraryWhyPanel(i) : null),
        ...line("The build", el("span", { text: build })),
        ...line("Saves", i.saves ? libSavesCell(i) : null),
        ...line("Year", i.year ? el("span", { text: String(i.year) }) : null)),
      sums, actions);
      if (i.category === "kept" && i.item === "file") {
        const rules = panel.querySelector(".tree-facts");
        Better.of(i).then((it) => {
          if (!it || !it.better.length || !rules.isConnected) return;
          rules.append(el("dt", { text: "A better match" }), el("dd", {},
            el("div", { class: "small", text: "Your rules prefer another version of this game, which is not in your folder:" }),
            ...it.better.map((b) => el("div", {}, copyChip(b.name, "", "name"),
              ...b.files.flatMap((f) => [f.crc ? el("div", {}, copyChip(f.crc, "CRC32")) : null, f.md5 ? el("div", {}, copyChip(f.md5, "MD5")) : null,
                f.sha1 ? el("div", {}, copyChip(f.sha1, "SHA-1")) : null])))));
        });
      }
      if (i.cs_kind === null || i.cs_kind === undefined || i.cs_id === null || i.cs_id === undefined) return;
      sums.replaceChildren(el("span", { class: "muted small", text: "Loading checksums..." }));
      try {
        const payload = i.checksums || await get(`/api/scan/checksums?${qs({ kind: i.cs_kind, id: i.cs_id })}`);
        i.checksums = payload;
        sums.replaceChildren(checksumPanel(payload));
      } catch (err) {
        sums.replaceChildren(el("span", { class: "error-text small", text: err.message }));
      }
    },

    bind() {
      const search = debounce(() => { this.f.q = $("files-q").value.trim(); this.reload(); }, 350);
      $("files-q").addEventListener("input", search);
      $("files-clear").addEventListener("click", () => {
        this.f = { show: "have", state: "", region: "", language: "", rated: "", q: "" };
        $("files-q").value = "";
        this.reload();
      });
      $("saves-box").addEventListener("toggle", () => { if ($("saves-box").open) showSavesTable(); });
      tabKeys($("files-sides"));
    },
  };

  // ------------------------------------------------------------------ settings for every system
  const Settings = {
    ratingsTimer: 0,

    /** The Ratings panel: what LaunchBox and the user's IGDB key have given; polled while a download runs. */
    async loadRatings() {
      let r;
      try { r = await get("/api/ratings"); } catch (err) { return; }
      const lb = $("set-ratings-lb"), ig = r.igdb || {};
      const lbText = r.status === "absent" ? "LaunchBox ratings: not downloaded yet."
        : r.status === "updating" ? "LaunchBox ratings: downloading..."
          : `LaunchBox ratings: data of ${r.installed || "?"}${r.games ? `, ${fmt(r.games)} titles` : ""}${r.status === "update_available" ? " - an update is on its way" : ""}.`;
      const note = (r.missing_platforms || []).length ? ` It lacks ${r.missing_platforms.join(", ")}: it is downloaded again.` : "";
      lb.textContent = lbText + note + (r.error ? ` ${r.error}` : "");
      const idBox = $("igdb-id");
      if (document.activeElement !== idBox && !idBox.value) idBox.value = ig.client_id || "";
      if (ig.has_secret && !$("igdb-secret").value) $("igdb-secret").placeholder = "Client secret (kept: type a new one to change it)";
      const busy = ig.updating || r.status === "updating";
      $("igdb-status").textContent = ig.error ? `IGDB: ${ig.error}`
        : ig.updating ? "IGDB: downloading the ratings of every system..."
          : ig.installed ? `IGDB ratings: ${fmt(ig.games)} titles, made ${String(ig.built_at || "").slice(0, 10)}.`
            : ig.configured ? "IGDB: key saved; the ratings are not downloaded yet." : "No IGDB key.";
      $("igdb-remove").disabled = !ig.configured;
      clearTimeout(this.ratingsTimer);
      if (busy && $("view-settings") && !$("view-settings").classList.contains("hidden")) {
        this.ratingsTimer = setTimeout(() => this.loadRatings(), 2500);
      }
    },

    async saveIgdb(remove = false) {
      const body = remove ? { client_id: "", client_secret: "" } : { client_id: $("igdb-id").value.trim(), client_secret: $("igdb-secret").value.trim() };
      try {
        await post("/api/ratings/igdb", body);
        if (remove) { $("igdb-id").value = ""; $("igdb-secret").value = ""; $("igdb-secret").placeholder = "Client secret"; }
        else $("igdb-secret").value = "";
        toast(remove ? "The IGDB key is removed" : "The IGDB key is saved; the ratings are being downloaded", "ok");
        Updates.load();
      } catch (err) { toast(err.message, "error"); }
      this.loadRatings();
    },

    async testIgdb() {
      $("igdb-status").textContent = "Testing the key...";
      try {
        const r = await post("/api/ratings/igdb/test", { client_id: $("igdb-id").value.trim(), client_secret: $("igdb-secret").value.trim() });
        toast(r.message, r.ok ? "ok" : "error", 6000);
        $("igdb-status").textContent = r.message;
      } catch (err) { toast(err.message, "error"); this.loadRatings(); }
    },

    tiles: { timer: 0, ui: null, info: null },

    /** The Tiles panel: one ticked, orderable list of every tile; each change is saved after a short pause. */
    async loadTiles() {
      let info;
      try { info = await get("/api/settings/tiles"); } catch (err) { $("set-tiles-list").textContent = `The tiles are not available: ${err.message}`; return; }
      this.tiles.info = info;
      const labels = Object.fromEntries(info.catalog.map((t) => [t.id, t.label]));
      const descs = Object.fromEntries(info.catalog.map((t) => [t.id, t.text]));
      const ui = sortableList({ items: info.order, label: "Tiles of the system pages", noun: "tile", checks: info.shown, maxChecks: info.max, labels, descs,
        onLimit: () => toast(`At most ${info.max} tiles: clear one first`, "info"),
        onCommit: (order, _before, on) => this.saveTiles(order, on) });
      ui.root.querySelector(".sortable-bar").classList.add("hidden");
      ui.root.querySelector(".sortable-hint").textContent = "Tick a tile to show it. Drag the handle (or use the arrows) to put the ticked ones in order.";
      this.tiles.ui = ui;
      $("set-tiles-list").replaceChildren(ui.root);
      this.tilesNote(info.shown.length, info.max);
    },

    tilesNote(n, max) { $("set-tiles-note").textContent = `${n} of ${max} tiles shown`; },

    saveTiles(order, on) {
      const shown = order.filter((id) => on.includes(id));
      this.tilesNote(shown.length, this.tiles.info.max);
      state.tiles = shown;                          // (the pages change at once; the save follows)
      Totals.shown[""] = "";
      renderTotals();
      clearTimeout(this.tiles.timer);
      this.tiles.timer = setTimeout(async () => {
        try { const out = await post("/api/settings/tiles", { shown }); state.tiles = out.shown; loadInsights(); } catch (err) { toast(err.message, "error"); this.loadTiles(); }
      }, 250);
    },

    async resetTiles() {
      try {
        const out = await post("/api/settings/tiles", { reset: true });
        state.tiles = out.shown;
        Totals.shown[""] = "";
        await this.loadTiles();
      } catch (err) { toast(err.message, "error"); }
    },

    show() {
      this.loadRatings();
      this.loadTiles();
      this.render();
      get("/api/library/defaults").then((d) => { state.libDefaults = d; renderDefaultsRules(); })
        .catch((err) => $("set-library-rules").replaceChildren(el("div", { class: "muted", text: `Your defaults are not available: ${err.message}` })));
    },

    render() {
      const a = (state.status && state.status.archive) || { dir: "", overrides: {} };
      $("set-archive-dir").value = a.dir || "";
      const p = currentPlatform();
      $("set-archive-default").textContent = a.dir ? "Every system archives to this folder, unless it has its own (below)."
        : "Not set: nothing is archived. What the rules leave out stays in each system's folder (_excluded, _superseded ...) Choose a folder to turn archiving on.";
      const own = Object.entries(a.overrides || {}).sort(([x], [y]) => x.localeCompare(y));
      $("set-archive-own-box").classList.toggle("hidden", !own.length);
      $("set-archive-own").replaceChildren(...own.map(([name, dir]) => el("li", {},
        el("b", { text: name }), " ", el("code", { text: dir }), " ",
        el("button", { class: "btn btn-small btn-ghost", text: "Use the common folder", on: { click: () => this.save("", name) } }))));
    },

    async save(dir, platform = "") {
      try {
        state.status.archive = await post("/api/settings/archive", { dir, ...(platform ? { platform } : {}) });
        this.render();
        syncAsideDir();
        if (Previews.has("lib")) resetLibraryPreview();
        toast(platform ? `${platform} uses the common archive folder` : dir ? "Archive folder saved" : "Archive folder: back to the default", "ok");
      } catch (err) { toast(err.message, "error"); }
    },

    bind() {
      $("igdb-save").addEventListener("click", () => this.saveIgdb());
      $("igdb-test").addEventListener("click", () => this.testIgdb());
      $("igdb-remove").addEventListener("click", () => this.saveIgdb(true));
      $("set-archive-dir").addEventListener("change", () => this.save($("set-archive-dir").value.trim()));
      $("set-archive-browse").addEventListener("click", () => FolderBrowser.open($("set-archive-dir"), "Choose the archive folder"));
    },
  };

  // ----------------------------------------------------------- shared UI
  function updateActionState() {
    const hasScan = !!state.scan;
    const canScan = !!state.platform && !!folderOf(currentPlatform()).trim();
    $("scan-btn").dataset.blocked = canScan ? "0" : "1";
    for (const id of ["lib-plan-btn"]) $(id).dataset.blocked = hasScan ? "0" : "1";
    if (!hasScan) for (const id of ["lib-undo-btn"]) $(id).dataset.blocked = "1";
    if (!hasScan || libPlan === null) $("lib-apply-btn").dataset.blocked = hasScan ? "0" : "1";
    // Apply buttons are enabled once a scan exists (until a preview shows there is
    // nothing to do); the confirmation step re-checks the counts anyway.
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

  function bindMoreMenu() {
    const btn = $("more-btn"), pop = $("more-pop");
    const close = () => { pop.classList.add("hidden"); btn.setAttribute("aria-expanded", "false"); };
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = pop.classList.contains("hidden");
      pop.classList.toggle("hidden", !open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    pop.addEventListener("click", close);
    document.addEventListener("click", (e) => { if (!$("more-menu").contains(e.target)) close(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  }

  function bind() {
    bindMoreMenu();
    initSide();
    for (const id of ["scan-btn", "scan-force-btn", "lib-plan-btn", "lib-apply-btn", "lib-undo-btn"]) {
      $(id).setAttribute("data-needs-idle", "");
    }
    $("updates-btn").addEventListener("click", () => Updates.check());
    $("updates-retry").addEventListener("click", () => Updates.check());
    $("scan-retry").addEventListener("click", () => { $("scan-error").classList.add("hidden"); startScan(); });
    window.addEventListener("focus", () => { if (!Updates.wasRunning) Updates.load(); });
    Previews.bindAll();
    $("lib-vanish-box").addEventListener("toggle", () => { if ($("lib-vanish-box").open && !vanishTable && libPlan) showVanishTable(); });
    RetroArch.bind();
    $("lib-apply-btn").addEventListener("click", applyLibrary);
    $("set-tiles-reset").addEventListener("click", () => Settings.resetTiles());
    Files.bind();
    $("lib-undo-btn").addEventListener("click", undoLibrary);
    for (const id of ["lib-where-here", "lib-where-other", "lib-export-mode", "lib-export-sidecars", "lib-export-sync"]) $(id).addEventListener("change", exportSettingsChanged);
    $("lib-export-dest").addEventListener("change", exportSettingsChanged);
    $("lib-aside-on").addEventListener("change", asideChanged);
    $("lib-aside-dir").addEventListener("change", archiveFieldChanged);
    $("lib-aside-dir").addEventListener("input", renderAsideWhere);
    $("lib-aside-browse").addEventListener("click", () => FolderBrowser.open($("lib-aside-dir"), "Choose the folder for the archived files"));
    $("lib-export-browse-btn").addEventListener("click", () => FolderBrowser.open($("lib-export-dest"), "Choose the folder to build the library in"));
    $("library-reset").addEventListener("click", async () => {
      try {
        adoptProfile(state.platform, await post("/api/library/profile", { platform: state.platform, reset: true }));
        renderLibraryRules();
      } catch (err) { toast(err.message, "error"); }
    });
    $("set-library-reset").addEventListener("click", async () => {
      try {
        state.libDefaults = await post("/api/library/defaults", { reset: true });
        defaultsChanged();
        renderDefaultsRules();
      } catch (err) { toast(err.message, "error"); }
    });
    // a click or key inside a rules panel makes it the one the shared controls save to
    for (const [id, ctx] of [["library-rules", platformRules], ["set-library-rules", defaultsRules]]) {
      for (const ev of ["pointerdown", "focusin", "keydown"]) $(id).addEventListener(ev, () => { rctx = ctx; }, true);
    }
    for (const id of ["lib-labels", "lib-savedisk"]) {
      $(id).addEventListener("change", () => Previews.stale("lib", "Options changed since this preview"));
    }
    $("fb-up").addEventListener("click", () => FolderBrowser.parent && FolderBrowser.list(FolderBrowser.parent));
    $("fb-hidden").addEventListener("change", () => FolderBrowser.current && FolderBrowser.list(FolderBrowser.current));
    $("fb-choose").addEventListener("click", () => FolderBrowser.choose());
    Settings.bind();
    Switch.bind();
    $("scan-btn").addEventListener("click", () => startScan(false));
    $("scan-force-btn").addEventListener("click", () => startScan(true));
    bindFolderField();
    for (const tab of TABS) $(`tabbtn-${tab}`).addEventListener("click", () => openTab(tab));
    tabKeys($("sys-tabs"));
    window.addEventListener("hashchange", applyRoute);
    $("chdman-save").addEventListener("click", () => saveChdman({ path: $("chdman-path").value.trim() }));
    $("chdman-refresh").addEventListener("click", () => loadChdman(true));
    $("chdman-engine").addEventListener("change", (e) => saveChdman({ engine: e.target.value }));
    $("chd-writer").addEventListener("change", (e) => saveChdman({ writer: e.target.value }));
    $("chd-preset").addEventListener("change", (e) => saveChdman({ preset: e.target.value }));
    $("lib-convert").addEventListener("change", saveConvertOption);
    $("chd-verify-scan").addEventListener("change", (e) => saveChdman({ verify_scan: e.target.checked }));
    $("quit-btn").addEventListener("click", quit);
    $("databases-check").addEventListener("click", () => $("updates-btn").click());
    document.addEventListener("click", (e) => {
      for (const m of document.querySelectorAll(".col-menu[open]")) if (!m.contains(e.target)) m.open = false;
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") for (const m of document.querySelectorAll(".col-menu[open]")) m.open = false;
    });
    const dense = $("density-btn");
    const applyDensity = () => { document.body.classList.toggle("dense", Prefs.dense); dense.setAttribute("aria-pressed", Prefs.dense ? "true" : "false"); dense.textContent = Prefs.dense ? "Roomy rows" : "Compact rows"; };
    dense.addEventListener("click", () => { Prefs.dense = !Prefs.dense; applyDensity(); });
    applyDensity();
  }

  /** The single folder field of the library page: edits are saved right away (no Save button to forget). */
  function bindFolderField() {
    const input = $("folder-input");
    input.addEventListener("input", syncFolder);
    // `change` = the field lost focus / Enter after an edit, and what Browse / Folders dispatch
    input.addEventListener("change", async () => {
      const p = currentPlatform();
      if (!p) return;
      syncFolder();
      await commitFolder(p);
      paintFolder();
    });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") startScan(); });
    $("folder-clear-btn").addEventListener("click", async () => {
      const p = currentPlatform();
      if (!p) return;
      input.value = "";
      syncFolder();
      await commitFolder(p);
      paintFolder();
    });
    $("folder-browse-btn").addEventListener("click", () => {
      const p = currentPlatform();
      if (p) FolderBrowser.open(input, `Choose the ${p.name} folder`);
    });
  }

  async function init() {
    bind();
    await loadStatus();
    const s = state.status || {};
    state.platform = (s.scan && s.scan.platform) || s.last_platform || s.default_platform || null;
    if (state.platform) Prefs.restore(state.platform);
    await loadPlatforms();
    renderHome();          // status and platforms were just loaded: no second round trip (refreshScan would refetch both)
    applyScan();
    applyRoute();
    // Resume tracking a job that was started before a page reload.
    try {
      const job = await get("/api/job");
      if (job && job.status === "running") Jobs.track(job);
    } catch (_) { /* ignore */ }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
