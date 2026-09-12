/*
 * Camera-control behaviour for the #ptz-card partial (templates/_ptz.html).
 *
 * Loaded by both index.html and camera.html. It was inline in index.html until
 * the /camera page needed the same controls — copying it would have guaranteed
 * drift, and the two copies' top-level `const`/`let` would have collided.
 *
 * Everything here calls bare `fetch(...)`, which resolves window.fetch at call
 * time, so the ?mock=1 shim in mock.js intercepts these regardless of script
 * order.
 */
(function () {
  "use strict";

  const card = document.getElementById("ptz-card");
  if (!card) return;

  function escapeHtml(s) {
    return String(s).replace(/[<>&"']/g, c => (
      {'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&#39;'}[c]
    ));
  }

  async function postForm(url, fields = {}) {
    const body = new URLSearchParams();
    for (const [k, v] of Object.entries(fields)) body.append(k, String(v));
    try { await fetch(url, { method: "POST", body }); } catch (_) { /* ignore */ }
  }

  // ---- Camera info poll --------------------------------------------------

  let _ptzMoveSpeed = 0.4;

  // index.html hides the whole card when the camera reports no PTZ support.
  // camera.html has nothing else on it, so it keeps the card up and explains
  // itself in the preview placeholder instead.
  const hideWhenUnsupported = card.dataset.hideWhenUnsupported !== "false";

  function renderPtzInfo(info) {
    const supported = !!(info && info.supported);
    if (!supported && hideWhenUnsupported) {
      card.hidden = true;
      return;
    }
    card.hidden = false;

    const placeholder = document.getElementById("ptz-placeholder");
    if (placeholder) {
      placeholder.textContent = supported
        ? "Waiting for camera…"
        : "Not connected. Check the camera is powered on and on this network, "
          + "then pick it from the list above.";
    }
    if (!supported) return;

    document.getElementById("ptz-source").textContent = info.source ? `— ${info.source}` : "";
    _ptzMoveSpeed = info.move_speed ?? 0.4;
    const recallSpeed = info.recall_speed ?? 0.5;
    const grid = document.getElementById("presets-grid");
    const presets = info.presets || [];
    grid.innerHTML = presets.length
      ? presets.map(p =>
          `<button type="button" class="preset-btn"
                   data-slot="${p.slot}" data-speed="${recallSpeed}">${escapeHtml(p.name)}</button>`
        ).join("")
      : `<p class="hint">No camera presets have been set up. Contact your Stake Technology Specialist if you need them.</p>`;
  }

  async function fetchPtzInfo() {
    try {
      const r = await fetch("/api/ptz/info");
      if (!r.ok) { return; }
      renderPtzInfo(await r.json());
    } catch (_) { /* ignore */ }
  }
  fetchPtzInfo();
  setInterval(fetchPtzInfo, 10000);

  // ---- Preview: pull a JPEG snapshot at ~5 fps ---------------------------

  const previewImg = document.getElementById("ptz-preview");
  async function refreshPreview() {
    const url = `/api/preview.jpg?t=${Date.now()}`;
    try {
      const r = await fetch(url);
      if (r.ok) {
        const blob = await r.blob();
        if (previewImg._lastBlobUrl) URL.revokeObjectURL(previewImg._lastBlobUrl);
        previewImg._lastBlobUrl = URL.createObjectURL(blob);
        previewImg.src = previewImg._lastBlobUrl;
        previewImg.hidden = false;
      } else {
        previewImg.hidden = true;
      }
    } catch (_) {
      previewImg.hidden = true;
    } finally {
      setTimeout(refreshPreview, 200);
    }
  }
  refreshPreview();

  // ---- Presets -----------------------------------------------------------

  // Delegated, because renderPtzInfo replaces the grid's contents wholesale.
  document.addEventListener("click", (evt) => {
    const btn = evt.target.closest(".preset-btn");
    if (!btn) return;
    postForm(`/api/ptz/preset/${btn.dataset.slot}`, { speed: btn.dataset.speed });
  });

  // ---- Manual pan / tilt / zoom ------------------------------------------

  // NDI PTZ motion is continuous: a non-zero speed starts the camera moving
  // and it keeps going until something sends zero. Hence press-and-hold.
  function bindHoldButton(btn, onPress, onRelease) {
    const start = (e) => { e.preventDefault(); onPress(); };
    const end   = (e) => { e.preventDefault(); onRelease(); };
    btn.addEventListener("mousedown", start);
    btn.addEventListener("touchstart", start, {passive: false});
    btn.addEventListener("mouseup", end);
    btn.addEventListener("mouseleave", end);
    btn.addEventListener("touchend", end);
    btn.addEventListener("touchcancel", end);
  }

  for (const b of document.querySelectorAll(".ptz-arrow")) {
    bindHoldButton(b,
      () => postForm("/api/ptz/pan_tilt", {
        pan:  Number(b.dataset.pan)  * _ptzMoveSpeed,
        tilt: Number(b.dataset.tilt) * _ptzMoveSpeed,
      }),
      () => postForm("/api/ptz/pan_tilt", { pan: 0, tilt: 0 })
    );
  }
  for (const b of document.querySelectorAll(".ptz-zoom-btn")) {
    bindHoldButton(b,
      () => postForm("/api/ptz/zoom", { speed: Number(b.dataset.zoom) * _ptzMoveSpeed }),
      () => postForm("/api/ptz/zoom", { speed: 0 })
    );
  }
  document.getElementById("ptz-stop").addEventListener("click",
    () => postForm("/api/ptz/stop"));

  // ---- Source picker (camera.html only) ----------------------------------

  // PtzController connects to whatever config.stream.last_source names. On the
  // main page that value is a side effect of starting a stream; an OBS-only
  // site never starts one, so the /camera page owns picking the camera and
  // persisting it via /api/config.
  const sourceSelect = document.getElementById("camera-source-select");
  if (sourceSelect) {
    async function refreshSources() {
      try {
        const r = await fetch("/api/sources");
        if (!r.ok) return;
        const sources = await r.json();
        const current = sourceSelect.value;
        sourceSelect.innerHTML = sources.length
          ? sources.map(s =>
              `<option value="${escapeHtml(s.name)}" ${s.name === current ? "selected" : ""}>${escapeHtml(s.name)}</option>`
            ).join("")
          : `<option value="">(searching for cameras…)</option>`;
      } catch (_) { /* ignore */ }
    }
    refreshSources();
    setInterval(refreshSources, 5000);

    sourceSelect.addEventListener("change", () => {
      postForm("/api/config", { last_source: sourceSelect.value });
      // Drop the stale connection label immediately; the next info poll
      // (or the reconnect it triggers) fills it back in.
      document.getElementById("ptz-source").textContent = "";
      fetchPtzInfo();
    });
  }
})();
