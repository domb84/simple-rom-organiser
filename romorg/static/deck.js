/* Simple ROM Organiser - the controller layer (Steam Deck).
 *
 * A gamepad can drive every page: the D-pad and the left stick move the focus to the nearest control in that direction, A presses
 * it, B goes back (it closes the open dialog or menu first), Y finds the search box, LB / RB switch tabs, LT / RT and the right
 * stick scroll, Menu opens the More menu, View goes to all systems. Steam Input shows a controller to a page in one of two ways -
 * as a gamepad (the Gamepad API, read here) or as keyboard keys (arrows, Enter, Escape, handled below) - so both work.
 *
 * "pad" mode (a class on <body>) is on after the controller was used and off when the mouse or touch is: it draws a big focus ring
 * and the strip that names the buttons. "deck" mode (the page was served for a Deck: --deck, or Steam's environment) is the layout
 * for a handheld and keeps the strip always on. Nothing here changes the page without one of the two.
 */
(() => {
  "use strict";

  const body = document.body;
  const meta = document.querySelector('meta[name="romorg-deck"]');
  const DECK = !!(meta && meta.content && !meta.content.startsWith("__")) || /[?&]deck=1\b/.test(location.search);
  if (DECK) body.classList.add("deck");

  const FOCUSABLE = 'a[href], button, input:not([type="hidden"]), select, textarea, summary, [tabindex]:not([tabindex="-1"])';
  const TEXT = 'input:not([type]), input[type="text"], input[type="search"], input[type="number"], input[type="password"], ' +
    'input[type="url"], textarea';
  const FIRST_DELAY = 420;      // ms before a held direction repeats
  const REPEAT = 120;           // ms between repeats
  const STICK = 0.55;           // how far a stick must be pushed to count as a direction

  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // ------------------------------------------------------------------------------------------------ what can take the focus
  const shown = (el) => {
    const r = el.getBoundingClientRect();
    return r.width >= 2 && r.height >= 2 && getComputedStyle(el).visibility !== "hidden";
  };
  const usable = (el) => !!el && el.isConnected && !el.disabled && !el.closest("[inert], [hidden], .hidden") && shown(el);

  /** The part of the page the controller is in: the top dialog, the open More menu, else the whole page. */
  function scopeRoot() {
    const modal = $$(".modal:not(.hidden)").pop();
    if (modal) return modal;
    const pop = document.getElementById("more-pop");
    if (pop && !pop.classList.contains("hidden")) return pop;
    return body;
  }
  const inModal = () => scopeRoot().classList.contains("modal");
  const focusables = (root) => $$(FOCUSABLE, root).filter(usable);

  /** Where the focus starts in a scope: a dialog's main button, else the first control of the page being shown. */
  function entry(root) {
    if (root.classList.contains("modal")) return focusables(root).find((e) => e.matches(".btn-primary")) || focusables(root)[0];
    const view = $$("main .view:not(.hidden)").find(shown);
    const inView = view ? focusables(view) : [];
    return inView.find((e) => !e.closest(".tabs")) || inView[0] || focusables(root)[0];
  }

  function reveal(el) {
    const top = (document.getElementById("topbar") || { offsetHeight: 0 }).offsetHeight;
    const bar = strip.classList.contains("on") ? strip.offsetHeight : 0;
    const r = el.getBoundingClientRect();
    if (r.top < top + 8 || r.bottom > window.innerHeight - bar - 8) el.scrollIntoView({ block: "center", inline: "nearest" });
  }
  function focusEl(el) {
    if (!el) return false;
    el.focus({ preventScroll: true });
    reveal(el);
    return true;
  }

  // ------------------------------------------------------------------------------------------------ moving
  /** How good a target ``r`` is for a move from ``c`` in ``dir``: small is good; Infinity = not that way. */
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

  /** The part of the page a control is in. A move stays inside it while anything lies that way, and only then crosses to the next:
   *  without this the control that is merely nearest wins, and a press "down" in the page lands in the list at its side. */
  const section = (el) => el.closest("#side, #topbar") || document.querySelector("main") || body;

  function move(dir) {
    const root = scopeRoot();
    const items = focusables(root);
    if (!items.length) return false;
    const cur = document.activeElement;
    if (!cur || cur === body || !root.contains(cur) || !usable(cur)) return focusEl(entry(root));
    const c = cur.getBoundingClientRect();
    const mine = section(cur);
    let best = null, bestScore = Infinity, other = null, otherScore = Infinity;
    for (const el of items) {
      if (el === cur || el.contains(cur) || cur.contains(el)) continue;
      const s = score(c, el.getBoundingClientRect(), dir);
      if (section(el) === mine) { if (s < bestScore) { best = el; bestScore = s; } }
      else if (s < otherScore) { other = el; otherScore = s; }
    }
    return focusEl(best || other);
  }

  // ------------------------------------------------------------------------------------------------ the buttons
  const isText = (el) => !!el && el.matches && el.matches(TEXT);

  function activate() {
    const el = document.activeElement;
    if (!el || el === body || !usable(el)) return focusEl(entry(scopeRoot()));
    if (isText(el)) { try { el.select(); } catch (_) { /* a type that cannot select */ } return true; }
    if (el.tagName === "SELECT") {                    // (the browser's own list cannot be reached with a controller: A takes the next choice)
      if (el.options.length) {
        el.selectedIndex = (el.selectedIndex + 1) % el.options.length;
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      }
      return true;
    }
    if (el.type === "range") return true;
    el.click();
    return true;
  }

  /** B: the open dialog or menu closes (the page's own Escape handling does it), else an open section collapses, else up a level. */
  function back() {
    if (scopeRoot() !== body || $$(".col-menu[open]").length) {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      return true;
    }
    const open = document.activeElement && document.activeElement.closest ? document.activeElement.closest("details[open]") : null;
    if (open) {
      const summary = open.querySelector(":scope > summary");
      open.open = false;
      if (summary) focusEl(summary);
      return true;
    }
    const link = $$(".back-link").find(usable);
    if (link) { link.click(); return true; }
    if (location.hash && location.hash !== "#/" && location.hash !== "#") { location.hash = "#/"; return true; }
    return false;
  }

  function search() {
    const box = $$('input[type="search"]').find(usable);
    if (!box) return false;
    focusEl(box);
    box.select();
    return true;
  }

  /** LB / RB: the tab list the focus is in, else the first one on the page. */
  function tabs(delta) {
    const lists = $$('[role="tablist"]').filter(shown);
    const cur = document.activeElement;
    const list = lists.find((l) => cur && l.contains(cur)) || lists[0];
    if (!list) return false;
    const all = $$('[role="tab"]', list).filter(usable);
    if (all.length < 2) return false;
    const i = Math.max(0, all.findIndex((t) => t.getAttribute("aria-selected") === "true"));
    const next = all[(i + delta + all.length) % all.length];
    next.click();
    focusEl(next);
    setTimeout(() => {                                  // (the page may draw the tabs again: take the selected one of the new ones)
      const now = $$('[role="tab"][aria-selected="true"]', list).find(usable);
      if (now && (!document.activeElement || document.activeElement === body || !usable(document.activeElement))) focusEl(now);
    }, 80);
    return true;
  }

  function scroller(el) {
    for (let n = el; n && n !== body; n = n.parentElement) {
      if (/(auto|scroll)/.test(getComputedStyle(n).overflowY) && n.scrollHeight > n.clientHeight + 4) return n;
    }
    return document.scrollingElement || document.documentElement;
  }
  function pageScroll(sign) {
    const root = scopeRoot();
    const cur = document.activeElement;
    const s = scroller(cur && cur !== body && root.contains(cur) ? cur : root.classList.contains("modal") ? (root.querySelector(".modal-box") || root) : body);
    s.scrollBy({ top: sign * s.clientHeight * 0.8, behavior: "smooth" });
    return true;
  }

  function moreMenu() {
    const btn = document.getElementById("more-btn");
    if (!btn || !usable(btn)) return false;
    const open = btn.getAttribute("aria-expanded") === "true";
    if (open) { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); return true; }
    btn.click();
    setTimeout(() => { const first = focusables(document.getElementById("more-pop"))[0]; if (first) focusEl(first); }, 0);
    return true;
  }

  const home = () => { location.hash = "#/"; return true; };

  // ------------------------------------------------------------------------------------------------ pad mode and the strip
  let padOn = false;
  const strip = document.createElement("div");
  strip.id = "deck-hints";
  strip.className = "deck-hints";
  strip.setAttribute("role", "note");
  strip.setAttribute("aria-label", "Controller buttons");
  body.appendChild(strip);
  let stripKey = "";

  function context() {
    const el = document.activeElement;
    if (inModal()) return "modal";
    const pop = document.getElementById("more-pop");
    if (pop && !pop.classList.contains("hidden")) return "menu";
    if (isText(el)) return "text";
    if (el && el.tagName === "SELECT") return "select";
    return "page";
  }

  function renderHints() {
    const on = DECK || padOn;
    strip.classList.toggle("on", on);
    body.classList.toggle("pad", padOn);
    if (!on) return;
    const ctx = context();
    const items = [["dpad", "Move"]];
    if (ctx === "modal") items.push(["A", "Select"], ["B", "Cancel"]);
    else if (ctx === "menu") items.push(["A", "Select"], ["B", "Close"]);
    else if (ctx === "text") items.push(["Steam+X", "Keyboard"], ["B", "Back"]);
    else {
      items.push(["A", ctx === "select" ? "Next choice" : "Select"], ["B", "Back"]);
      if ($$('input[type="search"]').some(usable)) items.push(["Y", "Search"]);
      if ($$('[role="tablist"]').some((l) => shown(l) && $$('[role="tab"]', l).length > 1)) items.push(["LB RB", "Tabs"]);
      items.push(["LT RT", "Scroll"], ["Menu", "More"]);
    }
    const key = items.map((i) => i.join(":")).join("|");
    if (key === stripKey) return;
    stripKey = key;
    strip.replaceChildren(...items.map(([btn, text]) => {
      const row = document.createElement("span");
      row.className = "deck-hint";
      const glyph = document.createElement("span");
      glyph.className = "pad-btn";
      glyph.dataset.btn = btn;
      glyph.textContent = btn === "dpad" ? "✥" : btn;
      row.append(glyph, ` ${text}`);
      return row;
    }));
  }

  function setPad(on) {
    if (on === padOn) return;
    padOn = on;
    renderHints();
  }

  // ------------------------------------------------------------------------------------------------ focus keeping
  let lastOutside = null;                   // what had the focus before a dialog took it
  let wasModal = false;

  document.addEventListener("focusin", (e) => {
    if (!inModal() && e.target !== body) lastOutside = e.target;
    renderHints();
  });
  document.addEventListener("focusout", () => setTimeout(renderHints, 0));

  function sync() {
    renderHints();
    const modal = inModal();
    if (wasModal && !modal && lastOutside && usable(lastOutside)) focusEl(lastOutside);       // the dialog closed: back to where we were
    wasModal = modal;
    if (!padOn) return;
    const root = scopeRoot();
    const cur = document.activeElement;
    if (!cur || cur === body || !root.contains(cur) || !usable(cur)) focusEl(entry(root));    // the page was drawn again under the focus
  }
  let syncTimer = 0;
  const schedule = () => { clearTimeout(syncTimer); syncTimer = setTimeout(sync, 60); };
  new MutationObserver(schedule).observe(body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class", "hidden", "open", "aria-expanded"] });
  window.addEventListener("hashchange", schedule);

  // ------------------------------------------------------------------------------------------------ the gamepad
  const NAMES = ["A", "B", "X", "Y", "LB", "RB", "LT", "RT", "View", "Menu", "L3", "R3", "up", "down", "left", "right"];
  const ACTIONS = {
    A: activate, B: back, Y: search, LB: () => tabs(-1), RB: () => tabs(1), Menu: moreMenu, View: home,
    X: () => { document.dispatchEvent(new CustomEvent("deck:x")); return true; },
    LT: () => pageScroll(-1), RT: () => pageScroll(1),
    up: () => move("up"), down: () => move("down"), left: () => move("left"), right: () => move("right"),
  };

  /** A press of a controller button or key: the first press only shows where the focus is (it does nothing else). */
  function press(name) {
    const wasOn = padOn;
    setPad(true);
    const cur = document.activeElement;
    const lost = !cur || cur === body || !scopeRoot().contains(cur) || !usable(cur);
    if ((!wasOn || lost) && ["A", "up", "down", "left", "right"].includes(name)) { focusEl(entry(scopeRoot())); return true; }
    const act = ACTIONS[name];
    return act ? act() : false;
  }

  const prev = {};                          // button -> was down at the last frame
  const due = {};                           // held direction -> when it repeats next
  let raf = 0;

  function poll(now) {
    raf = requestAnimationFrame(poll);
    const pads = (navigator.getGamepads ? Array.from(navigator.getGamepads()) : []).filter((p) => p && p.connected);
    const gp = pads.find((p) => p.mapping === "standard") || pads[0];
    if (!gp) return;
    const down = (i) => !!(gp.buttons[i] && (gp.buttons[i].pressed || gp.buttons[i].value > 0.5));
    const ax = (i) => (gp.axes && gp.axes[i]) || 0;
    const held = {
      up: down(12) || ax(1) < -STICK, down: down(13) || ax(1) > STICK, left: down(14) || ax(0) < -STICK, right: down(15) || ax(0) > STICK,
    };
    for (const d of Object.keys(held)) {
      if (!held[d]) { due[d] = 0; continue; }
      if (!due[d]) { press(d); due[d] = now + FIRST_DELAY; }
      else if (now >= due[d]) { press(d); due[d] = now + REPEAT; }
    }
    for (const i of [0, 1, 2, 3, 4, 5, 8, 9]) {
      const d = down(i);
      if (d && !prev[i]) press(NAMES[i]);
      prev[i] = d;
    }
    for (const i of [6, 7]) {                // the triggers page up / down, and again while held
      const d = down(i);
      if (d && (!prev[i] || now >= (due[i] || 0))) { press(NAMES[i]); due[i] = now + 300; }
      if (!d) due[i] = 0;
      prev[i] = d;
    }
    const ry = ax(3);
    if (Math.abs(ry) > 0.2) { setPad(true); scroller(scopeRoot()).scrollBy({ top: ry * 22 }); }
  }
  function startPolling() { if (!raf) raf = requestAnimationFrame(poll); }
  window.addEventListener("gamepadconnected", startPolling);
  if (navigator.getGamepads && Array.from(navigator.getGamepads()).some(Boolean)) startPolling();

  // ------------------------------------------------------------------------------------------------ keys, mouse and touch
  const ARROWS = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" };
  document.addEventListener("keydown", (e) => {                       // (Steam Input as a keyboard: arrows move, Enter and Escape already work)
    if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || !ARROWS[e.key] || !(padOn || DECK)) return;
    const t = e.target;
    const vertical = e.key === "ArrowUp" || e.key === "ArrowDown";
    // (the arrows belong to the control while they mean something in it: a line of text moves left and right, a list, a number and a
    // slider change - but up and down in one line of text mean nothing, so those leave it)
    if (t && t.matches && (t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.matches('input[type="range"], input[type="number"]'))) return;
    if (t && t.matches && isText(t) && !vertical) return;
    if (!vertical && t && t.closest && t.closest('[role="tablist"]')) return;
    e.preventDefault();
    press(ARROWS[e.key]);
  });
  document.addEventListener("mousemove", (e) => { if (e.movementX || e.movementY) setPad(false); }, { passive: true });
  document.addEventListener("pointerdown", (e) => { if (e.pointerType !== "") setPad(false); }, true);

  window.RomorgDeck = { deck: DECK, press, move, activate, back, tabs, hints: () => strip.textContent, pad: () => padOn };
  renderHints();
})();
