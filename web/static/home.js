// Home: start a study, follow it step by step, see each position's result, browse the library.
//
// Recording runs on the server; this page only starts it and watches. Closing the tab or
// losing WiFi does not stop a study (unlike a jog): the study's record is on the Pi, and the
// page picks it up again when reopened.

import { api, el, remember, seg } from "./api.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 1000;
const FINAL = new Set(["done", "stopped", "failed", "interrupted"]);
const VIEWS = { sources_map: "Sources", photo: "Photo", luminance_map: "Luminance",
  contribution_map: "Contribution", masks_map: "Masks" };
let view = remember("ugr.view") || "sources_map";
if (!(view in VIEWS)) view = "sources_map";

let st = null;            // /api/status
let profiles = [];
let camProfiles = [];
let selected = remember("ugr.study");
let study = null;
let studyEtag = "";

// ---------------------------------------------------------------- helpers

function fmtUgr(x) { return x === null || x === undefined ? "—" : x.toFixed(1); }

function ugrClass(x) {
  if (x === null || x === undefined) return "";
  return x > 22 ? "bad" : x > 19 ? "warn" : "ok";   // CIE 117: 19 just acceptable, 22 unacceptable
}

function when(iso) { return iso ? iso.replace("T", " ").replace("Z", "") : "—"; }

function setConn(up, why = "") {
  const c = $("conn");
  c.classList.toggle("up", up);
  c.textContent = up ? "connected" : "no server";
  c.title = why;
}

// ---------------------------------------------------------------- polling

async function poll() {
  try {
    st = await api("GET", "/api/status");
    setConn(true);
    if (st.state === "study" && st.motion && st.motion.detail.study !== selected) {
      select(st.motion.detail.study);   // a study started elsewhere: follow it
    }
    renderPreflight();
    // Keep reloading until the study itself says it is finished: the server stops listing
    // it as active a moment before its last save lands.
    if (selected && (!study || !FINAL.has(study.status) || isActive(selected))) await loadStudy();
  } catch (e) {
    setConn(false, e.message);
  }
  setTimeout(poll, POLL_MS);
}

function isActive(sid) {
  return !!st && (sid in (st.studies_active || {})
    || (st.state === "study" && st.motion && st.motion.detail.study === sid));
}

// ---------------------------------------------------------------- start

async function loadProfiles() {
  try { profiles = await api("GET", "/api/positions"); } catch { profiles = []; }
  const sel = $("profile");
  const keep = sel.value || remember("ugr.profile");
  sel.replaceChildren(...(profiles.length
    ? profiles.map((p) => el("option", { value: p.name },
      `${p.name}${p.vehicle ? ` · ${p.vehicle}` : ""} (${p.positions} positions)`))
    : [el("option", { value: "" }, "No profiles: record positions on the Movement page")]));
  if (profiles.some((p) => p.name === keep)) sel.value = keep;
  try { camProfiles = await api("GET", "/api/camera/profiles"); } catch { camProfiles = []; }
  const cs = $("camprof");
  const active = (camProfiles.find((p) => p.active) || {}).name;
  cs.replaceChildren(...camProfiles.map((p) => el("option", { value: p.name },
    `${p.name} · ${p.k ? `k ${p.k.toPrecision(4)}` : "not calibrated"}`)));
  if (active) cs.value = active;
}

function renderPreflight() {
  const prof = profiles.find((p) => p.name === $("profile").value);
  const checks = [
    [st.enabled, "drivers on"],
    [st.zeroed && st.certain, st.zeroed && !st.certain ? "zero uncertain: set it again" : "zero set"],
    [st.live_view.source !== "none", st.live_view.source === "none" ? "no camera"
      : `camera: ${st.live_view.source === "sim" ? "simulated" : "2000D"}`],
    camCheck(),
    [!!prof && prof.positions > 0, prof ? `${prof.positions} position(s) in the profile` : "a profile with positions"],
    [st.state === "idle", st.state === "idle" ? "head idle" : `head busy: ${st.state}`],
  ];
  const ok = checks.every(([good]) => good);
  $("preflight").replaceChildren(...checks.map(([good, text]) =>
    el("li", { class: good ? "ok" : "bad" }, good ? "✓ " : "✕ ", text,
      !good && /drivers|zero/.test(text) ? el("a", { href: "/movement" }, " → Movement") : "")));
  $("start").disabled = !ok;
  const d = st.disk;
  $("disk").textContent = d ? `Data disk: ${d.free_gb} GB free of ${d.total_gb} GB`
    + " (a simulated study is a few MB; a real one ~200 MB per position)" : "";
}

function camCheck() {
  const c = camProfiles.find((p) => p.name === $("camprof").value);
  if (!c) return [false, "a camera profile"];
  if (c.k) return [true, c.k_source === "typed" ? `camera profile: k ${c.k.toPrecision(4)}, typed by hand`
    : c.k_source === "iso" ? `camera profile: k ${c.k.toPrecision(4)}, estimated from the ISO rating (±30 %)`
      : `camera profile calibrated (k ${c.k.toPrecision(4)})`];
  // Uncalibrated is allowed (the simulator knows its k; a real study is flagged), but shown.
  return [st.live_view.source === "sim", st.live_view.source === "sim"
    ? "camera profile not calibrated (the simulator uses its own k)" : "camera profile not calibrated"];
}

async function start(e) {
  e.preventDefault();
  const f = e.target.elements;
  remember("ugr.profile", f.profile.value);
  try {
    const r = await api("POST", "/api/studies", {
      profile: f.profile.value, lighting: f.lighting.value, operator: f.operator.value,
      notes: f.notes.value, sweep: f.sweep.checked, check: f.check.checked,
      camera_profile: f.camera_profile.value || null,
    });
    select(r.study);
    await loadLibrary();
  } catch (err) {
    alert(err.message);
  }
}

// ---------------------------------------------------------------- one study

function select(sid) {
  selected = sid;
  study = null;
  studyEtag = "";
  remember("ugr.study", sid);
  loadStudy();
  for (const tr of document.querySelectorAll("#library-rows tr")) {
    tr.classList.toggle("here", tr.dataset.id === sid);
  }
}

async function loadStudy() {
  if (!selected) return;
  try {
    const s = await api("GET", `/api/studies/${seg(selected)}`);
    const tag = JSON.stringify([s.status, s.steps, s.summary, s.positions.map((p) => p.state), s.check && s.check.state]);
    if (tag === studyEtag) return;      // nothing new: keep the images still
    const statusChanged = study && study.status !== s.status;
    studyEtag = tag;
    study = s;
    renderStudy();
    renderCards();
    if (statusChanged) loadLibrary();
  } catch (e) {
    if (e.status === 404) { selected = null; remember("ugr.study", ""); }
  }
}

function renderStudy() {
  const s = study;
  $("study-empty").hidden = !!s;
  $("study-body").hidden = !s;
  if (!s) return;
  $("study-title").textContent = s.id;
  const pill = $("study-status");
  pill.textContent = s.status;
  pill.className = `pill ${{ done: "ok", recording: "busy", analysing: "busy",
    stopped: "warn", interrupted: "warn", failed: "bad" }[s.status] || ""}`;
  $("study-meta").replaceChildren(
    el("dt", {}, "Profile"), el("dd", {}, `${s.profile.name}${s.profile.vehicle ? ` · ${s.profile.vehicle}` : ""}`),
    el("dt", {}, "Lighting"), el("dd", {}, s.lighting),
    el("dt", {}, "Operator"), el("dd", {}, s.operator || "—"),
    el("dt", {}, "Started"), el("dd", {}, `${when(s.started_at)} UTC`),
    ...(s.notes ? [el("dt", {}, "Notes"), el("dd", {}, s.notes)] : []),
    el("dt", {}, "Software"), el("dd", { class: "muted" }, s.software && s.software.commit
      ? `${s.software.commit.slice(0, 10)}${s.software.dirty ? " (uncommitted changes)" : ""}` : "unknown"),
  );
  $("steps").replaceChildren(...s.steps.map((x) => el("li", { class: `step ${x.state}` },
    el("span", { class: "light", title: x.state }), el("span", { class: "label" }, x.label),
    el("span", { class: "detail muted" }, x.detail || x.state))));

  const sm = s.summary || {};
  const lb = s.sweep && s.sweep.result;
  const tiles = [
    ["Max UGR", fmtUgr(sm.ugr_max), ugrClass(sm.ugr_max), sm.ugr_max_position || ""],
    ["Over 19", sm.n_rated ? `${sm.n_over_19} / ${sm.n_rated}` : "—", sm.n_over_19 ? "warn" : "", "positions"],
    ["Over 22", sm.n_rated ? `${sm.n_over_22} / ${sm.n_rated}` : "—", sm.n_over_22 ? "bad" : "", "positions"],
    ["Check ΔUGR", sm.check_delta_ugr === null || sm.check_delta_ugr === undefined ? "—"
      : (sm.check_delta_ugr >= 0 ? "+" : "−") + Math.abs(sm.check_delta_ugr).toFixed(2), "", "repeatability"],
    ["L_b", lb ? `${lb.L_b.toFixed(2)}` : "frame", "", lb ? `cd/m² · ${Math.round(100 * lb.coverage_cos_weighted)} % covered` : "provisional"],
    ["Drift", s.drift ? `${s.drift.offset_deg.toFixed(3)}°` : "—", s.drift && s.drift.offset_deg > s.drift.limit_deg ? "bad" : "",
      s.drift ? `limit ${s.drift.limit_deg}° · zero ${s.zero ? `corrected ${s.zero.offset_deg.toFixed(2)}°` : "by hand"}`
        : s.zero ? "not measured" : "no optical zero"],
  ];
  $("summary").replaceChildren(...tiles.map(([k, v, cls, sub]) => el("div", { class: "tile" },
    el("span", { class: "k" }, k), el("span", { class: `v ${cls}` }, v), el("span", { class: "sub muted" }, sub))));
  $("study-flags").replaceChildren(...s.flags.map((f) => el("li", {}, f)));

  $("stop").hidden = s.status !== "recording";
  $("zip").href = `/api/studies/${seg(s.id)}/results.zip`;
  const xlsx = s.outputs && s.outputs.excel;
  $("excel").hidden = !xlsx;
  if (xlsx) $("excel").href = `/api/studies/${seg(s.id)}/files/${xlsx}`;
  $("reanalyse").hidden = !FINAL.has(s.status);
  const n = (s.analyses || []).length;
  $("reanalyse").title = n ? `Analysed ${n + 1} times; earlier results are kept in the study folder`
    : "Analyse the recorded brackets again (after calibrating k, or an analysis change)";
}

function renderCards() {
  const s = study;
  if (!s) { $("cards").replaceChildren(); return; }
  const entries = [...s.positions, ...(s.check ? [s.check] : [])];
  $("cards").replaceChildren(...entries.map((p) => {
    const r = p.result;
    const file = r && r.outputs && (r.outputs[view] || r.outputs.sources_map);
    const img = file
      ? el("a", { href: `/api/studies/${seg(s.id)}/files/${file}`, target: "_blank",
        title: "Open full size" },
      el("img", { src: `/api/studies/${seg(s.id)}/files/${file}`, alt: `${VIEWS[view]} of ${p.name}`, loading: "lazy" }))
      : el("div", { class: "card-wait muted" }, p.error ? `failed: ${p.error}` : p.state);
    const flags = r ? r.flags : [];
    const top = r && r.sources && r.sources[0];
    const sens = r && r.metrics && r.metrics.threshold_sensitivity;
    return el("article", { class: `card${p === s.check ? " check" : ""}` },
      img,
      el("div", { class: "card-body" },
        el("div", { class: "card-head" },
          el("span", { class: "name" }, p.name),
          el("span", { class: `ugr ${ugrClass(r && r.ugr)}` }, r ? fmtUgr(r.ugr) : "")),
        el("div", { class: "muted small" }, r
          ? `${r.category} · L_b ${r.L_b.toFixed(2)} cd/m² (${r.lb_method}) · ${r.n_sources} source(s)`
          : `pan ${p.pan_deg.toFixed(2)}°, tilt ${p.tilt_deg.toFixed(2)}°`),
        p.aim ? el("div", { class: `small${p.aim.flag || !p.aim.conclusive ? " warn" : " muted"}`,
          title: "Live view at this position against its reference image (phase correlation)" },
        p.aim.conclusive ? `Aim ${p.aim.total_deg.toFixed(2)}° off the reference image` : "Aim check inconclusive") : "",
        top ? el("div", { class: "small" }, `Top source: ${top.name} · ${Math.round(100 * top.share)} % of the glare`
          + (top.delta_ugr !== null && top.delta_ugr !== undefined ? ` · −${top.delta_ugr.toFixed(1)} UGR without it` : "")) : "",
        sens && sens.length ? el("div", { class: "muted small", title: "UGR at other detection thresholds" },
          "Threshold " + sens.map((t) => `${t.threshold_cd_m2 >= 1000 ? `${t.threshold_cd_m2 / 1000}k` : t.threshold_cd_m2}: ${fmtUgr(t.ugr)}`).join(" · ")) : "",
        flags.length ? el("details", { class: "small" }, el("summary", {}, `${flags.length} flag(s)`),
          el("ul", {}, ...flags.map((f) => el("li", {}, f)))) : ""));
  }));
}

// ---------------------------------------------------------------- library

let searchTimer = null;

async function loadLibrary() {
  let rows = [];
  try { rows = await api("GET", `/api/studies?q=${encodeURIComponent($("search").value)}`); } catch { /* keep */ }
  $("library-rows").replaceChildren(...(rows.length ? rows.map((r) => el("tr", {
    "data-id": r.id, class: r.id === selected ? "here" : "", tabindex: "0",
    onclick: () => select(r.id),
    onkeydown: (e) => { if (e.key === "Enter") select(r.id); },
  },
  el("td", { class: "num muted" }, when(r.started_at)),
  el("td", {}, r.profile), el("td", {}, r.vehicle || "—"), el("td", {}, r.lighting),
  el("td", {}, r.operator || "—"),
  el("td", { class: `num ${ugrClass(r.ugr_max)}` }, fmtUgr(r.ugr_max)),
  el("td", {}, r.status + (r.n_flags ? ` · ${r.n_flags} flag(s)` : ""))))
    : [el("tr", { class: "empty" }, el("td", { colspan: 7 }, $("search").value ? "No study matches." : "No studies yet."))]));
}

// ---------------------------------------------------------------- wiring

function wire() {
  $("start-form").addEventListener("submit", start);
  $("profile").addEventListener("change", () => st && renderPreflight());
  $("camprof").addEventListener("change", () => st && renderPreflight());
  $("stop").addEventListener("click", () => api("POST", "/api/mount/stop").catch((e) => alert(e.message)));
  $("reanalyse").addEventListener("click", async () => {
    if (!study || !confirm("Analyse this study's recorded brackets again? The current results "
      + "are kept in the study folder under previous/.")) return;
    try {
      await api("POST", `/api/studies/${seg(study.id)}/reanalyse`);
      studyEtag = "";
      await loadStudy();
    } catch (e) { alert(e.message); }
  });
  const views = $("views");
  views.replaceChildren(...Object.entries(VIEWS).map(([k, label]) => el("button", {
    "data-view": k, "aria-pressed": String(k === view), onclick: () => {
      view = k;
      remember("ugr.view", k);
      for (const b of views.children) b.setAttribute("aria-pressed", String(b.dataset.view === k));
      renderCards();
    },
  }, label)));
  $("search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadLibrary, 250);
  });
}

async function main() {
  wire();
  await loadProfiles();
  await loadLibrary();
  if (selected) loadStudy();
  poll();
}

main();
