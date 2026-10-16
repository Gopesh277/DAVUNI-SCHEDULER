// Dean's Office portal: department timetables + faculty meeting availability.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

async function api(path, opts = {}) {
  const res = await fetch(path, { cache: "no-store", headers: { "Content-Type": "application/json" }, ...opts });
  if (res.status === 401) { window.location.href = "/login.html"; throw new Error("Please sign in again."); }
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try { const j = await res.json(); msg = j.detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return res.json();
}
function notice(kind, html) {
  const icon = { err: "✕", ok: "✓", warn: "!", info: "i" }[kind] || "i";
  $("deanNotice").innerHTML = `<div class="notice ${kind}"><span>${icon}</span><div>${html}</div></div>`;
}
const clearNotice = () => { $("deanNotice").innerHTML = ""; };

const fmt12 = (t) => { const [h, m] = t.split(":").map(Number); return `${((h + 11) % 12) + 1}:${String(m).padStart(2, "0")} ${h >= 12 ? "PM" : "AM"}`; };
const range12 = (a, b) => `${fmt12(a)} – ${fmt12(b)}`;
const deptList = (arr) => (arr || []).map((d) => d.short).join(", ");

let overview = [], allFaculty = [], snap = null, view = "teacher";
const selected = new Set();

// Every time the portal is opened it starts blank: nothing selected, no results, no timetable loaded.
// (If the browser restores the page from its back/forward cache, reload it instead.)
window.addEventListener("pageshow", (e) => { if (e.persisted) window.location.reload(); });

function resetPortal() {
  snap = null; view = "teacher"; selected.clear();
  document.querySelectorAll("#deanTabs .tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === "tt"));
  $("panel-tt").classList.add("active"); $("panel-meet").classList.remove("active");
  document.querySelectorAll("#viewSeg button").forEach((x) => x.classList.toggle("active", x.dataset.view === "teacher"));
  $("ttBody").style.display = "none"; $("ttEmpty").style.display = "block";
  $("ttEmpty").textContent = "Choose a department above to see its latest timetable.";
  $("ttMeta").textContent = ""; $("ttChips").innerHTML = ""; $("ttGridHost").innerHTML = ""; $("ttUnplaced").innerHTML = "";
  $("facSearch").value = ""; $("facDept").value = "";
  $("whenStart").value = "10:30"; $("whenEnd").value = "11:30";
  $("whenDay").value = todayName() === "Sunday" ? "Monday" : todayName();
  $("meetResult").innerHTML = ""; clearNotice();
  renderChips();
}

// Re-read the latest data from the server (departments regenerate their timetables at any time).
async function refreshLists() {
  const [ov, fac] = await Promise.all([api("/api/dean/overview"), api("/api/dean/faculty")]);
  overview = ov.departments; allFaculty = fac.faculty;
  const keep = new Set(allFaculty.map((f) => f.key));
  [...selected].forEach((k) => { if (!keep.has(k)) selected.delete(k); });   // drop anyone who no longer has a timetable
  const dept = $("deptSelect").value;
  renderStats(); renderDeptSelect(); if (dept && overview.some((d) => d.id === dept)) $("deptSelect").value = dept;
  renderFacDeptFilter(); renderFacList(); renderChips();
}

// ------------------------------------------------------------------ tabs
document.querySelectorAll("#deanTabs .tab").forEach((t) => t.addEventListener("click", () => {
  document.querySelectorAll("#deanTabs .tab").forEach((x) => x.classList.toggle("active", x === t));
  ["tt", "meet"].forEach((n) => $("panel-" + n).classList.toggle("active", n === t.dataset.tab));
  refreshLists().catch((e) => notice("err", esc(e.message)));
}));
$("logoutBtn").addEventListener("click", async () => {
  try { await fetch("/api/logout", { method: "POST" }); } catch (e) {}
  window.location.href = "/login.html";
});

// ------------------------------------------------------------- stats + dept list
function renderStats() {
  const withTT = overview.filter((d) => d.has_timetable).length;
  const sessions = overview.reduce((n, d) => n + (d.placed || 0), 0);
  $("deanStats").innerHTML = [
    [`${withTT}/${overview.length}`, "Depts with timetable"],
    [allFaculty.length, "Faculty"],
    [sessions, "Sessions placed"],
  ].map(([n, l]) => `<div class="stat"><div class="stat-num">${n}</div><div class="stat-label">${l}</div></div>`).join("");
}
function renderDeptSelect() {
  const when = (iso) => { try { return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" }); } catch (e) { return ""; } };
  $("deptSelect").innerHTML = `<option value="" selected disabled>Select a department…</option>` + overview.map((d) =>
    `<option value="${d.id}">${esc(d.display)} — ${d.has_timetable ? "✓ " + when(d.generated_at) : "no timetable yet"}</option>`).join("");
}

// ------------------------------------------------------------ timetable viewer
async function loadTimetable() {
  const id = $("deptSelect").value;
  clearNotice();
  if (!id) { notice("warn", "Choose a department first."); return; }
  $("loadTtBtn").disabled = true;
  try {
    snap = await api(`/api/dean/timetable/${id}`);
  } catch (e) {
    snap = null;
    $("ttBody").style.display = "none";
    $("ttEmpty").style.display = "block";
    $("ttEmpty").textContent = e.message;
    $("ttMeta").textContent = "";
    $("loadTtBtn").disabled = false;
    return;
  }
  $("loadTtBtn").disabled = false;
  $("ttEmpty").style.display = "none";
  $("ttBody").style.display = "block";
  const st = snap.stats, ok = snap.status === "OPTIMAL" || snap.status === "FEASIBLE";
  const when = snap.generated_at ? new Date(snap.generated_at).toLocaleString() : "";
  $("ttMeta").textContent = snap.department ? snap.department.display : "";
  $("ttChips").innerHTML = [
    `<span class="pill ${ok && !st.unplaced ? "good" : "warn"}">${ok && !st.unplaced ? "Complete" : esc(snap.status)} · <b>${st.completion_percentage}%</b></span>`,
    `<span class="pill"><b>${st.placed}</b> sessions placed</span>`,
    st.unplaced ? `<span class="pill warn"><b>${st.unplaced}</b> unplaced</span>` : "",
    `<span class="pill"><b>${st.faculty}</b> faculty · <b>${st.sections}</b> sections · <b>${st.rooms}</b> rooms</span>`,
    when ? `<span class="pill">generated ${esc(when)}</span>` : "",
  ].join("");
  fillEntities();
}
function entitiesFor(v) {
  if (v === "room") return snap.config.rooms.slice();
  const key = v === "teacher" ? "teacher" : "base_section";
  return [...new Set(snap.placed.map((p) => p[key]))].sort((a, b) => a.localeCompare(b));
}
function fillEntities() {
  const list = entitiesFor(view);
  $("entitySelect").innerHTML = list.map((x) => `<option>${esc(x)}</option>`).join("") || "<option>(none)</option>";
  renderGrid();
}
document.querySelectorAll("#viewSeg button").forEach((b) => b.addEventListener("click", () => {
  view = b.dataset.view;
  document.querySelectorAll("#viewSeg button").forEach((x) => x.classList.toggle("active", x === b));
  if (snap) fillEntities();
}));
$("entitySelect").addEventListener("change", () => snap && renderGrid());
$("loadTtBtn").addEventListener("click", loadTimetable);
$("deptSelect").addEventListener("change", loadTimetable);

function renderGrid() {
  const cfg = snap.config, sel = $("entitySelect").value;
  const mine = snap.placed.filter((p) => (view === "teacher" ? p.teacher : view === "room" ? p.room : p.base_section) === sel);
  const cells = {};
  mine.forEach((p) => { for (let i = 0; i < p.duration; i++) (cells[`${p.day}|${p.start_period + i}`] ||= []).push({ p, cont: i > 0 }); });
  const slot = ({ p, cont }) => {
    const meta = view === "teacher" ? [p.section, p.room] : view === "room" ? [p.teacher, p.section] : [p.teacher, p.room];
    const grp = view === "section" && p.group ? ` · ${esc(p.group)}` : "";
    return `<div class="slot ${p.kind === "Practical" ? "lab" : p.kind === "Tutorial" ? "tutorial" : "theory"}">
      <span class="code">${esc(p.course_code)}</span> <span class="meta" style="display:inline">${esc(p.kind)}${grp}${cont ? " · cont." : ""}</span>
      ${cont ? "" : `<span class="meta">${esc(p.course_name)}</span><span class="meta">${meta.map(esc).join(" · ")}</span>`}</div>`;
  };
  const head = `<thead><tr><th class="timecol">Period</th>${cfg.days.map((d) => `<th>${esc(d)}</th>`).join("")}</tr></thead>`;
  const body = cfg.periods.map((per) => {
    const brk = per.index === cfg.day_break_after_period ? " brk" : "";
    return `<tr><th>P${per.index}<br>${esc(per.label)}</th>${cfg.days.map((_, di) =>
      `<td class="cell${brk}">${(cells[`${di}|${per.index}`] || []).map(slot).join("")}</td>`).join("")}</tr>`;
  }).join("");
  $("ttGridHost").innerHTML = `<table class="tt">${head}<tbody>${body}</tbody></table>`;
  $("entityPill").textContent = `${mine.reduce((n, p) => n + p.duration, 0)} periods / week`;

  $("ttUnplaced").innerHTML = snap.unplaced.length
    ? `<div class="notice warn" style="margin-top:12px;"><span>!</span><div><b>${snap.unplaced.length} session(s) could not be placed</b> in this department's timetable: ${
        snap.missing_courses.slice(0, 6).map((m) => `${esc(m.course_code)} (${esc(m.teacher)}, ${esc(m.section)})`).join("; ")}${snap.missing_courses.length > 6 ? "…" : ""}</div></div>`
    : "";
}

// ------------------------------------------------------------- faculty picker
function renderFacDeptFilter() {
  const seen = new Map();
  allFaculty.forEach((f) => f.departments.forEach((d) => seen.set(d.id, d.short)));
  $("facDept").innerHTML = `<option value="">All departments</option>` +
    [...seen].sort((a, b) => a[1].localeCompare(b[1])).map(([id, s]) => `<option value="${id}">${esc(s)}</option>`).join("");
}
function shownFaculty() {
  const q = $("facSearch").value.trim().toLowerCase(), dept = $("facDept").value;
  return allFaculty.filter((f) => (!q || f.name.toLowerCase().includes(q)) && (!dept || f.departments.some((d) => d.id === dept)));
}
function renderFacList() {
  const rows = shownFaculty();
  $("facList").innerHTML = rows.length
    ? rows.map((f) => `<label class="fac-item"><input type="checkbox" data-key="${esc(f.key)}" ${selected.has(f.key) ? "checked" : ""}>
        <span>${esc(f.name)}</span><span class="dpt">${esc(deptList(f.departments))}</span></label>`).join("")
    : `<div class="dean-empty" style="border:0;">${allFaculty.length ? "No faculty match." : "No department has generated a timetable yet."}</div>`;
}
function renderChips() {
  $("facChips").innerHTML = [...selected].map((k) => {
    const f = allFaculty.find((x) => x.key === k);
    return f ? `<span class="chip">${esc(f.name)}<button type="button" data-rm="${esc(k)}" aria-label="Remove">×</button></span>` : "";
  }).join("");
}
$("facList").addEventListener("change", (e) => {
  const k = e.target.dataset.key; if (!k) return;
  e.target.checked ? selected.add(k) : selected.delete(k);
  renderChips();
});
$("facChips").addEventListener("click", (e) => {
  const k = e.target.dataset.rm; if (!k) return;
  selected.delete(k); renderChips(); renderFacList();
});
$("facSearch").addEventListener("input", renderFacList);
$("facDept").addEventListener("change", renderFacList);
$("facSelectShown").addEventListener("click", () => { shownFaculty().forEach((f) => selected.add(f.key)); renderChips(); renderFacList(); });
$("facClear").addEventListener("click", () => { selected.clear(); renderChips(); renderFacList(); });

// ------------------------------------------------------------ availability
$("whenDay").innerHTML = DAYS.map((d) => `<option>${d}</option>`).join("");
const todayName = () => DAYS[(new Date().getDay() + 6) % 7];
$("whenDay").value = todayName() === "Sunday" ? "Monday" : todayName();

async function check({ right_now = false } = {}) {
  clearNotice();
  if (selected.size === 0) { notice("warn", "Pick at least one faculty member first."); return; }
  const start = $("whenStart").value, end = $("whenEnd").value;
  const body = { faculty: [...selected] };
  if (start) { body.day = $("whenDay").value; body.start = start; if (end && !right_now) body.end = end; }
  const btn = $("checkBtn"); btn.disabled = true; btn.textContent = "Checking…";
  try {
    const r = await api("/api/dean/availability", { method: "POST", body: JSON.stringify(body) });
    renderResult(r, !!body.end);
  } catch (e) {
    notice("err", esc(e.message));
  } finally { btn.disabled = false; btn.textContent = "Check availability"; }
}
$("checkBtn").addEventListener("click", () => check());
$("nowBtn").addEventListener("click", () => {
  const n = new Date();
  $("whenDay").value = todayName();
  $("whenStart").value = `${String(n.getHours()).padStart(2, "0")}:${String(n.getMinutes()).padStart(2, "0")}`;
  $("whenEnd").value = "";
  check({ right_now: true });
});

function whereHtml(b) {
  return `<div class="where"><span class="room">${esc(b.room)}</span> — ${esc(b.course_code)} ${esc(b.course_name)} (${esc(b.kind)})
    <span class="sub">${esc(b.section)} · ${esc(b.dept_display)} · ${range12(b.start, b.end)}</span></div>`;
}
function personHtml(p) {
  const depts = esc(deptList(p.departments));
  if (!p.free) {
    return `<div class="person busy"><div class="person-top"><span class="person-name">${esc(p.name)}</span><span class="badge busy">BUSY</span></div>
      <div class="person-depts">${depts}</div>${p.busy.map(whereHtml).join("")}
      <div class="note">Free from <b>${fmt12(p.free_from)}</b></div></div>`;
  }
  const n = p.next_class;
  return `<div class="person free"><div class="person-top"><span class="person-name">${esc(p.name)}</span><span class="badge free">FREE</span></div>
    <div class="person-depts">${depts}</div>
    <div class="note">${n ? `Next class: <b>${fmt12(n.start)}</b> in ${esc(n.room)} (${esc(n.dept)} · ${esc(n.course_code)})` : "No further classes that day."}</div></div>`;
}

function renderResult(r, hadEnd) {
  const out = [];
  const at = r.at_time;
  if (at) {
    const when = hadEnd ? `${esc(at.day)}, ${range12(at.start, at.end)}` : `${esc(at.day)} at ${fmt12(at.start)}`;
    const total = at.people.length;
    out.push(at.all_free
      ? `<div class="verdict free">✅ <b>${total === 1 ? "Free" : `All ${total} are free`}</b> on ${when}.</div>`
      : `<div class="verdict busy">⚠️ <b>${at.busy_count} of ${total}</b> ${at.busy_count === 1 ? "is" : "are"} busy on ${when}. Their locations are shown below.</div>`);
    const sorted = at.people.slice().sort((a, b) => Number(a.free) - Number(b.free) || a.name.localeCompare(b.name));
    out.push(`<div class="people">${sorted.map(personHtml).join("")}</div>`);
  }
  if (!r.has_data) {
    out.push(`<div class="dean-empty">No department has generated a timetable yet, so there is nothing to compare.</div>`);
  } else {
    const sug = r.days.flatMap((d) => (r.suggested[d] || []).map(([a, b]) => ({ d, a, b })));
    out.push(`<h3 class="sub-h">When is everyone free together?</h3>`);
    out.push(sug.length
      ? `<p class="hint" style="margin:0 0 8px; font-size:12.5px; color:var(--ink-soft);">Every selected person has no class during these windows — click one to check it.</p>
         <div class="suggest">${sug.map((s) => `<button type="button" data-d="${esc(s.d)}" data-a="${s.a}" data-b="${s.b}">${esc(s.d.slice(0, 3))} ${range12(s.a, s.b)}</button>`).join("")}</div>`
      : `<div class="notice warn"><span>!</span><div>There is <b>no common free time</b> during teaching hours for this group. Try fewer people, or look at the grid to see who is blocking each slot.</div></div>`);
    const selDay = $("whenDay").value, selA = $("whenStart").value;
    const head = `<thead><tr><th>Time</th>${r.days.map((d) => `<th>${esc(d)}</th>`).join("")}</tr></thead>`;
    const rows = r.slots.map((s, i) => `<tr><th>${range12(s.start, s.end)}</th>${r.days.map((d) => {
      const c = r.grid[d][i];
      const tip = c.busy.map((b) => `${b.name}: ${b.room} (${b.course_code}, ${b.dept})`).join("\n");
      const sel = d === selDay && at && s.start === selA ? " sel" : "";
      return c.all_free
        ? `<td class="wk ok${sel}" data-d="${esc(d)}" data-a="${s.start}" data-b="${s.end}">✓ free</td>`
        : `<td class="wk no${sel}" data-d="${esc(d)}" data-a="${s.start}" data-b="${s.end}" title="${esc(tip)}">✕ ${c.busy.length} busy<small>${esc(c.busy.slice(0, 2).map((b) => b.name.split(" ").slice(-1)[0]).join(", "))}${c.busy.length > 2 ? "…" : ""}</small></td>`;
    }).join("")}</tr>`).join("");
    out.push(`<div class="grid-wrap"><table class="week">${head}<tbody>${rows}</tbody></table></div>
      <p class="hint" style="margin-top:8px; font-size:12px; color:var(--ink-soft);">Hover a red cell to see who is busy and where. Click any cell to check that slot.</p>`);
  }
  $("meetResult").innerHTML = out.join("");
}
$("meetResult").addEventListener("click", (e) => {
  const t = e.target.closest("[data-d][data-a]"); if (!t) return;
  $("whenDay").value = t.dataset.d; $("whenStart").value = t.dataset.a; $("whenEnd").value = t.dataset.b;
  check();
});

// ---------------------------------------------------------------- init
(async function init() {
  try {
    const me = await fetch("/api/me", { cache: "no-store" }).then((r) => r.json());
    if (!me.authenticated) { window.location.replace("/login.html"); return; }
    if (me.role !== "dean") { window.location.replace("/"); return; }
    const [ov, fac] = await Promise.all([api("/api/dean/overview"), api("/api/dean/faculty")]);
    overview = ov.departments; allFaculty = fac.faculty;
    renderStats(); renderDeptSelect(); renderFacDeptFilter(); renderFacList();
    resetPortal();   // blank start: nothing selected, nothing loaded
    if (!overview.some((d) => d.has_timetable)) {
      $("ttEmpty").textContent = "No department has generated a timetable yet. Once a department generates one, it will appear here.";
    }
  } catch (e) {
    notice("err", esc(e.message));
  }
})();
