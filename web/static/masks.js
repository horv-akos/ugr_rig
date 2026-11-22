// Mask editor: draw a position's exclusion, glazing and luminaire polygons on its reference
// image. Coordinates are normalised 0..1 from the top left (CLAUDE.md), so the same polygons
// fit the 3000 x 2000 working frame and any binning.
//
//   exclude    never a glare source (e.g. a light outside the vehicle)
//   glazing    windscreen and side glass: daylight is excluded there; reflections of the
//              cab's own lights in it are a source in dark studies (not detected yet)
//   luminaire  a named, known light; its pixels form one source (inclusion ROI)

import { api, el, seg } from "./api.js";

export const KINDS = {
  exclude: { label: "Exclude", color: "242, 85, 75" },
  glazing: { label: "Glazing", color: "90, 209, 255" },
  include: { label: "Luminaire", color: "255, 196, 64" },
};
export const SOURCE_TYPES = ["white_led", "amber", "red", "display", "other"];

// Draw a masks document into `rect` (the picture's box on the canvas).
export function drawMasks(g, rect, doc, { fill = 0.18, highlight = null } = {}) {
  const shapes = toShapes(doc);
  shapes.forEach((s, i) => {
    const c = KINDS[s.kind].color;
    const pts = s.polygon.map(([x, y]) => [rect.x + x * rect.w, rect.y + y * rect.h]);
    g.beginPath();
    pts.forEach(([x, y], j) => (j ? g.lineTo(x, y) : g.moveTo(x, y)));
    g.closePath();
    g.fillStyle = `rgba(${c}, ${i === highlight ? fill * 2.2 : fill})`;
    g.fill();
    g.strokeStyle = `rgba(${c}, 0.95)`;
    g.lineWidth = i === highlight ? 3 : 1.5;
    g.stroke();
    if (s.kind === "include") {
      const [x, y] = pts.reduce(([a, b], [px, py]) => [a + px / pts.length, b + py / pts.length], [0, 0]);
      const text = s.name + (s.type ? ` · ${s.type}` : "") + (s.reflects ? ` · in glass: ${s.reflects}` : "");
      g.font = "12px system-ui, sans-serif";
      const w = g.measureText(text).width;
      g.fillStyle = "rgba(0, 0, 0, 0.65)";
      g.fillRect(x - w / 2 - 3, y - 9, w + 6, 17);
      g.fillStyle = `rgb(${c})`;
      g.fillText(text, x - w / 2, y + 4);
    }
  });
}

export function toShapes(doc) {
  if (!doc) return [];
  return [
    ...(doc.exclude || []).map((polygon) => ({ kind: "exclude", polygon })),
    ...(doc.glazing || []).map((polygon) => ({ kind: "glazing", polygon })),
    ...(doc.include || []).map((r) => ({ kind: "include", polygon: r.polygon, name: r.name,
      type: r.type, threshold_cd_m2: r.threshold_cd_m2, reflects: r.reflects || null })),
  ];
}

function fromShapes(shapes) {
  const doc = { exclude: [], glazing: [], include: [] };
  for (const s of shapes) {
    if (s.kind === "include") {
      const roi = { name: s.name, type: s.type || null, polygon: s.polygon };
      if (s.threshold_cd_m2) roi.threshold_cd_m2 = s.threshold_cd_m2;
      if (s.reflects) roi.reflects = s.reflects;
      doc.include.push(roi);
    } else {
      doc[s.kind].push(s.polygon);
    }
  }
  return doc;
}

// Fit an image of natural size (iw, ih) into a box, as object-fit: contain does.
export function fitRect(box, iw, ih) {
  const bw = box.clientWidth, bh = box.clientHeight;
  const s = Math.min(bw / iw, bh / ih);
  return { x: (bw - iw * s) / 2, y: (bh - ih * s) / 2, w: iw * s, h: ih * s };
}

// ---------------------------------------------------------------- editor

const $ = (id) => document.getElementById(id);
const CLOSE_PX = 14;   // a tap this close to the first corner closes the shape

let ed = null;         // editor state while the dialog is open

export function openMaskEditor({ profile, position, here, onSaved, say }) {
  const P = `/api/positions/${seg(profile)}/${seg(position.name)}`;
  // Messages go to the dialog itself (the page log is behind it) and to the page log.
  const tell = (msg, kind = "") => {
    const s = $("mask-status");
    s.textContent = msg;
    s.className = `hint ${kind}`;
    say(msg, kind);
  };
  ed = { P, position, here, onSaved, say: tell, shapes: [], draft: [], kind: "exclude",
         dirty: false, highlight: null, version: null };
  $("mask-title").textContent = `Masks · ${position.name}`;
  $("mask-status").textContent = "";
  setKind("exclude");
  $("mask-retake").disabled = !here;
  $("mask-retake").title = here ? "" : "Go to this position first";
  loadImage();
  api("GET", `${P}/masks`).then((doc) => {
    ed.shapes = toShapes(doc);
    ed.version = doc.version;
    $("mask-status").textContent = doc.version ? `saved as ${doc.version}` : "no masks yet";
    renderList();
    draw();
  }).catch((e) => say(e.message, "bad"));
  $("mask-dialog").showModal();
  $("mask-box").focus();   // Enter / Backspace act on the drawing, not on the Close button
}

function loadImage() {
  const img = $("mask-img");
  $("mask-msg").textContent = "Loading the reference image…";
  img.src = `${ed.P}/image?t=${Date.now()}`;
}

function setKind(kind) {
  ed.kind = kind;
  for (const b of document.querySelectorAll("#mask-kinds button")) {
    b.setAttribute("aria-pressed", String(b.dataset.kind === kind));
  }
  // Hidden but still taking its space: a layout that jumps would move the image under the
  // operator's finger between choosing the kind and tapping the first corner.
  $("lum-fields").classList.toggle("off", kind !== "include");
  for (const f of $("lum-fields").querySelectorAll("input, select")) f.disabled = kind !== "include";
}

function rect() {
  const img = $("mask-img");
  return fitRect($("mask-box"), img.naturalWidth || 3, img.naturalHeight || 2);
}

function draw() {
  if (!ed) return;
  const box = $("mask-box"), cv = $("mask-cv");
  const dpr = window.devicePixelRatio || 1;
  const W = box.clientWidth, H = box.clientHeight;
  cv.width = Math.round(W * dpr);
  cv.height = Math.round(H * dpr);
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);
  if (!$("mask-img").naturalWidth) return;
  const r = rect();
  drawMasks(g, r, fromShapes(ed.shapes), { fill: 0.22, highlight: ed.highlight });
  if (ed.draft.length) {
    const c = KINDS[ed.kind].color;
    const pts = ed.draft.map(([x, y]) => [r.x + x * r.w, r.y + y * r.h]);
    g.strokeStyle = `rgb(${c})`;
    g.lineWidth = 2;
    g.setLineDash([5, 4]);
    g.beginPath();
    pts.forEach(([x, y], j) => (j ? g.lineTo(x, y) : g.moveTo(x, y)));
    g.stroke();
    g.setLineDash([]);
    pts.forEach(([x, y], j) => {
      g.beginPath();
      g.arc(x, y, j === 0 ? 6 : 3.5, 0, 2 * Math.PI);
      g.fillStyle = j === 0 ? "rgba(0,0,0,0.6)" : `rgb(${c})`;
      g.fill();
      g.strokeStyle = `rgb(${c})`;
      g.stroke();
    });
  }
}

function renderList() {
  const names = [...new Set(ed.shapes.filter((s) => s.kind === "include" && !s.reflects).map((s) => s.name))];
  $("lum-names").replaceChildren(...names.map((n) => el("option", { value: n })));
  const items = ed.shapes.map((s, i) => el("li", {
    onmouseenter: () => { ed.highlight = i; draw(); },
    onmouseleave: () => { ed.highlight = null; draw(); },
  },
  el("span", { class: "swatch", style: `background: rgb(${KINDS[s.kind].color})` }),
  el("span", { class: "grow" }, s.kind === "include"
    ? `${s.name}${s.type ? ` · ${s.type}` : ""}${s.reflects ? ` · reflection of ${s.reflects}` : ""}` : KINDS[s.kind].label,
  el("span", { class: "muted" }, `  ${s.polygon.length} corners`)),
  el("button", { class: "quiet", title: "Delete this shape", onclick: () => {
    ed.shapes.splice(i, 1);
    ed.dirty = true;
    ed.highlight = null;
    renderList();
    draw();
  } }, "✕")));
  $("mask-list").replaceChildren(...(items.length ? items
    : [el("li", { class: "muted" }, "No shapes. Pick a kind and tap the corners on the image.")]));
  $("mask-save").disabled = !ed.dirty;
}

function addPoint(e) {
  if (!ed || !$("mask-img").naturalWidth) return;
  const r = rect();
  const b = $("mask-box").getBoundingClientRect();
  const px = e.clientX - b.left, py = e.clientY - b.top;
  if (ed.draft.length >= 3) {
    const [fx, fy] = ed.draft[0];
    if (Math.hypot(r.x + fx * r.w - px, r.y + fy * r.h - py) <= CLOSE_PX) { finish(); return; }
  }
  const x = Math.min(1, Math.max(0, (px - r.x) / r.w));
  const y = Math.min(1, Math.max(0, (py - r.y) / r.h));
  ed.draft.push([Number(x.toFixed(5)), Number(y.toFixed(5))]);
  draw();
}

function finish() {
  if (ed.draft.length < 3) { ed.say("a shape needs at least 3 corners", "warn"); return; }
  const shape = { kind: ed.kind, polygon: ed.draft };
  if (ed.kind === "include") {
    const name = $("lum-name").value.trim();
    if (!name) { ed.say("name the luminaire first (e.g. dome, cluster)", "warn"); $("lum-name").focus(); return; }
    if (ed.shapes.some((s) => s.kind === "include" && s.name === name)) {
      ed.say(`there is already a luminaire called “${name}”`, "warn");
      return;
    }
    shape.name = name;
    shape.type = $("lum-type").value === "other" ? null : $("lum-type").value;
    const refl = $("lum-reflects").value.trim();
    if (refl === name) { ed.say("a luminaire cannot be its own reflection", "warn"); return; }
    shape.reflects = refl || null;
    $("lum-name").value = "";
    $("lum-reflects").value = "";
  }
  ed.shapes.push(shape);
  ed.draft = [];
  ed.dirty = true;
  renderList();
  draw();
}

async function save() {
  try {
    const doc = await api("PUT", `${ed.P}/masks`, fromShapes(ed.shapes));
    ed.dirty = false;
    ed.version = doc.version;
    $("mask-status").textContent = `saved as ${doc.version}`;
    ed.say(`masks saved for “${ed.position.name}”`);
    renderList();
    ed.onSaved();
  } catch (e) {
    ed.say(e.message, "bad");
  }
}

async function retake() {
  try {
    await api("POST", `${ed.P}/image`);
    ed.say(`new reference image for “${ed.position.name}”`);
    loadImage();
    ed.onSaved();
  } catch (e) {
    ed.say(e.message, "bad");
  }
}

function mayClose() {
  return !ed || !ed.dirty || confirm("Close without saving the masks?");
}

export function wireMaskEditor() {
  const dlg = $("mask-dialog");
  for (const b of document.querySelectorAll("#mask-kinds button")) {
    b.addEventListener("click", () => { setKind(b.dataset.kind); ed.draft = []; draw(); });
  }
  $("mask-box").addEventListener("pointerdown", (e) => {
    e.preventDefault();
    $("mask-box").focus();
    addPoint(e);
  });
  $("mask-undo").addEventListener("click", () => { ed.draft.pop(); draw(); });
  $("mask-finish").addEventListener("click", finish);
  $("mask-cancel").addEventListener("click", () => { ed.draft = []; draw(); });
  $("mask-save").addEventListener("click", save);
  $("mask-retake").addEventListener("click", retake);
  $("mask-close").addEventListener("click", () => { if (mayClose()) dlg.close(); });
  const img = $("mask-img");
  img.addEventListener("load", () => {
    $("mask-msg").textContent = "";
    $("mask-box").style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`;
    draw();
  });
  img.addEventListener("error", () => {
    $("mask-msg").textContent = ed && ed.here
      ? "No reference image yet: press “Retake image from live view”."
      : "No reference image yet: go to this position, then open the masks again to take one.";
    draw();
  });
  // Unsaved work is not thrown away silently. Escape first drops an unfinished shape; with
  // unsaved shapes, Escape and Close ask first.
  dlg.addEventListener("cancel", (e) => {
    if (ed && ed.draft.length) { e.preventDefault(); ed.draft = []; draw(); return; }
    if (!mayClose()) e.preventDefault();
  });
  dlg.addEventListener("close", () => { ed = null; });
  // Drawing keys only while the image has focus; elsewhere Enter activates the focused button.
  $("mask-box").addEventListener("keydown", (e) => {
    if (!ed) return;
    if (e.key === "Enter") { e.preventDefault(); finish(); }
    if (e.key === "Backspace") { e.preventDefault(); ed.draft.pop(); draw(); }
  });
  new ResizeObserver(draw).observe($("mask-box"));
}
