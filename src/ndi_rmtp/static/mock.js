/*
 * Mock backend (dev only). Activated by adding `?mock=1` to the URL; inert
 * otherwise.
 *
 * Fakes every /api endpoint and simulates the state machine so the UI can be
 * worked on without a real camera or a real YouTube stream. Shared by
 * index.html and camera.html — the camera page is unusable on a dev Mac
 * without it, since there's no NDI source to preview.
 *
 * Page-specific values (ward, quality, presets) come from a
 * <script type="application/json" id="mock-seed"> block that the template
 * renders; this file stays free of Jinja so it can live in /static.
 *
 * Load this BEFORE ptz.js: ptz.js kicks off its info poll and preview loop at
 * load time, and the fetch shim has to be installed by then.
 */
(function () {
  const params = new URLSearchParams(location.search);
  if (params.get("mock") !== "1") return;

  // ---- Mock state ---------------------------------------------------------
  // A small state machine mirroring the real one. `state` transitions on
  // button clicks (Start / Stop) with realistic latency; /api/status reads
  // straight off this object.
  let seed = {};
  try {
    seed = JSON.parse(document.getElementById("mock-seed").textContent);
  } catch (_) { /* defaults below cover it */ }

  const mock = {
    state: "IDLE",
    started_at: null,
    muted: false,
    sacrament: false,
    source: "UHD CAMERA (NDI HX3, 192.168.110.9)",
    ward: seed.ward || "",
    quality: seed.quality || "high",
    default_preset: seed.default_preset || "",
    presets: seed.presets || [],
    preset_slots: seed.preset_slots || [],
    last_preset_recall: null,
  };

  // Visible banner so it's obvious you're in mock mode. Uses normal flow (not
  // position:fixed) so the real page header underneath stays fully readable —
  // this is a dev indicator, not a persistent chrome.
  const banner = document.createElement("div");
  banner.textContent = "🧪 Mock mode — /api is faked, no real stream will start";
  banner.style.cssText = [
    "background:#f59e0b","color:#111","font:600 12px/1.4 system-ui",
    "text-align:center","padding:4px 8px","letter-spacing:0.02em",
  ].join(";");
  document.addEventListener("DOMContentLoaded", () => document.body.prepend(banner));

  // ---- Endpoint responders ------------------------------------------------
  async function fakePreviewBlob() {
    const c = document.createElement("canvas");
    c.width = 1280; c.height = 720;
    const ctx = c.getContext("2d");
    const grad = ctx.createLinearGradient(0, 0, 0, 720);
    grad.addColorStop(0, "#1f2937"); grad.addColorStop(1, "#0b1220");
    ctx.fillStyle = grad; ctx.fillRect(0, 0, 1280, 720);
    // Overlay a subtle grid so it looks vaguely like framing lines.
    ctx.strokeStyle = "rgba(148, 163, 184, 0.15)"; ctx.lineWidth = 2;
    for (let x = 0; x <= 1280; x += 160) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, 720); ctx.stroke(); }
    for (let y = 0; y <= 720; y += 90)  { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(1280, y); ctx.stroke(); }
    ctx.fillStyle = "#e2e8f0"; ctx.font = "bold 64px system-ui";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("MOCK PREVIEW", 640, 320);
    ctx.font = "24px system-ui"; ctx.fillStyle = "#94a3b8";
    ctx.fillText(mock.source, 640, 380);
    if (mock.last_preset_recall) {
      ctx.font = "22px system-ui"; ctx.fillStyle = "#a5f3fc";
      ctx.fillText("→ " + mock.last_preset_recall, 640, 440);
    }
    return new Promise(r => c.toBlob(b => r(b), "image/jpeg", 0.85));
  }

  function statusPayload() {
    const uptime = mock.started_at
      ? Math.max(0, Math.floor((Date.now() - mock.started_at) / 1000))
      : 0;
    const logs = [];
    if (mock.state === "STARTING") logs.push("[mock] spawning ndi-streamer…", "[mock] connecting to " + mock.source);
    if (mock.state === "STREAMING") {
      logs.push("[mock] connected to " + mock.source);
      logs.push("[mock] Output #0, flv, to 'rtmp://a.rtmp.youtube.com/live2/…'");
      const frames = 30 * uptime;
      logs.push(`[mock] frames=${frames} fps=30.0 bitrate=6010kbps`);
      if (mock.muted) logs.push("[mock] audio: muted");
      if (mock.sacrament) logs.push("[mock] sacrament image active");
    }
    if (mock.state === "STOPPING") logs.push("[mock] SIGTERM sent, draining muxer");
    return {
      state: mock.state,
      uptime_s: uptime,
      last_error: null,
      muted: mock.muted,
      sacrament_mode: mock.sacrament,
      log_tail: logs,
      cpu_temp_c: 62.4,
    };
  }

  function ptzInfoPayload() {
    return {
      supported: true,
      connected: true,
      source: mock.source,
      presets: mock.presets.map((name, i) => ({ name, slot: mock.preset_slots[i] })),
      recall_speed: 0.1,
      move_speed: 0.2,
      default_preset: mock.default_preset,
    };
  }

  async function handle(url, method, formBody) {
    const u = String(url);
    method = (method || "GET").toUpperCase();
    if (u.startsWith("/api/status"))    return { status: 200, json: statusPayload() };
    if (u.startsWith("/api/ptz/info"))  return { status: 200, json: ptzInfoPayload() };
    if (u.startsWith("/api/preview.jpg")) return { status: 200, blob: await fakePreviewBlob(), contentType: "image/jpeg" };
    if (u.startsWith("/api/sources"))   return { status: 200, json: [{ name: mock.source, address: "192.168.110.9" }] };
    // Cycle the YouTube liveness states so both branches of the UI (the green
    // "Live on YouTube" pill and its complement, the help card) are reachable
    // without a real channel. `?yt=<state>` pins one.
    if (u.startsWith("/api/youtube")) {
      const pinned = new URLSearchParams(location.search).get("yt");
      const state = pinned || (mock.state === "STREAMING" ? "not_visible" : "scheduled");
      return {
        status: 200,
        json: {
          state,
          video_id: state === "live" || state === "scheduled" ? "dQw4w9WgXcQ" : "",
          watch_url: state === "live" || state === "scheduled"
            ? "https://www.youtube.com/watch?v=dQw4w9WgXcQ" : "",
          checked_at: Date.now() / 1000,
          channel_id: "UCbLlSmx8Wvg85m8jPgruTuQ",
          control_room_url: "https://studio.youtube.com/channel/UCbLlSmx8Wvg85m8jPgruTuQ/livestreaming/dashboard",
        },
      };
    }
    if (u.startsWith("/api/start") && method === "POST") {
      if (mock.state !== "IDLE" && mock.state !== "ERROR") return { status: 409, json: { detail: "stream already running" } };
      mock.state = "STARTING";
      setTimeout(() => { mock.state = "STREAMING"; mock.started_at = Date.now(); }, 1500);
      return { status: 200, json: { ok: true } };
    }
    if (u.startsWith("/api/stop") && method === "POST") {
      mock.state = "STOPPING";
      setTimeout(() => { mock.state = "IDLE"; mock.started_at = null; mock.muted = false; mock.sacrament = false; mock.last_preset_recall = null; }, 1000);
      return { status: 200, json: { ok: true } };
    }
    if (u.startsWith("/api/mute") && method === "POST") {
      mock.muted = (formBody && formBody.get("muted")) === "true";
      return { status: 200, json: { ok: true, muted: mock.muted } };
    }
    if (u.startsWith("/api/sacrament") && method === "POST") {
      mock.sacrament = (formBody && formBody.get("enabled")) === "true";
      if (mock.sacrament) mock.muted = true;
      return { status: 200, json: { ok: true, sacrament_mode: mock.sacrament } };
    }
    if (u.match(/^\/api\/ptz\/preset\/(\d+)/) && method === "POST") {
      const slot = Number(u.match(/^\/api\/ptz\/preset\/(\d+)/)[1]);
      const idx = mock.preset_slots.indexOf(slot);
      if (idx >= 0) mock.last_preset_recall = mock.presets[idx];
      return { status: 200, json: { ok: true } };
    }
    if (u.startsWith("/api/ptz/pan_tilt") || u.startsWith("/api/ptz/zoom") || u.startsWith("/api/ptz/stop")) {
      return { status: 200, json: { ok: true } };
    }
    if (u.startsWith("/api/config") && method === "POST") {
      if (formBody) {
        if (formBody.has("active_ward")) mock.ward = formBody.get("active_ward");
        if (formBody.has("quality"))     mock.quality = formBody.get("quality");
        if (formBody.has("default_preset")) mock.default_preset = formBody.get("default_preset");
        if (formBody.has("last_source"))    mock.source = formBody.get("last_source");
      }
      return { status: 200, json: { ok: true } };
    }
    return null; // fall through to real fetch
  }

  // ---- fetch() shim -------------------------------------------------------
  const origFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    const method = (init && init.method) || (typeof input === "object" && input.method) || "GET";
    let body = init && init.body;
    if (body instanceof URLSearchParams) { /* already fine */ }
    else if (typeof body === "string")   { body = new URLSearchParams(body); }
    else                                  { body = null; }
    const r = await handle(url, method, body);
    if (r === null) return origFetch(input, init);
    if (r.blob) return new Response(r.blob, { status: r.status, headers: { "Content-Type": r.contentType } });
    return new Response(JSON.stringify(r.json), { status: r.status, headers: { "Content-Type": "application/json" } });
  };

  // ---- XHR shim (htmx uses XHR, not fetch) --------------------------------
  const OrigXHR = window.XMLHttpRequest;
  window.XMLHttpRequest = function () {
    const xhr = new OrigXHR();
    const origOpen = xhr.open.bind(xhr);
    const origSend = xhr.send.bind(xhr);
    let _url = "", _method = "GET";
    xhr.open = function (method, url, ...rest) {
      _url = String(url); _method = String(method).toUpperCase();
      return origOpen(method, url, ...rest);
    };
    xhr.send = function (body) {
      if (!_url.startsWith("/api/")) return origSend(body);
      let form = null;
      if (body instanceof URLSearchParams) form = body;
      else if (typeof body === "string" && body) form = new URLSearchParams(body);
      handle(_url, _method, form).then(r => {
        if (r === null) return origSend(body);
        const text = r.json ? JSON.stringify(r.json) : "";
        Object.defineProperty(xhr, "status", { value: r.status, configurable: true });
        Object.defineProperty(xhr, "statusText", { value: "OK", configurable: true });
        Object.defineProperty(xhr, "responseText", { value: text, configurable: true });
        Object.defineProperty(xhr, "response",     { value: text, configurable: true });
        Object.defineProperty(xhr, "readyState",   { value: 4, configurable: true });
        xhr.dispatchEvent(new Event("readystatechange"));
        xhr.dispatchEvent(new Event("load"));
        xhr.dispatchEvent(new Event("loadend"));
      });
    };
    return xhr;
  };
})();
