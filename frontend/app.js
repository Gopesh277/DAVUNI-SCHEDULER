const API = "/api";

let cfg = null;             // { rooms, lab_rooms, days, periods, day_break_after_period, practical_block_size }
let solverSettings = null;  // { time_limit, workers, balance }
let courses = [];           // current register rows (with row_id)
let editRows = [];          // working copy for the editor
let schedule = null;        // last /api/generate or /api/schedule response
let diagnostics = null;     // last /api/courses/upload|reset|api/analyze "diagnostics" block

// =====================================================================
// fetch helpers
// =====================================================================
async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : undefined,
    ...opts,
  });
  if (res.status === 401) {
    window.location.href = "/login.html";
    throw new Error("Session expired. Redirecting to sign-in...");
  }
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = j.detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}
const getJSON = (path) => api(path);
const putJSON = (path, body) => api(path, { method: "PUT", body: JSON.stringify(body) });
const postJSON = (path, body) => api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });

// =====================================================================
// notices
// =====================================================================
function notice(kind, html) {
  document.getElementById("noticeArea").innerHTML =
    `<div class="notice ${kind}"><span>${kind === "ok" ? "✓" : kind === "err" ? "✕" : "⚠"}</span><div>${html}</div></div>`;
}
function clearNotice() { document.getElementById("noticeArea").innerHTML = ""; }

// =====================================================================
// board stats
// =====================================================================
function renderStats() {
  const teachers = new Set(courses.map(c => (c.faculty || "Unassigned").trim()));
  const sections = new Set(courses.map(c => c.base_section));
  const conflicts = schedule ? schedule.stats.unplaced : 0;
  const issues = diagnostics ? diagnostics.error_count + diagnostics.warning_count : 0;
  const stats = [
    { n: courses.length, l: "Course rows" },
    { n: teachers.size, l: "Faculty" },
    { n: sections.size, l: "Sections" },
    { n: cfg ? cfg.rooms.length : 0, l: "Rooms" },
    { n: conflicts, l: "Unplaced", warn: conflicts > 0 },
    { n: issues, l: "Register issues", warn: diagnostics && diagnostics.error_count > 0 },
  ];
  document.getElementById("boardStats").innerHTML = stats.map(s => `
    <div class="stat"><div class="stat-num ${s.warn ? "warn" : ""}">${s.n}</div><div class="stat-label">${s.l}</div></div>
  `).join("");
}

function renderScheduleNotice() {
  if (!schedule) { clearNotice(); return; }
  if ((schedule.status === "INFEASIBLE" || schedule.status === "UNKNOWN") && schedule.stats.placed === 0) {
    notice("err", `<b>No timetable could be produced</b> (solver status: ${schedule.status}). ${schedule.message || ""} Raise the time limit in Settings, or thin out the register — some teacher, section, or room combination is over-booked for the available ${cfg.days.length * cfg.periods.length} weekly slots.`);
  } else if (schedule.stats.unplaced === 0) {
    notice("ok", `<b>All ${schedule.stats.sessions_total} weekly sessions placed</b> with no teacher, section or room clashes, in ${schedule.wall_time_seconds}s (${schedule.status}).`);
  } else {
    const sample = schedule.unplaced.slice(0, 4).map(u => `${u.course_code} (${u.teacher} · ${u.section})`).join(", ");
    const pct = schedule.stats.completion_percentage;
    const capWarn = schedule.capacity && schedule.capacity.warnings.length
      ? ` Likely cause: ${schedule.capacity.warnings[0]}`
      : "";
    notice("warn", `<b>Best-effort timetable — ${pct}% complete.</b> ${schedule.stats.unplaced} session(s) couldn't be placed out of ${schedule.stats.sessions_total} — e.g. ${sample}${schedule.stats.unplaced > 4 ? ", …" : ""}.${capWarn} Try a longer time limit, or thin out the register on Manage data.`);
  }
}

// =====================================================================
// grid rendering (shared by room / teacher / section views)
// =====================================================================
function cellKey(d, p) { return `${d}-${p}`; }

function buildGridHTML(sessionsForThis, labelFn) {
  const days = cfg.days, periods = cfg.periods;
  const map = {};
  sessionsForThis.forEach(s => {
    const key = cellKey(s.day, s.periods[0]);
    (map[key] = map[key] || []).push(s);
  });
  let html = `<table class="tt"><thead><tr><th>Day</th>`;
  periods.forEach(p => html += `<th class="timecol">${p.label}</th>`);
  html += `</tr></thead><tbody>`;
  days.forEach((day, di) => {
    html += `<tr><th>${day}</th>`;
    const occupiedCols = new Set();
    periods.forEach(p => {
      if (occupiedCols.has(p.index)) return;
      const items = map[cellKey(di, p.index)] || [];
      if (items.length === 0) { html += `<td class="cell"></td>`; return; }
      const first = items[0];
      html += `<td class="cell" ${first.duration === 2 ? 'colspan="2"' : ""}>`;
      if (first.duration === 2) occupiedCols.add(p.index + 1);
      items.forEach(s => {
        const groupTag = s.group ? ` · ${s.group}` : "";
        html += `<div class="slot ${s.kind === "Practical" ? "lab" : ""}">
          <span class="code">${s.course_code}</span>
          <span class="meta">${labelFn(s)}</span>
          <span class="meta">${s.kind}${s.duration === 2 ? " · 2 periods" : ""} · Room ${s.room}${groupTag}</span>
        </div>`;
      });
      html += `</td>`;
    });
    html += `</tr>`;
  });
  html += `</tbody></table>`;
  return html;
}

function renderRoomView() {
  const sel = document.getElementById("roomSelect");
  const rooms = cfg.rooms;
  if (sel.dataset.count != rooms.length) {
    sel.innerHTML = rooms.map(r => `<option value="${r}">Room ${r}</option>`).join("");
    sel.dataset.count = rooms.length;
  }
  const room = sel.value || rooms[0];
  document.getElementById("roomsHint").textContent = `${rooms.length} rooms configured`;
  const sessions = (schedule?.placed || []).filter(s => s.room === room);
  document.getElementById("roomPill").textContent = `${sessions.length} session-slots this week`;
  document.getElementById("roomGridHost").innerHTML = sessions.length
    ? buildGridHTML(sessions, s => `${s.teacher} · ${s.section}`)
    : `<div class="empty-state">No sessions in this room yet — click Generate timetable.</div>`;
}

function renderTeacherView() {
  const sel = document.getElementById("teacherSelect");
  const teachers = [...new Set(courses.map(c => (c.faculty || "Unassigned").trim()))].sort();
  if (sel.dataset.count != teachers.length) {
    sel.innerHTML = teachers.map(t => `<option value="${t}">${t}</option>`).join("");
    sel.dataset.count = teachers.length;
  }
  const teacher = sel.value || teachers[0];
  const sessions = (schedule?.placed || []).filter(s => s.teacher === teacher);
  const hrs = sessions.reduce((a, s) => a + s.duration, 0);
  const row = courses.find(c => (c.faculty || "").trim() === teacher);
  const nature = row ? row.nature : "Regular";
  const cls = /contract/i.test(nature || "") ? "con" : "reg";
  document.getElementById("teacherPill").innerHTML = `${hrs} contact hrs/week · <span class="tagchip ${cls}">${nature}</span>`;
  document.getElementById("teacherGridHost").innerHTML = sessions.length
    ? buildGridHTML(sessions, s => `${s.course_code} · ${s.section}`)
    : `<div class="empty-state">No sessions for this faculty member yet — click Generate timetable.</div>`;
}

function renderSectionView() {
  const sel = document.getElementById("sectionSelect");
  const sections = [...new Set(courses.map(c => c.base_section))].sort();
  if (sel.dataset.count != sections.length) {
    sel.innerHTML = sections.map(s => `<option value="${s}">${s}</option>`).join("");
    sel.dataset.count = sections.length;
  }
  const section = sel.value || sections[0];
  const sessions = (schedule?.placed || []).filter(s => s.base_section === section);
  const hrs = sessions.reduce((a, s) => a + s.duration, 0);
  const groupCount = new Set(sessions.filter(s => s.group).map(s => s.group)).size;
  document.getElementById("sectionPill").textContent = groupCount
    ? `${hrs} class hrs/week · incl. ${groupCount} sub-group${groupCount > 1 ? "s" : ""}`
    : `${hrs} class hrs/week`;
  document.getElementById("sectionGridHost").innerHTML = sessions.length
    ? buildGridHTML(sessions, s => `${s.course_code} · ${s.teacher}`)
    : `<div class="empty-state">No sessions for this section yet — click Generate timetable.</div>`;
}

function renderLoadView() {
  const rows = schedule?.faculty_load || [];
  if (!rows.length) {
    document.getElementById("loadHost").innerHTML = `<div class="empty-state">Generate a timetable to see the load balance.</div>`;
    return;
  }
  const max = Math.max(...rows.map(r => r.total), 1);
  let html = `<table class="simple"><thead><tr>
    <th>Faculty</th><th>Register</th><th>Theory</th><th>Practical</th><th>Tutorial</th><th>Busiest day</th><th>Weekly load</th>
  </tr></thead><tbody>`;
  rows.forEach(r => {
    const cls = /contract/i.test(r.nature || "") ? "con" : "reg";
    const pct = Math.min(100, (r.total / max) * 100);
    const over = r.total > 22;
    const atCap = r.busiest_day >= r.daily_cap;
    html += `<tr>
      <td><b>${r.faculty}</b></td>
      <td><span class="tagchip ${cls}">${r.nature}</span></td>
      <td>${r.theory}</td><td>${r.practical}</td><td>${r.tutorial}</td>
      <td><span ${atCap ? 'style="color:var(--gold-deep);font-weight:600;"' : ""}>${r.busiest_day} / ${r.daily_cap} cap</span></td>
      <td><div style="display:flex;align-items:center;gap:8px;">
        <div class="bar-track"><div class="bar-fill ${over ? "over" : ""}" style="width:${pct}%"></div></div>
        <span style="font-family:'JetBrains Mono',monospace;font-size:11.5px;">${r.total} hrs</span>
      </div></td>
    </tr>`;
  });
  html += `</tbody></table>`;
  document.getElementById("loadHost").innerHTML = html;
}

function renderAllViews() {
  renderStats();
  renderScheduleNotice();
  renderRoomView();
  renderTeacherView();
  renderSectionView();
  renderLoadView();
}

// =====================================================================
// Manage data (editor)
// =====================================================================
const EDITOR_FIELDS = [
  { k: "faculty", label: "Faculty" },
  { k: "nature", label: "Register", type: "select", options: ["Regular", "Contractual", "Senior", "Super Senior", "New Faculty"] },
  { k: "course_code", label: "Code" },
  { k: "course_name", label: "Course" },
  { k: "course_type", label: "Type" },
  { k: "programme", label: "Programme" },
  { k: "semester", label: "Sem" },
  { k: "theory_hrs", label: "Th", type: "number" },
  { k: "tutorial_hrs", label: "Tu", type: "number" },
  { k: "practical_hrs", label: "Pr", type: "number" },
];

function renderEditor() {
  editRows = courses.map(c => ({ ...c }));
  document.getElementById("rowCountPill").textContent = `${editRows.length} rows`;
  drawEditorTable();
}
function drawEditorTable() {
  let html = `<table class="editor"><thead><tr>`;
  EDITOR_FIELDS.forEach(f => html += `<th>${f.label}</th>`);
  html += `<th></th></tr></thead><tbody>`;
  editRows.forEach((row, i) => {
    html += `<tr data-i="${i}">`;
    EDITOR_FIELDS.forEach(f => {
      if (f.type === "select") {
        html += `<td><select data-field="${f.k}">${f.options.map(o => `<option ${row[f.k] === o ? "selected" : ""}>${o}</option>`).join("")}</select></td>`;
      } else {
        const v = (row[f.k] ?? "").toString().replace(/"/g, "&quot;");
        html += `<td><input data-field="${f.k}" type="${f.type || "text"}" value="${v}"></td>`;
      }
    });
    html += `<td><button class="row-del" title="Remove row">✕</button></td></tr>`;
  });
  html += `</tbody></table>`;
  const host = document.getElementById("editorHost");
  host.innerHTML = html;
  host.querySelectorAll("tr[data-i]").forEach(tr => {
    const i = Number(tr.dataset.i);
    tr.querySelectorAll("[data-field]").forEach(inp => {
      const handler = () => { editRows[i][inp.dataset.field] = inp.value; };
      inp.addEventListener("input", handler);
      inp.addEventListener("change", handler);
    });
    tr.querySelector(".row-del").addEventListener("click", () => {
      editRows.splice(i, 1);
      drawEditorTable();
      document.getElementById("rowCountPill").textContent = `${editRows.length} rows`;
    });
  });
}

document.getElementById("addRowBtn").addEventListener("click", () => {
  editRows.push({ faculty: "", nature: "Regular", course_code: "", course_name: "", course_type: "Th",
    programme: "", semester: "", theory_hrs: 0, tutorial_hrs: 0, practical_hrs: 0 });
  drawEditorTable();
  document.getElementById("rowCountPill").textContent = `${editRows.length} rows`;
});

document.getElementById("applyEditsBtn").addEventListener("click", async () => {
  const btn = document.getElementById("applyEditsBtn");
  setBusy(btn, true, "Saving…");
  try {
    const payload = editRows.map(r => ({
      faculty: r.faculty || "Unassigned",
      nature: r.nature || "Regular",
      course_code: r.course_code || "-",
      course_name: r.course_name || "Untitled course",
      course_type: r.course_type || "",
      programme: r.programme || "Unassigned",
      semester: r.semester || "",
      theory_hrs: Number(r.theory_hrs) || 0,
      tutorial_hrs: Number(r.tutorial_hrs) || 0,
      practical_hrs: Number(r.practical_hrs) || 0,
      emp_id: r.emp_id || null,
    }));
    const res = await putJSON("/courses", payload);
    courses = res.courses;
    await regenerate();
  } catch (e) {
    notice("err", `<b>Couldn't save the register.</b> ${e.message}`);
  } finally {
    setBusy(btn, false, "Save &amp; regenerate");
  }
});

// =====================================================================
// Upload / reset
// =====================================================================
document.getElementById("uploadBtn").addEventListener("click", () => document.getElementById("fileInput").click());
document.getElementById("fileInput").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const btn = document.getElementById("uploadBtn");
  setBusy(btn, true, "Uploading…");
  try {
    const fd = new FormData();
    fd.append("file", file);
    const res = await api("/courses/upload", { method: "POST", body: fd });
    courses = res.courses;
    diagnostics = res.diagnostics;
    if (document.getElementById("panel-data").classList.contains("active")) renderEditor();
    await regenerate();
  } catch (err) {
    notice("err", `<b>Couldn't read "${file.name}".</b> ${err.message}`);
  } finally {
    setBusy(btn, false, "↑ Load teaching-load file");
    e.target.value = "";
  }
});

document.getElementById("resetBtn").addEventListener("click", async () => {
  const btn = document.getElementById("resetBtn");
  setBusy(btn, true, "Resetting…");
  try {
    const res = await postJSON("/courses/reset");
    courses = res.courses;
    diagnostics = res.diagnostics;
    if (document.getElementById("panel-data").classList.contains("active")) renderEditor();
    await regenerate();
  } catch (e) {
    notice("err", `<b>Couldn't reset.</b> ${e.message}`);
  } finally {
    setBusy(btn, false, "↺ Reset to sample register");
  }
});

document.getElementById("downloadBtn").addEventListener("click", () => {
  window.location.href = API + "/schedule/download";
});

// =====================================================================
// Settings tab
// =====================================================================
function renderSettings() {
  document.getElementById("roomsBox").value = cfg.rooms.join("\n");
  document.getElementById("daysBox").value = cfg.days.join("\n");
  document.getElementById("breakAfterInput").value = cfg.day_break_after_period;
  document.getElementById("timeLimitInput").value = solverSettings.time_limit;
  document.getElementById("workersInput").value = solverSettings.workers;
  document.getElementById("balanceInput").checked = !!solverSettings.balance;
  document.getElementById("maxConsecutiveInput").value = cfg.max_consecutive_periods;
  document.getElementById("defaultCapInput").value = cfg.default_position_daily_cap;
  renderLabRoomChips();
  renderPeriodRows();
  renderPositionCapRows();
}

let positionCapRows = [];
function renderPositionCapRows() {
  positionCapRows = Object.entries(cfg.position_daily_caps || {}).map(([position, cap]) => ({ position, cap }));
  drawPositionCapRows();
}
function drawPositionCapRows() {
  const host = document.getElementById("positionCapsHost");
  host.innerHTML = positionCapRows.map((row, i) => `
    <div class="position-cap-row" data-i="${i}">
      <input type="text" class="pc-name" value="${(row.position ?? "").toString().replace(/"/g, "&quot;")}" placeholder="Register category, e.g. Senior">
      <input type="number" class="pc-cap" value="${row.cap}" min="1" max="20">
      <span class="cap-suffix">periods/day</span>
      <button class="row-del" title="Remove category">✕</button>
    </div>`).join("");
  host.querySelectorAll(".position-cap-row").forEach(row => {
    const i = Number(row.dataset.i);
    row.querySelector(".pc-name").addEventListener("input", (e) => positionCapRows[i].position = e.target.value);
    row.querySelector(".pc-cap").addEventListener("input", (e) => positionCapRows[i].cap = Number(e.target.value));
    row.querySelector(".row-del").addEventListener("click", () => {
      positionCapRows.splice(i, 1);
      drawPositionCapRows();
    });
  });
}
document.getElementById("addPositionBtn").addEventListener("click", () => {
  positionCapRows.push({ position: "New category", cap: 5 });
  drawPositionCapRows();
});

function renderLabRoomChips() {
  const host = document.getElementById("labRoomsHost");
  host.innerHTML = cfg.rooms.map(r => {
    const on = cfg.lab_rooms.includes(r);
    return `<label class="chip ${on ? "on" : ""}" data-room="${r}">
      <input type="checkbox" ${on ? "checked" : ""}> ${r}
    </label>`;
  }).join("");
  host.querySelectorAll(".chip").forEach(chip => {
    chip.addEventListener("click", (e) => {
      e.preventDefault();
      const room = chip.dataset.room;
      const idx = cfg.lab_rooms.indexOf(room);
      if (idx === -1) cfg.lab_rooms.push(room); else cfg.lab_rooms.splice(idx, 1);
      chip.classList.toggle("on");
      chip.querySelector("input").checked = cfg.lab_rooms.includes(room);
    });
  });
}

function renderPeriodRows() {
  const host = document.getElementById("periodsHost");
  host.innerHTML = cfg.periods.map((p, i) => `
    <div class="period-row" data-i="${i}">
      <input type="number" class="p-index" value="${p.index}" min="1">
      <input type="text" class="p-label" value="${p.label}">
      <button class="row-del" title="Remove period">✕</button>
    </div>`).join("");
  host.querySelectorAll(".period-row").forEach(row => {
    const i = Number(row.dataset.i);
    row.querySelector(".p-index").addEventListener("input", (e) => cfg.periods[i].index = Number(e.target.value));
    row.querySelector(".p-label").addEventListener("input", (e) => cfg.periods[i].label = e.target.value);
    row.querySelector(".row-del").addEventListener("click", () => {
      cfg.periods.splice(i, 1);
      renderPeriodRows();
    });
  });
}

document.getElementById("addPeriodBtn").addEventListener("click", () => {
  const nextIndex = cfg.periods.length ? Math.max(...cfg.periods.map(p => p.index)) + 1 : 1;
  cfg.periods.push({ index: nextIndex, label: "New period" });
  renderPeriodRows();
});

document.getElementById("saveSettingsBtn").addEventListener("click", async () => {
  const btn = document.getElementById("saveSettingsBtn");
  setBusy(btn, true, "Saving…");
  try {
    const rooms = document.getElementById("roomsBox").value.split("\n").map(s => s.trim()).filter(Boolean);
    const days = document.getElementById("daysBox").value.split("\n").map(s => s.trim()).filter(Boolean);
    const labRooms = cfg.lab_rooms.filter(r => rooms.includes(r));
    const periods = cfg.periods.map(p => ({ index: Number(p.index), label: p.label }))
      .sort((a, b) => a.index - b.index);

    const newConfig = {
      rooms, lab_rooms: labRooms.length ? labRooms : rooms, days, periods,
      day_break_after_period: Number(document.getElementById("breakAfterInput").value) || 4,
      practical_block_size: cfg.practical_block_size,
      max_consecutive_periods: Number(document.getElementById("maxConsecutiveInput").value) || 2,
      position_daily_caps: Object.fromEntries(
        positionCapRows.filter(r => r.position && r.position.trim()).map(r => [r.position.trim(), Number(r.cap) || 1])
      ),
      default_position_daily_cap: Number(document.getElementById("defaultCapInput").value) || 5,
    };
    const cfgRes = await putJSON("/config", newConfig);
    cfg = cfgRes.config;

    const settings = {
      time_limit: Number(document.getElementById("timeLimitInput").value) || 60,
      workers: Number(document.getElementById("workersInput").value) || 8,
      balance: document.getElementById("balanceInput").checked,
    };
    const settingsRes = await putJSON("/solver-settings", settings);
    solverSettings = settingsRes.solver_settings;

    document.getElementById("settingsSavedPill").textContent = "saved";
    renderSettings();
    await regenerate();
  } catch (e) {
    notice("err", `<b>Couldn't save settings.</b> ${e.message}`);
  } finally {
    setBusy(btn, false, "Save settings &amp; regenerate");
  }
});

// =====================================================================
// Tabs
// =====================================================================
document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById(`panel-${tab.dataset.tab}`).classList.add("active");
    if (tab.dataset.tab === "data") renderEditor();
    if (tab.dataset.tab === "settings") renderSettings();
  });
});
["roomSelect", "teacherSelect", "sectionSelect"].forEach(id => {
  document.getElementById(id).addEventListener("change", () => {
    if (id === "roomSelect") renderRoomView();
    if (id === "teacherSelect") renderTeacherView();
    if (id === "sectionSelect") renderSectionView();
  });
});

// =====================================================================
// Generate / init
// =====================================================================
function setBusy(btn, busy, label) {
  btn.disabled = busy;
  btn.dataset.label = btn.dataset.label || btn.innerHTML;
  btn.innerHTML = busy ? `<span class="spinner"></span> ${label}` : btn.dataset.label;
}

async function regenerate() {
  const btn = document.getElementById("regenBtn");
  setBusy(btn, true, "Solving… (can take 1-2 min)");
  clearNotice();
  try {
    schedule = await postJSON("/generate", {});
    document.getElementById("genMeta").textContent = `generated ${new Date().toLocaleString()} · ${schedule.wall_time_seconds}s`;
    renderAllViews();
  } catch (e) {
    // Don't leave a stale (possibly pre-fix, possibly just outdated)
    // schedule on screen when a generate call fails -- clear it so the
    // views honestly reflect "nothing generated", not old data.
    schedule = null;
    renderStats();
    renderRoomView();
    renderTeacherView();
    renderSectionView();
    renderLoadView();
    notice("err", `<b>Generation failed.</b> ${e.message} If this was a network timeout, the solver may still have been working — try Generate again, or raise the time limit in Settings.`);
  } finally {
    setBusy(btn, false, "⟳ Generate timetable");
  }
}
document.getElementById("regenBtn").addEventListener("click", regenerate);

document.getElementById("logoutBtn").addEventListener("click", async () => {
  try { await fetch("/api/logout", { method: "POST" }); } catch (e) {}
  window.location.href = "/login.html";
});

async function init() {
  const [configRes, coursesRes] = await Promise.all([getJSON("/config"), getJSON("/courses")]);
  cfg = configRes.config;
  solverSettings = configRes.solver_settings;
  courses = coursesRes.courses;
  renderStats();
  try {
    schedule = await getJSON("/schedule");
    document.getElementById("genMeta").textContent = `last generated · ${schedule.wall_time_seconds}s`;
    renderAllViews();
  } catch (e) {
    // no schedule yet -- kick one off automatically
    await regenerate();
  }
}
init();
