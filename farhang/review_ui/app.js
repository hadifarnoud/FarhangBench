const META = JSON.parse(document.getElementById("meta").textContent);
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const STATUS = { unreviewed: "Unreviewed", in_review: "In review", validated: "Validated", disputed: "Disputed", outdated: "Outdated", retired: "Retired" };
let ITEMS = [], current = null, choice = null, verdict = null;
const store = (k, v) => { try { v === undefined ? null : localStorage.setItem(k, v); return localStorage.getItem(k); } catch (e) { return null; } };
$("who").value = store("farhang-reviewer") || "";
let SIGNED_IN = false, ME = {};

async function api(path, body) {
  const res = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const data = await res.json().catch(() => ({ error: "bad response" }));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
const who = () => $("who").value.trim();

function fill(id, pairs, label) {
  const el = $(id), cur = el.value;
  el.innerHTML = `<option value="">${label}</option>` + pairs.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("");
  el.value = cur;
}
function initFilters() {
  fill("f-track", Object.entries(META.tracks).map(([k, v]) => [k, `${k} — ${v}`]), "All tracks");
  fill("f-task", Object.keys(META.tasks).filter(t => !$("f-track").value || t[0] === $("f-track").value).map(t => [t, t]), "All tasks");
}
function filtered() {
  const scope = $("f-scope").value, show = $("f-show").value;
  return ITEMS.filter(it => (scope === "all" || (scope === "gated" ? it.gated : (scope === "native") === (it.source === "farhangbench")))
    && it.status !== "retired" && it.status !== "outdated"
    && (!$("f-track").value || it.track === $("f-track").value) && (!$("f-task").value || it.task === $("f-task").value)
    && (show === "all" || (show === "todo" && !it.my_verdict) || (show === "mine" && it.my_verdict) || (show === "disputed" && it.status === "disputed")));
}
function renderList() {
  const rows = filtered();
  $("count").textContent = `${rows.length.toLocaleString()} items`;
  $("list").innerHTML = rows.slice(0, 400).map(it => `<div class="row${current && current.id === it.id ? " sel" : ""}" data-id="${esc(it.id)}">
    <div class="top"><code>${esc(it.id)}</code><span>${esc(it.task)}</span>
    <span class="chip ${it.status}">${STATUS[it.status]}${it.verdicts ? " · " + it.verdicts : ""}</span>
    ${it.my_verdict ? `<span class="chip mine">mine: ${esc(it.my_verdict)}</span>` : it.answered ? '<span class="chip mine">answered</span>' : ""}</div>
    <div class="txt">${esc(it.prompt)}</div></div>`).join("")
    + (rows.length > 400 ? `<div class="empty">Showing 400 of ${rows.length}; narrow the filters.</div>` : "")
    + (rows.length ? "" : `<div class="empty">Nothing left in this view. ${$("f-show").value === "todo" ? "Well done!" : ""}</div>`);
}
$("list").addEventListener("click", e => { const row = e.target.closest(".row"); if (row) open(row.dataset.id); });

function welcome() {
  $("main").innerHTML = `<div class="welcome"><h2>How reviewing works</h2><ol>
    <li><b>Answer first, without the key.</b> Pick or type the answer you believe is right, or press “I don't know”. Your first answer is saved and can't be changed. This is what makes the review independent.</li>
    <li><b>Then judge the key.</b> You'll see the official answer. Mark it correct, wrong or not sure, tick any problems, and leave a note or the right answer if you know it.</li>
    <li>An item counts towards the FarhangScore once two reviewers, other than its author, have answered and at least two thirds of them say the key is correct.</li>
    <li><b>Judge for Iran today.</b> Customs change. If something was the norm once but isn't any more, mark the key wrong and tick “No longer the norm in Iran today”. Items most reviewers mark that way are dropped.</li>
  </ol><p class="muted">Use reliable sources (dictionaries, Ganjoor, Dehkhoda, Farhangestan) when you check a key. Don't ask an AI model: the point is a human check.</p>
  <p class="muted">Keys: 1–5 pick an option · Enter submit · after the key: C correct, W wrong, U not sure · Ctrl+Enter save and go to the next item.</p>
  ${who() ? `<button class="primary" id="go">Start with the first item</button>` : `<p><b>Type your name at the top to begin.</b></p>`}</div>`;
  if ($("go")) $("go").onclick = next;
}
function next() {
  const rows = filtered().filter(it => !it.my_verdict && (!current || it.id !== current.id));
  rows.length ? open(rows[0].id) : ($("main").innerHTML = '<div class="empty">No more items to review in this view.</div>');
}
const imgs = it => (it.images || []).length ? `<div class="images">` + it.images.map(im =>
  `<figure><img src="/${esc(im.path)}" alt="Item image"><figcaption>${esc(im.credit || "")} · ${esc(im.license || "")}</figcaption></figure>`).join("") + "</div>" : "";

async function open(id) {
  current = ITEMS.find(it => it.id === id); choice = null; verdict = null;
  renderList();
  if (current.answered) {
    try { return renderKey(await api(`/api/key?reviewer=${encodeURIComponent(who())}&id=${encodeURIComponent(id)}`)); }
    catch (e) { /* fall through to blind */ }
  }
  renderBlind();
}
function head(it) {
  return `<h2><code>${esc(it.id)}</code></h2><div class="muted">${esc(META.tracks[it.track] || it.track)} · ${esc(it.task)} — ${esc(META.tasks[it.task] || "")}${it.source !== "farhangbench" ? ` · source: ${esc(it.source)}` : ""}</div>`
    + imgs(it) + (it.context ? `<h3>Context · متن</h3><div class="block" dir="auto">${esc(it.context)}</div>` : "")
    + `<h3>Question · پرسش</h3><div class="block" dir="auto">${esc(it.prompt)}</div>`;
}
function renderBlind() {
  const it = current;
  let h = `<div class="card">${head(it)}<h3>Your answer · پاسخ شما <span class="muted nw">(before you see the key)</span></h3>`;
  if (it.format === "mcq") h += `<div class="opts">` + it.options.map((o, i) => `<button class="opt" data-i="${i}" dir="auto"><b>${i + 1}</b><span>${esc(o)}</span></button>`).join("") + "</div>";
  else if (it.format === "judgment") h += `<div class="opts">` + it.labels.map((l, i) => `<button class="opt" data-i="${i}" dir="auto"><b>${i + 1}</b><span>${esc(l)}</span></button>`).join("") + "</div>";
  else if (it.format === "generation") h += `<textarea class="answer-input" id="ans" dir="auto" placeholder="Optional: write your own answer. Generation items are scored by a rubric, so only your verdict on the rubric counts."></textarea>`;
  else h += `<input class="answer-input" id="ans" dir="auto" placeholder="Type your answer">`;
  h += `<div class="actions"><button class="primary" id="submit">Submit answer · ثبت پاسخ</button><button id="idk">I don't know · نمی‌دانم</button>
    <span class="hint">Your first answer is saved and can't be changed.</span></div><div id="err"></div></div>`;
  $("main").innerHTML = h; $("main").scrollTop = 0;
  document.querySelectorAll(".opt").forEach(b => b.onclick = () => pick(+b.dataset.i));
  $("submit").onclick = () => submitBlind(false);
  $("idk").onclick = () => submitBlind(true);
  if ($("ans")) $("ans").focus();
}
function pick(i) { choice = i; document.querySelectorAll(".opt").forEach(b => b.classList.toggle("sel", +b.dataset.i === i)); }
async function submitBlind(dontKnow) {
  if (!who()) return showErr("Type your name at the top first.");
  const it = current;
  let answer = null;
  if (!dontKnow) {
    if (it.format === "mcq") answer = choice;
    else if (it.format === "judgment") answer = choice === null ? null : it.labels[choice];
    else answer = $("ans").value.trim();
    if (answer === null || answer === "") { if (it.format !== "generation") return showErr("Pick or type an answer, or press “I don't know”."); }
  }
  try {
    const key = await api("/api/blind", { reviewer: who(), item_id: it.id, answer });
    it.answered = true; renderKey(key);
  } catch (e) { showErr(e.message); }
}
function showErr(text) { const box = $("err"); if (box) box.innerHTML = `<div class="msg">${esc(text)}</div>`; }

function renderKey(key) {
  const it = current, rv = key.review || {}, a = key.answer || {}, md = key.metadata || {};
  verdict = rv.verdict || null;
  let h = `<div class="card">${head(it)}`;
  const mine = rv.blind_answer;
  if (it.format === "mcq" || it.format === "judgment") {
    const opts = it.format === "mcq" ? it.options : it.labels;
    const keyIdx = it.format === "mcq" ? a.index : opts.indexOf(a.label);
    const mineIdx = it.format === "mcq" ? mine : opts.indexOf(mine);
    h += `<h3>Options · گزینه‌ها</h3><div class="opts">` + opts.map((o, i) => `<div class="opt${i === keyIdx ? " key" : ""}${i === mineIdx && i !== keyIdx ? " wrongpick" : ""}" dir="auto"><b>${i + 1}</b><span>${esc(o)}</span>`
      + `<span class="tag">${i === keyIdx ? "key" : ""}${i === mineIdx ? (i === keyIdx ? " · your answer" : "your answer") : ""}${key.distractor_types ? " · " + esc(key.distractor_types[i]) : ""}</span></div>`).join("") + "</div>";
  } else {
    h += `<h3>Key · پاسخ رسمی</h3>` + (a.accept ? `<div class="accept">${a.accept.map(x => `<span dir="auto">${esc(x)}</span>`).join("")}</div>` : `<div class="muted">Scored by rubric</div>`);
    if (a.reject && a.reject.length) h += `<div class="muted" dir="auto">Rejected: ${a.reject.map(esc).join(" · ")}</div>`;
    h += `<h3>Your answer · پاسخ شما</h3><div class="block" dir="auto">${mine ? esc(mine) : '<span class="muted">(none)</span>'}</div>`;
  }
  if (it.own_source) h += `<div class="result neutral">You are an author of this dataset, so your verdict won't count towards validation. Your flags, such as “No longer the norm in Iran today”, are still shown to admins.</div>`;
  if (rv.blind_correct === true) h += `<div class="result ok">Your blind answer matches the key.</div>`;
  else if (rv.blind_correct === false) h += `<div class="result no">${mine === null || mine === "" ? "You didn't know the answer." : "Your blind answer differs from the key. Is the key wrong, or is your answer?"}</div>`;
  else h += `<div class="result neutral">Generation item: judge whether the rubric and reference below describe a good answer.</div>`;
  if (key.rubric) h += `<h3>Rubric · معیارها</h3><table class="kv">` + key.rubric.map(c => `<tr><td>${esc(c.id)}${c.gate ? " (gate)" : ""}</td><td dir="auto">${esc(c.description)}</td></tr>`).join("") + "</table>";
  if (key.reference) h += `<h3>Reference answer</h3><div class="block" dir="auto">${esc(key.reference)}</div>`;
  const notes = ["variation_note", "sources", "as_of", "region", "difficulty"].filter(k => md[k]);
  if (notes.length) h += `<h3>Notes from the author</h3><table class="kv">` + notes.map(k => `<tr><td>${esc(k)}</td><td dir="auto">${esc(Array.isArray(md[k]) ? md[k].join(", ") : md[k])}</td></tr>`).join("") + "</table>";
  h += `<h3>Is the key correct? · آیا پاسخ رسمی درست است؟</h3><div class="verdicts">
      <button data-v="correct">Correct · درست (C)</button><button data-v="wrong">Wrong · نادرست (W)</button><button data-v="unsure">Not sure · مطمئن نیستم (U)</button></div>
    <h3>Problems with the item</h3><div class="flags">` + Object.entries(META.flags).map(([k, label]) =>
      `<label><input type="checkbox" value="${k}"${(rv.flags || []).includes(k) ? " checked" : ""}> ${esc(label)}</label>`).join("") + `</div>
    <h3>Correct answer, if the key is wrong</h3><input class="field" id="sugg" dir="auto" value="${esc(rv.suggested_answer || "")}">
    <h3>Comment and source</h3><textarea class="field" id="comment" dir="auto" placeholder="e.g. Dehkhoda gives …">${esc(rv.comment || "")}</textarea>
    <div class="actions"><button class="primary" id="save">Save and next · ذخیره</button><span class="hint">${rv.verdict ? `You saved “${esc(rv.verdict)}” earlier; saving again replaces it.` : ""}</span></div><div id="err"></div></div>`;
  $("main").innerHTML = h; $("main").scrollTop = 0;
  document.querySelectorAll(".verdicts button").forEach(b => { b.classList.toggle("on", b.dataset.v === verdict); b.onclick = () => setVerdict(b.dataset.v); });
  $("save").onclick = saveVerdict;
}
function setVerdict(v) { verdict = v; document.querySelectorAll(".verdicts button").forEach(b => b.classList.toggle("on", b.dataset.v === v)); }
async function saveVerdict() {
  if (!verdict) return showErr("Choose correct, wrong or not sure.");
  const flags = [...document.querySelectorAll(".flags input:checked")].map(x => x.value);
  try {
    await api("/api/verdict", { reviewer: who(), item_id: current.id, verdict, flags, comment: $("comment").value, suggested_answer: $("sugg").value });
    await reload(); next();
  } catch (e) { showErr(e.message); }
}
document.addEventListener("keydown", e => {
  if (!current || $("review-view").style.display === "none") return;
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && $("save")) { e.preventDefault(); return saveVerdict(); }
  if (e.key === "Enter" && $("submit") && (!typing || document.activeElement.id === "ans") && document.activeElement.tagName !== "TEXTAREA") { e.preventDefault(); return submitBlind(false); }
  if (typing) return;
  if ($("submit") && /^[1-5]$/.test(e.key) && document.querySelector(`.opt[data-i="${+e.key - 1}"]`)) pick(+e.key - 1);
  if ($("save")) { const v = { c: "correct", w: "wrong", u: "unsure" }[e.key.toLowerCase()]; if (v) setVerdict(v); }
});

async function reload() {
  if (!who()) { ITEMS = []; renderList(); return; }
  ITEMS = await api(`/api/items?reviewer=${encodeURIComponent(who())}`);
  if (current) current = ITEMS.find(it => it.id === current.id) || null;
  renderList();
}
async function renderProgress() {
  const p = await api("/api/progress");
  const pct = v => v === null || v === undefined ? "–" : (100 * v).toFixed(0) + "%";
  const row = r => `<tr><td>${esc(r.group.replace("source:", ""))}</td><td class="num">${r.items || 0}</td><td class="num">${r.unreviewed || 0}</td>
    <td class="num">${r.in_review || 0}</td><td class="num">${r.validated || 0}</td><td class="num">${r.disputed || 0}</td><td class="num">${r.outdated || 0}</td>
    <td class="num">${pct(r.human_accuracy)}</td><td class="num">${r.blind_answers}</td></tr>`;
  const table = (title, rows) => `<h3>${title}</h3><table><thead><tr><th></th><th class="num">Items</th><th class="num">Unreviewed</th><th class="num">In review</th>
    <th class="num">Validated</th><th class="num">Disputed</th><th class="num">Outdated</th><th class="num">Human accuracy</th><th class="num">Blind answers</th></tr></thead><tbody>${rows.map(row).join("")}</tbody></table>`;
  const native = p.rows.filter(r => !r.group.startsWith("source:")), ref = p.rows.filter(r => r.group.startsWith("source:"));
  const tot = native.reduce((a, r) => { for (const k of ["items", "validated", "disputed"]) a[k] += r[k] || 0; return a; }, { items: 0, validated: 0, disputed: 0 });
  $("progress-view").innerHTML = `<div class="page"><h2 class="title">Progress</h2>
    <p>${tot.validated} of ${tot.items} native items validated${tot.disputed ? `, ${tot.disputed} disputed` : ""}. Human accuracy is the share of blind answers that matched the key: a human baseline, and a warning sign when it is low.</p>
    ${table("Native items by task", native)}${ref.some(r => r.blind_answers || r.review_required) ? table("Imported and derived sources (social norms count only once validated; others are audits)", ref.filter(r => r.blind_answers || r.review_required)) : ""}
    <h3>Reviewers</h3><p>${Object.entries(p.reviewers).map(([k, v]) => `${esc(k)} (${v})`).join(", ") || "No reviews yet."}
    · <a href="/api/export">Download all reviews (zip)</a> to commit them to <code>validation/</code></p>
    <h3>Disputed or flagged items, for authors to fix</h3>` + (p.problems.length ? `<table><thead><tr><th>Item</th><th>Status and flags</th><th>Reviewer notes</th></tr></thead><tbody>`
      + p.problems.map(x => `<tr><td><code>${esc(x.id)}</code><div class="muted">${esc(x.task)}</div><div class="fa" dir="auto">${esc(x.prompt)}</div></td>
        <td><span class="chip ${x.status}">${STATUS[x.status]}</span><div class="muted">${x.flags.map(f => esc(META.flags[f] || f)).join(", ")}</div></td>
        <td>${x.comments.map(c => `<div><b>${esc(c.reviewer)}</b>: ${esc(c.verdict)}${c.suggested_answer ? ` — suggests <span dir="auto">${esc(c.suggested_answer)}</span>` : ""}${c.comment ? `<div class="fa" dir="auto">${esc(c.comment)}</div>` : ""}</div>`).join("")}</td></tr>`).join("")
      + "</tbody></table>" : `<p class="muted">None.</p>`) + `<p class="muted">Fix items in <code>python -m farhang browse</code>. Editing an item resets its reviews, because reviewers judged the old version.</p></div>`;
}
const VIEWS = ["review", "progress", "people"];
function tab(name) {
  VIEWS.forEach(v => $("t-" + v).classList.toggle("on", v === name));
  $("review-view").style.display = name === "review" ? "" : "none"; $("filters").style.display = name === "review" ? "" : "none";
  $("progress-view").classList.toggle("hidden", name !== "progress");
  $("people-view").classList.toggle("hidden", name !== "people");
  if (name === "progress") renderProgress().catch(e => $("progress-view").innerHTML = `<div class="msg">${esc(e.message)}</div>`);
  if (name === "people") renderPeople().catch(e => $("people-view").innerHTML = `<div class="msg">${esc(e.message)}</div>`);
}

async function renderPeople(notice) {
  const [users, progress] = await Promise.all([api("/api/users"), api("/api/progress")]);
  const count = progress.reviewers || {};
  const rows = users.map(u => `<tr><td><b>${esc(u.name)}</b>${u.name === ME.reviewer ? ' <span class="muted">(you)</span>' : ""}</td>
    <td>${u.role === "admin" ? "Admin" : "Reviewer"}</td>
    <td class="num">${count[u.name] || 0}</td>
    <td>${!u.managed ? '<span class="muted">from the users Secret</span>' : u.must_change ? '<span class="chip in_review">waiting for first sign-in</span>' : '<span class="chip validated">active</span>'}</td>
    <td class="muted">${u.created ? esc(u.created.slice(0, 10)) + (u.created_by ? " by " + esc(u.created_by) : "") : ""}</td>
    <td>${u.managed && u.name !== ME.reviewer ? `<button data-act="reset" data-name="${esc(u.name)}">Reset password</button>
      <button data-act="role" data-role="${u.role === "admin" ? "reviewer" : "admin"}" data-name="${esc(u.name)}">Make ${u.role === "admin" ? "reviewer" : "admin"}</button>
      <button data-act="remove" data-name="${esc(u.name)}">Remove</button>` : ""}</td></tr>`).join("");
  $("people-view").innerHTML = `<div class="page"><h2 class="title">People</h2>
    <p class="muted">Add a reviewer and send them the one-time password privately (not in a group chat). They choose their own password when they first sign in. Removing someone keeps their reviews.</p>
    <div id="notice">${notice || ""}</div>
    <h3>Add a person</h3>
    <div class="controls"><input id="new-name" placeholder="Name, e.g. maryam" size="20" dir="auto">
      <select id="new-role"><option value="reviewer">Reviewer</option><option value="admin">Admin (can manage people)</option></select>
      <button class="primary" id="add-user">Add</button></div>
    <h3>Accounts</h3>
    <div class="wrap"><table><thead><tr><th>Name</th><th>Role</th><th class="num">Reviews</th><th>Status</th><th>Added</th><th></th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
  $("add-user").onclick = () => people({ action: "add", name: $("new-name").value, role: $("new-role").value });
  $("new-name").addEventListener("keydown", e => { if (e.key === "Enter") $("add-user").click(); });
  document.querySelectorAll("#people-view button[data-act]").forEach(b => b.onclick = () => {
    const { act, name, role } = b.dataset;
    if (act === "remove" && !confirm(`Remove ${name}? Their reviews are kept.`)) return;
    if (act === "reset" && !confirm(`Reset ${name}'s password? Their current password stops working.`)) return;
    people({ action: act, name, role });
  });
}
async function people(body) {
  try {
    const res = await api("/api/users", body);
    const notice = res.password
      ? `<div class="secret">One-time password for <b>${esc(res.name)}</b>: <code>${esc(res.password)}</code><br><span class="muted">Copy it now; it isn't shown again. Their sign-in name is <b>${esc(res.name)}</b>.</span></div>`
      : `<div class="ok-msg">Done.</div>`;
    await renderPeople(notice);
  } catch (e) { $("notice").innerHTML = `<div class="msg">${esc(e.message)}</div>`; }
}

function passwordForm(forced) {
  const host = forced ? $("main") : $("people-view");
  if (!forced) {
    VIEWS.forEach(v => $("t-" + v).classList.remove("on"));
    ["review-view", "filters"].forEach(id => $(id).style.display = "none");
    $("progress-view").classList.add("hidden");
    host.classList.remove("hidden");
  }
  host.innerHTML = `<div class="welcome"><h2>${forced ? "Choose your password" : "Change your password"}</h2>
    ${forced ? `<p>Welcome, <b>${esc(ME.reviewer)}</b>. You signed in with a one-time password; choose your own to continue.</p>` : ""}
    <div class="form">${forced ? "" : `<label>Current password<input type="password" id="pw0" autocomplete="current-password"></label>`}
    <label>New password (at least 10 characters)<input type="password" id="pw1" autocomplete="new-password"></label>
    <label>Again<input type="password" id="pw2" autocomplete="new-password"></label>
    <div><button class="primary" id="pw-save">Save password</button></div></div><div id="err"></div></div>`;
  if ($("pw0")) $("pw0").focus(); else $("pw1").focus();
  $("pw-save").onclick = async () => {
    if ($("pw1").value !== $("pw2").value) return showErr("The two passwords differ.");
    try {
      const body = { new_password: $("pw1").value };
      if (!forced) body.current_password = $("pw0").value;
      await api("/api/password", body);
      host.innerHTML = `<div class="welcome"><div class="ok-msg">Password saved.${forced ? "" : " You stay signed in here; any other sessions were signed out."}</div>
        <button class="primary" id="pw-cont">Continue</button></div>`;
      $("pw-cont").onclick = () => location.reload();
    } catch (e) { showErr(e.message); }
  };
}

function loginForm() {
  document.querySelector(".tabs").classList.add("hidden");
  $("filters").style.display = "none";
  $("who-row").classList.add("hidden");
  $("list").style.display = "none";
  $("review-view").style.gridTemplateColumns = "1fr";
  $("main").innerHTML = `<div class="welcome"><h2>Sign in · ورود</h2>
    <div class="form"><label>Name<input id="li-name" autocomplete="username" dir="auto"></label>
    <label>Password<input type="password" id="li-pass" autocomplete="current-password"></label>
    <div><button class="primary" id="li-btn">Sign in</button></div></div><div id="err"></div></div>`;
  const submit = async () => {
    $("li-btn").disabled = true;
    try {
      await api("/api/login", { name: $("li-name").value, password: $("li-pass").value });
      location.reload();
    } catch (e) { $("li-btn").disabled = false; showErr(e.message); }
  };
  $("li-btn").onclick = submit;
  $("li-pass").addEventListener("keydown", e => { if (e.key === "Enter") submit(); });
  $("li-name").addEventListener("keydown", e => { if (e.key === "Enter") $("li-pass").focus(); });
  $("li-name").focus();
}

$("t-review").onclick = () => tab("review"); $("t-progress").onclick = () => tab("progress"); $("t-people").onclick = () => tab("people");
$("pw-btn").onclick = () => passwordForm(false);
$("signout-btn").onclick = async () => { try { await api("/api/logout", {}); } catch (e) { /* already out */ } location.reload(); };
$("who").addEventListener("change", async () => { store("farhang-reviewer", who()); current = null; await reload(); welcome(); });
["f-scope", "f-task", "f-show"].forEach(id => $(id).addEventListener("input", renderList));
$("f-track").addEventListener("input", () => { $("f-task").value = ""; initFilters(); renderList(); });
(async () => {
  try {
    ME = await api("/api/whoami");
    if (ME.auth_enabled && !ME.signed_in) return loginForm();
    if (ME.signed_in) {
      SIGNED_IN = true; $("who").value = ME.reviewer; $("who").readOnly = true; $("who").title = "Signed in";
      $("signout-btn").classList.remove("hidden");
      if (ME.can_change_password) $("pw-btn").classList.remove("hidden");
      if (ME.role === "admin") $("t-people").classList.remove("hidden");
      if (ME.must_change) {
        ["filters", "list"].forEach(id => $(id).style.display = "none"); $("review-view").style.gridTemplateColumns = "1fr";
        document.querySelector(".tabs").classList.add("hidden"); $("pw-btn").classList.add("hidden");
        return passwordForm(true);
      }
    }
  } catch (e) { /* older server */ }
  initFilters(); welcome(); reload();
})();
