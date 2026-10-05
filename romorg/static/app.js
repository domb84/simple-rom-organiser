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
    gamesRated: "",    // "" | "1" (rated) | "0" (unrated)  -  Browse > Games
    gamesSort: "",     // "" | rating_desc | rating_asc | name_asc | name_desc
    libSort: "",       // the same, for the Library preview
    vanishSort: "",    // ... and for "Games that vanish"
    kindSort: {},      // Browse: result kind -> sort of the matched / unmatched / missing tabs
    convertFilter: "",
    organiseFilter: "",
    organiseDest: "",
    m3uFilter: "",
    kickFilter: "",
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
        if (!job.platform || job.platform === state.platform) {
          $("scan-error").classList.toggle("hidden", !failed);
          $("scan-error-text").textContent = failed ? job.error : "";
        }
        state.scanFailed[job.platform || ""] = failed ? job.error : "";
      }
      if (job.status === "cancelled") toast(`${job.kind} cancelled`, "info");
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
      if (total > 0) countText = isBytes ? `${fmtBytes(done)} / ${fmtBytes(total)}` : `${fmt(done)} / ${fmt(total)}`;
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
      renderCardJob(job, msg, countText, pct);
      clearTimeout(this.hideTimer);
      if (job.status === "done" || job.status === "cancelled") this.hideTimer = setTimeout(() => box.classList.add("hidden"), 8000);
    },
  };

  const JOB_LABEL = { scan: "Scan", verify: "Verify", library: "Library", organise: "Organise", convert: "Convert",
    m3u: "Playlists", kickstart: "Kickstarts" };


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
    $("kick-native-browse-btn").classList.toggle("hidden", !s.dialog_available);
    $("folder-native-btn").classList.toggle("hidden", !s.dialog_available);
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
  //   #/system/<slug>/<tab>[?view=...]    <tab> = overview | library | browse | tools
  const TABS = ["overview", "library", "browse", "tools"];
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
    ["computers", "Computers", (p) => !isGameFolder(p) && p.source !== "nointro"],
    ["consoles", "Cartridge consoles", (p) => !isGameFolder(p) && p.source === "nointro"],
    ["discs", "Disc systems", (p) => isGameFolder(p)],
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

  /** What the card's single primary button does in the system's current state. */
  function cardAction(p) {
    const rec = p.last_scan;
    const live = state.lastScan && state.lastScan.platform === p.name;
    if (!p.folder) return { label: "Set folder", kind: "folder" };
    if (!rec && !live) return { label: "Scan", kind: "scan" };
    if (rec && rec.folder && rec.folder !== p.folder) return { label: "Scan", kind: "scan" };
    return { label: "Build library", kind: "library" };
  }

  function systemCard(p) {
    const rec = p.last_scan;
    const dat = datStatus(p);
    const action = cardAction(p);
    const stale = !!(rec && rec.folder && p.folder && rec.folder !== p.folder);
    const disc = rec && rec.chd_files !== undefined;
    const have = rec ? rec.have : 0, total = rec ? rec.total : 0;
    const pct = rec ? (rec.pct !== undefined ? rec.pct : (total ? (100 * have) / total : 0)) : 0;
    const result = rec ? el("div", { class: "syscard-result" },
      el("div", { class: "syscard-big" },
        el("span", { class: "value", text: fmt(have) }), el("span", { class: "muted", text: ` of ${fmt(total)} (${pctText(pct)})` })),
      progressBar(pct),
      el("div", { class: "syscard-stats" },
        el("span", { class: "stat-missing", text: `Missing ${fmt(rec.missing)}` }),
        ...(disc ? [el("span", { text: `Identified ${fmt(rec.identified)}` }), el("span", { text: `Verified ${fmt(rec.verified)}` })] : [])))
      : el("div", { class: "syscard-result muted", text: "Not scanned yet" });
    const when = rec ? `Scanned ${scanTime(rec)}${stale ? " (folder changed since)" : ""}` : "";
    const job = el("div", { class: "card-job hidden" },
      el("div", { class: "cj-msg small" }), el("div", { class: "progress" }, el("div", { class: "progress-bar" })));
    const button = el("button", {
      class: "btn btn-primary syscard-action", text: action.label, "data-action": action.kind,
      on: { click: (e) => { e.stopPropagation(); cardPrimary(p, action.kind); } },
    });
    if (action.kind === "scan" || action.kind === "library") button.setAttribute("data-needs-idle", "");
    if (action.kind === "scan") button.setAttribute("data-scan", "");
    return el("article", { class: `syscard ${dat.kind === "missing" ? "dat-missing" : ""}`, "data-platform": p.name },
      el("h3", { class: "syscard-title" }, el("a", { class: "syscard-link", href: Route.build(slugOf(p), "overview"), text: p.name })),
      el("div", { class: "syscard-meta" },
        el("span", { class: `badge src-${p.source}`, text: sourceLabel(p.source) }),
        el("span", { class: `dat-chip ${dat.kind}`, text: dat.text, title: dat.title || null })),
      p.folder ? el("div", { class: "mono syscard-folder", title: p.folder }, el("bdi", { text: p.folder }))
        : el("div", { class: "muted syscard-nofolder", text: "No folder set" }),
      result, job,
      el("div", { class: "syscard-foot" }, el("span", { class: "muted small syscard-when", text: when }), button));
  }

  function renderHome() {
    const box = $("home-groups");
    if (!state.platforms.length) { box.replaceChildren(el("div", { class: "empty", text: "No systems defined." })); return; }
    const groups = GROUPS.map(([key, title, test]) => {
      const list = state.platforms.filter((p) => groupOf(p) === key);
      return list.length ? el("section", { class: "group", "aria-label": title },
        el("h2", { class: "group-title", text: title }), el("div", { class: "syscards" }, list.map(systemCard))) : null;
    });
    box.replaceChildren(...groups.filter(Boolean));
    Jobs.setRunning(Jobs.running);
    if (Jobs.last) renderCardJob(Jobs.last);
  }

  /** The card's primary button. Scan: also start it. Set folder: choose it right here. Build library: open the tab. */
  function cardPrimary(p, kind) {
    if (kind === "folder") chooseFolder(p);
    else if (kind === "scan") scanPlatform(p.name);
    else gotoSystem(p, "library");
  }

  /** Choose a system's folder (native dialog when there is one, else the in-app browser); saved immediately. */
  function chooseFolder(p) {
    const holder = el("input", { type: "text", value: p.folder || "" });
    holder.addEventListener("change", async () => {
      state.drafts[p.name] = holder.value;
      await commitFolder(p);
      renderHome();
    });
    const title = `Choose the ${p.name} folder`;
    if (state.status && state.status.dialog_available) nativeBrowse(null, holder, title);
    else FolderBrowser.open(holder, title);
  }

  /** Live progress of the running job on the card of its system (the other cards stay as they are). */
  function renderCardJob(job, msg, countText, pct) {
    if (!job) return;
    if (msg === undefined) {
      const pr = job.progress || {};
      msg = pr.message || "";
      pct = pr.total > 0 ? Math.min(100, (100 * pr.done) / pr.total) : null;
    }
    document.querySelectorAll(".syscard").forEach((card) => {
      const box = card.querySelector(".card-job");
      const mine = job.status === "running" && !!job.platform && card.dataset.platform === job.platform;
      card.classList.toggle("running", mine);
      box.classList.toggle("hidden", !mine);
      if (!mine) return;
      box.querySelector(".cj-msg").textContent = `${JOB_LABEL[job.kind] || job.kind}: ${msg}${pct !== null && pct !== undefined ? ` (${pct.toFixed(0)}%)` : ""}`;
      const bar = box.querySelector(".progress");
      bar.classList.toggle("indeterminate", pct === null || pct === undefined);
      box.querySelector(".progress-bar").style.width = pct === null || pct === undefined ? "0%" : `${pct}%`;
    });
  }

  // ----------------------------------------------- system page header + folder field
  function renderSystemHead() {
    const p = currentPlatform();
    if (!p) return;
    $("sys-title").textContent = p.name;
    $("sys-source").textContent = sourceLabel(p.source);
    $("sys-source").className = `badge src-${p.source}`;
    $("sys-sub").textContent = p.extensions && p.extensions.length ? p.extensions.join(" ") : "";
    $("folder-example").textContent = `/run/media/deck/<SD>/roms/${p.folder_hint || "..."}`;
    $("folder-input").placeholder = `/run/media/deck/<SD>/roms/${p.folder_hint || "..."}`;
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

  function renderPlatform() {
    const p = currentPlatform();
    state.rendered = p ? p.name : null;
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
      el("b", { text: missing.length === (p ? p.dats.length : 0) ? `The DAT for ${p ? p.name : "this system"} is not installed yet. ` : `${missing.length} of ${p ? p.dats.length : 0} DATs for ${p ? p.name : "this system"} are not installed yet. ` }),
      updating ? "It is being downloaded now (see the update line at the top)."
        : "It is downloaded automatically - just press Scan, or use Check for updates at the top.");
    const ds = p ? datStatus(p) : { kind: "missing", text: "" };
    $("dat-status-line").replaceChildren(
      el("span", { class: `dat-chip ${ds.kind}`, text: ds.text }),
      p ? el("span", { class: "muted small", text: ` ${p.dats.length} DAT${p.dats.length === 1 ? "" : "s"} · ${sourceLabel(p.source)}` }) : null);

    // Organise: what goes where for this layout.
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
    renderSystemHead();
    renderScanTarget();
    updateActionState();
  }

  // ------------------------------------------------------------------ tabs
  const toolsApply = (p) => !!p && (!!p.convertible || hasKickstart(p) || isGameFolder(p));

  /** Show only the tools this system uses; hide the Tools tab when nothing applies. */
  function renderTabs() {
    const p = currentPlatform();
    const show = { "tool-convert": !!(p && p.convertible), "tool-verify": isGameFolder(p), "tool-kickstart": hasKickstart(p) };
    for (const [id, on] of Object.entries(show)) $(id).classList.toggle("hidden", !on);
    $("tabbtn-tools").classList.toggle("hidden", !toolsApply(p));
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
    if (tab === "tools" && !toolsApply(p)) tab = "overview";
    state.tab = tab;
    renderTabs();
    if (tab === "overview" || tab === "library") loadTotals();
    if (tab === "library") {
      renderLibraryGate();
      if (statsStale) refreshLibraryStats();
    } else if (tab === "browse") {
      renderBrowse();
    } else if (tab === "tools") {
      if (hasKickstart(p) && !kickDirsLoaded) loadKickDirs();
      if (isGameFolder(p)) loadChdman();
    }
  }

  /** Apply the address: home, or a system with its tab (and Browse filters). */
  function applyRoute() {
    state.route = Route.parse(location.hash);
    const r = state.route;
    const home = r.view === "home" || !state.platforms.length;
    let p = home ? null : platformBySlug(r.slug);
    if (!home && !p) { if (state.platforms.length) history.replaceState(null, "", "#/"); state.route = Route.parse("#/"); p = null; }
    $("view-home").classList.toggle("hidden", !!p);
    $("view-system").classList.toggle("hidden", !p);
    if (!p) { document.title = "Simple ROM Organiser"; state.viewWas = "home"; renderHome(); window.scrollTo(0, 0); return; }
    document.title = `${p.name} - Simple ROM Organiser`;
    if (p.name !== state.platform) selectPlatform(p.name);
    else if (state.rendered !== p.name || state.viewWas !== "system") { renderPlatform(); applyScan(); }
    if (r.tab === "browse") applyBrowseParams(r.params);
    state.viewWas = "system";
    renderSystemHead();
    showTab(r.tab);
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
    state.kickFilter = "";
    activeTab = "";
    kickDirsLoaded = false;                       // each system has its own Kickstart destination
    $("lib-labels").checked = !isGameFolder(currentPlatform());   // |Disc N labels are PUAE syntax: off for disc-system playlists
    setKickDest(currentPlatform());
    $("folder-input").value = folderOf(currentPlatform());
    $("scan-error").classList.toggle("hidden", !state.scanFailed[name]);
    $("scan-error-text").textContent = state.scanFailed[name] || "";
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
    if (btn) btn.disabled = true;
    try {
      const res = await post("/api/fs/pick", { start: target.value.trim(), title });
      if (res.path) { target.value = res.path; target.dispatchEvent(new Event("input")); target.dispatchEvent(new Event("change")); }
      else if (res.timeout) toast("The folder dialog did not answer - use \"Folders...\" instead.", "info", 8000);
    } catch (err) {
      toast(err.message, "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ------------------------------------------------------------- scan + overview
  function renderScanTarget() {
    paintFolder();
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
    if (state.scan && state.resultDat && !state.scan.dat_names.includes(state.resultDat)) state.resultDat = "";
    browseDirty = true;
    Previews.onScan();                // a preview of the same system stays (marked out of date), others are dropped
    renderScanOverview();
    if (inSystem() && state.tab === "browse") renderBrowse();
    renderLibraryIntro();
    renderLibraryGate();
    renderOrganiseIntro();
    renderConvertIntro();
    renderM3UIntro();
    renderKickIntro();
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
        ? el("button", { class: "btn btn-primary", text: "Scan folder", "data-needs-idle": "", on: { click: startScan } })
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
    if (s.unsupported) tabs.push(["unsupported", "Unsupported", s.unsupported]);
    if (s.errors) tabs.push(["errors", "Errors", s.errors]);
    if (!tabs.some((t) => t[0] === activeTab)) activeTab = tabs[0][0];
    if (activeTab !== "games") { state.gamesHave = ""; state.gamesRated = ""; }
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

  /** Sega Dreamcast: which engine reads the CHDs, and the Verify fully button. */
  function renderDcBar(s) {
    const dc = s.chd_files !== undefined;
    $("dc-bar").classList.toggle("hidden", !dc);
    $("verify-empty").classList.toggle("hidden", dc);
    if (!dc) return;
    $("dc-engine-line").textContent = s.engine === "chdman"
      ? `CHDs were read with chdman (${s.chdman}) - every track was checked, so they are verified.`
      : (s.engine === "mixed" ? `CHDs were read with the built-in reader; chdman (${s.chdman}) decoded the ones the reader cannot. `
        : "CHDs were read with the built-in reader (no chdman needed). ")
        + "Data tracks are hashed, audio is checked by length until you press Verify fully." + (s.identified ? ` ${fmt(s.identified)} CHD(s) are still only identified.` : "")
        + (s.needs_chdman ? ` ${fmt(s.needs_chdman)} CHD(s) use a compression this version does not know (made by a newer chdman?) - they are left in place; install that chdman to identify them.` : "");
    const speed = s.engine_text || state.dcSpeed || "";
    $("dc-speed-line").textContent = speed ? `Last decode: ${speed}` : "";
    $("dc-speed-line").classList.toggle("hidden", !speed);
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
        el("li", { text: "The built-in reader decodes with every CPU core at once (and the system\u2019s libFLAC for audio): typically 100-250 MB/s on a Steam Deck, so a 1 GB disc takes seconds and an 8 GB PlayStation 2 DVD well under a minute or two. Nothing is written to disk." }),
        el("li", { text: "The built-in reader reads every CHD chdman 0.289 can (all versions and compressions, parent files next to their child). chdman is only used for a CHD in a format newer than that, or when you chose \"always chdman\": it extracts the disc to scratch space (in RAM when that fits with a safe reserve, otherwise in the app\u2019s cache folder - never in your game folder) and deletes it again. You can cancel at any time." })));
    if (!(await confirmDialog({ title: "Verify fully", body, okText: "Verify" }))) return;
    Jobs.start("/api/dc/verify", {});
  }

  Jobs.handlers.verify = async (job) => {
    if ((job.status === "done" || job.status === "cancelled") && job.result) {
      const r = job.result;
      const failed = Array.isArray(r.failed) ? r.failed : [];
      if (r.engine_text) state.dcSpeed = r.engine_text;
      toast(`Verified ${fmt(r.verified || 0)} CHD(s)${failed.length ? `, ${fmt(failed.length)} do not match Redump` : ""}${r.engine_text ? ` - ${r.engine_text}` : ""}`, failed.length ? "error" : "ok", 12000);
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

  const SORTABLE_KINDS = new Set(["games", "matched", "unmatched", "missing"]);
  const kindSort = (kind) => (kind === "games" ? state.gamesSort : (state.kindSort[kind] || ""));

  function resultTableOptions(kind, tagBar, reload) {
    const dat = DAT_FILTER_TABS.has(kind) ? state.resultDat : "";
    const fetchKind = async ({ offset, limit, q, checksums }) => {
      const have = kind === "games" ? state.gamesHave : "";
      const rated = kind === "games" && ratingsAvailable() ? state.gamesRated : "";
      const sort = kindSort(kind);
      const tagParams = TAG_TABS.has(kind) ? state.tagFilter : {};
      const data = await get(`/api/scan/results?${qs({ kind, offset, limit, q, dat, have, rated, sort, checksums: checksums ? "1" : "", ...tagParams })}`);
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
                if (i.aside) return el("span", { class: "badge skip", text: "set aside", title: "In one of the app's own folders (_excluded/, _superseded/ ...) on purpose" });
                return el("span", { class: "badge move", text: i.placed_ok ? "rename" : "move" });
              },
            },
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
            { label: "Size", cls: "num", sortKey: "size", sortFirst: "desc", render: (i) => fmtBytes(i.size) },
            { label: "CRC32", cls: "mono", render: (i) => i.crc },
          ],
        };
      case "unmatched":
        return {
          fetch: fetchKind, placeholder: "Search unmatched files...", emptyText: "Every file matched a DAT.",
          columns: [
            { label: "Local file", cls: "wrap", sortKey: "name", sortFirst: "asc", always: true, render: (i) => fileCell(i.file) },
            ...(isGameFolder(currentPlatform()) ? [{ label: "Why", cls: "wrap", render: (i) => el("span", { class: "muted", text: i.reason || "" }) }] : []),
            { label: "Size", cls: "num", sortKey: "size", sortFirst: "desc", render: (i) => fmtBytes(i.size) },
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
      organise: { btn: "plan-btn", apply: "apply-btn", output: "organise-output", noun: "files", count: (d) => (d.all !== undefined ? d.all : sumCounts(d.counts)), run: () => showOrganisePlan(), table: () => organiseTable, reset: () => resetOrganisePreview() },
      convert: { btn: "convert-plan-btn", apply: "convert-apply-btn", output: "convert-output", noun: "files", count: (d) => sumCounts(d.counts), run: () => showConvertPlan(), table: () => convertTable, reset: () => resetConvertPreview() },
      m3u: { btn: "m3u-plan-btn", apply: "m3u-apply-btn", output: "m3u-output", noun: "playlists", count: (d) => sumCounts(d.counts), run: () => showM3UPlan(), table: () => m3uTable, reset: () => resetM3UPreview() },
      kick: { btn: "kick-plan-btn", apply: "kick-apply-btn", output: "kick-output", noun: "files", count: (d) => sumCounts(d.counts), run: () => showKickPlan(), table: () => kickTable, reset: () => resetKickPreview() },
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
    labels: $("lib-labels").checked, savedisk: $("lib-savedisk").checked,
  });

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
    $("organise-latest-only").checked = !!(p && p.latest_only);
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
      items: info.regions || [], label: "Region priority, best region first", noun: "region", prioCount: 4,
      prioLabel: "Prioritised (tried first)", restLabel: "Everything else (alphabetical unless you move it up)",
      onCommit: (order) => commitRegionOrder(name, order),
    });
    regionSave.ui = ui;
    regionSave.uiPlatform = name;
    const section = el("div", { class: "rules-group", id: "rules-regions" },
      el("div", { class: "rules-head", text: "Region priority" }),
      el("div", { class: "sub note", text: (catalogOf(info).find((e) => e.id === "region_priority") || {}).description || "Best region first." }),
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
    $("library-rules-summary").textContent = bits.join(" \u00B7 ");
    $("library-rules-note").textContent = JSON.stringify(prof) === JSON.stringify(info.defaults) ? "Default rules" : "Custom rules";
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
    Previews.staleAll("Rules changed since this preview", ["lib", "organise", "convert"]);
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
  }

  function renderLibraryIntro() {
    if (!Previews.has("lib")) resetLibraryPreview();
    refreshUndo();
    refreshLibraryStats();
  }

  function renderLibraryCards(plan) {
    const r = plan.reasons || {}, pl = plan.playlists || {}, vanish = plan.vanish || {}, cats = plan.categories || {};
    // the cards count what this build moves; files a previous build already set aside are named next to it
    const aside = (label, moving, all) => ((all || 0) > (moving || 0) ? `${label} now (${fmt(all - (moving || 0))} already set aside)` : label);
    $("lib-cards").replaceChildren(
      card(fmt(r.kept), "Kept", "ok"),
      card(fmt((r.renamed || 0) + (r.moved || 0)), `Renamed / moved (${fmt(r.renamed || 0)} / ${fmt(r.moved || 0)})`, "info"),
      card(fmt(r.excluded), aside("Excluded", r.excluded, cats.excluded), r.excluded ? "warn" : ""),
      card(fmt(r.superseded), aside("Superseded", r.superseded, cats.superseded), r.superseded ? "warn" : ""),
      ...(isGameFolder(currentPlatform()) ? [] : [card(fmt(r.incomplete), aside("Incomplete", r.incomplete, cats.incomplete), r.incomplete ? "warn" : "")]),
      card(fmt(r.duplicates), aside("Duplicates", r.duplicates, cats.duplicate), r.duplicates ? "warn" : ""),
      ...(r.unmatched ? [card(fmt(r.unmatched), `Unmatched → ${UNMATCHED}/`, "warn")] : []),
      ...(pl.write || pl.remove || pl.ok ? [card(fmt(pl.write), "Playlists to write", pl.write ? "info" : ""),
        card(fmt(pl.remove), "Playlists to remove", pl.remove ? "warn" : "")] : []),
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
      : `Incomplete sets (${fmt(total)}) - disks missing, set aside as _incomplete`;
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
    else if (cat === "kept") lines.push(i.reason && i.reason.includes("always keep") ? "You chose to always keep this game." : "Kept: no rule sets this file aside, and it is the best version you have.");
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
            ...libOptions(), reason: state.libReason, status: state.libStatus, why: state.libWhy, offset, limit, q, refresh,
            sort: state.libSort, checksums: state.showChecksums ? true : undefined,
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
          // every row of a category, moving or already in place (an already built library has few moves but many rows)
          const rc = data.categories || { kept: r.kept, excluded: r.excluded, superseded: r.superseded, incomplete: r.incomplete,
            duplicate: r.duplicates, unmatched: r.unmatched, playlist: (pl.write || 0) + (pl.ok || 0) + (pl.remove || 0) + (pl.conflict || 0) };
          filterChips($("lib-reason-filters"), Object.fromEntries(Object.entries(rc).filter(([, n]) => n)), state.libReason, REASON_ORDER,
            (key) => { state.libReason = key; if (key !== "excluded") state.libWhy = ""; reload(); }, (k) => REASON_LABEL[k] || k, "All reasons");
          filterChips($("lib-status-filters"), data.counts || {}, state.libStatus, ["move", "rename", "delete", "conflict", "skip", "ok"],
            (key) => { state.libStatus = key; reload(); }, (k) => k, "All statuses");
          $("lib-apply-btn").dataset.blocked = data.empty ? "1" : "0";
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
          { label: "Year", sortKey: "year", sortFirst: "asc", cls: "num", when: (d) => !!d.has_year, render: (i) => (i.year ? String(i.year) : "") },
          { label: "Size", sortKey: "size", sortFirst: "desc", cls: "num", render: (i) => (i.size ? fmtBytes(i.size) : "") },
          { label: "Why", cls: "wrap why-col", render: libraryNote },
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
        line(r.unmatched, `unmatched file(s) → ${UNMATCHED}/ (they match nothing in the DATs; subfolders are kept)`),
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
    Jobs.start("/api/library/apply", { ...libOptions(), plan_id: plan.plan_id });   // the server refuses a plan of other rules
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
    if (wasOpen && state.scan) { vanishTable = null; if (libTable) libTable.offset = 0; await showLibraryPlan(); }
  };

  // --------------------------------------------------------- 3b. Organise (advanced)
  let organiseTable = null;
  let organisePlan = null; // last plan response (counts, by_dest, ...)
  const ACTIONABLE = ["move", "rename", "delete"];
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

  function resetOrganisePreview() {
    $("organise-empty").textContent = state.scan ? "Press \"Preview changes\" to see what would move." : "Run a scan first.";
    $("organise-empty").classList.remove("hidden");
    $("organise-output").classList.add("hidden");
    organiseTable = null;
    organisePlan = null;
  }

  function renderOrganiseIntro() {
    if (!Previews.has("organise")) resetOrganisePreview();
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
    Previews.busy("organise");
    $("organise-empty").classList.add("hidden");
    $("organise-output").classList.remove("hidden");
    if (!organiseTable) {
      organiseTable = new PagedTable($("organise-table"), {
        placeholder: "Search file names or folders...",
        emptyText: "Nothing in this category.",
        fetch: Previews.wrap("organise", async ({ offset, limit, q, refresh }) => {
          const data = await post("/api/organise/plan", {
            status: state.organiseFilter, dest: state.organiseDest, offset, limit, q,
            latest_only: latestOnly(), refresh,
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
        }),
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
      plan = await post("/api/organise/plan", { limit: 1, latest_only: latestOnly() });
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
    Jobs.start("/api/organise/apply", { latest_only: latestOnly() });
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
    if (wasOpen && state.scan) { if (organiseTable) organiseTable.offset = 0; await showOrganisePlan(); }
  };

  // ----------------------------------------------------------- 4. Convert
  let convertTable = null;
  const VIA_TEXT = { headerless: "remove 512-byte copier header", byteswapped: "byte-swap to big-endian .z64", chdman: "written as a CHD, then checked against Redump by the built-in reader", builtin: "written as a CHD, then checked against Redump by the built-in reader" };
  const MODE_TEXT = { createcd: "written as a CD image CHD, then checked against Redump by the built-in reader", createdvd: "written as a DVD image CHD, then checked against Redump by the built-in reader" };

  const CHD_INTRO = "Turns an unpacked Redump set (a .gdi or .cue with one .bin / .raw file per track, or a single .iso) into a CHD (a CD image, or a DVD image for a PlayStation 2 .iso) - the app writes it itself, chdman is not needed. "
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
    box.classList.remove("missing");
    const notes = (chdmanInfo.notes || []).join(" ");
    $("chdman-line").textContent = (found ? `chdman found: ${chdmanInfo.label}${chdmanInfo.bundled ? " - shipped with the app" : ""} (optional: the app reads and writes CHDs by itself).`
      : "chdman not found - it is not needed: the app reads and writes CHDs by itself.")
      + (notes ? ` (${notes})` : "")
      + (chdmanInfo.flac ? (chdmanInfo.flac.native ? " Audio decoding: native libFLAC." : " Audio decoding: built-in Python FLAC (slow) - libFLAC was not found.") : "")
      + (chdmanInfo.workers ? ` Decode processes: ${chdmanInfo.workers}.` : "");
    const steps = $("chdman-steps");
    const wantsChdman = chdmanInfo.writer === "chdman" || chdmanInfo.engine === "chdman";
    steps.classList.toggle("hidden", found || !wantsChdman);       // how to install it: only when it was asked for
    steps.replaceChildren(...(found || !wantsChdman ? [] : (chdmanInfo.steps || []).map((t) => el("li", { text: t }))));
    if (document.activeElement !== $("chdman-path")) $("chdman-path").value = chdmanInfo.override || "";
    $("chdman-engine").value = chdmanInfo.engine || "auto";
    $("chd-writer").value = chdmanInfo.writer || "auto";
    const zstdOption = $("chd-preset").querySelector('option[value="zstd"]');
    zstdOption.disabled = !chdmanInfo.zstd_writer || chdmanInfo.writer === "chdman";
    $("chd-preset").value = zstdOption.disabled ? "default" : (chdmanInfo.preset || "default");
    $("chd-preset-note").classList.toggle("hidden", $("chd-preset").value !== "zstd");
  }

  async function saveChdman(body) {
    try {
      chdmanInfo = await post("/api/chdman", body);
      renderChdman();
      if (chdmanInfo.warning) toast(chdmanInfo.warning, "error", 8000);
      else toast("Saved", "ok");
      if (convertTable) { convertTable.offset = 0; convertTable.load(); }
    } catch (err) { toast(err.message, "error", 8000); }
  }

  function renderConvertIntro() {
    const dc = isGameFolder(currentPlatform());
    $("convert-title").textContent = dc ? "Convert raw Redump sets to CHD (optional)" : "Convert to No-Intro format (optional)";
    if (dc) $("convert-intro").textContent = CHD_INTRO;
    if (dc) loadChdman(); else $("chdman-box").classList.add("hidden");
    if (!Previews.has("convert")) resetConvertPreview();
  }

  function resetConvertPreview() {
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
    Previews.busy("convert");
    $("convert-empty").classList.add("hidden");
    $("convert-output").classList.remove("hidden");
    if (!convertTable) {
      convertTable = new PagedTable($("convert-table"), {
        placeholder: "Search file names...",
        emptyText: isGameFolder(currentPlatform()) ? "No raw Redump sets found in this folder." : "Nothing to convert - every matched file is already in No-Intro format.",
        fetch: Previews.wrap("convert", async ({ offset, limit, q, refresh }) => {
          const data = await post("/api/convert/plan", { status: state.convertFilter, offset, limit, q, latest_only: latestOnly(), refresh });
          renderConvertCards(data);
          if (data.chdman) { chdmanInfo = { ...(chdmanInfo || {}), ...data.chdman }; renderChdman(); }
          filterChips($("convert-filters"), data.counts || {}, state.convertFilter, ["convert", "conflict", "skip"],
            (key) => { state.convertFilter = key; convertTable.offset = 0; convertTable.load(); });
          $("convert-apply-btn").dataset.blocked = (data.counts || {}).convert ? "0" : "1";
          Jobs.setRunning(Jobs.running);
          return data;
        }),
        columns: [
          { label: "Status", render: (i) => badge(i.status) },
          {
            label: "Change (relative to the console folder)", cls: "wrap", render: (i) => el("div", {},
              el("div", { class: "rename-from mono-path", text: i.from }),
              el("div", {}, el("span", { class: "rename-arrow", text: "→ " }), el("span", { class: "rename-to mono-path", text: i.to })),
              i.status === "convert" ? el("div", { class: "sub mono-path", text: `original kept as ${i.original_to}` }) : null),
          },
          { label: "How", cls: "wrap", render: (i) => el("span", { class: "muted", text: (MODE_TEXT[i.mode] || VIA_TEXT[i.via] || i.via || "-") + (i.note ? ` (${i.note})` : "") }) },
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
    const body = isGameFolder(currentPlatform()) ? el("div", {},
      el("p", { text: `Convert ${fmt(n)} raw set${n === 1 ? "" : "s"} inside ${state.scan.root} to CHD${data.chdman && data.chdman.writer === "chdman" && data.chdman.found ? ` with chdman (${data.chdman.label})` : ""}${data.chdman && data.chdman.preset === "zstd" && !(data.chdman.writer === "chdman" && data.chdman.found) ? " with Zstandard compression (needs an emulator from 2024 or later)" : ""}?` }),
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
      toast(`Converted ${fmt(converted)} file(s)${failed.length ? `, ${fmt(failed.length)} failed` : ""}${r.cancelled ? " (cancelled)" : ""}${r.verify_text ? ` - ${r.verify_text}` : ""}${r.generated_gdi ? `; ${fmt(r.generated_gdi)} .gdi generated from the .cue` : ""}`,
        failed.length || r.error ? "error" : "ok", 12000);
      showFailures("convert-failures", "Could not convert:", failed,
        (f) => `${f.src}${f.dst ? ` → ${f.dst}` : ""}: ${f.error || "failed"}`,
        [r.error ? `Stopped: ${r.error}` : "",
          r.rescan_error ? `The folder could not be re-scanned (${r.rescan_error}) - scan it again.` : ""]);
    }
    const wasOpen = !$("convert-output").classList.contains("hidden");
    await refreshScan();
    if (wasOpen && state.scan) { if (convertTable) convertTable.offset = 0; await showConvertPlan(); }
  };

  // -------------------------------------------------------------- 5. M3U
  let m3uTable = null;
  const m3uOptions = () => ({ savedisk: $("m3u-savedisk").checked, labels: $("m3u-labels").checked });

  function renderM3UIntro() {
    if (!Previews.has("m3u")) resetM3UPreview();
  }

  function resetM3UPreview() {
    $("m3u-empty").textContent = state.scan ? "Press \"Preview playlists\" to find multi-disk sets." : "Run a scan first.";
    $("m3u-empty").classList.remove("hidden");
    $("m3u-output").classList.add("hidden");
    m3uTable = null;
  }

  async function showM3UPlan() {
    Previews.busy("m3u");
    $("m3u-empty").classList.add("hidden");
    $("m3u-output").classList.remove("hidden");
    if (!m3uTable) {
      m3uTable = new PagedTable($("m3u-table"), {
        pageSize: 25,
        placeholder: "Search playlists...",
        emptyText: "No multi-disk sets found among the matched files.",
        fetch: Previews.wrap("m3u", async ({ offset, limit, q, refresh }) => {
          const data = await post("/api/m3u/plan", { ...m3uOptions(), status: state.m3uFilter, offset, limit, q, refresh });
          const counts = data.counts || {};
          filterChips($("m3u-filters"), counts, state.m3uFilter, ["write", "stale", "ok", "incomplete", "conflict"],
            (key) => { state.m3uFilter = key; m3uTable.offset = 0; m3uTable.load(); });
          $("m3u-apply-btn").dataset.blocked = counts.write || counts.stale ? "0" : "1";
          Jobs.setRunning(Jobs.running);
          return data;
        }),
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
    if (m3uTable) m3uTable.offset = 0;
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
    if (!Previews.has("kick")) resetKickPreview();
    if (!kickDirsLoaded && hasKickstart(p)) loadKickDirs();
  }

  function resetKickPreview() {
    const p = currentPlatform();
    $("kick-empty").textContent = kickReady(p) ? "Pick a destination, then press \"Preview\"."
      : kickFolderMode(p) ? `Choose the ${p ? p.name : "system"} folder in step 1 first.` : "Run a scan first.";
    $("kick-empty").classList.remove("hidden");
    $("kick-output").classList.add("hidden");
    $("kick-source").textContent = "";
    kickTable = null;
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
    Previews.busy("kick");
    $("kick-empty").classList.add("hidden");
    $("kick-output").classList.remove("hidden");
    const platform = state.platform;
    kickTable = new PagedTable($("kick-table"), {
      placeholder: "Search Kickstarts...",
      emptyText: "Nothing in this category.",
      fetch: Previews.wrap("kick", async ({ offset, limit, q }) => {
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
      }),
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
    Previews.bindAll();
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
    for (const id of ["lib-labels", "lib-savedisk"]) {
      $(id).addEventListener("change", () => Previews.stale("lib", "Options changed since this preview"));
    }
    $("fb-up").addEventListener("click", () => FolderBrowser.parent && FolderBrowser.list(FolderBrowser.parent));
    $("fb-hidden").addEventListener("change", () => FolderBrowser.current && FolderBrowser.list(FolderBrowser.current));
    $("fb-choose").addEventListener("click", () => FolderBrowser.choose());
    $("scan-btn").addEventListener("click", startScan);
    bindFolderField();
    for (const tab of TABS) $(`tabbtn-${tab}`).addEventListener("click", () => openTab(tab));
    tabKeys($("sys-tabs"));
    tabKeys($("result-tabs"));
    window.addEventListener("hashchange", applyRoute);
    $("apply-btn").addEventListener("click", applyOrganise);
    $("undo-btn").addEventListener("click", undoLast);
    $("m3u-apply-btn").addEventListener("click", writeM3Us);
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
      Previews.staleAll("Rules changed since this preview", ["lib", "organise", "convert"]);
      scheduleTotals();
    });
    $("dc-verify-btn").addEventListener("click", verifyFully);
    $("chdman-save").addEventListener("click", () => saveChdman({ path: $("chdman-path").value.trim() }));
    $("chdman-refresh").addEventListener("click", () => loadChdman(true));
    $("chdman-engine").addEventListener("change", (e) => saveChdman({ engine: e.target.value }));
    $("chd-writer").addEventListener("change", (e) => saveChdman({ writer: e.target.value }));
    $("chd-preset").addEventListener("change", (e) => saveChdman({ preset: e.target.value }));
    $("convert-apply-btn").addEventListener("click", applyConvert);
    $("convert-undo-btn").addEventListener("click", undoLast);
    for (const id of ["m3u-labels", "m3u-savedisk"]) {
      $(id).addEventListener("change", () => Previews.stale("m3u", "Options changed since this preview"));
    }
    $("kick-dest").addEventListener("input", debounce(() => {
      kickTable = null;
      Previews.clear("kick");
      $("kick-output").classList.add("hidden");
      $("kick-empty").classList.remove("hidden");
      try { renderKickDirs(JSON.parse($("kick-dirs").dataset.dirs || "[]")); } catch (_) { /* ignore */ }
      updateActionState();
    }, 150));
    $("kick-dest").addEventListener("change", saveKickDest);
    $("kick-dest").addEventListener("keydown", (e) => { if (e.key === "Enter" && kickReady(currentPlatform())) showKickPlan(); });
    $("kick-browse-btn").addEventListener("click", () => FolderBrowser.open($("kick-dest"), "Choose the RetroArch system / BIOS folder"));
    $("kick-native-browse-btn").addEventListener("click", (e) => nativeBrowse(e.currentTarget, $("kick-dest"), "Choose the RetroArch system / BIOS folder"));
    $("kick-apply-btn").addEventListener("click", applyKick);
    $("quit-btn").addEventListener("click", quit);
    $("databases-btn").addEventListener("click", () => toggleDatabases());
    document.addEventListener("click", (e) => {
      for (const m of document.querySelectorAll(".col-menu[open]")) if (!m.contains(e.target)) m.open = false;
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") for (const m of document.querySelectorAll(".col-menu[open]")) m.open = false;
    });
    const dense = $("density-btn");
    const applyDensity = () => { document.body.classList.toggle("dense", Prefs.dense); dense.setAttribute("aria-pressed", Prefs.dense ? "true" : "false"); dense.textContent = Prefs.dense ? "Roomy" : "Compact"; };
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
    $("folder-native-btn").addEventListener("click", (e) => {
      const p = currentPlatform();
      if (p) nativeBrowse(e.currentTarget, input, `Choose the ${p.name} folder`);
    });
  }

  async function init() {
    bind();
    await loadStatus();
    const s = state.status || {};
    state.platform = (s.scan && s.scan.platform) || s.last_platform || s.default_platform || null;
    if (state.platform) Prefs.restore(state.platform);
    await loadPlatforms();
    setKickDest(currentPlatform());
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
