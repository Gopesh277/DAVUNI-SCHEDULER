const form = document.getElementById("loginForm");
const errorBox = document.getElementById("loginError");
const submitBtn = document.getElementById("loginSubmit");
const deptSelect = document.getElementById("department");
const deptHeading = document.getElementById("deptHeading");
const footDept = document.getElementById("footDept");
let departments = [];

// Show the chosen department in the letterhead exactly like the main page does.
function showDepartment() {
  const d = departments.find((x) => x.id === deptSelect.value);
  deptHeading.textContent = d ? d.display : "Select your department";
  footDept.textContent = d ? `Dept. of ${d.short}` : "All departments";
  document.title = d ? `Sign in · DAV University · ${d.short} Timetable` : "Sign in · DAV University · Timetable Management System";
}

fetch("/api/departments")
  .then((r) => r.json())
  .then((data) => {
    departments = data.departments || [];
    deptSelect.insertAdjacentHTML("beforeend", departments.map(
      (d) => `<option value="${d.id}">${d.display}</option>`).join(""));
    let saved = null;
    try { saved = localStorage.getItem("tt_department"); } catch (e) {}
    if (saved && departments.some((d) => d.id === saved)) deptSelect.value = saved;
    showDepartment();
    (deptSelect.value ? document.getElementById("username") : deptSelect).focus();
  })
  .catch(() => showError("Couldn't load the department list. Please refresh the page."));

deptSelect.addEventListener("change", showDepartment);

function showError(msg) {
  errorBox.textContent = msg;
  errorBox.style.display = "flex";
}

// If already signed in, skip straight to the app.
fetch("/api/me")
  .then((r) => r.json())
  .then((d) => { if (d.authenticated) window.location.replace("/"); })
  .catch(() => {});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorBox.style.display = "none";
  submitBtn.disabled = true;
  submitBtn.textContent = "Signing in…";

  const department = deptSelect.value;
  if (!department) {
    showError("Please select your department.");
    submitBtn.disabled = false;
    submitBtn.textContent = "Sign in";
    return;
  }
  const username = document.getElementById("username").value;
  const password = document.getElementById("password").value;

  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ department, username, password }),
    });
    if (!res.ok) {
      let msg = "Invalid username or password.";
      try { const j = await res.json(); msg = j.detail || msg; } catch (e) {}
      throw new Error(msg);
    }
    try { localStorage.setItem("tt_department", department); } catch (e) {}
    const params = new URLSearchParams(window.location.search);
    const next = params.get("next");
    window.location.href = next && next.startsWith("/") ? next : "/";
  } catch (err) {
    showError(err.message);
    submitBtn.disabled = false;
    submitBtn.textContent = "Sign in";
  }
});
