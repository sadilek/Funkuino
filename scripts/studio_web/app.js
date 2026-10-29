"use strict";
// @ts-check
// Type-check (run alongside `node --check app.js`):
//   npx --yes typescript@latest tsc --noEmit --allowJs --checkJs --target es2022 --lib es2022,dom app.js
/* Funkuino Studio frontend.
 *
 * One WebSocket carries all server->client events (each with a global monotonic
 * `seq`); every mutation goes through REST. On (re)connect we refetch /api/state
 * and replay per-session agent events via ?since=<lastSeq>, applying everything
 * idempotently by seq. Sections below:
 *   1. small DOM/format helpers        6. Sync tab
 *   2. REST client                     7. Karten tab
 *   3. global state + WS               8. Agent tab (sessions, events, cards)
 *   4. tabs + toasts                   9. init
 *   5. Bibliothek tab
 */

/* ===== Contract types (HTTP/WS shapes shared with the backend) ===== */
/**
 * @typedef {"song"|"album"|"folge"|"hoerspiel"|"other"} UnitKind
 * @typedef {{ rel?: string, status: "present"|"missing" }} Cover
 * @typedef {{ id: string, kind: UnitKind, series?: string, title?: string, files?: number,
 *   bytes?: number, mtime?: number, cover?: Cover,
 *   sync?: "synced"|"pending"|"partial"|"unsynced"|"unknown",
 *   print?: "printed"|"changed"|"new"|"no_cover", cards?: string[] }} Unit
 * @typedef {{ host?: string, deviceId?: string, fileMtime?: number }} SyncManifest
 * @typedef {{ reachable?: boolean, host?: string|null, info?: any }} Device
 * @typedef {"running"|"waiting_user"|"waiting_input"|"done"|"error"} AgentStatus
 * @typedef {{ id: string, label?: string, model?: string, status?: AgentStatus, progress?: IntakeProgress|null }} SessionInfo
 * @typedef {{ sync?: boolean, cards?: boolean, unitSync?: string[] }} Jobs
 * @typedef {{ available?: boolean, reason?: string, sessions?: SessionInfo[] }} AgentState
 * @typedef {{ playing?: boolean, unitId?: string|null, name?: string|null, pausePlay?: boolean }} Player
 * @typedef {{ path: string|null, mp3: boolean, hint: string|null }} ToolState
 * @typedef {{ units?: Unit[], syncManifest?: SyncManifest, device?: Device|null, jobs?: Jobs,
 *   agent?: AgentState, rfid?: { listening: boolean }, player?: Player|null,
 *   tools?: { ffmpeg?: ToolState }, prefs?: { librarySort?: "date"|"alpha" },
 *   cardsBacklog?: { newCovers?: number, perPage?: number } }} StatePayload
 * @typedef {{ phase: "probe"|"download"|"merge"|"done", item?: number, total?: number,
 *   title?: string, bytes?: number, bytesTotal?: number|null, ts?: number }} IntakeProgress
 * @typedef {{ id: string, fileOrUrl: string, playMode: number, unitId: string|null }} Assignment
 * @typedef {{ question?: string, header?: string, multiSelect?: boolean,
 *   options?: { label: string, description?: string }[] }} Question
 * @typedef {{ rel: string, printed: "printed"|"new"|"changed", birthtime?: number, size?: number, mtime?: number }} CoverInfo
 * @typedef {{k:"user",text?:string} | {k:"text",text?:string}
 *   | {k:"tool",name?:string,summary?:string} | {k:"tool_result",summary?:string}
 *   | {k:"question",requestId:string,questions:Question[]}
 *   | {k:"permission",requestId:string,tool?:string,input?:any,summary?:string,pattern?:string|null}
 *   | {k:"status",status:AgentStatus} | {k:"result",costUsd?:number,turns?:number,error?:string}} AgentEvent
 * @typedef {(
 *   {t:"sync.log",line?:string} | {t:"sync.done",stats?:any} | {t:"sync.error",msg?:string}
 *   | {t:"cards.log",line?:string} | {t:"cards.done",code?:number}
 *   | {t:"unit.sync",id:string,status:"running"|"progress"|"done"|"error",pct?:number,line?:string,error?:string}
 *   | {t:"intake.progress",sessionId:string,progress:IntakeProgress}
 *   | {t:"rfid.card",id:string,known?:boolean,assignment?:Assignment|null}
 *   | {t:"rfid.assigned",tagId:string,unitId?:string} | {t:"rfid.unassigned",tagId:string,unitId?:string|null}
 *   | {t:"player",playing?:boolean,unitId?:string|null,name?:string|null,pausePlay?:boolean}
 *   | {t:"state.changed"} | {t:"agent.event",sessionId:string,event:AgentEvent}
 * ) & { seq?: number }} WsEvent
 */

/* =============================== 1. helpers =============================== */

/** @type {(sel: string, root?: Document|Element) => any} */
const $ = (sel, root = document) => root.querySelector(sel);
/** @type {(sel: string, root?: Document|Element) => any[]} */
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/**
 * @param {string} tag
 * @param {Record<string, any>|null} [attrs]
 * @param {...any} kids
 * @returns {any}
 */
function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k === "html") n.innerHTML = v;            // only ever fed sanitized strings
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids) {
    if (kid == null) continue;
    n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return n;
}

/** @param {*} s @returns {string} */
function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/** @param {*} n @returns {string} */
function fmtBytes(n) {
  n = Number(n) || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(1) + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
  if (n >= 1e3) return (n / 1e3).toFixed(0) + " KB";
  return n + " B";
}

/** @param {*} usd @returns {string} */
function fmtCost(usd) {
  const v = Number(usd);
  if (!isFinite(v)) return "";
  return v.toFixed(v < 0.01 ? 4 : 2).replace(".", ",") + " $";
}

/* Markdown-lite: ESCAPE first, then apply a safe subset (fenced/inline code,
 * **bold**, paragraphs, - / 1. lists). Returns an HTML string of trusted tags.
 * @param {string|null|undefined} src @returns {string} */
/** @param {string|null|undefined} src */
function mdLite(src) {
  const segments = String(src == null ? "" : src).split(/```/);
  let out = "";
  segments.forEach((seg, i) => {
    if (i % 2 === 1) {                                 // inside a fence
      const nl = seg.indexOf("\n");
      const body = nl >= 0 ? seg.slice(nl + 1) : seg;  // drop the language line
      out += "<pre><code>" + escapeHtml(body.replace(/\n$/, "")) + "</code></pre>";
      return;
    }
    for (const block of seg.split(/\n{2,}/)) {
      const t = block.trim();
      if (!t) continue;
      const lines = t.split("\n");
      const bullet = lines.every(l => /^\s*[-*]\s+/.test(l));
      const ordered = lines.every(l => /^\s*\d+[.)]\s+/.test(l));
      if (bullet || ordered) {
        const items = lines.map(l => "<li>" + inlineMd(l.replace(/^\s*(?:[-*]|\d+[.)])\s+/, "")) + "</li>").join("");
        out += ordered ? "<ol>" + items + "</ol>" : "<ul>" + items + "</ul>";
      } else {
        out += "<p>" + lines.map(inlineMd).join("<br>") + "</p>";
      }
    }
  });
  return out;
}
/** @param {string} line @returns {string} */
function inlineMd(line) {
  return escapeHtml(line)
    .replace(/`([^`]+)`/g, (_, c) => "<code>" + c + "</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

/* =============================== 2. REST =============================== */

/**
 * @param {string} path
 * @param {string} [method]
 * @param {any} [body]
 * @returns {Promise<any>}
 */
async function api(path, method = "GET", body) {
  /** @type {RequestInit & { headers: Record<string, string> }} */
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const res = await fetch(path, opt);
  let data = null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) { try { data = await res.json(); } catch { /* ignore */ } }
  if (!res.ok) {
    const err = /** @type {Error & { status?: number, data?: any }} */ (
      new Error((data && data.error) || res.statusText || ("HTTP " + res.status)));
    err.status = res.status; err.data = data;
    throw err;
  }
  return data;
}

/* =============================== 3. state + WS =============================== */

const State = {
  /** @type {StatePayload|null} */ data: null,   // last /api/state payload
  lastSeq: 0,                       // highest applied global seq
  /** @type {Set<number>} */ seen: new Set(),    // applied seqs (idempotency guard)
  syncRunning: false,
  cardsRunning: false,
  /** @type {Set<string>} */ unitSync: new Set(),        // unit ids currently uploading
  /** @type {Map<string, number>} */ unitSyncPct: new Map(), // unit id -> latest sync percent
  /** @type {Set<string>|null} */ knownUnitIds: null,    // prev fetch's ids; new ones flash
  /** @type {Player|null} */ player: null,               // now-playing on the device
  /** @type {"date"|"alpha"} */ sortMode: "date",        // library sort, persisted in config.json
  /** @type {{cards?: boolean, agent?: boolean}} */ features: {},  // server-side feature flags
  _reconnecting: false,
};

// Karten picker (integrated card chooser).
const Cards = {
  /** @type {CoverInfo[]} */ covers: [],
  perPage: 12,
  /** @type {string[]} */ selection: [],   // ordered rels
  loaded: false,
};

// UI-local set of unit ids queued for printing from the Bibliothek Druck column
// (independent of the Karten-tab picker).
/** @type {Set<string>} */
const PrintSel = new Set();

// Library download intake strips, keyed by agent session id.
/** @type {Map<string, any>} */
const Intakes = new Map();

/** @type {WebSocket|null} */
let ws = null;
let wsBackoff = 500;
/** @type {ReturnType<typeof setTimeout>|undefined} */
let stateTimer;

function connectWS() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  /** @type {WebSocket} */
  let sock;
  try { sock = new WebSocket(proto + "//" + location.host + "/ws"); }
  catch { scheduleReconnect(); return; }
  ws = sock;

  sock.onopen = async () => {
    wsBackoff = 500;
    await refreshState();
    await replayAgentEvents();        // catch up anything missed while disconnected
  };
  sock.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    handleWsEvent(m);
  };
  sock.onclose = scheduleReconnect;
  sock.onerror = () => { try { sock.close(); } catch { /* ignore */ } };
}
function scheduleReconnect() {
  if (State._reconnecting) return;
  State._reconnecting = true;
  setTimeout(() => { State._reconnecting = false; connectWS(); }, wsBackoff);
  wsBackoff = Math.min(wsBackoff * 1.8, 10000);
}

/** @param {WsEvent} m */
function handleWsEvent(m) {
  if (typeof m.seq === "number") {
    if (m.seq <= State.lastSeq || State.seen.has(m.seq)) return;   // already applied
    State.seen.add(m.seq);
    State.lastSeq = Math.max(State.lastSeq, m.seq);
  }
  switch (m.t) {
    case "sync.log":    appendLog("sync-log", m.line); break;
    case "sync.done":   onSyncDone(m.stats); break;
    case "sync.error":  appendLog("sync-log", m.msg, true); setSyncRunning(false); toast(m.msg || "Sync-Fehler"); break;
    case "cards.log":   appendLog("cards-log", m.line); break;
    case "cards.done":  onCardsDone(m.code); break;
    case "unit.sync":   handleUnitSyncEvent(m); break;
    case "intake.progress": handleIntakeProgress(m); break;
    case "rfid.card":   handleRfidCard(m); break;
    case "player":      handlePlayerEvent(m); break;
    case "rfid.assigned": invalidateAssignments(); break;   // client toasts on POST; refresh via state.changed
    case "rfid.unassigned": invalidateAssignments(); break; // same: refresh via state.changed
    case "state.changed": invalidateAssignments(); debouncedState(); break;
    case "agent.event": applyAgentEvent(m.sessionId, m.event, m.seq); break;
  }
}

function debouncedState() {
  clearTimeout(stateTimer);
  stateTimer = setTimeout(refreshState, 300);
}

/** @param {boolean} [refresh] */
async function refreshState(refresh) {
  try {
    const s = await api("/api/state" + (refresh ? "?refresh=1" : ""));
    State.data = s;
    // Restore per-unit sync rings from the server's authoritative job list.
    State.unitSync = new Set((s.jobs && s.jobs.unitSync) || []);
    for (const id of [...State.unitSyncPct.keys()]) if (!State.unitSync.has(id)) State.unitSyncPct.delete(id);
    State.player = s.player || null;
    State.features = s.features || {};
    State.sortMode = (s.prefs && s.prefs.librarySort) === "alpha" ? "alpha" : "date";
    updateSortToggleLabel();
    applyFeatureVisibility();
    renderDeviceChip(s.device);
    renderToolBanner(s.tools);
    renderLibrary();
    renderNowPlaying();
    renderSyncPanel(s);
    renderCardsPanel(s);
    setSyncTabSpinner(!!(s.jobs && s.jobs.sync));
    renderAgentAvailability(s.agent);
    reconcileSessions(s.agent && s.agent.sessions);
    if (isTabActive("karten")) loadCovers();
  } catch (/** @type {any} */ e) {
    // Keep the app alive; surface once.
    renderDeviceChip(null);
  }
}

/** @param {string} name */
function isTabActive(name) {
  const p = document.getElementById("tab-" + name);
  return !!(p && p.classList.contains("active"));
}

/* =============================== 4. tabs + toasts =============================== */

// Hides the Kartendruck tab (and, via renderLibrary, its Druck column) when the
// server was started with --no-cards, and the Agent tab plus the library
// toolbar's URL-download field when started with --no-agent -- the latter is
// a second entry point into the same agent-only /api/agent/sessions endpoint
// (startLibraryDownload), easy to overlook since it lives in the Bibliothek
// tab, not the Agent tab itself. Idempotent: safe to call on every state
// refresh, and bounces the user off a tab if it was active when disabled.
function applyFeatureVisibility() {
  const cardsOn = State.features.cards !== false;
  const agentOn = State.features.agent !== false;
  const tabBtn = $$(".tab").find(b => b.dataset.tab === "karten");
  if (tabBtn) tabBtn.hidden = !cardsOn;
  if (!cardsOn && isTabActive("karten")) activateTab("bibliothek");
  const agentBtn = $$(".tab").find(b => b.dataset.tab === "agent");
  if (agentBtn) agentBtn.hidden = !agentOn;
  if (!agentOn && isTabActive("agent")) activateTab("bibliothek");
  const libDl = document.querySelector(".lib-dl");
  if (libDl) libDl.hidden = !agentOn;
}

function initTabs() {
  $$(".tab").forEach(btn => btn.addEventListener("click", () => {
    $$(".tab").forEach(b => b.classList.toggle("active", b === btn));
    const name = btn.dataset.tab;
    $$(".tabpane").forEach(p => p.classList.toggle("active", p.id === "tab-" + name));
    if (name === "agent") scrollActiveTranscript();
    if (name === "karten") loadCovers();
  }));
}

/** @param {string} name */
function activateTab(name) {
  const btn = $$(".tab").find(b => b.dataset.tab === name);
  if (btn) btn.click();
}

/** @param {string} msg @param {string} [kind] */
function toast(msg, kind) {
  const t = el("div", { class: "toast" + (kind === "ok" ? " ok" : ""), text: msg });
  $("#toasts").append(t);
  setTimeout(() => { t.style.opacity = "0"; setTimeout(() => t.remove(), 250); }, 4200);
}

/** @param {string} id @param {any} line @param {boolean} [isErr] */
function appendLog(id, line, isErr) {
  const pane = document.getElementById(id);
  if (!pane) return;
  const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 40;
  pane.append(el("div", { class: isErr ? "err" : null, text: String(line == null ? "" : line) }));
  if (atBottom) pane.scrollTop = pane.scrollHeight;
}

/* =============================== 5. Bibliothek =============================== */

/** @type {Record<string, string>} */
const KIND_LABEL = { song: "Lied", album: "Album", folge: "Folge", hoerspiel: "Hörspiel", other: "Sonstiges" };
// German phonebook order (DIN 5007-2): ä/ö/ü sort as "ae"/"oe"/"ue", not as
// plain a/o/u (that would be dictionary order, DIN 5007-1). `numeric: true`
// additionally makes "Folge 2" sort before "Folge 10".
const deCollator = new Intl.Collator("de-u-co-phonebk", { sensitivity: "base", numeric: true });

/** @param {Unit} unit */
function thumbFor(unit) {
  const rel = unit.cover && unit.cover.rel;
  if (rel && unit.cover && unit.cover.status === "present") {
    return el("img", { class: "thumb", loading: "lazy", src: "/thumb?rel=" + encodeURIComponent(rel) + "&px=72", alt: "" });
  }
  return el("div", { class: "thumb ph", text: "♪", title: "Kein Cover" });
}

// Determinate circular progress ring for an in-flight unit sync — a plain SVG
// (deterministic in every browser): a faint full track circle plus one accent
// arc via stroke-dashoffset, starting at 12 o'clock. No pct yet → indeterminate
// (a short arc that CSS-rotates). All numbers below are ours (safe innerHTML).
const RING_C = 2 * Math.PI * 10;   // circumference of the r=10 circle (~62.83)
const RING_SPIN_MS = 900;          // must match .ring.indet svg animation duration
/** @param {string} id */
function syncRing(id) {
  const pct = State.unitSyncPct.get(id);
  const indet = pct == null;
  const p = indet ? 0 : Math.max(0, Math.min(100, Math.round(pct)));
  const arc = indet
    ? `stroke-dasharray="${(RING_C * 0.28).toFixed(2)} ${RING_C.toFixed(2)}"`
    : `stroke-dasharray="${RING_C.toFixed(2)}" stroke-dashoffset="${(RING_C * (1 - p / 100)).toFixed(2)}"`;
  const svg = '<svg viewBox="0 0 22 22" width="22" height="22" aria-hidden="true">'
    + '<circle class="ring-track" cx="11" cy="11" r="10" fill="none" stroke-width="2"/>'
    + '<circle class="ring-arc" cx="11" cy="11" r="10" fill="none" stroke-width="2" stroke-linecap="round"'
    + ` transform="rotate(-90 11 11)" ${arc}/>`
    + '</svg>';
  const ring = el("span", { class: "ring" + (indet ? " indet" : ""), "data-ring-id": id,
    title: indet ? "Wird übertragen…" : p + " %", html: svg });
  if (indet) {
    // Phase-lock the spin to a global clock: a negative delay = how far the
    // virtual cycle has advanced, so a re-created element continues smoothly
    // instead of restarting at 0 (fixes the "quarter turn then snap back").
    const s = ring.querySelector("svg");
    if (s) s.style.animationDelay = "-" + (performance.now() % RING_SPIN_MS).toFixed(0) + "ms";
  }
  return ring;
}

// A status cell = one square checkbox-style box. Variants:
//   ok   → filled ✓   warn → outlined ⟳   todo → empty box   na → muted –
/** @param {string} variant @param {string} title @param {{count?: number}} [opts] */
function boxCell(variant, title, opts) {
  const glyph = variant === "ok" ? "✓" : variant === "warn" ? "⟳" : variant === "na" ? "–" : "";
  const box = el("span", { class: "box " + variant, title, text: glyph });
  if (opts && opts.count && opts.count > 1) box.append(el("span", { class: "box-count", text: String(opts.count) }));
  return el("td", { class: "col-pipe" }, box);
}

/** @param {string} kind @param {Unit} unit */
function pipeCell(kind, unit) {
  if (kind === "download") {
    const parts = (unit.files || 1) + " Datei" + ((unit.files || 1) === 1 ? "" : "en");
    return boxCell("ok", "Heruntergeladen · " + parts + " · " + fmtBytes(unit.bytes));
  }
  if (kind === "cover") {
    const present = unit.cover && unit.cover.status === "present";
    return boxCell(present ? "ok" : "todo", present ? "Cover vorhanden" : "Cover fehlt");
  }
  if (kind === "sync") return syncCell(unit);
  if (kind === "print") return printCell(unit);
  return cardCell(unit);
}

// Sync box doubles as the per-unit upload control: not-synced states morph into
// an upload affordance on hover/focus and trigger POST /api/units/sync on click;
// a determinate ring shows while running; synced ✓ stays non-interactive.
/** @param {Unit} unit */
function syncCell(unit) {
  if (State.unitSync.has(unit.id)) return el("td", { class: "col-pipe" }, syncRing(unit.id));
  /** @type {Record<string, string[]>} */
  const map = {
    synced:   ["ok",   "✓", "Auf dem Gerät"],
    pending:  ["warn", "⟳", "Übertragung ausstehend"],
    partial:  ["warn", "⟳", "Teilweise übertragen"],
    unsynced: ["todo", "",  "Nicht auf dem Gerät"],
    unknown:  ["na",   "–", "Kein Sync-Manifest"],
  };
  const m = map[unit.sync || ""] || ["na", "–", "Unbekannt"];
  if (unit.sync === "synced") return boxCell("ok", m[2]);
  const box = el("button", {
    class: "box " + m[0] + " clickable upload",
    title: m[2] + " · Klick: auf das Gerät hochladen",
    onclick: (/** @type {any} */ e) => { e.stopPropagation(); syncUnit(unit.id); },
  },
    el("span", { class: "glyph-status", text: m[1] }),
    el("span", { class: "glyph-upload", text: "↑" }));   // text-presentation arrow (stable metrics)
  return el("td", { class: "col-pipe" }, box);
}

// Druck box doubles as an inline print picker: click toggles "queued for print".
// Selected uses an accent (persimmon) fill — never green, since green = printed.
/** @param {Unit} unit */
function printCell(unit) {
  const hasCover = unit.cover && unit.cover.status === "present";
  const selected = PrintSel.has(unit.id);
  let variant, glyph, title;
  if (selected) { variant = "sel"; glyph = "✓"; title = "Zum Druck ausgewählt"; }
  else {
    /** @type {Record<string, string[]>} */
    const map = {
      printed:  ["ok",   "✓", "Gedruckt"],
      changed:  ["warn", "⟳", "Cover geändert seit Druck"],
      new:      ["todo", "",  "Noch nicht gedruckt"],
      no_cover: ["na",   "–", "Kein Cover zum Drucken"],
    };
    const m = map[unit.print || ""] || ["na", "–", "Unbekannt"];
    variant = m[0]; glyph = m[1]; title = m[2];
  }
  const box = el("button", {
    class: "box " + variant + (hasCover ? " clickable" : ""),
    title: hasCover ? title + " · Klick: zum Druck aus-/abwählen" : title,
    disabled: !hasCover,
    onclick: (/** @type {any} */ e) => { e.stopPropagation(); togglePrintSel(unit); },
  }, glyph);
  return el("td", { class: "col-pipe" }, box);
}

// Karte box is the inline RFID assign/unassign control (see section 8c).
/** @param {Unit} unit */
function cardCell(unit) {
  const cards = unit.cards;
  if (cards == null) return boxCell("na", "RFID-Status unbekannt");
  const assigned = cards.length > 0;
  const pulse = !assigned && !!RFID.active;   // highlight click targets while a card waits
  let variant = assigned ? "ok" : "todo";
  const title = assigned
    ? "Karte" + (cards.length > 1 ? "n" : "") + ": " + cards.join(", ") + " · Klick: Zuordnung aufheben"
    : (RFID.active ? "Aktive Karte hier zuordnen" : "Keiner Karte zugeordnet");
  const box = el("button", {
    class: "box " + variant + " clickable" + (pulse ? " pulse" : ""),
    title, onclick: (/** @type {any} */ e) => { e.stopPropagation(); onCardBoxClick(unit); },
  }, assigned ? "✓" : "");
  if (assigned && cards.length > 1) box.append(el("span", { class: "box-count", text: String(cards.length) }));
  // Hover/focus opens the play-mode popover (assigned units, or empty+active card).
  box.addEventListener("mouseenter", () => showModePopover(box, unit));
  box.addEventListener("mouseleave", schedulePopoverHide);
  box.addEventListener("focus", () => showModePopover(box, unit));
  box.addEventListener("blur", schedulePopoverHide);
  return el("td", { class: "col-pipe" }, box);
}

/** Both library tables share these widths. The column heads live in their own
 *  table above the scroller (so the scrollbar starts at the first row and the
 *  head's bottom border cannot scroll away), which only lines up with the rows
 *  if both use `table-layout: fixed` and the same columns. */
function libColgroup() {
  // Thumb column: the 44px image plus its 14px left padding — with a fixed
  // layout the column no longer grows to fit them and the cover would collide
  // with the title.
  const cardsOn = State.features.cards !== false;
  const pipeCols = cardsOn ? [56, 56, 56, 56, 56] : [56, 56, 56, 56];  // Down/Cover/Sync/(Druck)/Karte
  const cols = [...pipeCols, 58, null, 40];
  return el("colgroup", null, ...cols.map(w =>
    el("col", w === null ? null : { style: `width:${w}px` })));
}

function renderLibrary() {
  const head = $("#lib-head");
  const body = $("#lib-body");
  const s = State.data;
  const units = (s && s.units) || [];
  const q = ($("#lib-search").value || "").trim().toLowerCase();

  const totalBytes = units.reduce((a, u) => a + (u.bytes || 0), 0);
  $("#lib-totals").textContent = `${units.length} Titel · ${fmtBytes(totalBytes)}`;

  // Prune print selection of units that vanished after a refetch (stale count),
  // before any early return so the floating bar can't outlive its units.
  const liveIds = new Set(units.map(u => u.id));
  let printChanged = false;
  for (const id of [...PrintSel]) if (!liveIds.has(id)) { PrintSel.delete(id); printChanged = true; }
  if (printChanged) renderPrintBar();

  const filtered = q ? units.filter(u =>
    ((u.title || "") + " " + (u.series || "") + " " + (u.id || "")).toLowerCase().includes(q)) : units;

  body.replaceChildren();
  head.replaceChildren();
  if (!units.length) {
    body.append(el("div", { class: "empty", text: "Die Bibliothek ist leer. Lade Titel mit dem Agenten oder ./download." }));
    return;
  }
  if (!filtered.length) {
    body.append(el("div", { class: "empty", text: "Kein Titel passt zu „" + q + "“." }));
    return;
  }

  // Group by series (fallback: kind label).
  const groups = new Map();
  for (const u of filtered) {
    const key = u.series || KIND_LABEL[u.kind] || "Sonstiges";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(u);
  }
  const LOOSE_GROUP = KIND_LABEL.other;  // "Sonstiges": top-level mp3s with no series

  let ordered;
  if (State.sortMode === "alpha") {
    // Folders alphabetical first (German phonebook order), the loose
    // "Sonstiges" bucket always last.
    ordered = Array.from(groups.entries()).sort((a, b) => {
      const aLoose = a[0] === LOOSE_GROUP, bLoose = b[0] === LOOSE_GROUP;
      if (aLoose !== bLoose) return aLoose ? 1 : -1;
      return deCollator.compare(a[0], b[0]);
    });
  } else {
    // Original default: groups newest-first.
    const groupMtime = (/** @type {Unit[]} */ arr) => Math.max(...arr.map(u => u.mtime || 0));
    ordered = Array.from(groups.entries()).sort((a, b) => groupMtime(b[1]) - groupMtime(a[1]));
  }

  // Flash rows whose id wasn't in the previous fetch (e.g. a fresh download).
  const prev = State.knownUnitIds;
  const isNew = (/** @type {string} */ id) => prev !== null && !prev.has(id);

  // Row order (per user feedback): status block LEFT, then thumbnail, then title.
  const syncHost = (s && s.syncManifest && s.syncManifest.host) || "";
  const cardsOn = State.features.cards !== false;
  const colCount = cardsOn ? 8 : 7;  // grouprow colspan must track the pipe-cell count below
  const headCells = [
    el("th", { class: "col-pipe", text: "Down", title: "Audio heruntergeladen" }),
    el("th", { class: "col-pipe", text: "Cover", title: "Titelbild für die Karte" }),
    el("th", { class: "col-pipe", text: "Sync", title: syncHost ? "Gerät: " + syncHost : "Auf dem Gerät" }),
  ];
  if (cardsOn) {
    headCells.push(el("th", { class: "col-pipe", text: "Druck", title: "RFID-Karte gedruckt" }));
  }
  headCells.push(
    el("th", { class: "col-pipe", text: "Karte", title: "RFID-Karte zugeordnet" }),
    el("th", { class: "col-thumb", text: "Titel", colspan: "2" }),
    el("th", { class: "col-play" }),
  );
  const headTable = el("table", { class: "lib-table" });
  headTable.append(libColgroup());
  headTable.append(el("thead", null, el("tr", null, ...headCells)));
  head.append(headTable);

  const table = el("table", { class: "lib-table" });
  table.append(libColgroup());
  // "active" = the device has a loaded track, playing OR paused (backend sends a
  // paused track as playing:false + pausePlay:true; stopped clears both).
  const pl = State.player;
  const playingId = (pl && (pl.playing || pl.pausePlay) && pl.unitId) || null;
  const tbody = el("tbody");
  for (const [name, arr] of ordered) {
    if (State.sortMode === "alpha") {
      // Within a series group, subfolder-based units ("folge", themselves a
      // folder of files) sort before direct-file units ("hoerspiel", a single
      // mp3 straight in the series folder) — folders first, titles after, as
      // for other kinds (song/album/other) every item shares the same kind so
      // this comparison is a no-op and only the title compare below applies.
      arr.sort((/** @type {Unit} */ a, /** @type {Unit} */ b) => {
        const aFolder = a.kind === "folge", bFolder = b.kind === "folge";
        if (aFolder !== bFolder) return aFolder ? -1 : 1;
        return deCollator.compare(a.title || a.id || "", b.title || b.id || "");
      });
    } else {
      arr.sort((/** @type {Unit} */ a, /** @type {Unit} */ b) => (b.mtime || 0) - (a.mtime || 0));
    }
    tbody.append(el("tr", { class: "grouprow" },
      el("td", { colspan: String(colCount) },
        document.createTextNode(name + "  "),
        el("span", { class: "gcount", text: arr.length + " Titel" }))));
    for (const u of arr) {
      const playing = u.id === playingId;
      const rowCells = [pipeCell("download", u), pipeCell("cover", u), pipeCell("sync", u)];
      if (cardsOn) rowCells.push(pipeCell("print", u));
      rowCells.push(pipeCell("card", u));
      tbody.append(el("tr", { class: "unitrow" + (isNew(u.id) ? " flash-new" : "") + (playing ? " playing" : "") },
        ...rowCells,
        el("td", { class: "col-thumb" }, thumbFor(u)),
        el("td", null,
          el("div", { class: "unit-title", text: u.title || u.id || "ohne Titel" }),
          el("div", { class: "unit-sub" },
            el("span", { class: "badge", text: KIND_LABEL[u.kind] || u.kind || "?" }),
            document.createTextNode("  " + fmtBytes(u.bytes)))),
        el("td", { class: "col-play" }, playCell(u, playing))));
    }
  }
  table.append(tbody);
  body.append(table);

  // Remember the full id set (not the filtered view) for the next diff.
  State.knownUnitIds = liveIds;
}

// -- print selection (Druck column → floating print bar) --
/** @param {Unit} unit */
function togglePrintSel(unit) {
  if (!(unit.cover && unit.cover.status === "present")) return;
  if (PrintSel.has(unit.id)) PrintSel.delete(unit.id); else PrintSel.add(unit.id);
  renderLibrary();
  renderPrintBar();
}
function renderPrintBar() {
  const bar = $("#print-bar");
  if (!bar) return;
  const n = PrintSel.size;
  bar.hidden = n === 0;
  const c = $("#print-bar-count");
  if (c) c.textContent = n + (n === 1 ? " Karte ausgewählt" : " Karten ausgewählt");
  const go = $("#print-bar-go");
  if (go) go.disabled = State.cardsRunning || n === 0;
}
function clearPrintSel() { PrintSel.clear(); renderLibrary(); renderPrintBar(); }
async function printSelected() {
  if (!PrintSel.size || State.cardsRunning) return;
  const rels = ((State.data && State.data.units) || [])
    .filter(u => PrintSel.has(u.id) && u.cover && u.cover.status === "present")
    .map(u => /** @type {string} */ (u.cover && u.cover.rel));
  if (!rels.length) return;
  try {
    await api("/api/cards/render", "POST", { rels });
    $("#cards-log").replaceChildren();
    setCardsRunning(true);
    PrintSel.clear();
    renderLibrary(); renderPrintBar();
    toast("Druck gestartet", "ok");
  } catch (/** @type {any} */ e) {
    if (e.status === 409) toast("Es läuft bereits ein Karten-Vorgang.");
    else toast("Drucken fehlgeschlagen: " + e.message);
  }
}

// -- play / stop (right-side control; playing row shows a persistent Stop) --
/** @param {Unit} u @param {boolean} playing */
function playCell(u, playing) {
  if (playing) {
    const paused = !!(State.player && State.player.pausePlay);
    return el("button", {
      class: "play-btn stop" + (paused ? " paused" : ""),
      title: paused ? "Pausiert · zum Stoppen klicken" : "Spielt gerade · zum Stoppen klicken",
      onclick: (/** @type {any} */ e) => { e.stopPropagation(); stopPlayer(); },
    }, "■");
  }
  return el("button", {
    class: "play-btn play",
    title: "Auf dem Gerät abspielen",
    onclick: (/** @type {any} */ e) => { e.stopPropagation(); playUnit(u.id); },
  }, "▶");
}

/** @param {string} id */
async function playUnit(id) {
  try { await api("/api/units/play", "POST", { id }); }   // player event repaints the row
  catch (/** @type {any} */ e) {
    if (e.status === 503) toast("Gerät nicht erreichbar.");
    else if (e.status === 409) toast("Es läuft gerade eine andere Aktion.");
    else toast("Abspielen fehlgeschlagen: " + e.message);
  }
}
async function stopPlayer() {
  try { await api("/api/player/stop", "POST"); }
  catch (/** @type {any} */ e) {
    if (e.status === 503) toast("Gerät nicht erreichbar.");
    else toast("Stopp fehlgeschlagen: " + e.message);
  }
}

/** @param {any} m */
function handlePlayerEvent(m) {
  State.player = { playing: m.playing, unitId: m.unitId, name: m.name, pausePlay: m.pausePlay };
  if (State.data) State.data.player = State.player;
  renderLibrary();
  renderNowPlaying();
}

// Header chip: only when playing an unmatched track (no row indicator to show it).
function renderNowPlaying() {
  const chip = $("#now-playing");
  if (!chip) return;
  const p = State.player;
  const active = !!(p && (p.playing || p.pausePlay));   // playing or paused
  const show = active && !p.unitId;                     // only when no row can show it
  chip.hidden = !show;
  if (!show) { chip.replaceChildren(); return; }
  chip.classList.toggle("paused", !!p.pausePlay);
  const name = (p.name || "").replace(/^\(\d+\/\d+\):\s*/, "") || "unbekannt";   // drop "(N/M): " prefix
  chip.replaceChildren(
    el("span", { class: "np-glyph", text: p.pausePlay ? "⏸" : "▶" }),
    el("span", { class: "np-name", text: (p.pausePlay ? "pausiert: " : "spielt: ") + name }),
    el("button", { class: "np-stop", title: "Stopp", text: "■",
      onclick: (/** @type {any} */ e) => { e.stopPropagation(); stopPlayer(); } }));
}

/**
 * ffmpeg is the only thing Funkuino needs but cannot bring along outside the
 * macOS app. Every download and every merge dies without it, so the state of it
 * is shown as a persistent bar rather than as a toast at the moment of failure.
 * The install command is a <code> the user can select in one click.
 * @param {{ffmpeg?: {path: string|null, mp3: boolean, hint: string|null}}|undefined} tools
 */
function renderToolBanner(tools) {
  const bar = $("#tool-banner");
  const ff = tools && tools.ffmpeg;
  if (!ff || (ff.path && ff.mp3)) { bar.hidden = true; return; }
  const what = ff.path
    ? "ffmpeg kann keine MP3s erzeugen (libmp3lame fehlt)."
    : "ffmpeg fehlt.";
  bar.replaceChildren(
    el("span", { class: "tb-what", text: what }),
    el("span", { text: "Download und Zusammenführen funktionieren erst danach." }),
    ...(ff.hint ? [el("code", { text: ff.hint })] : []));
  bar.hidden = false;
}

/* =============================== 6. Sync =============================== */

/** @param {Device|null} dev */
function renderDeviceChip(dev) {
  const chip = $("#device-chip");
  const online = !!(dev && dev.reachable);
  chip.classList.toggle("online", online);
  chip.classList.toggle("offline", !online);
  const host = (dev && (dev.host || (dev.info && (dev.info.hostname || dev.info.host)))) ||
    (State.data && State.data.syncManifest && State.data.syncManifest.host) || "ESPuino";
  $("#device-chip-text").textContent = host + (online ? " · online" : " · offline");
}

/** @param {StatePayload} s */
function renderSyncPanel(s) {
  setSyncRunning(!!(s.jobs && s.jobs.sync));
  const info = (s.device && s.device.info) || null;
  const dl = $("#device-info");
  dl.replaceChildren();
  if (!info) {
    dl.append(el("div", { class: "subtle", style: "grid-column:1/3", text: "Noch keine Geräteinfo. „Ping“ drücken." }));
    return;
  }
  // ESPuino /info nests everything: {software:{version,branch}, hardware:{model},
  // memory:{freeHeap}, wifi:{ip,macAddress,rssi}, battery:{chargeLevel,currVoltage}}
  const sw = info.software || {}, hw = info.hardware || {}, mem = info.memory || {};
  const wifi = info.wifi || {}, bat = info.battery || {};
  const version = String(sw.version || "").replace(/^Software-revision:\s*/i, "");
  const rows = [
    ["Status", (s.device && s.device.reachable) ? "online" : "offline (letzter Stand)"],
    ["Host", s.device && s.device.host],
    ["IP", wifi.ip],
    ["WLAN", wifi.rssi != null ? wifi.rssi + " dBm" : null],
    ["Software", version ? version + (sw.branch ? " (" + sw.branch + ")" : "") : null],
    ["Board", hw.model],
    ["Heap frei", mem.freeHeap != null ? fmtBytes(mem.freeHeap) : null],
    ["Akku", bat.chargeLevel != null
      ? Math.round(bat.chargeLevel) + " %" + (bat.currVoltage != null ? " · " + Number(bat.currVoltage).toFixed(2) + " V" : "")
      : null],
  ];
  for (const [k, v] of rows) {
    if (v == null || v === "") continue;
    dl.append(el("dt", { text: k }), el("dd", { text: String(v) }));
  }
}

/** @param {boolean} running */
function setSyncRunning(running) {
  State.syncRunning = running;
  ["sync-dry", "sync-run", "sync-del"].forEach(id => { const b = /** @type {HTMLButtonElement|null} */ (document.getElementById(id)); if (b) b.disabled = running; });
  setSyncTabSpinner(running);
}

/** @param {boolean} running */
function setSyncTabSpinner(running) {
  const s = $("#sync-tab-spin");
  if (s) s.hidden = !running;
}

// -- targeted per-unit sync --
/** @param {string} id */
async function syncUnit(id) {
  if (State.unitSync.has(id)) return;
  State.unitSync.add(id);         // optimistic spinner; unit.sync events confirm
  renderLibrary();
  try {
    await api("/api/units/sync", "POST", { id });
  } catch (/** @type {any} */ e) {
    State.unitSync.delete(id);
    renderLibrary();
    if (e.status === 409) toast("Ein Sync läuft bereits.");
    else toast("Übertragung fehlgeschlagen: " + e.message);
  }
}

/** @param {string} id @returns {any} */
function findSyncRing(id) {
  for (const el of document.querySelectorAll(".ring")) {
    const r = /** @type {HTMLElement} */ (el);
    if (r.dataset && r.dataset.ringId === id) return r;
  }
  return null;
}
// Update a determinate ring's arc IN PLACE (no element re-creation), so frequent
// progress events don't rebuild the row / restart animations.
/** @param {any} ring @param {number} pct */
function updateRingArc(ring, pct) {
  const p = Math.max(0, Math.min(100, Math.round(pct)));
  const arc = ring.querySelector(".ring-arc");
  if (!arc) return;
  ring.classList.remove("indet");
  arc.setAttribute("stroke-dasharray", RING_C.toFixed(2));
  arc.setAttribute("stroke-dashoffset", (RING_C * (1 - p / 100)).toFixed(2));
  ring.setAttribute("title", p + " %");
}

/** @param {any} m */
function handleUnitSyncEvent(m) {
  if (!m || !m.id) return;
  const id = m.id;
  if (m.status === "running" || m.status === "progress") {
    const wasRunning = State.unitSync.has(id);
    State.unitSync.add(id);
    if (typeof m.pct === "number") {
      const ring = findSyncRing(id);
      const wasDeterminate = ring && !ring.classList.contains("indet");
      State.unitSyncPct.set(id, m.pct);
      if (wasDeterminate) { updateRingArc(ring, m.pct); return; }   // in place, no rebuild
      renderLibrary();                                              // create / flip indet→determinate once
    } else {
      // Running with no pct: only build the row if the (indeterminate) ring
      // isn't there yet — otherwise leave it spinning (don't restart it).
      if (!wasRunning || !findSyncRing(id)) renderLibrary();
    }
  } else if (m.status === "done") {
    State.unitSync.delete(id);
    State.unitSyncPct.delete(id);
    renderLibrary();               // backend also emits state.changed → ✓ on refetch
  } else if (m.status === "error") {
    State.unitSync.delete(id);
    State.unitSyncPct.delete(id);
    renderLibrary();
    toast(m.error || "Übertragung fehlgeschlagen.");
  }
}

/** @param {any} stats */
function onSyncDone(stats) {
  setSyncRunning(false);
  if (stats && typeof stats === "object") {
    const bits = [];
    if (stats.uploaded != null) bits.push(stats.uploaded + " hochgeladen");
    if (stats.deleted != null) bits.push(stats.deleted + " gelöscht");
    if (stats.skipped != null) bits.push(stats.skipped + " übersprungen");
    if (stats.bytes != null) bits.push(fmtBytes(stats.bytes));
    $("#sync-stats").textContent = "Fertig: " + (bits.join(" · ") || "keine Änderungen");
  } else {
    $("#sync-stats").textContent = "Sync fertig.";
  }
  toast("Sync abgeschlossen", "ok");
}

/** @param {boolean} dryRun @param {boolean} del */
async function startSync(dryRun, del) {
  if (State.syncRunning) return;
  // A real delete needs confirmation; a dry-run with delete only previews it.
  if (del && !dryRun && !confirm("Sync mit Löschen entfernt Dateien auf dem Gerät, die lokal fehlen. Fortfahren?")) return;
  $("#sync-log").replaceChildren();
  $("#sync-stats").textContent = "";
  setSyncRunning(true);
  try {
    await api("/api/sync", "POST", { dryRun: !!dryRun, delete: !!del, confirm: !!del });
    appendLog("sync-log", (dryRun ? "Dry-Run" : del ? "Sync mit Löschen" : "Sync") + " gestartet…");
  } catch (/** @type {any} */ e) {
    setSyncRunning(false);
    if (e.status === 409) toast("Es läuft bereits ein Sync.");
    else toast("Sync-Start fehlgeschlagen: " + e.message);
  }
}

async function pingDevice() {
  const chip = $("#device-chip");
  chip.style.opacity = "0.6";
  try {
    const resp = await api("/api/device/ping", "POST");
    const info = (resp && resp.info) || resp;
    const host = (State.data && State.data.device && State.data.device.host) || null;
    renderDeviceChip({ reachable: true, info, host });
    if (State.data) { State.data.device = { reachable: true, info, host }; renderSyncPanel(State.data); }
    toast("Gerät erreichbar", "ok");
  } catch (/** @type {any} */ e) {
    // Flip reachability in the shared state and re-render the panel from it
    // (keep the cached info/host); the chip + Status row must agree.
    const dev = (State.data && State.data.device) || {};
    if (State.data) {
      State.data.device = { reachable: false, info: dev.info, host: dev.host };
      renderSyncPanel(State.data);
    }
    renderDeviceChip({ reachable: false, info: dev.info, host: dev.host });
    toast("Gerät nicht erreichbar");
  } finally { chip.style.opacity = ""; }
}

/* =============================== 7. Karten =============================== */

/** @param {StatePayload} s */
function renderCardsPanel(s) {
  const b0 = s.cardsBacklog || {};
  State.cardsBacklogEmpty = b0.newCovers === 0;
  setCardsRunning(!!(s.jobs && s.jobs.cards));
  const b = s.cardsBacklog || {};
  const n = b.newCovers != null ? b.newCovers : null;
  State.cardsBacklogEmpty = n === 0;
  const per = b.perPage || 12;
  $("#backlog-num").textContent = n != null ? n : "–";
  if (n == null) {
    $("#backlog-word").textContent = "Anzahl unbekannt";
    $("#backlog-fill").style.width = "0%";
    $("#backlog-cap").textContent = `${per} pro Seite`;
    return;
  }
  if (n === 0) {
    $("#backlog-word").textContent = "alles gedruckt";
    $("#backlog-fill").style.width = "0%";
    $("#backlog-cap").textContent = "Keine neuen Cover.";
    return;
  }
  const fullPages = Math.floor(n / per);
  const rest = n % per;
  $("#backlog-word").textContent = "noch nicht gedruckte Cover";
  $("#backlog-fill").style.width = (rest === 0 ? 100 : (rest / per) * 100).toFixed(0) + "%";
  const parts = [`${per} pro Seite`];
  if (fullPages > 0) parts.push(`${fullPages} volle Seite${fullPages === 1 ? "" : "n"}`);
  if (rest > 0) parts.push(`${rest} für die nächste`);
  $("#backlog-cap").textContent = parts.join(" · ");
}

/** @param {boolean} running */
function setCardsRunning(running) {
  State.cardsRunning = running;
  ["cards-print", "cards-dry", "cards-undo"].forEach(id => { const el2 = /** @type {HTMLButtonElement|null} */ (document.getElementById(id)); if (el2) el2.disabled = running; });
  // Nothing new to print is a real state, not a job lock: keep the button dead
  // rather than let it run and report "0 new covers".
  const print = /** @type {HTMLButtonElement|null} */ (document.getElementById("cards-print"));
  if (print && !running && State.cardsBacklogEmpty) print.disabled = true;
  renderPickerBar();   // the picker's render buttons follow the same job lock
  renderPrintBar();    // and so does the Bibliothek print bar
}

/** @param {number} [code] */
function onCardsDone(code) {
  setCardsRunning(false);
  appendLog("cards-log", "Fertig (Code " + code + ").");
  if (code === 0) toast("Karten-Vorgang fertig", "ok");
  else toast("Karten-Vorgang endete mit Code " + code);
  if (isTabActive("karten")) loadCovers();   // refresh greyed/printed state
}

/** @param {string} path @param {any} body @param {string} label */
async function cardsAction(path, body, label) {
  if (State.cardsRunning) return;
  try {
    await api(path, "POST", body);
    $("#cards-log").replaceChildren();
    setCardsRunning(true);
    appendLog("cards-log", label + " gestartet…");
  } catch (/** @type {any} */ e) {
    if (e.status === 409) toast("Es läuft bereits ein Karten-Vorgang.");
    else toast(label + " fehlgeschlagen: " + e.message);
  }
}

/* ---- integrated card picker ---- */

async function loadCovers() {
  try {
    const r = await api("/api/cards/covers");
    Cards.covers = (r && r.covers) || [];
    if (r && r.perPage) Cards.perPage = r.perPage;
    Cards.loaded = true;
    // Drop selections whose cover no longer exists.
    const have = new Set(Cards.covers.map(c => c.rel));
    Cards.selection = Cards.selection.filter(rel => have.has(rel));
    renderCoversGrid();
  } catch (/** @type {any} */ e) {
    $("#cards-grid").replaceChildren(el("div", { class: "empty", text: "Cover konnten nicht geladen werden." }));
  }
}

function renderCoversGrid() {
  const grid = $("#cards-grid");
  grid.replaceChildren();
  if (!Cards.covers.length) {
    grid.append(el("div", { class: "empty", text: "Noch keine Cover vorhanden. Lade zuerst Titel." }));
    renderPickerBar();
    return;
  }
  for (const c of Cards.covers) {
    const order = Cards.selection.indexOf(c.rel);
    const tile = el("button", {
      class: "cover-tile" + (c.printed === "printed" ? " printed" : "") + (order >= 0 ? " selected" : ""),
      title: c.rel + (c.printed === "printed" ? " · gedruckt" : c.printed === "changed" ? " · geändert" : ""),
      onclick: () => toggleCover(c.rel),
    },
      el("img", { loading: "lazy", src: "/thumb?rel=" + encodeURIComponent(c.rel) + "&px=200", alt: "" }));
    if (order >= 0) tile.append(el("span", { class: "order", text: String(order + 1) }));
    if (c.printed === "printed") tile.append(el("span", { class: "badge-print", text: "gedruckt" }));
    else if (c.printed === "changed") tile.append(el("span", { class: "badge-print", text: "geändert" }));
    grid.append(tile);
  }
  renderPickerBar();
}

/** @param {string} rel */
function toggleCover(rel) {
  const i = Cards.selection.indexOf(rel);
  if (i >= 0) Cards.selection.splice(i, 1);
  else Cards.selection.push(rel);
  renderCoversGrid();
  if (Cards.selection.length >= Cards.perPage) renderCardsPage();   // auto-render a full page
}

function renderPickerBar() {
  const n = Cards.selection.length;
  const cnt = $("#pick-count"); if (cnt) cnt.textContent = String(n);
  const per = $("#pick-per"); if (per) per.textContent = String(Cards.perPage);
  const clear = $("#pick-clear"); if (clear) clear.disabled = n === 0 || State.cardsRunning;
  const render = $("#pick-render"); if (render) render.disabled = n === 0 || State.cardsRunning;
}

function clearSelection() {
  Cards.selection = [];
  renderCoversGrid();
}

async function renderCardsPage() {
  if (State.cardsRunning || !Cards.selection.length) return;
  const rels = Cards.selection.slice();
  try {
    await api("/api/cards/render", "POST", { rels });
    $("#cards-log").replaceChildren();
    setCardsRunning(true);
    appendLog("cards-log", "Seite mit " + rels.length + " Karten wird erzeugt…");
    Cards.selection = [];          // consumed; printed state arrives via reload
    renderCoversGrid();
  } catch (/** @type {any} */ e) {
    if (e.status === 409) toast("Es läuft bereits ein Karten-Vorgang.");
    else toast("Seite erzeugen fehlgeschlagen: " + e.message);
  }
}

/* =============================== 8. Agent =============================== */

const Agent = {
  /** @type {Map<string, any>} */ sessions: new Map(),   // id -> session record
  /** @type {string|null} */ current: null,
  available: false,
};

/** @param {AgentState} agent */
function renderAgentAvailability(agent) {
  Agent.available = !!(agent && agent.available);
  $("#agent-unavailable").hidden = Agent.available;
  $("#agent-live").hidden = !Agent.available;
  $("#agent-unavailable-msg").textContent =
    (agent && agent.reason) || "Der Claude-Agent ist noch nicht eingerichtet.";
  const cli = agent && agent.cli;
  $("#agent-cli-state").textContent = cli
    ? "Gefunden: " + cli
    : "Nicht gefunden — Installationsanleitung: claude.com/claude-code";
  $("#agent-cli-state").classList.toggle("missing", !cli);

  // The URL intake in the library is the agent's front door: without it the
  // field would accept a URL and then fail with a toast.
  const url = $("#lib-url"), load = $("#lib-load");
  if (url && load) {
    url.disabled = load.disabled = !Agent.available;
    url.placeholder = Agent.available
      ? "URL zum Laden…" : "URL zum Laden — Agent einrichten";
    const hint = Agent.available ? "" : "Zum Laden von URLs den Agenten einrichten (Tab „Agent“).";
    url.title = load.title = hint;
  }
  updateComposerState();
}

async function saveAgentToken() {
  const field = $("#agent-token");
  const value = (field.value || "").trim();
  if (!value) { toast("Bitte zuerst den Token einsetzen."); return; }
  try {
    await api("/api/agent/token", "POST", { token: value });
    field.value = "";
    toast("Token gespeichert.");
    refreshState();
  } catch (e) {
    toast("Konnte den Token nicht speichern.");
  }
}

/** @param {SessionInfo[]} list */
function reconcileSessions(list) {
  // `undefined` = state carried no session info (don't touch); `[]` = server
  // definitively has no sessions (e.g. after a restart) → end the confirmed ones.
  if (!Array.isArray(list)) return;
  const present = new Set();
  for (const info of list) {
    present.add(info.id);
    let sess = Agent.sessions.get(info.id);
    if (!sess) sess = createSessionUI(info);
    if (info.label) sess.label = info.label;
    if (info.model) sess.model = info.model;
    if (info.status) sess.status = info.status;
    sess.confirmed = true;
    sess.ended = false;
    // Restore an active download's intake strip + progress after a reload.
    if (info.progress && !["done", "error"].includes(info.status || "")) {
      const rec = Intakes.get(info.id) || ensureIntakeStrip(info.id, info.label);
      applyIntakeProgress(rec, info.progress);
    }
  }
  for (const sess of Agent.sessions.values()) {
    // Only a session the server once knew and now dropped counts as ended;
    // a freshly created optimistic session (not yet confirmed) is left alone.
    if (sess.confirmed && !present.has(sess.id)) sess.ended = true;
  }
  renderSessionList();
  if (Agent.current) { renderStatus(Agent.sessions.get(Agent.current)); updateComposerState(); }
}

/** @param {SessionInfo} info */
function createSessionUI(info) {
  const holder = el("div", { class: "transcript-holder", "data-sid": info.id });
  $("#transcripts").append(holder);
  const sess = {
    id: info.id, label: info.label || "Session", model: info.model || "sonnet",
    status: info.status || "running", holder, statusEl: null, lastToolBody: null,
    rendered: new Set(), confirmed: false, ended: false,
  };
  Agent.sessions.set(info.id, sess);
  return sess;
}

/** @param {string|null} id */
function selectSession(id) {
  Agent.current = id;
  $$(".transcript-holder").forEach(h => h.classList.toggle("active", h.dataset.sid === id));
  $("#transcript-placeholder").hidden = !!id;
  renderSessionList();
  const sess = id ? Agent.sessions.get(id) : null;
  renderStatus(sess);
  updateComposerState();
  scrollActiveTranscript();
}

function renderSessionList() {
  const wrap = $("#session-list");
  wrap.replaceChildren();
  const ids = Array.from(Agent.sessions.keys());
  $("#session-empty").hidden = ids.length > 0;
  for (const id of ids) {
    const s = Agent.sessions.get(id);
    const meta = s.ended ? "beendet" : (s.model + " · " + statusLabel(s.status));
    wrap.append(el("button", {
      class: "session-item" + (id === Agent.current ? " active" : "") + (s.ended ? " ended" : ""),
      onclick: () => selectSession(id),
    },
      el("span", { class: "s-label", text: s.label }),
      el("span", { class: "s-meta", text: meta })));
  }
}

/** @type {Record<string, string>} */
const STATUS_LABEL = {
  running: "läuft…", waiting_user: "wartet auf Antwort…", done: "fertig", error: "Fehler",
};
/** @param {string|undefined} st */
function statusLabel(st) { return (st && STATUS_LABEL[st]) || st || ""; }

/** @param {any} sess */
function renderStatus(sess) {
  const line = $("#agent-status");
  const stop = $("#agent-stop");
  if (!sess) { line.textContent = ""; stop.hidden = true; return; }
  if (sess.ended) { line.textContent = "Session beendet. Server wurde neu gestartet."; stop.hidden = true; return; }
  const running = sess.status === "running" || sess.status === "waiting_user";
  stop.hidden = !running;
  const txt = statusLabel(sess.status);
  if (running) line.innerHTML = '<span class="dotpulse">●</span> ' + escapeHtml(txt);
  else line.textContent = txt + (sess.lastResult || "");
}

// Enable/disable the composer based on the current session's liveness.
function updateComposerState() {
  const sess = Agent.current ? Agent.sessions.get(Agent.current) : null;
  const blocked = !Agent.available || (sess && sess.ended);
  $("#agent-text").disabled = !!blocked;
  $("#agent-send").disabled = !!blocked;
  if (sess && sess.ended) $("#agent-stop").hidden = true;
}

/* ---- event application (idempotent by seq) ---- */

/** @param {string} sessionId @param {AgentEvent} event @param {number} [seq] */
function applyAgentEvent(sessionId, event, seq) {
  if (!sessionId || !event) return;
  let sess = Agent.sessions.get(sessionId);
  if (!sess) sess = createSessionUI({ id: sessionId });
  if (seq != null) {
    if (sess.rendered.has(seq)) return;
    sess.rendered.add(seq);
  }
  const holder = sess.holder;
  switch (event.k) {
    case "user":
      holder.append(el("div", { class: "msg user", text: event.text || "" }));
      break;
    case "text":
      holder.append(el("div", { class: "msg agent" }, el("div", { class: "md", html: mdLite(event.text) })));
      break;
    case "tool": {
      const body = el("div", { class: "tool-body" });
      const chip = el("details", { class: "tool-chip" },
        el("summary", null, el("span", { class: "tool-name", text: event.name || "Tool" }),
          document.createTextNode(event.summary ? ": " + event.summary : "")),
        body);
      sess.lastToolBody = body;
      holder.append(chip);
      break;
    }
    case "tool_result":
      if (sess.lastToolBody) sess.lastToolBody.textContent = event.summary || "(kein Ergebnis)";
      else holder.append(el("div", { class: "tool-chip" }, el("div", { class: "tool-body", text: event.summary || "" })));
      break;
    case "question":
      holder.append(buildQuestionCard(sess, event));
      break;
    case "permission":
      holder.append(buildPermissionCard(sess, event));
      break;
    case "status":
      sess.status = event.status;
      renderSessionList();
      if (sess.id === Agent.current) renderStatus(sess);
      break;
    case "result": {
      const bits = [];
      if (event.costUsd != null) bits.push(fmtCost(event.costUsd));
      if (event.turns != null) bits.push(event.turns + " Schritte");
      sess.lastResult = bits.length ? " · " + bits.join(" · ") : "";
      if (event.error) { sess.status = "error"; holder.append(el("div", { class: "msg agent" }, el("div", { class: "cmd-box err", text: "Fehler: " + event.error }))); }
      renderSessionList();
      if (sess.id === Agent.current) renderStatus(sess);
      break;
    }
  }
  if (Intakes.has(sessionId)) feedIntake(sessionId, event);
  if (sess.id === Agent.current) scrollActiveTranscript();
}

function scrollActiveTranscript() {
  const t = $("#transcripts");
  if (t) t.scrollTop = t.scrollHeight;
}

/* ---- AskUserQuestion card ---- */

/** @param {any} sess @param {{requestId: string, questions: Question[]}} event */
function buildQuestionCard(sess, event) {
  const questions = event.questions || [];
  const state = questions.map((/** @type {Question} */ q) => (
    { q, selected: /** @type {any} */ (q.multiSelect ? new Set() : null), other: "" }));

  const body = el("div", { class: "ac-body" });
  const card = el("div", { class: "act-card", "data-rid": event.requestId },
    el("div", { class: "ac-head" }, "Frage vom Agenten"), body);

  state.forEach((/** @type {any} */ st, /** @type {number} */ qi) => {
    const q = st.q;
    const block = el("div", { class: "q-block" });
    if (q.header) block.append(el("div", { class: "q-header", text: q.header }));
    block.append(el("div", { class: "q-text", text: q.question || "" }));
    const opts = q.options || [];

    if (q.multiSelect) {
      for (const o of opts) {
        const cb = el("input", { type: "checkbox" });
        cb.addEventListener("change", () => { cb.checked ? st.selected.add(o.label) : st.selected.delete(o.label); });
        block.append(el("label", { class: "opt-check" }, cb,
          el("span", null, el("b", { text: o.label }),
            o.description ? el("span", { class: "o-desc", text: o.description }) : null)));
      }
    } else {
      /** @type {any[]} */
      const btns = [];
      for (const o of opts) {
        const b = el("button", { class: "opt-btn", onclick: () => {
          st.selected = o.label; st.other = "";
          btns.forEach(x => x.classList.toggle("sel", x === b));
          otherInput.value = "";
        } }, el("b", { text: o.label }), o.description ? el("span", { class: "o-desc", text: o.description }) : null);
        btns.push(b); block.append(b);
      }
    }
    const otherInput = el("input", { class: "input", placeholder: "Andere Antwort…", style: "margin-top:4px" });
    otherInput.addEventListener("input", () => {
      st.other = otherInput.value;
      if (!q.multiSelect && otherInput.value) { st.selected = null; $$(".opt-btn", block).forEach(x => x.classList.remove("sel")); }
    });
    block.append(otherInput);
    body.append(block);
  });

  const okBtn = el("button", { class: "btn primary", text: "Antworten" });
  body.append(el("div", { class: "row" }, okBtn));
  okBtn.addEventListener("click", async () => {
    /** @type {Record<string, any>} */
    const answers = {};
    for (const st of state) {
      const key = st.q.question || "";
      if (st.q.multiSelect) {
        const arr = Array.from(st.selected);
        if (st.other.trim()) arr.push(st.other.trim());
        if (!arr.length) { toast("Bitte mindestens eine Option wählen."); return; }
        answers[key] = arr;
      } else {
        const val = st.other.trim() || st.selected;
        if (!val) { toast("Bitte eine Option wählen oder eine Antwort eingeben."); return; }
        answers[key] = val;
      }
    }
    okBtn.disabled = true;
    try {
      const r = await api(`/api/agent/sessions/${sess.id}/answer`, "POST", { requestId: event.requestId, answers });
      if (r && r.ok === false) { expireCard(card); return; }
      resolveCard(card, "Beantwortet");
    } catch (/** @type {any} */ e) {
      if (e.status === 409) expireCard(card);
      else { okBtn.disabled = false; toast("Antwort fehlgeschlagen: " + e.message); }
    }
  });
  return card;
}

/* ---- permission card ---- */

/** @param {any} sess @param {{requestId: string, tool?: string, input?: any, summary?: string, pattern?: string|null}} event */
function buildPermissionCard(sess, event) {
  const body = el("div", { class: "ac-body" });
  const card = el("div", { class: "act-card perm", "data-rid": event.requestId },
    el("div", { class: "ac-head" }, "Freigabe nötig · " + (event.tool || "Tool")), body);
  body.append(el("div", { class: "cmd-box", text: event.summary || JSON.stringify(event.input || {}) }));

  const send = async (/** @type {boolean} */ allow, /** @type {boolean} */ always) => {
    $$("button", card).forEach(b => b.disabled = true);
    try {
      const r = await api(`/api/agent/sessions/${sess.id}/permission`, "POST", { requestId: event.requestId, allow, always: !!always });
      if (r && r.ok === false) { expireCard(card); return; }
      resolveCard(card, allow ? (always ? "Immer erlaubt" : "Erlaubt") : "Abgelehnt");
    } catch (/** @type {any} */ e) {
      if (e.status === 409) expireCard(card);
      else { $$("button", card).forEach(b => b.disabled = false); toast("Fehlgeschlagen: " + e.message); }
    }
  };
  // "Immer erlauben" only when the backend can name the exact pattern it would
  // whitelist — otherwise (compound commands, pattern null) only allow/deny once.
  const btns = [el("button", { class: "btn primary", text: "Erlauben", onclick: () => send(true, false) })];
  if (event.pattern) {
    btns.push(el("button", { class: "btn", text: "Immer erlauben",
      title: "Erlaubt künftig ohne Nachfrage: " + event.pattern, onclick: () => send(true, true) }));
  }
  btns.push(el("button", { class: "btn danger", text: "Ablehnen", onclick: () => send(false, false) }));
  body.append(el("div", { class: "row" }, ...btns));
  if (event.pattern) {
    body.append(el("div", { class: "perm-pattern" },
      document.createTextNode("„Immer erlauben“ gilt für "), el("code", { text: event.pattern })));
  }
  return card;
}

/** @param {any} card @param {string} label */
function resolveCard(card, label) {
  $$("button, input", card).forEach(b => { b.disabled = true; });
  card.append(el("div", { class: "ac-resolved", text: "✓ " + label }));
}

// Backend rejected the resolution (already answered, or stale after interrupt/restart).
/** @param {any} card */
function expireCard(card) {
  $$("button, input", card).forEach(b => { b.disabled = true; });
  card.classList.add("expired");
  card.append(el("div", { class: "ac-resolved", text: "Anfrage ist nicht mehr aktiv." }));
  toast("Anfrage ist nicht mehr aktiv.");
}

/* ---- sending turns ---- */

/** @param {string} text */
async function sendAgentMessage(text) {
  text = (text || "").trim();
  if (!text || !Agent.available) return;
  const cur = Agent.current ? Agent.sessions.get(Agent.current) : null;
  if (cur && cur.ended) { toast("Diese Session ist beendet. Starte eine neue Session."); return; }
  const model = $("#agent-model").value;
  try {
    if (Agent.current) {
      await api(`/api/agent/sessions/${Agent.current}/message`, "POST", { text });
      // Optimistic echo (server also emits a `user` event; deduped by seq only for
      // server events — this local echo has no seq, so guard against a double by
      // relying on the server echo; we skip local echo to avoid duplicates).
    } else {
      const r = await api("/api/agent/sessions", "POST", { kind: "chat", text, model });
      const sess = createSessionUI({ id: r.id, model });
      selectSession(r.id);
    }
    $("#agent-text").value = "";
  } catch (/** @type {any} */ e) {
    if (e.status === 503) toast("Agent nicht verfügbar (SDK_TOKEN fehlt).");
    else toast("Senden fehlgeschlagen: " + e.message);
  }
}

/** @param {string} url */
async function startUrlSession(url) {
  url = (url || "").trim();
  if (!url || !Agent.available) return;
  const model = $("#agent-model").value;
  try {
    const r = await api("/api/agent/sessions", "POST", { kind: "url", url, model });
    createSessionUI({ id: r.id, model, label: "Intake: " + url });
    selectSession(r.id);
    $("#agent-url").value = "";
    $("#agent-text").value = "";
  } catch (/** @type {any} */ e) {
    if (e.status === 503) toast("Agent nicht verfügbar (SDK_TOKEN fehlt).");
    else toast("URL-Intake fehlgeschlagen: " + e.message);
  }
}

/** @param {any} t */
function looksLikeUrl(t) { return /^https?:\/\/\S+$/i.test((t || "").trim()); }

async function replayAgentEvents() {
  const ids = Array.from(Agent.sessions.keys());
  for (const id of ids) {
    try {
      const r = await api(`/api/agent/sessions/${id}/events?since=${State.lastSeq}`);
      const events = (r && r.events) || (Array.isArray(r) ? r : []);
      for (const item of events) {
        const seq = item.seq;
        const ev = item.event || item;   // tolerate {seq,event} or a bare event
        if (typeof seq === "number") {
          if (State.seen.has(seq)) continue;
          State.seen.add(seq);
          State.lastSeq = Math.max(State.lastSeq, seq);
        }
        applyAgentEvent(id, ev, seq);
      }
    } catch { /* session may be gone; ignore */ }
  }
}

/* =============================== 8b. library download intakes =============================== */

// Start a URL intake from the Bibliothek toolbar and show a progress strip there.
/** @param {string} url */
async function startLibraryDownload(url) {
  url = (url || "").trim();
  if (!url) return;
  if (!looksLikeUrl(url)) { toast("Bitte eine gültige URL eingeben."); return; }
  if (!Agent.available) { toast("Agent nicht verfügbar (SDK_TOKEN fehlt)."); return; }
  const model = $("#agent-model").value || "sonnet";
  try {
    const r = await api("/api/agent/sessions", "POST", { kind: "url", url, model });
    createSessionUI({ id: r.id, model, label: "Intake: " + url });
    ensureIntakeStrip(r.id, url);
    $("#lib-url").value = "";
  } catch (/** @type {any} */ e) {
    if (e.status === 503) toast("Agent nicht verfügbar (SDK_TOKEN fehlt).");
    else toast("Download-Start fehlgeschlagen: " + e.message);
  }
}

/** @param {string} id @param {string} [label] */
function ensureIntakeStrip(id, label) {
  if (Intakes.has(id)) return Intakes.get(id);
  const state = el("span", { class: "in-state" }, el("span", { class: "spin" }));
  const activity = el("div", { class: "in-activity", text: "Startet…" });
  const badge = el("span", { class: "in-badge", text: "Frage offen", hidden: true });
  const dismiss = el("button", { class: "in-dismiss", title: "Ausblenden", text: "×", hidden: true });
  const barFill = el("span", { class: "in-bar-fill" });
  const bar = el("div", { class: "in-bar", hidden: true }, barFill);   // hidden until real progress
  const strip = el("div", { class: "intake", title: "Zum Agenten springen", onclick: () => gotoAgentSession(id) },
    state,
    el("div", { class: "in-body" },
      el("div", { class: "in-title", text: "Download-Intake · " + (label || id) }),
      activity, bar),
    badge, dismiss);
  dismiss.addEventListener("click", (/** @type {any} */ e) => { e.stopPropagation(); removeIntakeStrip(id); });
  const rec = { strip, state, activity, badge, dismiss, bar, barFill, done: false, pending: false, progress: null };
  Intakes.set(id, rec);
  $("#intake-strips").append(strip);
  return rec;
}

// Reflect the latest agent activity for an intake session into its strip. While
// a real download-progress snapshot is driving the label, agent text/tool lines
// don't overwrite it (progress is the honest signal).
/** @param {string} id @param {AgentEvent} event */
function feedIntake(id, event) {
  const rec = Intakes.get(id);
  if (!rec || rec.done) return;
  const clearPending = () => { rec.pending = false; rec.badge.hidden = true; };
  const downloading = rec.progress && rec.progress.phase === "download";
  switch (event.k) {
    case "text": {
      const t = (event.text || "").trim().replace(/\s+/g, " ");
      if (t && !downloading) rec.activity.textContent = t.slice(0, 160);
      clearPending();
      break;
    }
    case "tool":
      if (!downloading) rec.activity.textContent = (event.name || "Tool") + (event.summary ? ": " + event.summary : "");
      clearPending();
      break;
    case "question":
    case "permission":
      rec.pending = true; rec.badge.hidden = false;
      break;
    case "status":
      if (event.status === "running") clearPending();
      else if (event.status === "done") finishIntake(id, false);
      else if (event.status === "error") finishIntake(id, true);
      break;
    case "result":
      finishIntake(id, !!event.error);
      break;
  }
}

/** @param {any} m */
function handleIntakeProgress(m) {
  const rec = m && Intakes.get(m.sessionId);
  if (rec) applyIntakeProgress(rec, m.progress);
}

// Determinate bar only during phase "download"; probe/merge show a phase label
// and hide the bar (no honest total → no flashy sweep).
/** @param {any} rec @param {any} p */
function applyIntakeProgress(rec, p) {
  if (!rec || rec.done || !p) return;
  rec.progress = p;
  if (p.phase === "download") {
    const total = p.total || 0, item = p.item || 0;
    const bf = p.bytesTotal > 0 ? Math.min(1, (p.bytes || 0) / p.bytesTotal) : 0;
    const frac = total > 0 ? Math.max(0, Math.min(1, ((item - 1) + bf) / total)) : 0;
    rec.bar.hidden = false;
    rec.barFill.style.width = (frac * 100).toFixed(1) + "%";
    rec.activity.textContent = "Track " + item + "/" + total + (p.title ? " · " + p.title : "");
  } else {
    rec.bar.hidden = true;
    rec.activity.textContent = p.phase === "probe" ? "analysiere…"
      : p.phase === "merge" ? "führe zusammen…" : (p.title || "…");
  }
}

/** @param {string} id @param {boolean} isError */
function finishIntake(id, isError) {
  const rec = Intakes.get(id);
  if (!rec || rec.done) return;
  rec.done = true;
  rec.strip.classList.add(isError ? "error" : "done");
  rec.state.replaceChildren(document.createTextNode(isError ? "✕" : "✓"));
  rec.activity.textContent = isError ? "Fehler beim Download" : "Fertig";
  rec.badge.hidden = true;
  rec.bar.hidden = true;
  rec.dismiss.hidden = false;
  if (!isError) refreshState(true);   // pull in the new title; new rows flash-highlight
}

/** @param {string} id */
function removeIntakeStrip(id) {
  const rec = Intakes.get(id);
  if (rec) { rec.strip.remove(); Intakes.delete(id); }
}

/** @param {string} id */
function gotoAgentSession(id) {
  activateTab("agent");
  selectSession(id);
}

/* =============================== 8c. RFID card assignment =============================== */

// While a card sits on the reader it is "active": clicking an empty Karte box
// then assigns it to that unit (one click, smart default playMode — no dialog).
const RFID = {
  /** @type {ReturnType<typeof setTimeout>|null} */ bannerTimer: null,
  /** @type {{ id: string, assignment: Assignment|null }|null} */ active: null,
  /** @type {Assignment[]|null} */ assignments: null,   // cache of GET /api/rfid/assignments (lazy)
  /** @type {Promise<Assignment[]>|null} */ assignmentsPromise: null,
  /** @type {Map<string, number>} */ pendingMode: new Map(),  // unitId -> chosen playMode
  /** @type {string|null} */ popoverUnit: null,
  /** @type {ReturnType<typeof setTimeout>|null} */ popHideTimer: null,
};
const FILE_KINDS = new Set(["song", "hoerspiel", "other"]);   // single-file units
/** @type {Record<string, number>} */
const DEFAULT_MODE = { song: 1, hoerspiel: 3, album: 5, folge: 5, other: 3 };
const MODE_OPTS = {
  file:   [[1, "Einzeltitel"], [2, "Einzeltitel-Schleife"], [3, "Hörbuch (merkt Position)"]],
  folder: [[5, "Ordner, sortiert"], [6, "Ordner, zufällig"], [7, "Ordner, sortiert (Schleife)"]],
};
/** @param {string} kind */
function modeOptionsFor(kind) { return FILE_KINDS.has(kind) ? MODE_OPTS.file : MODE_OPTS.folder; }

function invalidateAssignments() { RFID.assignments = null; }
function fetchAssignments() {
  if (RFID.assignments) return Promise.resolve(RFID.assignments);
  if (RFID.assignmentsPromise) return RFID.assignmentsPromise;
  RFID.assignmentsPromise = api("/api/rfid/assignments")
    .then(r => { RFID.assignments = (r && r.assignments) || []; RFID.assignmentsPromise = null; return /** @type {Assignment[]} */ (RFID.assignments); })
    .catch(() => { RFID.assignmentsPromise = null; return []; });
  return RFID.assignmentsPromise;
}

/** @param {string} id */
function unitById(id) { return ((State.data && State.data.units) || []).find(u => u.id === id) || null; }
/** @param {string} kind */
function defaultPlayMode(kind) { return DEFAULT_MODE[kind] || (FILE_KINDS.has(kind) ? 3 : 5); }
/** @param {any} a */
function titleForAssignment(a) {
  if (!a) return null;
  const u = a.unitId && unitById(a.unitId);
  if (u) return u.title || u.id;
  if (a.fileOrUrl) return a.fileOrUrl.replace(/^\//, "");
  return null;
}

// A card was placed on the reader (fires on any placement, playback included).
/** @param {any} m */
function handleRfidCard(m) {
  if (!m.id) return;
  RFID.active = { id: m.id, assignment: m.assignment || null };
  showRfidBanner(!!m.known, m.id, titleForAssignment(m.assignment));
  renderLibrary();   // pulse empty Karte boxes as click targets
}

// Sticky banner = the assignment mode itself, so it lingers ~60 s.
/** @param {boolean} known @param {string} id @param {string} title */
function showRfidBanner(known, id, title) {
  const bar = $("#rfid-banner");
  bar.replaceChildren();
  bar.classList.toggle("known", known);
  bar.classList.toggle("unknown", !known);
  const text = el("span", { class: "rb-text" });
  if (known) text.append("Karte erkannt: ", el("b", { text: title || "unbekannter Titel" }),
    document.createTextNode(" – zum Neu-Zuordnen eine Karte-Box anklicken"));
  else text.append("Neue Karte aufgelegt ", el("span", { class: "mono", text: "(" + id + ")" }),
    document.createTextNode(" – leere Karte-Box eines Titels anklicken"));
  const dismiss = el("button", { class: "in-dismiss", title: "Ausblenden", text: "×" });
  dismiss.addEventListener("click", clearActiveCard);
  bar.append(el("span", { class: "rb-icon", text: known ? "🏷" : "✨" }), text,
    el("span", { class: "spacer" }), dismiss);
  bar.hidden = false;
  clearTimeout(RFID.bannerTimer ?? undefined);
  RFID.bannerTimer = setTimeout(clearActiveCard, 60000);
}

function clearActiveCard() {
  RFID.active = null;
  RFID.pendingMode.clear();
  const b = $("#rfid-banner");
  if (b) b.hidden = true;
  clearTimeout(RFID.bannerTimer ?? undefined);
  hideModePopover();
  renderLibrary();
}

// Karte box click: assigned → offer to remove; empty → assign active card or hint.
/** @param {Unit} unit */
function onCardBoxClick(unit) {
  const cards = unit.cards || [];
  if (cards.length) { openUnassign(unit); return; }
  if (!RFID.active) { toast("Karte auflegen zum Zuordnen"); return; }
  assignActiveCard(unit);
}

/** @param {Unit} unit */
async function assignActiveCard(unit) {
  const act = RFID.active;
  if (!act) return;
  // Reassigning directly, no confirm — the success toast is confirmation enough
  // and a wrong assignment is one click to fix.
  const playMode = RFID.pendingMode.get(unit.id) || defaultPlayMode(unit.kind);
  try {
    await api("/api/rfid/assign", "POST", { tagId: act.id, unitId: unit.id, playMode });
    toast("Karte zugeordnet: " + (unit.title || unit.id), "ok");
    clearActiveCard();
  } catch (/** @type {any} */ e) {
    if (e.status === 503) toast("Gerät nicht erreichbar. Zuordnung nicht gespeichert.");
    else toast("Zuordnung fehlgeschlagen: " + e.message);
  }
}

// Small confirm dialog to delete a unit's card link(s).
/** @param {Unit} unit */
function openUnassign(unit) {
  const cards = unit.cards || [];
  if (!cards.length) return;
  $("#unassign-title").textContent = "Zuordnung aufheben";
  const body = $("#unassign-body");
  body.replaceChildren(el("div", { class: "subtle", text: unit.title || unit.id }));
  for (const id of cards) {
    const btn = el("button", { class: "btn danger", text: cards.length === 1 ? "Zuordnung aufheben" : "Entfernen" });
    const row = el("div", { class: "unassign-row" }, el("span", { class: "mono", text: "Karte " + id }), btn);
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      if (await doUnassign(id)) { row.remove(); if (!body.querySelector(".unassign-row")) closeUnassign(); }
      else btn.disabled = false;
    });
    body.append(row);
  }
  const dlg = $("#unassign-dialog");
  if (typeof dlg.showModal === "function") { if (!dlg.open) dlg.showModal(); } else dlg.setAttribute("open", "");
}
function closeUnassign() { const d = $("#unassign-dialog"); if (d.close) d.close(); else d.removeAttribute("open"); }

/** @param {string} tagId */
async function doUnassign(tagId) {
  try {
    await api("/api/rfid/unassign", "POST", { tagId });
    toast("Zuordnung aufgehoben", "ok");
    return true;
  } catch (/** @type {any} */ e) {
    if (e.status === 503) toast("Gerät nicht erreichbar.");
    else toast("Aufheben fehlgeschlagen: " + e.message);
    return false;
  }
}

// --- play-mode hover/focus popover on the Karte box ---
/** @param {string} kind @param {number} value @param {(v: number)=>void} onChange */
function makeModeSelect(kind, value, onChange) {
  const sel = el("select", { class: "input mode-sel", title: "Abspielmodus" });
  for (const [v, label] of modeOptionsFor(kind)) sel.append(el("option", { value: String(v), text: label }));
  sel.value = String(value);
  sel.addEventListener("change", () => onChange(Number(sel.value)));
  return sel;
}

// Show a mode popover for `unit` anchored to `box`. Assigned units show one row
// per card (current mode, editable → re-POST); an unassigned unit with an active
// card shows the pending default (editable → remembered for the click-to-assign).
/** @param {any} box @param {Unit} unit */
async function showModePopover(box, unit) {
  cancelPopoverHide();
  const cards = unit.cards || [];
  if (!cards.length && !RFID.active) return;   // nothing actionable
  const pop = $("#mode-popover");
  RFID.popoverUnit = unit.id;
  const title = el("div", { class: "mp-title", text: "Abspielmodus" });
  if (cards.length) {
    pop.replaceChildren(title, el("div", { class: "subtle", text: "…" }));
    positionPopover(box); pop.hidden = false;
    const assignments = await fetchAssignments();
    if (RFID.popoverUnit !== unit.id) return;   // pointer moved away during fetch
    pop.replaceChildren(title);
    for (const id of cards) {
      const a = assignments.find(x => x.id === id);
      const cur = a ? a.playMode : defaultPlayMode(unit.kind);
      const row = el("div", { class: "mp-row" });
      if (cards.length > 1) row.append(el("span", { class: "mono mp-cardid", text: id }));
      row.append(makeModeSelect(unit.kind, cur, (/** @type {number} */ v) => changeMode(id, unit, v)));
      pop.append(row);
    }
  } else {
    const cur = RFID.pendingMode.get(unit.id) || defaultPlayMode(unit.kind);
    pop.replaceChildren(
      el("div", { class: "mp-title", text: "Modus beim Zuordnen" }),
      el("div", { class: "mp-row" }, makeModeSelect(unit.kind, cur, (/** @type {number} */ v) => RFID.pendingMode.set(unit.id, v))));
    pop.hidden = false;
  }
  positionPopover(box);
}

/** @param {string} tagId @param {Unit} unit @param {number} playMode */
async function changeMode(tagId, unit, playMode) {
  try {
    await api("/api/rfid/assign", "POST", { tagId, unitId: unit.id, playMode });
    toast("Modus geändert", "ok");
    invalidateAssignments();
  } catch (/** @type {any} */ e) {
    if (e.status === 503) toast("Gerät nicht erreichbar.");
    else toast("Änderung fehlgeschlagen: " + e.message);
  }
}

/** @param {any} box */
function positionPopover(box) {
  const pop = $("#mode-popover");
  if (!box.getBoundingClientRect) return;
  const r = box.getBoundingClientRect();
  pop.style.top = Math.max(8, r.top) + "px";
  pop.style.left = (r.right + 8) + "px";
}
function hideModePopover() { const p = $("#mode-popover"); if (p) p.hidden = true; RFID.popoverUnit = null; }
function schedulePopoverHide() { clearTimeout(RFID.popHideTimer ?? undefined); RFID.popHideTimer = setTimeout(hideModePopover, 160); }
function cancelPopoverHide() { clearTimeout(RFID.popHideTimer ?? undefined); }

/* =============================== 9. init =============================== */

function initAgentUI() {
  $("#agent-new").addEventListener("click", () => { Agent.current = null; selectSession(null); $("#agent-text").focus(); });
  $("#agent-send").addEventListener("click", () => sendAgentMessage($("#agent-text").value));
  $("#agent-stop").addEventListener("click", async () => {
    if (!Agent.current) return;
    try { await api(`/api/agent/sessions/${Agent.current}/interrupt`, "POST"); } catch (/** @type {any} */ e) { toast("Stopp fehlgeschlagen: " + e.message); }
  });
  $("#agent-url-go").addEventListener("click", () => startUrlSession($("#agent-url").value));
  $("#agent-url").addEventListener("keydown", (/** @type {any} */ e) => { if (e.key === "Enter") startUrlSession(e.target.value); });

  const ta = $("#agent-text");
  ta.addEventListener("keydown", (/** @type {any} */ e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendAgentMessage(ta.value); }
  });
}

function initSyncCards() {
  $("#sync-dry").addEventListener("click", () => startSync(true, true));
  $("#sync-run").addEventListener("click", () => startSync(false, false));
  $("#sync-del").addEventListener("click", () => startSync(false, true));
  $("#device-ping").addEventListener("click", pingDevice);
  $("#device-chip").addEventListener("click", pingDevice);

  $("#cards-print").addEventListener("click", () => cardsAction("/api/cards/print", { dryRun: false }, "Drucken"));
  $("#cards-dry").addEventListener("click", () => cardsAction("/api/cards/print", { dryRun: true }, "Dry-Run"));
  $("#cards-undo").addEventListener("click", () => cardsAction("/api/cards/undo", {}, "Rückgängig"));
  $("#pick-clear").addEventListener("click", clearSelection);
  $("#pick-render").addEventListener("click", renderCardsPage);
}

function updateSortToggleLabel() {
  const btn = $("#lib-sort-toggle");
  if (!btn) return;
  const alpha = State.sortMode === "alpha";
  btn.textContent = alpha ? "Sortierung: A–Z" : "Sortierung: Datum";
  btn.title = alpha
    ? "Alphabetisch (deutsche Telefonbuchsortierung) — Ordner zuerst, Titel zuletzt. Klick: nach Datum sortieren."
    : "Neueste zuerst. Klick: alphabetisch sortieren (deutsche Telefonbuchsortierung).";
}

function initLibrary() {
  /** @type {ReturnType<typeof setTimeout>|undefined} */
  let t;
  $("#lib-search").addEventListener("input", () => { clearTimeout(t); t = setTimeout(renderLibrary, 120); });
  $("#lib-refresh").addEventListener("click", () => refreshState(true));
  $("#lib-sort-toggle").addEventListener("click", () => {
    State.sortMode = State.sortMode === "alpha" ? "date" : "alpha";
    updateSortToggleLabel();
    renderLibrary();
    api("/api/prefs", "POST", { librarySort: State.sortMode })
      .catch((/** @type {any} */ e) => toast("Sortierung nicht gespeichert: " + e.message));
  });
  $("#lib-load").addEventListener("click", () => startLibraryDownload($("#lib-url").value));
  $("#lib-url").addEventListener("keydown", (/** @type {any} */ e) => { if (e.key === "Enter") startLibraryDownload(e.target.value); });
  $("#agent-token-save").addEventListener("click", saveAgentToken);
  $("#agent-token").addEventListener("keydown", (/** @type {any} */ e) => { if (e.key === "Enter") saveAgentToken(); });
}

function initRfid() {
  $("#unassign-close").addEventListener("click", closeUnassign);
  // A click on the ::backdrop targets the <dialog> element itself → close.
  $("#unassign-dialog").addEventListener("click", (/** @type {any} */ e) => { if (e.target === $("#unassign-dialog")) closeUnassign(); });
  $("#print-bar-go").addEventListener("click", printSelected);
  $("#print-bar-clear").addEventListener("click", clearPrintSel);

  // Mode popover: hover-bridge (stay open while the pointer is inside it),
  // and dismiss on Esc or a click outside it.
  const pop = $("#mode-popover");
  pop.addEventListener("mouseenter", cancelPopoverHide);
  pop.addEventListener("mouseleave", schedulePopoverHide);
  document.addEventListener("keydown", (/** @type {any} */ e) => { if (e.key === "Escape") hideModePopover(); });
  document.addEventListener("mousedown", (/** @type {any} */ e) => {
    if (!pop.hidden && e.target instanceof Node && !pop.contains(e.target)) hideModePopover();
  });
}

// Show a shadow under the bottom sticky bar only once content scrolls beneath it.
/** Shadow under the sticky table/picker heads once their pane is scrolled.
 *  Bound to <main>, the page's only scroll container — the document itself
 *  never scrolls (see the app layout block in style.css). */
function initScrollShadow() {
  const pane = $("main");
  if (!pane) return;
  const onScroll = () => document.body.classList.toggle("scrolled", pane.scrollTop > 4);
  pane.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
}

/** The native shell loads us with ?shell=mac; the stylesheet then makes the
 *  header double as the window's title bar (see .app-header).
 *
 *  The header also becomes the window's drag region: the app's WebView covers
 *  the whole window including the (transparent) title bar, so AppKit never sees
 *  those clicks and the window could not be moved. Report the drag instead —
 *  but not from the controls in the header, which must stay clickable. */
function initShell() {
  const shell = new URLSearchParams(location.search).get("shell");
  if (!shell) return;
  document.documentElement.dataset.shell = shell;

  const drag = window.webkit?.messageHandlers?.shellDrag;
  const header = $(".app-header");
  if (!drag || !header) return;
  header.addEventListener("mousedown", (ev) => {
    if (ev.button !== 0) return;
    if (ev.target.closest("button, a, input, select, textarea, [role='button']")) return;
    drag.postMessage(null);
  });
}

function init() {
  initShell();
  initTabs();
  initLibrary();
  initSyncCards();
  initAgentUI();
  initRfid();
  initScrollShadow();
  refreshState();       // first paint even before WS is up
  connectWS();
}

document.addEventListener("DOMContentLoaded", init);
