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
    tab: "overview",   // the open tab of the system page
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
        const scanning = job.kind === "scan" || (job.kind === "collection" && job.result && job.result.action === "collection_scan");
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
      // a job of one step (a Collection scan) reports a fraction of 1: "0 / 1" says nothing, the percentage does
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
    m3u: "Playlists", collection: "Collection", retroarch: "RetroArch", switch: "Switch scan", switchdb: "Switch title database", switchverify: "Switch checksums" };


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
  const rawKinds = (p) => { const d = discOf(p); return d && d.iso ? ".cue / .iso" : d && !d.gd ? ".cue" : ".gdi / .cue"; };
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
    $("col-root").placeholder = "D:\\Emulation\\roms";
    $("col-dest").placeholder = "D:\\Emulation\\library";
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
          toggleDatabases(true);            // show what is being checked, database by database
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
    renderDatabases(u);

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

  /** The "Databases" panel: every database the app uses, one row per DAT, with the installed and the newest known version. */
  const DB_STATUS = { up_to_date: ["ok", "up to date"], update_available: ["move", "update available"], updating: ["move", "updating"],
    absent: ["missing", "not installed"], missing: ["missing", "not installed"], unknown: ["skip", "not checked this session"], error: ["conflict", "check failed"] };
  function renderDatabases(u) {
    const box = $("databases-panel");
    if (!box || box.classList.contains("hidden")) return;
    const rows = [];
    const status = (st) => { const [cls, text] = DB_STATUS[st] || ["skip", st || "-"]; return el("span", { class: `badge ${cls}`, text }); };
    const row = (source, name, installed, latest, st, checked) => rows.push(el("tr", {},
      el("td", { text: source }), el("td", { class: "wrap", text: name }),
      el("td", { class: "num", text: installed || "not installed" }), el("td", { class: "num", text: latest || "-" }),
      el("td", {}, status(st)), el("td", { class: "num muted", text: checked ? checkedText(checked) : "never" })));
    const t = u.tosec || {};
    row("TOSEC", "Full DAT pack (all TOSEC systems)", t.installed, t.latest, t.status, t.checked_at);
    for (const [key, label] of [["nointro", "No-Intro"], ["whdload", "WHDLoad"], ["redump", "Redump"]]) {
      const b = u[key] || {};
      const dats = b.dats || [];
      if (!dats.length) row(label, "-", b.installed, b.latest, b.status, b.checked_at);
      for (const d of dats) row(label, d.name, d.version, d.latest || (d.status === "up_to_date" ? d.version : null), d.status, b.checked_at);
    }
    const r = u.ratings || {};
    row("Ratings", `LaunchBox community ratings${r.games ? ` (${fmt(r.games)} games)` : ""}`, r.installed, r.latest, r.status, r.checked_at);
    box.replaceChildren(
      el("div", { class: "table-wrap" }, el("table", {},
        el("thead", {}, el("tr", {}, ["Source", "Database", "Installed", "Newest known", "Status", "Last checked"].map((h) => el("th", { text: h })))),
        el("tbody", {}, rows))),
      el("div", { class: "muted small", text: "Versions are the date in each DAT. \"Newest known\" is what the last check found online; No-Intro and WHDLoad are compared by content, so they show the installed date once they are current." }));
  }

  function toggleDatabases(open) {
    const box = $("databases-panel"), btn = $("databases-btn");
    const show = open === undefined ? box.classList.contains("hidden") : open;
    box.classList.toggle("hidden", !show);
    btn.setAttribute("aria-expanded", show ? "true" : "false");
    if (show && state.updates) renderDatabases(state.updates);
  }

  // ------------------------------------------------------------- routing
  // Hash routes (no server support needed; back / forward / reload just work):
  //   #/                                  home: one card per system
  //   #/system/<slug>/<tab>[?view=...]    <tab> = overview | library | browse
  const TABS = ["overview", "library", "browse"];
  const slugOf = (p) => p.slug || String(p.name).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  const Route = {
    /** "#/system/snes/browse?view=missing&have=0" -> {view: "system", slug, tab, params}; anything else -> home. */
    parse(hash) {
      const text = String(hash || "").replace(/^#/, "");
      const [path, query = ""] = text.split("?");
      const parts = path.split("/").filter(Boolean);
      if (parts[0] === "system" && parts[1]) {
        const tab = TABS.includes(parts[2]) ? parts[2] : "overview";
        return { view: "system", slug: decodeURIComponent(parts[1]), tab, params: Object.fromEntries(new URLSearchParams(query)) };
      }
      if (parts[0] === "collection") return { view: "collection", slug: "", tab: "", params: {} };
      if (parts[0] === "retroarch") return { view: "retroarch", slug: "", tab: "", params: {} };
      if (parts[0] === "chd") return { view: "chd", slug: "", tab: "", params: {} };
      if (parts[0] === "settings") return { view: "settings", slug: "", tab: "", params: {} };
      if (parts[0] === "switch") return { view: "system", slug: "nintendo-switch", tab: "overview", params: {} };
      if (parts[0] === "ps2") return { view: "system", slug: "sony-playstation-2", tab: "overview", params: {} };
      if (parts[0] === "wii") return { view: "system", slug: "nintendo-wii", tab: "overview", params: {} };
      if (parts[0] === "wiiu") return { view: "system", slug: "nintendo-wii-u", tab: "overview", params: {} };
      return { view: "home", slug: "", tab: "", params: {} };
    },
    build(slug, tab = "overview", params = {}) {
      const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();
      return `#/system/${encodeURIComponent(slug)}/${tab}${q ? `?${q}` : ""}`;
    },
  };
  const inSystem = () => !!state.route && state.route.view === "system";
  const platformBySlug = (slug) => state.platforms.find((p) => slugOf(p) === slug) || null;
  const goto = (hash) => { if (location.hash === hash) applyRoute(); else location.hash = hash; };
  const gotoSystem = (p, tab = "overview", params = {}) => goto(Route.build(slugOf(p), tab, params));
  /** Keep the address in step with the Browse view (no new history entry, so Back still leaves the page). */
  function syncBrowseHash() {
    const p = currentPlatform();
    if (!p || !inSystem() || state.tab !== "browse") return;
    const params = { view: activeTab || "", have: state.gamesHave, dat: state.resultDat };
    history.replaceState(null, "", Route.build(slugOf(p), "browse", params));
    state.route = Route.parse(location.hash);
  }

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

  /** The sidebar: Collection is a fixed link above; below, the systems by group, each with a status dot and the % owned. */
  function sideItem(p) {
    const rec = p.last_scan;
    const dat = datStatus(p);
    const stale = !!(rec && rec.folder && p.folder && rec.folder !== p.folder);
    const have = rec ? rec.have : 0, total = rec ? rec.total : 0;
    const pct = rec ? (rec.pct !== undefined ? rec.pct : (total ? (100 * have) / total : 0)) : null;
    const kind = dat.kind === "missing" ? "warn" : rec && !stale ? "ok" : "off";
    const saves = rec && rec.saves && rec.saves.files ? rec.saves : null;
    const tip = [p.name, p.folder || "No folder set", dat.text,
      rec ? `${fmt(have)} of ${fmt(total)} (${pctText(pct)}), scanned ${scanTime(rec)}${stale ? " (folder changed since)" : ""}` : "Not scanned yet",
      saves ? `${nPlural(saves.files, "save or save state", "saves and save states")} in RetroArch` : "",
      ].filter(Boolean).join("\n");
    return el("a", { class: "nav-item", href: Route.build(slugOf(p), state.route && state.route.view === "system" ? state.tab || "overview" : "overview"),
      "data-platform": p.name, "data-kind": kind, title: tip,
      "aria-current": inSystem() && state.platform === p.name ? "true" : null },
      el("span", { class: `dot ${kind}` }),
      el("span", { class: "nav-name", text: p.name }),
      el("span", { class: "nav-meta" },
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
    $("collection-link").setAttribute("aria-current", state.route && state.route.view === "collection" ? "true" : "false");
    $("retroarch-link").setAttribute("aria-current", state.route && state.route.view === "retroarch" ? "true" : "false");
    $("chd-link").setAttribute("aria-current", state.route && state.route.view === "chd" ? "true" : "false");
    $("settings-link").setAttribute("aria-current", state.route && state.route.view === "settings" ? "true" : "false");
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

    if (tab === "overview" || tab === "library") loadTotals();
    if (tab === "library") {
      renderLibraryGate();
      if (statsStale) refreshLibraryStats();
    } else if (tab === "browse") {
      renderBrowse();
    }
  }

  /** Apply the address: home, or a system with its tab (and Browse filters). */
  function applyRoute() {
    state.route = Route.parse(location.hash);
    const r = state.route;
    const collection = r.view === "collection";
    if (r.view === "home" && state.platforms.length && wideLayout()) {     // a wide window always shows a system or Collection
      history.replaceState(null, "", Route.build(slugOf(currentPlatform() || state.platforms[0]), "overview"));
      state.route = Route.parse(location.hash);
      return applyRoute();
    }
    if (!wideLayout()) setSide(r.view === "home");
    $("view-collection").classList.toggle("hidden", !collection);
    $("view-retroarch").classList.toggle("hidden", r.view !== "retroarch");
    $("view-chd").classList.toggle("hidden", r.view !== "chd");
    $("view-settings").classList.toggle("hidden", r.view !== "settings");
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
    if (collection) {
      $("view-home").classList.add("hidden");
      $("view-system").classList.add("hidden");
      document.title = "Collection - Simple ROM Organiser";
      state.viewWas = "collection";
      Collection.show();
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
    if (r.tab === "browse") applyBrowseParams(r.params);
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
    state.resultDat = "";
    state.tagFilter = { region: "", language: "", video: "", flag: "", rule: "" };
    state.gamesHave = "";
    state.libReason = "";
    state.libStatus = "";
    state.gamesSort = state.libSort = state.vanishSort = "";
    state.kindSort = {};
    Prefs.restore(name);
    // plan filters belong to the previous system (its destinations / statuses)
    state.organiseFilter = "";
    state.organiseDest = "";
    state.convertFilter = "";
    state.m3uFilter = "";
    activeTab = "";
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

  function scanPlatform(name) {
    selectPlatform(name);
    startScan();
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
    if (state.scan && state.resultDat && !state.scan.dat_names.includes(state.resultDat)) state.resultDat = "";
    browseDirty = true;
    Previews.onScan();                // a preview of the same system stays (marked out of date), others are dropped
    renderScanOverview();
    if (inSystem() && state.tab === "browse") renderBrowse();
    renderLibraryIntro();
    renderLibraryGate();
    refreshUndo();
    renderLibraryConvert();
    updateActionState();
    loadTotals();
  }

  /** A summary card; with ``link`` it opens the Browse tab on the matching list (deep link). */
  function card(value, label, cls = "", link = null) {
    const body = [el("div", { class: "value", text: value }), el("div", { class: "label", text: label })];
    const p = currentPlatform();
    if (link && p) {
      return el("a", { class: `card card-link ${cls}`, href: Route.build(slugOf(p), "browse", link),
        title: "Show these in the Browse tab" }, ...body);
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

  let activeTab = "";          // the open list of the Browse tab: games | matched | missing | unmatched | ...
  let browseDirty = true;      // the Browse tab must be (re)built before it is shown
  const DAT_FILTER_TABS = new Set(["matched", "missing", "games"]);
  const TAG_TABS = DAT_FILTER_TABS;
  const CHECKSUM_TABS = new Set(["matched", "missing", "unmatched", "games"]);
  const byGame = (s) => s && s.count_by === "game";

  /** have / missing / total / percent of a scan summary (games for No-Intro and Redump, ROMs otherwise). */
  function summaryNumbers(s) {
    const games = byGame(s);
    const total = games ? first(s.games_total, s.dat_total) : s.dat_total;
    const have = games ? first(s.games_have, s.have) : s.have;
    const missing = games ? first(s.games_missing, s.missing) : s.missing;
    return { games, total, have, missing, pct: total ? (100 * have) / total : 0 };
  }

  /** The Overview tab's results: summary cards (links into Browse) and the per-DAT cards. */
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
      card(fmt(s.duplicates), "Duplicates", s.duplicates ? "warn" : "", L("matched")),
      ...(normalised ? [card(fmt(normalised), "Matched with header / byte-swapped", "info", L("matched"))] : []),
      ...(s.chd_files !== undefined ? [
        card(fmt(s.verified), "CHDs verified (every track)", s.verified ? "ok" : "", L("matched")),
        card(fmt(s.identified), "CHDs identified (data tracks)", s.identified ? "info" : "", L("matched")),
        card(fmt(s.raw), "Raw sets (convertible to CHD)", s.raw ? "warn" : "", L("matched"))]
        : s.convertible ? [card(fmt(s.convertible), "Convertible to No-Intro format", "info", L("matched"))] : []),
      ...(s.unsupported ? [card(fmt(s.unsupported), "Unsupported archives", "warn", L("unsupported"))] : []),
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
        href: Route.build(slugOf(p), "browse", { view: dGames ? "games" : "matched", dat: name }),
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
      ? `${nPlural(t.files, "save file", "save files")} for ${nPlural(t.sets, "game", "games")} (${fmt(t.rom_sets)} with a ROM here, ${fmt(t.dat_sets)} without, ${fmt(t.unmatched_sets)} unmatched)`
      : "No save files found for this system.";
    const plan = libPlan && libPlan.saves;
    const will = [];
    if (plan) {
      if (plan.follow && plan.rename && plan.rename.files) will.push(`${plan.elsewhere ? "copy" : "rename"} ${nPlural(plan.rename.files, "file", "files")} with their ROMs`);
      if (plan.kept) will.push(`keep ${nPlural(plan.kept, "game", "games")} the rules would archive`);
      if (plan.archive && plan.archive.files) will.push(`archive ${nPlural(plan.archive.files, "file", "files")} with their ROMs`);
      if (plan.leave && plan.leave.files) will.push(`leave ${nPlural(plan.leave.files, "file", "files")} of archived games where they are`);
    }
    box.replaceChildren(
      el("h2", { id: "ov-saves-title", text: `Saves (${(t.sources && t.sources.length ? t.sources : ["RetroArch"]).join(", ")})` }),
      el("div", { class: "saves-line", id: "ov-saves-line", text: line }),
      t.sets ? el("div", { class: "sub", text: `${savesText(t)}${t.screenshots ? ` \u00b7 ${nPlural(t.screenshots, "state screenshot", "state screenshots")} (not counted)` : ""} \u00b7 ${fmtBytes(t.bytes)}` }) : null,
      plan ? el("div", { class: "sub", id: "ov-saves-plan", text: will.length ? `A build with the current rules would ${will.join(", ")}.` : "A build with the current rules changes none of them." })
        : el("div", { class: "sub", text: "Press Preview library on the Library tab to see what a build would do with them." }),
      t.sets && p ? el("div", {}, el("a", { href: Route.build(slugOf(p), "browse", { view: "saves" }), text: "Which title does each belong to?" })) : null);
  }

  /** The RetroArch choice changed (or a config appeared): the saves columns, the saves option and the Saves card follow. */
  async function refreshSavesAwareness() {
    for (const k of Object.keys(state.library)) delete state.library[k];
    if (typeof Collection !== "undefined") Collection.loaded = false;
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
          : `Choose the folder of ${p ? p.name : "this system"} on the Overview tab, then scan it.`),
      folder
        ? el("button", { class: "btn btn-primary", text: "Scan folder", "data-needs-idle": "", on: { click: () => startScan(false) } })
        : el("button", { class: "btn btn-primary", text: "Go to Overview", on: { click: () => openTab("overview") } }));
    Jobs.setRunning(Jobs.running);
  }

  function renderLibraryGate() {
    const has = !!state.scan;
    $("lib-gate").classList.toggle("hidden", has);
    $("lib-body").classList.toggle("hidden", !has);
    if (!has) scanGate($("lib-gate"), "Building the library");
  }

  /** Apply the Browse parameters of the address (view / have / dat); a plain tab click keeps the old filters. */
  function applyBrowseParams(params) {
    if (!params || !params.view) return;
    const have = params.have || "", dat = params.dat || "";
    if (activeTab !== params.view || state.gamesHave !== have || state.resultDat !== dat) browseDirty = true;
    activeTab = params.view;
    state.gamesHave = have;
    state.resultDat = dat;
  }

  /** The Browse tab: result lists with search, filters, paging and checksums. Built only when it is opened. */
  function renderBrowse() {
    const scan = state.scan;
    $("browse-gate").classList.toggle("hidden", !!scan);
    $("browse-body").classList.toggle("hidden", !scan);
    if (!scan) { scanGate($("browse-gate"), "Browsing the results"); return; }
    if (!browseDirty && $("result-tabs").childElementCount) return;
    browseDirty = false;
    const s = scan.summary;
    const { games, total, have, missing } = summaryNumbers(s);
    const multi = scan.dat_names.length > 1;
    const tabs = [];
    if (games) tabs.push(["games", "Games", total]);
    tabs.push(["matched", "Matched files", s.matched_files], ["missing", games ? "Missing games" : "Missing", missing],
      ["unmatched", "Unmatched", s.unmatched_files]);
    if (scan.saves) tabs.push(["saves", "Saves", scan.saves.sets]);
    if (s.unsupported) tabs.push(["unsupported", "Unsupported", s.unsupported]);
    if (s.errors) tabs.push(["errors", "Errors", s.errors]);
    if (!tabs.some((t) => t[0] === activeTab)) activeTab = tabs[0][0];
    if (activeTab !== "games") { state.gamesHave = ""; state.gamesRated = ""; state.gamesSaves = ""; }
    if (!DAT_FILTER_TABS.has(activeTab)) state.resultDat = "";
    $("result-tabs").replaceChildren(...tabs.map(([key, label, count]) => el("button", {
      class: `tab ${key === activeTab ? "active" : ""}`, role: "tab", "aria-selected": key === activeTab ? "true" : "false",
      tabindex: key === activeTab ? "0" : "-1",
      on: { click: () => { activeTab = key; if (key !== "games") { state.gamesHave = ""; state.gamesRated = ""; } browseDirty = true; renderBrowse(); } },
    }, label, el("span", { class: "count", text: `(${fmt(count)})` }))));
    syncBrowseHash();

    const container = el("div");
    const filters = [];
    if (DAT_FILTER_TABS.has(activeTab) && multi) {
      const select = el("select", {
        class: "input select", "aria-label": "Filter by DAT",
        on: { change: (e) => { state.resultDat = e.target.value; browseDirty = true; renderBrowse(); } },
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
          text: `${label} (${fmt(n)})`, on: { click: () => { state.gamesHave = key; drawHave(); syncBrowseHash(); reload(); } },
        })));
      drawHave();
      filters.push(chipBox);
      if (scan.saves) {
        const sbox = el("div", { class: "chips", id: "saves-chips" });
        const drawSaves = () => sbox.replaceChildren(...[["", "With or without saves"], ["1", "Games with saves"]].map(([key, label]) =>
          el("button", { class: `chip ${state.gamesSaves === key ? "active" : ""}`, "data-saves": key, text: label,
            on: { click: () => { state.gamesSaves = key; drawSaves(); reload(); } } })));
        drawSaves();
        filters.push(sbox);
      }
      if (ratingsAvailable()) {
        const rbox = el("div", { class: "chips", id: "rated-chips" });
        const drawRated = () => rbox.replaceChildren(...[["", "Rated or not"], ["1", "Rated"], ["0", "Unrated"]].map(([key, label]) =>
          el("button", { class: `chip ${state.gamesRated === key ? "active" : ""}`, "data-rated": key, text: label,
            on: { click: () => { state.gamesRated = key; drawRated(); reload(); } } })));
        drawRated();
        filters.push(rbox);
      }
    }
    const tagBar = TAG_TABS.has(activeTab)
      ? el("details", { class: "tag-filters" }, el("summary", { text: "Filters" }), el("div", { class: "tag-filter-rows" })) : null;
    $("result-table").replaceChildren(...[...filters, tagBar, container].filter(Boolean));
    table = new PagedTable(container, resultTableOptions(activeTab, tagBar, reload));
    table.load();
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
          text: `${facetLabel(key, value)} (${fmt(n)})`, title: key === "rule" ? "Rows that a library rule would archive" : null,
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
    box.querySelector(".tag-filter-rows").replaceChildren(...rows);
    const active = TAG_FACETS.map(([key]) => [key, state.tagFilter[key]]).filter(([, v]) => v).map(([k, v]) => facetLabel(k, v));
    box.querySelector("summary").textContent = active.length ? `Filters: ${active.join(", ")}` : "Filters: region, language, tags";
    box.classList.toggle("hidden", !rows.length);
  }

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
      el("div", { class: "cs-file", text: base || "(unnamed)", title: name }),
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
    const labels = { dat: `DAT (${src})`, local: payload.kind === "unmatched" || payload.kind === "disc_unmatched" ? "Your file – no match" : "Your file" };
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

  const SORTABLE_KINDS = new Set(["games", "matched", "unmatched", "missing", "saves"]);
  const kindSort = (kind) => (kind === "games" ? state.gamesSort : (state.kindSort[kind] || ""));

  function resultTableOptions(kind, tagBar, reload) {
    const dat = DAT_FILTER_TABS.has(kind) ? state.resultDat : "";
    const fetchKind = async ({ offset, limit, q, checksums }) => {
      const have = kind === "games" ? state.gamesHave : "";
      const rated = kind === "games" && ratingsAvailable() ? state.gamesRated : "";
      const saves = kind === "games" ? state.gamesSaves : "";
      const sort = kindSort(kind);
      const tagParams = TAG_TABS.has(kind) ? state.tagFilter : {};
      const data = await get(`/api/scan/results?${qs({ kind, offset, limit, q, dat, have, rated, saves, sort, checksums: checksums ? "1" : "", ...tagParams })}`);
      if (tagBar) renderTagBar(tagBar, data.facets, reload);
      return data;
    };
    const multi = state.scan && state.scan.dat_names.length > 1;
    const detail = CHECKSUM_TABS.has(kind) ? { kind } : null;
    const sortable = SORTABLE_KINDS.has(kind) ? { sort: {
      get: () => kindSort(kind),
      set: (v) => { if (kind === "games") state.gamesSort = v; else state.kindSort = { ...state.kindSort, [kind]: v }; },
    } } : {};
    return Object.assign({ detail, id: `browse-${kind}` }, sortable, resultColumns(kind, fetchKind, dat, multi));
  }

  function resultColumns(kind, fetchKind, dat, multi) {
    switch (kind) {
      case "games":
        return {
          fetch: fetchKind, placeholder: "Search games or file names...", emptyText: "No games in this category.",
          rowClass: (i) => (i.have ? "row-have" : "row-missing"),
          columns: [
            { label: "", render: (i) => badge(i.have ? "ok" : "missing", i.have ? "have" : "missing") },
            {
              label: "Game", cls: "wrap", sortKey: "name", sortFirst: "asc", always: true, render: (i) => el("div", {},
                el("div", { class: "game-name", text: i.name }),
                gameChips(i.tags, i.level),
                multi && !dat ? el("div", { class: "sub", text: shortDat(i.dat || "") }) : null),
            },
            { label: "Rating", sortKey: "rating", sortFirst: "desc", when: () => ratingsAvailable(), render: ratingCell },
            { label: "Year", sortKey: "year", sortFirst: "asc", cls: "num", when: (d) => !!d.has_year, render: (i) => (i.year ? String(i.year) : "-") },
            { label: "Saves", sortKey: "saves", sortFirst: "desc", cls: "num", when: (d) => !!d.saves_on, render: (i) => savesCell(i.saves) },
            { label: "Size", sortKey: "size", sortFirst: "desc", cls: "num", render: (i) => fmtBytes(i.size) },
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
            { label: "Local file", cls: "wrap", sortKey: "name", sortFirst: "asc", always: true, render: (i) => fileCell(i.file) },
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
                if (i.aside) return el("span", { class: "badge skip", text: "archived", title: "In one of the app's own folders (_excluded/, _superseded/ ...) on purpose" });
                return el("span", { class: "badge move", text: i.placed_ok ? "rename" : "move" });
              },
            },
            { label: "Saves", sortKey: "saves", sortFirst: "desc", cls: "num", when: (d) => !!d.saves_on, render: (i) => savesCell(i.saves) },
            { label: "Size", cls: "num", sortKey: "size", sortFirst: "desc", render: (i) => fmtBytes(i.size) },
          ],
        };
      case "missing":
        return {
          fetch: fetchKind, placeholder: "Search missing entries...", emptyText: "Nothing missing - complete set!",
          rowClass: () => "row-missing",
          columns: [
            {
              label: "DAT entry", cls: "wrap", sortKey: "name", sortFirst: "asc", always: true, render: (i) => el("div", {}, el("div", { text: i.set_name || i.name }),
                tagChips(i.tags),
                multi && !dat ? el("div", { class: "sub", text: shortDat(i.dat || "") }) : null),
            },
            { label: "Saves", sortKey: "saves", sortFirst: "desc", cls: "num", when: (d) => !!d.saves_on, render: (i) => savesCell(i.saves) },
            { label: "Size", cls: "num", sortKey: "size", sortFirst: "desc", render: (i) => fmtBytes(i.size) },
            { label: "CRC32", cls: "mono", render: (i) => i.crc },
          ],
        };
      case "saves":
        return {
          fetch: fetchKind, placeholder: "Search titles or save names...", emptyText: "No save files found for this system.",
          columns: [
            {
              label: "Title", cls: "wrap", sortKey: "name", sortFirst: "asc", always: true, render: (i) => el("div", {},
                el("div", { class: "game-name", text: i.title }),
                i.game && i.game !== i.title ? el("div", { class: "sub", text: i.game }) : null,
                i.name !== i.title ? el("div", { class: "sub mono-path", text: i.name }) : null),
            },
            {
              label: "Matched to", cls: "wrap", render: (i) => (i.match === "rom" ? badge("ok", "ROM here")
                : i.match === "dat" ? badge("info", "title, no ROM here") : badge("skip", "unmatched")),
            },
            { label: "Saves", sortKey: "files", sortFirst: "desc", cls: "num", render: (i) => el("div", {}, el("div", { class: "saves-total", text: fmt(i.files) }),
              el("div", { class: "sub", text: savesText(i) + (i.screenshots ? ` \u00b7 ${nPlural(i.screenshots, "screenshot", "screenshots")}` : "") })) },
            { label: "From", render: (i) => (i.source && i.source !== "retroarch" ? i.label : i.cores.length ? i.cores.join(", ") : "-") },
          ],
        };
      case "unmatched":
        return {
          fetch: fetchKind, placeholder: "Search unmatched files...", emptyText: "Every file matched a DAT.",
          columns: [
            { label: "Local file", cls: "wrap", sortKey: "name", sortFirst: "asc", always: true, render: (i) => (i.bios
              ? el("div", {}, fileCell(i.file), el("div", { class: "sub" }, badge("info", "BIOS / firmware"), ` ${i.bios}: kept with the ROMs, never archived`))
              : fileCell(i.file)) },
            ...(isGameFolder(currentPlatform()) ? [{ label: "Why", cls: "wrap", render: (i) => el("span", { class: "muted", text: i.reason || "" }) }] : []),
            { label: "Size", cls: "num", sortKey: "size", sortFirst: "desc", render: (i) => fmtBytes(i.size) },
            ...(isGameFolder(currentPlatform()) ? [] : [{ label: "CRC32", cls: "mono", render: (i) => i.crc || "" }]),
          ],
        };
      case "unsupported":
        return {
          fetch: fetchKind, emptyText: "None.",
          columns: [{ label: "File (install 7-Zip to read .7z / .rar; Wii and WIA .rvz files are not read)", cls: "wrap", render: (i) => fileCell(i.file) }],
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
      lib: { btn: "lib-plan-btn", apply: "lib-apply-btn", output: "lib-output", noun: "files", count: (d) => (d.files !== undefined ? d.files : sumCounts(d.counts)), run: () => showLibraryPlan(), table: () => libTable, reset: () => resetLibraryPreview() },
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
        this.items[id] = { id, cfg, btn, icon, label, first, status, hint, banner, bannerText, out, apply: $(cfg.apply),
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
      if (it.state !== "ready" && it.state !== "stale") return;
      it.state = "stale";
      it.why = why;
      this.paint(it);
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
      it.hint.classList.toggle("hidden", !has || busy);
      it.banner.classList.toggle("hidden", !stale);
      it.bannerText.textContent = stale ? `${it.why || "Rules changed since this preview"} — recalculate to see the current result.` : "";
      // while an out-of-date preview is being recalculated (or that failed) it stays dimmed and Build / Apply stay disabled
      const lock = stale || ((busy || err) && it.at !== null);
      it.out.classList.toggle("pv-stale", lock);
      it.out.setAttribute("aria-busy", busy ? "true" : "false");
      if (it.apply) {
        it.apply.dataset.stale = lock ? "1" : "0";
        it.apply.title = lock ? "This preview is out of date - recalculate it first" : "";
      }
      Jobs.setRunning(Jobs.running);
    },
  };

  // ------------------------------------------- Overview: library totals (Amendment 17)
  // "With your library rules: have N of M games": M = games the rules keep for the WHOLE DAT, N = those the user has.
  // The server computes it in the background (GET /api/library/totals); this block polls while it calculates.
  const Totals = { seq: 0, timer: null, data: {}, shown: {} };
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
    if (data.ratings_pending) Updates.load();          // the server asked for the data: show its progress
    if (data.calculating || data.ratings_pending) Totals.timer = setTimeout(loadTotals, data.ratings_pending ? 1500 : 900);
  }

  function totalsLine(cls, text) { return el("div", { class: `totals-line ${cls}`, text }); }

  /** Both blocks: all DAT entries (the scan) and the library rules' target. Re-rendered only when something changed. */
  function renderTotals() {
    renderTotalsInto("");          // Overview
    renderTotalsInto("lib-");      // Library tab: the same two blocks, so the rules and the result sit together
  }

  function renderTotalsInto(prefix) {
    const p = currentPlatform();
    const box = $(`${prefix}totals-pair`);
    if (!p) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    const data = Totals.data[p.name] || null;
    const scan = state.scan;
    const key = JSON.stringify([p.name, scan && scan.id, scan && scan.summary && scan.summary.have, p.last_scan && p.last_scan.at, data]);
    if (key === Totals.shown[prefix]) return;
    Totals.shown[prefix] = key;

    // left: every DAT entry
    const left = $(`${prefix}totals-all-body`);
    if (scan) {
      const { games, total, have, missing, pct } = summaryNumbers(scan.summary);
      left.replaceChildren(
        el("div", { class: "totals-main" }, el("span", { class: "value", text: fmt(have) }),
          el("span", { class: "muted", text: ` of ${fmt(total)} ${games ? "games" : "ROMs"} (${pctText(pct)})` })),
        progressBar(pct),
        totalsLine("", `Missing ${fmt(missing)}`),
        totalsLine("muted", "Every entry of the DAT counts, whatever the rules say."));
    } else {
      const rec = p.last_scan;
      left.replaceChildren(rec
        ? el("div", {}, el("div", { class: "totals-main" }, el("span", { class: "value", text: fmt(rec.have) }),
          el("span", { class: "muted", text: ` of ${fmt(rec.total)} (${pctText(rec.pct || 0)})` })),
        totalsLine("muted", `Last scan ${scanTime(rec)}. Scan again to refresh the details.`))
        : totalsLine("muted", "Not scanned yet. Scan the folder to see how much of the DAT you have."));
    }

    // right: with the library rules
    const right = $(`${prefix}totals-lib-body`);
    right.classList.remove("totals-stale");
    const link = prefix
      ? el("button", { class: "btn btn-small btn-ghost totals-rules", type: "button", text: "Edit the rules",
        on: { click: () => { const box = $("library-rules-box"); box.open = true; box.scrollIntoView({ block: "start", behavior: "smooth" }); } } })
      : el("button", { class: "btn btn-small btn-ghost totals-rules", type: "button", text: "Change the rules",
        on: { click: () => openTab("library") } });
    if (!data) {
      right.replaceChildren(el("div", { class: "totals-wait" }, el("span", { class: "spinner", "aria-hidden": "true" }), " calculating..."));
      return;
    }
    if (data.available === false) {
      right.replaceChildren(totalsLine("muted", "The DAT of this system is not installed yet - check for updates first."));
      return;
    }
    if (data.ratings_pending) {
      right.replaceChildren(el("div", { class: "totals-wait" }, el("span", { class: "spinner", "aria-hidden": "true" }),
        " waiting for the ratings data (see Ratings in the rules)..."), link);
      return;
    }
    if (data.error) {
      right.replaceChildren(totalsLine("error-text", `Could not calculate: ${data.error}`));
      return;
    }
    const wait = data.calculating
      ? el("div", { class: "totals-wait" }, el("span", { class: "spinner", "aria-hidden": "true" }), data.stale ? " recalculating..." : " calculating...") : null;
    if (data.target_games === null || data.target_games === undefined) {
      right.replaceChildren(wait || totalsLine("muted", "-"));
      return;
    }
    const m = data.target_games;
    const n = data.have_games;
    const lines = [];
    if (n !== null && n !== undefined) {
      const pct = data.percent !== null && data.percent !== undefined ? data.percent : (m ? (100 * n) / m : 0);
      lines.push(
        el("div", { class: "totals-main" }, el("span", { class: "value", text: fmt(n) }),
          el("span", { class: "muted", text: ` of ${fmt(m)} games (${pctText(pct)})` })),
        progressBar(pct),
        totalsLine("", `Missing ${fmt(m - n)}`));
      if (data.not_preferred) {
        lines.push(totalsLine("hint hint-up", `${fmt(data.not_preferred)} of your ${fmt(n)} ${data.not_preferred === 1 ? "is" : "are"} not the preferred version (an upgrade is available)`));
      }
      if (data.owned_but_excluded) {
        lines.push(totalsLine("hint hint-ex", `${fmt(data.owned_but_excluded)} ${data.owned_but_excluded === 1 ? "game you own is" : "games you own are"} excluded by your rules`));
      }
      if (data.owned_incomplete) {
        lines.push(totalsLine("hint hint-inc", `${fmt(data.owned_incomplete)} multi-disk ${data.owned_incomplete === 1 ? "game you own is" : "games you own are"} incomplete`));
      }
    } else {
      lines.push(
        el("div", { class: "totals-main" }, el("span", { class: "value", text: fmt(m) }),
          el("span", { class: "muted", text: " games in the target library" })),
        totalsLine("muted", "Scan to see how many you have"));
    }
    if (data.rating && data.rating.games) {
      const rt = data.rating;
      lines.push(totalsLine("muted small", `Rating filter: ${fmt(rt.excluded)} of ${fmt(rt.games)} games left out (${fmt(rt.unrated)} of them have no usable rating).`));
    }
    if (data.rank_scope === "owned") {
      lines.push(totalsLine("muted small", "Top N counts only the games you own; the target lists every game that passes the other rules."));
    }
    if (data.target_incomplete) {
      lines.push(totalsLine("muted small", `${fmt(data.target_incomplete)} more game${data.target_incomplete === 1 ? " is" : "s are"} only partly in the DAT and cannot be completed.`));
    }
    right.replaceChildren(...lines, ...(wait ? [wait] : []), link);
    right.classList.toggle("totals-stale", !!data.stale);
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
  const REASON_LABEL = { kept: "Kept", excluded: "Excluded", superseded: "Superseded", incomplete: "Incomplete",
    duplicate: "Duplicates", unmatched: "Unmatched", playlist: "Playlists" };
  const REASON_ORDER = ["kept", "excluded", "superseded", "incomplete", "duplicate", "unmatched", "playlist"];
  const REASON_BADGE = { kept: "ok", excluded: "bad", superseded: "info", incomplete: "warn", duplicate: "warn",
    unmatched: "skip", playlist: "move" };
  let libTable = null;
  let vanishTable = null;
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
    // The Browse list may have been drawn before the profile arrived: its rating column and chips depend on it.
    if (!hadRatings && ratingsAvailable() && inSystem() && state.tab === "browse") { browseDirty = true; renderBrowse(); }
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

  // --------------------------------------------------------------------------------------------------
  // Reorderable list (used by the region priority). Mouse + touch drag through Pointer Events on a handle
  // (touch-action: none on the handle only, so the rest of the row still scrolls the list), keyboard pick-up
  // (Space/Enter, arrows, Space/Enter to drop, Escape to cancel), and Top / up / down buttons as a fallback.
  // ``items`` is the FULL order; a filter only hides rows. ``onCommit(order, before)`` runs once per completed move.
  function sortableList({ items, label, noun = "item", prioCount = 0, prioLabel = "Prioritised", restLabel = "Everything else", onCommit }) {
    let order = [...items];
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

    function buildRow(name) {
      const act = (how) => () => commit(name, how);
      const btn = (cls, text, title, aria, how) => el("button", { type: "button", class: `btn btn-icon ${cls}`, text, title, "aria-label": aria,
        on: { click: act(how) } });
      const row = el("li", { class: "sortable-row", "data-name": name, tabindex: "-1" },
        el("span", { class: "drag-handle", "data-drag-handle": "", "aria-hidden": "true", title: `Drag to reorder ${name}`, text: "⋮⋮" }),
        el("span", { class: "region-n" }), el("span", { class: "region-name", text: name }),
        btn("row-top", "Top", "Move to top", `Move ${name} to top`, "top"),
        btn("row-up", "▲", "Prefer more", `Move ${name} up`, "up"),
        btn("row-down", "▼", "Prefer less", `Move ${name} down`, "down"));
      return row;
    }

    /** Decorations that depend on the order: numbers, priority highlight, divider, disabled buttons, tab stops. */
    function refresh() {
      const vis = visibleNames();
      if (!visible(current) && vis.length) current = vis[0];
      order.forEach((name, i) => {
        const row = rows.get(name);
        row.querySelector(".region-n").textContent = String(i + 1);
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
      onCommit([...order], before);
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
        const hit = !filter || name.toLowerCase().includes(filter);
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
      setItems(next) {
        cancelGrab(false);
        if (drag) dragEnd(false);
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
    const order = regionSave.pending[name];
    delete regionSave.pending[name];
    regionSave.busy = true;
    try {
      const resp = await post("/api/library/profile", { platform: name, region_priority: order });
      regionSave.confirmed[name] = [...(resp.regions || order)];
      const newer = regionSave.pending[name];
      if (newer) resp.regions = [...newer];           // a later move is already shown: keep it on screen
      adoptProfile(name, resp);
      if (name === state.platform) renderRulesSummary(resp);
    } catch (err) {
      toast(`Could not save the region priority: ${err.message}`, "error");
      delete regionSave.pending[name];
      const info = state.library[name];
      const back = regionSave.confirmed[name];
      if (info && back) {
        info.regions = [...back];
        if (name === state.platform && regionSave.ui && regionSave.uiPlatform === name) regionSave.ui.setItems(back);
        if (name === state.platform) renderRulesSummary(info);
      }
    } finally {
      regionSave.busy = false;
      if (Object.keys(regionSave.pending).length) flushRegionSave();
    }
  }

  function commitRegionOrder(name, order) {
    const info = state.library[name];
    if (!info) return;
    if (!regionSave.confirmed[name]) regionSave.confirmed[name] = [...(info.regions || [])];
    info.regions = [...order];                          // optimistic
    regionSave.pending[name] = [...order];
    renderRulesSummary(info);
    clearTimeout(regionSave.timer);
    regionSave.timer = setTimeout(flushRegionSave, 250);
  }

  function regionSection(info) {
    const name = state.platform;
    if (!regionSave.pending[name] && !regionSave.busy) regionSave.confirmed[name] = [...(info.regions || [])];
    const oldList = document.querySelector("#rules-regions .sortable-list");
    const keepScroll = oldList ? oldList.scrollTop : 0;
    const ui = sortableList({
      items: info.regions || [], label: "Region priority, best region first", noun: "region",
      onCommit: (order) => commitRegionOrder(name, order),
    });
    regionSave.ui = ui;
    regionSave.uiPlatform = name;
    const section = el("div", { class: "rules-group", id: "rules-regions" },
      el("div", { class: "rules-head", text: "Region priority" }),
      el("div", { class: "sub note", text: `${(catalogOf(info).find((e) => e.id === "region_priority") || {}).description || "Best region first."} The whole list counts, in this order; regions you have not moved stay in alphabetical order after the ones you placed.` }),
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
    else if (avail.region_priority && (info.regions || []).length) bits.push(`${info.regions.slice(0, 2).join(" > ")} first`);
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
    if (prof.saved_games && prof.saved_games !== "keep") bits.push(prof.saved_games === "archive" ? "saves archived with their games" : "saves left alone");
    $("library-rules-summary").textContent = bits.join(" \u00B7 ");
    $("library-rules-note").textContent = JSON.stringify(prof) === JSON.stringify(info.defaults) ? "Default rules" : "Custom rules";
  }

  // ---- What happens to the saves of a game a rule replaces or archives (Amendment 31): one group for the Library rules and
  // one for the Collection rules. Both exist only when a RetroArch config is known (the profile then has ``saved_games``).
  const SAVED_CHOICES = [
    { value: "keep", label: "Keep both ROMs",
      text: "The ROM the rules would replace or archive stays as well, so the saves still have their game. Its saves are renamed if the ROM is renamed." },
    { value: "archive", label: "Archive the saves with the ROM",
      text: "The ROM is replaced or archived as the rules say, and its saves and save states move to the archive with it (into a _saves folder). Undo brings them back. Not while RetroArch is running." },
    { value: "leave", label: "Leave the saves where they are",
      text: "The ROM is replaced or archived as the rules say; its saves are not touched." },
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
    const found = where === "col" ? (Collection.info && Collection.info.scan && Collection.info.scan.saves) || null
      : state.scan && state.scan.saves ? state.scan.saves : null;
    const renames = !found || found.renames !== false;                  // (only RetroArch's saves are named after the ROM)
    const names = found && found.sources && found.sources.length ? found.sources.join(", ") : "RetroArch";
    return el("div", { class: "rules-group", id: `${where}-saves-group` },
      el("div", { class: "rules-head", text: `Saves (${names})` }),
      el("label", { class: "rating-label" }, el("span", { class: "rule-label", text: "When a rule replaces or archives a game you have saves for: " }), select),
      el("div", { class: "sub note", id: `${where}-saved-games-note`, text: choice.text }),
      renames ? el("label", { class: "check rule-check", title: "The same setting as on the RetroArch page" }, follow,
        el("span", { class: "rule-text" }, el("span", { class: "rule-line" }, el("span", { class: "rule-label", text: "Rename saves with their ROMs" })),
          el("span", { class: "sub", text: "When a build gives a ROM its database name, its saves and save states get the new name too (a build in another folder copies them). Each core's folder stays separate; nothing is overwritten." }))) : null,
      el("div", { class: "sub note", text: where === "col"
        ? "This is the default for every game. Building into another folder archives nothing from your folders: the saves stay where they are, and only the games it copies are chosen by this setting."
        : "This is the default for every game; on the Library tab you can choose differently for a single game. Building into another folder archives nothing here, so saves are left alone there; the setting still decides which games are copied." }));
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
    if (!s.elsewhere && s.archive && s.archive.files)
      out.push(card(fmt(s.archive.files), `Save files ${apply ? "archived" : "to archive"} with their games (${fmt(s.archive.games)} game${s.archive.games === 1 ? "" : "s"})`, "warn"));
    if (!s.elsewhere && s.archive && (s.archive.running || []).length)
      out.push(card(s.archive.running.join(", "), `running: close ${s.archive.running.length > 1 ? "them" : "it"}, or its saves stay where they are`, "bad"));
    if (!s.elsewhere && s.leave && s.leave.files)
      out.push(card(fmt(s.leave.files), `Save files left where they are (${fmt(s.leave.games)} archived game${s.leave.games === 1 ? "" : "s"})`, "info"));
    return out;
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
    if (ratingEntries(info).length) groups.push(ratingsSection(info));
    if (!groups.length) groups.push(el("div", { class: "muted", text: "No library rules apply to this system." }));
    if (prof.saved_games !== undefined) groups.push(savesGroup(prof.saved_games, (v) => saveProfile({ saved_games: v }), "lib"));
    $("library-rules").replaceChildren(...groups);
    renderRulesSummary(info);
    updateRuleCounts();
    renderRatingsStatus();
  }

  // ---- Ratings group (Amendment 18): rendered from the catalog entries with group "ratings"; no rule list here.
  const ratingsOf = () => (state.updates && state.updates.ratings) || null;

  function ratingsSection(info) {
    const prof = info.profile;
    const save = (e, raw) => {
      const text = String(raw).trim();
      let v = text === "" ? null : Number(text);
      if (e.kind === "number" && v !== null && Number.isNaN(v)) { toast(`${e.label} needs a number`, "error"); renderLibraryRules(); return; }
      if (v === null && e.default !== null && e.default !== undefined) v = e.default;
      if (v === prof[e.field]) return;
      saveProfile({ [e.field]: v });
    };
    const rows = ratingEntries(info).map((e) => {
      if (e.kind === "option") {
        return ruleRow(e, !!prof[e.field], (on) => saveProfile({ [e.field]: on }), null);
      }
      let control;
      if (e.kind === "choice") {
        control = el("select", { class: "input select", "data-field": e.field, "aria-label": e.label,
          on: { change: (ev) => saveProfile({ [e.field]: ev.target.value }) } },
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
    });
    return el("div", { class: "rules-group", id: "rules-ratings" },
      el("div", { class: "rules-head", text: "Ratings" }),
      el("div", { class: "sub note", id: "ratings-credit" }),
      el("div", { class: "ratings-status", id: "ratings-status", role: "status", "aria-live": "polite" }),
      el("div", { class: "rating-rows" }, rows),
      el("div", { class: "sub", id: "ratings-hint" }),
      el("div", { class: "notice notice-warn ratings-note", id: "ratings-note",
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
        : (code === "language" ? "" : "Press Preview library to see how many titles this affects.");
    }
  }

  /** Background refresh of the plan numbers (rules panel counts) after a scan or a rules change. */
  let statsSeq = 0;
  let statsStale = true;       // the rule counts are fetched when the Library tab is open (not on every scan)
  const refreshLibraryStats = debounce(async () => {
    if (!state.scan) { state.libStats = null; updateRuleCounts(); return; }
    if (!inSystem() || state.tab !== "library") { statsStale = true; return; }
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
    $("lib-empty").textContent = state.scan ? "Press \"Preview library\" to see what would happen." : "Run a scan first.";
    $("lib-empty").classList.remove("hidden");
    $("lib-output").classList.add("hidden");
    libTable = null;
    vanishTable = null;
    libPlan = null;
    state.libWhy = "";
    state.libSaves = "";
    $("lib-saves-filters").classList.add("hidden");
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
      ...(asd ? [card(fmt(asd.coming + asd.existing), "To move out to the archive folder", "info")] : []),
      ...(plan.convert ? [card(fmt(plan.convert.count), plan.convert.kind === "chd" ? "Raw discs to convert to CHD first" : "Dumps to clean up first", plan.convert.count ? "info" : "ok")] : []),
      card(fmt((r.renamed || 0) + (r.moved || 0)), `Renamed / moved (${fmt(r.renamed || 0)} / ${fmt(r.moved || 0)})`, "info"),
      card(fmt(r.excluded), aside("Excluded", r.excluded, cats.excluded), r.excluded ? "warn" : ""),
      card(fmt(r.superseded), aside("Superseded", r.superseded, cats.superseded), r.superseded ? "warn" : ""),
      ...(isGameFolder(currentPlatform()) ? [] : [card(fmt(r.incomplete), aside("Incomplete", r.incomplete, cats.incomplete), r.incomplete ? "warn" : "")]),
      card(fmt(r.duplicates), aside("Duplicates", r.duplicates, cats.duplicate), r.duplicates ? "warn" : ""),
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
    // excluded files per reason (language, flag_*, rules): click one to list just those rows
    const why = {};
    for (const [k, n] of Object.entries(r)) if (k.startsWith("excluded_") && n) why[k.slice(9)] = n;
    const whyBox = $("lib-why-filters");
    whyBox.classList.toggle("hidden", !Object.keys(why).length);
    const infoNow = state.library[state.platform];
    const whyOrder = infoNow ? [...catalogOf(infoNow).filter((e) => e.kind === "exclude").map((e) => e.id),
      ...catalogOf(infoNow).filter((e) => e.kind === "keep_flag").map((e) => `flag_${e.id}`), "language", ...(infoNow.rating_codes || []), ...(infoNow.override_codes || [])] : [];
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
      : `Incomplete sets (${fmt(total)}) - disks missing, archived as _incomplete`;
    $("lib-incomplete").replaceChildren(...sets.map((x) => el("li", {},
      el("span", { text: x.name }), " ",
      el("span", { class: "tag tag-status", text: `missing ${isGameFolder(currentPlatform()) ? "disc" : "disk"} ${x.missing.join(", ")} of ${x.total}` }),
      el("span", { class: "sub", text: ` have ${x.present.length ? x.present.join(", ") : "none"} - ${shortDat(x.dat)}` }))),
    ...(total > sets.length ? [el("li", { class: "muted", text: `... and ${fmt(total - sets.length)} more` })] : []));
  }

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
        : "Kept: no rule sets this file aside, and it is the best version you have.");
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

  const SAVED_OPTIONS = [["", "Use the default"], ["keep", "Keep both ROMs"], ["archive", "Archive the saves with the ROM"], ["leave", "Leave the saves"]];
  const SAVED_EFFECT = { rename: ["info", "renamed with the ROM"], keep: ["ok", "ROM kept for them"], archive: ["warn", "archived with the ROM"], leave: ["skip", "left where they are"] };

  /** The Saves chips of the Library preview: all rows / the games with saves / the saves this build changes. */
  function renderLibrarySavesFilters(plan, reload) {
    const box = $("lib-saves-filters");
    const s = plan.saves;
    box.classList.toggle("hidden", !s);
    if (!s) { state.libSaves = ""; return; }
    const chips = [["", "Saves: all rows", null], ["with", "Games with saves", s.rows_with], ["affected", "Saves affected by this build", s.rows_affected]];
    box.replaceChildren(...chips.map(([key, label, n]) => el("button", {
      class: `chip ${state.libSaves === key ? "active" : ""}`, "data-saves": key, text: n === null ? label : `${label} (${fmt(n)})`,
      on: { click: () => { state.libSaves = key; reload(); } } })));
  }

  /** The Saves cell of a Library row: how many, what the build does with them, and the choice for this game. */
  function libSavesCell(i) {
    const s = i.item === "file" ? i.saves : null;
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
      toast(`${s.game}: ${choice ? SAVED_OPTIONS.find((o) => o[0] === choice)[1].toLowerCase() : "back to the default"} - press Recalculate to see what it changes`, "ok", 6000);
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
      toast(action === "clear" ? `${fmt(seen.size)} game(s) back under the rules` : `${fmt(seen.size)} game(s) will always be ${action === "keep" ? "kept" : "excluded"} - press Recalculate to see it`, "ok", 6000);
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

  async function showLibraryPlan() {
    Previews.busy("lib");
    $("lib-empty").classList.add("hidden");
    $("lib-output").classList.remove("hidden");
    if (!libTable) {
      vanishTable = null;
      libTable = new PagedTable($("lib-table"), {
        id: "library",
        placeholder: "Search file names, folders or playlists...",
        emptyText: "Nothing in this category.",
        detail: { kind: "library", ref: (i) => (i.cs_kind ? { kind: i.cs_kind, id: i.cs_id } : null), extra: libraryWhyPanel },
        sort: { get: () => state.libSort, set: (v) => { state.libSort = v; } },
        select: {
          id: (i) => `${i.from}`, can: (i) => i.item === "file" && !!i.game_ref,
          actions: [
            { label: "Always keep", title: "Keep these games whatever the rules say", run: (items) => setOverrides(items, "keep") },
            { label: "Always exclude", title: "Set these games aside whatever the rules say", run: (items) => setOverrides(items, "exclude") },
            { label: "Back to the rules", title: "Remove your choice for these games", run: (items) => setOverrides(items, "clear") },
          ],
        },
        fetch: Previews.wrap("lib", async ({ offset, limit, q, refresh }) => {
          const data = await post("/api/library/plan", {
            ...buildOptions(), reason: state.libReason, status: state.libStatus, why: state.libWhy, saves: state.libSaves || undefined, offset, limit, q, refresh,
            sort: state.libSort, checksums: state.showChecksums ? true : undefined,
          });
          libPlan = data;
          renderLibraryCards(data);
          renderSavesCard();
          const box = $("lib-warnings");
          const notes = [...(data.warnings || [])];
          if (data.missing_dats && data.missing_dats.length) notes.push(`Not checked (DAT not installed): ${data.missing_dats.map(shortDat).join(", ")}.`);
          box.classList.toggle("hidden", !notes.length);
          box.replaceChildren(...notes.map((n) => el("div", { text: n })));
          const reload = () => { libTable.offset = 0; libTable.load(); };
          renderLibrarySavesFilters(data, reload);
          const r = data.reasons || {}, pl = data.playlists || {};
          // every row of a category, moving or already in place (an already built library has few moves but many rows)
          const rc = data.categories || { kept: r.kept, excluded: r.excluded, superseded: r.superseded, incomplete: r.incomplete,
            duplicate: r.duplicates, unmatched: r.unmatched, playlist: (pl.write || 0) + (pl.ok || 0) + (pl.remove || 0) + (pl.conflict || 0) };
          filterChips($("lib-reason-filters"), Object.fromEntries(Object.entries(rc).filter(([, n]) => n)), state.libReason, REASON_ORDER,
            (key) => { state.libReason = key; if (key !== "excluded") state.libWhy = ""; reload(); }, (k) => REASON_LABEL[k] || k, "All reasons");
          filterChips($("lib-status-filters"), data.counts || {}, state.libStatus, ["move", "rename", "delete", "conflict", "skip", "ok"],
            (key) => { state.libStatus = key; reload(); }, (k) => k, "All statuses");
          $("lib-apply-btn").dataset.blocked = data.empty && !(data.aside && data.aside.existing) && !(data.convert && data.convert.count) ? "1" : "0";
          Jobs.setRunning(Jobs.running);
          return data;
        }),
        columns: [
          { label: "Status", render: (i) => badge(i.status, i.status === "move" && i.kind === "rename" ? "rename" : i.status) },
          { label: "Reason", render: (i) => badge(REASON_BADGE[i.category] || "skip", REASON_LABEL[i.category] || i.category) },
          {
            label: "Change (relative to the system folder)", cls: "wrap change-col", sortKey: "name", sortFirst: "asc", always: true, render: (i) => {
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
          { label: "Rating", sortKey: "rating", sortFirst: "desc", when: () => ratingsAvailable(),
            render: (i) => (i.item === "playlist" ? el("span", { class: "muted", text: "" }) : ratingCell(i)) },
          { label: "Saves", sortKey: "saves", sortFirst: "desc", cls: "wrap saves-col", when: (d) => !!d.saves, render: libSavesCell },
          { label: "Year", sortKey: "year", sortFirst: "asc", cls: "num", when: (d) => !!d.has_year, render: (i) => (i.year ? String(i.year) : "") },
          { label: "Size", sortKey: "size", sortFirst: "desc", cls: "num", render: (i) => (i.size ? fmtBytes(i.size) : "") },
          { label: "Why", cls: "wrap why-col", render: libraryNote },
        ],
      });
    }
    await libTable.load();
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
      if (libTable) { libTable.offset = 0; showLibraryPlan(); }
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
      const saveText = (sv.followed || sv.copied) ? `; ${fmt((sv.followed || 0) + (sv.copied || 0))} save file(s) ${sv.copied ? "copied to" : "renamed to"} the new names` : (r.action === "undo" && sv.restored ? `; ${fmt(sv.restored)} save file(s) renamed back` : "");
      const sa = r.saves_archived;
      const archText = sa ? (sa.skipped_running ? `; ${(sa.running || ["RetroArch"]).join(", ")} ${(sa.running || [1]).length > 1 ? "are" : "is"} running: ${sa.moved ? "some" : "the"} saves of the archived games were not moved` : `; ${fmt(sa.moved)} save file(s) archived with their games`) : "";
      const asideText = r.aside ? `; ${fmt(r.aside.moved)} archived file(s) moved out to ${r.aside.path}` : (r.action === "undo" && r.aside_restored ? `; ${fmt(r.aside_restored)} archived file(s) brought back` : "");
      const convText = r.converted ? `; ${fmt(r.converted.count)} converted first${r.converted.failed.length ? ` (${fmt(r.converted.failed.length)} could not be converted)` : ""}` : (r.action === "undo" && r.converted_back ? "; the conversion was undone too" : "");
      toast(text + saveText + archText + asideText + convText + (failed.length ? `, ${fmt(failed.length)} failed` : ""), failed.length || r.error ? "error" : "ok", 8000);
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
      if (wasOpen && state.scan && libTable) { libTable.offset = 0; await showLibraryPlan(); }
      return;
    }
    await refreshScan();
    if (wasOpen && state.scan) { vanishTable = null; if (libTable) libTable.offset = 0; await showLibraryPlan(); }
  };

  // --------------------------------------------------------- 3b. Organise (advanced)
  const ACTIONABLE = ["move", "rename", "delete"];
  const latestOnly = () => !!(currentPlatform() && currentPlatform().latest_only);   // the library rule: there is one setting for it

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
  /** The saves of every system of a Collection preview, added up as one plan (what ``savesCards`` shows). */
  function colSavesTotal(r) {
    const rows = (r.systems || []).map((x) => x.saves_plan).filter((s) => s && s.found);
    if (!rows.length) return null;
    const sum = (f) => rows.reduce((n, s) => n + f(s), 0);
    return { found: true, follow: rows.some((s) => s.follow), elsewhere: rows.every((s) => s.elsewhere), mode: rows[0].mode, kept: sum((s) => s.kept || 0),
      leave: { files: sum((s) => (s.leave || {}).files || 0), games: sum((s) => (s.leave || {}).games || 0) },
      rename: { files: sum((s) => s.rename.files), games: sum((s) => s.rename.games), conflicts: sum((s) => s.rename.conflicts) },
      archive: { files: sum((s) => s.archive.files), games: sum((s) => s.archive.games), states: sum((s) => s.archive.states), to: "the _saves folder of each system in the archive" } };
  }

  // ------------------------------------------------------------------ the emulators whose saves are looked at (besides RetroArch)
  const Emu = {
    list: [],

    async show() {
      try { this.list = (await get("/api/emulators")).emulators; } catch (err) { toast(err.message, "error"); return; }
      this.render();
      Switch.load();
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
      const c = r.counts;
      $("ra-bios-out").classList.remove("hidden");
      $("ra-bios-cards").replaceChildren(
        card(fmt(r.cores.length), "Cores", "info"), card(fmt(c.ok + c.present), "In place", "ok"),
        ...(c.wrong ? [card(fmt(c.wrong), "Wrong checksum", "bad")] : []),
        ...(c.found ? [card(fmt(c.found), "Found, not placed yet", "info")] : []),
        card(fmt(c.missing), "Missing", c.missing ? "warn" : ""),
        card(fmt(r.required_missing), "Required, not in place", r.required_missing ? "bad" : "ok"));
      $("ra-bios-where").textContent = `Cores of ${r.scope}. System folder: ${r.system_dir}. Searched: ${r.searched.length ? r.searched.join(", ") : "nothing"}.${r.complete ? "" : " The search stopped early (time limit): files found by checksum under another name may be missing from the list."}`;
      const LABEL = { ok: "verified", present: "there (no checksum to compare)", wrong: "there, wrong checksum", found: "found - not in place", missing: "missing" };
      $("ra-bios-table").replaceChildren(...r.cores.flatMap((core) => core.firmware.map((f) => el("tr", {},
        el("td", { text: core.core, title: (core.serves || []).join(", ") }), el("td", { class: "mono small", text: f.path }), el("td", { text: f.optional ? "optional" : "required" }),
        el("td", {}, badge(f.status === "ok" || f.status === "present" ? "ok" : f.status === "found" ? "info" : f.status === "wrong" ? "bad" : f.optional ? "skip" : "warn", LABEL[f.status])),
        el("td", { class: "mono small", text: f.source || "" })))));
    },

    async biosApply() {
      const r = this.bios;
      if (!r) { toast("Press Check first to see what can be placed.", "info"); return; }
      if (!r.counts.found) { toast("No file to place: nothing missing was found in the folders searched.", "info"); return; }
      const mode = $("ra-bios-mode").value;
      if (!(await confirmDialog({ title: "Place BIOS files", body: `${mode === "copy" ? "Copy" : "Move"} ${fmt(r.counts.found)} file(s) into ${r.system_dir}? Nothing is overwritten; Undo puts them back.`, okText: mode === "copy" ? "Copy" : "Move" }))) return;
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
  const Switch = {
    info: null,        // GET /api/switch

    async load() {
      try { this.info = await get("/api/switch"); } catch (err) { toast(err.message, "error"); return; }
      const i = this.info, db = i.db;
      $("sw-db-line").textContent = db.available ? `Title database: ${fmt(db.titles)} games and ${fmt(db.ncas)} files known, fetched ${db.fetched}.`
        : "No title database yet: it is downloaded on the first start (or press Check for updates). Until then Switch games cannot be matched.";
      $("sw-keys-line").textContent = i.keys.path ? `prod.keys: ${i.keys.chosen ? "chosen" : "found"} (used only to read the title ID of a game card dump)`
        : "prod.keys: not found (a game card dump is then told by its name only).";
    },

    bind() {
      $("sw-verify-btn").addEventListener("click", () => Jobs.start("/api/switch/verify", {}));
      $("sw-db-btn").addEventListener("click", () => Jobs.start("/api/switch/db/update", {}));
    },
  };
  Jobs.handlers.switchverify = async (job) => {
    if (job.status === "done" && job.result && job.result.verify) {
      const v = job.result.verify;
      toast(`Checksums: ${fmt(v.checked)} files checked${v.skipped ? `, ${fmt(v.skipped)} already known` : ""}, ${fmt(v.damaged)} damaged`, v.damaged ? "error" : "ok", 8000);
    }
  };
  Jobs.handlers.switchdb = async (job) => {
    if (job.status === "done") { await Switch.load(); toast("Switch title database updated", "ok"); }
  };

  // ------------------------------------------------------------------ settings for every system
  const Settings = {
    show() { this.render(); },

    render() {
      const a = (state.status && state.status.archive) || { dir: "", overrides: {} };
      $("set-archive-dir").value = a.dir || "";
      const p = currentPlatform();
      $("set-archive-default").textContent = a.dir ? "Every system archives to this folder, unless it has its own (below)."
        : "Not set: nothing is archived. What the rules leave out stays in each system's folder (_excluded, _superseded ...) and Collection leaves files that match nothing where they are. Choose a folder to turn archiving on.";
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
      $("set-archive-dir").addEventListener("change", () => this.save($("set-archive-dir").value.trim()));
      $("set-archive-browse").addEventListener("click", () => FolderBrowser.open($("set-archive-dir"), "Choose the archive folder"));
    },
  };

  // ------------------------------------------------------------------ collection (v0.2): a whole ROM folder
  // One scan reads every file once and finds its system in all the databases; the preview and the builds only sort that scan's
  // metadata, so they are quick.
  const Collection = {
    info: null,
    loaded: false,
    last: null,           // the last preview / build answer
    saveSeq: 0,
    savesSeq: 0,

    async show() {
      if (!this.loaded) {
        try { this.info = await get("/api/collection"); } catch (err) { toast(err.message, "error"); return; }
        this.loaded = true;
        const i = this.info;
        $("col-root").value = i.root || "";
        $("col-dest").value = i.dest || "";
        $("col-mode").value = i.mode;
        $("col-sidecars").checked = !!i.sidecars;
        $("col-sync").checked = !!i.sync;
        $("col-aside").value = i.aside || "";
        $("col-sweep").checked = i.sweep !== false;
        $("col-convert").checked = !!i.convert;
        $("col-place-inplace").checked = i.place !== "elsewhere";
        $("col-place-elsewhere").checked = i.place === "elsewhere";
      }
      this.render();
    },

    adopt(info) { this.info = info; this.render(); },

    /** Saves of single games (RetroArch): the games the shared rules affect that have saves, each with its own choice. Loaded when
     *  the list is opened; a choice is stored in the shared rules (``saved_overrides``). */
    async loadSavesGames() {
      const box = $("col-saves-games"), info = this.info;
      const show = !!(info && info.profile && info.profile.saved_games !== undefined && info.scan && info.scan.saves && info.scan.saves.sets);
      box.classList.toggle("hidden", !show);
      if (!show || !box.open) return;
      const n = ++this.savesSeq;
      let data;
      try { data = await post("/api/collection/saves", { q: $("col-saves-q").value.trim(), limit: 200 }); }
      catch (err) { $("col-saves-note").textContent = err.message; return; }
      if (n !== this.savesSeq) return;
      $("col-saves-games-title").textContent = `Saves of single games: choose for each game (${fmt(data.total)})`;
      $("col-saves-table").replaceChildren(...data.items.map((i) => {
        const eff = SAVED_EFFECT[i.effect];
        return el("tr", {},
          el("td", { text: i.platform }),
          el("td", {}, el("div", { text: i.title }), i.game !== i.title ? el("div", { class: "sub", text: i.game }) : null),
          el("td", { text: savesText(i) }),
          el("td", {}, eff ? badge(eff[0], eff[1]) : null),
          el("td", {}, el("select", { class: "input select saves-choice", "aria-label": `If a rule replaces ${i.game}: what happens to its saves`,
            on: { change: (e) => this.setSavedChoice(i, e.target.value) } },
          ...SAVED_OPTIONS.map(([v, label]) => el("option", { value: v, text: label, selected: v === i.choice })))));
      }));
      $("col-saves-note").textContent = data.total > data.items.length ? `Showing the first ${fmt(data.items.length)} of ${fmt(data.total)}: search to find the others.`
        : data.total ? "" : "No game with saves is affected by the rules above.";
    },

    async setSavedChoice(i, choice) {
      try {
        const info = await post("/api/collection/saved", { choice: choice || "default", games: [{ dat: i.dat, game: i.game }] });
        this.adopt(info);
        toast(`${i.title}: ${choice ? SAVED_OPTIONS.find((o) => o[0] === choice)[1].toLowerCase() : "back to the default"} - press Preview to see what it changes`, "ok", 6000);
      } catch (err) { toast(err.message, "error"); }
    },

    async save(changes) {
      const n = ++this.saveSeq;               // answers can arrive out of order: only the newest one is shown
      try {
        const info = await post("/api/collection/save", changes);
        if (n === this.saveSeq) this.adopt(info);
      } catch (err) { toast(err.message, "error"); }
    },

    inplace() { return $("col-place-inplace").checked; },

    render() {
      const info = this.info;
      if (!info) return;
      const scan = info.scan;
      const inp = this.inplace();
      $("col-systems-panel").classList.toggle("hidden", !scan);
      $("col-do-panel").classList.toggle("hidden", !scan);
      $("col-rules-box").classList.toggle("hidden", !scan);
      $("col-actions").classList.toggle("hidden", !scan);
      $("col-inplace-opts").classList.toggle("hidden", !inp);
      $("col-elsewhere").classList.toggle("hidden", inp);
      $("col-apply-btn").textContent = inp ? "Build library" : "Build collection";
      $("col-restore").classList.toggle("hidden", !inp);
      const move = $("col-mode").value === "move";
      if (move) $("col-sync").checked = false;
      $("col-sync").disabled = move;
      const toConvert = scan ? scan.systems.reduce((n, x) => n + (x.convert || 0), 0) : 0;
      const chdN = scan ? scan.systems.filter((x) => x.chd).reduce((n, x) => n + (x.convert || 0), 0) : 0;
      $("col-convert-wrap").classList.toggle("hidden", !inp || !toConvert);
      $("col-convert-label").textContent = `Convert first: ${chdN ? `${fmt(chdN)} raw disc set(s) to CHD` : ""}${chdN && toConvert > chdN ? " and " : ""}${toConvert > chdN ? `${fmt(toConvert - chdN)} SNES / N64 dump(s) to the database's format` : ""} (the originals move to _converted_originals/; this takes a while)`;
      $("col-scan-btn").textContent = scan ? "Rescan folder" : "Scan folder";
      $("col-scan-btn").title = scan ? "Reads new and changed files only: what was read before is remembered (also after a stopped scan)" : "";
      $("col-scan-force-btn").classList.toggle("hidden", !scan);
      if (!scan) {
        $("col-scan-info").textContent = $("col-root").value.trim()
          ? "Not scanned yet. The scan reads every file once; after it, previewing and tidying are quick."
          : "Choose the folder with your ROMs, then scan it.";
        $("col-scan-notes").classList.add("hidden");
      } else {
        const games = scan.systems.reduce((n, x) => n + x.games, 0);
        $("col-scan-info").textContent = `Scanned ${fmt(scan.files)} files (${fmtBytes(scan.bytes)}) in ${fmtDuration(scan.seconds)}: ${fmt(games)} games in ${scan.systems.length} system${scan.systems.length === 1 ? "" : "s"}${scan.unmatched ? `, ${fmt(scan.unmatched)} ${scan.unmatched === 1 ? "file matches" : "files match"} nothing` : ""}${scan.other ? `, ${fmt(scan.other)} ${scan.other === 1 ? "is not a ROM" : "are not ROMs"}` : ""}${scan.ambiguous ? `, ${fmt(scan.ambiguous)} ${scan.ambiguous === 1 ? "fits" : "fit"} more than one system` : ""}. Scan again after you add or remove files.`;
        $("col-scan-notes").classList.toggle("hidden", !scan.notes.length);
        $("col-scan-notes").replaceChildren(...scan.notes.map((n) => el("div", { text: n })));
        $("col-systems").replaceChildren(...scan.systems.map((x) => {
          const folder = el("td", { class: "mono small", text: x.hint });
          return el("tr", {},
            el("td", { text: x.name }), folder, el("td", { class: "num", text: fmt(x.games) }), el("td", { class: "num", text: fmt(x.files) }),
            ...(scan.saves ? [el("td", { class: "num", "data-saves-of": x.name, title: x.saves ? `${savesText(x.saves)}; ${fmt(x.saves.rom_sets)} with a ROM here, ${fmt(x.saves.dat_sets)} without, ${fmt(x.saves.unmatched_sets)} unmatched` : "",
              text: x.saves && x.saves.files ? `${fmt(x.saves.files)} (${nPlural(x.saves.sets, "game", "games")})` : "-" })] : []),
            el("td", {}, el("input", { type: "checkbox", checked: x.own_rules, "aria-label": `${x.name} uses its own rules`,
              on: { change: (e) => this.save({ systems: { [x.name]: { own_rules: e.target.checked } } }) } })));
        }));
        $("col-saves-th").classList.toggle("hidden", !scan.saves);
        $("col-saves-line").classList.toggle("hidden", !scan.saves);
        $("col-saves-line").textContent = scan.saves
          ? `Saves (${(scan.saves.sources && scan.saves.sources.length ? scan.saves.sources : ["RetroArch"]).join(", ")}): ${scan.saves.sets ? `${nPlural(scan.saves.files, "save file", "save files")} for ${nPlural(scan.saves.sets, "game", "games")} (${fmt(scan.saves.rom_sets)} with a ROM here, ${fmt(scan.saves.dat_sets)} without, ${fmt(scan.saves.unmatched_sets)} unmatched)` : "none found"}.` : "";
        $("col-leftovers").textContent = scan.unmatched || scan.other
          ? `${fmt(scan.unmatched)} ${scan.unmatched === 1 ? "file matches" : "files match"} no game and ${fmt(scan.other)} ${scan.other === 1 ? "is not a ROM" : "are not ROMs"}: they are listed in the preview.` : "";
      }
      $("col-aside").placeholder = info.aside_default ? `Default: ${info.aside_default}` : "Archive folder (or set one for every system in Settings)";
      const hasArchive = !!($("col-aside").value.trim() || info.aside_default);
      $("col-sweep").disabled = !hasArchive;
      $("col-aside-none").classList.toggle("hidden", hasArchive);
      $("col-restore").classList.toggle("hidden", !hasArchive || !inp);
      $("col-undo-btn").dataset.blocked = info.last && Object.keys(info.last).length && (info.last.runs && Object.keys(info.last.runs).length || info.last.sort || (info.last.archive && info.last.archive.length) || info.last.rename || info.last.sweep) ? "0" : "1";
      this.renderRules();
      Jobs.setRunning(Jobs.running);
    },

    rulesBody(extra = {}) {
      const p = this.info.profile;
      return { exclude: p.exclude, latest_only: p.latest_only, one_per_game: p.one_per_game, keep_other_language: p.keep_other_language, languages: p.languages,
        region_priority: p.region_priority, keep_flags: p.keep_flags, ...(p.saved_games ? { saved_games: p.saved_games } : {}), ...extra };
    },

    setRules(changes) { return this.save({ global: this.rulesBody(changes) }); },

    renderRules() {
      const info = this.info, p = info.profile;
      const tickRow = (label, checked, onChange, hint = "") => el("label", { class: "check rule-check" },
        el("input", { type: "checkbox", checked, on: { change: (e) => onChange(e.target.checked) } }),
        el("span", { class: "rule-text" }, el("span", { class: "rule-line" }, el("span", { class: "rule-label", text: label })),
          hint ? el("span", { class: "sub", text: hint }) : null));
      const toggle = (list, key, on) => (on ? [...list, key] : list.filter((x) => x !== key));
      const groups = [];
      groups.push(el("div", { class: "rules-group" },
        el("div", { class: "rules-head", text: "Leave out (not kept)" }),
        el("div", { class: "rules-grid" }, info.rules.map((r) => tickRow(r.label, p.exclude.includes(r.key), (on) => this.setRules({ exclude: toggle(p.exclude, r.key, on) }))))));
      groups.push(el("div", { class: "rules-group" },
        el("div", { class: "rules-head", text: "Options" }),
        el("div", { class: "rules-grid" },
          tickRow("One version per game", p.one_per_game, (on) => this.setRules({ one_per_game: on }), "Systems with regions (No-Intro / Redump): the best region wins."),
          tickRow("Keep games that exist only in other languages", p.keep_other_language, (on) => this.setRules({ keep_other_language: on }), "No-Intro and Redump: a game with no version in your languages anywhere in the database (a Japan-only release) is kept instead of left out."),
          tickRow("Latest versions only", p.latest_only, (on) => this.setRules({ latest_only: on }), "Older revisions are left out where the DAT has versions."))));
      const langs = Object.entries(info.languages);
      groups.push(el("div", { class: "rules-group" },
        el("div", { class: "rules-head", text: "Languages" }),
        el("div", { class: "sub note", text: "Games with no version in a ticked language are left out. Nothing ticked: every language stays. Applies to systems with language tags." }),
        el("div", { class: "lang-grid" }, langs.map(([code, name]) => el("label", { class: "check lang-check" },
          el("input", { type: "checkbox", checked: p.languages.includes(code),
            on: { change: (e) => this.setRules({ languages: toggle(p.languages, code, e.target.checked) }) } }),
          el("span", { text: name })))),
        p.languages.length > 1 ? el("div", { class: "lang-order" }, el("span", { class: "muted small", text: "Preferred first:" }),
          ...p.languages.map((c, i) => el("span", { class: "lang-pill" }, el("span", { text: `${i + 1}. ${info.languages[c] || c}` }),
            el("button", { class: "btn btn-icon", text: "▲", disabled: i === 0, "aria-label": `Move ${info.languages[c] || c} up`, on: { click: () => this.setRules({ languages: moved(p.languages, i, -1) }) } }),
            el("button", { class: "btn btn-icon", text: "▼", disabled: i === p.languages.length - 1, "aria-label": `Move ${info.languages[c] || c} down`, on: { click: () => this.setRules({ languages: moved(p.languages, i, 1) }) } })))) : null));
      const ui = sortableList({ items: info.regions, label: "Region priority, best region first", noun: "region",
        onCommit: (order) => this.setRules({ region_priority: order }) });
      groups.push(el("div", { class: "rules-group", id: "col-regions" },
        el("div", { class: "rules-head", text: "Region priority" }),
        el("div", { class: "sub note", text: "Which region's version is kept when a game has several, best region first. The whole list counts, in this order; regions you have not moved stay in alphabetical order after the ones you placed. Applies to No-Intro and Redump systems." }), ui.root));
      if (info.keep_flags.length) {
        groups.push(el("div", { class: "rules-group" },
          el("div", { class: "rules-head", text: "Keep these kinds of variants (Amiga TOSEC)" }),
          el("div", { class: "rules-grid" }, info.keep_flags.map((f) => tickRow(f.label, p.keep_flags.includes(f.id), (on) => this.setRules({ keep_flags: toggle(p.keep_flags, f.id, on) }))))));
      }
      if (p.saved_games !== undefined) groups.push(savesGroup(p.saved_games, (v) => this.setRules({ saved_games: v }), "col"));
      $("col-rules").replaceChildren(...groups);
      this.loadSavesGames();
      const custom = Object.keys(info.global).length > 0;
      $("col-rules-note").textContent = custom ? "Shared rules are set." : "Nothing set: every system uses its own defaults.";
      $("col-rules-summary").textContent = custom ? `${p.exclude.length} exclusions \u00B7 ${p.languages.length ? p.languages.join(", ") : "all languages"} \u00B7 ${p.region_priority.slice(0, 2).join(" > ")} first` : "defaults";
    },

    // ---- scan, preview, build, undo
    async scan() {
      const root = $("col-root").value.trim();
      if (!root) { toast("Choose the folder with your ROMs first.", "error"); return; }
      $("col-output").classList.add("hidden");
      await Jobs.start("/api/collection/scan", { root });
    },

    async rescanAll() {
      const root = $("col-root").value.trim();
      if (!root) return;
      if (!(await confirmDialog({ title: "Recalculate every checksum", okText: "Recalculate",
        body: el("p", { text: "Every file is read again and its checksums are calculated again. A normal rescan reads only new and changed files, which is almost always enough. This can take a long time." }) }))) return;
      $("col-output").classList.add("hidden");
      await Jobs.start("/api/collection/scan", { root, force: true });
    },

    async run(path, body = {}) {
      if (!this.info || !this.info.scan) { toast("Scan the folder first.", "error"); return false; }
      if (this.inplace()) {
        await this.save({ place: "inplace", aside: $("col-aside").value.trim(), sweep: $("col-sweep").checked });
      } else {
        if (!$("col-dest").value.trim()) { toast("Choose the folder to build the collection in.", "error"); return false; }
        await this.save({ place: "elsewhere", dest: $("col-dest").value.trim(), mode: $("col-mode").value, sidecars: $("col-sidecars").checked, sync: $("col-sync").checked });
      }
      Jobs.start(path, body);
      return true;
    },

    async apply() {
      const inp = this.inplace();
      const sure = await confirmDialog(inp ? {
        title: "Build the library in this folder", danger: true, okText: "Apply",
        body: el("div", {},
          el("p", { text: `Sort ${fmt(this.info.scan.systems.reduce((n, x) => n + x.files, 0))} files of ${this.info.scan.systems.length} system(s) in ${$("col-root").value.trim()}.` }),
          el("ul", {},
            el("li", { text: "Every file moves into its system's folder, named with the standard short names. Folders left empty are removed." }),
            ...([el("li", { text: "Files are renamed to the databases' names and sorted by the rules." }),
              ...($("col-convert").checked && !$("col-convert-wrap").classList.contains("hidden") ? [el("li", { text: $("col-convert-label").textContent.replace(/\.$/, "") + "." })] : []),
              el("li", { text: $("col-sweep").checked && $("col-aside").value.trim() + this.info.aside_default ? `What the rules archive is moved out into ${$("col-aside").value.trim() || this.info.aside_default}.` : "What the rules archive goes to _excluded, _superseded ... inside each system's folder." })]),
            el("li", { text: $("col-aside").value.trim() + this.info.aside_default ? "Files that match nothing, and files that are not ROMs (a save or a note beside a ROM too), go to the archive folder. Nothing is deleted."
              : "Files that match nothing, and files that are not ROMs, stay where they are (no archive folder is set). Nothing is deleted." }),
            ...(this.last ? savesLines(colSavesTotal(this.last)) : []),
            el("li", { text: "\"Undo last\" puts everything back." }))) } : {
        title: "Build the collection", okText: "Build collection",
        body: el("div", {},
          el("p", { text: `Build ${this.info.scan.systems.length} system(s) from ${$("col-root").value.trim()} into ${$("col-dest").value.trim()}.` }),
          el("ul", {},
            el("li", { text: $("col-mode").value === "move" ? "Move: the files leave the ROM folder. Undo moves them back." : "Copy: the ROM folder keeps its files." }),
            el("li", { text: "Only what the rules keep is built. Nothing in the destination is overwritten; running it again adds only what is missing." }),
            ...($("col-sync").checked ? [el("li", { text: "SYNC is on: files built before that the rules no longer keep (or whose source is gone) are removed from the destination. Preview first to see how many." })] : []),
            el("li", { text: "\"Undo last\" removes what this build added." }))) });
      if (!sure) return;
      const mass = !inp && this.last && this.last.sync && (this.last.systems || []).some((x) => x.mass_removal);
      if (mass && !(await confirmDialog({ title: "Remove most of a library?", danger: true, okText: "Yes, remove them",
        body: `The last preview shows a sync that removes most of the files built before in: ${this.last.systems.filter((x) => x.mass_removal).map((x) => x.platform).join(", ")}. Only do this if you changed the rules on purpose and the source is complete.` }))) return;
      this.run("/api/collection/apply", mass ? { allow_mass_removal: true } : {});
    },

    async undo() {
      if (!(await confirmDialog({ title: "Undo last", body: "Put everything the last collection run did back: renamed folders, moved files, archived files. Files you changed or moved since are left.", okText: "Undo", danger: true }))) return;
      try {
        const r = await post("/api/collection/undo", {});
        toast(`${r.removed || !r.restored ? `Removed ${fmt(r.removed)} file(s)` : `Put back ${fmt(r.restored)} file(s)`}${r.removed && r.restored ? `, put back ${fmt(r.restored)}` : ""}${r.skipped.length ? `, ${fmt(r.skipped.length)} left (changed or not restorable)` : ""}. Scan again to see the folder as it is now.`, r.skipped.length || r.errors.length ? "error" : "ok", 9000);
        this.adopt(await get("/api/collection"));
        this.last = null;
        $("col-output").classList.add("hidden");
      } catch (err) { toast(err.message, "error"); }
    },

    async restoreAside() {
      if (!(await confirmDialog({ title: "Bring archived files back", body: "Move everything in the archive folder back into each system's folder (_excluded, _superseded ...)? The next tidy decides again.", okText: "Bring back" }))) return;
      try {
        const r = await post("/api/collection/aside/restore", {});
        toast(`Moved ${fmt(r.moved)} file(s) back${r.failed.length ? `, ${fmt(r.failed.length)} failed` : ""}. Scan again to see the folder as it is now.`, r.failed.length ? "error" : "ok", 8000);
        this.adopt(await get("/api/collection"));
      } catch (err) { toast(err.message, "error"); }
    },

    // ---- what a preview / build answers
    /** "Folders are renamed to the standard short names": what will be (or was) renamed, shown with every preview and result. */
    showRenames(r) {
      const rows = r.renames || [];
      const box = $("col-renames");
      box.classList.toggle("hidden", !rows.length);
      if (!rows.length) return;
      const apply = /_apply$/.test(r.action || "");
      const verb = (x) => (x.status === "conflict" ? "left alone" : x.status === "failed" ? "could not be renamed" : x.status === "in the destination" ? "in the destination" : apply ? "renamed" : "will be renamed");
      box.replaceChildren(el("div", { text: "Folder names (the standard short names of EmulationStation-DE, EmuDeck and RetroDECK):" }),
        ...rows.map((x) => el("div", { class: "mono small", text: `${x.platform}: ${(x.from || "").split(/[\\/]/).pop()} -> ${(x.to || "").split(/[\\/]/).pop()}  (${verb(x)}${x.note ? `: ${x.note}` : ""})` })));
    },

    head(cols) {
      $("col-thead").replaceChildren(el("tr", {}, ...cols.map(([text, num]) => el("th", { class: num ? "num" : "", text }))));
    },

    showInplace(r) {
      const t = r.totals, apply = r.action === "collection_apply", sort = r.sort || { counts: {}, total: 0 };
      const moved = r.systems.reduce((n, x) => n + ((x.result || {}).moved || 0), 0);
      const aside = t.excluded + t.superseded + t.incomplete + t.duplicates;
      const sorted = Object.entries(sort.counts).filter(([k]) => !k.startsWith("_") && k !== "sidecar" && k !== "rename").reduce((n, [, v]) => n + v, 0);
      $("col-output").classList.remove("hidden");
      $("col-cards").replaceChildren(
        card(fmt(r.systems.length || Object.keys(sort.counts).length), "Systems", "info"),
        card(fmt(sorted), apply ? "Files sorted into systems" : "Files to sort into systems", sorted ? "info" : "ok"),
        ...(r.systems.some((x) => x.convert) ? [card(fmt(r.systems.reduce((n, x) => n + (x.convert || 0), 0)), "To convert first", "info")] : []),
        ...((sort.counts._unmatched || t.unmatched) ? [card(fmt((sort.counts._unmatched || 0) + (t.unmatched || 0)), "Match nothing: archived", "warn")] : []),
        ...(sort.counts._other ? [card(fmt(sort.counts._other), "Not ROMs: archived", "warn")] : []),
        ...(r.systems.length ? [card(fmt(t.kept), "Kept", "ok"), card(fmt(t.renamed + t.moved), apply ? "Files renamed or moved to the databases' names" : "Files to rename or move", t.renamed + t.moved ? "info" : "ok"),
          card(fmt(aside), "Archived by the rules", aside ? "warn" : "")] : []),
                ...(t.conflict ? [card(fmt(t.conflict), "Conflicts (left alone)", "bad")] : []),
        ...(r.aside ? [card(fmt(r.aside.moved !== undefined ? r.aside.moved : r.aside.files), r.aside.moved !== undefined ? "Moved out of the ROM folders" : "Files to archive", "info")] : []),
        ...savesCards(colSavesTotal(r), apply),
        ...(r.saves_archived ? [card(fmt(r.saves_archived.files), r.saves_archived.skipped_running ? `Saves NOT archived: ${(r.saves_archived.running || ["RetroArch"]).join(", ")} ${(r.saves_archived.running || [1]).length > 1 ? "are" : "is"} running` : "Save files archived with their games", r.saves_archived.skipped_running ? "bad" : "warn")] : []),
        ...(apply ? [card(fmt(moved), "Files tidied now", "ok")] : [card(fmt(t.actionable), "Library changes", t.actionable ? "info" : "ok")]));
      this.head([["System"], ["Result"], ["Games", 1], ["Kept", 1], ["Changed", 1], ["Archived", 1], ["Notes"]]);
      // files that match no database belong to no system: one line, one total (those in system folders are archived too)
      const unmatchedAll = (sort.counts._unmatched || 0) + (t.unmatched || 0);
      const unmatchedRow = unmatchedAll ? el("tr", {},
        el("td", { text: "Unmatched files" }), el("td", {}, badge("warn", apply ? "archived" : "to archive")), el("td", { class: "num", text: "" }),
        el("td", { class: "num", text: "" }), el("td", { class: "num", text: "" }), el("td", { class: "num", text: fmt(unmatchedAll) }),
        el("td", { class: "notes small", text: "Match nothing in any database, so they belong to no system." })) : null;
      $("col-table").replaceChildren(...r.systems.map((x) => {
        const c = x.counts || {}, res = x.result;
        const text = x.status !== "ok" ? x.error || x.status : res ? `tidied ${fmt(res.moved)}` : x.actionable ? `${fmt(x.actionable)} changes` : "already tidy";
        return el("tr", {},
          el("td", { text: x.platform + (x.own_rules ? " (own rules)" : "") }), el("td", {}, badge(x.status === "ok" ? "ok" : "bad", text)),
          el("td", { class: "num", text: fmt(x.games || 0) }), el("td", { class: "num", text: fmt(c.kept || 0) }),
          el("td", { class: "num", text: fmt((c.renamed || 0) + (c.moved || 0)) }),
          el("td", { class: "num", text: fmt((c.excluded || 0) + (c.superseded || 0) + (c.incomplete || 0) + (c.duplicates || 0)) }),
          el("td", { class: "notes small", text: [...(x.failed || []).slice(0, 3).map((f) => `${f.src || f.path || ""}: ${f.error || "failed"}`),
            ...(x.saves && x.saves.followed ? [`${fmt(x.saves.followed)} save file(s) renamed`] : [])].join(" \u00B7 ") }));
      }), ...(unmatchedRow ? [unmatchedRow] : []));
      showFailures("col-failures", "Problems:", [...r.systems.filter((x) => x.status !== "ok"),
        ...(((sort.result || {}).failed) || []).map((f) => ({ platform: "Sorting", error: `${f.path}: ${f.error}`, status: "failed" }))], (x) => `${x.platform}: ${x.error || x.status}`);
      if (apply) toast(`Done: ${fmt((sort.result || {}).moved || 0)} file(s) sorted, ${fmt(moved)} tidied${r.systems.some((x) => x.status !== "ok" || (x.failed || []).length) ? " (with problems)" : ""}`,
        r.systems.some((x) => x.status !== "ok" || (x.failed || []).length) ? "error" : "ok", 9000);
    },

    showResult(r) {
      this.last = r;
      this.showRenames(r);
      if (r.place === "inplace") { this.showInplace(r); return; }
      this.head([["System"], ["Result"], ["Files kept", 1], ["To copy", 1], ["To move", 1], ["Already there", 1], ["Notes"]]);
      const t = r.totals;
      const apply = r.action === "collection_apply";
      const built = r.systems.reduce((n, x) => n + ((x.result || {}).created || 0), 0);
      $("col-output").classList.remove("hidden");
      $("col-cards").replaceChildren(
        card(fmt(r.systems.length), "Systems", "info"),
        card(fmt(t.files), "Files kept", "ok"),
        card(fmtBytes(t.bytes_copy), "To copy", t.bytes_copy ? "info" : ""),
        ...(t.bytes_linked ? [card(fmtBytes(t.bytes_linked), "Moved on the same drive (instant)", "info")] : []),
        ...(apply ? [card(fmt(built), "Files built now", "ok")] : [card(fmt(t.pending), "Would be added", t.pending ? "info" : "")]),
        ...(t.replace ? [card(fmt(t.replace), "To replace (source changed)", "info")] : []),
        ...(t.remove ? [card(fmt(t.remove), apply ? "Removed by sync" : "To remove (no longer kept)", "warn")] : []),
        ...(t.conflicts ? [card(fmt(t.conflicts), "Conflicts (left alone)", "bad")] : []),
        ...savesCards(colSavesTotal(r), apply),
        card(fmtBytes(r.free), r.enough_space ? "Free on the destination" : "Free space is NOT enough", r.enough_space ? "" : "bad"));
      $("col-table").replaceChildren(...r.systems.map((x) => {
        const counts = x.counts || {};
        const res = x.result;
        const resultText = x.status !== "ok" ? x.error || x.status : res ? `built ${fmt(res.created)} (+${fmt(res.playlists)} playlists)${res.removed ? `, removed ${fmt(res.removed)}` : ""}` : x.pending ? `${fmt(x.pending)} to add` : "up to date";
        return el("tr", {},
          el("td", { text: x.platform }),
          el("td", {}, badge(x.status === "ok" ? "ok" : "bad", resultText)),
          el("td", { class: "num", text: fmt(x.files || 0) }),
          el("td", { class: "num", text: `${fmt(counts.copy || 0)} (${fmtBytes(x.bytes_copy || 0)})` }),
          el("td", { class: "num", text: fmt(counts.move || 0) }),
          el("td", { class: "num", text: fmt(counts.exists || 0) }),
          el("td", { class: "notes small", text: [...(x.notes || []), ...((x.counts || {}).remove ? [`${fmt(x.counts.remove)} to remove, e.g. ${x.removals.filter((q) => !q.skip).slice(0, 2).map((q) => q.rel).join(", ")}`] : []), ...(x.conflicts || []).slice(0, 3).map((c) => `${c.rel}: ${c.reason}`),
            ...(x.failed || []).slice(0, 3).map((f) => `${f.rel}: ${f.error}`)].join(" \u00B7 ") }));
      }));
      const failedAny = r.systems.some((x) => x.status !== "ok" || (x.failed || []).length);
      showFailures("col-failures", "Problems:", r.systems.filter((x) => x.status !== "ok"), (x) => `${x.platform}: ${x.error || x.status}`);
      if (apply) toast(`Built ${fmt(built)} file(s) in ${r.dest}${r.cancelled ? " - cancelled" : ""}${failedAny ? " (with problems)" : ""}`, failedAny ? "error" : "ok", 9000);
    },

    bind() {
      $("col-scan-btn").addEventListener("click", () => this.scan());
      $("col-scan-force-btn").addEventListener("click", () => this.rescanAll());
      $("col-root").addEventListener("keydown", (e) => { if (e.key === "Enter") this.scan(); });
      $("col-root").addEventListener("change", () => this.save({ root: $("col-root").value.trim() }));
      $("col-root-browse").addEventListener("click", () => FolderBrowser.open($("col-root"), "Choose the folder with your ROMs"));
      $("col-dest-browse").addEventListener("click", () => FolderBrowser.open($("col-dest"), "Choose the folder to build the collection in"));
      $("col-aside-browse").addEventListener("click", () => FolderBrowser.open($("col-aside"), "Choose the archive folder"));
      $("col-dest").addEventListener("change", () => this.save({ dest: $("col-dest").value.trim() }));
      $("col-aside").addEventListener("change", () => this.save({ aside: $("col-aside").value.trim() }));
      $("col-mode").addEventListener("change", () => this.save({ mode: $("col-mode").value }));
      $("col-sidecars").addEventListener("change", () => this.save({ sidecars: $("col-sidecars").checked }));
      $("col-sync").addEventListener("change", () => this.save({ sync: $("col-sync").checked }));
      $("col-sweep").addEventListener("change", () => this.save({ sweep: $("col-sweep").checked }));
      $("col-convert").addEventListener("change", () => this.save({ convert: $("col-convert").checked }));
      for (const id of ["col-place-elsewhere", "col-place-inplace"]) {
        $(id).addEventListener("change", () => { this.save({ place: this.inplace() ? "inplace" : "elsewhere" }); this.render(); $("col-output").classList.add("hidden"); });
      }
      $("col-rules-reset").addEventListener("click", () => this.save({ global: {} }));
      $("col-saves-games").addEventListener("toggle", () => this.loadSavesGames());
      $("col-saves-q").addEventListener("input", debounce(() => this.loadSavesGames(), 250));
      $("col-plan-btn").addEventListener("click", () => this.run("/api/collection/plan"));
      $("col-apply-btn").addEventListener("click", () => this.apply());
      $("col-undo-btn").addEventListener("click", () => this.undo());
      $("col-restore").addEventListener("click", () => this.restoreAside());
    },
  };

  Jobs.handlers.collection = async (job) => {
    const r = job.result;
    if ((job.status === "done" || job.status === "cancelled") && r && r.action !== "collection_scan") Collection.showResult(r);
    try { Collection.adopt(await get("/api/collection")); } catch (_) { /* keep what is shown */ }
    if (job.status === "done" && r && r.action === "collection_scan" && Collection.info && Collection.info.scan) {
      toast(`Scanned: ${fmt(Collection.info.scan.systems.reduce((n, x) => n + x.games, 0))} games in ${Collection.info.scan.systems.length} systems`, "ok", 6000);
    }
  };

  // ----------------------------------------------------------- shared UI
  function updateActionState() {
    const hasScan = !!state.scan;
    const canScan = !!state.platform && !!folderOf(currentPlatform()).trim();
    $("scan-btn").dataset.blocked = canScan ? "0" : "1";
    for (const id of ["lib-plan-btn"]) $(id).dataset.blocked = hasScan ? "0" : "1";
    if (!hasScan) for (const id of ["lib-undo-btn"]) $(id).dataset.blocked = "1";
    if (!hasScan || libTable === null) $("lib-apply-btn").dataset.blocked = hasScan ? "0" : "1";
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
    Collection.bind();
    RetroArch.bind();
    $("lib-apply-btn").addEventListener("click", applyLibrary);
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
        state.library[state.platform] = await post("/api/library/profile", { platform: state.platform, reset: true });
        const p = currentPlatform();
        if (p) { p.library = state.library[state.platform].profile; p.latest_only = !!p.library.latest_only; }
            profileChanged();
        renderLibraryRules();
      } catch (err) { toast(err.message, "error"); }
    });
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
    tabKeys($("result-tabs"));
    window.addEventListener("hashchange", applyRoute);
    $("chdman-save").addEventListener("click", () => saveChdman({ path: $("chdman-path").value.trim() }));
    $("chdman-refresh").addEventListener("click", () => loadChdman(true));
    $("chdman-engine").addEventListener("change", (e) => saveChdman({ engine: e.target.value }));
    $("chd-writer").addEventListener("change", (e) => saveChdman({ writer: e.target.value }));
    $("chd-preset").addEventListener("change", (e) => saveChdman({ preset: e.target.value }));
    $("lib-convert").addEventListener("change", saveConvertOption);
    $("chd-verify-scan").addEventListener("change", (e) => saveChdman({ verify_scan: e.target.checked }));
    $("quit-btn").addEventListener("click", quit);
    $("databases-btn").addEventListener("click", () => toggleDatabases());
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

  /** The single folder field of the Overview tab: edits are saved right away (no Save button to forget). */
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
