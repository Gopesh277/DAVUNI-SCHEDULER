const form = document.getElementById("loginForm");
const errorBox = document.getElementById("loginError");
const submitBtn = document.getElementById("loginSubmit");

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

  const username = document.getElementById("username").value;
  const password = document.getElementById("password").value;

  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      let msg = "Invalid username or password.";
      try { const j = await res.json(); msg = j.detail || msg; } catch (e) {}
      throw new Error(msg);
    }
    const params = new URLSearchParams(window.location.search);
    const next = params.get("next");
    window.location.href = next && next.startsWith("/") ? next : "/";
  } catch (err) {
    showError(err.message);
    submitBtn.disabled = false;
    submitBtn.textContent = "Sign in";
  }
});
