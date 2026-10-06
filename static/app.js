/* Unique Download Manager — UI logic (vanilla, no build step) */
"use strict";

/* ============================== utilities ============================= */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtBytes(n, speed = false) {
  if (n == null || isNaN(n)) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  const d = i === 0 ? 0 : v >= 100 ? 0 : v >= 10 ? 1 : 2;
  return `${v.toFixed(d)} ${u[i]}${speed ? "/s" : ""}`;
}

function fmtEta(sec) {
  if (sec == null || !isFinite(sec)) return "—";
  if (sec <= 0) return "0s";
  sec = Math.round(sec);
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  const h = Math.floor(sec / 3600);
  return `${h}h ${String(Math.floor((sec % 3600) / 60)).padStart(2, "0")}m`;
}

const baseName = (p) => String(p || "").split("/").filter(Boolean).pop() || p || "";

function extOf(name) {
  const m = /\.([A-Za-z0-9]{1,6})$/.exec(name || "");
  return m ? m[1].toUpperCase() : "";
}

const STATUS_LABEL = {
  queued: "Queued", resolving: "Resolving", downloading: "Downloading",
  waiting_network: "Waiting for network", paused: "Paused",
  completed: "Completed", failed: "Failed", canceled: "Canceled",
};

const ACTIVE_STATES = new Set(["resolving", "downloading", "waiting_network"]);
const IFACE_COLORS = ["#22d3ee", "#a78bfa", "#34d399", "#fbbf24", "#f472b6", "#60a5fa", "#fb7185", "#c084fc"];

function colorForIface(ip, networks) {
  const idx = Math.max(0, (networks || []).findIndex((n) => n.ip === ip));
  return IFACE_COLORS[idx % IFACE_COLORS.length];
}

/* ================================ api ================================ */

async function api(path, method = "GET", body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* ignore */ }
  if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status})`);
  return data;
}

function toast(msg, kind = "") {
  const box = $("#toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 350); }, 4200);
  while (box.children.length > 4) box.firstChild.remove();
}

/* ============================== app state ============================ */

const state = {
  snap: null,
  overrideDir: null,           // per-add folder override
  expanded: new Set(),          // task ids with details open
  dirMode: "override",          // "override" | "settings"
  dirPath: "",
  dirHidden: false,
  lastSseAt: 0,
};

/* =========================== snapshot render ========================= */

const taskEls = new Map();      // id -> {card, refs}

function onSnapshot(snap) {
  state.snap = snap;
  state.lastSseAt = Date.now();
  renderHeader(snap);
  renderNetworks(snap);
  renderTasks(snap);
}

function renderHeader(snap) {
  const c = snap.combined;
  $("#hdrSpeed").textContent = fmtBytes(c.app_speed, true);
  $("#hdrTx").textContent = fmtBytes(c.system_tx, true);
  $("#comboSpeed").textContent = fmtBytes(c.app_speed, true);
  $("#comboTx").textContent = `↑ ${fmtBytes(c.system_tx, true)}`;
  const up = snap.networks.filter((n) => n.up);
  const enabled = up.filter((n) => n.enabled);
  $("#comboIfaces").textContent = `${enabled.length} network${enabled.length === 1 ? "" : "s"} active`;
  $("#netCount").textContent = snap.networks.length;
  const conns = snap.tasks.reduce((a, t) => a + (t.connections || 0), 0);
  $("#activeConnLabel").textContent = `${conns} connection${conns === 1 ? "" : "s"}`;
  $("#netSummary").textContent = up.length
    ? up.map((n) => n.kind).join(" · ")
    : "no active network";
  $("#queueSummary").textContent =
    `${snap.active_count} downloading · ${snap.queued_count} queued · ${fmtBytes(c.app_speed, true)}`;
  renderSpark(c.history || []);
}

function renderSpark(history) {
  const line = $("#sparkLine"), fill = $("#sparkFill");
  if (!history.length) { line.setAttribute("points", ""); fill.setAttribute("points", ""); return; }
  const max = Math.max(...history, 1);
  const n = history.length;
  const pts = history.map((v, i) => {
    const x = n === 1 ? 120 : (i / (n - 1)) * 120;
    const y = 43 - (v / max) * 40;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  line.setAttribute("points", pts.join(" "));
  fill.setAttribute("points", `0,44 ${pts.join(" ")} 120,44`);
}

/* ------------------------------ networks ----------------------------- */

const netEls = new Map();

function renderNetworks(snap) {
  const list = $("#netList");
  const seen = new Set();
  const maxSpeed = Math.max(1, ...snap.networks.map((n) => n.app_speed));

  snap.networks.forEach((n, i) => {
    seen.add(n.name);
    let refs = netEls.get(n.name);
    if (!refs) {
      const card = document.createElement("div");
      card.className = "net-card";
      card.innerHTML = `
        <div class="net-card-head">
          <span class="iface-kind"></span>
          <span class="iface-name"></span>
          <span class="iface-ip"></span>
          <label class="switch" title="Use this network for downloads">
            <input type="checkbox"><i></i>
          </label>
        </div>
        <div class="net-card-rows">
          <span>Download <b data-r="app">—</b></span>
          <span>System <b data-r="sys">—</b></span>
        </div>
        <div class="util-bar"><i data-r="util" style="width:0%"></i></div>
        <div class="net-card-foot">
          <span data-r="state">up</span>
          <span class="workers-chip" data-r="workers">0 conns</span>
        </div>`;
      const color = IFACE_COLORS[i % IFACE_COLORS.length];
      card.style.setProperty("--iface-color", color);
      refs = {
        card,
        kind: $(".iface-kind", card),
        name: $(".iface-name", card),
        ip: $(".iface-ip", card),
        toggle: $('input[type="checkbox"]', card),
        app: $('[data-r="app"]', card),
        sys: $('[data-r="sys"]', card),
        util: $('[data-r="util"]', card),
        st: $('[data-r="state"]', card),
        wk: $('[data-r="workers"]', card),
      };
      refs.toggle.addEventListener("change", () => toggleIface(n.name, refs.toggle.checked));
      netEls.set(n.name, refs);
      list.appendChild(card);
    }
    refs.kind.textContent = n.kind;
    refs.name.textContent = n.name;
    refs.ip.textContent = n.ip;
    refs.app.textContent = fmtBytes(n.app_speed, true);
    refs.sys.textContent = `↓${fmtBytes(n.rx_speed, true)} ↑${fmtBytes(n.tx_speed, true)}`;
    refs.util.style.width = `${Math.min(100, (n.app_speed / maxSpeed) * 100)}%`;
    refs.wk.textContent = `${n.workers} conn${n.workers === 1 ? "" : "s"}`;
    refs.st.innerHTML = n.up
      ? `<i class="dot dot-live"></i>${n.speed_mbps ? n.speed_mbps + " Mbps" : "connected"}`
      : `<i class="dot" style="background:var(--red)"></i>disconnected`;
    if (refs.toggle.checked !== n.enabled) refs.toggle.checked = n.enabled;
    refs.card.classList.toggle("off", !n.enabled);
    refs.card.classList.toggle("down", !n.up);
    // keep DOM order in sync
    if (list.children[i] !== refs.card) list.insertBefore(refs.card, list.children[i] || null);
  });

  netEls.forEach((refs, name) => {
    if (!seen.has(name)) { refs.card.remove(); netEls.delete(name); }
  });
}

async function toggleIface(name, on) {
  const s = state.snap.settings;
  const all = (state.snap.networks || []).map((n) => n.name);
  let enabled = s.enabled_ifaces == null ? [...all] : s.enabled_ifaces.filter((x) => all.includes(x));
  if (on && !enabled.includes(name)) enabled.push(name);
  if (!on) enabled = enabled.filter((x) => x !== name);
  try {
    await api("/api/settings", "POST", { enabled_ifaces: enabled });
    toast(`${name} ${on ? "enabled" : "disabled"} for downloads`, "ok");
  } catch (e) { toast(e.message, "err"); }
}

/* -------------------------------- tasks ------------------------------ */

function taskCardTemplate() {
  return `
    <div class="task-top">
      <div class="file-ico" data-r="ico">FILE</div>
      <div class="task-main">
        <div class="task-name-row">
          <h3 class="task-name placeholder" data-r="name">Resolving…</h3>
          <span class="chip chip-resolving" data-r="chip">RESOLVING</span>
        </div>
        <div class="task-host" data-r="host"></div>
      </div>
      <div class="task-actions">
        <button class="tbtn go" data-act="pause" title="Pause" hidden>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M9 5v14M15 5v14"/></svg>
        </button>
        <button class="tbtn go" data-act="resume" title="Resume" hidden>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 4.5l12 7.5-12 7.5z"/></svg>
        </button>
        <button class="tbtn danger" data-act="cancel" title="Cancel" hidden>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button>
        <button class="tbtn" data-act="up" title="Raise priority">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5m0 0l-6 6m6-6l6 6"/></svg>
        </button>
        <button class="tbtn" data-act="down" title="Lower priority">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14m0 0l6-6m-6 6l-6-6"/></svg>
        </button>
        <button class="tbtn" data-act="reveal" title="Show in folder" hidden>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/></svg>
        </button>
        <button class="tbtn danger" data-act="remove" title="Remove from list">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 002 2h6a2 2 0 002-2l1-12M9 7V5a2 2 0 012-2h2a2 2 0 012 2v2"/></svg>
        </button>
      </div>
    </div>

    <div class="progress-overview">
      <div class="progress-wrap">
        <div class="progress" data-r="prog" role="progressbar" aria-label="Download progress"
             aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
          <i aria-hidden="true"></i><b data-r="pct">0%</b>
        </div>
      </div>

      <div class="stats">
        <div class="stat"><div class="k">Speed</div><div class="v accent" data-r="speed">—</div></div>
        <div class="stat"><div class="k">ETA</div><div class="v" data-r="eta">—</div></div>
        <div class="stat"><div class="k">Downloaded</div><div class="v dim" data-r="size">—</div></div>
        <div class="stat"><div class="k">Connections</div><div class="v" data-r="conns">0</div></div>
      </div>
    </div>

    <div class="iface-split" data-r="split" hidden>
      <div class="iface-bar" data-r="splitBar"></div>
      <div class="iface-legend" data-r="splitLegend"></div>
    </div>

    <div class="task-error" data-r="err" hidden></div>

    <dl class="task-details" data-r="details" hidden></dl>
    <button class="disclose" data-act="details">
      <span>Details</span>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
    </button>`;
}

function createTaskCard(t) {
  const card = document.createElement("div");
  card.className = "task-card";
  card.dataset.id = t.id;
  card.innerHTML = taskCardTemplate();
  const refs = {};
  $$("[data-r]", card).forEach((n) => (refs[n.dataset.r] = n));
  const actions = {};
  $$("[data-act]", card).forEach((n) => (actions[n.dataset.act] = n));
  const entry = { card, refs, actions };
  taskEls.set(t.id, entry);
  return entry;
}

function updateTaskCard(t, entry) {
  const { card, refs, actions } = entry;
  const active = ACTIVE_STATES.has(t.status);
  const done = t.status === "completed";

  card.className = `task-card st-${t.status}` + (active ? " st-active" : "");

  /* name / icon */
  const fname = t.filename || "";
  if (fname) {
    refs.name.textContent = fname;
    refs.name.classList.remove("placeholder");
    refs.name.title = fname;
  } else {
    refs.name.textContent = t.status === "failed" ? "Download failed" : "Resolving filename…";
    refs.name.classList.add("placeholder");
  }
  const ext = extOf(fname);
  refs.ico.textContent = ext && ext.length <= 4 ? ext : "FILE";

  /* host */
  let host = "";
  try { host = new URL(t.final_url || t.url).host; } catch (_) { host = t.url; }
  refs.host.textContent = host;

  /* status chip */
  refs.chip.textContent = STATUS_LABEL[t.status] || t.status;
  refs.chip.className = `chip chip-${t.status === "waiting_network" ? "waiting" : t.status}`;

  /* action buttons */
  const canPause = ["resolving", "downloading", "waiting_network", "queued"].includes(t.status);
  const canResume = ["paused", "failed"].includes(t.status);
  const canCancel = active || t.status === "queued";
  const showReveal = done && t.path;
  setBtn(actions.pause, canPause, () => act(t.id, "pause"));
  setBtn(actions.resume, canResume, () => act(t.id, "resume"));
  setBtn(actions.cancel, canCancel, () => act(t.id, "cancel"));
  setBtn(actions.reveal, showReveal, () => api("/api/open", "POST", { path: t.path })
    .catch((e) => toast(e.message, "err")));
  actions.up.onclick = () => act(t.id, "priority", { delta: 1 });
  actions.down.onclick = () => act(t.id, "priority", { delta: -1 });

  /* progress */
  const pct = t.total ? Math.min(100, t.progress) : (done ? 100 : 0);
  const indeterminate = active && !t.total && t.status !== "resolving";
  refs.prog.classList.toggle("indeterminate", indeterminate);
  refs.prog.style.setProperty("--progress", `${pct}%`);
  refs.pct.textContent = indeterminate ? "..." : `${pct.toFixed(pct > 0 && pct < 10 ? 2 : 1)}%`;
  if (indeterminate) {
    refs.prog.removeAttribute("aria-valuenow");
    refs.prog.setAttribute("aria-valuetext", "Progress unknown");
  } else {
    refs.prog.setAttribute("aria-valuenow", pct.toFixed(1));
    refs.prog.setAttribute("aria-valuetext", `${pct.toFixed(1)}%`);
  }

  /* stats */
  refs.speed.textContent = t.status === "waiting_network" ? "offline" : fmtBytes(t.speed, true);
  refs.eta.textContent = done ? "done" : fmtEta(t.eta);
  refs.size.textContent = t.total
    ? `${fmtBytes(t.downloaded)} / ${fmtBytes(t.total)}`
    : fmtBytes(t.downloaded);
  refs.conns.textContent = String(t.connections || 0);

  /* per-interface contribution */
  const ifaces = Object.entries(t.iface_bytes || {}).filter(([, b]) => b > 0);
  if (ifaces.length) {
    refs.split.hidden = false;
    const totalB = ifaces.reduce((a, [, b]) => a + b, 0) || 1;
    refs.splitBar.innerHTML = ifaces.map(([ip, b]) =>
      `<span style="width:${(b / totalB) * 100}%;background:${colorForIface(ip, state.snap.networks)}" title="${esc(ip)}"></span>`
    ).join("");
    refs.splitLegend.innerHTML = ifaces.map(([ip, b]) => {
      const net = (state.snap.networks || []).find((n) => n.ip === ip);
      const label = net ? `${net.kind} · ${net.name}` : ip;
      const spd = (t.iface_speed || {})[ip];
      return `<span class="lg"><span class="sw" style="background:${colorForIface(ip, state.snap.networks)}"></span>` +
        `${esc(label)} <b>${fmtBytes(b)}</b>${spd ? ` <span class="spd">↓${fmtBytes(spd, true)}</span>` : ""}</span>`;
    }).join("");
  } else {
    refs.split.hidden = true;
  }

  /* error */
  if (t.error && (t.status === "failed" || t.status === "waiting_network")) {
    refs.err.hidden = false;
    refs.err.textContent = t.error;
  } else {
    refs.err.hidden = true;
  }

  /* details */
  const open = state.expanded.has(t.id);
  refs.details.hidden = !open;
  card.querySelector(".disclose").classList.toggle("open", open);
  card.querySelector(".disclose span").textContent = open ? "Hide details" : "Details";
  if (open) {
    refs.details.innerHTML = `
      <dt>URL</dt><dd>${esc(t.url)}</dd>
      ${t.final_url && t.final_url !== t.url ? `<dt>Resolved</dt><dd>${esc(t.final_url)}</dd>` : ""}
      <dt>Saved to</dt><dd>${esc(t.path || t.directory)}</dd>
      <dt>Resume</dt><dd>${t.supports_range ? "supported (HTTP Range)" : "not supported by server"}</dd>
      ${t.total ? `<dt>Chunks</dt><dd>${t.chunks.finished}/${t.chunks.total} done · ${fmtBytes(t.chunks.size)} each</dd>` : ""}
      ${t.priority ? `<dt>Priority</dt><dd>+${t.priority}</dd>` : ""}
      ${t.completed_at ? `<dt>Finished</dt><dd>${new Date(t.completed_at * 1000).toLocaleString()}</dd>` : ""}`;
  }
}

function setBtn(btn, show, handler) {
  if (!btn) return;
  btn.hidden = !show;
  btn.onclick = show ? handler : null;
}

async function act(id, action, body) {
  try {
    if (action === "remove") await api(`/api/tasks/${id}`, "DELETE");
    else await api(`/api/tasks/${id}/${action}`, "POST", body);
  } catch (e) { toast(e.message, "err"); }
}

function renderTasks(snap) {
  const list = $("#taskList");
  const seen = new Set();

  snap.tasks.forEach((t, i) => {
    seen.add(t.id);
    let entry = taskEls.get(t.id);
    if (!entry) entry = createTaskCard(t);
    updateTaskCard(t, entry);
    if (list.children[i] !== entry.card) list.insertBefore(entry.card, list.children[i] || null);
  });

  taskEls.forEach((entry, id) => {
    if (!seen.has(id)) { entry.card.remove(); taskEls.delete(id); state.expanded.delete(id); }
  });

  $("#emptyState").hidden = snap.tasks.length > 0;
}

/* task card delegated clicks */
$("#taskList").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const card = btn.closest(".task-card");
  if (!card) return;
  const id = card.dataset.id;
  const actName = btn.dataset.act;
  if (actName === "details") {
    state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
    const entry = taskEls.get(id);
    if (entry && state.snap) {
      const t = state.snap.tasks.find((x) => x.id === id);
      if (t) updateTaskCard(t, entry);
    }
    return;
  }
  if (["up", "down", "remove", "cancel", "pause", "resume"].includes(actName)) {
    if (actName === "up") act(id, "priority", { delta: 1 });
    else if (actName === "down") act(id, "priority", { delta: -1 });
    else act(id, actName);
  }
});

/* ============================== add form ============================= */

$("#addForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#urlInput");
  const url = input.value.trim();
  if (!url) return;
  const btn = $("#btnAdd");
  btn.disabled = true;
  try {
    const task = await api("/api/add", "POST", {
      url, directory: state.overrideDir || undefined,
    });
    input.value = "";
    state.overrideDir = null;
    updateDirChip();
    toast(`Queued: ${task.filename || baseName(new URL(safeUrl(url)).pathname) || "download"}`, "ok");
  } catch (err) {
    toast(err.message, "err");
  } finally {
    btn.disabled = false;
    input.focus();
  }
});

function safeUrl(u) { try { return new URL(u).toString(); } catch (_) { return u; } }

$("#btnPaste").addEventListener("click", async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (text) { $("#urlInput").value = text.trim(); $("#urlInput").focus(); }
    else toast("Clipboard is empty");
  } catch (_) {
    toast("Clipboard access denied — paste with ⌘V / Ctrl+V", "err");
  }
});

function updateDirChip() {
  const dir = state.overrideDir || (state.snap && state.snap.settings.directory) || "";
  $("#dirChipLabel").textContent = baseName(dir) || dir;
  $("#dirChip").style.borderColor = state.overrideDir ? "var(--accent)" : "";
  $("#dirChip").style.color = state.overrideDir ? "var(--accent-2)" : "";
}

/* ============================== settings ============================= */

function openSettings() {
  const s = state.snap.settings;
  $("#setDir").value = s.directory;
  $("#setMax").value = s.max_downloads;
  $("#setConn").value = s.connections;
  $("#maxDlVal").textContent = `= ${s.max_downloads}`;
  $("#connVal").textContent = `= ${s.connections}`;
  renderIfaceChecks();
  $("#settingsModal").hidden = false;
}

function renderIfaceChecks() {
  const s = state.snap.settings;
  const nets = state.snap.networks;
  const box = $("#ifaceChecks");
  if (!nets.length) {
    box.innerHTML = `<div class="hint">No network interfaces detected yet.</div>`;
    return;
  }
  box.innerHTML = nets.map((n, i) => {
    const on = s.enabled_ifaces == null || s.enabled_ifaces.includes(n.name);
    return `<label class="iface-check">
      <input type="checkbox" data-name="${esc(n.name)}" ${on ? "checked" : ""}>
      <span class="ck-kind" style="color:${IFACE_COLORS[i % IFACE_COLORS.length]};
        background:${IFACE_COLORS[i % IFACE_COLORS.length]}1c">${esc(n.kind)}</span>
      <span>${esc(n.name)}</span>
      <span class="ck-ip">${esc(n.ip)}</span>
    </label>`;
  }).join("");
}

$("#btnSettings").addEventListener("click", openSettings);
$("#setMax").addEventListener("input", () => ($("#maxDlVal").textContent = `= ${$("#setMax").value}`));
$("#setConn").addEventListener("input", () => $("#connVal").textContent = `= ${$("#setConn").value}`);
$("#setDirBrowse").addEventListener("click", () => openDirModal("settings", $("#setDir").value));

$("#btnSaveSettings").addEventListener("click", async () => {
  try {
    const enabled = $$("#ifaceChecks input:checked").map((i) => i.dataset.name);
    await api("/api/settings", "POST", {
      directory: $("#setDir").value,
      max_downloads: +$("#setMax").value,
      connections: +$("#setConn").value,
      enabled_ifaces: enabled,
    });
    $("#settingsModal").hidden = true;
    updateDirChip();
    toast("Settings saved", "ok");
  } catch (e) { toast(e.message, "err"); }
});

/* ======================= directory browser modal ===================== */

function openDirModal(mode, startPath) {
  state.dirMode = mode;
  const current = mode === "settings"
    ? (startPath || (state.snap && state.snap.settings.directory) || "")
    : (state.overrideDir || (state.snap && state.snap.settings.directory) || "");
  $("#dirTitle").textContent = mode === "settings" ? "Choose default folder"
    : "Choose folder for this download";
  $("#dirModal").hidden = false;
  loadDir(current);
}

async function loadDir(path) {
  try {
    const data = await api(`/api/fs?path=${encodeURIComponent(path)}&hidden=${state.dirHidden ? 1 : 0}`);
    state.dirPath = data.path;
    $("#dirPath").textContent = data.path;
    $("#dirPath").title = data.path;
    $("#dirUp").disabled = !data.parent;
    $("#dirQuick").innerHTML = (data.quick || [])
      .map((q) => `<button data-path="${esc(q.path)}">${esc(q.label)}</button>`).join("");
    $("#dirList").innerHTML = data.dirs.length
      ? data.dirs.map((d) => `<button class="dir-item" data-path="${esc(data.path + "/" + d)}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/></svg>
          <span>${esc(d)}</span></button>`).join("")
      : `<div class="dir-empty">No folders here</div>`;
    if (!data.writable) toast("Warning: this folder may not be writable", "err");
  } catch (e) {
    $("#dirList").innerHTML = `<div class="dir-empty">${esc(e.message)}</div>`;
  }
}

$("#dirList").addEventListener("click", (e) => {
  const item = e.target.closest(".dir-item");
  if (item) loadDir(item.dataset.path);
});
$("#dirQuick").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b) loadDir(b.dataset.path);
});
$("#dirUp").addEventListener("click", () => {
  const parts = state.dirPath.split("/").filter(Boolean);
  parts.pop();
  loadDir("/" + parts.join("/"));
});
$("#dirHidden").addEventListener("change", (e) => {
  state.dirHidden = e.target.checked;
  loadDir(state.dirPath);
});
$("#btnUseDir").addEventListener("click", () => {
  if (!state.dirPath) return;
  if (state.dirMode === "settings") {
    $("#setDir").value = state.dirPath;
  } else {
    state.overrideDir = state.dirPath;
    updateDirChip();
    toast(`Saving to: ${state.dirPath}`, "ok");
  }
  $("#dirModal").hidden = true;
});
$("#dirChip").addEventListener("click", () => {
  if (state.overrideDir) {
    // second click clears the override
    state.overrideDir = null;
    updateDirChip();
    toast("Using the default folder");
    return;
  }
  openDirModal("override");
});

/* modal close buttons */
$$(".modal-backdrop").forEach((bd) => {
  bd.addEventListener("click", (e) => {
    if (e.target === bd || e.target.closest("[data-close]")) bd.hidden = true;
  });
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $$(".modal-backdrop").forEach((bd) => (bd.hidden = true));
});

/* ============================ live updates =========================== */

function connect() {
  if (!("EventSource" in window)) { startPolling(); return; }
  const es = new EventSource("/api/events");
  es.onmessage = (e) => {
    try { onSnapshot(JSON.parse(e.data)); } catch (_) { /* skip bad frame */ }
  };
  es.onerror = () => { /* EventSource auto-reconnects */ };
  // Safety net: if SSE goes silent, poll too.
  setInterval(() => {
    if (Date.now() - state.lastSseAt > 6000) startPolling();
  }, 3000);
}

let pollTimer = null;
function startPolling() {
  if (pollTimer) return;
  const tick = async () => {
    try { onSnapshot(await api("/api/snapshot")); } catch (_) { /* server down */ }
  };
  tick();
  pollTimer = setInterval(tick, 1000);
}

/* ================================ boot =============================== */

(async function boot() {
  try {
    onSnapshot(await api("/api/snapshot"));
  } catch (e) {
    toast("Cannot reach the download server", "err");
  }
  connect();
  updateDirChip();
})();
