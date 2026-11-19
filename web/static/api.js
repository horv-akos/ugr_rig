// Shared helpers for the rig pages. Plain ES module: no build step, nothing from a CDN
// (the Pi's own WiFi has no internet).

export async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch { /* empty or not JSON */ }
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    if (data && Array.isArray(data.detail)) {
      msg = data.detail.map((e) => `${e.loc.slice(1).join(".")}: ${e.msg}`).join("; ");
    } else if (data && data.detail) {
      msg = data.detail;
    }
    const err = new Error(msg);
    err.status = r.status;
    err.type = data && data.error;
    throw err;
  }
  return data;
}

// Path segment for a profile or position name (they may hold spaces and quotes).
export const seg = (s) => encodeURIComponent(s);

// +12.345 / −3.210: a real minus sign, so the columns line up.
export function deg(x, digits = 3) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  return (x < 0 ? "−" : "+") + Math.abs(x).toFixed(digits);
}

// Per-viewer conveniences only (remembered profile, arrow mode). Private windows and
// cleared site data make storage throw or forget, so every access is guarded.
export function remember(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* storage unavailable */ }
  return null;
}

// Small DOM builder: el("td", {class: "num"}, "text", child...). Text goes in as text,
// never as HTML, so names typed by the operator cannot inject markup.
export function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v === null || v === undefined) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
}
