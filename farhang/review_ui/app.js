// FarhangBench review: a small hash-routed app, no libraries. Every string from the server or the data goes through
// html`` (which escapes interpolations) so item text, names and comments can't inject markup. No inline styles or
// handlers either (CSP): clicks go through data-act attributes, widths through element.style from here.
"use strict";

const META = (() => { try { return JSON.parse(document.getElementById("meta").textContent); } catch (e) { return {}; } })();
META.tracks = META.tracks || {}; META.tasks = META.tasks || {}; META.flags = META.flags || {};

const STATUS = { unreviewed: "Unreviewed", in_review: "In review", validated: "Validated", disputed: "Disputed", outdated: "Outdated", retired: "Retired" };
const VERDICT = { correct: "Correct", wrong: "Wrong", unsure: "Not sure" };
const VERDICT_FA = { correct: "درست", wrong: "نادرست", unsure: "مطمئن نیستم" };
const VERDICT_ICON = { correct: "✓", wrong: "✗", unsure: "?" };
const NATIVE = "farhangbench";
const SCOPES = { native: "Native items", gated: "Social norms (gated)", reference: "Imported & derived", all: "All sources" };
const SHOWS = { todo: "To do", mine: "Done", disputed: "Disputed", all: "All" };
const AUDIT_ACTIONS = ["add", "reset", "role", "disable", "enable", "remove", "rename", "login", "login_failed", "logout", "password_change", "peek"];
const PAGE = 50;

// ------------------------------------------------------------------ escaping and tiny DOM helpers

class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = s => String(s).replace(/[&<>"']/g, c => ESC[c]);
function show(v) {
  if (v instanceof Raw) return v.s;
  if (Array.isArray(v)) return v.map(show).join("");
  if (v === null || v === undefined || v === false || v === true) return "";
  return esc(v);
}
function html(strings, ...vals) {
  let out = strings[0];
  for (let i = 0; i < vals.length; i++) out += show(vals[i]) + strings[i + 1];
  return new Raw(out);
}
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
function mount(el, content) {
  el.innerHTML = show(content);
  $$("[data-w]", el).forEach(n => { n.style.width = Math.max(0, Math.min(100, +n.dataset.w)) + "%"; });
  $$("[data-h]", el).forEach(n => { n.style.height = Math.max(0, Math.min(100, +n.dataset.h)) + "%"; });
  return el;
}
const store = {
  get(k, d = null) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
};
const enc = encodeURIComponent;
const pct = v => v === null || v === undefined ? "–" : Math.round(100 * v) + "%";
const n = v => (v || 0).toLocaleString("en-US");
const plural = (k, word, many) => `${n(k)} ${k === 1 ? word : (many || word + "s")}`;
const ICON = {
  search: html`<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>`,
  left: html`<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>`,
  right: html`<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>`,
  more: html`<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></svg>`,
  copy: html`<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/></svg>`,
  plus: html`<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>`,
  link: html`<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/></svg>`,
  download: html`<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12m-5-5 5 5 5-5M5 21h14"/></svg>`,
};

// ------------------------------------------------------------------ time

function parseTime(t) { const d = t ? new Date(t) : null; return d && !isNaN(d) ? d : null; }
function ago(t) {
  const d = parseTime(t); if (!d) return "never";
  const s = Math.round((Date.now() - d) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 30) return `${Math.round(s / 86400)} d ago`;
  return d.toISOString().slice(0, 10);
}
function stamp(t) {
  const d = parseTime(t); if (!d) return "";
  return d.toLocaleString("en-GB", { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
const timeEl = t => t ? html`<time datetime="${t}" title="${stamp(t)}">${ago(t)}</time>` : html`<span class="faint">—</span>`;

// ------------------------------------------------------------------ state

const S = {
  me: null,                 // /api/whoami
  reviewer: "",             // the name reviews are saved under
  items: null, itemsAt: 0,  // /api/items (this reviewer's queue)
  filters: {
    scope: store.get("fb-scope", "native"), show: store.get("fb-show", "todo"),
    track: store.get("fb-track", ""), task: store.get("fb-task", ""), q: "",
  },
  limit: 200,
  cur: null,                // { id, item, key, choice, verdict, flags }
  routeToken: 0,
  reviewers: null,          // admin cache: /api/admin/reviewers
  sources: null,            // admin cache: sources from /api/progress
};
const isAdmin = () => !!S.me && (S.me.local || S.me.role === "admin");
const withWho = (path) => S.me && S.me.local ? path + (path.includes("?") ? "&" : "?") + "reviewer=" + enc(S.reviewer) : path;

class ApiError extends Error { constructor(msg, status, data) { super(msg); this.status = status; this.data = data || {}; } }
async function api(path, body) {
  const opts = body === undefined ? { headers: { Accept: "application/json" } }
    : { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(body) };
  let res;
  try { res = await fetch(path, opts); } catch (e) { throw new ApiError("Can't reach the server. Check your connection and try again.", 0); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new ApiError(data.error || `${res.status} ${res.statusText}`, res.status, data);
    if (res.status === 401 && S.me && S.me.signed_in && path !== "/api/login") { signedOut(); }
    if (res.status === 403 && data.must_change && S.me) { S.me.must_change = true; route(); }
    throw err;
  }
  return data;
}
function signedOut() {
  S.me = Object.assign({}, S.me, { signed_in: false, reviewer: null, role: null });
  S.items = null; S.reviewers = null;
  toast("Your session ended. Please sign in again.", "error");
  route();
}

// ------------------------------------------------------------------ theme

const THEMES = ["auto", "light", "dark"];
function applyTheme() {
  const t = store.get("fb-theme", "auto");
  if (t === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
}
applyTheme();

// ------------------------------------------------------------------ toasts

function toast(text, kind = "ok") {
  const box = $("#toasts");
  const el = document.createElement("div");
  el.className = "toast" + (kind === "error" ? " error" : "");
  mount(el, html`<span class="t-icon" aria-hidden="true">${kind === "error" ? "!" : "✓"}</span><span>${text}</span><button class="t-close" type="button" aria-label="Dismiss">×</button>`);
  box.append(el);
  try { if (box.matches(":popover-open")) box.hidePopover(); box.showPopover(); } catch (e) { /* no popover support: stays below dialogs */ }
  const dismiss = () => { el.classList.add("leaving"); setTimeout(() => el.remove(), 220); };
  const timer = setTimeout(dismiss, 4000);
  $(".t-close", el).addEventListener("click", () => { clearTimeout(timer); dismiss(); });
  while (box.children.length > 3) box.firstChild.remove();
}

// ------------------------------------------------------------------ dialogs

function openDialog(content, { wide = false, onClose } = {}) {
  closeMenus();
  const d = document.createElement("dialog");
  d.className = "dialog" + (wide ? " wide" : "");
  mount(d, content);
  document.body.append(d);
  d.addEventListener("close", () => { d.remove(); if (onClose) onClose(d.returnValue); });
  d.addEventListener("click", e => { if (e.target === d) d.close("cancel"); });
  d.showModal();
  const first = $("[autofocus]", d) || $("input, textarea, select", d);
  if (first) first.focus();
  return d;
}
function confirmDialog({ title, body, confirm = "Confirm", danger = false, tone = "t-warn", icon = "!", typed = "" }) {
  return new Promise(resolve => {
    const d = openDialog(html`<form method="dialog">
      <div class="dialog-body">
        <div class="dialog-icon ${danger ? "t-bad" : tone}" aria-hidden="true">${icon}</div>
        <h2>${title}</h2>
        <div class="lead">${body}</div>
        ${typed ? html`<label class="field"><span>Type <code>${typed}</code> to confirm</span>
          <input class="input" name="typed" autocomplete="off" spellcheck="false" autofocus></label>` : ""}
      </div>
      <div class="dialog-foot">
        <button class="btn" value="cancel" type="submit" formnovalidate ${typed ? "" : "autofocus"}>Cancel</button>
        <button class="btn ${danger ? "danger" : "primary"}" value="ok" type="submit" data-role="ok" ${typed ? "disabled" : ""}>${confirm}</button>
      </div></form>`, { onClose: v => resolve(v === "ok") });
    if (typed) {
      const input = $("input[name=typed]", d), ok = $("[data-role=ok]", d);
      input.addEventListener("input", () => { ok.disabled = input.value.trim() !== typed; });
    }
  });
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", ""); ta.className = "sr-only";
    document.body.append(ta); ta.select();
    let ok = false; try { ok = document.execCommand("copy"); } catch (e2) { ok = false; }
    ta.remove(); return ok;
  }
}

// ------------------------------------------------------------------ menus (popovers positioned from JS)

function closeMenus(except) {
  $$(".menu").forEach(m => { if (m !== except) m.hidden = true; });
  $$("[aria-haspopup=menu]").forEach(b => { if (!except || b.nextElementSibling !== except) b.setAttribute("aria-expanded", "false"); });
}
function toggleMenu(btn) {
  const menu = btn.nextElementSibling;
  const open = menu.hidden;
  closeMenus(open ? menu : null);
  menu.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  if (open) {
    const r = btn.getBoundingClientRect();
    menu.style.top = (r.bottom + 6) + "px";
    const w = menu.offsetWidth;
    const left = menu.classList.contains("align-right") ? r.right - w : r.left;
    menu.style.left = Math.max(8, Math.min(left, window.innerWidth - w - 8)) + "px";
    const first = $("a, button", menu);
    if (first) first.focus();
  }
}
document.addEventListener("click", e => {
  if (!e.target.closest(".menu") && !e.target.closest("[aria-haspopup=menu]")) closeMenus();
});
window.addEventListener("resize", () => closeMenus());

// ------------------------------------------------------------------ actions (event delegation)

const ACT = {};
document.addEventListener("click", e => {
  const el = e.target.closest("[data-act]");
  if (!el || el.disabled) return;
  const fn = ACT[el.dataset.act];
  if (!fn) return;
  if (el.tagName === "A" && !el.getAttribute("href")) e.preventDefault();
  if (el.tagName === "BUTTON" || el.dataset.prevent) e.preventDefault();
  fn(el, e);
});
document.addEventListener("click", e => {  // table rows and feed entries that link somewhere
  const row = e.target.closest("[data-href]");
  if (!row || e.target.closest("a, button, input, select, textarea, label")) return;
  location.hash = row.dataset.href;
});
document.addEventListener("keydown", e => {
  const row = e.target.closest && e.target.closest("[data-href]");
  if (row && e.target === row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); location.hash = row.dataset.href; }
});
ACT.menu = el => toggleMenu(el);
ACT.theme = () => {
  const t = THEMES[(THEMES.indexOf(store.get("fb-theme", "auto")) + 1) % 3];
  store.set("fb-theme", t); applyTheme(); renderTopbar(); toast(`Theme: ${t === "auto" ? "match the system" : t}`);
};
ACT.shortcuts = () => showShortcuts();
ACT.signout = async () => {
  closeMenus();
  try { await api("/api/logout", {}); } catch (e) { /* already signed out */ }
  S.me = Object.assign({}, S.me, { signed_in: false, reviewer: null, role: null });
  S.items = null; S.reviewers = null; S.cur = null;
  history.replaceState(null, "", location.pathname + location.search + "#/review");
  route();
};
ACT.pwtoggle = el => {
  const input = el.parentElement.querySelector("input");
  const showing = input.type === "text";
  input.type = showing ? "password" : "text";
  el.textContent = showing ? "Show" : "Hide";
  el.setAttribute("aria-label", showing ? "Show password" : "Hide password");
  el.setAttribute("aria-pressed", String(!showing));
};
$("#skip-link").addEventListener("click", () => { const m = $("#view [autofocus]") || $("#view"); m.focus(); });

// ------------------------------------------------------------------ router

function parseHash() {
  const h = location.hash.replace(/^#\/?/, "");
  const qi = h.indexOf("?");
  const path = qi < 0 ? h : h.slice(0, qi);
  const query = Object.fromEntries(new URLSearchParams(qi < 0 ? "" : h.slice(qi + 1)));
  return { parts: path.split("/").filter(Boolean).map(p => { try { return decodeURIComponent(p); } catch (e) { return p; } }), query };
}
const qs = obj => { const p = new URLSearchParams(); Object.entries(obj).forEach(([k, v]) => { if (v !== "" && v !== null && v !== undefined && v !== 0 && v !== "0") p.set(k, v); }); const s = p.toString(); return s ? "?" + s : ""; };
function replaceHash(h) { history.replaceState(null, "", location.pathname + location.search + h); route(); }
window.addEventListener("hashchange", () => route());

async function route() {
  if (!S.me) return;
  closeMenus();
  $$("dialog.dialog").forEach(d => d.close("route"));
  const view = $("#view");
  if (S.me.auth_enabled && !S.me.signed_in) { $("#topbar").hidden = true; return renderLogin(view); }
  if (S.me.must_change) { $("#topbar").hidden = true; return renderPassword(view, true); }
  const { parts, query } = parseHash();
  const [a = "review", b, c] = parts;
  const token = ++S.routeToken;
  renderTopbar(a === "admin" ? "admin/" + (b || "activity") : a);
  document.body.dataset.view = a;
  const stale = () => token !== S.routeToken;
  try {
    if (a === "review") return await viewReview(view, b, stale);
    if (a === "me") return await viewMe(view, stale);
    if (a === "progress") return await viewProgress(view, stale);
    if (a === "password") return renderPassword(view, false);
    if (a === "admin") {
      if (!isAdmin()) return viewMessage(view, "Admins only", "This page is for admins. Ask an admin if you need access.");
      if (!b || b === "activity") return await viewActivity(view, query, stale);
      if (b === "reviewers" && c) return await viewReviewer(view, c, stale);
      if (b === "reviewers") return await viewReviewers(view, stale);
      if (b === "items" && c) return await viewItemAudit(view, c, stale);
      if (b === "items") return await viewItems(view, query, stale);
      if (b === "people") return await viewPeople(view, stale);
      if (b === "audit") return await viewAudit(view, query, stale);
    }
    viewMessage(view, "Page not found", "There's nothing at this address.", html`<a class="btn" href="#/review">Go to review</a>`);
  } catch (e) {
    if (stale()) return;
    if (e.status === 401 || (e.data && e.data.must_change)) return;
    viewMessage(view, "Something went wrong", e.message, html`<button class="btn" data-act="retry">Try again</button>`);
  }
}
ACT.retry = () => route();
function viewMessage(view, title, text, extra = "") {
  delete view.dataset.screen;
  mount(view, html`<div class="page narrow"><div class="card empty"><div class="empty-icon" aria-hidden="true">○</div><b>${title}</b><p>${text}</p>${extra}</div></div>`);
}
function loading(view) {
  delete view.dataset.screen;
  mount(view, html`<div class="boot" aria-busy="true"><span class="spinner" aria-hidden="true"></span><span>Loading…</span></div>`);
}

// ------------------------------------------------------------------ top bar

function displayName() { return (S.me && S.me.display_name) || S.reviewer || ""; }
function renderTopbar(active = "") {
  const bar = $("#topbar");
  bar.hidden = false;
  const admin = isAdmin();
  const nav = (key, label) => html`<a href="#/${key}" class="${active === key ? "on" : ""}" ${active === key ? html`aria-current="page"` : ""}>${label}</a>`;
  const adminLinks = [["activity", "Activity", "Every answer and verdict"], ["reviewers", "Reviewers", "Who did what"], ["items", "Items", "Status and audit"],
    ...(S.me.local ? [] : [["people", "People", "Accounts"], ["audit", "Audit log", "Sign-ins and changes"]])];
  const theme = store.get("fb-theme", "auto");
  const themeLabel = { auto: "Theme: system", light: "Theme: light", dark: "Theme: dark" }[theme];
  const initial = (displayName() || "?").trim().charAt(0) || "?";
  mount(bar, html`<div class="topbar-inner">
    <a class="brand" href="#/review" aria-label="FarhangBench review, home"><span class="brand-mark" aria-hidden="true">ف</span>
      <span class="brand-name">FarhangBench<span class="brand-sub">بازبینی</span></span></a>
    <nav class="nav" aria-label="Main">
      ${nav("review", "Review")}${nav("me", "My work")}${nav("progress", "Progress")}
      ${admin ? html`<button class="nav-btn ${active.startsWith("admin/") ? "on" : ""}" data-act="menu" aria-haspopup="menu" aria-expanded="false" id="admin-menu-btn">Admin <span class="caret" aria-hidden="true">▾</span></button>
        <div class="menu" role="menu" hidden>${adminLinks.map(([k, label, desc]) =>
          html`<a role="menuitem" href="#/admin/${k}" class="${active === "admin/" + k ? "on" : ""}">${label}<span class="menu-desc">${desc}</span></a>`)}</div>` : ""}
    </nav>
    <div class="topbar-right">
      ${S.me.local ? html`<label class="who"><span>Reviewing as</span><input class="input" id="who" value="${S.reviewer}" placeholder="your name" autocomplete="name" spellcheck="false" dir="auto"></label>
        <button class="icon-btn" data-act="menu" aria-haspopup="menu" aria-expanded="false" aria-label="Settings" title="Settings">${ICON.more}</button>
        <div class="menu align-right" role="menu" hidden>
          <button role="menuitem" data-act="shortcuts">Keyboard shortcuts <span class="menu-desc"><kbd>?</kbd></span></button>
          <button role="menuitem" data-act="theme">${themeLabel}</button>
          <a role="menuitem" href="/api/export">${ICON.download} Download reviews (zip)</a>
        </div>`
      : html`<button class="user-btn" data-act="menu" aria-haspopup="menu" aria-expanded="false" aria-label="Account menu" id="user-menu-btn">
          <span class="avatar" aria-hidden="true">${initial}</span><span class="user-name" dir="auto">${displayName()}</span><span class="caret faint" aria-hidden="true">▾</span></button>
        <div class="menu align-right" role="menu" hidden>
          <div class="menu-head"><span class="avatar lg" aria-hidden="true">${initial}</span><div><b dir="auto">${displayName()}</b><span>${S.reviewer} · ${S.me.role === "admin" ? "Admin" : "Reviewer"}</span></div></div>
          <hr>
          ${S.me.can_change_password ? html`<a role="menuitem" href="#/password">Change password</a>` : ""}
          <button role="menuitem" data-act="shortcuts">Keyboard shortcuts <span class="menu-desc"><kbd>?</kbd></span></button>
          <button role="menuitem" data-act="theme">${themeLabel}</button>
          ${admin ? html`<a role="menuitem" href="/api/export">Download reviews (zip)</a>` : ""}
          <hr>
          <button role="menuitem" class="danger-item" data-act="signout" id="signout-btn">Sign out</button>
        </div>`}
    </div></div>`);
  const who = $("#who");
  if (who) {
    who.addEventListener("change", () => {
      S.reviewer = who.value.trim().toLowerCase().replace(/[^\p{L}\p{N}_-]+/gu, "-").replace(/^[-_]+|[-_]+$/g, "").slice(0, 40);  // as reviewer_slug
      who.value = S.reviewer;
      store.set("fb-reviewer", S.reviewer);
      S.items = null; S.cur = null;
      route();
    });
    who.addEventListener("keydown", e => { if (e.key === "Enter") who.blur(); });
  }
}

// ------------------------------------------------------------------ sign in

function renderLogin(view) {
  delete view.dataset.screen;
  mount(view, html`<div class="auth-page"><form class="auth-card" id="login-form" novalidate>
    <div class="auth-brand"><span class="brand-mark lg" aria-hidden="true">ف</span>
      <div><div class="auth-title">FarhangBench</div><div class="auth-sub">Item review · <span class="fa">بازبینی</span></div></div></div>
    <div><h1>Sign in</h1></div>
    <p class="lead">Use the name and password an admin gave you.</p>
    <label class="field"><span>Name</span>
      <input class="input" id="li-name" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" required autofocus></label>
    <label class="field"><span>Password</span>
      <span class="pw-wrap"><input class="input" id="li-pass" name="password" type="password" autocomplete="current-password" required>
      <button type="button" class="pw-toggle" data-act="pwtoggle" aria-label="Show password" aria-pressed="false">Show</button></span></label>
    <div class="form-error" id="li-error" role="alert" hidden></div>
    <button class="btn primary lg block" type="submit" id="li-btn">Sign in</button>
    <p class="auth-foot">Forgot your password? Ask an admin to reset it.</p>
  </form></div>`);
  const form = $("#login-form"), err = $("#li-error"), btn = $("#li-btn");
  let timer = null;
  const fail = text => { err.textContent = text; err.hidden = false; };
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const name = $("#li-name").value.trim(), password = $("#li-pass").value;
    if (!name || !password) return fail(!name ? "Enter your name." : "Enter your password.");
    err.hidden = true; btn.disabled = true; btn.textContent = "Signing in…";
    try {
      const res = await api("/api/login", { name, password });
      S.me = res.user; S.reviewer = res.user.reviewer || ""; S.items = null;
      if (!location.hash || location.hash === "#/login") history.replaceState(null, "", location.pathname + "#/review");
      route();
    } catch (ex) {
      btn.disabled = false; btn.textContent = "Sign in";
      if (ex.status === 429) {
        let wait = ex.data.retry_after || 60;
        const tick = () => {
          const m = Math.floor(wait / 60), s = wait % 60;
          fail(`Too many failed tries. Try again in ${m ? m + " min " : ""}${s} s.`);
          btn.disabled = wait > 0;
          if (wait-- <= 0) { clearInterval(timer); err.hidden = true; }
        };
        clearInterval(timer); tick(); timer = setInterval(tick, 1000);
      } else {
        fail(ex.status === 401 ? "Wrong name or password." : ex.message);
        $("#li-pass").select();
      }
    }
  });
  $("#li-name").focus();
}

function pwStrength(p) {
  if (!p) return 0;
  let s = p.length >= 10 ? 2 : 1;
  if (p.length >= 14) s++;
  if (/[^A-Za-z]/.test(p) && /[A-Za-z]/.test(p) && p.length >= 10) s++;
  return Math.min(4, s);
}
function renderPassword(view, forced) {
  delete view.dataset.screen;
  if (!forced && !S.me.can_change_password) {
    return viewMessage(view, "Password managed elsewhere", "Your account comes from the deployment's users Secret, so its password is changed there.");
  }
  const inner = html`<form class="auth-card" id="pw-form" novalidate>
    ${forced ? html`<div class="auth-brand"><span class="brand-mark lg" aria-hidden="true">ف</span>
      <div><div class="auth-title">Welcome, <span dir="auto">${displayName()}</span></div><div class="auth-sub">One last step before you start</div></div></div>` : ""}
    <div><h1>${forced ? "Choose your password" : "Change your password"}</h1></div>
    <p class="lead">${forced ? "You signed in with a one-time password. Choose your own to continue; only you will know it." : "Other devices you're signed in on will be signed out."}</p>
    ${forced ? "" : html`<label class="field"><span>Current password</span><span class="pw-wrap">
      <input class="input" id="pw0" type="password" autocomplete="current-password" autofocus>
      <button type="button" class="pw-toggle" data-act="pwtoggle" aria-label="Show password" aria-pressed="false">Show</button></span></label>`}
    <label class="field"><span>New password</span><span class="pw-wrap">
      <input class="input" id="pw1" type="password" autocomplete="new-password" ${forced ? "autofocus" : ""} aria-describedby="pw-hint">
      <button type="button" class="pw-toggle" data-act="pwtoggle" aria-label="Show password" aria-pressed="false">Show</button></span>
      <span class="meter" id="pw-meter" aria-hidden="true"><span></span><span></span><span></span><span></span></span>
      <span class="pw-hint" id="pw-hint">At least 10 characters. A few unrelated words work well.</span></label>
    <label class="field"><span>New password again</span>
      <input class="input" id="pw2" type="password" autocomplete="new-password"></label>
    <div class="form-error" id="pw-error" role="alert" hidden></div>
    <button class="btn primary lg block" type="submit" id="pw-save">${forced ? "Save and continue" : "Save password"}</button>
    ${forced ? html`<button class="btn ghost block" type="button" data-act="signout">Sign out</button>` : html`<a class="btn ghost block" href="#/review">Cancel</a>`}
  </form>`;
  mount(view, forced ? html`<div class="auth-page">${inner}</div>` : html`<div class="page narrow"><div class="auth-page auth-inline">${inner}</div></div>`);
  const p1 = $("#pw1"), hint = $("#pw-hint"), meter = $("#pw-meter"), err = $("#pw-error");
  p1.addEventListener("input", () => {
    const s = pwStrength(p1.value);
    meter.className = "meter m" + s;
    const left = 10 - p1.value.length;
    hint.textContent = left > 0 ? `${left} more character${left === 1 ? "" : "s"} needed.` : "Long enough.";
    hint.classList.toggle("ok", left <= 0);
  });
  $("#pw-form").addEventListener("submit", async e => {
    e.preventDefault();
    err.hidden = true;
    const body = { new_password: p1.value };
    if (p1.value.length < 10) { err.textContent = "Use at least 10 characters."; err.hidden = false; return p1.focus(); }
    if (p1.value !== $("#pw2").value) { err.textContent = "The two passwords don't match."; err.hidden = false; return $("#pw2").focus(); }
    if (!forced) body.current_password = $("#pw0").value;
    const btn = $("#pw-save"); btn.disabled = true;
    try {
      await api("/api/password", body);
      S.me = await api("/api/whoami");
      toast(forced ? "Password saved. Welcome!" : "Password changed. Other sessions were signed out.");
      replaceHash("#/review");
    } catch (ex) { btn.disabled = false; err.textContent = ex.message; err.hidden = false; }
  });
  (forced ? p1 : $("#pw0")).focus();
}

// ------------------------------------------------------------------ review: data

async function loadItems(force) {
  if (!S.reviewer) { S.items = []; return S.items; }
  if (!S.items || force || Date.now() - S.itemsAt > 5 * 60 * 1000) {
    S.items = await api(withWho("/api/items"));
    S.itemsAt = Date.now();
  }
  return S.items;
}
function inScope(it, scope) {
  if (scope === "all") return true;
  if (scope === "gated") return it.gated;
  return (scope === "native") === (it.source === NATIVE);
}
function queue() {
  const f = S.filters, q = f.q.trim().toLowerCase();
  return (S.items || []).filter(it => {
    if (!inScope(it, f.scope)) return false;
    if (f.track && it.track !== f.track) return false;
    if (f.task && it.task !== f.task) return false;
    const live = it.status !== "retired" && it.status !== "outdated";
    if (f.show === "todo" && (it.my_verdict || it.peeked || !live)) return false;
    if (f.show === "mine" && !(it.my_verdict || it.answered)) return false;
    if (f.show === "disputed" && it.status !== "disputed") return false;
    if (q && !it.id.toLowerCase().includes(q) && !String(it.prompt).toLowerCase().includes(q)) return false;
    return true;
  });
}
const isTodo = it => !it.my_verdict && !it.peeked && it.status !== "retired" && it.status !== "outdated";

// ------------------------------------------------------------------ review: screen

async function viewReview(view, id, stale) {
  if (!S.reviewer) return renderWelcome(view);
  if (view.dataset.screen !== "review" || !$("#queue-list", view)) {
    if (!S.items) loading(view);
    await loadItems();
    if (stale()) return;
    renderReviewShell(view);
  } else {
    await loadItems();
    if (stale()) return;
  }
  const layout = $(".review", view);
  layout.classList.toggle("has-item", !!id);
  if (!id) {
    S.cur = null;
    renderQueue();
    return renderPaneIntro();
  }
  await openItem(id, stale);
}

function renderWelcome(view) {
  delete view.dataset.screen;
  mount(view, html`<div class="page narrow"><div class="welcome card card-pad">
    <h1>Welcome to FarhangBench review</h1>
    <p class="muted">Type your name in the top bar to start. Your reviews are saved under that name.</p>
    ${howItWorks()}
  </div></div>`);
  const who = $("#who"); if (who) who.focus();
}
function howItWorks() {
  return html`<ol>
    <li><div><b>Answer first, without the key.</b> Pick or type the answer you believe is right, or press “I don't know”. Your first answer is saved and can't be changed; that's what makes the review independent.</div></li>
    <li><div><b>Then judge the key.</b> Mark the official answer correct, wrong or not sure, tick any problems, and leave a note or the right answer if you know it.</div></li>
    <li><div><b>Judge for Iran today.</b> If something was the norm once but isn't any more, mark the key wrong and tick “No longer the norm in Iran today”.</div></li>
    <li><div>An item counts towards the FarhangScore once two reviewers other than its author have answered and at least two thirds say the key is correct. Check keys against reliable sources (Dehkhoda, Ganjoor, Farhangestan), not an AI model.</div></li>
  </ol>`;
}

function renderReviewShell(view) {
  view.dataset.screen = "review";
  const f = S.filters;
  const tracks = Object.entries(META.tracks);
  mount(view, html`<div class="review">
    <aside class="queue" aria-label="Item queue">
      <div class="queue-filters">
        <div class="seg" role="group" aria-label="Show">${Object.entries(SHOWS).map(([k, label]) =>
          html`<button type="button" data-act="show" data-v="${k}" aria-pressed="${String(f.show === k)}">${label}</button>`)}</div>
        <div class="search">${ICON.search}<input class="input sm" type="search" id="q" placeholder="Search id or text" value="${f.q}" aria-label="Search items" spellcheck="false" dir="auto"><kbd class="desktop-only" aria-hidden="true">/</kbd></div>
        <select class="input sm" id="f-scope" aria-label="Source">${Object.entries(SCOPES).map(([k, label]) => html`<option value="${k}" ${f.scope === k ? "selected" : ""}>${label}</option>`)}</select>
        <div class="row2">
          <select class="input sm" id="f-track" aria-label="Track"><option value="">All tracks</option>${tracks.map(([k, v]) => html`<option value="${k}" ${f.track === k ? "selected" : ""}>${k} · ${v}</option>`)}</select>
          <select class="input sm" id="f-task" aria-label="Task"></select>
        </div>
      </div>
      <div class="queue-meta" id="queue-meta"></div>
      <div class="queue-list" id="queue-list" role="list"></div>
    </aside>
    <section class="pane" id="pane" aria-label="Item"><div class="pane-inner" id="pane-inner"></div></section>
  </div>`);
  fillTasks();
  const set = (k, v) => { S.filters[k] = v; if (k !== "q") store.set("fb-" + k, v); S.limit = 200; renderQueue(); if (!S.cur) renderPaneIntro(); };
  $("#f-scope").addEventListener("change", e => set("scope", e.target.value));
  $("#f-track").addEventListener("change", e => { S.filters.task = ""; store.set("fb-task", ""); fillTasks(); set("track", e.target.value); });
  $("#f-task").addEventListener("change", e => set("task", e.target.value));
  let t = null;
  $("#q").addEventListener("input", e => { clearTimeout(t); t = setTimeout(() => set("q", e.target.value), 120); });
  $("#q").addEventListener("keydown", e => { if (e.key === "Escape") { e.target.value = ""; set("q", ""); e.target.blur(); } });
}
function fillTasks() {
  const el = $("#f-task"); if (!el) return;
  const tr = S.filters.track;
  const tasks = Object.keys(META.tasks).filter(t => !tr || t.startsWith(tr));
  mount(el, html`<option value="">All tasks</option>${tasks.map(t => html`<option value="${t}" ${S.filters.task === t ? "selected" : ""}>${t}</option>`)}`);
}
ACT.show = el => {
  S.filters.show = el.dataset.v; store.set("fb-show", el.dataset.v); S.limit = 200;
  $$(".seg [data-act=show]").forEach(b => b.setAttribute("aria-pressed", String(b === el)));
  renderQueue(); if (!S.cur) renderPaneIntro();
};
ACT.more = () => { S.limit += 300; renderQueue(); };

function renderQueue() {
  const list = $("#queue-list"); if (!list) return;
  const rows = queue();
  const all = S.items || [];
  const todoAll = all.filter(it => inScope(it, S.filters.scope) && isTodo(it) && !it.answered).length;
  const answeredAll = all.filter(it => inScope(it, S.filters.scope) && isTodo(it) && it.answered).length;
  const doneAll = all.filter(it => inScope(it, S.filters.scope) && it.my_verdict).length;
  mount($("#queue-meta"), html`<span><b>${n(rows.length)}</b> ${rows.length === 1 ? "item" : "items"}</span><span class="spacer"></span>
    <span title="Reviewed by you in this source scope">${n(doneAll)} done · ${n(answeredAll)} answered · ${n(todoAll)} to do</span>`);
  const sel = S.cur && S.cur.id;
  const shown = rows.slice(0, S.limit);
  mount(list, html`${shown.map(it => html`<a role="listitem" class="qrow ${it.id === sel ? "sel" : ""}" href="#/review/${enc(it.id)}" data-id="${it.id}" ${it.id === sel ? html`aria-current="true"` : ""}>
      <div class="qrow-top"><span class="dot st-${it.status}" title="${STATUS[it.status] || it.status}"></span><code>${it.id}</code><span class="task">${it.task}</span>
        ${it.my_verdict ? html`<span class="chip v-${it.my_verdict}">${VERDICT_ICON[it.my_verdict]} ${VERDICT[it.my_verdict]}</span>`
          : it.peeked ? html`<span class="chip t-violet" title="You saw the key in the admin view">key seen</span>`
          : it.answered ? html`<span class="chip t-info">answered</span>` : ""}</div>
      <div class="qrow-text clamp2" dir="auto">${it.prompt}</div></a>`)}
    ${rows.length > shown.length ? html`<div class="queue-more"><button class="btn sm" data-act="more">Show more (${n(rows.length - shown.length)} left)</button></div>` : ""}
    ${rows.length ? "" : html`<div class="empty"><div class="empty-icon" aria-hidden="true">✓</div><b>${S.filters.show === "todo" ? "Nothing left to do here" : "No items"}</b>
      <p>${S.filters.show === "todo" ? "You've reviewed every item in this view. Try another source or track." : "Try other filters."}</p></div>`}`);
}
function scrollQueueToSel() {
  const el = $("#queue-list .qrow.sel");
  if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
}

function renderPaneIntro() {
  const inner = $("#pane-inner"); if (!inner) return;
  const next = queue().find(isTodo);
  const todo = (S.items || []).filter(isTodo).length;
  mount(inner, html`<div class="welcome">
    <h1>${todo ? "Ready when you are" : "All caught up"}</h1>
    <p class="muted">${todo ? html`You have <b>${n(todo)}</b> items to review across all sources.` : "There's nothing left in your queue."}</p>
    ${howItWorks()}
    <div class="actions">${next ? html`<a class="btn primary lg" href="#/review/${enc(next.id)}" id="start-btn">Start with the next item <kbd>J</kbd></a>` : ""}
      <button class="btn ghost" data-act="shortcuts">Keyboard shortcuts <kbd>?</kbd></button></div>
  </div>`);
}

async function openItem(id, stale) {
  const it = (S.items || []).find(x => x.id === id);
  S.cur = { id, item: it, key: null, choice: null, verdict: null, flags: new Set() };
  renderQueue(); scrollQueueToSel();
  const inner = $("#pane-inner");
  if (!it) {
    return mount(inner, html`<div class="card empty"><b>Not in your queue</b><p>There's no item <code>${id}</code> for you to review. It may not exist, or you're its author.</p>
      ${isAdmin() ? html`<a class="btn" href="#/admin/items/${enc(id)}">Open in the admin audit view</a>` : ""}</div>`);
  }
  if (it.answered) {
    mount(inner, html`<div class="boot"><span class="spinner"></span></div>`);
    try {
      const key = await api(withWho(`/api/key?id=${enc(id)}`));
      if (stale() || !S.cur || S.cur.id !== id) return;
      S.cur.key = key;
      S.cur.verdict = (key.review || {}).verdict || null;
      S.cur.flags = new Set((key.review || {}).flags || []);
      return renderKeyPane();
    } catch (e) { if (stale()) return; }
  }
  renderBlindPane();
}

function neighbours() {
  const rows = queue(), i = S.cur ? rows.findIndex(r => r.id === S.cur.id) : -1;
  return { rows, i, prev: i > 0 ? rows[i - 1] : null, next: i >= 0 ? rows[i + 1] || null : rows[0] || null };
}
function itemHeader(it, stepsHtml) {
  const { rows, i, prev, next } = neighbours();
  return html`<div class="item-bar">
      <a class="btn sm back-queue" href="#/review">${ICON.left} Queue</a>
      <span class="pos">${i >= 0 ? `${n(i + 1)} of ${n(rows.length)}` : "Not in this view"}</span>
      <span class="spacer"></span>
      <button class="btn ghost sm" data-act="copylink" title="Copy a link to this item">${ICON.link}<span class="desktop-only">Copy link</span></button>
      <button class="icon-btn" data-act="nav" data-dir="prev" ${prev ? "" : "disabled"} aria-label="Previous item (K)" title="Previous (K)">${ICON.left}</button>
      <button class="icon-btn" data-act="nav" data-dir="next" ${next ? "" : "disabled"} aria-label="Next item (J)" title="Next (J)">${ICON.right}</button>
    </div>
    <header class="item-head">
      <div class="id-line"><h1>${it.id}</h1><span class="chip st-${it.status}"><span class="dot st-${it.status}"></span>${STATUS[it.status] || it.status}${it.verdicts ? ` · ${it.verdicts}` : ""}</span>
        ${it.source !== NATIVE ? html`<span class="chip outline">${it.source}</span>` : ""}${it.gated ? html`<span class="chip t-warn" title="Counts only once validated">gated</span>` : ""}</div>
      <div class="task-line">${META.tracks[it.track] || it.track} · <b>${it.task}</b>${META.tasks[it.task] ? ` — ${META.tasks[it.task]}` : ""}</div>
    </header>
    ${stepsHtml}`;
}
function steps(phase) {
  return html`<ol class="steps" aria-label="Steps"><li class="${phase === 1 ? "on" : "done"}" ${phase === 1 ? html`aria-current="step"` : ""}>Answer blind</li><span class="sep" aria-hidden="true"></span>
    <li class="${phase === 2 ? "on" : ""}" ${phase === 2 ? html`aria-current="step"` : ""}>Judge the key</li></ol>`;
}
function itemBody(it) {
  const imgs = (it.images || []).length ? html`<div class="images">${it.images.map(im => html`<figure><img src="/${im.path}" alt="Item image" loading="lazy"><figcaption>${im.credit || ""}${im.license ? ` · ${im.license}` : ""}</figcaption></figure>`)}</div>` : "";
  return html`${imgs}
    ${it.context ? html`<div class="block"><h2 class="label">Context <span class="fa">متن</span></h2><div class="prose ctx" dir="auto">${it.context}</div></div>` : ""}
    <div class="block"><h2 class="label">Question <span class="fa">پرسش</span></h2><div class="prose q" dir="auto" lang="${it.lang || "fa"}">${it.prompt}</div></div>`;
}
const optionsOf = it => it.format === "mcq" ? it.options || [] : it.format === "judgment" ? it.labels || [] : null;

function renderBlindPane() {
  const it = S.cur.item, inner = $("#pane-inner");
  const opts = optionsOf(it);
  let answer;
  if (it.peeked) {
    answer = html`<div class="notice t-violet"><span class="notice-icon">!</span><div>You've seen this item's key in the admin audit view, so your answer wouldn't be blind. Leave this one to other reviewers.</div></div>`;
  } else {
    answer = html`<div class="answer-card">
      <h2 class="label">Your answer <span class="fa">پاسخ شما</span><span class="dot-sep" aria-hidden="true">·</span><span class="muted"> before you see the key</span></h2>
      ${opts ? html`<div class="options" role="radiogroup" aria-label="Options" dir="${it.lang === "en" ? "ltr" : "rtl"}">${opts.map((o, i) => html`<button type="button" class="option" role="radio" aria-checked="false" data-act="pick" data-i="${i}">
          <kbd>${i < 9 ? i + 1 : ""}</kbd><span class="opt-text" dir="auto">${o}</span></button>`)}</div>`
        : it.format === "generation" ? html`<textarea class="input answer-input" id="ans" dir="auto" aria-label="Your answer" placeholder="Optional: write your own answer. Generation items are scored by a rubric, so your verdict on the rubric is what counts."></textarea>`
        : html`<input class="input answer-input" id="ans" dir="auto" aria-label="Your answer" placeholder="Type your answer" autocomplete="off" spellcheck="false">`}
      <div class="actions">
        <button class="btn primary" data-act="submit-blind" id="submit-blind">Submit answer <kbd>↵</kbd></button>
        <button class="btn" data-act="idk" id="idk-btn">I don't know <span class="fa">· نمی‌دانم</span></button>
        <span class="hint">Your first answer is final.</span>
      </div>
      <div class="err-box" id="pane-err"></div>
    </div>`;
  }
  mount(inner, html`<article>${itemHeader(it, steps(1))}${itemBody(it)}${answer}</article>`);
  $("#pane").scrollTop = 0; window.scrollTo(0, 0);
  if (it.own_source && !it.peeked) $("#pane-err").before(mount(document.createElement("div"), html`<div class="notice t-info"><span class="notice-icon">i</span><div>You're an author of this dataset, so your verdict won't count towards validation; your flags are still shown to admins.</div></div>`));
  const input = $("#ans");
  if (input && matchMedia("(min-width: 861px)").matches) input.focus();
}
ACT.pick = el => pick(+el.dataset.i);
function pick(i) {
  if (!S.cur) return;
  S.cur.choice = i;
  $$(".option[data-act=pick]").forEach(b => b.setAttribute("aria-checked", String(+b.dataset.i === i)));
}
function paneError(text) {
  const box = $("#pane-err");
  if (box) mount(box, html`<div class="form-error" role="alert">${text}</div>`); else toast(text, "error");
}
ACT["submit-blind"] = () => submitBlind(false);
ACT.idk = () => submitBlind(true);
async function submitBlind(dontKnow) {
  const cur = S.cur; if (!cur || cur.key || cur.busy) return;
  const it = cur.item;
  let answer = null;
  if (!dontKnow) {
    if (it.format === "mcq") answer = cur.choice;
    else if (it.format === "judgment") answer = cur.choice === null ? null : it.labels[cur.choice];
    else answer = ($("#ans") || {}).value ? $("#ans").value.trim() : "";
    if ((answer === null || answer === "") && it.format !== "generation") return paneError("Pick or type an answer, or press “I don't know”.");
  }
  cur.busy = true;
  $$("#submit-blind, #idk-btn").forEach(b => { b.disabled = true; });
  try {
    const key = await api("/api/blind", { reviewer: S.reviewer, item_id: it.id, answer });
    it.answered = true;
    if (S.cur !== cur) return;
    cur.key = key; cur.busy = false;
    cur.verdict = (key.review || {}).verdict || null;
    cur.flags = new Set((key.review || {}).flags || []);
    renderQueue();
    renderKeyPane();
  } catch (e) {
    cur.busy = false;
    $$("#submit-blind, #idk-btn").forEach(b => { b.disabled = false; });
    paneError(e.message);
  }
}

function fmtAnswer(it, ans) {
  if (ans === null || ans === undefined || ans === "") return null;
  if (it.format === "mcq" && typeof ans === "number" && it.options) return `${ans + 1}. ${it.options[ans] ?? "?"}`;
  return String(ans);
}
function keyBlock(it, key, mine, showMine = true) {
  const a = key.answer || {}, opts = optionsOf(it);
  let out;
  if (opts) {
    const keyIdx = it.format === "mcq" ? a.index : opts.indexOf(a.label);
    const mineIdx = mine === undefined || mine === null ? -1 : it.format === "mcq" ? mine : opts.indexOf(mine);
    out = html`<div class="block"><h2 class="label">Options and key <span class="fa">گزینه‌ها و پاسخ</span></h2><div class="options" dir="${it.lang === "en" ? "ltr" : "rtl"}">${opts.map((o, i) => {
      const isKey = i === keyIdx, isMine = showMine && i === mineIdx;
      return html`<div class="option ${isKey ? "is-key" : isMine ? "is-wrongpick" : "is-dim"}"><kbd>${i + 1}</kbd><span class="opt-text" dir="auto">${o}</span>
        <span class="opt-tags">${isKey ? html`<span class="chip t-ok">✓ Key</span>` : ""}${isMine ? html`<span class="chip ${isKey ? "t-ok" : "t-bad"}">Your answer</span>` : ""}
        ${key.distractor_types && key.distractor_types[i] && !isKey ? html`<span class="chip outline">${key.distractor_types[i]}</span>` : ""}
        ${a.abstain_label && o === a.abstain_label ? html`<span class="chip outline">abstain</span>` : ""}</span></div>`;
    })}</div></div>`;
  } else {
    out = html`<div class="block"><h2 class="label">Key <span class="fa">پاسخ رسمی</span></h2>
      ${a.accept ? html`<div class="accepts">${a.accept.map(x => html`<span dir="auto">${x}</span>`)}</div>` : html`<p class="muted">Scored by the rubric below.</p>`}
      ${a.match ? html`<p class="faint">Match: ${a.match}</p>` : ""}
      ${a.reject && a.reject.length ? html`<h2 class="label">Rejected answers</h2><div class="accepts reject">${a.reject.map(x => html`<span dir="auto">${x}</span>`)}</div>` : ""}</div>
      ${showMine ? html`<div class="block"><h2 class="label">Your answer <span class="fa">پاسخ شما</span></h2><div class="prose sm" dir="auto">${mine === null || mine === undefined || mine === "" ? html`<span class="faint">(none)</span>` : mine}</div></div>` : ""}`;
  }
  const md = key.metadata || {};
  const notes = ["variation_note", "sources", "as_of", "region", "difficulty", "category"].filter(k => md[k] && (!Array.isArray(md[k]) || md[k].length));
  return html`${out}
    ${key.rubric ? html`<div class="block"><h2 class="label">Rubric <span class="fa">معیارها</span></h2><table class="kv">${key.rubric.map(c => html`<tr><td>${c.id}${c.gate ? html` <span class="chip t-warn">gate</span>` : ""}</td><td dir="auto">${c.description}</td></tr>`)}</table></div>` : ""}
    ${key.reference ? html`<div class="block"><h2 class="label">Reference answer</h2><div class="prose sm" dir="auto">${key.reference}</div></div>` : ""}
    ${notes.length ? html`<div class="block"><h2 class="label">Notes from the author</h2><table class="kv">${notes.map(k => html`<tr><td>${k.replace(/_/g, " ")}</td><td dir="auto">${Array.isArray(md[k]) ? md[k].join(", ") : md[k]}</td></tr>`)}</table></div>` : ""}`;
}

function renderKeyPane() {
  const cur = S.cur, it = cur.item, key = cur.key, rv = key.review || {};
  const mine = rv.blind_answer;
  const result = rv.blind_correct === true ? html`<div class="result t-ok"><span class="result-icon">✓</span>Your blind answer matches the key.</div>`
    : rv.blind_correct === false ? html`<div class="result t-bad"><span class="result-icon">✗</span>${mine === null || mine === "" ? "You didn't know the answer." : "Your blind answer differs from the key. Is the key wrong, or is your answer?"}</div>`
    : html`<div class="result t-info"><span class="result-icon">i</span>Judge whether the key, rubric and reference describe a good answer.</div>`;
  const vb = v => html`<button type="button" class="vbtn v-${v}" data-act="verdict" data-v="${v}" aria-pressed="${String(cur.verdict === v)}">
    <span class="vicon" aria-hidden="true">${VERDICT_ICON[v]}</span><span class="vtext"><b>${VERDICT[v]}</b><span class="fa">${VERDICT_FA[v]}</span></span><kbd>${v[0].toUpperCase()}</kbd></button>`;
  mount($("#pane-inner"), html`<article>${itemHeader(it, steps(2))}${itemBody(it)}${result}
    ${it.own_source ? html`<div class="notice t-info block"><span class="notice-icon">i</span><div>You're an author of this dataset, so your verdict won't count towards validation; your flags are still shown to admins.</div></div>` : ""}
    ${keyBlock(it, key, mine)}
    <section class="verdict-card" aria-labelledby="vh">
      <h2 class="label" id="vh">Is the key correct? <span class="fa">آیا پاسخ رسمی درست است؟</span></h2>
      <div class="verdicts" role="group" aria-label="Verdict">${vb("correct")}${vb("wrong")}${vb("unsure")}</div>
      <h2 class="label">Problems with the item <span class="muted">· optional</span></h2>
      <div class="flag-chips">${Object.entries(META.flags).map(([k, label]) => html`<button type="button" class="fchip" data-act="flag" data-f="${k}" aria-pressed="${String(cur.flags.has(k))}">${label}</button>`)}</div>
      <div class="grid2">
        <label class="field"><span>Correct answer, if the key is wrong</span><input class="input" id="sugg" dir="auto" value="${rv.suggested_answer || ""}" autocomplete="off"></label>
        <label class="field"><span>Comment and source</span><textarea class="input" id="comment" dir="auto" rows="2" placeholder="e.g. Dehkhoda gives …">${rv.comment || ""}</textarea></label>
      </div>
      <div class="actions">
        <button class="btn primary" data-act="save-verdict" id="save-verdict">Save and next <kbd>↵</kbd></button>
        <span class="hint">${rv.verdict ? html`You saved <b>${VERDICT[rv.verdict]}</b> ${timeEl(rv.verdict_time)}; saving again replaces it.` : "Ctrl+Enter saves from a text field."}</span>
      </div>
      <div class="err-box" id="pane-err"></div>
    </section></article>`);
  $("#pane").scrollTop = 0;
}
ACT.verdict = el => setVerdict(el.dataset.v);
function setVerdict(v) {
  if (!S.cur || !S.cur.key) return;
  S.cur.verdict = v;
  $$(".vbtn").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === v)));
}
ACT.flag = el => {
  const f = el.dataset.f, set = S.cur.flags;
  set.has(f) ? set.delete(f) : set.add(f);
  el.setAttribute("aria-pressed", String(set.has(f)));
};
ACT["save-verdict"] = () => saveVerdict();
async function saveVerdict() {
  const cur = S.cur; if (!cur || !cur.key || cur.busy) return;
  if (!cur.verdict) return paneError("Choose correct, wrong or not sure.");
  cur.busy = true;
  const btn = $("#save-verdict"); if (btn) btn.disabled = true;
  const flags = [...cur.flags];
  try {
    await api("/api/verdict", { reviewer: S.reviewer, item_id: cur.id, verdict: cur.verdict, flags,
      comment: $("#comment").value, suggested_answer: $("#sugg").value });
    const it = cur.item;
    const wasTodo = !it.my_verdict;
    it.my_verdict = cur.verdict; it.my_flags = flags; it.my_time = new Date().toISOString(); it.answered = true;
    if (wasTodo) it.verdicts = (it.verdicts || 0) + 1;
    toast(`Saved: ${VERDICT[cur.verdict]}${flags.length ? ` · ${plural(flags.length, "flag")}` : ""}`);
    cur.busy = false;
    const ix = new Map(S.items.map((x, i) => [x, i])), pos = ix.get(it);
    const cands = queue().filter(r => r !== it && isTodo(r));
    const next = cands.find(r => ix.get(r) > pos) || cands[0];
    if (next) location.hash = `#/review/${enc(next.id)}`;
    else { toast("That was the last item in this view."); location.hash = "#/review"; }
  } catch (e) { cur.busy = false; if (btn) btn.disabled = false; paneError(e.message); }
}
ACT.nav = el => step(el.dataset.dir === "next" ? 1 : -1);
function step(d) {
  const { prev, next } = neighbours();
  const target = d > 0 ? next : prev;
  if (target) location.hash = `#/review/${enc(target.id)}`;
}
ACT.copylink = async () => { const ok = await copyText(location.href); toast(ok ? "Link copied" : "Couldn't copy the link", ok ? "ok" : "error"); };

// ------------------------------------------------------------------ keyboard

function showShortcuts() {
  const k = (keys, label) => html`<div><span>${label}</span><span>${keys.map(x => html`<kbd>${x}</kbd>`)}</span></div>`;
  openDialog(html`<form method="dialog"><div class="dialog-body"><h2>Keyboard shortcuts</h2>
    <div class="help-grid">
      <h3>Review</h3>
      ${k(["1", "–", "9"], "Pick an option")}${k(["↵"], "Submit answer / save verdict")}
      ${k(["C"], "Key is correct")}${k(["W"], "Key is wrong")}
      ${k(["U"], "Not sure")}${k(["Ctrl", "↵"], "Save from a text field")}
      <h3>Moving around</h3>
      ${k(["J"], "Next item")}${k(["K"], "Previous item")}
      ${k(["N"], "Next item")}${k(["P"], "Previous item")}
      ${k(["/"], "Search the queue")}${k(["Esc"], "Leave a field / close")}
      ${k(["?"], "This help")}${k(["G", "then", "R"], "Go to review")}
    </div></div>
    <div class="dialog-foot"><button class="btn primary" value="ok" autofocus>Got it</button></div></form>`, { wide: true });
}
let gPending = 0;
document.addEventListener("keydown", e => {
  if (e.defaultPrevented || e.altKey) return;
  if ($("dialog[open]")) return;
  const t = e.target, tag = t.tagName;
  const typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || t.isContentEditable;
  const onReview = document.body.dataset.view === "review" && S.cur;
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && onReview) {
    e.preventDefault();
    return S.cur.key ? saveVerdict() : submitBlind(false);
  }
  if (typing) {
    if (e.key === "Escape") t.blur();
    if (e.key === "Enter" && onReview && t.id === "ans" && tag === "INPUT") { e.preventDefault(); submitBlind(false); }
    if (e.key === "Enter" && onReview && t.id === "sugg") { e.preventDefault(); saveVerdict(); }
    return;
  }
  if (e.ctrlKey || e.metaKey) return;
  const key = e.key;
  if (key === "?") { e.preventDefault(); return showShortcuts(); }
  if (gPending && Date.now() - gPending < 1200) {
    gPending = 0;
    const dest = { r: "#/review", m: "#/me", p: "#/progress", a: "#/admin/activity", i: "#/admin/items" }[key.toLowerCase()];
    if (dest) { e.preventDefault(); location.hash = dest; }
    return;
  }
  if (key === "g") { gPending = Date.now(); return; }
  if (document.body.dataset.view !== "review" || !$("#queue-list")) return;
  if (key === "/") { e.preventDefault(); const q = $("#q"); if (q) { if (!$(".review.has-item") || matchMedia("(min-width: 861px)").matches) q.focus(); } return; }
  const lower = key.toLowerCase();
  if (lower === "j" || lower === "n") { e.preventDefault(); return step(1); }
  if (lower === "k" || lower === "p") { e.preventDefault(); return step(-1); }
  if (!S.cur) return;
  if (!S.cur.key) {
    if (/^[1-9]$/.test(key) && $(`.option[data-i="${+key - 1}"]`)) { e.preventDefault(); return pick(+key - 1); }
    if (key === "Enter" && (tag !== "BUTTON" || t.dataset.act === "pick")) { e.preventDefault(); return submitBlind(false); }
    return;
  }
  const v = { c: "correct", w: "wrong", u: "unsure" }[lower];
  if (v) { e.preventDefault(); return setVerdict(v); }
  if (key === "Enter" && (tag !== "BUTTON" || ["verdict", "flag"].includes(t.dataset.act))) { e.preventDefault(); return saveVerdict(); }
});

// ------------------------------------------------------------------ shared bits for stats

function sparkline(perDay, w = 84, h = 22) {
  const list = perDay || [];
  const max = Math.max(1, ...list.map(d => d.n));
  const bw = w / Math.max(1, list.length);
  const total = list.reduce((a, d) => a + d.n, 0);
  return html`<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="${total} in the last 14 days">${list.map((d, i) => {
    const bh = d.n ? Math.max(3, Math.round((d.n / max) * h)) : 2;
    return html`<rect class="${d.n ? "" : "zero"}" x="${(i * bw + 1).toFixed(1)}" y="${h - bh}" width="${Math.max(1, bw - 2).toFixed(1)}" height="${bh}" rx="1"><title>${d.date}: ${d.n}</title></rect>`;
  })}</svg>`;
}
function dayChart(perDay) {
  const list = perDay || [];
  const max = Math.max(1, ...list.map(d => d.n));
  return html`<div class="chart" role="img" aria-label="Reviews per day, last 14 days">${list.map((d, i) => {
    const day = new Date(d.date + "T00:00:00");
    const label = isNaN(day) ? "" : (i === list.length - 1 ? "today" : day.toLocaleDateString("en-GB", { weekday: "narrow" }));
    return html`<div class="col" title="${d.date}: ${d.n}"><span class="colnum">${d.n || ""}</span><span class="colwrap"><span class="colbar ${d.n ? "" : "zero"}" data-h="${d.n ? Math.max(4, (100 * d.n) / max) : 2}"></span></span><span class="collabel">${label}</span></div>`;
  })}</div>`;
}
function tile(label, value, foot = "") {
  return html`<div class="tile"><div class="tile-label">${label}</div><div class="tile-value">${value}</div>${foot ? html`<div class="tile-foot">${foot}</div>` : ""}</div>`;
}
function statusBar(counts, total, lg = false) {
  const order = ["validated", "in_review", "disputed", "outdated", "unreviewed"];
  const t = total || order.reduce((a, k) => a + (counts[k] || 0), 0) || 1;
  return html`<div class="bar ${lg ? "lg" : ""}" role="img" aria-label="${order.filter(k => counts[k]).map(k => `${counts[k]} ${STATUS[k].toLowerCase()}`).join(", ") || "no items"}">${order.filter(k => k !== "unreviewed" && counts[k]).map(k => html`<span class="st-${k}" data-w="${(100 * counts[k]) / t}" title="${STATUS[k]}: ${counts[k]}"></span>`)}</div>`;
}

// ------------------------------------------------------------------ my work

async function viewMe(view, stale) {
  if (!S.reviewer) return renderWelcome(view);
  loading(view);
  const [stats] = await Promise.all([api(withWho("/api/me/stats")), loadItems()]);
  if (stale()) return;
  view.dataset.screen = "me";
  const done = (S.items || []).filter(it => it.answered || it.my_verdict).sort((a, b) => String(b.my_time || "").localeCompare(String(a.my_time || "")));
  const q = stats.queue || {};
  const vc = { correct: 0, wrong: 0, unsure: 0 };
  done.forEach(it => { if (it.my_verdict) vc[it.my_verdict]++; });
  const week = (stats.per_day || []).slice(-7).reduce((a, d) => a + d.n, 0);
  mount(view, html`<div class="page">
    <div class="page-head"><div><h1>My work</h1><div class="sub">What you've reviewed, as <b dir="auto">${displayName()}</b> <span class="fa">· کارهای من</span></div></div>
      <div class="actions"><a class="btn primary" href="#/review">Continue reviewing</a></div></div>
    <div class="tiles">
      ${tile("Verdicts", n(stats.verdicts), plural(stats.blind_answers, "blind answer"))}
      ${tile("Blind accuracy", pct(stats.blind_accuracy), "Your blind answers that matched the key")}
      ${tile("Agreement", pct(stats.agreement), "Verdicts matching the other reviewers")}
      ${tile("This week", n(week), "Answers and verdicts, last 7 days")}
    </div>
    <div class="audit-grid section-gap">
      <div class="card card-pad"><div class="section-title flush">Last 14 days</div>${dayChart(stats.per_day)}</div>
      <div class="card"><div class="card-title">Left to review</div><dl class="facts">
        <div><dt>Native items</dt><dd>${n(q.native)}</dd></div>
        <div><dt>Social norms (gated)</dt><dd>${n(q.gated)}</dd></div>
        <div><dt>Imported &amp; derived</dt><dd>${n(q.reference)}</dd></div>
        <div><dt>Your verdicts</dt><dd><span class="tick">${vc.correct}</span> · <span class="cross">${vc.wrong}</span> · <span class="faint">${vc.unsure}</span></dd></div>
      </dl></div>
    </div>
    <h2 class="section-title">History <span class="muted">${plural(done.length, "item")} · open one to change your verdict</span></h2>
    <div class="card">${done.length ? html`<ul class="feed">${done.slice(0, 300).map(it => html`<li class="clickable" data-href="#/review/${enc(it.id)}" tabindex="0">
        <div class="when"><b>${ago(it.my_time)}</b>${stamp(it.my_time)}</div>
        <div class="what"><div class="line1"><a href="#/review/${enc(it.id)}"><code>${it.id}</code></a><span class="faint">${it.task}</span>
          ${it.my_verdict ? html`<span class="chip v-${it.my_verdict}">${VERDICT_ICON[it.my_verdict]} ${VERDICT[it.my_verdict]}</span>` : html`<span class="chip t-info">answered, no verdict yet</span>`}
          ${(it.my_flags || []).map(f => html`<span class="chip flag">${META.flags[f] || f}</span>`)}
          <span class="spacer"></span><span class="chip st-${it.status}">${STATUS[it.status] || it.status}</span></div>
          <div class="prompt clamp2" dir="auto">${it.prompt}</div></div></li>`)}</ul>
        ${done.length > 300 ? html`<div class="pager">Showing the latest 300.</div>` : ""}`
      : html`<div class="empty"><b>Nothing yet</b><p>Items you review show up here.</p><a class="btn primary" href="#/review">Start reviewing</a></div>`}</div>
  </div>`);
}

// ------------------------------------------------------------------ progress

async function viewProgress(view, stale) {
  loading(view);
  const p = await api("/api/progress");
  if (stale()) return;
  view.dataset.screen = "progress";
  const native = p.rows.filter(r => !r.group.startsWith("source:"));
  const ref = p.rows.filter(r => r.group.startsWith("source:") && (r.blind_answers || r.review_required || r.validated || r.in_review || r.disputed));
  const tot = native.reduce((a, r) => { ["items", "validated", "disputed", "in_review", "unreviewed", "outdated"].forEach(k => { a[k] += r[k] || 0; }); return a; },
    { items: 0, validated: 0, disputed: 0, in_review: 0, unreviewed: 0, outdated: 0 });
  const reviewers = Object.entries(p.reviewers || {});
  const table = rows => html`<div class="card table-wrap"><table class="table responsive"><thead><tr><th>Group</th><th class="num">Items</th><th>Progress</th>
    <th class="num">Validated</th><th class="num">In review</th><th class="num">Disputed</th><th class="num">Outdated</th><th class="num">Human acc.</th><th class="num">Blind</th></tr></thead>
    <tbody>${rows.map(r => html`<tr><td><span class="cell-main">${r.group.replace("source:", "")}</span>${r.review_required ? html` <span class="chip t-warn">gated</span>` : ""}
        ${META.tasks[r.group] ? html`<div class="cell-sub">${META.tasks[r.group]}</div>` : ""}</td>
      <td class="num" data-label="Items">${n(r.items)}</td><td class="hide-sm">${statusBar(r, r.items)}</td>
      <td class="num" data-label="Validated">${n(r.validated)}</td><td class="num" data-label="In review">${n(r.in_review)}</td>
      <td class="num" data-label="Disputed">${r.disputed ? html`<span class="cross">${n(r.disputed)}</span>` : "0"}</td><td class="num" data-label="Outdated">${n(r.outdated)}</td>
      <td class="num" data-label="Human accuracy">${pct(r.human_accuracy)}</td><td class="num" data-label="Blind answers">${n(r.blind_answers)}</td></tr>`)}</tbody></table></div>`;
  mount(view, html`<div class="page wide">
    <div class="page-head"><div><h1>Progress</h1><div class="sub">How far validation has come <span class="fa">· پیشرفت</span></div></div>
      ${isAdmin() ? html`<div class="actions"><a class="btn" href="/api/export">${ICON.download} Download all reviews</a></div>` : ""}</div>
    <div class="tiles">
      ${tile("Native items validated", html`${n(tot.validated)} <span class="faint">/ ${n(tot.items)}</span>`, pct(tot.items ? tot.validated / tot.items : null) + " of the FarhangScore set")}
      ${tile("In review", n(tot.in_review), "Some verdicts, not settled yet")}
      ${tile("Disputed", n(tot.disputed), "Need an author's attention")}
      ${tile("Reviewers", n(reviewers.length), `${n(reviewers.reduce((a, [, v]) => a + v, 0))} verdicts in total`)}
    </div>
    <div class="card card-pad section-gap"><div class="vsplit">${statusBar(tot, tot.items, true)}
      <div class="legend">${["validated", "in_review", "disputed", "outdated", "unreviewed"].map(k => html`<span><i class="dot st-${k}"></i>${STATUS[k]} ${n(tot[k])}</span>`)}</div></div></div>
    <h2 class="section-title">Native items by task <span class="muted">human accuracy = share of blind answers that matched the key</span></h2>
    ${table(native)}
    ${ref.length ? html`<h2 class="section-title">Imported and derived sources <span class="muted">gated sources count only once validated; the rest are audits</span></h2>${table(ref)}` : ""}
    <h2 class="section-title">Reviewers</h2>
    <div class="card card-pad">${reviewers.length ? html`<div class="flag-chips flush">${reviewers.map(([k, v]) => isAdmin()
      ? html`<a class="chip lg outline" href="#/admin/reviewers/${enc(k)}">${k} · ${n(v)}</a>` : html`<span class="chip lg outline">${k} · ${n(v)}</span>`)}</div>` : html`<span class="muted">No verdicts yet.</span>`}</div>
    <h2 class="section-title">Disputed or flagged <span class="muted">${plural(p.problems.length, "item")} for authors to fix in <code>farhang browse</code></span></h2>
    <div class="card">${p.problems.length ? html`<ul class="feed">${p.problems.slice(0, 100).map(x => html`<li ${isAdmin() ? html`class="clickable" data-href="#/admin/items/${enc(x.id)}" tabindex="0"` : ""}>
        <div class="when"><span class="chip st-${x.status}">${STATUS[x.status]}</span></div>
        <div class="what"><div class="line1"><code>${x.id}</code><span class="faint">${x.task}</span>${x.flags.map(f => html`<span class="chip flag">${META.flags[f] || f}</span>`)}</div>
          <div class="prompt clamp2" dir="auto">${x.prompt}</div>
          ${(x.comments || []).filter(c => c.comment || c.suggested_answer || c.verdict === "wrong").map(c => html`<div class="comment"><b>${c.reviewer}</b> · ${VERDICT[c.verdict] || c.verdict}${c.suggested_answer ? html` — suggests <span class="fa" dir="auto">${c.suggested_answer}</span>` : ""}${c.comment ? html`<div class="fa" dir="auto">${c.comment}</div>` : ""}</div>`)}</div></li>`)}</ul>
        ${p.problems.length > 100 ? html`<div class="pager">Showing 100 of ${n(p.problems.length)}.${isAdmin() ? html` <a href="#/admin/items?status=disputed">See all in Items</a>` : ""}</div>` : ""}`
      : html`<div class="empty"><b>No open problems</b><p>Nothing is disputed or flagged.</p></div>`}</div>
    <p class="faint section-gap">Editing an item in <code>farhang browse</code> resets its reviews, because reviewers judged the old version.</p>
  </div>`);
}

// ------------------------------------------------------------------ admin: shared

async function reviewersList(force) {
  if (!S.reviewers || force) S.reviewers = await api(withWho("/api/admin/reviewers"));
  return S.reviewers;
}
async function sourcesList() {
  if (!S.sources) {
    const p = await api("/api/progress");
    S.sources = [NATIVE, ...p.rows.filter(r => r.group.startsWith("source:")).map(r => r.group.slice(7))];
  }
  return S.sources;
}
const nameOf = name => { const r = (S.reviewers || []).find(x => x.name === name); return r && r.display_name ? r.display_name : ""; };
const reviewerLink = name => html`<a class="who-link" href="#/admin/reviewers/${enc(name)}">${name}</a>${nameOf(name) ? html` <span class="faint" dir="auto">${nameOf(name)}</span>` : ""}`;
function pager(total, offset, limit, base, query) {
  if (total <= limit && !offset) return "";
  const prev = Math.max(0, offset - limit), next = offset + limit;
  return html`<div class="pager"><span>${n(total ? offset + 1 : 0)}–${n(Math.min(total, offset + limit))} of ${n(total)}</span>
    ${offset > 0 ? html`<a class="btn sm" href="${base}${qs(Object.assign({}, query, { offset: prev }))}">${ICON.left} Newer</a>` : html`<button class="btn sm" disabled>${ICON.left} Newer</button>`}
    ${next < total ? html`<a class="btn sm" href="${base}${qs(Object.assign({}, query, { offset: next }))}">Older ${ICON.right}</a>` : html`<button class="btn sm" disabled>Older ${ICON.right}</button>`}</div>`;
}
function bindFilters(root, base, query) {
  $$("[data-filter]", root).forEach(el => {
    const ev = el.tagName === "INPUT" && el.type !== "checkbox" ? "change" : "change";
    el.addEventListener(ev, () => {
      const q = Object.assign({}, query, { offset: 0 });
      $$("[data-filter]", root).forEach(f => { q[f.dataset.filter] = f.type === "checkbox" ? (f.checked ? "1" : "") : f.value.trim(); });
      location.hash = base + qs(q);
    });
    if (el.tagName === "INPUT" && el.type === "search") el.addEventListener("keydown", e => { if (e.key === "Enter") el.dispatchEvent(new Event("change")); });
  });
}
const sel = (name, label, opts, value) => html`<select class="input sm" data-filter="${name}" aria-label="${label}"><option value="">${label}</option>${opts.map(o => {
  const [v, t] = Array.isArray(o) ? o : [o, o];
  return html`<option value="${v}" ${String(value || "") === String(v) ? "selected" : ""}>${t}</option>`;
})}</select>`;

function activityFeed(rows, { showReviewer = true } = {}) {
  if (!rows.length) return html`<div class="empty"><b>No activity</b><p>Nothing matches these filters.</p></div>`;
  return html`<ul class="feed">${rows.map(r => html`<li>
    <div class="when"><b>${ago(r.time)}</b>${stamp(r.time)}</div>
    <div class="what"><div class="line1">
      ${showReviewer ? reviewerLink(r.reviewer) : ""}
      ${r.type === "blind"
        ? html`<span class="chip ${r.correct === true ? "t-ok" : r.correct === false ? "t-bad" : "t-info"}">Blind answer ${r.correct === true ? "✓" : r.correct === false ? "✗" : ""}</span>`
        : r.hidden ? html`<span class="chip t-info">Verdict</span>`
        : html`<span class="chip v-${r.verdict}">Verdict: ${VERDICT[r.verdict] || r.verdict}</span>`}
      <a href="#/admin/items/${enc(r.item_id)}" data-act="audit-open" data-id="${r.item_id}"><code>${r.item_id}</code></a>
      <span class="faint">${r.task || ""}</span>${r.current ? "" : html`<span class="chip outline" title="The item changed since">old version</span>`}
      ${(r.flags || []).map(f => html`<span class="chip flag">${META.flags[f] || f}</span>`)}</div>
      ${r.hidden ? html`<div class="cell-sub faint">hidden — <a href="#/admin/items/${enc(r.item_id)}">open the item to see answers</a></div>` : ""}
      ${r.prompt ? html`<div class="prompt clamp1" dir="auto">${r.prompt}</div>` : ""}
      ${r.type === "blind" && r.answer !== null && r.answer !== undefined && r.answer !== "" ? html`<div class="cell-sub">Answered <span class="fa" dir="auto">${typeof r.answer === "number" ? `option ${r.answer + 1}` : r.answer}</span></div>` : ""}
      ${r.type === "blind" && (r.answer === null || r.answer === "") ? html`<div class="cell-sub">Answered “I don't know”</div>` : ""}
      ${r.comment || r.suggested_answer ? html`<div class="comment">${r.suggested_answer ? html`<div>Suggests <span class="fa" dir="auto">${r.suggested_answer}</span></div>` : ""}${r.comment ? html`<div class="fa" dir="auto">${r.comment}</div>` : ""}</div>` : ""}
    </div></li>`)}</ul>`;
}

// Opening an item's audit view shows its key; warn first when the admin hasn't answered it blind yet.
const peekOk = new Set();
ACT["audit-open"] = async (el, e) => {
  e.preventDefault();
  const id = el.dataset.id;
  if (el.dataset.answered === "1" || el.dataset.peeked === "1" || peekOk.has(id)) peekOk.add(id);
  location.hash = `#/admin/items/${enc(id)}`;
};
async function needsPeekConfirm(id) {
  if (peekOk.has(id)) return false;
  try {
    const res = await api(withWho(`/api/admin/items?q=${enc(id)}&limit=500`));
    const row = res.rows.find(r => r.id === id);
    if (!row) return false;
    return !(row.my_answered || row.peeked);
  } catch (e) { return true; }
}

// ------------------------------------------------------------------ admin: activity

async function viewActivity(view, query, stale) {
  loading(view);
  const offset = +query.offset || 0;
  const [res, reviewers] = await Promise.all([
    api(withWho(`/api/admin/activity${qs(Object.assign({}, query, { limit: PAGE, offset }))}`)), reviewersList()]);
  if (stale()) return;
  view.dataset.screen = "activity";
  mount(view, html`<div class="page">
    <div class="page-head"><div><h1>Activity</h1><div class="sub">Every blind answer and verdict, newest first</div></div></div>
    <div class="filters" id="filters">
      ${sel("reviewer", "All reviewers", reviewers.map(r => [r.name, r.display_name ? `${r.name} · ${r.display_name}` : r.name]), query.reviewer)}
      ${sel("type", "Answers and verdicts", [["blind", "Blind answers"], ["verdict", "Verdicts"]], query.type)}
      ${sel("verdict", "Any verdict", Object.entries(VERDICT), query.verdict)}
      <input class="input sm" type="search" data-filter="item" placeholder="Item id" value="${query.item || ""}" aria-label="Item id" spellcheck="false">
      <label class="check"><input type="checkbox" data-filter="flagged" ${query.flagged ? "checked" : ""}> Flagged only</label>
      ${Object.keys(query).some(k => k !== "offset" && query[k]) ? html`<a class="btn ghost sm" href="#/admin/activity">Clear</a>` : ""}
    </div>
    <div class="card">${activityFeed(res.rows)}${pager(res.total, offset, PAGE, "#/admin/activity", query)}</div>
  </div>`);
  bindFilters($("#filters"), "#/admin/activity", query);
}

// ------------------------------------------------------------------ admin: reviewers

async function viewReviewers(view, stale) {
  loading(view);
  const rows = await reviewersList(true);
  if (stale()) return;
  view.dataset.screen = "reviewers";
  rows.sort((a, b) => String(b.last_activity || "").localeCompare(String(a.last_activity || "")) || a.name.localeCompare(b.name));
  mount(view, html`<div class="page wide">
    <div class="page-head"><div><h1>Reviewers</h1><div class="sub">Everyone with an account or a review. Agreement = verdicts that match the other reviewers' majority.</div></div></div>
    <div class="card table-wrap"><table class="table responsive"><thead><tr><th>Reviewer</th><th class="num">Blind</th><th class="num">Verdicts</th>
      <th>Correct · wrong · unsure</th><th class="num">Blind acc.</th><th class="num">Agreement</th><th class="num">Flags</th><th>Last active</th><th>14 days</th></tr></thead>
      <tbody>${rows.map(r => html`<tr class="clickable" data-href="#/admin/reviewers/${enc(r.name)}" tabindex="0">
        <td><a class="cell-main" href="#/admin/reviewers/${enc(r.name)}">${r.name}</a>${r.role === "admin" ? html` <span class="chip t-accent">admin</span>` : ""}${!r.has_account ? html` <span class="chip outline" title="Has reviews but no account here">no account</span>` : ""}
          ${r.display_name ? html`<div class="cell-sub" dir="auto">${r.display_name}</div>` : ""}</td>
        <td class="num" data-label="Blind answers">${n(r.blind_answers)}</td><td class="num" data-label="Verdicts">${n(r.verdicts)}</td>
        <td data-label="Verdicts"><span class="tick">${r.verdict_counts.correct}</span> · <span class="cross">${r.verdict_counts.wrong}</span> · <span class="faint">${r.verdict_counts.unsure}</span></td>
        <td class="num" data-label="Blind accuracy">${pct(r.blind_accuracy)}</td><td class="num" data-label="Agreement">${pct(r.agreement)}</td>
        <td class="num" data-label="Flags">${n(r.flags_raised)}</td><td data-label="Last active">${timeEl(r.last_activity)}</td><td class="hide-sm">${sparkline(r.per_day)}</td></tr>`)}</tbody></table>
      ${rows.length ? "" : html`<div class="empty"><b>No reviewers yet</b></div>`}</div>
  </div>`);
}

async function viewReviewer(view, name, stale) {
  loading(view);
  const [r] = await Promise.all([api(withWho(`/api/admin/reviewers/${enc(name)}`)), reviewersList()]);
  if (stale()) return;
  view.dataset.screen = "reviewer";
  const vc = r.verdict_counts || {};
  const vt = (vc.correct || 0) + (vc.wrong || 0) + (vc.unsure || 0);
  mount(view, html`<div class="page">
    <nav class="crumbs" aria-label="Breadcrumb"><a href="#/admin/reviewers">Reviewers</a><span aria-hidden="true">/</span><span>${r.name}</span></nav>
    <div class="page-head"><div class="profile-head"><span class="avatar lg" aria-hidden="true">${(r.display_name || r.name).charAt(0)}</span>
      <div><h1>${r.display_name ? html`<span dir="auto">${r.display_name}</span>` : r.name}</h1>
      <div class="sub">${r.display_name ? html`<code>${r.name}</code> · ` : ""}${r.role === "admin" ? "Admin" : r.role ? "Reviewer" : "No account"} · last active ${timeEl(r.last_activity)}</div></div></div>
      <div class="actions"><a class="btn" href="#/admin/activity?reviewer=${enc(r.name)}">All activity</a></div></div>
    <div class="tiles">
      ${tile("Blind answers", n(r.blind_answers))}
      ${tile("Verdicts", n(r.verdicts), `${n(r.flags_raised)} flags raised`)}
      ${tile("Blind accuracy", pct(r.blind_accuracy), "Answers that matched the key")}
      ${tile("Agreement", pct(r.agreement), "With the other reviewers' majority")}
    </div>
    <div class="audit-grid section-gap">
      <div class="card card-pad"><div class="section-title flush">Last 14 days</div>${dayChart(r.per_day)}</div>
      <div class="card card-pad"><div class="section-title flush">Verdicts on current items</div>
        <div class="vsplit"><div class="bar lg">${["correct", "wrong", "unsure"].filter(k => vc[k]).map(k => html`<span class="v-${k}" data-w="${(100 * vc[k]) / (vt || 1)}"></span>`)}</div>
        <div class="legend">${["correct", "wrong", "unsure"].map(k => html`<span><i class="dot v-${k}"></i>${VERDICT[k]} ${n(vc[k])}</span>`)}</div></div></div>
    </div>
    <h2 class="section-title">Recent activity <span class="muted">latest ${n(r.recent.length)}</span></h2>
    <div class="card">${activityFeed(r.recent, { showReviewer: false })}</div>
  </div>`);
}

// ------------------------------------------------------------------ admin: items

async function viewItems(view, query, stale) {
  loading(view);
  const offset = +query.offset || 0;
  const [res, reviewers, sources] = await Promise.all([
    api(withWho(`/api/admin/items${qs(Object.assign({}, query, { limit: PAGE, offset }))}`)), reviewersList(), sourcesList()]);
  if (stale()) return;
  view.dataset.screen = "items";
  mount(view, html`<div class="page wide">
    <div class="page-head"><div><h1>Items</h1><div class="sub">Status of every item; disputed first. Opening one shows its key.</div></div></div>
    <div class="filters" id="filters">
      <input class="input sm grow" type="search" data-filter="q" placeholder="Search id or question text" value="${query.q || ""}" aria-label="Search" dir="auto" spellcheck="false">
      ${sel("status", "Any status", Object.entries(STATUS), query.status)}
      ${sel("source", "Any source", sources, query.source)}
      ${sel("task", "Any task", Object.keys(META.tasks), query.task)}
      ${sel("flag", "Any flag", Object.entries(META.flags), query.flag)}
      ${sel("reviewer", "Any reviewer", reviewers.map(r => r.name), query.reviewer)}
      <label class="check"><input type="checkbox" data-filter="has_comments" ${query.has_comments ? "checked" : ""}> With comments</label>
      ${Object.keys(query).some(k => k !== "offset" && query[k]) ? html`<a class="btn ghost sm" href="#/admin/items">Clear</a>` : ""}
    </div>
    <div class="card table-wrap"><table class="table responsive"><thead><tr><th>Item</th><th>Question</th><th>Status</th><th class="num">Verdicts</th><th>Flags</th><th class="num">Reviewers</th><th>Last activity</th></tr></thead>
      <tbody>${res.rows.map(r => html`<tr class="clickable" tabindex="0" data-act="audit-open" data-id="${r.id}" data-answered="${r.my_answered ? "1" : "0"}" data-peeked="${r.peeked ? "1" : "0"}" data-prevent="1">
        <td><code class="cell-main">${r.id}</code><div class="cell-sub">${r.task}${r.source !== NATIVE ? ` · ${r.source}` : ""}</div></td>
        <td class="prompt-cell"><div class="fa clamp2" dir="auto">${r.prompt}</div></td>
        <td data-label="Status"><span class="chip st-${r.status}">${STATUS[r.status] || r.status}</span>${r.peeked ? html` <span class="chip t-violet" title="You've seen this key">seen</span>` : ""}${r.my_answered ? html` <span class="chip t-info" title="You answered blind">answered</span>` : ""}</td>
        <td class="num" data-label="Verdicts">${r.verdicts ? html`<span class="tick">${r.agree}</span><span class="faint"> / ${r.verdicts}</span>` : html`<span class="faint">0</span>`}</td>
        <td data-label="Flags">${r.flags.map(f => html`<span class="chip flag">${META.flags[f] || f}</span> `)}${r.comments_count ? html`<span class="chip outline">${plural(r.comments_count, "comment")}</span>` : ""}</td>
        <td class="num" data-label="Reviewers">${r.reviewers.length}</td>
        <td data-label="Last activity">${timeEl(r.last_activity)}</td></tr>`)}</tbody></table>
      ${res.rows.length ? "" : html`<div class="empty"><b>No items</b><p>Nothing matches these filters.</p></div>`}
      ${pager(res.total, offset, PAGE, "#/admin/items", query)}</div>
  </div>`);
  bindFilters($("#filters"), "#/admin/items", query);
}
document.addEventListener("keydown", e => {  // keyboard open for item rows
  const row = e.target.closest && e.target.closest("tr[data-act=audit-open]");
  if (row && e.target === row && e.key === "Enter") { e.preventDefault(); ACT["audit-open"](row, e); }
});

async function viewItemAudit(view, id, stale) {
  if (await needsPeekConfirm(id)) {
    if (stale()) return;
    const ok = await confirmDialog({
      title: "Show the answer key?", icon: "!",
      body: html`This shows the key of <code>${id}</code>. You won't be able to review this item blind afterwards; seeing the key is recorded in the audit log.`,
      confirm: "Show the key",
    });
    if (stale()) return;
    if (!ok) { if (history.length > 1 && view.dataset.screen) history.back(); else replaceHash("#/admin/items"); return; }
    peekOk.add(id);
  }
  loading(view);
  const d = await api(withWho(`/api/admin/items/${enc(id)}`));
  if (stale()) return;
  peekOk.add(id);
  const listed = d.peeked && (S.items || []).find(x => x.id === id);
  if (listed) listed.peeked = true;  // keep the review queue in step without reloading it
  await reviewersList().catch(() => null);
  view.dataset.screen = "item-audit";
  const it = d.item, st = d.status;
  const reviewCard = rv => html`<div class="rcard">
      <div class="rname"><span class="avatar" aria-hidden="true">${rv.reviewer.charAt(0)}</span><div>${reviewerLink(rv.reviewer)}
        <div class="cell-sub">${rv.is_author ? html`<span class="chip t-warn">author · doesn't count</span>` : ""}</div></div></div>
      <div class="stack">
        <div><div class="k">Blind answer</div><div class="v">${"blind_answer" in rv
          ? html`${rv.blind_correct === true ? html`<span class="tick">✓</span> ` : rv.blind_correct === false ? html`<span class="cross">✗</span> ` : ""}${fmtAnswer(it, rv.blind_answer) === null ? html`<span class="faint">I don't know</span>` : html`<span class="fa" dir="auto">${fmtAnswer(it, rv.blind_answer)}</span>`}
             <div class="cell-sub">${stamp(rv.blind_time)}</div>` : html`<span class="faint">—</span>`}</div></div>
        <div><div class="k">Verdict</div><div class="v">${rv.verdict ? html`<span class="chip v-${rv.verdict}">${VERDICT_ICON[rv.verdict]} ${VERDICT[rv.verdict]}</span> <span class="cell-sub">${stamp(rv.verdict_time)}</span>` : html`<span class="faint">No verdict yet</span>`}</div></div>
      </div>
      <div class="stack">
        ${(rv.flags || []).length ? html`<div><div class="k">Flags</div><div class="v">${rv.flags.map(f => html`<span class="chip flag">${META.flags[f] || f}</span> `)}</div></div>` : ""}
        ${rv.suggested_answer ? html`<div><div class="k">Suggested answer</div><div class="v fa" dir="auto">${rv.suggested_answer}</div></div>` : ""}
        ${rv.comment ? html`<div><div class="k">Comment</div><div class="v fa" dir="auto">${rv.comment}</div></div>` : ""}
        ${!(rv.flags || []).length && !rv.suggested_answer && !rv.comment ? html`<div class="faint">No flags or comments.</div>` : ""}
      </div></div>`;
  mount(view, html`<div class="page wide">
    <nav class="crumbs" aria-label="Breadcrumb"><a href="#/admin/items">Items</a><span aria-hidden="true">/</span><span>${it.id}</span></nav>
    <div class="page-head"><div><h1 class="mono">${it.id}</h1><div class="sub">${META.tracks[it.track] || it.track} · <b>${it.task}</b>${META.tasks[it.task] ? ` — ${META.tasks[it.task]}` : ""}${it.source !== NATIVE ? ` · source ${it.source}` : ""}</div></div>
      <div class="actions"><button class="btn" data-act="copylink">${ICON.link} Copy link</button><a class="btn" href="#/admin/activity?item=${enc(it.id)}">Activity</a></div></div>
    ${d.peeked ? html`<div class="notice t-violet block"><span class="notice-icon">!</span><div>You've seen this key, so your own blind answer to this version won't be accepted.</div></div>` : ""}
    <div class="audit-grid">
      <div class="card card-pad">${itemBody(it)}${keyBlock(it, it, null, false)}</div>
      <aside class="card"><div class="card-title">Status <span class="spacer"></span><span class="chip st-${st.status}">${STATUS[st.status] || st.status}</span></div>
        <dl class="facts">
          <div><dt>Verdicts</dt><dd>${n(st.verdicts)}</dd></div>
          <div><dt>Say the key is correct</dt><dd>${n(st.agree)}</dd></div>
          <div><dt>Human accuracy</dt><dd>${pct(st.human_accuracy)}</dd></div>
          <div><dt>Reviewers</dt><dd>${st.reviewers.length}</dd></div>
          <div><dt>Format</dt><dd>${it.format}</dd></div>
          ${it.file ? html`<div><dt>File</dt><dd class="mono clamp1" title="${it.file}">${String(it.file).split("/").slice(-2).join("/")}</dd></div>` : ""}
        </dl>
        ${st.flags.length ? html`<div class="card-pad flag-chips flush">${st.flags.map(f => html`<span class="chip flag">${META.flags[f] || f}</span>`)}</div>` : ""}
      </aside>
    </div>
    <h2 class="section-title">Reviews <span class="muted">${plural(d.reviews.length, "reviewer")} on the current version</span></h2>
    <div class="card review-cards">${d.reviews.length ? d.reviews.map(reviewCard) : html`<div class="empty"><b>No reviews yet</b><p>Nobody has answered this version of the item.</p></div>`}</div>
    ${d.history.length ? html`<details class="card section-gap"><summary>Older versions <span class="muted">${plural(d.history.length, "record")} on earlier edits of this item</span></summary>
      <div class="table-wrap"><table class="table responsive"><thead><tr><th>Time</th><th>Reviewer</th><th>Record</th><th>Details</th></tr></thead><tbody>${d.history.map(h => html`<tr>
        <td data-label="Time">${stamp(h.time)}</td><td>${reviewerLink(h.reviewer)}</td>
        <td>${h.type === "blind" ? html`Blind answer ${h.correct === true ? html`<span class="tick">✓</span>` : h.correct === false ? html`<span class="cross">✗</span>` : ""}` : html`<span class="chip v-${h.verdict}">${VERDICT[h.verdict] || h.verdict}</span>`}</td>
        <td><span class="fa" dir="auto">${h.type === "blind" ? (h.answer === null ? "I don't know" : typeof h.answer === "number" ? `option ${h.answer + 1}` : h.answer) : h.comment || ""}</span>
          ${(h.flags || []).map(f => html` <span class="chip flag">${META.flags[f] || f}</span>`)}<div class="cell-sub mono">${String(h.item_hash || "").slice(0, 10)}</div></td></tr>`)}</tbody></table></div></details>` : ""}
  </div>`);
}

// ------------------------------------------------------------------ admin: people

async function viewPeople(view, stale) {
  if (S.me.local) return viewMessage(view, "No accounts here", "This server runs without sign-in, so there are no accounts to manage.");
  loading(view);
  const [users, reviewers] = await Promise.all([api("/api/admin/users"), reviewersList(true)]);
  if (stale()) return;
  view.dataset.screen = "people";
  const work = Object.fromEntries(reviewers.map(r => [r.name, r]));
  const status = u => !u.managed ? html`<span class="chip outline" title="Defined in the deployment's users Secret">from Secret</span>`
    : u.disabled ? html`<span class="chip t-bad">disabled</span>`
    : u.must_change ? html`<span class="chip t-warn">awaiting first sign-in</span>` : html`<span class="chip t-ok">active</span>`;
  const actions = u => {
    if (!u.managed || u.name === S.reviewer) return "";
    return html`<div class="row-actions"><button class="icon-btn" data-act="menu" aria-haspopup="menu" aria-expanded="false" aria-label="Actions for ${u.name}" title="Actions">${ICON.more}</button>
      <div class="menu align-right" role="menu" hidden>
        <button role="menuitem" data-act="person" data-op="rename" data-name="${u.name}" data-display="${u.display_name || ""}">Edit display name</button>
        <button role="menuitem" data-act="person" data-op="reset" data-name="${u.name}">Reset password</button>
        <button role="menuitem" data-act="person" data-op="role" data-role="${u.role === "admin" ? "reviewer" : "admin"}" data-name="${u.name}">Make ${u.role === "admin" ? "reviewer" : "admin"}</button>
        <button role="menuitem" data-act="person" data-op="${u.disabled ? "enable" : "disable"}" data-name="${u.name}">${u.disabled ? "Enable account" : "Disable account"}</button>
        <hr><button role="menuitem" class="danger-item" data-act="person" data-op="remove" data-name="${u.name}">Remove…</button>
      </div></div>`;
  };
  mount(view, html`<div class="page wide">
    <div class="page-head"><div><h1>People</h1><div class="sub">Accounts that can sign in. Removing someone keeps their reviews.</div></div>
      <div class="actions"><button class="btn primary" data-act="add-person" id="add-person">${ICON.plus} Add person</button></div></div>
    <div class="card table-wrap"><table class="table responsive"><thead><tr><th>Name</th><th>Role</th><th>Status</th><th class="num">Verdicts</th><th>Last sign-in</th><th class="num">Sessions</th><th>Added</th><th></th></tr></thead>
      <tbody>${users.map(u => html`<tr class="${u.disabled ? "is-disabled" : ""}" data-user="${u.name}">
        <td><span class="cell-main">${u.display_name ? html`<span dir="auto">${u.display_name}</span>` : u.name}</span>${u.name === S.reviewer ? html`<span class="you">you</span>` : ""}
          ${u.display_name ? html`<div class="cell-sub"><code>${u.name}</code></div>` : ""}</td>
        <td data-label="Role">${u.role === "admin" ? html`<span class="chip t-accent">Admin</span>` : html`<span class="chip">Reviewer</span>`}</td>
        <td data-label="Status">${status(u)}</td>
        <td class="num" data-label="Verdicts">${work[u.name] ? html`<a href="#/admin/reviewers/${enc(u.name)}">${n(work[u.name].verdicts)}</a>` : "0"}</td>
        <td data-label="Last sign-in">${timeEl(u.last_login)}</td>
        <td class="num" data-label="Sessions">${n(u.active_sessions)}</td>
        <td data-label="Added" class="cell-sub">${u.created ? html`${stamp(u.created)}${u.created_by ? ` by ${u.created_by}` : ""}` : "—"}</td>
        <td>${actions(u)}</td></tr>`)}</tbody></table></div>
    <p class="faint section-gap">Send one-time passwords privately, not in a group chat. People choose their own password the first time they sign in.</p>
  </div>`);
}

function signInMessage(name, display, password) {
  const url = location.origin + location.pathname;
  return `Hi ${display || name},\n\nYou have an account for FarhangBench review: ${url}\n\nName: ${name}\nOne-time password: ${password}\n\n`
    + `When you sign in you'll choose your own password. Each item: answer it first without the key, then judge the key.`;
}
function showPassword(name, display, password, title) {
  const msg = signInMessage(name, display, password);
  const d = openDialog(html`<form method="dialog"><div class="dialog-body">
      <div class="dialog-icon t-ok" aria-hidden="true">✓</div>
      <h2>${title}</h2>
      <p class="lead">Copy this one-time password now; it won't be shown again. <b dir="auto">${display || name}</b> chooses their own password when they first sign in.</p>
      <div class="field"><span class="field-label">One-time password for <code>${name}</code></span>
        <div class="secret-box"><code id="otp">${password}</code><button type="button" class="btn sm" data-act="copy" data-copy="otp" autofocus>${ICON.copy} Copy</button></div></div>
      <label class="field"><span>Message to send them <span class="help">· privately</span></span>
        <textarea class="input message-box" id="signin-msg" rows="9" readonly>${msg}</textarea></label>
    </div><div class="dialog-foot"><button type="button" class="btn" data-act="copy" data-copy="signin-msg">${ICON.copy} Copy message</button><button class="btn primary" value="done">Done</button></div></form>`, { wide: true });
  return d;
}
ACT.copy = async el => {
  const src = document.getElementById(el.dataset.copy);
  const ok = await copyText(src.value !== undefined && src.tagName === "TEXTAREA" ? src.value : src.textContent);
  toast(ok ? "Copied" : "Couldn't copy; select the text instead", ok ? "ok" : "error");
};

ACT["add-person"] = () => {
  const d = openDialog(html`<form id="add-form" novalidate><div class="dialog-body">
      <h2>Add a person</h2>
      <p class="lead">They sign in with the name below. Reviews are saved under it.</p>
      <div class="grid2 even">
        <label class="field"><span>Sign-in name</span><input class="input" name="name" required autocomplete="off" spellcheck="false" autocapitalize="none" placeholder="e.g. maryam" autofocus></label>
        <label class="field"><span>Display name <span class="help">· optional</span></span><input class="input" name="display_name" dir="auto" autocomplete="off" placeholder="e.g. مریم"></label>
      </div>
      <div class="field"><span class="field-label">Role</span><div class="radio-cards">
        <label class="radio-card"><input type="radio" name="role" value="reviewer" checked><div><b>Reviewer</b><span>Reviews items</span></div></label>
        <label class="radio-card"><input type="radio" name="role" value="admin"><div><b>Admin</b><span>Also manages people and sees everyone's work</span></div></label>
      </div></div>
      <div class="field"><span class="field-label">Password</span><div class="radio-cards">
        <label class="radio-card"><input type="radio" name="pwmode" value="generate" checked><div><b>One-time password</b><span>Generated now; they choose their own at first sign-in</span></div></label>
        <label class="radio-card"><input type="radio" name="pwmode" value="choose"><div><b>Choose one</b><span>You set it and tell them</span></div></label>
      </div></div>
      <div class="vsplit" id="choose-box" hidden>
        <label class="field"><span>Password <span class="help">· at least 10 characters</span></span><span class="pw-wrap"><input class="input" name="password" type="password" autocomplete="new-password">
          <button type="button" class="pw-toggle" data-act="pwtoggle" aria-label="Show password" aria-pressed="false">Show</button></span></label>
        <label class="check"><input type="checkbox" name="must_change" checked> Ask them to choose a new password at first sign-in</label>
      </div>
      <div class="form-error" id="add-error" role="alert" hidden></div>
    </div>
    <div class="dialog-foot"><button type="button" class="btn" data-act="close-dialog">Cancel</button><button class="btn primary" type="submit" id="add-submit">Add person</button></div></form>`, { wide: true });
  const form = $("#add-form", d), err = $("#add-error", d);
  $$("input[name=pwmode]", d).forEach(r => r.addEventListener("change", () => { $("#choose-box", d).hidden = form.pwmode.value !== "choose"; if (form.pwmode.value === "choose") form.password.focus(); }));
  form.addEventListener("submit", async e => {
    e.preventDefault();
    err.hidden = true;
    const body = { action: "add", name: form.name.value.trim(), role: form.role.value, display_name: form.display_name.value.trim() };
    if (!body.name) { err.textContent = "Enter a sign-in name."; err.hidden = false; return form.name.focus(); }
    if (form.pwmode.value === "choose") {
      if (form.password.value.length < 10) { err.textContent = "Use at least 10 characters for the password."; err.hidden = false; return form.password.focus(); }
      body.password = form.password.value; body.must_change = form.must_change.checked;
    }
    $("#add-submit", d).disabled = true;
    try {
      const res = await api("/api/admin/users", body);
      d.close("done");
      S.reviewers = null;
      await viewPeople($("#view"), () => false);
      if (res.password) showPassword(res.name, body.display_name, res.password, `${body.display_name || res.name} can now sign in`);
      else toast(`Added ${res.name}`);
    } catch (ex) { $("#add-submit", d).disabled = false; err.textContent = ex.message; err.hidden = false; }
  });
};
ACT["close-dialog"] = el => el.closest("dialog").close("cancel");

ACT.person = async el => {
  closeMenus();
  const { op, name } = el.dataset;
  const refresh = () => viewPeople($("#view"), () => false);
  const run = async (body, done) => {
    try { const res = await api("/api/admin/users", Object.assign({ name }, body)); await refresh(); done(res); }
    catch (e) { toast(e.message, "error"); }
  };
  if (op === "rename") {
    const d = openDialog(html`<form id="rename-form"><div class="dialog-body"><h2>Display name</h2><p class="lead">Shown instead of <code>${name}</code>. Can be in Persian. Leave empty to show the sign-in name.</p>
      <label class="field"><span>Display name</span><input class="input" name="display_name" dir="auto" value="${el.dataset.display || ""}" autofocus autocomplete="off"></label></div>
      <div class="dialog-foot"><button type="button" class="btn" data-act="close-dialog">Cancel</button><button class="btn primary" type="submit">Save</button></div></form>`);
    $("#rename-form", d).addEventListener("submit", e => { e.preventDefault(); const v = e.target.display_name.value.trim(); d.close("ok"); run({ action: "rename", display_name: v }, () => toast("Display name saved")); });
    return;
  }
  if (op === "reset") {
    if (!await confirmDialog({ title: `Reset ${name}'s password?`, body: "Their current password stops working at once and they're signed out everywhere. You'll get a new one-time password to send them.", confirm: "Reset password" })) return;
    return run({ action: "reset" }, res => showPassword(name, "", res.password, `New one-time password for ${name}`));
  }
  if (op === "role") {
    const role = el.dataset.role;
    if (!await confirmDialog({ title: `Make ${name} ${role === "admin" ? "an admin" : "a reviewer"}?`, body: role === "admin" ? "Admins manage people and can see every reviewer's work and every key." : "They'll lose access to the admin pages. Their sessions end so the change applies at once.", confirm: `Make ${role}`, tone: "t-info", icon: "i" })) return;
    return run({ action: "role", role }, () => toast(`${name} is now ${role === "admin" ? "an admin" : "a reviewer"}`));
  }
  if (op === "disable") {
    if (!await confirmDialog({ title: `Disable ${name}?`, body: "They're signed out at once and can't sign in until you enable the account again. Their reviews are kept.", confirm: "Disable", danger: true })) return;
    return run({ action: "disable" }, () => toast(`${name} is disabled`));
  }
  if (op === "enable") return run({ action: "enable" }, () => toast(`${name} can sign in again`));
  if (op === "remove") {
    if (!await confirmDialog({ title: `Remove ${name}?`, body: "The account is deleted and they can't sign in. Their reviews are kept. This can't be undone.", confirm: "Remove account", danger: true, typed: name })) return;
    return run({ action: "remove" }, () => toast(`${name} was removed`));
  }
};

// ------------------------------------------------------------------ admin: audit log

const AUDIT_LABEL = { add: "Added account", reset: "Reset password", role: "Changed role", disable: "Disabled", enable: "Enabled", remove: "Removed account",
  rename: "Changed display name", login: "Signed in", login_failed: "Failed sign-in", logout: "Signed out", password_change: "Changed password", peek: "Viewed a key" };
const AUDIT_TONE = { login_failed: "t-bad", remove: "t-bad", disable: "t-bad", peek: "t-violet", add: "t-ok", enable: "t-ok", reset: "t-warn", role: "t-info", login: "", logout: "", password_change: "t-info", rename: "" };
async function viewAudit(view, query, stale) {
  if (S.me.local) return viewMessage(view, "No audit log here", "This server runs without sign-in; the audit log records sign-ins and account changes on a shared deployment.");
  loading(view);
  const offset = +query.offset || 0;
  const res = await api(`/api/admin/audit${qs(Object.assign({}, query, { limit: PAGE, offset }))}`);
  if (stale()) return;
  view.dataset.screen = "audit";
  const target = r => {
    if (r.action === "peek") { const id = String(r.target).split("@")[0]; return html`<a href="#/admin/items/${enc(id)}"><code>${id}</code></a>`; }
    return r.target && r.target !== r.actor ? html`<code>${r.target}</code>` : "";
  };
  mount(view, html`<div class="page">
    <div class="page-head"><div><h1>Audit log</h1><div class="sub">Sign-ins, account changes and key views, newest first</div></div></div>
    <div class="filters" id="filters">
      <input class="input sm" type="search" data-filter="actor" placeholder="Who (sign-in name)" value="${query.actor || ""}" aria-label="Actor" spellcheck="false">
      ${sel("action", "Any event", AUDIT_ACTIONS.map(a => [a, AUDIT_LABEL[a]]), query.action)}
      ${query.actor || query.action ? html`<a class="btn ghost sm" href="#/admin/audit">Clear</a>` : ""}
    </div>
    <div class="card table-wrap"><table class="table responsive"><thead><tr><th>When</th><th>Who</th><th>Event</th><th>Target</th><th>Detail</th></tr></thead>
      <tbody>${res.rows.map(r => html`<tr><td data-label="When"><span title="${stamp(r.time)}">${stamp(r.time)}</span></td>
        <td><b>${r.actor || "—"}</b></td><td><span class="chip ${AUDIT_TONE[r.action] || ""}">${AUDIT_LABEL[r.action] || r.action}</span></td>
        <td data-label="Target">${target(r)}</td><td data-label="Detail" class="cell-sub" dir="auto">${r.detail || ""}</td></tr>`)}</tbody></table>
      ${res.rows.length ? "" : html`<div class="empty"><b>Nothing logged</b><p>No events match.</p></div>`}
      ${pager(res.total, offset, PAGE, "#/admin/audit", query)}</div>
  </div>`);
  bindFilters($("#filters"), "#/admin/audit", query);
}

// ------------------------------------------------------------------ start

(async function start() {
  try {
    S.me = await api("/api/whoami");
  } catch (e) {
    S.me = { auth_enabled: false, local: true, role: "admin", signed_in: false };
  }
  S.reviewer = S.me.local ? store.get("fb-reviewer", "") : (S.me.reviewer || "");
  if (!location.hash) history.replaceState(null, "", location.pathname + location.search + "#/review");
  route();
})();
