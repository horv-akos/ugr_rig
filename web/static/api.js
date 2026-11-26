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

// Live view into an <img>, pulled one frame at a time: the next frame is asked for only once
// the last has arrived, so on a slow link the frame rate drops instead of frames queueing up
// behind the head (an MJPEG stream lagged ~10 s on the rig's WiFi). Nothing is fetched while
// the tab is hidden, which leaves the link to the jog pings. stop() ends it.
// feed.fps: frames shown per second; feed.lag_s: about how old the shown frame was on arrival.
//
// Battery saver: after feed.idle_s (the status's live_view.operator_idle_s; 0: never) without a
// touch, click, key or scroll on the page, nothing more is fetched, so the server leaves live
// view and lowers the mirror; the last frame is dimmed and feed.idle is set. Any input brings it
// back. feed.poke() counts as input: the pages call it while the head moves.
// The live-view message while the feed is idle.
export function idleText(feed) {
  const min = Math.round(feed.idle_s / 60);
  const after = min >= 1 ? `${min} min` : `${Math.round(feed.idle_s)} s`;
  return `Live view paused after ${after} without input, to spare the camera. Tap or click to resume.`;
}

const INPUT_EVENTS = ["pointerdown", "pointermove", "keydown", "wheel", "touchstart"];

export function liveFeed(img) {
  const feed = { fps: 0, lag_s: null, error: null, idle: false, idle_s: 300, poke, stop };
  let stopped = false, seq = -1, shown = null, last = 0, lastInput = performance.now();
  const sleep = (ms) => new Promise((res) => setTimeout(res, ms));
  for (const t of INPUT_EVENTS) document.addEventListener(t, poke, { capture: true, passive: true });

  function poke() { lastInput = performance.now(); }

  function quiet() {
    const q = feed.idle_s > 0 && performance.now() - lastInput > feed.idle_s * 1000;
    if (q !== feed.idle) {
      feed.idle = q;
      img.style.opacity = q ? "0.35" : "";
    }
    return q;
  }

  async function loop() {
    while (!stopped) {
      if (quiet() || document.hidden) { await sleep(500); continue; }
      try {
        const r = await fetch(`/api/live/next?after=${seq}`, { cache: "no-store" });
        const t0 = performance.now();
        if (!r.ok) {
          let msg = `HTTP ${r.status}`;
          try { msg = (await r.json()).detail || msg; } catch { /* not JSON */ }
          throw new Error(msg);
        }
        const s = Number(r.headers.get("X-Frame-Seq"));
        const age = Number(r.headers.get("X-Frame-Age")) || 0;
        const blob = await r.blob();
        if (stopped) break;
        const url = URL.createObjectURL(blob);
        await new Promise((res) => {
          img.addEventListener("load", res, { once: true });
          img.addEventListener("error", res, { once: true });
          img.src = url;
        });
        if (shown) URL.revokeObjectURL(shown);
        shown = url;
        const now = performance.now();
        if (s !== seq) {
          if (last) feed.fps = 0.8 * feed.fps + 0.2 * (1000 / Math.max(now - last, 1));
          last = now;
          feed.lag_s = age + (now - t0) / 1000;
        }
        seq = s;
        feed.error = null;
      } catch (e) {
        feed.error = e.message;
        await sleep(1000);
      }
    }
  }

  function stop() {
    stopped = true;
    for (const t of INPUT_EVENTS) document.removeEventListener(t, poke, { capture: true });
    img.style.opacity = "";
    img.removeAttribute("src");
    if (shown) URL.revokeObjectURL(shown);
    shown = null;
  }

  loop();
  return feed;
}
