"use strict";

const $ = (id) => document.getElementById(id);

async function boot() {
  let s;
  try {
    const resp = await fetch("/api/session");
    if (!resp.ok) throw new Error("Service error: " + resp.status);
    s = await resp.json();
  } catch (e) {
    const el = $("login-error");
    el.textContent = String(e.message || e);
    el.hidden = false;
    return;
  }
  if (s && s.authenticated) {
    window.location.replace("/");
    return;
  }
  $("setup-hint").hidden = !(s && s.setup_required);
}

document.addEventListener("DOMContentLoaded", () => {
  $("show-password").addEventListener("change", () => {
    $("password").type = $("show-password").checked ? "text" : "password";
  });
  $("password").addEventListener("keyup", (e) => {
    let caps = false;
    try {
      caps = e.getModifierState && e.getModifierState("CapsLock");
    } catch (err) { /* older browsers: no warning */ }
    $("caps-hint").hidden = !caps;
  });
  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("login-form").querySelector('button[type="submit"]');
    if (btn) btn.disabled = true;
    try {
      const resp = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: $("password").value }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.error || ("Login failed: " + resp.status));
      window.location.replace("/");
    } catch (err) {
      const el = $("login-error");
      el.textContent = String(err.message || err);
      el.hidden = false;
    } finally {
      if (btn) btn.disabled = false;
    }
  });
  boot();
});
