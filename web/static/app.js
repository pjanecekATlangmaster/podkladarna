function parseApiError(res, text) {
  if (res.status === 413) {
    return (
      "Soubor je příliš velký pro reverse proxy (HTTP 413). " +
      "Na Synology zvyšte client_max_body_size v nginx (viz DEPLOY.md), " +
      "nebo nahrajte soubory přes http://IP_NAS:8672 bez HTTPS proxy."
    );
  }
  if (res.status === 503) {
    return (
      "Server je zaneprázdněn – fronta generování je plná. " +
      "Počkejte na dokončení běžících jobů a zkuste to znovu."
    );
  }
  if (res.status === 502 || res.status === 504) {
    return (
      `Proxy timeout (HTTP ${res.status}). LAZ soubory jsou velké – ` +
      "prodlužte timeout reverse proxy nebo uploadujte přímo na port 8672."
    );
  }
  try {
    const data = JSON.parse(text);
    if (Array.isArray(data.detail)) {
      return data.detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
    }
    if (typeof data.detail === "string") {
      return data.detail;
    }
  } catch (_) {
    /* not JSON */
  }
  const trimmed = (text || "").trim();
  if (trimmed.includes("Request Entity Too Large")) {
    return parseApiError({ status: 413 }, text);
  }
  return trimmed.slice(0, 800) || res.statusText || `HTTP ${res.status}`;
}

function showFormError(message) {
  const el = document.getElementById("form-error");
  el.textContent = message;
  el.classList.remove("hidden");
}

function clearFormError() {
  const el = document.getElementById("form-error");
  el.textContent = "";
  el.classList.add("hidden");
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(parseApiError(res, text));
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return res.json();
  return res;
}

let selectedJobId = null;
let logAfter = 0;
let pollTimer = null;

async function loadPresets() {
  const data = await api("/api/presets");
  const sel = document.getElementById("preset_id");
  sel.innerHTML = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "Vyberte typ mapy";
  placeholder.disabled = true;
  placeholder.selected = true;
  sel.appendChild(placeholder);
  for (const [id, p] of Object.entries(data)) {
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = p.label || id;
    sel.appendChild(opt);
  }
  sel.value = "";
}

async function loadJobs() {
  const data = await api("/api/jobs");
  updateWorkerStatus(data);
  const list = document.getElementById("jobs-list");
  list.innerHTML = "";
  for (const job of data.jobs) {
    const div = document.createElement("div");
    div.className = "job-item";
    if (job.id === selectedJobId) div.classList.add("selected");
    div.dataset.id = job.id;
    let queueLabel = "";
    if (job.queue_position === 0) {
      queueLabel = " · právě běží";
    } else if (job.queue_position) {
      queueLabel = ` · fronta #${job.queue_position}`;
    }
    div.innerHTML = `
      <strong>${escapeHtml(job.name)}</strong>
      <div class="status status-${job.status}">${job.status}${job.phase ? " · " + job.phase : ""}${queueLabel}</div>
      <div class="status">${job.preset_id} · ${job.created_at.slice(0, 19)}</div>
      ${job.error ? `<div class="status error">${escapeHtml(job.error)}</div>` : ""}
    `;
    div.onclick = () => selectJob(job.id);
    list.appendChild(div);
  }
  if (selectedJobId) await refreshLog();
}

function updateWorkerStatus(data) {
  const el = document.getElementById("worker-status");
  if (!el) return;
  if (data.busy) {
    const q = data.queue_size || 0;
    const maxQ = data.max_queue_size || "?";
    el.textContent =
      `Generování běží` +
      (q ? ` · ${q} job${q === 1 ? "" : "ů"} ve frontě (max ${maxQ})` : "");
    el.classList.add("busy");
  } else {
    el.textContent = "Server volný – lze spustit nové generování";
    el.classList.remove("busy");
  }
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

async function selectJob(id) {
  selectedJobId = id;
  logAfter = 0;
  document.getElementById("job-detail").classList.remove("hidden");
  const job = await api(`/api/jobs/${id}`);
  document.getElementById("detail-title").textContent = job.name;
  document.getElementById("detail-status").textContent =
    `Stav: ${job.status} · preset: ${job.preset_id}` + (job.error ? ` · ${job.error}` : "");
  document.getElementById("detail-download").href = `/api/jobs/${id}/download`;
  document.getElementById("detail-download").classList.toggle("hidden", !job.has_output);
  document.getElementById("detail-preview").href = `/api/jobs/${id}/preview.png`;
  document.getElementById("detail-preview").classList.toggle("hidden", !job.has_preview);
  const img = document.getElementById("detail-img");
  if (job.has_preview) {
    img.src = `/api/jobs/${id}/preview.png?t=${Date.now()}`;
    img.classList.remove("hidden");
  } else {
    img.classList.add("hidden");
  }
  document.getElementById("detail-log").textContent = "";
  await refreshLog();
  await loadJobs();
}

async function refreshLog() {
  if (!selectedJobId) return;
  const data = await api(`/api/jobs/${selectedJobId}/log?after=${logAfter}`);
  const pre = document.getElementById("detail-log");
  for (const line of data.lines) {
    pre.textContent += line.line + "\n";
    logAfter = line.id;
  }
  pre.scrollTop = pre.scrollHeight;
}

document.getElementById("job-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const btn = document.getElementById("submit-btn");
  clearFormError();
  const mode = currentSourceMode();
  if (!form.preset_id.value) {
    showFormError("Vyberte typ mapy.");
    form.preset_id.focus();
    return;
  }
  if (mode === "map" && !document.getElementById("bbox-input").value) {
    showFormError("Nakreslete výřez na mapě (dva protilehlé rohy).");
    return;
  }
  if (mode === "upload") {
    const dmr = form.dmr_files.files;
    const dmp = form.dmp_files.files;
    if (!dmr.length || !dmp.length) {
      showFormError("Nahrajte DMR i DMP (LAZ/LAS).");
      return;
    }
  }
  btn.disabled = true;
  btn.textContent = mode === "map" ? "Zakládám job…" : "Nahrávám soubory… (může trvat minuty)";
  try {
    const fd = new FormData(form);
    fd.set("source_mode", mode);
    fd.set("run_vectors", form.run_vectors.checked ? "true" : "false");
    fd.set("output_png", form.output_png.checked ? "true" : "false");
    fd.set("output_dxf", form.output_dxf.checked ? "true" : "false");
    fd.set("output_zabaged_clean", form.output_zabaged_clean.checked ? "true" : "false");
    fd.set("savetempfolders", form.savetempfolders.checked ? "true" : "false");
    if (mode === "upload") {
      fd.delete("bbox");
    }
    const job = await api("/api/jobs", { method: "POST", body: fd });
    await loadJobs();
    await selectJob(job.id);
  } catch (err) {
    showFormError(err.message);
    alert(err.message);
    await loadJobs();
  } finally {
    btn.disabled = false;
    btn.textContent = "Spustit generování";
  }
});

function currentSourceMode() {
  const checked = document.querySelector("input[name=source_mode]:checked");
  return checked ? checked.value : "map";
}

function syncSourceMode() {
  const mode = currentSourceMode();
  document.getElementById("map-mode").classList.toggle("hidden", mode !== "map");
  document.getElementById("upload-mode").classList.toggle("hidden", mode !== "upload");
  if (mode === "map") {
    setTimeout(() => {
      if (bboxMap) bboxMap.invalidateSize();
    }, 50);
  }
}

let bboxMap = null;
let bboxCorners = [];

const CZ = { south: 48.35, west: 11.85, north: 51.25, east: 19.1 };

function czBounds() {
  return L.latLngBounds([CZ.south, CZ.west], [CZ.north, CZ.east]);
}

function inCzechia(latlng) {
  return (
    latlng.lat >= CZ.south &&
    latlng.lat <= CZ.north &&
    latlng.lng >= CZ.west &&
    latlng.lng <= CZ.east
  );
}

function initBboxMap() {
  document.querySelectorAll("input[name=source_mode]").forEach((elRadio) => {
    elRadio.addEventListener("change", syncSourceMode);
  });
  const clearBtn = document.getElementById("bbox-clear");
  if (clearBtn) clearBtn.addEventListener("click", clearBbox);
  syncSourceMode();
  const el = document.getElementById("bbox-map");
  if (!el) return;
  if (typeof L === "undefined") {
    setSheetInfo("Mapová knihovna se nenačetla (Leaflet).", "err");
    return;
  }
  const bounds = czBounds();
  bboxMap = L.map(el, {
    scrollWheelZoom: true,
    maxBounds: bounds,
    maxBoundsViscosity: 1.0,
    minZoom: 6,
    worldCopyJump: false,
  }).setView([49.8, 15.5], 7);
  const tileOpts = {
    maxZoom: 18,
    noWrap: true,
    bounds,
    attribution: "&copy; OpenStreetMap",
  };
  // Přes Cloudflare OSM často padá (Referer / Rocket Loader).
  // Lokální náhled na NAS: OSM přímo. Záloha: proxy /tiles/ přes origin.
  const osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", tileOpts);
  let usedProxy = false;
  osm.on("tileerror", () => {
    if (usedProxy) return;
    usedProxy = true;
    bboxMap.removeLayer(osm);
    L.tileLayer("/tiles/{z}/{x}/{y}.png", tileOpts).addTo(bboxMap);
  });
  osm.addTo(bboxMap);
  bboxMap.on("click", onMapClick);
}

function onMapClick(e) {
  if (!inCzechia(e.latlng)) {
    setSheetInfo("Výřez jen na území Česka.", "err");
    return;
  }
  if (bboxCorners.length >= 2) {
    clearBbox();
  }
  bboxCorners.push(e.latlng);
  if (bboxCorners.length === 1) {
    setSheetInfo("Druhý roh výřezu…", "");
    L.circleMarker(e.latlng, { radius: 5, color: "#cc00cc" }).addTo(bboxMap);
  }
  if (bboxCorners.length === 2) {
    const b = L.latLngBounds(bboxCorners[0], bboxCorners[1]);
    L.rectangle(b, { color: "#cc00cc", weight: 2, fillOpacity: 0.15 }).addTo(bboxMap);
    bboxMap.fitBounds(b, { padding: [20, 20], maxZoom: 14 });
    const west = b.getWest();
    const south = b.getSouth();
    const east = b.getEast();
    const north = b.getNorth();
    document.getElementById("bbox-input").value = [west, south, east, north].join(",");
    lookupSheets();
  }
}

function clearBbox() {
  bboxCorners = [];
  document.getElementById("bbox-input").value = "";
  if (bboxMap) {
    bboxMap.eachLayer((layer) => {
      if (layer instanceof L.Rectangle || layer instanceof L.CircleMarker) {
        bboxMap.removeLayer(layer);
      }
    });
  }
  setSheetInfo("Nakreslete obdélník dvěma kliknutími.", "");
}

function setSheetInfo(text, cls) {
  const el = document.getElementById("sheet-info");
  el.textContent = text;
  el.className = "sheet-info" + (cls ? " " + cls : "");
}

async function lookupSheets() {
  const bbox = document.getElementById("bbox-input").value;
  if (!bbox) return;
  setSheetInfo("Zjišťuji mapové listy SM5…", "");
  try {
    const data = await api(`/api/sheets?bbox=${encodeURIComponent(bbox)}`);
    if (!data.count) {
      setSheetInfo(data.label, "err");
      return;
    }
    if (data.too_large) {
      setSheetInfo(
        `${data.label}. Zmenšete výřez (max ${data.max_sheets} listů).`,
        "warn"
      );
      return;
    }
    setSheetInfo(`${data.label}. Odhad: cca ${data.estimate_minutes} min.`, "ok");
  } catch (err) {
    setSheetInfo(err.message, "err");
  }
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    await loadJobs();
  }, 2500);
}

loadPresets().then(loadJobs).then(startPolling);
initBboxMap();
