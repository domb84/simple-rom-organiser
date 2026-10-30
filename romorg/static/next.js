/* Simple ROM Organiser - the big-screen UI (v0.5). Vanilla JS, no external resources.
 *
 * One small kit (tiles, rows, switches, list pickers, a side panel, a progress view, the button strip) and one input layer: a
 * controller (the Gamepad API, or Steam Input as keys), a keyboard, a mouse or touch all drive the same screens. Screens are hash
 * routes; each returns its header, its body and what the buttons do there. The old UI stays at "/classic" for a while.
 */
(() => {
  "use strict";

  // ------------------------------------------------------------------ utils
  const TOKEN = document.querySelector('meta[name="romorg-token"]').content;
  const deckMeta = document.querySelector('meta[name="romorg-deck"]');
  const DECK = !!(deckMeta && deckMeta.content && !deckMeta.content.startsWith("__")) || /[?&]deck=1\b/.test(location.search);
  const $ = (id) => document.getElementById(id);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const body = document.body;
  if (DECK) body.classList.add("deck");

  /** Create an element. props: {class, text, on: {event: fn}, ...attrs}. */
  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "style") node.style.cssText = value;      // (the page's CSP refuses style attributes; the CSSOM is allowed)
      else if (key === "on") for (const [ev, fn] of Object.entries(value)) node.addEventListener(ev, fn);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  const ICONS = {
    play: '<path d="M6 4l13 8-13 8V4z"/>',
    archive: '<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10h14V9"/><path d="M10 13h4"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1"/>',
    folder: '<path d="M3 6h7l2 2h9v11H3z"/>',
    list: '<path d="M4 5h16M4 12h16M4 19h16"/>',
    toggle: '<rect x="3" y="7" width="18" height="10" rx="5"/><circle cx="16" cy="12" r="3"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
    cloud: '<path d="M7 18a4 4 0 0 1-.5-8A6 6 0 0 1 18 9a4 4 0 0 1 0 9z"/>',
    check: '<path d="M5 12l5 5 9-10"/>',
    tiles: '<rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="3" width="8" height="8" rx="1.5"/><rect x="3" y="13" width="8" height="8" rx="1.5"/><rect x="13" y="13" width="8" height="8" rx="1.5"/>',
    db: '<ellipse cx="12" cy="5.5" rx="7.5" ry="2.5"/><path d="M4.5 5.5v13c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5v-13M4.5 12c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5"/>',
    chip: '<rect x="6" y="6" width="12" height="12" rx="1.5"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
    gamepad: '<path d="M7 8h10a5 5 0 0 1 4.6 7l-.6 1.4a2.5 2.5 0 0 1-4 .8L15 15H9l-2 2.2a2.5 2.5 0 0 1-4-.8L2.4 15A5 5 0 0 1 7 8z"/><path d="M8 11v3M6.5 12.5h3"/>',
    save: '<path d="M5 3h11l3 3v15H5z"/><path d="M8 3v5h7V3M8 21v-7h8v7"/>',
    disc: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/>',
    home: '<path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/>',
    drive: '<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M7 14h2"/>',
    sd: '<path d="M7 3h8l4 4v14H7z"/><path d="M10 7v3M13 7v3"/>',
    usb: '<rect x="7" y="9" width="10" height="12" rx="1"/><path d="M9 9V3h6v6"/>',
    rescan: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
    open: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v6H4V6h6"/>',
    stop: '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    test: '<path d="M5 12l5 5 9-10"/>',
    power: '<path d="M12 3v9"/><path d="M6.3 6.3a8 8 0 1 0 11.4 0"/>',
  };
  const SVG = "http://www.w3.org/2000/svg";
  function icon(name, cls = "") {
    const s = document.createElementNS(SVG, "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("aria-hidden", "true");
    if (cls) s.setAttribute("class", cls);
    s.innerHTML = ICONS[name] || "";
    return s;
  }

  /** ``node.replaceChildren`` that skips the pieces a screen leaves out (null, undefined, false). */
  const put = (node, ...kids) => node.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
  const fmt = (n) => (n === null || n === undefined ? "-" : Number(n).toLocaleString());
  const pct = (a, b) => (b ? Math.round((100 * a) / b) : 0);
  const baseName = (p) => String(p || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop() || String(p || "");
  function ago(iso) {
    if (!iso) return "never";
    const t = new Date(iso).getTime();
    if (!t) return iso;
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 90) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} minutes ago`;
    if (s < 86400 * 1.5) return `${Math.round(s / 3600)} hours ago`;
    return new Date(t).toLocaleDateString();
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ------------------------------------------------------------------ API
  async function api(method, path, data) {
    const opts = { method, headers: {} };
    if (method === "POST") {
      opts.headers["Content-Type"] = "application/json";
      opts.headers["X-Romorg-Token"] = TOKEN;
      opts.body = JSON.stringify(data || {});
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (_) {
      throw new Error("Cannot reach the app - is it still running?");
    }
    let out = null;
    try { out = await res.json(); } catch (_) { /* empty body */ }
    if (!res.ok) {
      const err = new Error((out && out.error) || `${res.status} ${res.statusText}`);
      if (out && out.code) err.code = out.code;
      err.status = res.status;
      throw err;
    }
    return out;
  }
  const get = (path) => api("GET", path);
  const post = (path, data) => api("POST", path, data);
  const qs = (params) => new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();

  function toast(text, kind = "", ms = 5000) {
    const t = el("div", { class: `toast ${kind}`, text });
    $("toasts").append(t);
    setTimeout(() => t.remove(), ms);
  }

  /** The running job, followed until it ends. ``onProgress(job)`` on every poll. */
  async function followJob(onProgress, isAlive = () => true) {
    for (;;) {
      let job = null;
      try { job = await get("/api/job"); } catch (_) { /* the next poll tries again */ }
      if (job && onProgress && isAlive()) onProgress(job);
      if (job && job.status !== "running") return job;
      await sleep(400);
    }
  }

  // ------------------------------------------------------------------ what the app knows
  const S = { status: null, platforms: [], tilesCatalog: null };
  async function loadStatus() { S.status = await get("/api/status"); return S.status; }
  async function loadPlatforms() { S.platforms = await get("/api/platforms"); return S.platforms; }
  const bySlug = (slug) => S.platforms.find((p) => p.slug === slug);
  const isGameFolder = (p) => !!p && p.layout === "game_folder";
  const archiveBase = () => (S.status && S.status.archive && S.status.archive.dir) || "";
  const archiveFor = (p) => (p && S.status && S.status.archive && S.status.archive.overrides[p.name]) || archiveBase();
  const SOURCE = { tosec: "TOSEC", nointro: "No-Intro", redump: "Redump" };
  function archiveText(loc) {
    if (!loc) return "Not set up";
    if (loc.startsWith("s3://")) return `S3 · ${loc.slice(5)}`;
    return loc;
  }
  /** The options of a build of system ``p``: where it is built and where what it sets aside goes (the saved choices). */
  function buildOptions(p) {
    const e = (S.status && S.status.library_export) || {};
    const o = { labels: Prefs.get(`labels:${p.name}`, !isGameFolder(p)) };
    if (e.enabled && e.dest) Object.assign(o, { export_to: e.dest, export_mode: e.mode, export_sidecars: e.sidecars, export_sync: e.sync });
    else if (archiveFor(p)) o.aside_to = archiveFor(p);
    return o;
  }
  const classic = (hash) => `/classic${hash || ""}`;
  /** This browser's own choices (playlist labels, hidden folders...): never needed, so a refused storage is no error. */
  const Prefs = {
    get(k, d) { try { const v = localStorage.getItem(`romorg-next:${k}`); return v === null ? d : JSON.parse(v); } catch (_) { return d; } },
    set(k, v) { try { localStorage.setItem(`romorg-next:${k}`, JSON.stringify(v)); } catch (_) { /* private window */ } },
  };

  // ------------------------------------------------------------------ the kit
  /** A big button. {title, sub, icon, href, onClick, primary, short, small, check, disabled, id} */
  function tile(o) {
    const cls = ["tile", o.primary && "primary", o.short && "short", o.small && "small", o.cls].filter(Boolean).join(" ");
    const kids = [
      o.icon ? icon(o.icon, "tile-icon") : null,
      el("span", { class: "tile-text" }, el("span", { class: "tile-title", text: o.title }),
        o.sub !== undefined && o.sub !== null ? el("span", { class: "tile-sub", text: o.sub }) : null, o.extra || null),
      o.check ? el("span", { class: "tile-check", "aria-label": "Chosen", role: "img" }, icon("check")) : null,
    ];
    const attrs = { class: cls, id: o.id, "data-key": o.key, "aria-label": o.label };
    if (o.href && !o.disabled) return el("a", { ...attrs, href: o.href }, kids);
    return el("button", { ...attrs, type: "button", disabled: o.disabled, on: o.onClick ? { click: o.onClick } : undefined }, kids);
  }

  /** A number tile. {label, number, of, unit, bar (0-100), warn, sub, href, key} */
  function statTile(o) {
    if (o.loading) {                     // (not worked out yet: a shimmering bone where the number goes, a spinner, what it waits for)
      return el("div", { class: "tile stat loading", "data-key": o.key }, el("span", { class: "mini", "aria-hidden": "true" }),
        el("span", { class: "tile-label", text: o.label }), el("span", { class: "bone" }), el("span", { class: "tile-sub", text: o.sub || "" }));
    }
    const kids = [
      o.label ? el("span", { class: "tile-label", text: o.label }) : null,
      el("span", { class: `tile-number${o.warn ? " warn" : ""}${o.smallNumber ? " small" : ""}` }, el("b", { text: o.number }),
        o.of !== undefined && o.of !== null ? el("span", { text: `of ${o.of}` }) : null, o.unit ? el("span", { text: o.unit }) : null),
      o.bar !== undefined && o.bar !== null ? el("span", { class: "bar" }, el("span", { style: `width:${Math.max(0, Math.min(100, o.bar))}%` }))
        : o.sub ? el("span", { class: "tile-sub", text: o.sub }) : el("span", {}),
    ];
    if (o.href) return el("a", { class: "tile", href: o.href, "data-key": o.key }, kids);
    return el("div", { class: "tile stat", "data-key": o.key }, kids);
  }

  /** One line: {name, sub, value, valueCls, icon, dot, href, onClick, sw (on/off), arrow, id, disabled} */
  function row(o) {
    const kids = [
      o.dot ? el("span", { class: "dot", style: `background:${o.dot}` }) : null,
      o.icon ? icon(o.icon, "row-icon") : null,
      el("span", { class: "row-name" }, o.name, o.sub ? el("small", { text: o.sub }) : null),
      o.tag ? el("span", { class: `tag ${o.tag[1] || ""}`, text: o.tag[0] }) : null,
      o.sw !== undefined ? el("span", { class: `switch${o.sw ? " on" : ""}`, "aria-hidden": "true" }) : null,
      o.value !== undefined && o.value !== null ? el("span", { class: `row-value ${o.valueCls || ""}`, text: o.value + (o.arrow ? " ›" : "") })
        : o.arrow ? el("span", { class: "row-value", text: "›" }) : null,
    ];
    const attrs = { class: `row${o.cls ? ` ${o.cls}` : ""}`, id: o.id, "data-key": o.key };
    if (o.info) return el("div", { ...attrs, class: `${attrs.class} info` }, kids);           // a line of facts, not a control
    if (o.static) return el("div", { ...attrs, class: `${attrs.class} static` }, kids);
    if (o.href) return el("a", { ...attrs, href: o.href }, kids);
    return el("button", {
      ...attrs, type: "button", disabled: o.disabled, role: o.sw !== undefined ? "switch" : undefined,
      "aria-checked": o.sw !== undefined ? String(!!o.sw) : undefined, on: o.onClick ? { click: o.onClick } : undefined,
    }, kids);
  }

  /** A line with a text field: {label, value, placeholder, type, id} */
  function field(o) {
    const input = el("input", { type: o.type || "text", id: o.id, value: o.value || "", placeholder: o.placeholder || "",
      spellcheck: "false", autocomplete: o.type === "password" ? "new-password" : "off", "aria-label": o.label });
    return el("label", { class: "row field" }, el("span", { class: "row-name", text: o.label }), input);
  }

  function message(text, kind = "") { return el("div", { class: `message ${kind}`, text }); }

  /** A progress view: {node, update(job)} */
  function progressView(title) {
    const fill = el("span", { style: "width:0%" });
    const count = el("span", { text: "" }), share = el("span", { text: "" });
    const now = el("div", { class: "progress-now" });
    const head = el("h2", { class: "big-title", text: title });
    const node = el("div", { class: "progress" }, head, el("div", { class: "rows" }, el("span", { class: "bar big" }, fill),
      el("div", { class: "progress-line" }, count, share)), now);
    return {
      node,
      update(job) {
        const p = job.progress || {};
        const total = p.total || 0, done = p.done || 0;
        fill.style.width = total ? `${Math.min(100, (100 * done) / total)}%` : "4%";
        count.textContent = total ? `${fmt(done)} of ${fmt(total)}` : "";
        share.textContent = total ? `${pct(done, total)}%` : "";
        put(now, p.message ? el("span", { text: p.message }) : "");
      },
      title(text) { head.textContent = text; },
    };
  }


  // ------------------------------------------------------------------ "working on it": what, and for how long
  const fmtSecs = (n) => (n < 60 ? `${Math.round(n)} s` : `${Math.floor(n / 60)} min ${String(Math.round(n % 60)).padStart(2, "0")} s`);
  /** The short line of a tile while the totals are worked out: the step and its seconds. */
  function workText(w) {
    if (!w) return "Working it out";
    if (w.step === "queued") return "Waiting for its turn";
    return `${w.step === "target" ? "Whole database" : "Your files"} · ${fmtSecs(w.seconds || 0)}${w.expect ? ` of ~${fmtSecs(w.expect)}` : ""}`;
  }
  /** Shimmering rows / tiles where a list or numbers will be (the header chip says what is being worked out). */
  const skeletonRows = (n = 6) => el("div", { class: "rows" }, Array.from({ length: n }, () => el("span", { class: "bone row-bone" })));
  const skeletonTiles = (n = 3) => el("div", { class: "grid compact", style: `--cols:${n}` }, Array.from({ length: n }, () => el("div", { class: "tile stat loading" },
    el("span", { class: "mini", "aria-hidden": "true" }), el("span", { class: "bone line" }), el("span", { class: "bone" }), el("span", { class: "bone line" }))));

  // ------------------------------------------------------------------ work going on, wherever you are (the line on top, the header chip)
  const JOB_TEXT = { scan: "Scanning", library: "Building the library", whdload: "Downloading WHDLoad installs", whdbuild: "Building WHDLoad games",
    whdcheck: "Checking WHDLoad installs", retroarch: "RetroArch", restore: "Restoring from the archive", convert: "Converting", m3u: "Writing playlists" };
  /** The task the app runs (one at a time, on the server: leaving its screen never stops it), the database updates, and what the
   *  screen shown waits for. Polled; when a task ends while you are elsewhere, a toast says how it went. */
  const Activity = {
    local: new Map(), job: null, updates: null, watching: null, lastJob: null, tick: 0, told: new Set(),
    set(key, text) { this.local.set(key, text); this.draw(); },
    clear(key) { if (this.local.delete(key)) this.draw(); },
    clearLocal() { if (this.local.size) { this.local.clear(); this.draw(); } },
    items() {
      const out = [];
      const j = this.job;
      if (j && j.status === "running") {
        const pr = j.progress || {};
        out.push({ text: `${JOB_TEXT[j.kind] || "Working"}${j.platform ? ` · ${j.platform}` : ""}`, detail: pr.message || "",
          pct: pr.total ? Math.round((100 * pr.done) / pr.total) : null, job: j });
      }
      if (this.updates && this.updates.running) out.push({ text: "Updating the databases", detail: (this.updates.progress || {}).message || "" });
      for (const t of this.local.values()) out.push({ text: t, detail: "" });
      return out;
    },
    draw() {
      const items = this.items();
      $("topline").classList.toggle("hidden", !items.length);
      $("head-activity").classList.toggle("hidden", !items.length);
      if (items.length) {
        const first = items[0];
        $("head-activity-text").textContent = `${first.text}${first.pct !== null && first.pct !== undefined ? ` · ${first.pct}%` : ""}${items.length > 1 ? ` +${items.length - 1}` : ""}`;
      }
    },
    async poll() {
      let job = null;
      try { job = await get("/api/job"); } catch (_) { return; }
      const prev = this.lastJob;
      if (prev && prev.status === "running" && job && job.id === prev.id && job.status !== "running") this.finished(job);
      this.lastJob = job;
      this.job = job;
      if (this.tick++ % 3 === 0) { try { this.updates = await get("/api/updates"); } catch (_) { /* next time */ } }
      this.draw();
    },
    finished(job) {
      if (this.told.has(job.id)) return;                        // (the poll and the screen that started it may both see the end)
      this.told.add(job.id);
      const r = job.result || {};
      if (job.kind === "library" && job.status === "done" && r.whdload_next && this.watching !== job.id) {
        post("/api/whdload/build", { platform: r.whdload_next }).catch(() => {});   // (the rule "Build WHDLoad games": next, also when nobody watches)
      }
      if (this.watching === job.id) { return; }                 // (its own screen shows how it went)
      const what = JOB_TEXT[job.kind] || "The task";
      const n = (v) => (Array.isArray(v) ? v.length : v || 0);
      const failed = n(r.failed);
      if (job.status === "error") { toast(`${what} stopped: ${job.error || "an error"}`, "error", 10000); return; }
      if (job.status === "cancelled") { toast(`${what}: stopped`); return; }
      let text = `${what}: done${job.platform ? ` (${job.platform})` : ""}`;
      if (job.kind === "scan") text = `Scan finished${job.platform ? `: ${job.platform}` : ""}`;
      if (job.kind === "library") text = `Library built${job.platform ? ` (${job.platform})` : ""}: ${fmt(n(r.moved) || n(r.created))} files`;
      if (r.action === "whdload") text = `WHDLoad installs: ${fmt(n(r.downloaded))} downloaded`;
      if (r.action === "restore") text = `Restored ${fmt(r.restored || 0)} files from the archive`;
      toast(`${text}${failed ? `, ${fmt(failed)} not done` : ""}`, failed ? "error" : "", 8000);
    },
  };
  setInterval(() => Activity.poll(), 1500);
  Activity.poll();

  /** What is going on, as a list (the header chip, or Menu): and the running task can be stopped when it allows it. */
  async function showActivity() {
    const items = Activity.items();
    const rows = items.map((it) => row({ name: it.text, sub: it.detail || null, value: it.pct !== null && it.pct !== undefined ? `${it.pct}%` : "", info: true }));
    const j = Activity.job;
    const stop = j && j.status === "running" && j.cancellable ? el("button", { class: "btn danger", type: "button", on: { click: async () => {
      if (!await confirmDialog("Stop this task?", "Stop it", "Let it run")) return;
      try { await post("/api/job/cancel"); toast("Stopping…"); } catch (err) { toast(err.message, "error"); }
    } } }, "Stop the task") : null;
    await sheet("What's running", [el("div", { class: "rows" }, rows.length ? rows : [row({ name: "Nothing is running", static: true })]), stop], [["A", "Select"], ["B", "Close"]]);
  }

  // ------------------------------------------------------------------ overlays (list picker, confirm, menu)
  const overlays = [];
  function openOverlay(node, onCancel, hints) {
    const o = { node, onCancel, hints, before: document.activeElement };
    overlays.push(o);
    $("overlays").append(node);
    node.addEventListener("click", (e) => { if (e.target === node) closeOverlay(o, true); });
    setTimeout(() => focusEl(entry(node)));
    renderStrip();
    return o;
  }
  function closeOverlay(o, cancelled) {
    const i = overlays.indexOf(o);
    if (i < 0) return;
    overlays.splice(i, 1);
    o.node.remove();
    if (cancelled && o.onCancel) o.onCancel();
    if (o.before && o.before.isConnected) focusEl(o.before);
    renderStrip();
  }
  const topOverlay = () => overlays[overlays.length - 1] || null;

  /** A full-height list of choices from the right: resolves to the chosen value, or null (B / Esc / outside). */
  function picker(title, options) {
    return new Promise((resolve) => {
      let o = null;
      const pick = (v) => { closeOverlay(o, false); resolve(v); };
      const rows = options.map((opt) => el("button", { class: "row", type: "button", "data-current": opt.current ? "1" : null,
        on: { click: () => pick(opt.value) } },
        el("span", { class: "row-name" }, opt.label, opt.sub ? el("small", { text: opt.sub }) : null),
        opt.current ? el("span", { class: "row-current" }, icon("check"), "Current") : null));
      const sheet = el("section", { class: "sheet", role: "dialog", "aria-label": title }, el("h2", { text: title }), el("div", { class: "rows" }, rows));
      o = openOverlay(el("div", { class: "overlay side" }, sheet), () => resolve(null), [["A", "Choose"], ["B", "Cancel"]]);
      setTimeout(() => focusEl(rows.find((r) => r.dataset.current) || rows[0]));
    });
  }

  /** A question with two big buttons: resolves true / false. */
  /** A question with two big buttons: resolves true / false. The focus starts on the safe one ("no"). */
  function confirmDialog(question, yes = "Yes", no = "Cancel", detail = "") {
    return new Promise((resolve) => {
      let o = null;
      const done = (v) => { closeOverlay(o, false); resolve(v); };
      const noBtn = el("button", { class: "btn", type: "button", text: no, id: "confirm-no", on: { click: () => done(false) } });
      const box = el("section", { class: "dialog", role: "dialog", "aria-label": question }, el("h2", { text: question }),
        detail ? el("p", { text: detail }) : null,
        el("div", { class: "btns" }, noBtn, el("button", { class: "btn danger", type: "button", text: yes, id: "confirm-yes", on: { click: () => done(true) } })));
      o = openOverlay(el("div", { class: "overlay center" }, box), () => resolve(false), [["A", "Choose"], ["B", "Cancel"]]);
      setTimeout(() => focusEl(noBtn));
    });
  }

  async function appMenu() {
    if (topOverlay()) return;
    const busy = Activity.items();
    const v = await picker("Menu", [
      ...(busy.length ? [{ label: "What's running", sub: busy.map((i) => i.text).join(", "), value: "activity" }] : []),
      { label: "Classic interface", sub: "The old pages, for what is not here yet", value: "classic" },
      { label: "Quit", sub: "Stop the app", value: "quit" },
    ]);
    if (v === "activity") showActivity();
    if (v === "classic") location.href = classic();
    if (v === "quit" && await confirmDialog("Quit the app?", "Quit", "Cancel")) {
      try { await post("/api/quit"); } catch (_) { /* it may stop before it answers */ }
      Screen.show({ title: "ROM Organiser", body: el("div", { class: "screen" }, el("h2", { class: "big-title", text: "The app has stopped" }),
        el("p", { class: "lead", text: "You can close this window." })), hints: [] });
    }
  }

  // ------------------------------------------------------------------ input: controller, keyboard, mouse / touch
  const FOCUSABLE = 'a[href]:not([tabindex="-1"]), button:not([disabled]):not([tabindex="-1"]), input:not([type="hidden"]):not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';
  const TEXT = 'input:not([type]), input[type="text"], input[type="search"], input[type="number"], input[type="password"], textarea';
  const isText = (e) => !!e && !!e.matches && e.matches(TEXT);
  const shown = (e) => { const r = e.getBoundingClientRect(); return r.width >= 2 && r.height >= 2 && getComputedStyle(e).visibility !== "hidden"; };
  const usable = (e) => !!e && e.isConnected && !e.disabled && !e.closest("[hidden], .hidden") && shown(e);
  const scopeRoot = () => (topOverlay() ? topOverlay().node : $("app"));
  const focusables = (root) => $$(FOCUSABLE, root).filter(usable);

  function entry(root) {
    if (root === $("app")) {
      const want = Screen.current && Screen.current.focus;
      const target = typeof want === "string" ? $$(want, $("main")).find(usable) : want;
      if (target && usable(target)) return target;
      return focusables($("main"))[0] || focusables(root)[0];
    }
    return focusables(root)[0];
  }
  function focusEl(e) {
    if (!e) return false;
    e.focus({ preventScroll: true });
    const main = $("main");
    if (main.contains(e)) {
      const r = e.getBoundingClientRect(), m = main.getBoundingClientRect();
      if (r.top < m.top + 8 || r.bottom > m.bottom - 8) e.scrollIntoView({ block: "center", inline: "nearest" });
    } else if (e.closest(".sheet")) e.scrollIntoView({ block: "nearest" });
    return true;
  }

  function score(c, r, dir) {
    const cx = (c.left + c.right) / 2, cy = (c.top + c.bottom) / 2, rx = (r.left + r.right) / 2, ry = (r.top + r.bottom) / 2;
    let primary, ahead;
    if (dir === "right") { primary = r.left - c.right; ahead = rx > cx + 1; }
    else if (dir === "left") { primary = c.left - r.right; ahead = rx < cx - 1; }
    else if (dir === "down") { primary = r.top - c.bottom; ahead = ry > cy + 1; }
    else { primary = c.top - r.bottom; ahead = ry < cy - 1; }
    if (!ahead) return Infinity;
    const horizontal = dir === "left" || dir === "right";
    const overlap = horizontal ? Math.min(c.bottom, r.bottom) - Math.max(c.top, r.top) : Math.min(c.right, r.right) - Math.max(c.left, r.left);
    const gap = overlap > 0 ? 0 : -overlap;
    const offset = horizontal ? Math.abs(ry - cy) : Math.abs(rx - cx);
    return Math.max(primary, 0) + 3 * gap + 0.25 * offset;
  }
  /** A move stays in the part of the screen it starts in (header, list, side panel) while anything lies that way. */
  const section = (e) => e.closest("[data-section]") || body;
  function move(dir) {
    const root = scopeRoot();
    const items = focusables(root);
    if (!items.length) return false;
    const cur = document.activeElement;
    if (!cur || cur === body || !root.contains(cur) || !usable(cur)) return focusEl(entry(root));
    const c = cur.getBoundingClientRect();
    const mine = section(cur);
    let best = null, bestScore = Infinity, other = null, otherScore = Infinity;
    for (const e of items) {
      if (e === cur || e.contains(cur) || cur.contains(e)) continue;
      const s = score(c, e.getBoundingClientRect(), dir);
      if (section(e) === mine) { if (s < bestScore) { best = e; bestScore = s; } }
      else if (s < otherScore) { other = e; otherScore = s; }
    }
    if (!best && !other && (dir === "down" || dir === "up")) {        // the end of a long list: scroll what is left into view
      $("main").scrollBy({ top: (dir === "down" ? 1 : -1) * 200, behavior: "smooth" });
      return true;
    }
    return focusEl(best || other);
  }

  function activate() {
    const e = document.activeElement;
    if (!e || e === body || !usable(e)) return focusEl(entry(scopeRoot()));
    if (isText(e)) { try { e.select(); } catch (_) { /* a type that cannot select */ } return true; }
    e.click();
    return true;
  }

  function back() {
    const o = topOverlay();
    if (o) { closeOverlay(o, true); return true; }
    const cur = Screen.current;
    if (!cur) return false;
    if (typeof cur.back === "function") { cur.back(); return true; }
    if (cur.back) { location.hash = cur.back; return true; }
    return false;
  }

  function screenKey(name) {
    if (topOverlay()) return false;
    const fn = Screen.current && Screen.current.keys && Screen.current.keys[name];
    if (fn) { fn(); return true; }
    return false;
  }
  function search() {
    if (screenKey("Y")) return true;
    const box = $$('input[type="search"]', $("main")).find(usable);
    if (!box) return false;
    focusEl(box);
    return true;
  }
  function pageScroll(sign) {
    const target = topOverlay() ? topOverlay().node.querySelector(".sheet") || $("main") : $("main");
    target.scrollBy({ top: sign * target.clientHeight * 0.8, behavior: "smooth" });
    return true;
  }

  // the input in use: "mouse" (no ring, no strip), "keys" (ring, key names) or "pad" (ring, controller buttons)
  let mode = "mouse";
  function setMode(m) {
    if (m === mode) return;
    mode = m;
    body.classList.toggle("mouse", m === "mouse");
    body.classList.toggle("keys", m === "keys");
    body.classList.toggle("pad", m === "pad");
    renderStrip();
  }

  const ACTIONS = {
    A: activate, B: back, X: () => screenKey("X"), Y: search, LB: () => screenKey("LB"), RB: () => screenKey("RB"),
    LT: () => pageScroll(-1), RT: () => pageScroll(1), Start: () => screenKey("Start"), Menu: appMenu,
    View: () => { location.hash = "#/"; return true; },
    up: () => move("up"), down: () => move("down"), left: () => move("left"), right: () => move("right"),
  };
  /** A controller press (or the key standing in for one). The first one only shows where the focus is. */
  function press(name, from = "pad") {
    const was = mode;
    setMode(from);
    const cur = document.activeElement;
    const lost = !cur || cur === body || cur === $("main") || !scopeRoot().contains(cur) || !usable(cur);
    if ((was === "mouse" || lost) && ["A", "up", "down", "left", "right"].includes(name)) { focusEl(entry(scopeRoot())); return true; }
    const o = topOverlay();
    if (o && o.onPress && o.onPress(name)) return true;
    const act = ACTIONS[name];
    return act ? act() : false;
  }

  // the gamepad (standard mapping)
  const NAMES = ["A", "B", "X", "Y", "LB", "RB", "LT", "RT", "View", "Start", "L3", "R3", "up", "down", "left", "right"];
  const FIRST_DELAY = 420, REPEAT = 120, STICK = 0.55;
  const prev = {}, due = {};
  let raf = 0;
  function poll(now) {
    raf = requestAnimationFrame(poll);
    const pads = (navigator.getGamepads ? Array.from(navigator.getGamepads()) : []).filter((p) => p && p.connected);
    const gp = pads.find((p) => p.mapping === "standard") || pads[0];
    if (!gp) return;
    const down = (i) => !!(gp.buttons[i] && (gp.buttons[i].pressed || gp.buttons[i].value > 0.5));
    const ax = (i) => (gp.axes && gp.axes[i]) || 0;
    const held = { up: down(12) || ax(1) < -STICK, down: down(13) || ax(1) > STICK, left: down(14) || ax(0) < -STICK, right: down(15) || ax(0) > STICK };
    for (const d of Object.keys(held)) {
      if (!held[d]) { due[d] = 0; continue; }
      if (!due[d]) { press(d); due[d] = now + FIRST_DELAY; }
      else if (now >= due[d]) { press(d); due[d] = now + REPEAT; }
    }
    for (const i of [0, 1, 2, 3, 4, 5, 8]) {
      const d = down(i);
      if (d && !prev[i]) press(NAMES[i]);
      prev[i] = d;
    }
    const menu = down(9);                      // the Menu (Start) button: the screen's main action, else the menu
    if (menu && !prev[9]) { setMode("pad"); if (!screenKey("Start")) appMenu(); }
    prev[9] = menu;
    for (const i of [6, 7]) {
      const d = down(i);
      if (d && (!prev[i] || now >= (due[i] || 0))) { press(NAMES[i]); due[i] = now + 300; }
      if (!d) due[i] = 0;
      prev[i] = d;
    }
    const ry = ax(3);
    if (Math.abs(ry) > 0.2) { setMode("pad"); (topOverlay() ? topOverlay().node.querySelector(".sheet") || $("main") : $("main")).scrollBy({ top: ry * 22 }); }
  }
  function startPolling() { if (!raf) raf = requestAnimationFrame(poll); }
  window.addEventListener("gamepadconnected", startPolling);
  if (navigator.getGamepads && Array.from(navigator.getGamepads()).some(Boolean)) startPolling();

  // the keyboard (also Steam Input as keys)
  const ARROWS = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" };
  document.addEventListener("keydown", (e) => {
    if (e.defaultPrevented || e.altKey || e.metaKey) return;
    const t = e.target;
    const inText = isText(t);
    if (e.key === "Escape") { e.preventDefault(); press("B", "keys"); return; }
    if (e.ctrlKey) {
      if (e.key === "Enter") { e.preventDefault(); setMode("keys"); screenKey("Start"); }
      return;
    }
    if (e.key === "F10") { e.preventDefault(); setMode("keys"); appMenu(); return; }
    if (ARROWS[e.key]) {
      const vertical = e.key === "ArrowUp" || e.key === "ArrowDown";
      if (inText && !vertical) return;                     // left / right move the cursor in a text field
      e.preventDefault();
      press(ARROWS[e.key], "keys");
      return;
    }
    if (e.key === "Tab") { setMode("keys"); return; }
    if (e.key === "Enter" && inText) { setMode("keys"); move("down"); e.preventDefault(); return; }
    if (inText) return;
    if (e.key === "Enter" || e.key === " ") { setMode("keys"); return; }   // the browser presses the focused button
    const map = { "/": "Y", x: "X", X: "X", "[": "LB", "]": "RB", Backspace: "B" };
    if (map[e.key]) { e.preventDefault(); press(map[e.key], "keys"); }
    if (e.key === "PageDown" || e.key === "PageUp") { e.preventDefault(); press(e.key === "PageDown" ? "RT" : "LT", "keys"); }
  });
  document.addEventListener("mousemove", (e) => { if (e.movementX || e.movementY) setMode("mouse"); }, { passive: true });
  document.addEventListener("pointerdown", (e) => { if (e.pointerType) setMode("mouse"); }, true);
  document.addEventListener("focusin", () => renderStrip());

  // ------------------------------------------------------------------ the strip
  const KEY_NAMES = { A: "Enter", B: "Esc", X: "X", Y: "/", "LB RB": "[ ]", "LT RT": "PgUp PgDn", Start: "Ctrl+Enter", Menu: "F10", dpad: "Arrows" };
  function glyph(btn) {
    const text = mode === "keys" && !DECK ? (KEY_NAMES[btn] || btn) : btn === "dpad" ? "✥" : btn;
    const round = text.length === 1 || btn === "dpad";
    return el("span", { class: `glyph${round ? " round" : ""} ${["A", "B", "X", "Y"].includes(btn) && text.length <= 5 ? btn.toLowerCase() : ""}`, text });
  }
  let stripKey = "";
  function renderStrip() {
    const strip = $("strip");
    const o = topOverlay();
    let items;
    if (o) items = o.hints || [["A", "Select"], ["B", "Close"]];
    else if (isText(document.activeElement) && mode === "pad") items = [["Steam+X", "Keyboard"], ["B", "Back"]];
    else items = (Screen.current && Screen.current.hints) || [["A", "Select"], ["B", "Back"]];
    const key = mode + "|" + items.map((i) => i.join(":")).join("|");
    if (key === stripKey) return;
    stripKey = key;
    put(strip, ...items.map(([btn, text]) => el("span", { class: "hint" }, glyph(btn), text)));
  }

  // ------------------------------------------------------------------ screens and routing
  let navSerial = 0;
  const Screen = {
    current: null,
    timers: [],
    /** Draw a screen: {title, crumbs: [[text, href]], aside, back, body, hints, keys, focus} */
    show(s) {
      this.current = s;
      Activity.clearLocal();                 // (what the screen before waited for is not waited for any more)
      put($("head-crumbs"), ...(s.crumbs || []).map(([text, href], i) => [
        href ? el("a", { href, text, class: "crumb", tabindex: "-1" }) : el("span", { text }), " › "]).flat());
      $("head-title").textContent = s.title || "ROM Organiser";
      $("head-aside").textContent = s.aside || "";
      $("head-back").classList.toggle("hidden", !s.back);
      document.title = s.title && s.title !== "ROM Organiser" ? `${s.title} - ROM Organiser` : "ROM Organiser";
      put($("main"), s.body);
      $("main").scrollTop = 0;
      stripKey = "";
      renderStrip();
      setTimeout(() => { if (this.current === s && !topOverlay()) focusEl(entry($("app"))); });
    },
    clearTimers() { this.timers.forEach((t) => clearInterval(t)); this.timers = []; },
  };

  /** Each route: [pattern, async (ctx, ...groups) => screen]. ctx.alive() turns false once another screen is shown. */
  const ROUTES = [];
  function route(pattern, fn) { ROUTES.push([pattern, fn]); }

  async function navigate() {
    const serial = ++navSerial;
    Screen.clearTimers();
    Activity.watching = null;                // (a task followed on the screen left goes on: its end becomes a toast)
    while (overlays.length) closeOverlay(overlays[overlays.length - 1], true);
    const hash = location.hash.replace(/^#/, "") || "/";
    const [path, query = ""] = hash.split("?");
    const params = Object.fromEntries(new URLSearchParams(query));
    const ctx = {
      alive: () => serial === navSerial,
      params,
      every(fn, ms) { const t = setInterval(() => { if (serial === navSerial) fn(); }, ms); Screen.timers.push(t); return t; },
      show(s) { if (serial === navSerial) Screen.show(s); },
      redraw: () => { if (serial === navSerial) navigate(); },
    };
    for (const [pattern, fn] of ROUTES) {
      const m = path.match(pattern);
      if (!m) continue;
      try {
        const s = await fn(ctx, ...m.slice(1).map(decodeURIComponent));
        if (s) ctx.show(s);
      } catch (err) {
        ctx.show({ title: "Something went wrong", back: "#/", body: el("div", { class: "screen" }, message(err.message, "bad")), hints: [["B", "Back"]] });
      }
      return;
    }
    location.replace("#/");
  }
  window.addEventListener("hashchange", navigate);
  $("head-back").addEventListener("click", back);
  $("head-menu").addEventListener("click", appMenu);
  $("head-activity").addEventListener("click", showActivity);

  // ------------------------------------------------------------------ Home
  route(/^\/?$/, async () => {
    await Promise.all([loadStatus(), loadPlatforms()]);
    const st = S.status;
    const setUp = S.platforms.filter((p) => p.folder);
    const last = setUp.find((p) => p.name === st.last_platform) || setUp[0];
    // (no "Build library" here: a build is a system's - its own screen has the tile; the focus starts on the system used last)
    const top = el("div", { class: "grid compact", style: "--cols:2" },
      tile({ title: "Archive", icon: "archive", sub: archiveText(archiveBase()), href: "#/settings/archive", id: "home-archive", short: true }),
      tile({ title: "Settings", icon: "gear", sub: "Rules, folders, databases", href: "#/settings", id: "home-settings", short: true }));

    const sysTile = (p) => {
      const ls = p.last_scan;
      const extra = ls ? el("span", { class: "bar" }, el("span", { style: `width:${Math.round(ls.pct || 0)}%` })) : null;
      const t = tile({ title: p.name, small: true, href: `#/system/${p.slug}`, key: p.slug,
        sub: ls ? `${Math.round(ls.pct || 0)}%` : p.folder ? "Not scanned" : "Not set up", extra });
      t.dataset.name = p.name.toLowerCase();
      if (p.folder && ls) {                    // (resting on a system - pointer or controller focus - reads its saved scan, so the click finds it)
        let wait = 0;
        const rest = () => { clearTimeout(wait); wait = setTimeout(() => { post("/api/scan/warm", { platform: p.name }).catch(() => {}); }, 200); };
        const leave = () => clearTimeout(wait);
        t.addEventListener("pointerenter", rest); t.addEventListener("focus", rest);
        t.addEventListener("pointerleave", leave); t.addEventListener("blur", leave);
      }
      return t;
    };
    const groups = [["Your systems", setUp], ["Other systems", S.platforms.filter((p) => !p.folder)]].filter(([, list]) => list.length);
    const sections = groups.map(([title, list]) => el("section", { class: "section" },
      groups.length > 1 ? el("h2", { class: "section-title", text: title }) : null, el("div", { class: "grid small" }, list.map(sysTile))));
    const find = el("input", { type: "search", class: "search", id: "home-find", placeholder: "Find a system", "aria-label": "Find a system",
      autocomplete: "off", spellcheck: "false" });
    find.addEventListener("input", () => {
      const q = find.value.trim().toLowerCase();
      for (const s of sections) {
        let any = false;
        for (const t of $$(".tile", s)) { const hit = !q || t.dataset.name.includes(q); t.classList.toggle("hidden", !hit); any = any || hit; }
        s.classList.toggle("hidden", !any);
      }
    });
    const systems = el("div", { class: "section", id: "home-systems" },
      el("div", { class: "btns" }, groups.length === 1 ? el("h2", { class: "section-title", text: "Systems" }) : null, find), sections);
    return {
      title: "ROM Organiser", aside: `v${st.version}`, focus: last ? `#home-systems [data-key="${last.slug}"]` : "#home-find",
      body: el("div", { class: "screen" }, systems, top),
      hints: [["A", "Open"], ["Y", "Find a system"], ["Menu", "Options"]],
      keys: { Y: () => focusEl(find) },
    };
  });

  // ------------------------------------------------------------------ a system
  /** The tiles of the system screen that this UI draws (the others of the setting are drawn by the classic page). */
  const TILE_DRAW = {
    have_all: (d) => {
      const s = d.summary;
      if (!s) return null;
      const have = s.games_have ?? s.have, total = s.games_total ?? s.dat_total;
      return statTile({ key: "have_all", label: "Games you have", number: fmt(have), of: fmt(total), bar: pct(have, total) });
    },
    have_rules: (d) => {
      const t = d.totals;
      if (!t || t.calculating || t.target_games === null) return statTile({ key: "have_rules", label: "With your rules", loading: true, sub: workText(t && t.working) });
      return statTile({ key: "have_rules", label: "With your rules", number: fmt(t.have_games), of: fmt(t.target_games), bar: pct(t.have_games, t.target_games),
        href: `#/system/${d.p.slug}/target` });
    },
    needs_look: (d) => {
      const t = d.totals;
      if (!t || t.calculating || t.target_games === null) return statTile({ key: "needs_look", label: "Needs a look", loading: true, sub: "Waiting for your rules" });
      // (what your rules exclude is your choice, not a problem; an incomplete set is archived by the build: only versions you could improve count)
      const n = t.not_preferred || 0;
      return statTile({ key: "needs_look", label: "Needs a look", number: fmt(n), unit: n === 1 ? "game" : "games", warn: n > 0,
        sub: n ? "" : "Nothing to check", href: `#/system/${d.p.slug}/look` });
    },
    files_to_change: (d) => (d.plan ? statTile({ key: "files_to_change", label: "Files to change", number: fmt(d.plan.files), sub: "On the next build" })
      : statTile({ key: "files_to_change", label: "Files to change", loading: true, sub: "Working out what changes" })),
    unmatched: (d) => (d.summary && d.summary.unmatched_files ? statTile({ key: "unmatched", label: "Unmatched files", number: fmt(d.summary.unmatched_files), sub: "Not in the database" }) : null),
    bios: (d) => (d.summary && d.summary.bios_files ? statTile({ key: "bios", label: "BIOS and firmware", number: fmt(d.summary.bios_files), sub: "Found among your files" }) : null),
    saves: (d) => (d.saves && d.saves.files ? statTile({ key: "saves", label: "Saves", number: fmt(d.saves.files), sub: "Saves and save states" }) : null),
    database: (d) => {
      const dat = (d.p.dats || []).find((x) => x.present);
      return statTile({ key: "database", label: "Database", number: dat ? dat.version || "Installed" : "Missing", smallNumber: true,
        warn: !dat, sub: dat ? (SOURCE[dat.source] || dat.source || "") : "Install it in Databases" });
    },
    last_scan: (d) => statTile({ key: "last_scan", label: "Last scan", number: d.p.last_scan ? ago(d.p.last_scan.at) : "Never", smallNumber: true,
      sub: d.p.last_scan ? `${fmt(d.p.last_scan.matched_files)} files matched` : "" }),
    convert: (d) => (d.insights && d.insights.convert && d.insights.convert.games ? statTile({ key: "convert", label: "Convert",
      number: fmt(d.insights.convert.games), unit: "games", sub: "Could become CHDs" }) : null),
  };

  async function systemScreen(ctx, slug) {
    await Promise.all([loadPlatforms(), loadStatus()]);
    const p = bySlug(slug);
    if (!p) { location.replace("#/"); return null; }
    const setUp = S.platforms.filter((x) => x.folder);
    const step = (delta) => {
      const list = setUp.length ? setUp : S.platforms;
      const i = list.findIndex((x) => x.slug === slug);
      const next = list[(i + delta + list.length) % list.length];
      if (next && next.slug !== slug) location.hash = `#/system/${next.slug}`;
    };
    const head = { title: p.name, crumbs: [["Home", "#/"]], back: "#/", aside: SOURCE[p.source] || "",
      keys: { LB: () => step(-1), RB: () => step(1) } };
    const folderTile = tile({ title: "Folder", icon: "folder", short: true, sub: p.folder ? baseName(p.folder) : "Not chosen", href: `#/system/${slug}/folder` });

    if (!p.folder) {
      return { ...head, focus: "#sys-choose",
        body: el("div", { class: "screen" }, el("p", { class: "lead", text: "Choose the folder this system's games are in." }),
          el("div", { class: "grid", style: "--cols:2" }, tile({ title: "Choose the folder", icon: "folder", primary: true, id: "sys-choose",
            sub: "Subfolders are included", href: `#/system/${slug}/folder` }))),
        hints: [["A", "Select"], ["B", "Back"], ["LB RB", "Next system"]] };
    }

    let sel = { selected: false };
    // (reading a big folder's saved scan takes a few seconds the first time: the page shows at once and says so)
    const waiting = setTimeout(() => {
      if (!ctx.alive()) return;
      Activity.set("screen", "Loading the last scan");
      ctx.show({ ...head, body: el("div", { class: "screen" }, el("p", { class: "lead", text: "Loading the last scan of this folder…" }), skeletonTiles(5)),
        hints: [["B", "Back"], ["LB RB", "Next system"]] });
    }, 150);
    try { sel = await post("/api/scan/select", { platform: p.name }); } catch (_) { /* no scan */ }
    clearTimeout(waiting);
    Activity.clear("screen");
    if (!ctx.alive()) return null;
    const rescan = () => startScan(ctx, p);
    if (!sel.selected) {
      return { ...head, focus: "#sys-scan", keys: { ...head.keys, X: rescan },
        body: el("div", { class: "screen" }, el("div", { class: "grid" },
          tile({ title: "Scan the folder", icon: "rescan", primary: true, id: "sys-scan", sub: baseName(p.folder), onClick: rescan })),
          el("div", { class: "grid", style: "--cols:4" }, folderTile)),
        hints: [["A", "Select"], ["B", "Back"], ["LB RB", "Next system"]] };
    }
    await loadStatus();
    if (!ctx.alive()) return null;
    const scan = S.status.scan || {};
    const data = { p, summary: scan.summary || null, saves: scan.saves || null, totals: null, plan: null, insights: null };
    const shownIds = (S.status.tiles || []).filter((id) => TILE_DRAW[id]);
    const stats = el("div", { class: "grid compact", id: "sys-tiles" });
    const buildTile = tile({ title: "Build library", icon: "play", primary: true, id: "sys-build", sub: "Working out what changes…", href: `#/system/${slug}/build` });
    const drawTiles = () => {
      const focused = document.activeElement && document.activeElement.closest && document.activeElement.closest("#sys-tiles") ? document.activeElement.dataset.key || document.activeElement.id : "";
      const nodes = shownIds.map((id) => TILE_DRAW[id](data)).filter(Boolean);
      nodes.splice(Math.min(2, nodes.length), 0, buildTile);           // the main action ends the first row
      put(stats, ...nodes);
      if (focused) { const again = stats.querySelector(`[data-key="${focused}"], #${focused}`); if (again) focusEl(again); }
    };
    drawTiles();
    // Missing: what YOUR RULES keep that you do not have (Files › Missing lists the same), from the totals; shown once they are known
    const missingTile = tile({ title: "Missing", icon: "search", short: true, sub: "", href: `#/system/${slug}/files?view=missing` });
    missingTile.classList.add("hidden");
    const drawMissing = (failed = false) => {
      const t = data.totals;
      let games = 0;
      if (t && !t.calculating && t.have_games !== null && t.have_games !== undefined) {
        games = t.missing_games || 0;
      } else if (failed && data.summary) {
        games = data.summary.games_missing ?? data.summary.missing ?? 0;          // (no totals: against the whole database)
      } else return;
      const plural = (n, one, many) => `${fmt(n)} ${n === 1 ? one : many}`;
      missingTile.querySelector(".tile-sub").textContent = games ? plural(games, "game", "games") : "";      // (system disks are never missing)
      missingTile.classList.toggle("hidden", !games);
    };
    const actions = el("div", { class: "grid", style: "--cols:4" },
      tile({ title: "Files", icon: "list", short: true, sub: "Every file and game", href: `#/system/${slug}/files` }),
      tile({ title: "Needs a look", icon: "search", short: true, sub: "Fixes the rules suggest", href: `#/system/${slug}/look` }),
      tile({ title: "Rules", icon: "toggle", short: true, sub: "This system's library", href: `#/system/${slug}/rules` }),
      tile({ title: "Settings", icon: "gear", short: true, sub: "Building, archive", href: `#/system/${slug}/settings` }),
      folderTile,
      missingTile,
      data.saves && data.saves.files ? tile({ title: "Saves", icon: "save", short: true, sub: `${fmt(data.saves.files)} files`, href: `#/system/${slug}/saves` }) : null);
    const warn = (scan.missing_dats || []).length && !(p.dats || []).some((d) => d.present)
      ? message("This system's database is not installed: nothing can be recognised. Install it in Databases.", "warn") : null;

    // what takes a moment: the preview of the build, the totals of the rules, the insights
    buildTile.append(el("span", { class: "mini", "aria-hidden": "true" }));
    setTimeout(() => { if (ctx.alive() && !data.plan) Activity.set("plan", "Working out what changes"); }, 0);
    (async () => {
      try {
        const plan = await post("/api/library/plan", { ...buildOptions(p), limit: 1, summary: true });
        Activity.clear("plan");
        if (!ctx.alive()) return;
        buildTile.querySelector(".mini")?.remove();
        data.plan = plan;
        buildTile.querySelector(".tile-sub").textContent = plan.files ? `${fmt(plan.files)} file${plan.files === 1 ? "" : "s"} will change` : "Nothing to change";
        drawTiles();
      } catch (err) { Activity.clear("plan"); if (ctx.alive()) { buildTile.querySelector(".mini")?.remove(); buildTile.querySelector(".tile-sub").textContent = err.message; } }
    })();
    (async () => {
      for (let i = 0; i < 900 && ctx.alive(); i++) {
        try { data.totals = await get(`/api/library/totals?${qs({ platform: p.name })}`); } catch (_) { Activity.clear("totals"); drawMissing(true); break; }
        if (!ctx.alive()) return;
        drawTiles();
        drawMissing();
        if (!data.totals.calculating) { Activity.clear("totals"); break; }
        Activity.set("totals", `Working out the library · ${workText(data.totals.working)}`);
        await sleep(1000);
      }
    })();
    if (shownIds.includes("convert")) {
      get(`/api/library/insights?${qs({ platform: p.name })}`).then((ins) => { if (ctx.alive()) { data.insights = ins; drawTiles(); } }).catch(() => {});
    }
    return { ...head, focus: "#sys-build", keys: { ...head.keys, X: rescan },
      body: el("div", { class: "screen" }, warn, stats, actions),
      hints: [["A", "Select"], ["B", "Back"], ["LB RB", "Next system"], ["X", "Rescan"]] };
  }
  route(/^\/system\/([^/]+)$/, systemScreen);

  async function startScan(ctx, p) {
    try {
      await post("/api/scan", { path: p.folder, platform: p.name });
    } catch (err) { toast(err.message, "error"); return; }
    const view = progressView("Scanning…");
    let job = null;
    const stop = async () => { if (job && job.cancellable && await confirmDialog("Stop the scan?", "Stop", "Keep scanning")) post("/api/job/cancel").catch(() => {}); };
    ctx.show({ title: p.name, crumbs: [["Home", "#/"]], back: "#/", keys: { X: stop },
      body: el("div", { class: "screen" }, view.node, el("p", { class: "note", text: "You can go back and do something else: the scan carries on." })),
      hints: [["B", "Back (it carries on)"], ["X", "Stop"]] });
    job = await followJob((j) => { job = j; if (ctx.alive()) { Activity.watching = j.id; view.update(j); } }, ctx.alive);
    if (!ctx.alive()) { Activity.finished(job); return; }
    if (job.status === "error") toast(job.error || "The scan failed", "error", 8000);
    ctx.redraw();
  }

  // ------------------------------------------------------------------ Build library
  route(/^\/system\/([^/]+)\/build$/, async (ctx, slug) => {
    await Promise.all([loadPlatforms(), loadStatus()]);
    const p = bySlug(slug);
    if (!p) { location.replace("#/"); return null; }
    const head = { title: "Build library", crumbs: [[p.name, `#/system/${slug}`]], back: `#/system/${slug}` };
    if (!p.folder) return { ...head, body: el("div", { class: "screen" }, message("Choose this system's folder first.", "warn")), hints: [["B", "Back"]] };
    let sel = { selected: false };
    try { sel = await post("/api/scan/select", { platform: p.name }); } catch (_) { /* below */ }
    if (!sel.selected) {
      return { ...head, body: el("div", { class: "screen" }, message("Scan the folder first: the build works from the last scan.", "warn"),
        el("div", { class: "btns" }, el("a", { class: "btn", href: `#/system/${slug}`, text: `Back to ${p.name}` }))), hints: [["A", "Select"], ["B", "Back"]] };
    }
    ctx.show({ ...head, body: el("div", { class: "screen" }, el("h2", { class: "big-title", text: "What will change" }), skeletonTiles(3)), hints: [["B", "Back"]] });
    Activity.set("screen", "Working out what changes");
    const opts = buildOptions(p);
    const plan = await post("/api/library/plan", { ...opts, limit: 1, summary: true });
    if (!ctx.alive()) return null;
    const cat = plan.categories || {};
    const excluded = cat.excluded || 0, superseded = cat.superseded || 0;
    const other = (cat.incomplete || 0) + (cat.duplicate || 0) + (cat.unmatched || 0) + (cat.saves || 0) + (cat.bios || 0);
    const movedOrRenamed = Math.max(0, (plan.files || 0) - excluded - superseded - other);
    const conv = (plan.convert && plan.convert.count) || 0;
    const lists = (plan.playlists && plan.playlists.write) || 0;
    const nothing = !plan.files && !conv && !lists;
    const counts = [
      movedOrRenamed ? statTile({ label: "", number: fmt(movedOrRenamed), sub: "Renamed or moved" }) : null,
      excluded ? statTile({ label: "", number: fmt(excluded), sub: "Excluded by your rules" }) : null,
      superseded ? statTile({ label: "", number: fmt(superseded), sub: "Older versions replaced" }) : null,
      other ? statTile({ label: "", number: fmt(other), sub: "Set aside (incomplete, duplicates, other)" }) : null,
      conv ? statTile({ label: "", number: fmt(conv), sub: "Converted first" }) : null,
      lists ? statTile({ label: "", number: fmt(lists), sub: lists === 1 ? "Playlist written" : "Playlists written" }) : null,
    ].filter(Boolean);
    const where = opts.export_to ? `The library is built in ${opts.export_to}; this folder is only read.`
      : opts.aside_to ? `What is set aside goes to the archive: ${archiveText(opts.aside_to)}.`
        : "A build needs an archive: what the rules set aside is archived there, never deleted. Choose one first.";
    const startBtn = !opts.export_to && !opts.aside_to
      ? el("a", { class: "btn primary", id: "build-start", href: "#/settings/archive" }, icon("folder"), "Choose an archive")
      : el("button", { class: "btn primary", type: "button", id: "build-start", on: { click: () => runBuild(ctx, p, opts, plan) } }, icon("play"), "Start build");
    const listLink = el("a", { class: "btn", href: `#/system/${slug}/files?view=library`, text: "See the file list" });
    if (nothing) {
      return { ...head, focus: "#build-back",
        body: el("div", { class: "screen" }, el("h2", { class: "big-title", text: "Nothing to change" }),
          el("p", { class: "lead", text: "The folder already matches your rules." }),
          el("div", { class: "btns" }, el("a", { class: "btn", id: "build-back", href: `#/system/${slug}`, text: `Back to ${p.name}` }), listLink)),
        hints: [["A", "Select"], ["B", "Back"]] };
    }
    const title = plan.files ? `${fmt(plan.files)} file${plan.files === 1 ? "" : "s"} will change` : conv ? `${fmt(conv)} game${conv === 1 ? "" : "s"} to convert` : "Playlists to write";
    return { ...head, focus: "#build-start", keys: { Start: () => startBtn.click() },
      body: el("div", { class: "screen" }, el("h2", { class: "big-title", text: title }),
        el("div", { class: "grid compact", style: `--cols:${Math.min(4, Math.max(counts.length, 1))}` }, counts),
        where ? el("p", { class: "note", text: where }) : null,
        el("div", { class: "btns" }, startBtn, listLink)),
      hints: [["A", "Start"], ["B", "Back"]] };
  });

  async function runBuild(ctx, p, opts, plan) {
    let job;
    try {
      job = (await post("/api/library/apply", { ...opts, plan_id: plan.plan_id })).job;
    } catch (err) {
      if (err.code === "stale_plan") { toast("The folder or the rules changed: here is the new preview.", "", 6000); ctx.redraw(); return; }
      toast(err.message, "error", 8000);
      return;
    }
    const view = progressView("Building…");
    const head = { title: "Build library", crumbs: [[p.name, `#/system/${p.slug}`]] };
    ctx.show({ ...head, back: `#/system/${p.slug}`, body: el("div", { class: "screen" }, view.node,
      el("p", { class: "note", text: "You can go back and do something else: the build carries on, and the top of the screen shows it." })),
      hints: [["B", "Back (it carries on)"]] });
    job = await followJob((j) => { if (ctx.alive()) { Activity.watching = j.id; view.update(j); } }, ctx.alive);
    if (!ctx.alive()) { Activity.finished(job); return; }
    const r = job.result || {};
    if (job.status === "done" && r.whdload_next) {
      try {
        await post("/api/whdload/build", { platform: r.whdload_next });
        view.title("Building WHDLoad games…");
        await followJob((j) => view.update(j), ctx.alive);
      } catch (err) { toast(err.message, "error"); }
      if (!ctx.alive()) return;
    }
    showBuildResult(ctx, p, job, head);
  }

  function showBuildResult(ctx, p, job, head) {
    const r = job.result || {};
    const n = (v) => (Array.isArray(v) ? v.length : v || 0);
    const failed = (Array.isArray(r.failed) ? r.failed : []).concat(Array.isArray(r.skipped) ? r.skipped : []);
    const ok = job.status === "done" && !failed.length && !r.error;
    const lines = [];
    if (r.action === "library_export") lines.push(`${fmt(r.created || 0)} files built in ${r.dest}`);
    else lines.push(`${fmt(n(r.moved))} file${n(r.moved) === 1 ? "" : "s"} moved or renamed`);
    if (n(r.playlists_written)) lines.push(`${fmt(n(r.playlists_written))} playlist${n(r.playlists_written) === 1 ? "" : "s"} written`);
    if (r.converted && r.converted.count) lines.push(`${fmt(r.converted.count)} converted first`);
    if (r.split && r.split.zips) lines.push(`${fmt(r.split.zips)} zip${r.split.zips === 1 ? "" : "s"} cleaned: ${fmt(r.split.files)} file${r.split.files === 1 ? "" : "s"} that are no game went to Unmatched`);
    if (r.aside && r.aside.moved) lines.push(`${fmt(r.aside.moved)} file${r.aside.moved === 1 ? "" : "s"} ${r.aside.s3 ? "uploaded" : "moved"} to the archive`);
    const title = job.status === "error" ? "The build stopped" : ok ? "Library built" : "Built, with problems";
    const back = el("a", { class: "btn primary", id: "result-back", href: `#/system/${p.slug}`, text: `Back to ${p.name}` });
    ctx.show({ ...head, back: `#/system/${p.slug}`, focus: "#result-back",
      body: el("div", { class: "screen" }, el("h2", { class: "big-title", text: title }),
        job.error ? message(job.error, "bad") : null,
        el("div", { class: "rows" }, lines.map((t) => row({ name: t, static: true, icon: "check" }))),
        r.error ? message(`Stopped: ${r.error}`, "bad") : null,
        failed.length ? el("div", { class: "section" }, el("h3", { class: "section-title", text: `${fmt(failed.length)} not done` }),
          el("div", { class: "rows" }, failed.slice(0, 30).map((f) => row({ static: true, name: baseName(f.src || f.path || ""),
            sub: f.error || f.reason || "failed" })))) : null,
        el("div", { class: "btns" }, back, el("a", { class: "btn", href: "#/", text: "Home" }))),
      hints: [["A", "Select"], ["B", "Back"]] });
  }

  // ------------------------------------------------------------------ the folder browser
  const PLACE_ICON = (name) => (/sd|card|media/i.test(name) ? "sd" : /usb/i.test(name) ? "usb" : /home/i.test(name) ? "home" : "drive");
  /** One folder at a time, places on the left. o: {head, start, choose(path), chooseLabel} */
  async function folderScreen(ctx, o) {
    let data = null, top = null;
    const list = el("div", { class: "rows", "data-section": "list" });
    const places = el("div", { class: "rows places", "data-section": "places" });
    const where = el("div", { class: "crumb-path" });
    const open = async (path, focusFirst = true) => {
      try {
        data = await get(`/api/fs/list?${qs({ path, hidden: Prefs.get("hidden", false) ? "1" : "" })}`);
      } catch (err) { toast(err.message, "error"); return; }
      if (!ctx.alive()) return;
      if (top === null || !data.path.startsWith(top)) top = (data.places.find((pl) => data.path.startsWith(pl.path)) || {}).path || data.path;
      put(where, el("b", { text: data.path }));
      const name = baseName(data.path);
      const use = row({ name: `Use “${name}”`, icon: "check", id: "fb-use", onClick: () => o.choose(data.path) });
      put(list, use, ...data.dirs.map((d) => row({ name: d.name, icon: "folder", arrow: true, onClick: () => open(d.path) })),
        data.dirs.length ? null : row({ name: "No folders inside", static: true }));
      put(places, ...data.places.map((pl) => row({ name: pl.name, icon: PLACE_ICON(pl.name), onClick: () => { top = pl.path; open(pl.path); } })));
      Screen.current.hints = [["A", "Open"], ["B", data.parent && data.path !== top ? "Up a folder" : "Back"], ["Y", `Use “${name}”`]];
      stripKey = "";
      renderStrip();
      if (focusFirst) focusEl(list.querySelectorAll("button.row")[1] || use);
      else if (!topOverlay()) focusEl(use);
    };
    const s = {
      ...o.head,
      back: () => { if (data && data.parent && data.path !== top) open(data.parent); else location.hash = o.head.back; },
      focus: "#fb-use",
      keys: { Y: () => data && o.choose(data.path) },
      body: el("div", { class: "screen tight" }, where, el("div", { class: "split" }, places, el("div", { class: "grow" }, list))),
      hints: [["A", "Open"], ["B", "Up a folder"], ["Y", "Use this folder"]],
    };
    ctx.show(s);
    await open(o.start || "", false);
    return null;
  }

  route(/^\/system\/([^/]+)\/folder$/, async (ctx, slug) => {
    await loadPlatforms();
    const p = bySlug(slug);
    if (!p) { location.replace("#/"); return null; }
    return folderScreen(ctx, {
      head: { title: "Choose a folder", crumbs: [[p.name, `#/system/${slug}`]], back: `#/system/${slug}` },
      start: p.folder || "",
      choose: async (path) => {
        try {
          await post("/api/folders", { platform: p.name, path });
          toast(`${p.name}: ${path}`);
          location.hash = `#/system/${slug}`;
        } catch (err) { toast(err.message, "error"); }
      },
    });
  });

  // ------------------------------------------------------------------ Settings
  route(/^\/settings$/, async () => {
    await loadStatus();
    const u = S.status.updates || {};
    const updates = ["tosec", "nointro", "redump"].filter((k) => u[k] && u[k].status === "update_available").length;
    return {
      title: "Settings", crumbs: [["Home", "#/"]], back: "#/",
      body: el("div", { class: "screen" }, el("div", { class: "grid dense" },
        tile({ title: "Rules", icon: "toggle", sub: "What every library keeps", href: "#/settings/rules", id: "set-rules" }),
        tile({ title: "Archive", icon: "archive", sub: archiveText(archiveBase()), href: "#/settings/archive", id: "set-archive" }),
        tile({ title: "System page tiles", icon: "tiles", sub: "Which numbers a system shows", href: "#/settings/tiles", id: "set-tiles" }),
        tile({ title: "Databases", icon: "db", sub: updates ? `${updates} update${updates === 1 ? "" : "s"} available` : "TOSEC, No-Intro, Redump", href: "#/settings/databases", id: "set-databases" }),
        tile({ title: "BIOS and firmware", icon: "chip", sub: "What emulators need", href: "#/settings/bios", id: "set-bios" }),
        tile({ title: "Emulators", icon: "gamepad", sub: "RetroArch and others", href: "#/settings/emulators", id: "set-emulators" }),
        tile({ title: "Saves", icon: "save", sub: "What a build does with them", href: "#/settings/saves", id: "set-saves" }),
        tile({ title: "Disc images", icon: "disc", sub: "CHD and chdman", href: "#/settings/disc", id: "set-disc" }),
        tile({ title: "Advanced", icon: "gear", sub: "Rarely needed", href: "#/settings/advanced", id: "set-advanced" }))),
      hints: [["A", "Open"], ["B", "Back"]],
    };
  });

  // ------------------------------------------------------------------ Settings: the archive
  route(/^\/settings\/archive$/, async (ctx) => {
    await Promise.all([loadStatus(), loadPlatforms()]);
    const cur = archiveBase();
    const s3 = cur.startsWith("s3://");
    const conn = (S.status && S.status.s3) || {};
    const choices = el("div", { class: "grid", style: "--cols:2" },
      tile({ title: "Folder", icon: "folder", sub: "A drive or folder on this computer", check: !!cur && !s3, href: "#/settings/archive/folder", id: "arch-folder" }),
      tile({ title: "S3 bucket", icon: "cloud", sub: "Cloud storage", check: s3, href: "#/settings/archive/s3", id: "arch-s3" }));
    const rows = el("div", { class: "rows" });
    if (!cur) rows.append(message("A build needs an archive: choose where what the rules set aside goes. It is archived there, never deleted."));
    else if (s3) {
      const test = row({ name: "Test connection", icon: "test", id: "arch-test", value: "", onClick: async () => {
        const v = test.querySelector(".row-value");
        v.className = "row-value"; v.textContent = "Testing…";
        try {
          await post("/api/settings/s3/test", { location: cur });
          v.className = "row-value good"; v.textContent = "Connected";
        } catch (err) { v.className = "row-value bad"; v.textContent = err.message; }
      } });
      rows.append(
        row({ name: "Connection", icon: "cloud", value: conn.endpoint ? `${conn.endpoint}${conn.access_key ? ` · ${conn.access_key}` : ""}` : "Not set up", arrow: true, href: "#/settings/archive/s3" }),
        row({ name: "Bucket and folder", icon: "archive", value: cur.slice(5), arrow: true, href: "#/settings/archive/s3" }), test);
    } else {
      rows.append(row({ name: "Folder", icon: "folder", value: cur, arrow: true, href: "#/settings/archive/folder" }));
    }
    if (cur) rows.append(row({ name: "Stop archiving", icon: "stop", onClick: async () => {
      if (!await confirmDialog("Stop archiving?", "Stop archiving", "Cancel", "Files already in the archive stay there.")) return;
      try { await post("/api/settings/archive", { dir: "" }); ctx.redraw(); } catch (err) { toast(err.message, "error"); }
    } }));
    const overrides = Object.entries((S.status.archive && S.status.archive.overrides) || {});
    const own = overrides.length ? el("section", { class: "section" }, el("h2", { class: "section-title", text: "Systems with their own archive" }),
      el("div", { class: "rows" }, overrides.map(([name, loc]) => {
        const p = S.platforms.find((x) => x.name === name);
        return row({ name, value: archiveText(loc), arrow: !!p, href: p ? `#/settings/archive/folder?system=${p.slug}` : undefined, static: !p });
      }))) : null;
    return { title: "Archive", crumbs: [["Settings", "#/settings"]], back: "#/settings", focus: s3 ? "#arch-s3" : "#arch-folder",
      body: el("div", { class: "screen" }, choices, rows, own), hints: [["A", "Select"], ["B", "Back"]] };
  });

  route(/^\/settings\/archive\/folder$/, async (ctx) => {
    await Promise.all([loadStatus(), loadPlatforms()]);
    const p = ctx.params.system ? bySlug(ctx.params.system) : null;
    const cur = p ? archiveFor(p) : archiveBase();
    return folderScreen(ctx, {
      head: { title: p ? `Archive folder of ${p.name}` : "Archive folder", crumbs: [["Archive", "#/settings/archive"]], back: "#/settings/archive" },
      start: cur && !cur.startsWith("s3://") ? cur : "",
      choose: async (path) => {
        try {
          await post("/api/settings/archive", { dir: path, ...(p ? { platform: p.name } : {}) });
          toast(`Archive: ${path}`);
          location.hash = "#/settings/archive";
        } catch (err) { toast(err.message, "error"); }
      },
    });
  });

  route(/^\/settings\/archive\/s3$/, async (ctx) => {
    await loadStatus();
    const conn = (S.status && S.status.s3) || {};
    const cur = archiveBase();
    const loc = cur.startsWith("s3://") ? cur.slice(5) : "";
    const [bucket0, ...rest] = loc.split("/");
    const SERVICES = [{ label: "MEGA S4", endpoint: "s3.g.megas4.com" }, { label: "Another S3 service", endpoint: "" }];
    const known = SERVICES.find((x) => x.endpoint && x.endpoint === conn.endpoint);
    const service = row({ name: "Service", value: known ? known.label : conn.endpoint ? "Another S3 service" : "Choose", arrow: true, id: "s3-service",
      onClick: async () => {
        const v = await picker("Service", SERVICES.map((x) => ({ label: x.label, sub: x.endpoint || "Type its endpoint below", value: x.label,
          current: (known ? known.label : conn.endpoint ? "Another S3 service" : "") === x.label })));
        if (!v) return;
        const sv = SERVICES.find((x) => x.label === v);
        service.querySelector(".row-value").textContent = `${v} ›`;
        if (sv.endpoint) $("s3-endpoint").value = sv.endpoint;
        else focusEl($("s3-endpoint"));
      } });
    const fields = el("div", { class: "rows", "data-section": "fields" }, service,
      field({ label: "Endpoint", id: "s3-endpoint", value: conn.endpoint, placeholder: "s3.example.com" }),
      field({ label: "Region", id: "s3-region", value: conn.region, placeholder: "Optional" }),
      field({ label: "Access key", id: "s3-access", value: conn.access_key, placeholder: "Access key ID" }),
      field({ label: "Secret key", id: "s3-secret", type: "password", placeholder: conn.secret_set ? "Saved - type to replace" : "Secret key" }),
      field({ label: "Bucket", id: "s3-bucket", value: bucket0 || "", placeholder: "bucket-name" }),
      field({ label: "Folder in it", id: "s3-prefix", value: rest.join("/"), placeholder: "Optional, e.g. roms" }));
    const result = el("div", {});
    const save = async () => {
      const v = (id) => $(id).value.trim();
      if (!v("s3-endpoint") || !v("s3-access") || !v("s3-bucket")) { put(result, message("Endpoint, access key and bucket are needed.", "warn")); return; }
      if (!conn.secret_set && !v("s3-secret")) { put(result, message("Type the secret key.", "warn")); return; }
      put(result, message("Saving and testing…"));
      const location_ = `s3://${v("s3-bucket")}${v("s3-prefix") ? `/${v("s3-prefix").replace(/^\/+|\/+$/g, "")}` : ""}`;
      try {
        await post("/api/settings/s3", { endpoint: v("s3-endpoint"), region: v("s3-region"), access_key: v("s3-access"), secret_key: $("s3-secret").value });
        $("s3-secret").value = "";
        await post("/api/settings/archive", { dir: location_ });
        await post("/api/settings/s3/test", { location: location_ });
        if (ctx.alive()) put(result, message(`Connected. The archive is ${location_}.`, "good"));
      } catch (err) { if (ctx.alive()) put(result, message(err.message, "bad")); }
    };
    const forget = async () => {
      if (!await confirmDialog("Forget the S3 keys?", "Forget", "Cancel", "The archive setting stays; nothing in the bucket is touched.")) return;
      try { await post("/api/settings/s3", { clear: true }); ctx.redraw(); } catch (err) { toast(err.message, "error"); }
    };
    const panel = el("aside", { class: "panel", "data-section": "panel" },
      el("button", { class: "btn primary", type: "button", id: "s3-save", style: "min-width:0", on: { click: save } }, "Save and test"),
      el("button", { class: "btn", type: "button", on: { click: forget } }, "Forget keys"),
      el("p", { class: "note", text: "The keys stay on this computer, in a file only you can read." }), result);
    return { title: "S3 connection", crumbs: [["Archive", "#/settings/archive"]], back: "#/settings/archive", focus: "#s3-service",
      keys: { Start: save },
      body: el("div", { class: "screen" }, el("div", { class: "split" }, el("div", { class: "grow" }, fields), panel)),
      hints: [["A", "Select"], ["B", "Back"], ["Start", "Save and test"]] };
  });

  // ------------------------------------------------------------------ Settings: the tiles of the system screen
  route(/^\/settings\/tiles$/, async (ctx) => {
    const t = await get("/api/settings/tiles");
    let shownIds = (t.shown || []).slice();
    const list = el("div", { class: "rows" });
    const draw = (focusId) => {
      put(list, ...t.catalog.map((c) => {
        const on = shownIds.includes(c.id);
        const r = row({ name: c.label, sub: TILE_DRAW[c.id] ? c.text : `${c.text} (classic page only for now)`, sw: on, key: c.id,
          onClick: async () => {
            const next = on ? shownIds.filter((x) => x !== c.id) : shownIds.length >= 10 ? null : [...shownIds, c.id];
            if (!next) { toast("Up to ten tiles: switch one off first."); return; }
            try { shownIds = (await post("/api/settings/tiles", { shown: next })).shown || next; draw(c.id); }
            catch (err) { toast(err.message, "error"); }
          } });
        return r;
      }), row({ name: "Back to the standard tiles", icon: "rescan", id: "tiles-reset", onClick: async () => {
        try { shownIds = (await post("/api/settings/tiles", { reset: true })).shown || []; draw("tiles-reset"); }
        catch (err) { toast(err.message, "error"); }
      } }));
      if (focusId) focusEl(list.querySelector(`[data-key="${focusId}"]`) || $(focusId));
    };
    draw();
    return { title: "System page tiles", crumbs: [["Settings", "#/settings"]], back: "#/settings",
      body: el("div", { class: "screen tight" }, el("p", { class: "note", text: "The numbers each system shows, up to ten." }), list),
      hints: [["A", "Switch"], ["B", "Back"]] };
  });

  // ------------------------------------------------------------------ more of the kit: switches, choices, sheets
  /** A row with a switch. o.save(next) does the change (it may return the value that took); a failure puts it back. */
  function switchRow(o) {
    let on = !!o.on;
    const r = row({ name: o.name, sub: o.sub, sw: on, key: o.key, tag: o.tag, disabled: o.disabled, onClick: async () => {
      try {
        const got = await o.save(!on);
        on = got === undefined ? !on : !!got;
        r.querySelector(".switch").classList.toggle("on", on);
        r.setAttribute("aria-checked", String(on));
      } catch (err) { toast(err.message, "error"); }
    } });
    return r;
  }

  /** A row whose value opens a list picker: o.options [{label, value, sub}], o.current, o.save(value) */
  function choiceRow(o) {
    const label = (v) => (o.options.find((x) => x.value === v) || {}).label || o.empty || "Choose";
    let cur = o.current;
    const r = row({ name: o.name, sub: o.sub, value: label(cur), arrow: true, key: o.key, tag: o.tag, onClick: async () => {
      const v = await picker(o.title || o.name, o.options.map((x) => ({ ...x, current: x.value === cur })));
      if (v === null || v === cur) return;
      try {
        const got = await o.save(v);
        cur = got === undefined ? v : got;
        r.querySelector(".row-value").textContent = `${label(cur)} ›`;
      } catch (err) { toast(err.message, "error"); }
    } });
    return r;
  }

  /** A full-height panel from the right with any content: resolves when it closes. */
  function sheet(title, nodes, hints = [["A", "Select"], ["B", "Close"]], first = null) {
    return new Promise((resolve) => {
      const box = el("section", { class: "sheet", role: "dialog", "aria-label": title }, el("h2", { text: title }), ...nodes);
      const o = openOverlay(el("div", { class: "overlay side" }, box), () => resolve(null), hints);
      o.close = (v) => { closeOverlay(o, false); resolve(v); };
      if (first) setTimeout(() => focusEl(first));
      box.closeSheet = o.close;
    });
  }

  /** Tick several of a list (A ticks, Done keeps them): resolves to the ticked values in their order, or null. */
  function multiPicker(title, options, chosen) {
    return new Promise((resolve) => {
      const picked = chosen.slice();
      let o = null;
      const rows = options.map((opt) => {
        const r = row({ name: opt.label, sub: opt.sub, sw: picked.includes(opt.value), key: opt.value, onClick: () => {
          const i = picked.indexOf(opt.value);
          if (i >= 0) picked.splice(i, 1); else picked.push(opt.value);
          r.querySelector(".switch").classList.toggle("on", i < 0);
        } });
        return r;
      });
      const done = el("button", { class: "btn primary", type: "button", text: "Done", style: "min-width:0", on: { click: () => { closeOverlay(o, false); resolve(picked); } } });
      const box = el("section", { class: "sheet", role: "dialog", "aria-label": title }, el("h2", { text: title }), done, el("div", { class: "rows" }, rows));
      o = openOverlay(el("div", { class: "overlay side" }, box), () => resolve(null), [["A", "Tick"], ["B", "Cancel"], ["Start", "Done"]]);
      o.onPress = (name) => { if (name === "Start") { done.click(); return true; } return false; };
      setTimeout(() => focusEl(rows.find((r) => r.getAttribute("aria-checked") === "true") || rows[0]));
    });
  }

  /** Put a list in order: A picks a row up, up / down move it, A puts it down (B puts it back). With a mouse, each row's arrows.
   *  ``add()`` (optional) returns one more item. Resolves to the new order, or null. */
  function orderSheet(title, items, add) {
    return new Promise((resolve) => {
      let list = items.slice(), lifted = -1, before = null, o = null;
      const host = el("div", { class: "rows" });
      const draw = (focusIndex) => {
        put(host, ...list.map((it, i) => el("div", { class: "order-row" },
          el("button", { class: `row${i === lifted ? " lifted" : ""}`, type: "button", "data-i": String(i), on: { click: () => toggleLift(i) } },
            el("span", { class: "order-no", text: String(i + 1) }), el("span", { class: "row-name", text: it.label }),
            i === lifted ? el("span", { class: "row-value", text: "Moving" }) : null),
          el("button", { class: "order-btn", type: "button", "aria-label": `Move ${it.label} up`, tabindex: "-1", on: { click: () => shift(i, -1) } }, "↑"),
          el("button", { class: "order-btn", type: "button", "aria-label": `Move ${it.label} down`, tabindex: "-1", on: { click: () => shift(i, 1) } }, "↓"),
          el("button", { class: "order-btn", type: "button", "aria-label": `Remove ${it.label}`, tabindex: "-1", on: { click: () => { list.splice(i, 1); lifted = -1; draw(Math.min(i, list.length - 1)); } } }, "×"))));
        const target = host.querySelector(`[data-i="${focusIndex}"]`);
        if (target) focusEl(target);
      };
      const shift = (i, d) => {
        const j = i + d;
        if (j < 0 || j >= list.length) return;
        [list[i], list[j]] = [list[j], list[i]];
        if (lifted === i) lifted = j;
        draw(j);
      };
      const setHints = () => {
        o.hints = lifted >= 0 ? [["dpad", "Move it"], ["A", "Put down"], ["B", "Put it back"]] : [["A", "Pick up"], ["B", "Cancel"], ["Start", "Done"]];
        stripKey = "";
        renderStrip();
      };
      const toggleLift = (i) => {
        if (lifted === i) { lifted = -1; before = null; } else { lifted = i; before = list.slice(); }
        draw(i);
        setHints();
      };
      const done = el("button", { class: "btn primary", type: "button", text: "Done", style: "min-width:0", on: { click: () => { closeOverlay(o, false); resolve(list); } } });
      const addBtn = add ? el("button", { class: "row", type: "button", on: { click: async () => {
        const it = await add(list);
        if (it && !list.some((x) => x.value === it.value)) { list.push(it); draw(list.length - 1); }
      } } }, el("span", { class: "order-no", text: "+" }), el("span", { class: "row-name", text: "Add" })) : null;
      const box = el("section", { class: "sheet", role: "dialog", "aria-label": title }, el("h2", { text: title }), done, host, addBtn);
      o = openOverlay(el("div", { class: "overlay side" }, box), () => resolve(null), [["A", "Pick up"], ["B", "Cancel"], ["Start", "Done"]]);
      o.onPress = (name) => {
        if (name === "Start") { done.click(); return true; }
        if (lifted < 0) return false;
        if (name === "up" || name === "down") { shift(lifted, name === "up" ? -1 : 1); return true; }
        if (name === "B") {                        // put it back where it was
          const was = before ? before.indexOf(list[lifted]) : lifted;
          list = before || list;
          lifted = -1;
          before = null;
          draw(Math.max(0, was));
          setHints();
          return true;
        }
        return false;
      };
      draw(-1);
      setTimeout(() => draw(0), 0);              // (after the overlay's own first focus)
    });
  }

  /** A job started by ``start()`` followed on its own screen, then ``after(job)``. */
  async function runJob(ctx, head, title, start, after) {
    try { await start(); } catch (err) { toast(err.message, "error", 8000); return; }
    const view = progressView(title);
    ctx.show({ ...head, body: el("div", { class: "screen" }, view.node, el("p", { class: "note", text: "You can go back and do something else: it carries on, and the top of the screen shows it." })),
      hints: [["B", "Back (it carries on)"]] });
    const job = await followJob((j) => { if (ctx.alive()) { Activity.watching = j.id; view.update(j); } }, ctx.alive);
    if (!ctx.alive()) { Activity.finished(job); return; }       // (left while it ran: a toast says how it went)
    if (job.status === "error") toast(job.error || "It did not finish", "error", 8000);
    after(job);
  }

  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); toast("Copied"); } catch (_) { toast(text, "", 8000); }
  }

  // ------------------------------------------------------------------ Files
  const FILE_VIEWS = [
    { key: "library", label: "Library", side: "library", show: "have", count: (f) => f.show && f.show.have },
    { key: "excluded", label: "Excluded", side: "library", show: "excluded", count: (f) => f.show && f.show.excluded },
    { key: "missing", label: "Missing", side: "library", show: "missing", count: (f) => f.show && f.show.missing },
    { key: "archive", label: "Archive", side: "archive", show: "", count: (_f, sides) => sides && sides.archive },
  ];
  const STATE_TEXT = { moved: "Moved", renamed: "Renamed", new: "New", converted: "Converted", deleted: "Deleted", incoming: "Comes in",
    copy: "Copied", saves: "Saves", existing: "Already right", ignored: "Left alone", missing: "Missing", saveonly: "Saves only" };
  const STATE_DOT = { moved: "#4f9cff", renamed: "#4f9cff", new: "#3ecf8e", converted: "#3ecf8e", deleted: "#ff6b6b", incoming: "#3ecf8e",
    copy: "#4f9cff", saves: "#a98bff", existing: "#6b7686", ignored: "#6b7686", missing: "#f5c84a" };
  const CAT_TEXT = { kept: "Kept", excluded: "Excluded by your rules", superseded: "Older version replaced", incomplete: "Incomplete set",
    duplicate: "Duplicate", bios: "BIOS or firmware", saves: "Save files", unmatched: "Not in the database", playlist: "Playlist", missing: "Missing" };
  const fileFilters = {};          // per system: {state, region, language, machine, install, upgrade, rated, hide_saved}

  route(/^\/system\/([^/]+)\/files$/, async (ctx, slug) => {
    await Promise.all([loadPlatforms(), loadStatus()]);
    const p = bySlug(slug);
    if (!p) { location.replace("#/"); return null; }
    try { await post("/api/scan/select", { platform: p.name }); } catch (_) { /* the tree says when there is no scan */ }
    const f = fileFilters[slug] = fileFilters[slug] || {};
    let view = FILE_VIEWS.find((v) => v.key === ctx.params.view) || FILE_VIEWS[0];
    let path = "", facets = null, sides = null, page = null, query = "", searching = false;
    const ticked = new Set();
    let upgradeWaits = 0, archiveTidy = 0;
    const views = el("nav", { class: "views", "data-section": "views", "aria-label": "Views" });
    const bar = el("nav", { class: "filter-bar hidden", "data-section": "filters", "aria-label": "Filters" });      // (the choices of the filters, on the page itself)
    const list = el("div", { class: "rows", "data-section": "list" });
    const panel = el("aside", { class: "panel", "data-section": "panel" });
    const where = el("div", { class: "crumb-path" });
    const searchBox = el("input", { type: "search", class: "search hidden", id: "files-q", placeholder: "Search games, files or folders", "aria-label": "Search games, files or folders", autocomplete: "off", spellcheck: "false" });
    const head = { title: "Files", crumbs: [[p.name, `#/system/${slug}`]] };
    const params = (extra = {}) => qs({ platform: p.name, side: view.side, show: view.show, ...f, ...extra });

    const drawViews = () => put(views, el("span", { class: "glyph" }, "LB"), ...FILE_VIEWS.map((v) => {
      const n = facets ? v.count(facets, sides) : null;
      return el("button", { class: `chip${v === view ? " on" : ""}`, type: "button", tabindex: "-1", "data-view": v.key, on: { click: () => setView(v) } },
        n === null || n === undefined ? v.label : `${v.label} ${fmt(n)}`);
    }), el("span", { class: "glyph" }, "RB"), Object.keys(f).some((k) => f[k]) ? el("span", { class: "tag warn", text: "Filtered" }) : null);
    async function pickChip(key, title, entries, any) {
      const v = await picker(title, [{ label: any, value: "" }, ...entries.map(([k, n, label]) => ({ label: label || k, sub: `${fmt(n)}`, value: k }))]
        .map((o) => ({ ...o, current: (f[key] || "") === o.value })));
      if (v === null) return;
      f[key] = v;
      facets = null;
      load();
    }
    const drawFilters = () => {
      const fc = facets || {};
      const ent = (obj, label) => Object.entries(obj || {}).sort((a, b) => b[1] - a[1]).map(([k, n]) => [k, n, label || k]);
      const defs = [["machine", "Platform", "Any platform", ent(fc.machine)], ["language", "Language", "Any language", ent(fc.language)],
        ["region", "Region", "Any region", ent(fc.region)]];
      const toggles = [["upgrade", "Upgrade available", fc.upgrade], ["install", "WHDLoad", fc.install]];      // (on or off: nothing to choose)
      const chips = defs.filter(([key, , , entries]) => entries.length || f[key]).map(([key, title, any, entries]) =>
        el("button", { class: `chip filter${f[key] ? " on" : ""}`, type: "button", "data-filter": key, on: { click: () => pickChip(key, title, entries, any) } },
          f[key] ? `${title}: ${f[key]}` : `${title} ▾`));
      for (const [key, title, facet] of toggles) {
        if (!(facet && Object.keys(facet).length) && !f[key]) continue;
        chips.push(el("button", { class: `chip filter${f[key] ? " on" : ""}`, type: "button", "data-filter": key, "aria-pressed": f[key] ? "true" : "false",
          on: { click: () => { f[key] = f[key] ? "" : "1"; facets = null; load(); } } }, title));
      }
      if (Object.keys(f).some((k) => f[k])) chips.push(el("button", { class: "chip filter", type: "button", "data-filter": "clear", on: { click: () => { for (const k of Object.keys(f)) delete f[k]; facets = null; load(); } } }, "Clear filters"));
      bar.classList.toggle("hidden", !chips.length);
      put(bar, ...chips);
    };
    const setView = (v) => { view = v; path = ""; ticked.clear(); load(); };
    const step = (d) => setView(FILE_VIEWS[(FILE_VIEWS.indexOf(view) + d + FILE_VIEWS.length) % FILE_VIEWS.length]);

    const companion = (file) => /\.lha$/i.test(file.name) ? "WHDLoad" : /\.m3u$/i.test(file.name) ? "Playlist" : "";        // (what plays a game: beside its disks)
    const nameOf = (file) => companion(file) ? file.name : (file.row && (file.row.release || file.row.title)) || file.name.replace(/\.[^.]+$/, "");
    const fullPath = (name) => (path ? `${path}/${name}` : name);
    function describe(file) {
      const r = file.row || {};
      const facts = [];
      const add = (k, v) => { if (v) facts.push(el("dt", { text: k }), el("dd", { text: v })); };
      if (view.side === "archive") {
        add("In the archive", fullPath(file.name));
      } else if (file.state === "missing") {
        add("Not in your folder", r.name || file.name);
      } else {
        add("Now", file.from || file.name);
        if (file.state !== "existing" && r.to) add("After the build", r.to);
      }
      add("Why", r.reason ? r.reason.replace(/^excluded: /, "Excluded: ") : CAT_TEXT[r.category] || "");
      if (r.year) add("Year", String(r.year));
      if (file.upgrade) facts.push(el("dt", { text: "Better version" }), el("dd", {}, file.upgrade_why ? el("span", { class: "tag t-upgrade", text: file.upgrade_why }) : "The rules prefer another version of this game, which you do not have"));
      const btns = [];
      if (r.game_ref) btns.push(el("button", { class: "btn", type: "button", id: "panel-choose", on: { click: () => gameMenu(file) } }, "Keep or exclude…"));
      if (r.cs_kind !== undefined && r.cs_kind !== null) btns.push(el("button", { class: "btn", type: "button", on: { click: () => details(file) } }, "More details"));
      if (view.side === "archive") btns.push(el("button", { class: "btn primary", type: "button", style: "min-width:0", on: { click: () => restore([fullPath(file.name)]) } }, "Restore"));
      put(panel, el("span", { class: "panel-title", text: nameOf(file) }),
        el("span", { class: "tag", text: STATE_TEXT[file.state] || CAT_TEXT[r.category] || file.state }),
        el("dl", { class: "facts" }, facts), ...btns);
    }
    // what kind of game a folder holds (Amiga): its machine, what plays it, the language and version of the release kept
    const LANG_NAMES = { En: "English", De: "German", Fr: "French", It: "Italian", Es: "Spanish", Nl: "Dutch", Sv: "Swedish", Da: "Danish", No: "Norwegian",
      Fi: "Finnish", Pt: "Portuguese", Pl: "Polish", Ru: "Russian", Ja: "Japanese" };
    const factTags = (x) => !x ? [] : [x.platform, x.install, (x.languages || []).join(" "), x.version].filter(Boolean);   // (install: only "WHDLoad")
    function describeDir(d) {
      const states = Object.entries(d.states || {}).map(([k, n]) => `${fmt(n)} ${(STATE_TEXT[k] || k).toLowerCase()}`).join(", ");
      const x = d.facts, facts = [];
      if (x) {
        const add = (k, v) => { if (v) facts.push(el("dt", { text: k }), el("dd", { text: v })); };
        add("Played from", `${x.install === "WHDLoad" ? "A WHDLoad archive (.lha)" : x.source === "disc" ? "The disc" : "The disks, through a playlist"}, from ${x.source.toLowerCase()}`);
        add("Machine", [x.platform, (x.languages || []).map((c) => LANG_NAMES[c] || c).join(", "), x.version].filter(Boolean).join(" · "));
      }
      put(panel, el("span", { class: "panel-title", text: d.name }), el("p", { class: "lead", text: `${fmt(d.files)} file${d.files === 1 ? "" : "s"}` }),
        states ? el("p", { class: "note", text: states }) : null, facts.length ? el("dl", { class: "facts" }, facts) : null);
    }
    function tidyArchive() {
      runJob(ctx, head, "Tidying the archive…", () => post("/api/archive/tidy", { platform: p.name }), (job) => {
        const r = job.result || {};
        toast(`Tidied ${fmt(r.moved || 0)} file${r.moved === 1 ? "" : "s"}${r.failed_count ? `; ${fmt(r.failed_count)} failed` : ""}`, r.failed_count ? "error" : "", 8000);
        ctx.redraw();
      });
    }
    function describeTicked() {
      put(panel, el("span", { class: "panel-title", text: ticked.size ? `${fmt(ticked.size)} ticked` : "Nothing ticked" }),
        archiveTidy ? el("button", { class: "btn", type: "button", id: "panel-tidy", style: "min-width:0", on: { click: tidyArchive } }, `Tidy the archive (${fmt(archiveTidy)} files)`) : null,
        archiveTidy ? el("p", { class: "note", text: "Games an earlier build archived by their database folder move into a folder per game, like the library. Nothing is deleted." }) : null,
        el("button", { class: "btn primary", type: "button", style: "min-width:0", disabled: !ticked.size, on: { click: () => restore([...ticked]) } }, `Restore ${ticked.size || ""}`.trim()),
        el("p", { class: "note", text: "They go back into the library under their proper names. The archive keeps its copy." }));
    }

    function fileRow(file, sub) {
      const r = file.row || {};
      const tick = view.side === "archive" ? el("span", { class: `tick${ticked.has(fullPath(file.name)) ? " on" : ""}`, "aria-hidden": "true" }) : null;
      const b = el("button", { class: `row${r.saved_keep || file.saved_keep ? " dim" : ""}`, type: "button", "data-key": fullPath(file.name),
        on: { click: () => (view.side === "archive" ? describe(file) : r.game_ref ? gameMenu(file) : describe(file)), focus: () => describe(file) } },
        tick || el("span", { class: "dot", style: `background:${STATE_DOT[file.state] || "#6b7686"}` }),
        el("span", { class: "row-name" }, nameOf(file), sub ? el("small", { text: sub }) : null),
        companion(file) || file.upgrade ? el("span", { class: "row-tags" }, companion(file) ? el("span", { class: `tag t-${companion(file).toLowerCase()}`, text: companion(file) }) : null, file.upgrade ? upgradeTag(file.upgrade_why) : null) : null,
        el("span", { class: "row-value", text: STATE_TEXT[file.state] || "" }));
      b._file = file;
      return b;
    }
    const upgradeTag = (why) => el("span", { class: "tag t-upgrade", title: why ? `A better version of this game is available (${why})` : "A better version of this game is available", "aria-label": why ? `Upgrade available: ${why}` : "Upgrade available", text: why ? `↑ ${why}` : "↑" });
    function dirRow(d) {
      return el("button", { class: "row", type: "button", "data-key": `dir:${d.name}`, on: { click: () => { path = fullPath(d.name); load(); }, focus: () => describeDir(d) } },
        icon("folder", "row-icon"), el("span", { class: "row-name", text: d.name }),
        d.facts || d.upgrades ? el("span", { class: "row-tags" }, ...(d.facts ? factTags(d.facts) : []).map((t) => el("span", { class: `tag t-${String(t).toLowerCase().replace(/[^a-z0-9]+/g, "-")}`, text: t })), d.upgrades ? upgradeTag() : null) : null,
        el("span", { class: "row-value", text: `${fmt(d.files)} ›` }));
    }

    async function load(focusKey = "") {
      searching = false;
      searchBox.classList.add("hidden");
      put(list, skeletonRows(6));
      Activity.set("files", "Working out the files");
      try {
        page = await get(`/api/tree?${params({ path })}`);
      } catch (err) { Activity.clear("files"); put(list, message(err.message, "bad")); return; }
      Activity.clear("files");
      if (!ctx.alive()) return;
      if (!page.ready) { put(list, message("Scan this system first: the files come from its last scan.", "warn")); put(panel, ); return; }
      // (the counts of the library views come from the library side: the archive side's facets count the archive's files)
      if (page.facets && view.side === "library") facets = page.facets;
      if (page.sides) sides = { ...(sides || {}), [view.side]: page.sides[view.side] };
      if (view.side === "archive" && page.facets !== undefined) archiveTidy = page.archive_tidy || 0;
      if (page.upgrades_pending && upgradeWaits++ < 12) setTimeout(() => { if (ctx.alive() && !topOverlay()) { facets = null; load(); } }, 5000);      // (the better versions are still being worked out)
      if (!facets) {
        try {
          const top = await get(`/api/tree?${qs({ platform: p.name, side: "library", show: "have", ...f })}`);
          facets = top.facets;
          sides = { ...(top.sides || {}), ...(sides || {}) };
        } catch (_) { /* no counts */ }
      }
      if (view.side === "library" && (!sides || sides.archive === undefined)) {
        try { const a = await get(`/api/tree?${qs({ platform: p.name, side: "archive", ...f })}`); sides = { ...(sides || {}), archive: (a.sides || {}).archive }; }
        catch (_) { /* no count */ }
      }
      drawViews();
      drawFilters();
      put(where, el("b", { text: path || (view.side === "archive" ? archiveText(page.archive_root || archiveFor(p)) : baseName(page.root || p.folder)) }));
      Screen.current.title = path ? baseName(path) : "Files";
      $("head-title").textContent = Screen.current.title;
      $("head-aside").textContent = `${fmt(page.files_below || 0)} file${page.files_below === 1 ? "" : "s"}`;
      if (view.side === "archive" && page.none) {
        put(list, message("No archive is set up. Choose one in Settings › Archive.", "warn"));
        put(panel, el("a", { class: "btn", href: "#/settings/archive", text: "Archive settings" }));
        return;
      }
      const rows = [...page.dirs.map(dirRow), ...page.files.map((x) => fileRow(x))];
      const more = (page.total_files || 0) > (page.offset || 0) + page.files.length;
      if (more) rows.push(row({ name: "Show more", icon: "list", id: "files-more", onClick: () => loadMore() }));
      put(list, ...(rows.length ? rows : [row({ name: "Nothing here in this view", static: true })]));
      if (view.side === "archive") describeTicked();
      else put(panel, el("p", { class: "note", text: "The panel describes what is focused." }));
      updateHints();
      const target = (focusKey && list.querySelector(`[data-key="${CSS.escape(focusKey)}"]`)) || list.querySelector("button");
      if (target && mode !== "mouse") focusEl(target);
    }
    async function loadMore() {
      const offset = (page.offset || 0) + page.files.length;
      try {
        const next = await get(`/api/tree?${params({ path, offset })}`);
        page = { ...page, files: page.files.concat(next.files), offset: 0 };
        $("files-more").remove();
        const firstNew = next.files.map((x) => fileRow(x));
        list.append(...firstNew);
        if ((next.total_files || 0) > offset + next.files.length) list.append(row({ name: "Show more", icon: "list", id: "files-more", onClick: () => loadMore() }));
        if (firstNew[0]) focusEl(firstNew[0]);
      } catch (err) { toast(err.message, "error"); }
    }

    // search: every folder of the view, its matching files as one list
    let searchSerial = 0;
    async function runSearch() {
      const q = searchBox.value.trim();
      const mine = ++searchSerial;
      query = q;
      if (!q) { put(list, row({ name: "Type to search", static: true })); return; }
      const found = [];
      const queue = [""];
      let asked = 0;
      while (queue.length && found.length < 200 && asked < 40) {
        const at = queue.shift();
        asked++;
        let doc;
        try { doc = await get(`/api/tree?${params({ path: at, q })}`); } catch (_) { break; }
        if (mine !== searchSerial || !ctx.alive()) return;
        for (const x of doc.files) found.push({ file: x, at });
        for (const d of doc.dirs) if (d.files) queue.push(at ? `${at}/${d.name}` : d.name);
      }
      put(list, el("div", { class: "note", text: `${fmt(found.length)}${found.length >= 200 ? "+" : ""} found` }),
        ...found.map(({ file, at }) => { const r = fileRow(file, at || "Top folder"); r.addEventListener("focus", () => { path = at; }); return r; }));
    }
    const searchNow = (() => { let t; return () => { clearTimeout(t); t = setTimeout(runSearch, 250); }; })();
    searchBox.addEventListener("input", searchNow);
    function openSearch() {
      searching = true;
      searchBox.classList.remove("hidden");
      searchBox.value = query;
      focusEl(searchBox);
      runSearch();
      updateHints();
    }

    async function gameMenu(file) {
      const r = file.row || {};
      const opts = [
        { label: "Always keep", sub: "Whatever the rules say", value: "keep" },
        { label: "Always exclude", sub: "Whatever the rules say", value: "exclude" },
        { label: "Back to the rules", sub: "Remove your choice for this game", value: "clear" },
        { label: "More details", sub: "Database entry, checksums", value: "details" },
      ];
      describe(file);
      const v = await picker(nameOf(file), opts);
      if (!v) return;
      if (v === "details") { details(file); return; }
      try {
        await post("/api/library/override", { platform: p.name, action: v, games: [r.game_ref] });
        toast(v === "clear" ? "Back under the rules" : v === "keep" ? "Always kept from now on" : "Always excluded from now on");
        facets = null;
        load(fullPath(file.name));
      } catch (err) { toast(err.message, "error"); }
    }

    async function details(file) {
      const r = file.row || {};
      const facts = el("dl", { class: "facts" });
      const add = (k, v) => { if (v) facts.append(el("dt", { text: k }), el("dd", { text: v })); };
      add("Database entry", r.release || r.name || (r.game_ref && r.game_ref.release));
      add("Database", r.dat || (r.game_ref && r.game_ref.dat));
      add("Year · rating", [r.year, r.rating !== null && r.rating !== undefined ? `${r.rating}/10` : "not rated"].filter(Boolean).join(" · "));
      if (r.code) add("Game code", r.code);
      const sums = el("div", { class: "rows" }, el("div", { class: "note", text: "Loading checksums…" }));
      const box = sheet(nameOf(file), [facts, el("h3", { class: "section-title", text: "Checksums" }), sums], [["A", "Copy"], ["B", "Close"]]);
      try {
        const cs = await get(`/api/scan/checksums?${qs({ kind: r.cs_kind, id: r.cs_id })}`);
        const lines = [];
        const one = (label, value) => { if (value) lines.push(el("button", { class: "row", type: "button", on: { click: () => copyText(value) } },
          el("span", { class: "row-name" }, label, el("small", { class: "mono", text: value })), el("span", { class: "row-value", text: "Copy" }))); };
        for (const d of cs.dat || cs.tracks || []) {
          const h = d.dat || d;
          one(`${d.name || `Track ${d.number || ""}`} · CRC32`, h.crc32 || h.crc);
          one("MD5", h.md5);
          one("SHA-1", h.sha1);
        }
        put(sums, ...(lines.length ? lines : [el("div", { class: "note", text: "No checksums for this one." })]));
        if (lines[0] && topOverlay()) focusEl(lines[0]);
      } catch (err) { put(sums, message(err.message, "bad")); }
      await box;
    }

    async function restore(paths) {
      if (!paths.length) return;
      runJob(ctx, head, "Restoring…", () => post("/api/archive/restore", { platform: p.name, paths }), (job) => {
        const r = job.result || {};
        toast(`Restored ${fmt(r.restored || 0)} file${r.restored === 1 ? "" : "s"}${(r.failed || []).length ? `; ${fmt(r.failed.length)} failed` : ""}`, (r.failed || []).length ? "error" : "", 8000);
        ctx.redraw();
      });
    }

    async function filters() {
      const facet = facets || {};
      const pickFrom = (key, title, entries, any) => async () => {
        const v = await picker(title, [{ label: any, value: "" }, ...entries.map(([k, n, label]) => ({ label: label || k, sub: `${fmt(n)}`, value: k }))].map((o) => ({ ...o, current: (f[key] || "") === o.value })));
        if (v === null) return;
        f[key] = v;
        facets = null;
        if (topOverlay()) topOverlay().close(true);
      };
      const ent = (obj) => Object.entries(obj || {}).sort((a, b) => b[1] - a[1]);
      const rows = [
        row({ name: "What the build does", value: f.state ? STATE_TEXT[f.state] || f.state : "Anything", arrow: true, onClick: pickFrom("state", "What the build does", ent(facet.state).map(([k, n]) => [k, n, STATE_TEXT[k] || k]), "Anything") }),
        (facet.machine && Object.keys(facet.machine).length) || f.machine ? row({ name: "Platform", value: f.machine || "Any", arrow: true, onClick: pickFrom("machine", "Platform", ent(facet.machine), "Any") }) : null,
        (facet.install && Object.keys(facet.install).length) || f.install ? switchRow({ name: "WHDLoad games only", on: !!f.install, save: async (on) => { f.install = on ? "1" : ""; facets = null; return on; } }) : null,
        (facet.upgrade && Object.keys(facet.upgrade).length) || f.upgrade ? switchRow({ name: "Upgrade available only", on: !!f.upgrade, save: async (on) => { f.upgrade = on ? "1" : ""; facets = null; return on; } }) : null,
        row({ name: "Region", value: f.region || "Any", arrow: true, onClick: pickFrom("region", "Region", ent(facet.region), "Any") }),
        row({ name: "Language", value: f.language || "Any", arrow: true, onClick: pickFrom("language", "Language", ent(facet.language), "Any") }),
        row({ name: "Rating", value: f.rated === "1" ? "Rated" : f.rated === "0" ? "Unrated" : "Rated or not", arrow: true,
          onClick: pickFrom("rated", "Rating", [["1", (facet.rated || {})["1"] || 0, "Rated"], ["0", (facet.rated || {})["0"] || 0, "Unrated"]], "Rated or not") }),
        (facet.show || {}).kept_saves ? switchRow({ name: "Games kept for their saves", sub: "Dimmed, never counted", on: !f.hide_saved,
          save: async (on) => { f.hide_saved = on ? "" : "1"; facets = null; return on; } }) : null,
        row({ name: "Clear filters", icon: "rescan", onClick: () => { for (const k of Object.keys(f)) delete f[k]; facets = null; if (topOverlay()) topOverlay().close(true); } }),
      ].filter(Boolean);
      await sheet("Filter", [el("div", { class: "rows" }, rows)], [["A", "Choose"], ["B", "Done"]], rows[0]);
      load();
    }

    function updateHints() {
      const h = [["A", view.side === "archive" ? "Details" : "Open"], ["B", path ? "Up a folder" : "Back"], ["LB RB", "Views"], ["Y", "Search"], ["X", "Filter"]];
      if (view.side === "archive") h.splice(1, 0, ["Start", "Restore ticked"]);
      Screen.current.hints = h;
      stripKey = "";
      renderStrip();
    }
    const tick = () => {
      const e = document.activeElement;
      if (view.side !== "archive" || !e || !e._file) return;
      const key = fullPath(e._file.name);
      if (ticked.has(key)) ticked.delete(key); else ticked.add(key);
      e.querySelector(".tick").classList.toggle("on", ticked.has(key));
      describeTicked();
    };

    ctx.show({ ...head, focus: "#main [data-section=list] .row",         // (the list first: the filter chips above it are one step up)
      back: () => { if (searching) { load(); return; } if (path) { const up = path.split("/").slice(0, -1).join("/"); const from = `dir:${baseName(path)}`; path = up; load(from); } else location.hash = `#/system/${slug}`; },
      keys: { LB: () => step(-1), RB: () => step(1), Y: () => (view.side === "archive" ? tick() : openSearch()), X: filters,
        Start: () => { if (view.side === "archive" && ticked.size) restore([...ticked]); } },
      body: el("div", { class: "screen tight" }, views, bar, where, searchBox, el("div", { class: "split" }, el("div", { class: "grow" }, list), panel)),
      hints: [] });
    await load();
    return null;
  });

  // ------------------------------------------------------------------ a system: needs a look
  route(/^\/system\/([^/]+)\/look$/, async (ctx, slug) => {
    await Promise.all([loadPlatforms(), loadStatus()]);
    const p = bySlug(slug);
    if (!p) { location.replace("#/"); return null; }
    let sel = { selected: false };
    try { sel = await post("/api/scan/select", { platform: p.name }); } catch (_) { /* below */ }
    const head = { title: "Needs a look", crumbs: [[p.name, `#/system/${slug}`]], back: `#/system/${slug}` };
    if (!sel.selected) return { ...head, body: el("div", { class: "screen" }, message("Scan this system first.", "warn")), hints: [["B", "Back"]] };
    // (the build preview is usually worked out already: the placeholder rows only when the answer takes a moment)
    const waiting = setTimeout(() => {
      if (!ctx.alive()) return;
      ctx.show({ ...head, body: el("div", { class: "screen" }, skeletonRows(5)), hints: [["B", "Back"]] });
      Activity.set("screen", "Looking for problems");
    }, 150);
    const upgrades = await get(`/api/library/upgrades?${qs({ platform: p.name })}`).catch(() => ({ items: [] }));
    clearTimeout(waiting);
    Activity.clear("screen");
    if (!ctx.alive()) return null;
    // (only versions you could improve: what your rules exclude on purpose is in Files › Excluded, where a game can still be kept, and an
    // incomplete set is archived by the build)
    const items = upgrades.items || [];
    const list = el("div", { class: "rows", "data-section": "list" });
    const panel = el("aside", { class: "panel", "data-section": "panel" });
    const describe = (it) => put(panel, el("span", { class: "panel-title", text: it.title || it.name }),
      el("p", { class: "note", text: (it.better || []).length === 1 ? "Your rules prefer a version you do not have:" : "Your rules prefer versions you do not have:" }),
      ...(it.better || []).map((b) => el("p", { class: "note", text: (b.files || []).length > 1 ? `${b.name} (${b.files.length} disks)` : b.name })),
      it.why ? el("p", { class: "note" }, "Why: ", el("span", { class: "tag t-upgrade", text: it.why })) : null);
    const rows = items.map((it) => {
      const n = (it.better || []).length;
      const r = row({ name: it.title || it.name, sub: `${n} better version${n === 1 ? "" : "s"}`, onClick: () => describe(it) });
      r.addEventListener("focus", () => describe(it));
      return r;
    });
    put(list, ...(rows.length ? rows : [row({ name: "Nothing to look at here", static: true })]));
    put(panel, el("p", { class: "note", text: rows.length ? "Each game's details appear here." : "What your rules exclude is in Files › Excluded." }));
    if (mode !== "mouse" && rows[0]) focusEl(rows[0]);
    return { ...head,
      body: el("div", { class: "screen tight" }, el("div", { class: "split" }, el("div", { class: "grow" }, list), panel)),
      hints: [["A", "Select"], ["B", "Back"]] };
  });

  // ------------------------------------------------------------------ a system: what "with your rules" is made of
  route(/^\/system\/([^/]+)\/target$/, async (ctx, slug) => {
    await loadPlatforms();
    const p = bySlug(slug);
    if (!p) { location.replace("#/"); return null; }
    try { await post("/api/scan/select", { platform: p.name }); } catch (_) { /* the totals say so */ }
    const head = { title: "Your library", crumbs: [[p.name, `#/system/${slug}`]], back: `#/system/${slug}` };
    let t = null, shown = false;
    for (let i = 0; i < 900 && ctx.alive(); i++) {
      try { t = await get(`/api/library/totals?${qs({ platform: p.name })}`); } catch (err) { return { ...head, body: el("div", { class: "screen" }, message(err.message, "bad")), hints: [["B", "Back"]] }; }
      if (!t.calculating) break;
      if (!shown) { shown = true; ctx.show({ ...head, body: el("div", { class: "screen tight" }, el("h2", { class: "big-title", text: "Your library" }), skeletonRows(4)), hints: [["B", "Back"]] }); }
      Activity.set("screen", `Working out the library · ${workText(t.working)}`);
      await sleep(1000);
    }
    if (!ctx.alive() || !t) return null;
    if (t.error || t.target_games === null) return { ...head, body: el("div", { class: "screen" }, message(t.error || "Not worked out yet.", "warn")), hints: [["B", "Back"]] };
    const x = t.explain;
    const list = (title, names, more) => async () => sheet(title, [el("div", { class: "rows" }, names.map((n) => row({ name: n, info: true })),
      more > names.length ? row({ name: `and ${fmt(more - names.length)} more`, static: true }) : null)], [["B", "Close"]]);
    const rows = [];
    if (x) {
      rows.push(row({ name: `${fmt(x.top)} best rated`, sub: `Your rule: the top ${fmt(x.top_n)}${x.top < x.top_n ? `; only ${fmt(x.top)} complete rated games pass your other rules` : ""}`, icon: "check", info: true }));
      if (x.tie_count) rows.push(row({ name: `+ ${fmt(x.tie_count)} tied with the ${fmt(x.top_n)}th`, sub: "The same rating, votes and title: they stay or go together", arrow: true, onClick: list("Tied with the last place", x.ties, x.tie_count) }));
      if (x.incomplete_count) rows.push(row({ name: `+ ${fmt(x.incomplete_count)} rated but incomplete in the database`, sub: "Never counted towards the top, kept when they rank high enough", arrow: true, onClick: list("Incomplete in the database", x.incomplete, x.incomplete_count) }));
      if (x.unrated) rows.push(row({ name: `+ ${fmt(x.unrated)} without a rating`, sub: "Kept by \u201cKeep unrated games\u201d", info: true }));
      if (x.not_ranked_count) rows.push(row({ name: `+ ${fmt(x.not_ranked_count)} not ranked at all`, arrow: true,
        sub: `No ratings for ${Object.keys(x.not_ranked_by_dat || {}).join(", ")}: the rating rule does not apply to them`, onClick: list("Not ranked", x.not_ranked, x.not_ranked_count) }));
    }
    const missing = t.missing_names || [];
    return { ...head, aside: p.name,
      body: el("div", { class: "screen tight" },
        el("h2", { class: "big-title", text: `${fmt(t.target_games)} game${t.target_games === 1 ? "" : "s"} with your rules` }),
        el("p", { class: "lead", text: t.have_games !== null ? `You have ${fmt(t.have_games)}; ${fmt(t.missing_games)} missing.` : "Scan the folder to see which you have." }),
        rows.length ? el("section", { class: "section" }, el("h2", { class: "section-title", text: "Made of" }), el("div", { class: "rows" }, rows)) : null,
        t.missing_games ? el("section", { class: "section" }, el("h2", { class: "section-title", text: "You don't have" }),
          el("div", { class: "rows" }, missing.map((n) => row({ name: n, info: true })),
            t.missing_games > missing.length ? row({ name: `and ${fmt(t.missing_games - missing.length)} more: Files › Missing`, href: `#/system/${slug}/files?view=missing`, arrow: true }) : null)) : null,
        t.target_system ? el("section", { class: "section" }, el("h2", { class: "section-title", text: "System disks" }),
          el("div", { class: "rows" },
            row({ name: `${fmt(t.target_system)} Workbench, Kickstart and firmware disk${t.target_system === 1 ? "" : "s"}`, info: true,
              sub: `Kept as your rules say, but not counted as games or as missing${t.have_games !== null ? `; you have ${fmt(t.have_system || 0)}` : ""}` }))) : null,
        t.target_addons ? el("section", { class: "section" }, el("h2", { class: "section-title", text: "Updates and DLC" }),
          el("div", { class: "rows" }, row({ name: `${fmt(t.target_addons)} update${t.target_addons === 1 ? "" : "s"} and DLC in the catalogue`, info: true,
            sub: `Kept as your rules say, but not counted as games${t.have_addons !== null && t.have_addons !== undefined ? `; you have ${fmt(t.have_addons)}` : ""}` }))) : null),
      hints: [["A", "Select"], ["B", "Back"], ["LT RT", "Page"]] };
  });

  // ------------------------------------------------------------------ a system: saves
  route(/^\/system\/([^/]+)\/saves$/, async (ctx, slug) => {
    await loadPlatforms();
    const p = bySlug(slug);
    if (!p) { location.replace("#/"); return null; }
    try { await post("/api/scan/select", { platform: p.name }); } catch (_) { /* below */ }
    const head = { title: "Saves", crumbs: [[p.name, `#/system/${slug}`]], back: `#/system/${slug}` };
    const views = [["", "All"], ["1", "Matched"], ["0", "No game"]];
    let have = "";
    const nav = el("nav", { class: "views" });
    const list = el("div", { class: "rows", "data-section": "list" });
    const panel = el("aside", { class: "panel", "data-section": "panel" });
    const describe = (i) => put(panel, el("span", { class: "panel-title", text: i.title }),
      el("dl", { class: "facts" }, el("dt", { text: "Save files" }), el("dd", { text: `${fmt(i.files)}` }),
        el("dt", { text: "From" }), el("dd", { text: i.source && i.source !== "retroarch" ? i.label : (i.cores || []).join(", ") || "-" }),
        i.name !== i.title ? [el("dt", { text: "Named" }), el("dd", { text: i.name })] : null));
    const load = async () => {
      put(nav, el("span", { class: "glyph" }, "LB"), ...views.map(([v, label]) => el("button", { class: `chip${v === have ? " on" : ""}`, type: "button", tabindex: "-1",
        on: { click: () => { have = v; load(); } } }, label)), el("span", { class: "glyph" }, "RB"));
      let data;
      try { data = await get(`/api/scan/results?${qs({ kind: "saves", offset: 0, limit: 300, have })}`); } catch (err) { put(list, message(err.message, "bad")); return; }
      if (!ctx.alive()) return;
      const rows = (data.items || []).map((i) => {
        const r = row({ name: i.title, sub: `${fmt(i.files)} file${i.files === 1 ? "" : "s"}`, tag: i.match === "rom" ? ["Follows the game", "ok"] : i.match === "dat" ? ["No ROM here", "warn"] : ["No game", ""], onClick: () => describe(i) });
        r.addEventListener("focus", () => describe(i));
        return r;
      });
      put(list, ...(rows.length ? rows : [row({ name: "No saves found for this system", static: true })]));
      if (mode !== "mouse" && rows[0]) focusEl(rows[0]);
    };
    const step = (d) => { const i = views.findIndex(([v]) => v === have); have = views[(i + d + views.length) % views.length][0]; load(); };
    ctx.show({ ...head, keys: { LB: () => step(-1), RB: () => step(1) },
      body: el("div", { class: "screen tight" }, nav, el("div", { class: "split" }, el("div", { class: "grow" }, list), panel)),
      hints: [["A", "Details"], ["B", "Back"], ["LB RB", "Views"]] });
    await load();
    return null;
  });

  // ------------------------------------------------------------------ a system: its settings
  route(/^\/system\/([^/]+)\/settings$/, async (ctx, slug) => {
    await Promise.all([loadPlatforms(), loadStatus()]);
    const p = bySlug(slug);
    if (!p) { location.replace("#/"); return null; }
    const e = S.status.library_export || {};
    const own = (S.status.archive && S.status.archive.overrides[p.name]) || "";
    const saveExport = async (patch) => {
      const cur = (await loadStatus()).library_export || {};
      await post("/api/library/export/settings", { ...cur, ...patch });
      await loadStatus();
    };
    const head = { title: "Settings", crumbs: [[p.name, `#/system/${slug}`]], back: `#/system/${slug}` };
    const rows = [
      row({ name: "Folder", icon: "folder", value: p.folder ? baseName(p.folder) : "Not chosen", arrow: true, href: `#/system/${slug}/folder` }),
      choiceRow({ name: "Where to build", sub: "Every system", key: "where",
        options: [{ label: "In this folder", value: "here", sub: "The files are moved and renamed" }, { label: "In another folder", value: "other", sub: e.dest || "Choose it next" }],
        current: e.enabled ? "other" : "here",
        save: async (v) => {
          if (v === "here") { await saveExport({ enabled: false }); return v; }
          location.hash = `#/system/${slug}/build-folder`;
          return undefined;
        } }),
      row({ name: "Archive folder", icon: "archive", value: own ? archiveText(own) : "Same as every system", arrow: true, href: `#/settings/archive/folder?system=${slug}` }),
      switchRow({ name: "Disk labels in playlists", on: Prefs.get(`labels:${p.name}`, !isGameFolder(p)), key: "labels", save: async (on) => { Prefs.set(`labels:${p.name}`, on); return on; } }),
      p.name === "Commodore Amiga" ? row({ name: "Save disks", static: true,
        sub: "Every game played from its disks gets a playlist with a blank save disk; a game built for WHDLoad is played from its .lha, with no playlist" }) : null,
      p.name === "Nintendo Switch" ? switchRow({ name: "Read every file in full when scanning", sub: "Slower; checks each part against its own checksum", key: "swverify",
        on: await get("/api/switch").then((x) => !!x.config.verify_scan).catch(() => false), save: async (on) => (await post("/api/switch/config", { verify_scan: on })).config.verify_scan }) : null,
      row({ name: "Recalculate checksums", icon: "rescan", sub: "Reads every file again", onClick: async () => {
        if (!await confirmDialog("Recalculate every checksum?", "Recalculate", "Cancel", "Every file is read again. This can take a while.")) return;
        runJob(ctx, head, "Scanning…", () => post("/api/scan", { path: p.folder, platform: p.name, force: true }), () => ctx.redraw());
      } }),
      row({ name: "Databases", icon: "db", sub: "Versions, folders and counts per database", arrow: true, href: `#/system/${slug}/databases` }),
    ].filter(Boolean);
    const layout = async () => {
      let plan = null;
      try { plan = await post("/api/library/plan", { ...buildOptions(p), limit: 1, summary: true }); } catch (err) { toast(err.message, "error"); return; }
      const lines = Object.entries(plan.by_dest || {}).sort((a, b) => b[1] - a[1]).map(([d, n]) => row({ name: d, value: `${fmt(n)}`, info: true, icon: "folder" }));
      await sheet("Folders after building", [el("div", { class: "rows" }, lines.length ? lines : [row({ name: "Nothing moves", static: true })])], [["B", "Close"]]);
    };
    return { ...head, keys: { X: layout },
      body: el("div", { class: "screen tight" }, el("div", { class: "rows" }, rows)),
      hints: [["A", "Select"], ["B", "Back"], ["X", "Folders after building"]] };
  });

  route(/^\/system\/([^/]+)\/build-folder$/, async (ctx, slug) => {
    await Promise.all([loadPlatforms(), loadStatus()]);
    const p = bySlug(slug);
    if (!p) { location.replace("#/"); return null; }
    const e = S.status.library_export || {};
    return folderScreen(ctx, {
      head: { title: "Build the library in", crumbs: [["Settings", `#/system/${slug}/settings`]], back: `#/system/${slug}/settings` },
      start: e.dest || "",
      choose: async (path) => {
        try {
          await post("/api/library/export/settings", { ...e, enabled: true, dest: path, mode: e.mode || "copy" });
          toast(`The library is built in ${path}`);
          location.hash = `#/system/${slug}/settings`;
        } catch (err) { toast(err.message, "error"); }
      },
    });
  });

  // ------------------------------------------------------------------ rules (every system, or one)
  function rulesScreen(ctx, p) {
    const scope = p ? { url: `/api/library/profile?${qs({ platform: p.name })}`, save: (body) => post("/api/library/profile", { platform: p.name, ...body }) }
      : { url: "/api/library/defaults", save: (body) => post("/api/library/defaults", body) };
    const list = el("div", { class: "rows" });
    let info = null;
    const overridden = (field) => !!info && (info.inherit.overridden.includes(field) || (field === "exclude" && info.inherit.overridden.includes("exclude_amiga")));
    const tagOf = (field) => (p ? (overridden(field) ? ["This system", "own"] : ["Every system", ""]) : null);
    const applies = (c) => !p || ((c.applies_to || []).includes(info.style) && (info.inherit.applicable.includes(c.field) || c.field === "keep_flags" || c.field === "exclude"));
    const save = async (body, key) => { info = await scope.save(body); draw(key); };
    const langLabel = (codes) => codes.map((c) => (info.available_languages.find((x) => x.code === c) || {}).name || c).join(", ") || "Any";
    const NUM = { min_rating: [null, 5, 6, 6.5, 7, 7.5, 8], top_n: [null, 25, 50, 100, 250, 500, 1000], min_votes: [1, 5, 10, 25, 50] };
    function draw(focusKey) {
      const prof = info.profile;
      const sections = [["Exclude", (c) => c.kind === "exclude"], ["Keep these variants", (c) => c.kind === "keep_flag"],
        ["Choose versions", (c) => !["exclude", "keep_flag"].includes(c.kind) && c.group !== "ratings"], ["Ratings", (c) => c.group === "ratings"]];
      const out = [];
      for (const [title, test] of sections) {
        const rows = info.catalog.filter((c) => test(c) && applies(c)).map((c) => ruleRow(c, prof)).filter(Boolean);
        if (rows.length) out.push(el("h2", { class: "section-title", text: title }), ...rows);
      }
      put(list, ...out);
      const target = focusKey && list.querySelector(`[data-key="${CSS.escape(focusKey)}"]`);
      if (target) focusEl(target);
    }
    function ruleRow(c, prof) {
      const key = c.id;
      const tag = tagOf(c.field);
      if (c.kind === "exclude" || c.kind === "keep_flag") {
        const field = c.field;
        const on = (prof[field] || []).includes(c.id);
        return row({ name: c.kind === "keep_flag" ? `Keep ${c.label.toLowerCase()}` : c.label, sub: c.description, sw: on, key, tag,
          onClick: () => save({ [field]: on ? prof[field].filter((x) => x !== c.id) : [...prof[field], c.id] }, key).catch((err) => toast(err.message, "error")) });
      }
      if (c.field === "languages") {
        return row({ name: c.label, sub: "First = preferred", value: langLabel(prof.languages), arrow: true, key, tag, onClick: async () => {
          const v = await multiPicker("Languages", info.available_languages.map((l) => ({ label: l.name, value: l.code })), prof.languages);
          if (v && v.length) save({ languages: v }, key).catch((err) => toast(err.message, "error"));
        } });
      }
      if (c.field === "region_priority") {
        return row({ name: "Region order", sub: "First wins", value: prof.region_priority.join(", "), arrow: true, key, tag, onClick: async () => {
          const v = await orderSheet("Region order", prof.region_priority.map((r) => ({ label: r, value: r })),
            async (cur) => { const r = await picker("Add a region", info.regions.filter((x) => !cur.some((y) => y.value === x)).map((x) => ({ label: x, value: x }))); return r ? { label: r, value: r } : null; });
          if (v) save({ region_priority: v.map((x) => x.value) }, key).catch((err) => toast(err.message, "error"));
        } });
      }
      if (c.kind === "number") {
        const values = NUM[c.field] || [c.default];
        const label = (v) => (v === null || v === undefined ? "Off" : `${v}${c.unit && c.unit.startsWith("/") ? c.unit : ` ${c.unit || ""}`}`.trim());
        return row({ name: c.label, sub: c.description, value: label(prof[c.field]), arrow: true, key, tag, onClick: async () => {
          const v = await picker(c.label, values.map((x) => ({ label: label(x), value: x === null ? "off" : String(x), current: x === prof[c.field] })));
          if (v !== null) save({ [c.field]: v === "off" ? null : Number(v) }, key).catch((err) => toast(err.message, "error"));
        } });
      }
      if (c.kind === "choice") {
        const cur = (c.choices.find((x) => x.value === prof[c.field]) || {}).label || prof[c.field];
        return row({ name: c.label, sub: c.description, value: cur, arrow: true, key, tag, onClick: async () => {
          const v = await picker(c.label, c.choices.map((x) => ({ label: x.label, value: x.value, current: x.value === prof[c.field] })));
          if (v !== null) save({ [c.field]: v }, key).catch((err) => toast(err.message, "error"));
        } });
      }
      if (typeof prof[c.field] === "boolean") {
        return row({ name: c.label, sub: c.description, sw: prof[c.field], key, tag,
          onClick: () => save({ [c.field]: !prof[c.field] }, key).catch((err) => toast(err.message, "error")) });
      }
      return null;
    }
    const backToAll = async () => {
      const e = document.activeElement;
      const c = e && e.dataset && info.catalog.find((x) => x.id === e.dataset.key);
      if (!p || !c || !overridden(c.field)) return;
      try { info = await scope.save({ override: { fields: c.field === "exclude" ? ["exclude", "exclude_amiga"] : [c.field], on: false } }); draw(c.id); toast("Back to every system's rule"); }
      catch (err) { toast(err.message, "error"); }
    };
    return (async () => {
      info = await get(scope.url);
      draw();
      const n = p ? info.inherit.overridden.length : 0;
      return { title: "Rules", crumbs: p ? [[p.name, `#/system/${p.slug}`]] : [["Settings", "#/settings"]], back: p ? `#/system/${p.slug}` : "#/settings",
        aside: p ? (n ? `${n} set for this system` : "All follow every system") : "Every system",
        keys: p ? { X: backToAll } : {},
        body: el("div", { class: "screen tight" }, el("div", { class: "rows" }, list)),
        hints: p ? [["A", "Change"], ["B", "Back"], ["X", "Back to every system"], ["LT RT", "Page"]] : [["A", "Change"], ["B", "Back"], ["LT RT", "Page"]] };
    })();
  }
  route(/^\/settings\/rules$/, (ctx) => rulesScreen(ctx, null));
  route(/^\/system\/([^/]+)\/rules$/, async (ctx, slug) => {
    await loadPlatforms();
    const p = bySlug(slug);
    if (!p) { location.replace("#/"); return null; }
    return rulesScreen(ctx, p);
  });

  // ------------------------------------------------------------------ Settings: databases
  const DB_STATUS = { absent: ["Not installed", ""], unknown: ["Installed", "ok"], current: ["Up to date", "ok"], up_to_date: ["Up to date", "ok"],
    update_available: ["Update", "warn"], bundled: ["Built in", "ok"], error: ["Error", "bad"], installed: ["Installed", "ok"] };
  /** What the last update did with the WHDLoad installs of your Amiga games (they come with the databases). */
  function whdNote(w) {
    if (!w) return null;
    if (w.error) return el("p", { class: "note", text: `WHDLoad installs: ${w.error}` });
    if (w.skipped) return el("p", { class: "note", text: "WHDLoad installs: skipped while a job was running; the next start tries again." });
    const parts = [w.downloaded ? `${fmt(w.downloaded)} downloaded` : null, w.updated ? `${fmt(w.updated)} updated` : null,
      (w.failed || []).length ? `${fmt(w.failed.length)} failed` : null].filter(Boolean);
    return el("p", { class: "note", text: `WHDLoad installs for your Amiga games: ${parts.length ? parts.join(", ") : "all up to date"}${w.at ? ` (${ago(new Date(w.at * 1000).toISOString())})` : ""}.` });
  }

  route(/^\/settings\/databases$/, async (ctx) => {
    const [db, upd] = await Promise.all([get("/api/databases"), get("/api/updates")]);
    const groups = {};
    for (const r of db.databases || []) (groups[r.group] = groups[r.group] || []).push(r);
    const out = [];
    for (const [g, rows] of Object.entries(groups)) {
      out.push(el("h2", { class: "section-title", text: g }));
      for (const r of rows) {
        const [text, cls] = DB_STATUS[r.status] || [r.status || "", ""];
        out.push(row({ name: r.name, sub: [r.source, r.installed ? r.installed : null, r.used_by && r.used_by.length ? (r.used_by.length > 3 ? `${r.used_by.length} systems` : r.used_by.join(", ")) : null].filter(Boolean).join(" · "),
          tag: [text, cls], info: true }));
      }
    }
    const status = el("p", { class: "note", text: upd.running ? (upd.progress && upd.progress.message) || "Updating…" : upd.last_checked ? `Last checked ${ago(upd.last_checked)}` : "Not checked yet" });
    const check = el("button", { class: "btn primary", type: "button", id: "db-check", style: "min-width:0", on: { click: async () => {
      try { await post("/api/updates/check"); } catch (err) { toast(err.message, "error"); return; }
      for (let i = 0; i < 1800 && ctx.alive(); i++) {
        await sleep(1000);
        let u;
        try { u = await get("/api/updates"); } catch (_) { continue; }
        status.textContent = u.running ? (u.progress && u.progress.message) || "Updating…" : u.error ? u.error : "Done";
        if (!u.running) { if (ctx.alive()) ctx.redraw(); return; }
      }
    } } }, "Check for updates");
    return { title: "Databases", crumbs: [["Settings", "#/settings"]], back: "#/settings", focus: "#db-check",
      body: el("div", { class: "screen" }, el("div", { class: "split" }, el("div", { class: "grow rows", "data-section": "list" }, out),
        el("aside", { class: "panel", "data-section": "panel" }, check,
          el("button", { class: "btn", type: "button", on: { click: async () => { try { await post("/api/ratings/download"); toast("Ratings are downloading"); } catch (err) { toast(err.message, "error"); } } } }, "Download ratings"),
          status, el("p", { class: "note", text: upd.enabled ? "Automatic updates are on." : "Automatic updates are off." }),
          whdNote(upd.whdload_games)))),
      hints: [["A", "Select"], ["B", "Back"]] };
  });

  // ------------------------------------------------------------------ Settings: BIOS and firmware
  route(/^\/settings\/bios$/, async (ctx) => {
    const [b, ra] = await Promise.all([get("/api/bios"), get("/api/retroarch")]);
    const head = { title: "BIOS and firmware", crumbs: [["Settings", "#/settings"]], back: "#/settings" };
    const sources = (b.sources || []).map((s) => row({ name: s.name, sub: s.note, value: s.installed ? s.version : "Not installed", info: true }));
    const found = (b.found || []).map((f) => row({ name: f.name || f.file || "", sub: [f.platform, f.path].filter(Boolean).join(" · "), tag: ["Found", "ok"], info: true }));
    const results = el("div", { class: "rows", "data-section": "list" });
    const panel = el("aside", { class: "panel", "data-section": "panel" });
    const STATUS = { ok: ["In place", "ok"], present: ["In place", "ok"], wrong: ["Wrong file", "bad"], found: ["Found", "warn"], missing: ["Missing", "bad"] };
    const showCheck = (res) => {
      const rows = [];
      for (const c of res.cores || []) for (const fw of c.firmware || []) {
        const [t, cls] = STATUS[fw.status] || [fw.status, ""];
        const r = row({ name: fw.desc || fw.path, sub: `${c.display || c.core}${fw.optional ? " · optional" : ""} · ${fw.path}`, tag: [t, cls] });
        r.addEventListener("focus", () => put(panel, el("span", { class: "panel-title", text: fw.desc || fw.path }),
          el("dl", { class: "facts" }, el("dt", { text: "Needed by" }), el("dd", { text: c.display || c.core }), el("dt", { text: "In RetroArch" }), el("dd", { text: fw.target }),
            fw.source ? [el("dt", { text: "Found at" }), el("dd", { text: fw.source })] : null), placeBtn(res)));
        rows.push(r);
      }
      for (const k of (res.keys && res.keys.rows) || []) rows.push(row({ name: k.name || k.file, sub: k.emulator || "", tag: k.source ? ["Found", "warn"] : k.ok ? ["In place", "ok"] : ["Missing", "bad"], info: true }));
      put(results, ...(rows.length ? rows : [row({ name: "Nothing is missing", static: true })]));
      put(panel, el("span", { class: "panel-title", text: `${fmt((res.counts || {}).found || 0)} found, ${fmt((res.counts || {}).missing || 0)} missing` }), placeBtn(res));
      if (mode !== "mouse" && rows[0]) focusEl(rows[0]);
    };
    const placeBtn = (res) => ((res.counts || {}).found ? el("button", { class: "btn primary", type: "button", style: "min-width:0", on: { click: async () => {
      if (!await confirmDialog("Copy the found files into RetroArch?", "Copy them", "Cancel", "They are copied into RetroArch's system folder; your files stay where they are.")) return;
      runJob(ctx, head, "Placing files…", () => post("/api/retroarch/bios/apply", { mode: "copy" }), (job) => { const r = job.result || {}; toast(`${fmt(r.placed || 0)} placed${(r.failed || []).length ? `, ${fmt(r.failed.length)} failed` : ""}`); ctx.redraw(); });
    } } }, "Copy found files into place") : null);
    const check = () => runJob(ctx, head, "Checking the cores' BIOS files…", () => post("/api/retroarch/bios/scan", {}), (job) => {
      ctx.show({ ...head, body: el("div", { class: "screen" }, el("div", { class: "split" }, el("div", { class: "grow" }, results), panel)), hints: [["A", "Select"], ["B", "Back"]] });
      showCheck(job.result || {});
    });
    const noRa = !(ra.installs || []).length && !ra.selected;
    return { ...head, focus: "#bios-check",
      body: el("div", { class: "screen" },
        el("div", { class: "btns" }, el("button", { class: "btn primary", type: "button", id: "bios-check", disabled: noRa, on: { click: check } }, "Check the emulators' BIOS files"),
          noRa ? el("p", { class: "note", text: "No RetroArch was found: pick one in Emulators." }) : null),
        found.length ? el("section", { class: "section" }, el("h2", { class: "section-title", text: "Found in your ROM folders" }), el("div", { class: "rows" }, found)) : null,
        el("section", { class: "section" }, el("h2", { class: "section-title", text: "Where the checksums come from" }), el("div", { class: "rows" }, sources))),
      hints: [["A", "Select"], ["B", "Back"]] };
  });

  // ------------------------------------------------------------------ Settings: emulators
  route(/^\/settings\/emulators$/, async (ctx) => {
    const [ra, em] = await Promise.all([get("/api/retroarch"), get("/api/emulators")]);
    const installs = ra.installs || [];
    const label = (i) => i.label || i.name || i.cfg;
    const raRows = [
      installs.length ? choiceRow({ name: "RetroArch", key: "ra", options: installs.map((i) => ({ label: label(i), value: i.cfg, sub: i.cfg })), current: ra.selected,
        save: async (v) => { await post("/api/retroarch/select", { cfg: v }); return v; } })
        : row({ name: "RetroArch", value: "Not found", static: true }),
      row({ name: "Another RetroArch", sub: "Its folder or retroarch.cfg", icon: "folder", arrow: true, href: "#/settings/emulators/retroarch" }),
      switchRow({ name: "Rename saves with their ROMs", sub: "When a build renames a game", on: ra.follow !== false, key: "follow",
        save: async (on) => (await post("/api/retroarch/follow", { follow: on })).follow }),
      row({ name: "Saves, states and asset folders", sub: "Move them, one folder per core, shared folders", icon: "save", arrow: true, href: "#/settings/retroarch", id: "emu-ra" }),
    ];
    const others = (em.emulators || []).map((e) => row({ name: e.label, sub: e.folder || e.what, tag: e.found ? ["Found", "ok"] : ["Not found", ""], arrow: true,
      href: `#/settings/emulators/folder?key=${encodeURIComponent(e.key)}` }));
    return { title: "Emulators", crumbs: [["Settings", "#/settings"]], back: "#/settings",
      body: el("div", { class: "screen tight" }, el("h2", { class: "section-title", text: "RetroArch" }), el("div", { class: "rows" }, raRows),
        el("h2", { class: "section-title", text: "Other emulators" }), el("div", { class: "rows" }, others)),
      hints: [["A", "Select"], ["B", "Back"]] };
  });
  route(/^\/settings\/emulators\/retroarch$/, (ctx) => folderScreen(ctx, {
    head: { title: "Another RetroArch", crumbs: [["Emulators", "#/settings/emulators"]], back: "#/settings/emulators" }, start: "",
    choose: async (path) => { try { await post("/api/retroarch/select", { custom: path }); location.hash = "#/settings/emulators"; } catch (err) { toast(err.message, "error"); } },
  }));
  route(/^\/settings\/emulators\/folder$/, async (ctx) => {
    const em = await get("/api/emulators");
    const e = (em.emulators || []).find((x) => x.key === ctx.params.key);
    if (!e) { location.replace("#/settings/emulators"); return null; }
    return folderScreen(ctx, {
      head: { title: e.label, crumbs: [["Emulators", "#/settings/emulators"]], back: "#/settings/emulators" }, start: e.folder || "",
      choose: async (path) => { try { await post("/api/emulators/config", { source: e.key, folder: path }); location.hash = "#/settings/emulators"; } catch (err) { toast(err.message, "error"); } },
    });
  });

  // ------------------------------------------------------------------ Settings: saves
  const SAVED_CHOICES = [
    { value: "convert", label: "Copy or convert them to the new version", sub: "The old save stays where it is (default)" },
    { value: "carry", label: "Copy them to the better version", sub: "Both ROMs stay where a save would not work" },
    { value: "keep", label: "Keep both ROMs", sub: "The old ROM stays, so its saves keep their game" },
    { value: "archive", label: "Archive the saves with the ROM", sub: "They move to the archive with it" },
    { value: "leave", label: "Leave the saves where they are", sub: "They are not touched" },
  ];
  route(/^\/settings\/saves$/, async (ctx) => {
    const [d, beh, ra] = await Promise.all([get("/api/library/defaults"), get("/api/saves/behaviour"), get("/api/retroarch")]);
    const rows = [
      choiceRow({ name: "Your saves", title: "When a build changes the version of a game you have saves for", options: SAVED_CHOICES, current: d.profile.saved_games || "convert", key: "saved",
        save: async (v) => { await post("/api/library/defaults", { saved_games: v }); return v; } }),
      switchRow({ name: "Rename saves with their ROMs", on: ra.follow !== false, key: "follow", save: async (on) => (await post("/api/retroarch/follow", { follow: on })).follow }),
    ];
    const systems = (beh.rows || []).map((r) => {
      const names = r.systems.length > 3 ? `${r.systems.slice(0, 2).join(", ")} and ${r.systems.length - 2} more` : r.systems.join(", ");
      return row({ name: names, sub: r.kept_by, arrow: true, onClick: () => sheet(names, [el("dl", { class: "facts" },
        el("dt", { text: "Kept by" }), el("dd", { text: r.kept_by }), el("dt", { text: "Follows a renamed game" }), el("dd", { text: r.follows }),
        el("dt", { text: "Copied to another version" }), el("dd", { text: r.copies }), el("dt", { text: "Converted" }), el("dd", { text: r.converts }),
        el("dt", { text: "Systems" }), el("dd", { text: r.systems.join(", ") }))], [["B", "Close"]]) });
    });
    return { title: "Saves", crumbs: [["Settings", "#/settings"]], back: "#/settings",
      body: el("div", { class: "screen tight" }, el("h2", { class: "section-title", text: "When a build changes the version of a game you have saves for" }),
        el("div", { class: "rows" }, rows), el("h2", { class: "section-title", text: "How each system's saves behave" }), el("div", { class: "rows" }, systems)),
      hints: [["A", "Change"], ["B", "Back"]] };
  });

  // ------------------------------------------------------------------ Settings: disc images (chdman kept as it is)
  route(/^\/settings\/disc$/, async (ctx) => {
    const c = await get("/api/chdman");
    const saveC = async (body) => post("/api/chdman", body);
    const rows = [
      choiceRow({ name: "Compression of new CHDs", key: "preset", current: c.preset || "default",
        options: [{ label: "Standard", value: "default", sub: "LZMA / FLAC: every emulator reads it" }, ...(c.zstd_writer ? [{ label: "Zstandard", value: "zstd", sub: "About 3x faster, needs an emulator from 2024 or later" }] : [])],
        save: async (v) => (await saveC({ preset: v })).preset }),
      switchRow({ name: "Fully check DVD images when scanning", sub: "Reads the whole disc instead of trusting the CHD header. Slow.", on: c.verify_dvd, key: "dvd",
        save: async (on) => (await saveC({ verify_dvd: on })).verify_dvd }),
      switchRow({ name: "Check audio tracks on CDs when scanning", sub: "Data tracks are always checked.", on: c.verify_audio, key: "audio",
        save: async (on) => (await saveC({ verify_audio: on })).verify_audio }),
    ];
    const chdman = [
      row({ name: "chdman", sub: c.found ? (c.label || c.kind) : "Optional: the app reads and writes CHDs itself", tag: c.found ? ["Found", "ok"] : ["Not found", ""], arrow: true, key: "chdman",
        onClick: async () => {
          const input = el("input", { type: "text", value: c.override || "", placeholder: "Path to chdman (optional)", "aria-label": "Path to chdman" });
          const saveBtn = el("button", { class: "btn primary", type: "button", style: "min-width:0", on: { click: async () => {
            try { await saveC({ path: input.value.trim() }); topOverlay() && topOverlay().close(true); ctx.redraw(); } catch (err) { toast(err.message, "error"); }
          } } }, "Save");
          await sheet("chdman", [el("p", { class: "note", text: c.found ? `Found: ${c.label || c.kind}` : c.hint || "" }),
            ...(c.found ? [] : (c.steps || []).map((s) => el("p", { class: "note", text: `• ${s}` }))),
            el("label", { class: "row field" }, el("span", { class: "row-name", text: "Path" }), input), saveBtn], [["A", "Select"], ["B", "Close"]]);
        } }),
      choiceRow({ name: "Reading CHDs", key: "engine", current: c.engine || "auto",
        options: [{ label: "Built-in, chdman for the rest", value: "auto", sub: "chdman only for CHDs the built-in reader cannot decode" },
          { label: "Always built-in", value: "python", sub: "Never chdman, no temporary files" }, { label: "Always chdman", value: "chdman", sub: "Extracts every disc to temporary space" }],
        save: async (v) => (await saveC({ engine: v })).engine }),
      choiceRow({ name: "Writing CHDs", key: "writer", current: c.writer || "auto",
        options: [{ label: "Built-in", value: "auto", sub: "No chdman needed" }, { label: "chdman, when it is installed", value: "chdman" }],
        save: async (v) => (await saveC({ writer: v })).writer }),
    ];
    return { title: "Disc images", crumbs: [["Settings", "#/settings"]], back: "#/settings", aside: "Dreamcast, PlayStation, PlayStation 2",
      body: el("div", { class: "screen tight" }, el("div", { class: "rows" }, rows),
        c.flac_encoder === false ? message("libFLAC was not found: audio tracks are stored with LZMA, so CHDs with audio come out larger.", "warn") : null,
        el("h2", { class: "section-title", text: "chdman" }), el("div", { class: "rows" }, chdman)),
      hints: [["A", "Change"], ["B", "Back"]] };
  });

  // ------------------------------------------------------------------ Settings: advanced
  route(/^\/settings\/advanced$/, async (ctx) => {
    await Promise.all([loadStatus(), loadPlatforms()]);
    const r = await get("/api/ratings").catch(() => ({ igdb: {} }));
    const ig = r.igdb || {};
    const igdb = async () => {
      const id = el("input", { type: "text", value: ig.client_id || "", placeholder: "Client ID", "aria-label": "IGDB client ID" });
      const secret = el("input", { type: "password", placeholder: ig.has_secret ? "Saved - type to replace" : "Client secret", "aria-label": "IGDB client secret" });
      const out = el("div", {});
      await sheet("Your IGDB key", [el("p", { class: "note", text: "Optional: more ratings from IGDB.com with your own Twitch API key." }),
        el("label", { class: "row field" }, el("span", { class: "row-name", text: "Client ID" }), id),
        el("label", { class: "row field" }, el("span", { class: "row-name", text: "Secret" }), secret),
        el("button", { class: "btn primary", type: "button", style: "min-width:0", on: { click: async () => {
          try { await post("/api/ratings/igdb", { client_id: id.value.trim(), client_secret: secret.value }); put(out, message("Saved. The IGDB ratings are building in the background.", "good")); }
          catch (err) { put(out, message(err.message, "bad")); }
        } } }, "Save"), out], [["A", "Select"], ["B", "Close"]]);
      ctx.redraw();
    };
    const perDb = async () => {
      const setUp = S.platforms.filter((p) => p.folder);
      const v = await picker("Databases of", (setUp.length ? setUp : S.platforms).map((p) => ({ label: p.name, value: p.slug })));
      if (v) location.hash = `#/system/${v}/databases`;
    };
    return { title: "Advanced", crumbs: [["Settings", "#/settings"]], back: "#/settings",
      body: el("div", { class: "screen tight" }, el("div", { class: "rows" },
        row({ name: "A system's databases", sub: "Versions, folders and counts per database", arrow: true, onClick: perDb }),
        row({ name: "More ratings: your IGDB key", sub: "Optional", value: ig.configured ? "Set" : "Not set", arrow: true, onClick: igdb }),
        switchRow({ name: "Show hidden folders when choosing a folder", on: Prefs.get("hidden", false), save: async (on) => { Prefs.set("hidden", on); return on; } }),
        row({ name: "Data folder", value: S.status.data_dir || "", info: true }),
        row({ name: "Version", value: S.status.version, info: true }),
        row({ name: "Classic interface", sub: "The old pages", arrow: true, href: classic() }))),
      hints: [["A", "Select"], ["B", "Back"]] };
  });

  // ------------------------------------------------------------------ RetroArch: which one, saves and states, asset folders
  function fmtBytes(n) {
    if (!n && n !== 0) return "-";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  }
  const RA_HEAD = { crumbs: [["Emulators", "#/settings/emulators"]], back: "#/settings/emulators" };
  const raRunning = () => message("RetroArch is running. Close it before changing anything: it rewrites its config when it closes.", "warn");
  let raMove = null;          // the choices of "Move saves and states" while they are being made: {save_dir, state_dir, sort_saves, sort_states}
  let raBase = "";            // the shared folder looked at by "One shared folder for all"

  route(/^\/settings\/retroarch$/, async (ctx) => {
    const ra = await get("/api/retroarch");
    const installs = ra.installs || [];
    const st = ra.settings;
    raMove = null;
    if (!st) {
      return { title: "RetroArch", ...RA_HEAD,
        body: el("div", { class: "screen tight" }, message("No RetroArch was found. Point the app at its folder or its retroarch.cfg."),
          el("div", { class: "rows" }, row({ name: "Another RetroArch", sub: "Its folder or retroarch.cfg", icon: "folder", arrow: true, href: "#/settings/emulators/retroarch" }))),
        hints: [["A", "Select"], ["B", "Back"]] };
    }
    let shared = "";
    try { const a = await post("/api/retroarch/assets"); shared = `${(a.rows || []).filter((r) => !r.own).length} of ${(a.rows || []).length} outside RetroArch`; } catch (_) { /* informational */ }
    const label = (i) => i.label || i.name || i.cfg;
    const same = st.savefile_path && st.savefile_path === st.savestate_path;
    return { title: "RetroArch", ...RA_HEAD, aside: label(installs.find((i) => i.cfg === ra.selected) || {}) || "",
      body: el("div", { class: "screen tight" }, ra.running ? raRunning() : null, el("div", { class: "rows" },
        installs.length > 1 ? choiceRow({ name: "Which RetroArch", options: installs.map((i) => ({ label: label(i), value: i.cfg, sub: i.cfg })), current: ra.selected,
          save: async (v) => { await post("/api/retroarch/select", { cfg: v }); ctx.redraw(); return v; } })
          : row({ name: "Which RetroArch", sub: st.cfg, value: label(installs[0] || {}), info: true }),
        row({ name: "Saves and states", id: "ra-saves", sub: [st.sort_savefiles_enable ? "One folder per core" : null, same ? "States with the saves" : null].filter(Boolean).join(" · ") || null,
          value: st.savefile_path || "RetroArch's own", arrow: true, href: "#/settings/retroarch/move" }),
        row({ name: "Asset folders", sub: "Playlists, thumbnails, cheats and more", value: shared, arrow: true, href: "#/settings/retroarch/assets" }),
        switchRow({ name: "Rename saves with their ROMs", sub: "When a build renames a game", on: ra.follow !== false, key: "follow",
          save: async (on) => (await post("/api/retroarch/follow", { follow: on })).follow }),
        row({ name: "Another RetroArch", sub: "Its folder or retroarch.cfg", icon: "folder", arrow: true, href: "#/settings/emulators/retroarch" }))),
      focus: "#ra-saves", hints: [["A", "Select"], ["B", "Back"]] };
  });

  route(/^\/settings\/retroarch\/move$/, async (ctx) => {
    const ra = await get("/api/retroarch");
    const st = ra.settings;
    if (!st) { location.replace("#/settings/retroarch"); return null; }
    if (!raMove) raMove = { save_dir: st.savefile_path, state_dir: st.savestate_path === st.savefile_path ? "" : st.savestate_path,
      sort_saves: !!st.sort_savefiles_enable, sort_states: !!st.sort_savestates_enable };
    const m = raMove;
    const back = ra.backup || {};
    const head = { title: "Move saves and states", crumbs: [["RetroArch", "#/settings/retroarch"]], back: () => { raMove = null; location.hash = "#/settings/retroarch"; } };
    const preview = () => { if (!m.save_dir) { toast("Choose the folder for save files."); return; } location.hash = "#/settings/retroarch/preview"; };
    const rows = [
      row({ name: "Save files", sub: ".srm, .sav and others", value: m.save_dir || "Choose", arrow: true, id: "ra-save-dir", href: "#/settings/retroarch/folder?which=save" }),
      choiceRow({ name: "Save states", sub: ".state, .state1 and others", key: "states",
        options: [{ label: "Same as save files", value: "same" }, { label: "Their own folder", value: "own", sub: m.state_dir || "Choose it next" }],
        current: m.state_dir ? "own" : "same",
        save: async (v) => { if (v === "same") { m.state_dir = ""; return v; } location.hash = "#/settings/retroarch/folder?which=state"; return undefined; } }),
      switchRow({ name: "One folder per core for save files", on: m.sort_saves, save: async (on) => { m.sort_saves = on; return on; } }),
      switchRow({ name: "One folder per core for save states", on: m.sort_states, save: async (on) => { m.sort_states = on; return on; } }),
      switchRow({ name: "Back up the files to a zip first", sub: back.dir, on: back.enabled !== false, key: "backup",
        save: async (on) => (await post("/api/retroarch/select", { backup: on })).backup.enabled }),
      row({ name: "Backup folder", value: back.dir || "", arrow: true, href: "#/settings/retroarch/folder?which=backup" }),
    ];
    return { ...head, focus: "#ra-save-dir", keys: { Start: preview },
      body: el("div", { class: "screen" }, ra.running ? raRunning() : null, el("div", { class: "split" }, el("div", { class: "grow rows", "data-section": "list" }, rows),
        el("aside", { class: "panel", "data-section": "panel" },
          el("dl", { class: "facts" }, el("dt", { text: "Now" }), el("dd", { text: st.savefile_path || "RetroArch's own" }),
            el("dt", { text: "After" }), el("dd", { text: m.save_dir || "-" })),
          el("button", { class: "btn primary", type: "button", id: "ra-preview", on: { click: preview } }, "Preview")))),
      hints: [["A", "Change"], ["B", "Back"], ["Start", "Preview"]] };
  });

  route(/^\/settings\/retroarch\/folder$/, async (ctx) => {
    const which = ctx.params.which;
    const ra = await get("/api/retroarch");
    const st = ra.settings || {};
    const goBack = which === "asset" ? "#/settings/retroarch/assets" : which === "base" ? "#/settings/retroarch/shared" : "#/settings/retroarch/move";
    const titles = { save: "Folder for save files", state: "Folder for save states", backup: "Folder for the backup zips", base: "The shared folder", asset: "Folder for this setting" };
    const start = which === "save" ? (raMove && raMove.save_dir) || st.savefile_path : which === "state" ? (raMove && raMove.state_dir) || st.savestate_path
      : which === "backup" ? (ra.backup || {}).dir : which === "base" ? raBase : "";
    return folderScreen(ctx, {
      head: { title: titles[which] || "Choose a folder", crumbs: [["RetroArch", "#/settings/retroarch"]], back: goBack }, start: start || "",
      choose: async (path) => {
        try {
          if (which === "save" || which === "state") { if (!raMove) { location.hash = "#/settings/retroarch/move"; return; } raMove[which === "save" ? "save_dir" : "state_dir"] = path; }
          else if (which === "backup") await post("/api/retroarch/select", { backup_dir: path });
          else if (which === "base") raBase = path;
          else if (which === "asset") await post("/api/retroarch/assets/set", { key: ctx.params.key, folder: path });
          location.hash = goBack;
        } catch (err) { toast(err.message, "error"); }
      },
    });
  });

  route(/^\/settings\/retroarch\/preview$/, async (ctx) => {
    if (!raMove) { location.replace("#/settings/retroarch/move"); return null; }
    const m = raMove;
    const head = { title: "What will happen", crumbs: [["Move saves", "#/settings/retroarch/move"]], back: "#/settings/retroarch/move" };
    ctx.show({ ...head, body: el("div", { class: "screen" }, skeletonTiles(3)), hints: [["B", "Back"]] });
    Activity.set("screen", "Looking at every save and state");
    const body = { save_dir: m.save_dir, state_dir: m.state_dir || m.save_dir, sort_saves: m.sort_saves, sort_states: m.sort_states };
    let plan;
    try { plan = await post("/api/retroarch/plan", body); }
    catch (err) { return { ...head, body: el("div", { class: "screen" }, message(err.message, "bad")), hints: [["B", "Back"]] }; }
    const ra = await get("/api/retroarch");
    const c = plan.counts || {};
    const items = plan.items || [];
    const whole = items.length < 300;          // (the server sends up to 300 rows: split them only when that is all of them)
    const saves = whole ? items.filter((i) => i.status === "move" && i.kind !== "state").length : 0;
    const states = whole ? items.filter((i) => i.status === "move" && i.kind === "state").length : 0;
    const cfg = Object.entries(plan.cfg_changes || {});
    const everything = async () => {
      const lines = items.map((i) => row({ name: baseName(i.from), sub: `${i.to}${i.note ? ` · ${i.note}` : ""}`, info: true,
        tag: i.status === "needs_core" ? ["Needs its core first", "warn"] : i.status === "conflict" ? ["Already there", "bad"] : null }));
      await sheet("Every file", [el("div", { class: "rows" }, lines.length ? lines : [row({ name: "No file moves", static: true })])], [["B", "Close"]]);
    };
    if (plan.empty) {
      return { ...head, body: el("div", { class: "screen" }, el("h2", { class: "big-title", text: "Nothing to move" }),
        el("p", { class: "lead", text: "RetroArch already uses these folders." }),
        el("div", { class: "btns" }, el("a", { class: "btn", href: "#/settings/retroarch", text: "Back to RetroArch" }))), hints: [["A", "Select"], ["B", "Back"]] };
    }
    const backup = ra.backup || {};
    const go = el("button", { class: "btn primary", type: "button", id: "ra-go", disabled: plan.running, on: { click: async () => {
      runJob(ctx, head, "Moving saves…", () => post("/api/retroarch/apply", { ...body, backup: backup.enabled !== false }), (job) => {
        const r = job.result || {};
        raMove = null;
        const failed = r.failed || [];
        ctx.show({ title: job.status === "done" && !failed.length ? "Saves moved" : "Moved, with problems", crumbs: [["RetroArch", "#/settings/retroarch"]], back: "#/settings/retroarch", focus: "#ra-done",
          body: el("div", { class: "screen" }, job.error ? message(job.error, "bad") : null, el("div", { class: "rows" },
            row({ name: `${fmt(r.moved || 0)} file${r.moved === 1 ? "" : "s"} moved`, icon: "check", info: true }),
            row({ name: `RetroArch now uses ${m.save_dir}`, icon: "check", info: true }),
            r.backup ? row({ name: "Backup", value: r.backup, icon: "check", info: true }) : null,
            r.cfg_backup ? row({ name: "The old retroarch.cfg", value: r.cfg_backup, icon: "check", info: true }) : null,
            ...failed.slice(0, 30).map((f) => row({ name: baseName(f.path || ""), sub: f.error || "failed", info: true, tag: ["Not moved", "bad"] }))),
            el("div", { class: "btns" }, el("a", { class: "btn primary", id: "ra-done", href: "#/settings/retroarch", text: "Back to RetroArch" }), el("a", { class: "btn", href: "#/", text: "Home" }))),
          hints: [["A", "Select"], ["B", "Back"]] });
      });
    } } }, "Move and update RetroArch");
    const counts = [
      saves ? statTile({ number: fmt(saves), sub: "Save files" }) : null,
      states ? statTile({ number: fmt(states), sub: "Save states" }) : null,
      c.needs_core ? statTile({ number: fmt(c.needs_core), sub: "Need their core run once first", warn: true }) : null,
      c.conflict ? statTile({ number: fmt(c.conflict), sub: "Already there: left alone", warn: true }) : null,
    ].filter(Boolean);
    return { ...head, focus: "#ra-go", keys: { X: everything, Start: () => go.click() },
      body: el("div", { class: "screen tight" }, plan.running ? raRunning() : null,
        el("h2", { class: "big-title", text: `${fmt(c.move || 0)} file${c.move === 1 ? "" : "s"} will move` }),
        counts.length ? el("div", { class: "grid compact", style: `--cols:${Math.min(4, counts.length)}` }, counts) : null,
        cfg.length ? el("section", { class: "section" }, el("h2", { class: "section-title", text: "Changes in retroarch.cfg" }),
          el("div", { class: "rows" }, cfg.map(([k, v]) => row({ name: k, value: typeof v === "boolean" ? (v ? "on" : "off") : String(v), info: true })))) : null,
        ...(plan.notes || []).map((n) => message(n)),
        el("p", { class: "note", text: backup.enabled !== false ? `A backup zip goes to ${backup.dir} first${plan.free !== null && plan.free !== undefined ? ` · ${fmtBytes(plan.free)} free there` : ""} · ${fmtBytes(plan.bytes || 0)} to move.` : `No backup is made · ${fmtBytes(plan.bytes || 0)} to move.` }),
        el("div", { class: "btns" }, go, el("a", { class: "btn", href: "#/settings/retroarch/move", text: "Back" }))),
      hints: [["A", "Select"], ["B", "Back"], ["X", "See every file"]] };
  });

  route(/^\/settings\/retroarch\/assets$/, async (ctx) => {
    const ra = await get("/api/retroarch");
    if (!ra.settings) { location.replace("#/settings/retroarch"); return null; }
    const a = await post("/api/retroarch/assets");
    const rows = (a.rows || []).map((r) => row({ name: r.label, key: r.key, value: r.own ? "RetroArch's own" : r.current_path, tag: r.own ? ["Own", ""] : ["Shared", "ok"],
      onClick: async () => {
        const v = await picker(r.label, [{ label: "Choose a folder", value: "pick" }, { label: "Back to RetroArch's own", value: "own", current: r.own }]);
        if (v === "pick") location.hash = `#/settings/retroarch/folder?which=asset&key=${encodeURIComponent(r.key)}`;
        if (v === "own" && !r.own) {
          try { await post("/api/retroarch/assets/set", { key: r.key, folder: "" }); ctx.redraw(); } catch (err) { toast(err.message, "error"); }
        }
      } }));
    return { title: "Asset folders", crumbs: [["RetroArch", "#/settings/retroarch"]], back: "#/settings/retroarch", aside: "Only retroarch.cfg changes",
      keys: { X: () => { location.hash = "#/settings/retroarch/shared"; } },
      body: el("div", { class: "screen tight" }, ra.running ? raRunning() : null, el("div", { class: "rows" }, rows)),
      hints: [["A", "Change"], ["B", "Back"], ["X", "One shared folder for all"]] };
  });

  route(/^\/settings\/retroarch\/shared$/, async (ctx) => {
    const ra = await get("/api/retroarch");
    if (!ra.settings) { location.replace("#/settings/retroarch"); return null; }
    const res = await post("/api/retroarch/shared", raBase ? { base: raBase } : {});
    raBase = res.base || raBase;
    const picked = new Set((res.rows || []).filter((r) => r.status === "unset").map((r) => r.key));
    const changes = el("dl", { class: "facts" });
    const drawChanges = () => put(changes, ...(res.rows || []).filter((r) => picked.has(r.key)).flatMap((r) => [el("dt", { text: r.label }), el("dd", { text: r.want })]));
    const rows = (res.rows || []).map((r) => {
      if (r.status === "none") return row({ name: r.label, sub: "No such folder in the shared folder", static: true });
      if (r.status === "ok") return row({ name: r.label, sub: `${r.want} · already used`, info: true, tag: ["Already used", "ok"] });
      return switchRow({ name: r.label, sub: `${r.want}${r.status === "set" ? ` · now ${r.current_path}` : ""}`, on: picked.has(r.key), key: r.key,
        save: async (on) => { if (on) picked.add(r.key); else picked.delete(r.key); drawChanges(); return on; } });
    });
    drawChanges();
    const apply = async () => {
      if (!picked.size) { toast("Nothing switched on."); return; }
      try {
        const out = await post("/api/retroarch/shared/apply", { base: raBase, keys: [...picked] });
        toast(`RetroArch points at ${fmt(picked.size)} shared folder${picked.size === 1 ? "" : "s"} now${out.backup ? "; the old retroarch.cfg is kept" : ""}`);
        location.hash = "#/settings/retroarch/assets";
      } catch (err) { toast(err.message, "error"); }
    };
    return { title: "One shared folder for all", crumbs: [["Asset folders", "#/settings/retroarch/assets"]], back: "#/settings/retroarch/assets", keys: { Start: apply },
      body: el("div", { class: "screen" }, ra.running ? raRunning() : null, el("div", { class: "split" },
        el("div", { class: "grow rows", "data-section": "list" }, row({ name: "Shared folder", sub: "Where the sub-folders are looked for", value: raBase || "Choose", arrow: true, href: "#/settings/retroarch/folder?which=base" }), ...rows),
        el("aside", { class: "panel", "data-section": "panel" }, el("h3", { class: "section-title", text: "Changes" }), changes,
          el("button", { class: "btn primary", type: "button", id: "ra-shared-go", disabled: ra.running, on: { click: apply } }, "Point RetroArch at these"),
          el("p", { class: "note", text: "Only retroarch.cfg changes; the old one is kept as a backup. No file moves." })))),
      hints: [["A", "Switch"], ["B", "Back"], ["Start", "Point RetroArch at these"]] };
  });

  // ------------------------------------------------------------------ a system: its databases (what the classic "DAT" section showed)
  route(/^\/system\/([^/]+)\/databases$/, async (ctx, slug) => {
    await Promise.all([loadPlatforms(), loadStatus()]);
    const p = bySlug(slug);
    if (!p) { location.replace("#/"); return null; }
    let sel = { selected: false };
    if (p.folder) { try { sel = await post("/api/scan/select", { platform: p.name }); } catch (_) { /* no counts */ } }
    if (sel.selected) await loadStatus();
    const scan = sel.selected ? S.status.scan || {} : {};
    const perDat = (scan.summary && scan.summary.per_dat) || {};
    const rows = (p.dats || []).map((d) => {
      const s = perDat[d.name] || null;
      const games = s && s.count_by === "game";
      const have = s ? (games ? s.games_have ?? s.have : s.have) : null;
      const total = s ? (games ? s.games_total ?? s.dat_total : s.dat_total) : null;
      return row({ name: d.name, info: true,
        sub: [d.version ? `Version ${d.version}` : null, d.folder ? `Folder ${d.folder}/` : "The system's folder", d.m3u ? "Playlists" : null, s ? `${fmt(have)} of ${fmt(total)}` : null].filter(Boolean).join(" · "),
        tag: d.present ? ["Installed", "ok"] : ["Not installed", ""] });
    });
    const missing = (p.dats || []).filter((d) => !d.present).length;
    return { title: "Databases", crumbs: [[p.name, `#/system/${slug}`]], back: `#/system/${slug}/settings`, aside: SOURCE[p.source] || "",
      body: el("div", { class: "screen tight" },
        missing ? message(`${missing} of ${(p.dats || []).length} databases are not installed yet. Check for updates in Settings › Databases.`, "warn") : null,
        el("div", { class: "rows" }, rows.length ? rows : [row({ name: "No databases for this system", static: true })]),
        missing ? el("div", { class: "btns" }, el("a", { class: "btn", href: "#/settings/databases", text: "Databases" })) : null),
      hints: [["B", "Back"]] };
  });

  // ------------------------------------------------------------------ start
  window.RomorgNext = { press, mode: () => mode, hints: () => $("strip").textContent, setMode };
  navigate();
})();
