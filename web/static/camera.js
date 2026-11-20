// Camera page: camera profiles, and k from the matt target against the meter.
//
// The calibration is offered, never applied by itself: the operator sees k, its drift from the
// stored value and the target's uniformity, then accepts it into the profile. A study within
// 12 h of an accepted calibration of its profile counts it as its calibration shot.

import { api, el, remember, seg } from "./api.js";

const $ = (id) => document.getElementById(id);
let st = null;
let prof = null;           // the selected profile
let roi = null;            // [x0, y0, x1, y1], normalised, from a drag on live view
let drag = null;
let lastMotion = null;
let pending = null;        // id of the calibration just shot
let bodyShown = false;     // the body info needs the camera idle: retried until it is

function say(msg, kind = "") {
  const t = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  $("log").prepend(el("li", {}, el("time", {}, t), el("span", { class: kind }, msg)));
}

async function act(fn, ok) {
  try { const r = await fn(); if (ok) say(ok); return r; } catch (e) { say(e.message, "bad"); return null; }
}

// ---------------------------------------------------------------- status

async function poll() {
  try {
    st = await api("GET", "/api/status");
    $("conn").classList.add("up");
    $("conn").textContent = "connected";
    renderLive();
    if (!bodyShown && st.state === "idle" && st.live_view.source !== "none") showBody();
    const m = st.last_motion;
    if (m && m.kind === "calibrate" && m.id !== lastMotion) {
      if (lastMotion !== null) showResult(m);
      lastMotion = m.id;
    } else if (m && lastMotion === null) {
      lastMotion = m.id;
    }
    $("shoot").disabled = st.state !== "idle" || !roi || !prof || st.live_view.source === "none";
    $("shoot").textContent = st.state === "calibrate" ? "Shooting and measuring…" : "Shoot calibration bracket";
  } catch {
    $("conn").classList.remove("up");
    $("conn").textContent = "no server";
  }
  setTimeout(poll, 500);
}

function renderLive() {
  const img = $("live"), lv = st.live_view;
  if (lv.source !== "none" && !img.getAttribute("src")) img.src = `/api/live.mjpg?t=${Date.now()}`;
  $("live-msg").textContent = lv.source === "none" ? "No camera."
    : lv.state === "error" ? `Camera: ${lv.error}`
      : lv.state === "paused" ? "Live view paused while the camera shoots." : "";
}

// ---------------------------------------------------------------- ROI on live view

function imageRect() {
  const box = $("live-box"), img = $("live");
  const bw = box.clientWidth, bh = box.clientHeight;
  const iw = img.naturalWidth || 3, ih = img.naturalHeight || 2;
  const s = Math.min(bw / iw, bh / ih);
  return { x: (bw - iw * s) / 2, y: (bh - ih * s) / 2, w: iw * s, h: ih * s };
}

function norm(e) {
  const b = $("live-box").getBoundingClientRect(), r = imageRect();
  return [Math.min(1, Math.max(0, (e.clientX - b.left - r.x) / r.w)),
    Math.min(1, Math.max(0, (e.clientY - b.top - r.y) / r.h))];
}

function drawRoi() {
  const box = $("live-box"), cv = $("overlay"), dpr = window.devicePixelRatio || 1;
  cv.width = Math.round(box.clientWidth * dpr);
  cv.height = Math.round(box.clientHeight * dpr);
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, box.clientWidth, box.clientHeight);
  const r = imageRect();
  const cx = r.x + r.w / 2, cy = r.y + r.h / 2;
  g.strokeStyle = "rgba(255,196,64,.8)";
  g.beginPath(); g.moveTo(cx - 14, cy); g.lineTo(cx + 14, cy); g.moveTo(cx, cy - 14); g.lineTo(cx, cy + 14); g.stroke();
  const q = drag ? [Math.min(drag[0], drag[2]), Math.min(drag[1], drag[3]), Math.max(drag[0], drag[2]), Math.max(drag[1], drag[3])] : roi;
  if (!q) return;
  g.strokeStyle = "#5ad1ff";
  g.lineWidth = 2;
  g.fillStyle = "rgba(90,209,255,.12)";
  g.fillRect(r.x + q[0] * r.w, r.y + q[1] * r.h, (q[2] - q[0]) * r.w, (q[3] - q[1]) * r.h);
  g.strokeRect(r.x + q[0] * r.w, r.y + q[1] * r.h, (q[2] - q[0]) * r.w, (q[3] - q[1]) * r.h);
}

function wireRoi() {
  const box = $("live-box");
  box.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    try { box.setPointerCapture(e.pointerId); } catch { /* gone */ }
    const [x, y] = norm(e);
    drag = [x, y, x, y];
    drawRoi();
  });
  box.addEventListener("pointermove", (e) => { if (drag) { [drag[2], drag[3]] = norm(e); drawRoi(); } });
  box.addEventListener("pointerup", () => {
    if (!drag) return;
    const q = [Math.min(drag[0], drag[2]), Math.min(drag[1], drag[3]), Math.max(drag[0], drag[2]), Math.max(drag[1], drag[3])];
    drag = null;
    if (q[2] - q[0] < 0.005 || q[3] - q[1] < 0.005) { say("box too small: drag across the target", "warn"); drawRoi(); return; }
    roi = q.map((v) => Number(v.toFixed(4)));
    remember("ugr.cal.roi", JSON.stringify(roi));
    $("cal-roi").textContent = `${(100 * (roi[2] - roi[0])).toFixed(1)} × ${(100 * (roi[3] - roi[1])).toFixed(1)} % of the frame`;
    drawRoi();
  });
  new ResizeObserver(drawRoi).observe(box);
  $("live").addEventListener("load", drawRoi);
}

// ---------------------------------------------------------------- calibration

async function shoot() {
  const lux = document.querySelector('input[name="meter"]:checked').value === "lux";
  const body = { profile: prof.name, roi };
  if (lux) { body.lux = Number($("lux").value); body.rho = Number($("rho").value); } else { body.luminance = Number($("lum").value); }
  if ((lux && !(body.lux > 0 && body.rho > 0 && body.rho <= 1)) || (!lux && !(body.luminance > 0))) {
    say("enter the meter reading first", "warn");
    return;
  }
  $("cal-result").hidden = true;
  await act(() => api("POST", "/api/camera/calibrate", body), "shooting the target…");
}

function pct(x) { return x === null || x === undefined ? "—" : `${x >= 0 ? "+" : "−"}${Math.abs(100 * x).toFixed(1)} %`; }

function showResult(m) {
  const box = $("cal-result");
  box.hidden = false;
  if (m.error) {
    box.className = "cal-result bad";
    box.replaceChildren(el("strong", {}, "Calibration failed: "), m.error.detail,
      el("p", { class: "hint" }, "Clipped pixels: move the box off highlights or lower the light. No signal: the box missed the target."));
    say(`calibration failed: ${m.error.detail}`, "bad");
    return;
  }
  const r = m.result, k = r.measurement;
  pending = r.id;
  const big = k.drift !== null && Math.abs(k.drift) > 0.10;
  box.className = `cal-result ${big ? "warn" : "ok"}`;
  box.replaceChildren(
    el("div", { class: "kbig" }, `k = ${k.k.toPrecision(5)}`),
    el("div", {}, k.k_stored ? `stored ${k.k_stored.toPrecision(5)}, drift ${pct(k.drift)}` : "profile not calibrated before"),
    el("div", { class: "muted small" }, `target ${k.target_cd_m2.toFixed(1)} cd/m² · uniformity in the box ${(100 * k.cv).toFixed(1)} % · ${k.n_px} px`),
    big ? el("p", { class: "warn" }, "More than 10 % from the stored value: lens, aperture, a filter or in-camera processing changed? Check before accepting.") : "",
    k.cv > 0.05 ? el("p", { class: "warn" }, "The box is not uniform (> 5 %): is it fully on the target?") : "",
    ...(r.body_mismatch || []).map((x) => el("p", { class: "warn" }, x)),
    el("div", { class: "row" }, el("button", { class: "primary", onclick: () => accept(r.id) }, "Accept into the profile"),
      el("button", { class: "quiet", onclick: () => { box.hidden = true; } }, "Discard")));
  say(`measured k ${k.k.toPrecision(5)}${k.k_stored ? `, drift ${pct(k.drift)}` : ""}`);
  loadCalibrations();
}

async function accept(id) {
  const p = await act(() => api("POST", `/api/camera/profiles/${seg(prof.name)}/accept`, { calibration: id }),
    "k accepted into the profile");
  if (p) { $("cal-result").hidden = true; await loadProfiles(p.name); }
}

async function loadCalibrations() {
  if (!prof) return;
  const rows = await api("GET", `/api/camera/calibrations?profile=${encodeURIComponent(prof.name)}`).catch(() => []);
  $("cal-rows").replaceChildren(...(rows.length ? rows.map((r) => el("tr", {},
    el("td", { class: "muted" }, r.at.replace("T", " ").replace("Z", "")),
    el("td", { class: "num" }, r.measurement.k.toPrecision(5)),
    el("td", { class: "num" }, pct(r.measurement.drift)),
    el("td", { class: "num" }, `${(100 * r.measurement.cv).toFixed(1)} %`),
    el("td", {}, r.accepted_at ? "accepted" : el("button", { class: "quiet", onclick: () => accept(r.id) }, "Accept"))))
    : [el("tr", { class: "empty" }, el("td", { colspan: 5 }, "No calibrations yet."))]));
}

// ---------------------------------------------------------------- profiles

async function loadProfiles(select) {
  const list = await api("GET", "/api/camera/profiles");
  const sel = $("profile");
  const keep = select || sel.value || remember("ugr.camprof") || (list.find((p) => p.active) || {}).name;
  sel.replaceChildren(...list.map((p) => el("option", { value: p.name },
    `${p.name}${p.active ? " (used for studies)" : ""} · ${p.k ? `k ${p.k.toPrecision(4)}` : "not calibrated"}`)));
  if (list.some((p) => p.name === keep)) sel.value = keep;
  await loadProfile();
}

async function loadProfile() {
  prof = await api("GET", `/api/camera/profiles/${seg($("profile").value)}`);
  remember("ugr.camprof", prof.name);
  const f = $("prof-form").elements;
  for (const k of ["notes", "body_serial", "lens_name", "focal_mm", "distortion_k1", "vignetting_map",
    "aperture", "iso", "shutter_model", "spectral_factor"]) f[k].value = prof[k];
  f.shutter.value = prof.shutter.join(", ");
  f.spectral_factors.value = Object.entries(prof.spectral_factors).map(([k, v]) => `${k} = ${v}`).join("\n");
  $("activate").disabled = prof.active;
  $("cal-profile").textContent = prof.name;
  $("cal-stored").textContent = !prof.k ? "not calibrated"
    : `${prof.k.toPrecision(5)} · ${SOURCE[prof.k_source] || prof.k_source}`
      + (prof.k_measured_at ? ` ${utc(prof.k_measured_at)} UTC` : "")
      + (prof.k_note ? ` · ${prof.k_note}` : "");
  $("k-rows").replaceChildren(...(prof.k_history.length ? [...prof.k_history].reverse().map((r) => el("tr", {},
    el("td", { class: "muted" }, utc(r.at)),
    el("td", { class: "num" }, r.k.toPrecision(5)),
    el("td", { class: "num" }, pct(r.drift)),
    el("td", {}, r.source === "typed" ? `typed: ${r.note}`
      : `target ${r.target_cd_m2 === null ? "" : `${r.target_cd_m2.toFixed(0)} cd/m²`}`)))
    : [el("tr", { class: "empty" }, el("td", { colspan: 4 },
      prof.k ? "k from the config template, never measured or typed here." : "No k yet."))]));
  $("geometry").src = `/api/camera/profiles/${seg(prof.name)}/geometry.png?f=${prof.focal_mm}&k=${prof.distortion_k1}`;
  loadCalibrations();
}

const SOURCE = { target: "measured on the target", typed: "typed by hand", config: "from the config template" };

function utc(iso) { return iso.replace("T", " ").replace("Z", ""); }

async function setTypedK(e) {
  e.preventDefault();
  const f = e.target.elements;
  const k = Number(f.k.value), note = f.note.value.trim();
  if (!(k > 0) || !note) { say("enter k and where it comes from", "warn"); return; }
  if (prof.k && Math.abs(k - prof.k) / prof.k > 1e-9) {
    const change = pct((k - prof.k) / prof.k);
    if (!confirm(`Replace the stored k ${prof.k.toPrecision(5)} (${SOURCE[prof.k_source] || prof.k_source}) `
      + `with ${k.toPrecision(5)} (${change})?\n\nThe typed value is used for every study from now on. `
      + "The old value stays in the history.")) return;
  }
  const p = await act(() => api("POST", `/api/camera/profiles/${seg(prof.name)}/k`, { k, note }),
    `k set by hand to ${k.toPrecision(5)}`);
  if (p) { e.target.reset(); await loadProfiles(p.name); }
}

function parseFactors(text) {
  const out = {};
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    const m = line.match(/^\s*([\w-]+)\s*[=:]\s*([\d.]+)\s*$/);
    if (!m) throw new Error(`spectral factor line not understood: "${line.trim()}" (type = factor)`);
    out[m[1]] = Number(m[2]);
  }
  return out;
}

async function saveProfile(e) {
  e.preventDefault();
  const f = e.target.elements;
  let body;
  try {
    body = {
      notes: f.notes.value, body_serial: f.body_serial.value.trim(), lens_name: f.lens_name.value.trim(),
      focal_mm: Number(f.focal_mm.value), distortion_k1: Number(f.distortion_k1.value),
      vignetting_map: f.vignetting_map.value.trim(), aperture: Number(f.aperture.value),
      iso: Number(f.iso.value), shutter: f.shutter.value.split(",").map((s) => s.trim()).filter(Boolean),
      shutter_model: f.shutter_model.value, spectral_factor: Number(f.spectral_factor.value),
      spectral_factors: parseFactors(f.spectral_factors.value),
    };
  } catch (err) { say(err.message, "bad"); return; }
  const p = await act(() => api("PATCH", `/api/camera/profiles/${seg(prof.name)}`, body), `saved ${prof.name}`);
  if (p) await loadProfiles(p.name);
}

function wire() {
  wireRoi();
  try { roi = JSON.parse(remember("ugr.cal.roi") || "null"); } catch { roi = null; }
  if (roi) $("cal-roi").textContent = "the box from last time";
  for (const r of document.querySelectorAll('input[name="meter"]')) {
    r.addEventListener("change", () => {
      const lux = r.value === "lux" && r.checked;
      $("meter-e").hidden = !lux;
      $("meter-l").hidden = lux;
    });
  }
  $("shoot").addEventListener("click", shoot);
  $("profile").addEventListener("change", loadProfile);
  $("prof-form").addEventListener("submit", saveProfile);
  $("typed-k").addEventListener("submit", setTypedK);
  $("activate").addEventListener("click", async () => {
    if (await act(() => api("POST", `/api/camera/profiles/${seg(prof.name)}/activate`), `${prof.name} is used for studies now`)) loadProfiles(prof.name);
  });
  $("new-prof").addEventListener("click", async () => {
    const name = prompt(`Name of the new profile (a copy of ${prof.name}, uncalibrated):`);
    if (!name) return;
    const p = await act(() => api("POST", "/api/camera/profiles", { name, copy_from: prof.name }), `created ${name}`);
    if (p) loadProfiles(p.name);
  });
  $("del-prof").addEventListener("click", async () => {
    if (!confirm(`Move camera profile ${prof.name} to the trash?`)) return;
    if (await act(() => api("DELETE", `/api/camera/profiles/${seg(prof.name)}`), `${prof.name} moved to the trash`)) loadProfiles();
  });
  $("take-body").addEventListener("click", async () => {
    const p = await act(() => api("POST", `/api/camera/profiles/${seg(prof.name)}/body`), "body and lens taken from the camera");
    if (p) { await loadProfiles(p.name); showBody(); }
  });
}

async function showBody() {
  try {
    bodyShown = true;
    const r = await api("GET", "/api/camera/info");
    $("body-info").replaceChildren(
      el("dt", {}, "Camera now"), el("dd", {}, `${r.info.model || "?"} · serial ${r.info.serial || "?"}`),
      el("dt", {}, "Lens now"), el("dd", {}, r.info.lens || "?"),
      ...(r.mismatch.length ? [el("dt", {}, "Check"), el("dd", { class: "warn" }, r.mismatch.join("; "))] : []));
  } catch (e) {
    bodyShown = !/progress|busy/.test(e.message);
    $("body-info").replaceChildren(el("dt", {}, "Camera"), el("dd", { class: "muted" }, e.message));
  }
}

async function main() {
  wire();
  await loadProfiles();
  poll();
}

main();
