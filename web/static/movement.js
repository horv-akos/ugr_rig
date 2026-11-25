// Movement calibration page: jog the head with live view, set the zero, record positions.
//
// Hold-to-move is a dead-man: while an arrow is held the page pings the server every 100 ms,
// and the server ramps the jog down when the pings stop (released, tab hidden, WiFi gone).
// Status is polled at 4 Hz, which doubles as the connection check.

import { api, deg, el, remember, seg } from "./api.js";
import { drawMasks, openMaskEditor, wireMaskEditor } from "./masks.js";

const $ = (id) => document.getElementById(id);
const D2R = Math.PI / 180;
const PING_MS = 100;
const POLL_MS = 250;
const HERE_DEG = 0.05;           // a saved position counts as "here" within this

let st = null;                   // last /api/status
let geom = { fov_deg: [63.6, 44.9], width_px: 3000, height_px: 2000 };
let profile = null;              // selected profile, as /api/positions/{name} returns it
let lastMotionId = null;
let mode = remember("ugr.mode") || "hold";
let speedEditing = 0;            // timestamp of the last slider touch

// ---------------------------------------------------------------- messages

function say(msg, kind = "") {
  const t = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const log = $("log");
  log.prepend(el("li", {}, el("time", {}, t), el("span", { class: kind }, msg)));
  while (log.children.length > 40) log.lastChild.remove();
}

async function act(fn, okMsg) {
  try {
    const r = await fn();
    if (okMsg) say(okMsg);
    return r;
  } catch (e) {
    say(e.message, "bad");
    return null;
  }
}

// ---------------------------------------------------------------- status

async function poll() {
  try {
    st = await api("GET", "/api/status");
    setConn(true);
    render();
  } catch (e) {
    setConn(false, e.message);
  }
  setTimeout(poll, POLL_MS);
}

function setConn(up, why = "") {
  const c = $("conn");
  c.classList.toggle("up", up);
  c.textContent = up ? "connected" : "no server";
  c.title = why;
  if (!up) jogEnd(false);
}

function pill(text, kind = "", title = "") {
  return el("span", { class: `pill ${kind}`, title }, text);
}

function render() {
  const zeroed = st.zeroed, on = st.enabled, idle = st.state === "idle";
  $("pan").textContent = deg(st.pan_deg);
  $("tilt").textContent = deg(st.tilt_deg);

  const pills = [
    on ? pill("drivers on", "ok") : pill("drivers off", "warn"),
    !zeroed ? pill("no zero", "bad", `aim near the fiducial, then Zero on fiducial (moves are capped at ${st.unzeroed_jog_deg}° until then)`)
      : st.zero && st.zero.method === "optical" ? pill("zero: optical", "ok", `on the fiducial at ${(st.zero.at || "").replace("T", " ").replace("Z", "")} UTC`)
        : pill("zero: by eye", "warn", "about 0.3°, and studies get no drift figure. Zero on the fiducial to fix both"),
  ];
  const zc = st.zero_check;
  if (zeroed && zc) {
    const over = zc.offset_deg > zc.limit_deg;
    pills.push(pill(`drift ${zc.offset_deg.toFixed(3)}°`, over ? "bad" : "ok",
      `fiducial checked at ${zc.at.replace("T", " ").replace("Z", "")} UTC${over ? `: over ${zc.limit_deg}°, the head lost steps: zero again` : ""}`));
  }
  if (zeroed && !st.certain) pills.push(pill("position uncertain: re-zero", "bad"));
  const drv = st.drivers;
  if (on && drv && drv.configured) {
    const ax = Object.values(drv.axes || {});
    pills.push(pill("UART verified", "ok", ax.map((a) =>
      `address ${a.address}: 1/${a.microsteps}, run ${a.run_current_a} A, hold ${a.hold_current_a} A`).join("\n")));
  }
  for (const w of (drv && drv.warnings) || []) pills.push(pill(w, "warn"));
  const lash = Object.entries(st.backlash_ok || {}).filter(([, ok]) => !ok).map(([a]) => a);
  if (zeroed && lash.length) pills.push(pill(`backlash: ${lash.join(", ")}`, "warn",
    "Last move on this axis went against the approach direction. Saving takes it up first."));
  if (!idle) pills.push(pill(st.state, "busy"));
  $("pills").replaceChildren(...pills);

  $("enable").disabled = on;
  $("disable").disabled = !on || !idle;
  for (const b of document.querySelectorAll(".pad [data-dir]")) b.disabled = !on;
  $("zero").disabled = !on || !idle;
  $("zero-optical").disabled = !on || !idle || st.live_view.source === "none";
  $("zero-check").disabled = !on || !idle || !zeroed || st.live_view.source === "none";
  $("backlash").disabled = !on || !idle;
  const canMove = on && zeroed && idle;   // absolute moves and saving need the zero
  $("goto").querySelector("button").disabled = !canMove;
  for (const b of [$("save-form").querySelector("button"), $("gaze"), $("run")]) {
    b.disabled = !canMove || !profile;
  }
  document.querySelectorAll(".positions .go, #cov-prop .go").forEach((b) => { b.disabled = !canMove; });

  if (Date.now() - speedEditing > 1500) {
    $("speed").value = Math.round(st.speed * 100);
    showSpeed();
  }

  const lm = st.last_motion;
  if (lm && lm.id !== lastMotionId) {
    if (lastMotionId !== null) report(lm);
    lastMotionId = lm.id;
    if (["save", "run"].includes(lm.kind)) loadProfile();
  }

  markHere();
  renderLive();
  drawOverlay();
}

function report(m) {
  const r = m.result || {};
  const at = "pan_deg" in r ? ` at ${deg(r.pan_deg)}°, ${deg(r.tilt_deg)}°` : "";
  if (m.error) {
    say(`${m.kind} failed: ${m.error.detail}`, "bad");
  } else if (m.kind === "save") {
    say(`saved “${r.position.name}”${r.backlash_taken_up ? " (backlash taken up first)" : ""}`);
  } else if (m.kind === "run") {
    say(`visited ${r.visited.length} position(s)${r.complete ? "" : `, stopped (${m.stop_reason || "stop"})`}`);
  } else if (m.kind === "zero") {
    say(`zero set by eye${r.backlash_taken_up ? " (backlash taken up first)" : ""}`);
  } else if (m.kind === "optical_zero") {
    say(`zero on the fiducial: corrected by ${deg(r.correction_deg[0])}°, ${deg(r.correction_deg[1])}°; `
      + `check at (0, 0) ${r.residual_deg.toFixed(3)}°`, r.residual_deg > 0.05 ? "warn" : "");
  } else if (m.kind === "check_zero") {
    say(`zero check: fiducial ${r.offset_deg.toFixed(3)}° from (0, 0)`
      + (r.over_limit ? ` - over ${r.limit_deg}°: the head lost steps, zero on the fiducial again` : ""),
    r.over_limit ? "bad" : "");
  } else if (r.stopped) {
    say(`${m.kind} stopped${at} (${m.stop_reason || "stop"})`, m.stop_reason === "heartbeat lost" ? "warn" : "");
  } else if (m.kind !== "jog") {
    say(`${m.kind} done${at}`);
  }
}

// ---------------------------------------------------------------- motion

let jog = null;   // {dir, timer} while an arrow is held

function jogStart(dir) {
  if (jog || !st || !st.enabled) return;
  const me = { dir, timer: null };
  jog = me;
  pressed(dir, true);
  api("POST", "/api/mount/jog/start", { direction: dir }).then(() => {
    if (jog !== me) {                 // released before the server answered
      api("POST", "/api/mount/jog/stop").catch(() => {});
      return;
    }
    me.timer = setInterval(() => {
      api("POST", "/api/mount/jog/ping")
        .then((r) => { if (!r.jogging && jog === me) jogEnd(false); })
        .catch(() => {});
    }, PING_MS);
  }).catch((e) => {
    if (jog === me) { jog = null; pressed(dir, false); }
    say(e.message, "bad");
  });
}

function jogEnd(send = true) {
  const me = jog;
  if (!me) return;
  jog = null;
  clearInterval(me.timer);
  pressed(me.dir, false);
  if (send) api("POST", "/api/mount/jog/stop").catch(() => {});
}

function pressed(dir, on) {
  const b = document.querySelector(`.pad [data-dir="${dir}"]`);
  if (b) b.classList.toggle("pressed", on);
}

function press(dir) {
  if (mode === "hold") { jogStart(dir); return; }
  // One nudge at a time: a tap while the last step (and its backlash take-up) is still
  // running is dropped with a note, not an error.
  if (st && st.state !== "idle") { say("still moving: wait for the last step", "warn"); return; }
  api("POST", "/api/mount/nudge", { direction: dir, deg: Number(mode) }).catch((e) => {
    say(e.type === "RigBusy" ? "still moving: wait for the last step" : e.message,
      e.type === "RigBusy" ? "warn" : "bad");
  });
}

function release() {
  if (mode === "hold") jogEnd();
}

function stopAll() {
  jogEnd(false);
  act(() => api("POST", "/api/mount/stop"));
}

function setMode(m) {
  mode = m;
  remember("ugr.mode", m);
  if (m !== "hold") remember("ugr.step", m);   // what H switches back to
  for (const b of document.querySelectorAll(".seggroup button")) {
    b.setAttribute("aria-pressed", String(b.dataset.mode === m));
  }
}

function showSpeed() {
  const f = Number($("speed").value) / 100;
  const pan = st ? st.axes.pan.max_speed_deg_s * f : null;
  $("speed-val").textContent = `${Math.round(f * 100)} %` + (pan !== null ? ` · ${pan.toFixed(2)}°/s` : "");
}

// ---------------------------------------------------------------- live view

let liveRetry = null;

function liveWanted() {
  return $("opt-live").checked && st && st.live_view.source !== "none";
}

function renderLive() {
  const img = $("live"), lv = st.live_view;
  const want = liveWanted();
  if (want && !img.getAttribute("src") && !liveRetry) img.src = `/api/live.mjpg?t=${Date.now()}`;
  if (!want && img.getAttribute("src")) img.removeAttribute("src");

  let msg = "";
  if (lv.source === "none") msg = "No camera: the server was started with --camera none.";
  else if (!$("opt-live").checked) msg = "Live view off.";
  else if (lv.state === "error") msg = `Camera: ${lv.error}`;
  else if (lv.state === "paused") msg = "Live view paused while the camera captures.";
  else if (lv.state === "starting" || !img.naturalWidth) msg = "Starting live view…";
  $("live-msg").textContent = msg;
  $("live-info").textContent = lv.state === "running"
    ? `${lv.source === "sim" ? "simulated" : "camera"} · ${lv.fps} fps${exposureText(lv.exposure)}`
    : lv.state;
}

// The shutter the camera runs live view at. No effect on a measurement.
function exposureText(e) {
  if (!e) return "";
  const note = !e.auto ? ", exposure not controlled"
    : e.at_limit ? `, still too ${e.at_limit}` : "";
  return ` · ${e.shutter}${e.shutter.includes("/") ? "" : " s"}${note}`;
}

function liveError() {
  const img = $("live");
  img.removeAttribute("src");
  clearTimeout(liveRetry);
  liveRetry = setTimeout(() => { liveRetry = null; }, 2000);   // render() reconnects after this
}

// ---------------------------------------------------------------- overlay

// Same convention as photometry.masks.rotation: columns are the camera axes in the cabin
// frame; pan positive = right, tilt positive = up.
function rotation(panDeg, tiltDeg) {
  const p = panDeg * D2R, t = tiltDeg * D2R;
  const z = [Math.sin(p) * Math.cos(t), Math.sin(t), Math.cos(p) * Math.cos(t)];
  const x = [Math.cos(p), 0, -Math.sin(p)];
  const y = [z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0]];
  return [x, y, z];
}
const toWorld = (R, v) => [0, 1, 2].map((i) => R[0][i] * v[0] + R[1][i] * v[1] + R[2][i] * v[2]);
const toCam = (R, w) => R.map((a) => a[0] * w[0] + a[1] * w[1] + a[2] * w[2]);

// Keep the part of a polygon in front of the camera. Edges of a rectilinear frame are great
// circles, which a rectilinear view shows as straight lines, so clipping the corners and
// joining them with straight lines is exact.
function clipFront(poly, eps = 1e-3) {
  const out = [];
  for (let i = 0; i < poly.length; i++) {
    const a = poly[i], b = poly[(i + 1) % poly.length];
    const ain = a[2] >= eps, bin = b[2] >= eps;
    if (ain) out.push(a);
    if (ain !== bin) {
      const t = (eps - a[2]) / (b[2] - a[2]);
      out.push([a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), eps]);
    }
  }
  return out;
}

function imageRect(box) {
  // The overlay must sit on the picture, not the letterbox around it.
  const img = $("live");
  const bw = box.clientWidth, bh = box.clientHeight;
  const iw = img.naturalWidth || geom.width_px, ih = img.naturalHeight || geom.height_px;
  const s = Math.min(bw / iw, bh / ih);
  return { x: (bw - iw * s) / 2, y: (bh - ih * s) / 2, w: iw * s, h: ih * s };
}

function drawOverlay() {
  const box = $("live-box"), cv = $("overlay");
  const dpr = window.devicePixelRatio || 1;
  const W = box.clientWidth, H = box.clientHeight;
  if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
    cv.width = Math.round(W * dpr);
    cv.height = Math.round(H * dpr);
  }
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);
  if (!st) return;

  const r = imageRect(box);
  // At a saved position its masks line up with the picture: they were drawn on it.
  const masks = $("opt-outlines").checked ? hereMasks() : null;
  if (masks) drawMasks(g, r, masks, { fill: 0.12 });
  const tx = Math.tan(geom.fov_deg[0] * D2R / 2), ty = Math.tan(geom.fov_deg[1] * D2R / 2);
  const project = (c) => [r.x + (c[0] / c[2] / tx + 1) / 2 * r.w, r.y + (1 - c[1] / c[2] / ty) / 2 * r.h];
  const Rc = rotation(st.pan_deg, st.tilt_deg);
  g.font = "12px system-ui, sans-serif";
  g.lineWidth = 1.5;

  // Saved positions and forward gaze: angles from the zero, so only meaningful with one.
  if (st.zeroed && profile && $("opt-outlines").checked) {
    for (const p of profile.positions) {
      if (Math.abs(p.pan_deg - st.pan_deg) < HERE_DEG && Math.abs(p.tilt_deg - st.tilt_deg) < HERE_DEG) continue;
      const Rp = rotation(p.pan_deg, p.tilt_deg);
      const corners = [[-tx, ty, 1], [tx, ty, 1], [tx, -ty, 1], [-tx, -ty, 1]]
        .map((v) => toCam(Rc, toWorld(Rp, v)));
      const poly = clipFront(corners);
      if (poly.length < 3) continue;
      g.strokeStyle = "rgba(90, 162, 255, 0.85)";
      g.setLineDash([6, 4]);
      g.beginPath();
      poly.map(project).forEach(([x, y], i) => (i ? g.lineTo(x, y) : g.moveTo(x, y)));
      g.closePath();
      g.stroke();
      g.setLineDash([]);
      const c = toCam(Rc, toWorld(Rp, [0, 0, 1]));
      if (c[2] > 0.05) {
        const [x, y] = project(c);
        if (x > r.x - 50 && x < r.x + r.w + 50 && y > r.y - 20 && y < r.y + r.h + 20) {
          g.fillStyle = "rgba(90, 162, 255, 0.95)";
          g.fillRect(x - 4, y - 0.75, 8, 1.5);
          g.fillRect(x - 0.75, y - 4, 1.5, 8);
          label(g, p.name, x + 6, y - 6, "rgba(90, 162, 255, 1)");
        }
      }
    }
    if (profile.forward_gaze) {
      const c = toCam(Rc, toWorld(rotation(...profile.forward_gaze), [0, 0, 1]));
      if (c[2] > 0.05) {
        const [x, y] = project(c);
        g.strokeStyle = "#3fb950";
        g.beginPath();
        g.arc(x, y, 7, 0, 2 * Math.PI);
        g.stroke();
        label(g, "forward gaze", x + 10, y + 4, "#3fb950");
      }
    }
  }

  // Crosshair on the optical axis, with a 1° ring.
  const cx = r.x + r.w / 2, cy = r.y + r.h / 2;
  const ring = Math.tan(0.5 * D2R) / tx * r.w / 2;
  g.strokeStyle = "rgba(255, 196, 64, 0.95)";
  g.beginPath();
  g.moveTo(cx - 28, cy); g.lineTo(cx - ring - 3, cy);
  g.moveTo(cx + ring + 3, cy); g.lineTo(cx + 28, cy);
  g.moveTo(cx, cy - 28); g.lineTo(cx, cy - ring - 3);
  g.moveTo(cx, cy + ring + 3); g.lineTo(cx, cy + 28);
  g.stroke();
  g.beginPath();
  g.arc(cx, cy, ring, 0, 2 * Math.PI);
  g.stroke();

  const text = st.zeroed ? `pan ${deg(st.pan_deg)}°   tilt ${deg(st.tilt_deg)}°` : "no zero set";
  label(g, text, r.x + 8, r.y + 18, st.zeroed ? "#e4e7eb" : "#f2554b");
}

function label(g, text, x, y, color) {
  const w = g.measureText(text).width;
  g.fillStyle = "rgba(0, 0, 0, 0.6)";
  g.fillRect(x - 3, y - 13, w + 6, 17);
  g.fillStyle = color;
  g.fillText(text, x, y);
}

// ---------------------------------------------------------------- profiles

async function loadProfiles(select) {
  const sel = $("profile");
  let list = [];
  try { list = await api("GET", "/api/positions"); } catch (e) { say(e.message, "bad"); }
  const want = select || sel.value || remember("ugr.profile");
  sel.replaceChildren(...(list.length ? list.map((p) => el("option", { value: p.name },
    `${p.name}${p.vehicle ? ` · ${p.vehicle}` : ""} (${p.positions})`))
    : [el("option", { value: "" }, "No profiles yet: create one")]));
  if (list.some((p) => p.name === want)) sel.value = want;
  await loadProfile();
}

async function loadProfile() {
  const name = $("profile").value;
  profile = null;
  if (name) {
    try {
      profile = await api("GET", `/api/positions/${seg(name)}`);
      remember("ugr.profile", name);
    } catch (e) { say(e.message, "bad"); }
  }
  renderProfile();
  loadCoverage();
}

async function loadCoverage() {
  const line = $("cov-line");
  if (!profile || !profile.forward_gaze) {
    line.textContent = profile ? "Set the forward gaze to see how much of the driver's view the positions cover." : "";
    $("cov-details").hidden = true;
    return;
  }
  $("cov-details").hidden = false;
  let c;
  try { c = await api("GET", `/api/positions/${seg(profile.name)}/coverage`); } catch (e) { line.textContent = e.message; return; }
  const ok = c.coverage >= c.coverage_min;
  line.replaceChildren(el("span", { class: ok ? "ok" : "warn" },
    `Glare-weighted coverage ${Math.round(100 * c.coverage)} %`),
  ` of the forward field (target ${Math.round(100 * c.coverage_min)} %)`,
  !ok && c.proposal.frames.length ? ` · ${c.proposal.frames.length} more frame(s) proposed` : "");
  $("cov-img").src = `/api/positions/${seg(profile.name)}/coverage.png?t=${Date.now()}`;
  $("cov-prop").replaceChildren(...c.proposal.frames.map(([p, t], i) => el("li", {},
    el("span", {}, `tile ${i + 1}: pan ${deg(p)}°, tilt ${deg(t)}°`),
    el("button", { class: "quiet go", onclick: () => act(() => api("POST", "/api/mount/goto", { pan: p, tilt: t }),
      `going to tile ${i + 1}`) }, "Go"))));
}

function renderProfile() {
  $("delete-profile").disabled = !profile;
  $("copy-profile").disabled = !profile;
  $("edit-profile").disabled = !profile;
  const info = $("profile-info");
  if (!profile) {
    info.replaceChildren();
    $("pos-rows").replaceChildren(el("tr", { class: "empty" },
      el("td", { colspan: 5 }, "Create a profile to record positions in.")));
    return;
  }
  const opt = $("profile").selectedOptions[0];
  if (opt) opt.textContent = `${profile.name}${profile.vehicle ? ` · ${profile.vehicle}` : ""} (${profile.positions.length})`;
  const g = profile.forward_gaze;
  info.replaceChildren(
    el("dt", {}, "Vehicle"), el("dd", {}, profile.vehicle || "—"),
    el("dt", {}, "Zero reference"), el("dd", {}, profile.zero_reference || "— (say where the fiducial is)"),
    el("dt", {}, "Forward gaze"), el("dd", {}, g ? `${deg(g[0])}°, ${deg(g[1])}°` : "not set"),
  );
  const rows = profile.positions.map((p, i) => el("tr", { "data-name": p.name },
    el("td", { class: "num muted" }, i + 1),
    el("td", { class: "name" }, p.name),
    el("td", { class: "num" }, deg(p.pan_deg)),
    el("td", { class: "num" }, deg(p.tilt_deg)),
    el("td", { class: "act" },
      el("button", { class: "go", title: "Go to this position", onclick: () => gotoPosition(p.name) }, "Go"),
      el("button", { class: "masks-btn", title: maskTitle(p), onclick: () => openMasks(p) },
        "Masks", el("small", {}, maskBadge(p))),
      el("button", { class: "quiet", title: "Earlier", disabled: i === 0, onclick: () => move(i, -1) }, "↑"),
      el("button", { class: "quiet", title: "Later", disabled: i === profile.positions.length - 1,
        onclick: () => move(i, 1) }, "↓"),
      el("button", { class: "quiet", title: "Delete", onclick: () => deletePosition(p.name) }, "✕")),
  ));
  $("pos-rows").replaceChildren(...(rows.length ? rows : [el("tr", { class: "empty" },
    el("td", { colspan: 5 }, "No positions yet: aim, name it below, save."))]));
  $("save-form").elements.name.placeholder = `Name for the current view (e.g. pos ${profile.positions.length + 1})`;
  if (st) render();
}

function maskTotal(p) {
  return p.masks ? p.masks.exclude + p.masks.glazing + p.masks.include : 0;
}

function maskBadge(p) {
  if (!p.has_image) return "no image";
  return maskTotal(p) ? String(maskTotal(p)) : "";
}

function maskTitle(p) {
  const m = p.masks || {};
  return `${p.has_image ? "Reference image saved" : "No reference image yet"}; `
    + `${m.exclude || 0} exclusion, ${m.glazing || 0} glazing, ${m.include || 0} luminaire shape(s)`;
}

function isHere(p) {
  return !!st && st.zeroed && Math.abs(p.pan_deg - st.pan_deg) < HERE_DEG
    && Math.abs(p.tilt_deg - st.tilt_deg) < HERE_DEG;
}

function openMasks(p) {
  openMaskEditor({ profile: profile.name, position: p, here: isHere(p), say, onSaved: loadProfile });
}

// Masks of the position the head is at, cached by version, drawn over live view.
const maskCache = new Map();   // position id -> {version, doc}

function hereMasks() {
  if (!profile || !st || !st.zeroed) return null;
  const p = profile.positions.find(isHere);
  if (!p || !maskTotal(p)) return null;
  const hit = maskCache.get(p.id);
  if (hit && hit.version === p.masks.version) return hit.doc;   // current, or on its way
  maskCache.set(p.id, { version: p.masks.version, doc: null });
  api("GET", `/api/positions/${seg(profile.name)}/${seg(p.name)}/masks`)
    .then((doc) => { maskCache.set(p.id, { version: doc.version, doc }); drawOverlay(); })
    .catch(() => maskCache.delete(p.id));
  return null;
}

function markHere() {
  if (!profile || !st) return;
  for (const tr of document.querySelectorAll("#pos-rows tr[data-name]")) {
    const p = profile.positions.find((q) => q.name === tr.dataset.name);
    tr.classList.toggle("here", !!p && st.zeroed && Math.abs(p.pan_deg - st.pan_deg) < HERE_DEG
      && Math.abs(p.tilt_deg - st.tilt_deg) < HERE_DEG);
  }
}

const P = () => `/api/positions/${seg(profile.name)}`;

function gotoPosition(name) {
  act(() => api("POST", `${P()}/${seg(name)}/goto`, {}));
}

async function move(i, d) {
  const names = profile.positions.map((p) => p.name);
  [names[i], names[i + d]] = [names[i + d], names[i]];
  const r = await act(() => api("POST", `${P()}/order`, { names }));
  if (r) { profile = r; renderProfile(); }
}

async function deletePosition(name) {
  if (!confirm(`Delete position “${name}” from ${profile.name}?`)) return;
  const r = await act(() => api("DELETE", `${P()}/${seg(name)}`), `deleted “${name}”`);
  if (r) { profile = r; renderProfile(); }
}

// ---------------------------------------------------------------- wiring

function wire() {
  for (const b of document.querySelectorAll(".pad [data-dir]")) {
    b.addEventListener("pointerdown", (e) => {
      if (b.disabled) return;
      e.preventDefault();
      // Capture keeps the press when a finger slides off the button; release still ends it.
      try { b.setPointerCapture(e.pointerId); } catch { /* pointer already gone */ }
      press(b.dataset.dir);
    });
    for (const ev of ["pointerup", "pointercancel", "lostpointercapture"]) b.addEventListener(ev, release);
    b.addEventListener("contextmenu", (e) => e.preventDefault());
  }
  $("pad-stop").addEventListener("click", stopAll);
  $("stop").addEventListener("click", stopAll);
  $("estop").addEventListener("click", () => act(() => api("POST", "/api/mount/estop"),
    "emergency stop: position is now an estimate, set the zero again"));

  for (const b of document.querySelectorAll(".seggroup button")) {
    b.addEventListener("click", () => setMode(b.dataset.mode));
  }
  setMode(mode);

  const speed = $("speed");
  speed.addEventListener("input", () => { speedEditing = Date.now(); showSpeed(); });
  speed.addEventListener("change", () => {
    speedEditing = Date.now();
    act(() => api("POST", "/api/mount/speed", { fraction: Number(speed.value) / 100 }));
  });

  $("enable").addEventListener("click", () => act(() => api("POST", "/api/mount/enable"), "drivers on"));
  $("disable").addEventListener("click", () => {
    if (confirm("Release the drivers? The head can then move by hand, so the zero is dropped.")) {
      act(() => api("POST", "/api/mount/disable"), "drivers released, zero dropped");
    }
  });
  $("zero").addEventListener("click", () => {
    if (st && st.zeroed && !confirm("Replace the current zero by eye? Saved positions refer to the zero "
      + "they were recorded with, so only do this at the same fiducial.")) return;
    act(() => api("POST", "/api/mount/zero", {}));
  });
  $("zero-optical").addEventListener("click", () => {
    if (st && st.zeroed && st.zero && st.zero.method === "optical" && !confirm(
      "Zero on the fiducial again? Do it at the same fiducial: saved positions refer to it.")) return;
    act(() => api("POST", "/api/mount/zero/optical", {}), "centroiding the fiducial…");
  });
  $("zero-check").addEventListener("click", () => act(() => api("POST", "/api/mount/zero/check", {}),
    "back to (0, 0) to measure the fiducial…"));
  $("backlash").addEventListener("click", () => act(() => api("POST", "/api/mount/backlash", {})));

  $("goto").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = e.target.elements;
    act(() => api("POST", "/api/mount/goto", { pan: Number(f.pan.value), tilt: Number(f.tilt.value) }));
  });

  $("profile").addEventListener("change", loadProfile);
  const form = $("profile-form");
  const openForm = (editing, copying = false) => {
    form.reset();
    form.dataset.mode = editing ? "edit" : copying ? "copy" : "create";
    form.elements.name.disabled = editing;   // renaming would orphan results that name it
    $("profile-submit").textContent = editing ? "Save details" : copying ? `Copy “${profile.name}”` : "Create";
    if (copying) {
      form.elements.vehicle.value = profile.vehicle;
      form.elements.zero_reference.value = profile.zero_reference;
    }
    if (editing) {
      form.elements.name.value = profile.name;
      form.elements.vehicle.value = profile.vehicle;
      form.elements.zero_reference.value = profile.zero_reference;
    }
    form.hidden = false;
    (editing ? form.elements.vehicle : form.elements.name).focus();
  };
  $("new-profile").addEventListener("click", () => openForm(false));
  $("copy-profile").addEventListener("click", () => { if (profile) openForm(false, true); });
  $("edit-profile").addEventListener("click", () => { if (profile) openForm(true); });
  $("cancel-profile").addEventListener("click", () => { form.hidden = true; });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = form.elements;
    const r = form.dataset.mode === "edit"
      ? await act(() => api("PATCH", P(), {
        vehicle: f.vehicle.value, zero_reference: f.zero_reference.value,
      }), `updated “${profile.name}”`)
      : await act(() => api("POST", "/api/positions", {
        name: f.name.value, vehicle: f.vehicle.value, zero_reference: f.zero_reference.value,
        copy_from: form.dataset.mode === "copy" ? profile.name : null,
      }), form.dataset.mode === "copy" ? `copied “${profile.name}” to “${f.name.value}”`
        : `created profile “${f.name.value}”`);
    if (r) {
      form.reset();
      form.hidden = true;
      await loadProfiles(r.name);
    }
  });
  $("delete-profile").addEventListener("click", async () => {
    if (!profile || !confirm(`Move profile “${profile.name}” to the trash?`)) return;
    const r = await act(() => api("DELETE", P()), `moved “${profile.name}” to the trash`);
    if (r) await loadProfiles();
  });
  $("save-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const name = e.target.elements.name.value.trim();
    if (!profile || !name) return;
    act(() => api("POST", `${P()}/save`, { name })).then((r) => { if (r) e.target.reset(); });
  });
  $("gaze").addEventListener("click", async () => {
    const r = await act(() => api("POST", `${P()}/gaze`), "forward gaze set to the current view");
    if (r) { profile = r; renderProfile(); }
  });
  $("run").addEventListener("click", () => act(() => api("POST", `${P()}/run`, {})));

  $("opt-live").addEventListener("change", () => st && render());
  $("opt-outlines").addEventListener("change", drawOverlay);
  $("live").addEventListener("error", liveError);
  $("live").addEventListener("load", drawOverlay);

  // Keyboard: arrows move (held = jog in hold mode), space / Escape stop, H toggles mode.
  const keyDir = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" };
  const typing = (t) => t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement || t instanceof HTMLSelectElement;
  document.addEventListener("keydown", (e) => {
    // The mask editor has its own keys; the head must not move while it is open.
    if ($("mask-dialog").open) return;
    if (typing(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
    if (keyDir[e.key]) {
      e.preventDefault();
      if (!e.repeat && st && st.enabled) press(keyDir[e.key]);
    } else if (e.key === " " || e.key === "Escape") {
      e.preventDefault();
      stopAll();
    } else if (e.key === "h" || e.key === "H") {
      setMode(mode === "hold" ? remember("ugr.step") || "1" : "hold");
    }
  });
  document.addEventListener("keyup", (e) => { if (keyDir[e.key]) release(); });
  window.addEventListener("blur", () => jogEnd());
  document.addEventListener("visibilitychange", () => { if (document.hidden) jogEnd(); });

  new ResizeObserver(drawOverlay).observe($("live-box"));
  wireMaskEditor();
}

async function main() {
  wire();
  try { geom = await api("GET", "/api/camera/geometry"); } catch { /* keep the nominal FOV */ }
  $("live-box").style.aspectRatio = `${geom.width_px} / ${geom.height_px}`;
  await loadProfiles();
  poll();
}

main();
