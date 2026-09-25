"use strict";
let csrf = "";
let currentTab = "recordings";

const $ = (id) => document.getElementById(id);

let toastTimer = null;

function obj(v) {
  return v && typeof v === "object" && !Array.isArray(v) ? v : {};
}

function arr(v) {
  return Array.isArray(v) ? v : [];
}
function toast(msg, isError) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.toggle("error", !!isError);
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 5000);
}

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (csrf && opts.method && opts.method !== "GET") {
    headers["X-CSRF-Token"] = csrf;
  }
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  let resp;
  try {
    resp = await fetch(path, Object.assign({}, opts, { headers }));
  } catch (e) {
    showOffline();
    throw new Error("Cannot reach the service");
  }
  hideOffline();
  if (resp.status === 401) {
    window.location.replace("/");
    throw new Error("Session expired, login again");
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    // Ops endpoints answer {message}, PATCH endpoints {errors}: surface
    // the server detail instead of a bare status code.
    const detail = (data && (data.error || data.message)) ||
      (data && data.errors ? JSON.stringify(data.errors) : null);
    const err = new Error(detail || ("Request failed: " + resp.status));
    err.payload = data;
    throw err;
  }
  return data;
}

function showOffline() {
  $("offline-banner").hidden = false;
}

function hideOffline() {
  $("offline-banner").hidden = true;
}

let settingsDirty = false;

function showApp() {
  $("nav").hidden = false;
  $("logout").hidden = false;
  switchTab("recordings");
  layoutStage();
  loadStatus();
  loadChannels();
  loadSettings();
  loadRecordings();
  fetchUpdateChip();
}

function activatable(el, fn) {
  el.addEventListener("click", fn);
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fn();
    }
  });
}

function checkPlayerSession() {
  fetch("/api/session")
    .then((resp) => {
      if (!resp.ok) throw new Error("Session check failed: " + resp.status);
      return resp.json();
    })
    .then((s) => {
      if (s && !s.authenticated) window.location.replace("/");
      else if (s) toast("Cannot play this file", true);
      else toast("Cannot play this file", true);
    })
    .catch(() => toast("Cannot play this file", true));
}

function switchTab(name) {
  if (currentTab === "settings" && name !== "settings" && settingsDirty) {
    if (!window.confirm("Discard unsaved settings changes?")) return;
    settingsDirty = false;
    loadSettings();
  }
  currentTab = name;
  if (name === "recordings") closePlayer();
  document.querySelectorAll("#nav button[data-tab]").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  for (const id of ["status", "channels", "settings", "recordings", "events"]) {
    $("tab-" + id).hidden = id !== name;
  }
  if (name === "status") loadStatus();
  if (name === "channels") loadChannels();
  if (name === "recordings") loadRecordings();
  if (name === "events") loadEvents();
}

async function fetchUpdateChip() {
  const chip = $("update-chip");
  try {
    const r = await api("/api/update", {});
    const first = String(r.message || "").split("\n")[0];
    chip.hidden = false;
    if (first.startsWith("✅")) {
      chip.textContent = "Up to date";
      chip.className = "pill done";
    } else if (first.startsWith("📦")) {
      chip.textContent = "Update available";
      chip.className = "pill live";
    } else {
      chip.textContent = "Updates";
      chip.className = "pill";
    }
  } catch (e) {
    chip.hidden = true;
  }
}

function userWorkingControls() {
  const ae = document.activeElement;
  return ae && (ae.tagName === "SELECT" || ae.tagName === "INPUT" || ae.tagName === "TEXTAREA");
}

setInterval(() => {
  if (document.hidden) return;
  guarded("status", loadStatusQuiet);
  // Never rebuild a tab while the user works its controls: a rebuilt
  // channel dropdown would close mid-tap and drop the selection.
  if (userWorkingControls()) return;
  if (currentTab === "channels") guarded("channels", loadChannels);
  if (currentTab === "recordings") guarded("recordings", loadRecordings);
  if (currentTab === "events") guarded("events", loadEvents);
}, 30000);

const inFlight = {};
function guarded(name, fn) {
  if (inFlight[name]) return;
  inFlight[name] = true;
  Promise.resolve()
    .then(fn)
    .catch(() => {})
    .finally(() => {
      inFlight[name] = false;
    });
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  guarded("status", loadStatusQuiet);
  if (userWorkingControls()) return;
  if (currentTab === "recordings") guarded("recordings", loadRecordings);
  if (currentTab === "channels") guarded("channels", loadChannels);
  if (currentTab === "events") guarded("events", loadEvents);
});

async function loadEvents() {
  try {
    const data = obj(await api("/api/events?limit=100"));
    data.events = arr(data.events);
    const list = $("events-list");
    list.textContent = "";
    if (!data.events.length) {
      const li = document.createElement("li");
      li.className = "event-item muted";
      li.textContent = "No events yet.";
      list.appendChild(li);
      return;
    }
    for (const e of data.events) {
      const li = document.createElement("li");
      li.className = "event-item";
      const head = document.createElement("div");
      head.className = "event-head";
      const pill = document.createElement("span");
      pill.className = "pill " + (e.kind === "live" ? "live" : "done");
      pill.textContent = e.kind;
      head.appendChild(pill);
      if (e.channel) {
        const ch = document.createElement("span");
        ch.textContent = e.channel;
        head.appendChild(ch);
      }
      const time = document.createElement("span");
      time.className = "event-time";
      time.textContent = timeAgo(e.ts);
      head.appendChild(time);
      li.appendChild(head);
      const body = document.createElement("div");
      body.className = "event-text";
      body.textContent = e.text;
      li.appendChild(body);
      list.appendChild(li);
    }
  } catch (e) {
    toast(String(e.message || e), true);
  }
}

async function boot() {
  try {
    const s = await api("/api/session");
    if (s.authenticated) {
      csrf = s.csrf || "";
      showApp();
    } else {
      window.location.replace("/");
    }
  } catch (e) {
    toast(String(e.message || e), true);
  }
}

function kvCard(title, rows) {
  const card = document.createElement("div");
  card.className = "card";
  const h = document.createElement("h3");
  h.textContent = title;
  card.appendChild(h);
  const dl = document.createElement("dl");
  for (const [k, v] of rows) {
    const wrap = document.createElement("div");
    wrap.className = "kv";
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v;
    wrap.appendChild(dt);
    wrap.appendChild(dd);
    dl.appendChild(wrap);
  }
  card.appendChild(dl);
  return card;
}

function fmtGB(n) {
  return n === undefined || n === null ? "unknown" : Number(n).toFixed(1) + " GB";
}

async function loadStatus(quiet) {
  try {
    const raw = await api("/api/status");
    const s = Object.assign(
      { channels: [], recording: [], recordings_now: [] },
      raw
    );
    s.channels = arr(s.channels);
    s.recording = arr(s.recording);
    s.recordings_now = arr(s.recordings_now);
    s.endpoint = obj(s.endpoint);
    s.kick_webhook = obj(s.kick_webhook);
    s.mtproto = obj(s.mtproto);
    s.disk = obj(s.disk);
    s.update_check = obj(s.update_check);
    const host = kvCard("Service", [
      ["Version", s.version],
      ["Channels", String(s.channels.length)],
      ["Monitor every", s.monitoring_interval_s + "s"],
      ["Telegram bot", s.telegram_enabled ? "on" : "off"],
    ]);
    const access = kvCard("Access", [
      ["Endpoint", s.endpoint.enabled ? "on (" + s.endpoint.tunnel + ")" : "off"],
      ["Public URL", s.endpoint.public_url || "none"],
      ["Kick webhook", s.kick_webhook.enabled ? "on" : "off"],
      ["MTProto upload", s.mtproto.enabled ? "on" : "off"],
    ]);
    const disk = kvCard("Disk", [
      ["Archive", fmtGB(s.disk.archive_gb)],
      ["Free", fmtGB(s.disk.free_gb)],
      ["Cap", s.disk.cap_gb > 0 ? fmtGB(s.disk.cap_gb) : "off"],
      ["Update check", s.update_check.enabled ? "every " + s.update_check.interval_hours + "h" : "off"],
    ]);
    const meter = document.createElement("div");
    meter.className = "meter";
    const fill = document.createElement("div");
    const cap = s.disk.cap_gb > 0 ? s.disk.cap_gb : s.disk.total_fs_gb;
    const ratio = cap > 0 ? s.disk.archive_gb / cap : 0;
    fill.style.width = Math.min(100, Math.max(0, ratio * 100)).toFixed(1) + "%";
    if (ratio >= 1) meter.classList.add("over");
    meter.appendChild(fill);
    disk.appendChild(meter);
    const meterCaption = document.createElement("div");
    meterCaption.className = "muted";
    if (s.disk.cap_gb > 0) {
      meterCaption.textContent = fmtGB(s.disk.archive_gb) + " of " + fmtGB(s.disk.cap_gb) + " cap";
    } else if (s.disk.usage_ok && s.disk.total_fs_gb > 0) {
      meterCaption.textContent = fmtGB(s.disk.archive_gb) + " of " + fmtGB(s.disk.total_fs_gb) + " disk";
    } else {
      meterCaption.textContent = fmtGB(s.disk.archive_gb) + " archive, disk usage unknown";
    }
    disk.appendChild(meterCaption);
    const cards = $("status-cards");
    cards.textContent = "";
    cards.append(host, access, disk);
    const now = $("rec-now");
    now.textContent = "";
    if (!s.recording.length) {
      const span = document.createElement("span");
      span.className = "muted";
      span.textContent = "None";
      now.appendChild(span);
    }
    for (const ch of s.recording) {
      const pill = document.createElement("span");
      pill.className = "pill live";
      pill.textContent = "REC " + ch;
      now.appendChild(pill);
      now.appendChild(document.createTextNode(" "));
    }
    $("status-updated").textContent = "Status updated " + new Date().toLocaleTimeString();
  } catch (e) {
    if (!quiet) toast(String(e.message || e), true);
  }
}

function loadStatusQuiet() {
  return loadStatus(true).catch(() => {});
}

function actionBtn(label, cls, fn) {
  const b = document.createElement("button");
  b.textContent = label;
  if (cls) b.className = cls;
  b.addEventListener("click", () => fn().catch((e) => toast(String(e.message || e), true)));
  return b;
}

async function loadChannels() {
  try {
    const [data, feed] = await Promise.all([
      api("/api/channels"),
      api("/api/events?limit=200").catch(() => ({})),
    ]);
    const lastByChannel = new Map();
    for (const e of feed.events || []) {
      if (e.channel && !lastByChannel.has(e.channel)) lastByChannel.set(e.channel, e);
    }
    const list = $("channels-list");
    list.textContent = "";
    for (const ch of data.channels) {
      const li = document.createElement("li");
      li.className = "channel-card";
      const head = document.createElement("div");
      head.className = "channel-head";
      const name = document.createElement("span");
      name.className = "channel-name";
      name.textContent = ch.channel;
      head.appendChild(name);
      const pill = document.createElement("span");
      pill.className = "pill " + (ch.recording ? "live" : "done");
      pill.textContent = ch.recording ? "REC" : "idle";
      head.appendChild(pill);
      li.appendChild(head);
      const last = lastByChannel.get(ch.channel);
      const activity = document.createElement("div");
      activity.className = "rec-meta";
      activity.textContent = last
        ? "Last activity: " + last.kind + " · " + timeAgo(last.ts)
        : "Last activity: none yet";
      li.appendChild(activity);
      const modeField = document.createElement("label");
      modeField.className = "field";
      const modeCaption = document.createElement("span");
      modeCaption.textContent = "Output mode";
      modeField.appendChild(modeCaption);
      const modeSel = document.createElement("select");
      for (const m of ["disk", "youtube", "both", "default"]) {
        const o = document.createElement("option");
        o.value = m;
        o.textContent = m === "default" ? "global" : m;
        modeSel.appendChild(o);
      }
      modeSel.value = ch.output_mode_override || "default";
      modeSel.setAttribute("aria-label", "Output mode for " + ch.channel);
      modeSel.addEventListener("change", async () => {
        try {
          await api("/api/channels/" + encodeURIComponent(ch.channel), {
            method: "PATCH",
            body: JSON.stringify({ output_mode: modeSel.value }),
          });
          toast("Mode saved");
        } catch (e) {
          toast(String(e.message || e), true);
        }
        loadChannels();
      });
      modeField.appendChild(modeSel);
      li.appendChild(modeField);
      const qField = document.createElement("label");
      qField.className = "field";
      const qCaption = document.createElement("span");
      qCaption.textContent = "Quality (global: " + ch.quality + ")";
      qField.appendChild(qCaption);
      const qInput = document.createElement("input");
      qInput.value = ch.quality_override || "";
      qInput.placeholder = ch.quality;
      qInput.size = 8;
      qInput.setAttribute("aria-label", "Quality for " + ch.channel);
      qInput.addEventListener("change", async () => {
        try {
          const v = qInput.value.trim() || "default";
          await api("/api/channels/" + encodeURIComponent(ch.channel), {
            method: "PATCH",
            body: JSON.stringify({ quality: v }),
          });
          toast("Quality saved");
        } catch (e) {
          toast(String(e.message || e), true);
        }
        loadChannels();
      });
      qField.appendChild(qInput);
      li.appendChild(qField);
      const hField = document.createElement("label");
      hField.className = "field";
      const hCaption = document.createElement("span");
      hCaption.textContent = "Hold delay, seconds (global: " + ch.youtube_hold_seconds + ")";
      hField.appendChild(hCaption);
      const holdInput = document.createElement("input");
      holdInput.value = ch.youtube_hold_seconds_override !== null && ch.youtube_hold_seconds_override !== undefined
        ? String(ch.youtube_hold_seconds_override)
        : "";
      holdInput.placeholder = String(ch.youtube_hold_seconds);
      holdInput.size = 4;
      holdInput.inputMode = "numeric";
      holdInput.setAttribute("aria-label", "Hold delay for " + ch.channel);
      holdInput.addEventListener("change", async () => {
        try {
          const raw = holdInput.value.trim();
          if (raw !== "" && (!Number.isFinite(Number(raw)) || !Number.isInteger(Number(raw)) || Number(raw) < 0)) {
            toast("Hold delay must be a whole number of seconds of 0 or more", true);
            loadChannels();
            return;
          }
          const v = raw === "" ? "default" : Number(raw);
          await api("/api/channels/" + encodeURIComponent(ch.channel), {
            method: "PATCH",
            body: JSON.stringify({ youtube_hold_seconds: v }),
          });
          toast("Hold delay saved");
        } catch (e) {
          toast(String(e.message || e), true);
        }
        loadChannels();
      });
      hField.appendChild(holdInput);
      li.appendChild(hField);
      const wrap = document.createElement("div");
      wrap.className = "row-actions";
      wrap.appendChild(actionBtn("Remove", "danger", async () => {
        if (!window.confirm("Remove " + ch.channel + "?")) return;
        await api("/api/channels/" + encodeURIComponent(ch.channel), { method: "DELETE" });
        toast("Channel removed");
        loadChannels();
        loadStatus();
      }));
      li.appendChild(wrap);
      list.appendChild(li);
    }
    if (!data.channels.length) {
      const li = document.createElement("li");
      li.className = "channel-card muted";
      li.textContent = "No channels monitored.";
      list.appendChild(li);
    }
  } catch (e) {
    toast(String(e.message || e), true);
  }
}

const SETTING_DEFS = [
  { key: "output_mode", label: "Output mode", hint: "Where new recordings go", type: "select",
    options: [["Disk", "disk"], ["YouTube", "youtube"], ["Both", "both"]] },
  { key: "preferred_quality", label: "Preferred quality", hint: "Audio-only records sound and forces disk output", type: "select",
    options: [["Best", "best"], ["1080p", "1080p"], ["720p", "720p"], ["480p", "480p"], ["360p", "360p"], ["Audio only", "audio_only"]] },
  { key: "retention_days", label: "Retention", hint: "Delete recordings older than this", type: "preset-number",
    presets: [["Off", 0], ["1 day", 1], ["3 days", 3], ["7 days", 7], ["14 days", 14], ["30 days", 30]], unit: "days" },
  { key: "max_concurrent_recordings", label: "Max recordings", hint: "At the same time", type: "preset-number",
    presets: [["Unlimited", 0], ["1", 1], ["2", 2], ["3", 3], ["5", 5]], unit: "" },
  { key: "max_concurrent_youtube_streams", label: "Max restreams", hint: "At the same time", type: "preset-number",
    presets: [["Unlimited", 0], ["1", 1], ["2", 2], ["3", 3], ["5", 5]], unit: "" },
  { key: "record_chat", label: "Twitch chat", hint: "Save live chat with the video", type: "bool" },
  { key: "kick_record_chat", label: "Kick chat", hint: "Save live chat with the video", type: "bool" },
  { key: "disk_max_total_gb", label: "Disk cap", hint: "Archive size limit", type: "preset-number",
    presets: [["Off", 0], ["25 GB", 25], ["50 GB", 50], ["100 GB", 100], ["200 GB", 200]], unit: "GB" },
  { key: "disk_delete_oldest", label: "Delete oldest when full", hint: "Otherwise new recordings stop", type: "bool" },
];

function settingControl(def, current) {
  const lab = document.createElement("label");
  lab.className = "field";
  const caption = document.createElement("span");
  caption.textContent = def.label + " - " + def.hint;
  lab.appendChild(caption);
  let get;
  if (def.type === "select") {
    const sel = document.createElement("select");
    sel.id = "set-" + def.key;
    for (const [label, value] of def.options) {
      const o = document.createElement("option");
      o.value = value;
      o.textContent = label;
      sel.appendChild(o);
    }
    sel.value = String(current);
    lab.appendChild(sel);
    get = () => sel.value;
  } else if (def.type === "bool") {
    const box = document.createElement("input");
    box.type = "checkbox";
    box.id = "set-" + def.key;
    box.checked = current === true;
    lab.appendChild(box);
    get = () => box.checked;
  } else {
    const sel = document.createElement("select");
    for (const [label, value] of def.presets) {
      const o = document.createElement("option");
      o.value = String(value);
      o.textContent = label;
      sel.appendChild(o);
    }
    const custom = document.createElement("option");
    custom.value = "custom";
    custom.textContent = "Custom…";
    sel.appendChild(custom);
    const num = document.createElement("input");
    num.type = "number";
    num.min = "0";
    num.id = "set-" + def.key;
    const match = String(current);
    if (def.presets.some(([, v]) => String(v) === match)) {
      sel.value = match;
      num.hidden = true;
    } else {
      sel.value = "custom";
      num.value = match;
    }
    sel.setAttribute("aria-label", def.label + " preset");
    sel.addEventListener("change", () => {
      num.hidden = sel.value !== "custom";
    });
    lab.appendChild(sel);
    lab.appendChild(num);
    get = () => {
      if (sel.value !== "custom") return Number(sel.value);
      const n = Number(num.value);
      if (num.value.trim() === "" || !Number.isInteger(n) || n < 0) {
        throw new Error(def.label + " needs a whole number of 0 or more");
      }
      return n;
    };
  }
  return { lab, get };
}

const settingGetters = {};

async function loadSettings() {
  try {
    const s = await api("/api/settings");
    s.disk = obj(s.disk);
    const form = $("settings-form");
    form.textContent = "";
    const flat = {
      output_mode: s.output_mode,
      preferred_quality: s.preferred_quality,
      retention_days: s.retention_days,
      max_concurrent_recordings: s.max_concurrent_recordings,
      max_concurrent_youtube_streams: s.max_concurrent_youtube_streams,
      record_chat: s.record_chat,
      kick_record_chat: s.kick_record_chat,
      disk_max_total_gb: s.disk.max_total_gb,
      disk_delete_oldest: s.disk.delete_oldest,
    };
    for (const def of SETTING_DEFS) {
      const { lab, get } = settingControl(def, flat[def.key]);
      settingGetters[def.key] = get;
      form.appendChild(lab);
    }
  } catch (e) {
    toast(String(e.message || e), true);
  }
}

function fmtSize(n) {
  if (n >= 1073741824) return (n / 1073741824).toFixed(1) + " GB";
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return Math.floor(n / 1024) + " KB";
  return (n || 0) + " B";
}

function timeAgo(ts) {
  const diff = Math.max(0, Date.now() / 1000 - ts);
  if (diff < 60) return "just now";
  if (diff < 3600) return Math.floor(diff / 60) + " min ago";
  if (diff < 86400) return Math.floor(diff / 3600) + " h ago";
  return Math.floor(diff / 86400) + " d ago";
}

function layoutStage() {
  const playing = !$("player-wrap").hidden;
  document.querySelector(".rec-stage").classList.toggle("no-player", !playing);
  const toggle = $("toggle-list");
  if (toggle) toggle.hidden = !playing;
  if (!playing) setListHidden(false);
}

function stickPlayer() {
  const header = document.querySelector("header.top");
  const wrap = $("player-wrap");
  if (header && !wrap.hidden) {
    const top = header.offsetHeight + 8 + "px";
    wrap.style.top = top;
    const pane = document.querySelector(".rec-playerpane");
    if (pane) pane.style.top = top;
  }
}

let seekFlashTimer = null;
function flashSeek(text) {
  const el = $("seek-flash");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(seekFlashTimer);
  seekFlashTimer = setTimeout(() => {
    el.hidden = true;
  }, 600);
}

function initSeekTap() {
  const player = $("player");
  player.addEventListener("error", checkPlayerSession);
  let lastTap = 0;
  const seek = (clientX) => {
    const rect = player.getBoundingClientRect();
    const delta = clientX - rect.left < rect.width / 2 ? -10 : 10;
    const max = Number.isFinite(player.duration) ? player.duration : Infinity;
    player.currentTime = Math.min(Math.max(0, player.currentTime + delta), max);
    flashSeek((delta > 0 ? "+" : "") + delta + "s");
  };
  player.addEventListener("touchend", (e) => {
    const now = Date.now();
    const touch = e.changedTouches[0];
    if (now - lastTap < 350) {
      e.preventDefault();
      seek(touch.clientX);
      lastTap = 0;
    } else {
      lastTap = now;
    }
  }, { passive: false });
  player.addEventListener("dblclick", (e) => {
    e.preventDefault();
    seek(e.clientX);
  });
}

function channelOf(id) {
  const parts = id.split("/");
  return parts.length >= 2 ? parts[0] + ":" + parts[1] : parts[0];
}

let selectedRecs = new Set();
let playQueue = [];
let currentPlayId = null;
let lastSavedAt = -1;
const recCards = new Map();
const recGroups = new Map();

const POS_KEY = "sa:positions";
const POS_MAX = 200;

function loadPositions() {
  try {
    return JSON.parse(localStorage.getItem(POS_KEY) || "{}");
  } catch (e) {
    return {};
  }
}

function savePosition(id, t, d) {
  if (!id || !(t > 10)) return;
  const all = loadPositions();
  delete all[id];
  all[id] = { t: Math.floor(t), d: Math.floor(d || 0) };
  const keys = Object.keys(all);
  for (const k of keys.slice(0, Math.max(0, keys.length - POS_MAX))) delete all[k];
  try {
    localStorage.setItem(POS_KEY, JSON.stringify(all));
  } catch (e) {}
}

function savedPosition(id, duration) {
  const all = loadPositions();
  const p = all[id];
  if (!p || !(p.t > 10)) return 0;
  const end = Number.isFinite(duration) && duration > 0 ? duration : p.d || 0;
  if (end > 0 && p.t > end - 30) return 0;
  return p.t;
}

function forgetPosition(id) {
  if (!id) return;
  const all = loadPositions();
  if (id in all) {
    delete all[id];
    try {
      localStorage.setItem(POS_KEY, JSON.stringify(all));
    } catch (e) {}
  }
}

let playToken = 0;

async function playRecording(entry, collapse = true) {
  const my = ++playToken;
  const player = $("player");
  currentPlayId = entry.id;
  updatePlayerNav();
  const wrapEl = $("player-wrap");
  wrapEl.hidden = false;
  if (collapse) setListHidden(true);
  layoutStage();
  fitPlayer($("player-wrap"), $("player"));
  stickPlayer();
  wrapEl.scrollIntoView({ block: "nearest" });
  loadChat(entry.id, entry.name).catch(() => {});
  player.src = "/api/recordings/stream?id=" + encodeURIComponent(entry.id);
  const stored = loadPositions()[entry.id];
  if (stored && stored.t > 10) {
    // Metadata is not loaded yet, so an immediate seek is ignored.
    // Defer it until the stream reports its length. The guard reruns
    // then: a rapid switch or close retires this listener silently.
    player.addEventListener(
      "loadedmetadata",
      () => {
        if (my !== playToken || currentPlayId !== entry.id) return;
        try {
          player.currentTime = savedPosition(entry.id, player.duration);
        } catch (e) {}
      },
      { once: true }
    );
  }
  try {
    await player.play();
  } catch (e) {
    if (my !== playToken) return;
    toast("Cannot play this file: " + String((e && e.message) || e || "unknown error"), true);
  }
}

function updatePlayerNav() {
  const idx = playQueue.findIndex((e) => e.id === currentPlayId);
  $("player-prev").disabled = idx <= 0;
  $("player-next").disabled = idx < 0 || idx >= playQueue.length - 1;
}

function closePlayer() {
  if ($("player-wrap").hidden) return;
  const player = $("player");
  playToken++;
  savePosition(currentPlayId, player.currentTime, player.duration);
  lastSavedAt = -1;
  player.pause();
  player.playbackRate = 1;
  $("player-speed").value = "1";
  $("player-speed").querySelector("option[data-custom]")?.remove();
  player.removeAttribute("src");
  player.load();
  $("player-wrap").hidden = true;
  $("chat-panel").hidden = true;
  document.querySelector(".rec-stage").classList.remove("no-chat");
  chatMessages = [];
  currentPlayId = null;
  updatePlayerNav();
  setListHidden(false);
  layoutStage();
}

function stepVideo(delta) {
  const idx = playQueue.findIndex((e) => e.id === currentPlayId);
  const next = playQueue[idx + delta];
  if (next) playRecording(next, false);
}

function setRate(rate) {
  const r = Math.min(4, Math.max(0.25, Math.round(rate * 10) / 10));
  $("player").playbackRate = r;
  const speedSel = $("player-speed");
  const exact = [...speedSel.options].find((o) => Number(o.value) === r);
  if (exact) {
    speedSel.value = exact.value;
  } else {
    let custom = speedSel.querySelector("option[data-custom]");
    if (!custom) {
      custom = document.createElement("option");
      custom.dataset.custom = "1";
      speedSel.appendChild(custom);
    }
    custom.value = String(r);
    custom.textContent = r.toFixed(1) + "×";
    speedSel.value = String(r);
  }
}

function setListHidden(hidden) {
  if (hidden && $("player-wrap").hidden) return;
  document.querySelector(".rec-stage").classList.toggle("list-hidden", hidden);
  dockToggle(hidden);
  alignChat();
}

function dockToggle(collapsed) {
  const btn = $("toggle-list");
  if (!btn) return;
  if (collapsed) {
    const bar = document.querySelector(".player-bar");
    if (bar && btn.parentElement !== bar) bar.prepend(btn);
  } else {
    const toolbar = document.querySelector(".rec-listpane .toolbar");
    if (toolbar && btn.parentElement !== toolbar) toolbar.prepend(btn);
  }
}

function alignChat() {
  const panel = $("chat-panel");
  const wrap = $("player-wrap");
  const player = $("player");
  const card = panel.querySelector(".card");
  if (!card) return;
  if (panel.hidden || wrap.hidden || window.matchMedia("(max-width: 1100px)").matches) {
    card.style.maxHeight = "";
    return;
  }
  fitPlayer(wrap, player);
  const header = document.querySelector("header.top");
  if (header) panel.style.top = header.offsetHeight + 8 + "px";
  card.style.maxHeight = wrap.getBoundingClientRect().height + "px";
}

function fitPlayer(wrap, player) {
  if (window.matchMedia("(max-width: 1100px)").matches) {
    if (player.style.width) player.style.width = "";
    return;
  }
  const pane = wrap.parentElement;
  const colW = pane ? pane.clientWidth : player.clientWidth;
  const ratio =
    player.videoWidth > 0 && player.videoHeight > 0 ? player.videoWidth / player.videoHeight : 16 / 9;
  const naturalH = colW / ratio;
  const wrapRect = wrap.getBoundingClientRect();
  const playerRect = player.getBoundingClientRect();
  const chrome = Math.max(0, wrapRect.height - playerRect.height);
  const targetH = window.innerHeight - wrapRect.top - chrome - 12;
  if (targetH < naturalH - 1) {
    const w = Math.max(0, targetH * ratio);
    if (Math.abs(player.clientWidth - w) > 1) player.style.width = w + "px";
  } else if (player.style.width) {
    player.style.width = "";
  }
}

let playerSizeObs = null;
function watchPlayerSize() {
  if (playerSizeObs || typeof ResizeObserver === "undefined") return;
  const wrap = $("player-wrap");
  const player = $("player");
  if (!wrap || !player) return;
  playerSizeObs = new ResizeObserver(() => alignChat());
  playerSizeObs.observe(wrap);
  playerSizeObs.observe(player);
}

let chatMessages = [];
let lastChatSecond = -1;

async function loadChat(id, name) {
  const panel = $("chat-panel");
  const log = $("chat-log");
  const note = $("chat-note");
  $("chat-title").textContent = "Chat — " + name;
  log.textContent = "";
  note.hidden = true;
  panel.hidden = false;
  chatMessages = [];
  lastChatSecond = -1;
  try {
    const data = await api("/api/chat?id=" + encodeURIComponent(id));
    chatMessages = data.messages || [];
    if (!chatMessages.length) {
      panel.hidden = true;
      document.querySelector(".rec-stage").classList.add("no-chat");
      return;
    }
    document.querySelector(".rec-stage").classList.remove("no-chat");
    for (const m of chatMessages) {
      const div = document.createElement("div");
      div.className = "chat-msg future";
      div.setAttribute("role", "button");
      div.setAttribute("tabindex", "0");
      div.setAttribute("title", "Jump to " + Math.floor(m.t) + "s");
      const activate = () => {
        const player = $("player");
        player.currentTime = Math.max(0, m.t - 1);
        player.play().catch(() => {});
      };
      div.addEventListener("click", activate);
      div.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          activate();
        }
      });
      const user = document.createElement("span");
      user.className = "user";
      user.textContent = m.user + " ";
      div.appendChild(user);
      appendChatText(div, m.text, m.emotes);
      log.appendChild(div);
    }
    if (data.truncated) {
      note.textContent = "Showing first " + chatMessages.length + " messages.";
      note.hidden = false;
    }
  } catch (e) {
    panel.hidden = true;
    document.querySelector(".rec-stage").classList.add("no-chat");
  }
  alignChat();
}

function appendChatText(div, text, emotes) {
  if (!emotes || !emotes.length) {
    div.appendChild(document.createTextNode(text));
    return;
  }
  const spans = [...emotes].sort((a, b) => a.start - b.start);
  let pos = 0;
  for (const s of spans) {
    if (s.start < pos || s.start >= text.length) continue;
    const end = Math.min(s.end, text.length);
    if (end <= s.start || typeof s.src !== "string" || !/^https:\/\/|^data:image\//.test(s.src)) continue;
    if (s.start > pos) div.appendChild(document.createTextNode(text.slice(pos, s.start)));
    const img = document.createElement("img");
    img.className = "chat-emote";
    img.src = s.src;
    img.alt = text.slice(s.start, end);
    img.loading = "lazy";
    div.appendChild(img);
    pos = end;
  }
  if (pos < text.length) div.appendChild(document.createTextNode(text.slice(pos)));
}

function syncChat() {
  if (!chatMessages.length) return;
  const player = $("player");
  const t = player.currentTime;
  if (Math.floor(t) === lastChatSecond) return;
  lastChatSecond = Math.floor(t);
  let lo = 0;
  let hi = chatMessages.length - 1;
  let at = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (chatMessages[mid].t <= t) {
      at = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  const log = $("chat-log");
  const rows = log.children;
  for (let i = 0; i < rows.length && i < chatMessages.length; i++) {
    const past = i <= at;
    rows[i].classList.toggle("future", !past);
    rows[i].classList.toggle("now", i === at);
  }
  if (at >= 0 && $("chat-follow").checked && rows[at]) {
    if (window.matchMedia("(max-width: 1100px)").matches) {
      rows[at].scrollIntoView({ block: "nearest" });
      return;
    }
    const row = rows[at];
    const rowRect = row.getBoundingClientRect();
    const logRect = log.getBoundingClientRect();
    if (rowRect.top < logRect.top) log.scrollTop -= logRect.top - rowRect.top;
    else if (rowRect.bottom > logRect.bottom) log.scrollTop += rowRect.bottom - logRect.bottom;
  }
}

function buildRecCard(r, player) {
  const li = document.createElement("li");
  li.className = "rec-card";
  li.dataset.live = r.live ? "1" : "";
  const top = document.createElement("div");
  top.className = "rec-top";
  if (!r.live) {
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "rec-select";
    box.checked = selectedRecs.has(r.id);
    box.setAttribute("aria-label", "Select " + r.name);
    box.addEventListener("change", () => {
      if (box.checked) selectedRecs.add(r.id);
      else selectedRecs.delete(r.id);
      updateBulkButton();
    });
    top.appendChild(box);
  }
  const name = document.createElement("div");
  name.className = "rec-name";
  name.textContent = r.name;
  name.setAttribute("title", "Play");
  top.appendChild(name);
  li.appendChild(top);
  if (!r.live) {
    const playIt = () => playRecording({ id: r.id, name: r.name });
    name.classList.add("playable");
    name.addEventListener("click", playIt);
    const thumb = document.createElement("img");
    thumb.className = "rec-thumb";
    thumb.loading = "lazy";
    thumb.src = "/api/recordings/thumb?id=" + encodeURIComponent(r.id);
    thumb.alt = "";
    thumb.setAttribute("title", "Play");
    thumb.addEventListener("click", playIt);
    thumb.addEventListener("error", () => thumb.remove());
    li.appendChild(thumb);
  }
  const meta = document.createElement("div");
  meta.className = "rec-meta";
  meta.textContent = fmtSize(r.size) + " · " + timeAgo(r.mtime);
  li.appendChild(meta);
  const wrap = document.createElement("div");
  wrap.className = "row-actions";
  if (r.live) {
    const pill = document.createElement("span");
    pill.className = "pill live";
    pill.textContent = "REC";
    wrap.appendChild(pill);
    } else {
      if (r.playable) {
        wrap.appendChild(actionBtn("Play", "", () => playRecording({ id: r.id, name: r.name }).catch(() => {})));
      }
    const dl = document.createElement("a");
    dl.className = "btn";
    dl.href = "/api/recordings/stream?id=" + encodeURIComponent(r.id) + "&download=1";
    dl.textContent = "Download";
    wrap.appendChild(dl);
    wrap.appendChild(actionBtn("Delete", "danger", async () => {
      if (!window.confirm("Delete " + r.name + "?")) return;
      await api("/api/recordings?id=" + encodeURIComponent(r.id), { method: "DELETE" });
      forgetPosition(r.id);
      toast("Recording deleted");
      loadRecordings();
    }));
  }
  li.appendChild(wrap);
  return li;
}

function refreshRecCard(li, r, player) {
  if (!!li.dataset.live !== !!r.live) {
    const fresh = buildRecCard(r, player);
    li.replaceWith(fresh);
    recCards.set(r.id, fresh);
    return;
  }
  li.querySelector(":scope > .rec-meta").textContent = fmtSize(r.size) + " · " + timeAgo(r.mtime);
}

function buildChannelGroup() {
  const group = document.createElement("li");
  group.className = "channel-group";
  const head = document.createElement("div");
  head.className = "channel-head";
  const name = document.createElement("span");
  name.className = "channel-name";
  head.appendChild(name);
  group.appendChild(head);
  const filesList = document.createElement("ul");
  filesList.className = "files-list";
  group.appendChild(filesList);
  return group;
}

function refreshChannelGroup(group, ch, files, player, wanted) {
  const total = files.reduce((n, f) => n + f.size, 0);
  group.querySelector(":scope > .channel-head > .channel-name").textContent =
    ch + " (" + files.length + " file" + (files.length === 1 ? "" : "s") + ", " + fmtSize(total) + ")";
  const head = group.querySelector(":scope > .channel-head");
  const hasPill = !!head.querySelector(":scope > .pill");
  if (files.some((f) => f.live) && !hasPill) {
    const pill = document.createElement("span");
    pill.className = "pill live";
    pill.textContent = "REC";
    head.appendChild(pill);
  } else if (!files.some((f) => f.live) && hasPill) {
    head.querySelector(":scope > .pill").remove();
  }
  const filesList = group.querySelector(":scope > ul.files-list");
  for (const r of files) {
    wanted.add(r.id);
    let li = recCards.get(r.id);
    if (!li) {
      li = buildRecCard(r, player);
      recCards.set(r.id, li);
    } else {
      refreshRecCard(li, r, player);
      li = recCards.get(r.id);
    }
    filesList.appendChild(li);
  }
}

async function loadRecordings() {
  try {
    const filter = $("rec-filter").value.trim().toLowerCase();
    const onlyChannel = $("rec-channel").value;
    const sort = $("rec-sort").value;
    const data = obj(await api("/api/recordings?limit=500"));
    data.recordings = arr(data.recordings);
    data.total = Number(data.total) || 0;
    const chanSel = $("rec-channel");
    while (chanSel.options.length > 1) chanSel.remove(1);
    for (const r of data.recordings) {
      const ch = channelOf(r.id);
      if (![...chanSel.options].some((o) => o.value === ch)) {
        const o = document.createElement("option");
        o.value = ch;
        o.textContent = ch;
        chanSel.appendChild(o);
      }
    }
    if (![...chanSel.options].some((o) => o.value === onlyChannel)) {
      chanSel.value = "";
    } else {
      chanSel.value = onlyChannel;
    }
    const activeChannel = chanSel.value;
    let files = data.recordings.filter((r) => {
      if (activeChannel && channelOf(r.id) !== activeChannel) return false;
      return !filter || r.name.toLowerCase().includes(filter);
    });
    if (sort === "oldest") files = [...files].reverse();
    else if (sort === "largest") files = [...files].sort((a, b) => b.size - a.size);
    playQueue = files.filter((r) => r.playable && !r.live).map((r) => ({ id: r.id, name: r.name }));
    updatePlayerNav();
    const list = $("rec-list");
    for (const id of [...selectedRecs]) {
      if (!data.recordings.some((r) => r.id === id)) selectedRecs.delete(id);
    }
    updateBulkButton();
    const player = $("player");
    const groups = new Map();
    for (const r of files) {
      const ch = channelOf(r.id);
      if (!groups.has(ch)) groups.set(ch, []);
      groups.get(ch).push(r);
    }
    let shown = 0;
    const wantedGroups = new Set();
    const wanted = new Set();
    for (const [ch, chFiles] of groups) {
      shown += chFiles.length;
      wantedGroups.add(ch);
      let group = recGroups.get(ch);
      if (!group) {
        group = buildChannelGroup();
        recGroups.set(ch, group);
      }
      list.appendChild(group);
      refreshChannelGroup(group, ch, chFiles, player, wanted);
    }
    for (const [ch, group] of [...recGroups]) {
      if (!wantedGroups.has(ch)) {
        group.remove();
        recGroups.delete(ch);
      }
    }
    for (const [id, li] of [...recCards]) {
      if (!wanted.has(id)) {
        li.remove();
        recCards.delete(id);
      }
    }
    list.querySelectorAll(":scope > li.rec-card.muted").forEach((li) => li.remove());
    const counter = $("rec-count");
    if (data.total > data.recordings.length) {
      counter.textContent = "Showing " + data.recordings.length + " of " + data.total + " recordings - narrow the filter to see the rest.";
      counter.hidden = false;
    } else {
      counter.hidden = true;
    }
    if (!shown) {
      const li = document.createElement("li");
      li.className = "rec-card muted";
      li.textContent = "No recordings match.";
      list.appendChild(li);
    }
  } catch (e) {
    toast(String(e.message || e), true);
  }
}

function updateBulkButton() {
  const btn = $("rec-delete-selected");
  btn.hidden = selectedRecs.size === 0;
  btn.textContent = "Delete selected (" + selectedRecs.size + ")";
}

const NOTIFY_KEY = "sa:notify";
const SEEN_KEY = "sa:events-seen";
let notifyOn = false;
try {
  notifyOn = localStorage.getItem(NOTIFY_KEY) === "1";
} catch (e) {}

function lastSeenTs() {
  try {
    return Number(localStorage.getItem(SEEN_KEY) || 0);
  } catch (e) {
    return 0;
  }
}

function markSeen(ts) {
  try {
    localStorage.setItem(SEEN_KEY, String(ts));
  } catch (e) {}
}

function paintNotifyButton() {
  const btn = $("notify-toggle");
  if (btn) btn.textContent = "Notify: " + (notifyOn ? "on" : "off");
}

async function pollEventAlerts() {
  if (!notifyOn) return;
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  let data;
  try {
    data = obj(await api("/api/events?limit=20"));
  } catch (e) {
    return;
  }
  const events = arr(data.events).filter((e) => e && (e.kind === "live" || e.kind === "offline"));
  if (!events.length) return;
  const seen = lastSeenTs();
  let max = seen;
  for (const e of events) {
    const ts = Number(e.ts) || 0;
    if (ts > max) max = ts;
    if (ts > seen) {
      try {
        new Notification(e.kind === "live" ? "Live" : "Stream ended", {
          body: (e.channel ? e.channel + ": " : "") + (e.text || ""),
        });
      } catch (err) {}
    }
  }
  markSeen(max);
}

async function deleteSelected() {
  const ids = [...selectedRecs];
  if (!ids.length) return;
  if (!window.confirm("Delete " + ids.length + " recording" + (ids.length === 1 ? "" : "s") + "?")) return;
  const results = await Promise.allSettled(
    ids.map((id) => api("/api/recordings?id=" + encodeURIComponent(id), { method: "DELETE" }))
  );
  let ok = 0;
  let firstFailure = null;
  ids.forEach((id, i) => {
    if (results[i].status === "fulfilled") {
      ok += 1;
      selectedRecs.delete(id);
      forgetPosition(id);
    } else if (!firstFailure) {
      const reason = results[i].reason;
      firstFailure = String((reason && reason.message) || reason || "unknown error");
    }
  });
  updateBulkButton();
  toast(
    "Deleted " + ok + " of " + ids.length + (firstFailure ? " - first failure: " + firstFailure : ""),
    ok < ids.length
  );
  loadRecordings();
}

function out(text) {
  $("ops-out").textContent = text;
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("#nav button[data-tab]").forEach((b) => {
    b.addEventListener("click", () => switchTab(b.dataset.tab));
  });
  activatable($("update-chip"), () => switchTab("settings"));
  $("logout").addEventListener("click", async () => {
    try {
      await api("/api/logout", { method: "POST" });
    } catch (e) {
      toast(String(e.message || e), true);
      return;
    }
    window.location.replace("/");
  });
  $("refresh-status").addEventListener("click", () => loadStatus());
  $("offline-retry").addEventListener("click", () => {
    hideOffline();
    loadStatusQuiet();
    if (currentTab === "channels") loadChannels();
    if (currentTab === "recordings") loadRecordings();
    if (currentTab === "events") loadEvents();
  });
  $("settings-form").addEventListener("input", () => {
    settingsDirty = true;
  });
  $("rec-sort").addEventListener("change", loadRecordings);
  $("rec-channel").addEventListener("change", loadRecordings);
  $("rec-delete-selected").addEventListener("click", () => {
    deleteSelected().catch((e) => toast(String(e.message || e), true));
  });
  $("add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/channels", {
        method: "POST",
        body: JSON.stringify({ channel: $("add-input").value }),
      });
      $("add-input").value = "";
      toast("Channel added");
      loadChannels();
      loadStatus();
    } catch (err) {
      toast(String(err.message || err), true);
    }
  });
  async function saveSettings() {
    let res;
    try {
      const payload = {};
      for (const def of SETTING_DEFS) {
        const get = settingGetters[def.key];
        if (!get) throw new Error("Settings are still loading");
        const value = get();
        if (typeof value === "number" && (!Number.isFinite(value) || value < 0)) {
          throw new Error(def.label + " must be a number of 0 or more");
        }
        payload[def.key] = value;
      }
      res = await api("/api/settings", { method: "PATCH", body: JSON.stringify(payload) });
    } catch (err) {
      // A 400/409 still carries per-key results in the payload: show
      // them and keep the edits for retry instead of reloading.
      const errors = err && err.payload && err.payload.errors;
      if (errors && Object.keys(errors).length) {
        toast("Some keys failed: " + JSON.stringify(errors), true);
        return;
      }
      toast(String((err && err.message) || err), true);
      return;
    }
    toast("Settings saved");
    settingsDirty = false;
    loadSettings();
    loadStatus();
  }

  async function changePassword() {
    try {
      await api("/api/password", {
        method: "POST",
        body: JSON.stringify({ current: $("pw-current").value, new: $("pw-new").value }),
      });
      $("pw-current").value = "";
      $("pw-new").value = "";
      csrf = "";
      boot();
    } catch (err) { out(String(err.message || err)); }
  }

  $("save-settings").addEventListener("click", saveSettings);
  $("settings-form").addEventListener("submit", (e) => {
    e.preventDefault();
    saveSettings().catch((err) => toast(String(err.message || err), true));
  });
  $("refresh-rec").addEventListener("click", loadRecordings);
  document.querySelectorAll(".list-toggle").forEach((b) => {
    b.addEventListener("click", () => {
      setListHidden(!document.querySelector(".rec-stage").classList.contains("list-hidden"));
    });
  });
  $("player-prev").addEventListener("click", () => stepVideo(-1));
  $("player-next").addEventListener("click", () => stepVideo(1));
  $("refresh-events").addEventListener("click", loadEvents);
  paintNotifyButton();
  $("notify-toggle").addEventListener("click", async () => {
    if (!notifyOn) {
      if (!("Notification" in window)) {
        toast("Notifications not supported here", true);
        return;
      }
      let perm = "default";
      try {
        perm = await Notification.requestPermission();
      } catch (e) {}
      if (perm !== "granted") {
        toast("Notification permission denied", true);
        return;
      }
      notifyOn = true;
      try {
        localStorage.setItem(NOTIFY_KEY, "1");
      } catch (e) {}
      markSeen(Date.now() / 1000);
    } else {
      notifyOn = false;
      try {
        localStorage.setItem(NOTIFY_KEY, "0");
      } catch (e) {}
    }
    paintNotifyButton();
  });
  setInterval(pollEventAlerts, 30000);
  $("clear-events").addEventListener("click", async () => {
    if (!window.confirm("Clear all events?")) return;
    try {
      await api("/api/events", { method: "DELETE" });
      await loadEvents();
    } catch (e) {
      toast(String(e.message || e), true);
    }
  });
  let recFilterTimer = null;
  $("rec-filter").addEventListener("input", () => {
    clearTimeout(recFilterTimer);
    recFilterTimer = setTimeout(loadRecordings, 300);
  });
  $("player-close").addEventListener("click", closePlayer);
  $("player").addEventListener("timeupdate", syncChat);
  $("player").addEventListener("timeupdate", () => {
    const t = Math.floor($("player").currentTime);
    if (currentPlayId && t >= lastSavedAt + 5) {
      lastSavedAt = t;
      savePosition(currentPlayId, t, $("player").duration);
    }
  });
  $("player").addEventListener("ended", () => {
    forgetPosition(currentPlayId);
    lastSavedAt = -1;
  });
  $("player-speed").addEventListener("change", () => {
    setRate(Number($("player-speed").value) || 1);
  });
  $("player-speed").addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      setRate($("player").playbackRate + (e.deltaY < 0 ? 0.1 : -0.1));
    },
    { passive: false }
  );
  document.addEventListener("keydown", (e) => {
    if (e.key !== "PageUp" && e.key !== "PageDown") return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    if ($("player-wrap").hidden) return;
    e.preventDefault();
    stepVideo(e.key === "PageDown" ? 1 : -1);
  });
  initSeekTap();
  if (window.matchMedia("(max-width: 1100px)").matches) $("chat-follow").checked = false;
  window.addEventListener("resize", () => {
    stickPlayer();
    alignChat();
  });
  watchPlayerSize();
  $("op-reload").addEventListener("click", async () => {
    try {
      const r = await api("/api/reload", { method: "POST" });
      out(r.message);
    } catch (err) { out(String(err.message || err)); }
  });
  $("op-restart").addEventListener("click", async () => {
    if (!window.confirm("Restart the service?")) return;
    try {
      const r = await api("/api/restart", { method: "POST" });
      out(r.message);
    } catch (err) { out(String(err.message || err)); }
  });
  $("op-update").addEventListener("click", async () => {
    try {
      const r = await api("/api/update", {});
      out(r.message);
    } catch (err) { out(String(err.message || err)); }
  });
  $("pw-form").addEventListener("submit", (e) => {
    e.preventDefault();
    changePassword().catch((err) => out(String(err.message || err)));
  });
  boot();
});
