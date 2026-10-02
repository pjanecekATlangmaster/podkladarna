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
  if (res.status === 429) {
    try {
      const data = JSON.parse(text);
      if (data.detail) return String(data.detail);
    } catch (_) { /* fall through */ }
    return "Limit jobů z vaší sítě – počkejte na dokončení běžících generování.";
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
let selectedJobStatus = null;
let logAfter = 0;
let logSettledForJob = null;
let pollTimer = null;
let finishedPage = 0;
let focusFinishedJobId = null;
const FINISHED_PAGE_SIZE = 8;
let bboxMap = null;
let bboxCorners = [];
let bboxRect = null;
let bboxAllowed = false;
let lastSheets = null;
let mapOptions = {
  scales: [4000, 7500, 10000, 15000],
  contours_by_scale: {
    4000: [2, 2.5, 5],
    7500: [2, 2.5, 5],
    10000: [5],
    15000: [5],
  },
  default_contour_by_scale: {
    4000: 2.5,
    7500: 5,
    10000: 5,
    15000: 5,
  },
};

function formatContourLabel(meters) {
  const n = Number(meters);
  if (Number.isInteger(n)) return `${n} m`;
  return `${String(n).replace(".", ",")} m`;
}

function disciplinesForScale(scale) {
  const s = Number(scale);
  if (s === 4000) {
    return {
      count: 2,
      text: "2× .omap: jen sprint (ISSprOM) × 2 zdroje cest (ZABAGED / OSM)",
    };
  }
  if (s === 7500) {
    return {
      count: 4,
      text: "4× .omap: les + MTBO (oba 1:7500) × 2 zdroje cest — bez sprintu",
    };
  }
  if (s === 10000) {
    return {
      count: 4,
      text: "4× .omap: les + MTBO (1:10000) × 2 zdroje cest",
    };
  }
  if (s === 15000) {
    return {
      count: 4,
      text: "4× .omap: les + MTBO (1:15000) × 2 zdroje cest",
    };
  }
  return { count: 0, text: "Vyberte měřítko — ukáže se, kolik omapů vznikne." };
}

function jobScaleLabel(job) {
  const opts = job.options || {};
  let scale = opts.map_scale != null ? Number(opts.map_scale) : NaN;
  if (!Number.isFinite(scale) && opts.scalefactor != null) {
    scale = Math.round(Number(opts.scalefactor) * 10000);
  }
  let contour = opts.contour_interval != null ? Number(opts.contour_interval) : NaN;
  if (Number.isFinite(scale) && mapOptions.scales.includes(scale)) {
    if (!Number.isFinite(contour)) {
      contour = Number(mapOptions.default_contour_by_scale[scale]);
    }
    const cTxt = Number.isInteger(contour)
      ? String(contour)
      : String(contour).replace(".", ",");
    return `1:${scale} · ${cTxt} m`;
  }
  return job.preset_id || "?";
}

function rebuildContourOptions(preferred) {
  const scaleSel = document.getElementById("map_scale");
  const contourSel = document.getElementById("contour_interval");
  if (!scaleSel || !contourSel) return;
  const scale = Number(scaleSel.value);
  const allowed = mapOptions.contours_by_scale[scale] || [];
  const fallback = mapOptions.default_contour_by_scale[scale];
  let keep = preferred != null ? Number(preferred) : Number(contourSel.value);
  if (!allowed.some((c) => Math.abs(c - keep) < 1e-6)) {
    keep = fallback;
  }
  contourSel.innerHTML = "";
  if (!scaleSel.value) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.disabled = true;
    opt.selected = true;
    opt.textContent = "Nejdřív vyberte měřítko";
    contourSel.appendChild(opt);
    contourSel.disabled = true;
  } else {
    contourSel.disabled = false;
    for (const c of allowed) {
      const opt = document.createElement("option");
      opt.value = String(c);
      let label = formatContourLabel(c);
      if (Math.abs(c - fallback) < 1e-6) {
        label += " – výchozí";
      }
      opt.textContent = label;
      contourSel.appendChild(opt);
    }
    const match = [...contourSel.options].find(
      (o) => Math.abs(Number(o.value) - keep) < 1e-6
    );
    contourSel.value = match ? match.value : String(fallback);
  }
  updateOutputHints();
}

function updateOutputHints() {
  const scaleSel = document.getElementById("map_scale");
  const discHint = document.getElementById("output-disciplines-hint");
  const contourHint = document.getElementById("contour-hint");
  const scale = scaleSel ? Number(scaleSel.value) : NaN;
  const info = disciplinesForScale(scale);
  if (discHint) discHint.textContent = info.text;
  if (contourHint) {
    if (scale === 4000) {
      contourHint.textContent =
        "U 1:4000: 2 m / 2,5 m / 5 m. Platí pro omapy i kontury.";
    } else if (scale === 7500) {
      contourHint.textContent =
        "U 1:7500: 2 / 2,5 / 5 m. Platí pro všechny generované omapy.";
    } else if (scale === 10000 || scale === 15000) {
      contourHint.textContent = "U tohoto měřítka jen 5 m.";
    } else {
      contourHint.textContent = "Nabídka závisí na měřítku.";
    }
  }
}

async function loadMapOptions() {
  try {
    const data = await api("/api/map_options");
    if (data && data.scales) mapOptions = data;
  } catch (_) {
    /* použij vestavěné defaulty */
  }
  const scaleSel = document.getElementById("map_scale");
  if (scaleSel && !scaleSel.value) {
    scaleSel.value = "10000";
  }
  rebuildContourOptions();
  if (scaleSel) {
    scaleSel.addEventListener("change", () => {
      const scale = Number(scaleSel.value);
      rebuildContourOptions(mapOptions.default_contour_by_scale[scale]);
    });
  }
}

function jobIsLive(status) {
  return status === "running" || status === "queued" || status === "pending";
}

function jobItemHeadHtml(job) {
  let queueLabel = "";
  if (job.queue_position === 0) {
    queueLabel = " · právě běží";
  } else if (job.queue_position) {
    queueLabel = ` · fronta #${job.queue_position}`;
  }
  let timing = "";
  if (job.status === "done" || job.status === "failed") {
    const dur = formatDuration(job.duration_s);
    if (dur) timing = ` · ${dur}`;
  }
  return `
    <strong>${escapeHtml(job.name)}</strong>
    <div class="status status-${job.status}">${job.status}${job.phase ? " · " + job.phase : ""}${queueLabel}${timing}</div>
    <div class="status">${escapeHtml(jobScaleLabel(job))} · ${escapeHtml(formatWhen(job.created_at))}</div>
    ${job.error ? `<div class="status error">${escapeHtml(job.error)}</div>` : ""}
  `;
}

function splitJobs(jobs) {
  const live = [];
  const finished = [];
  for (const job of jobs) {
    if (jobIsLive(job.status)) live.push(job);
    else finished.push(job);
  }
  return { live, finished };
}

function jobItemEl(jobId) {
  return document.querySelector(`.job-item[data-id="${CSS.escape(jobId)}"]`);
}

function upsertJobItem(list, job, order) {
  const detail = jobDetailEl();
  let div = jobItemEl(job.id);
  if (!div) {
    div = document.createElement("div");
    div.className = "job-item";
    div.dataset.id = job.id;
    const head = document.createElement("div");
    head.className = "job-item-head";
    div.appendChild(head);
    div.onclick = (e) => {
      if (e.target.closest(".job-detail")) return;
      selectJob(job.id);
    };
  }
  div.classList.toggle("selected", job.id === selectedJobId);
  div.classList.toggle("expanded", job.id === selectedJobId);
  div.dataset.status = job.status || "";
  let head = div.querySelector(":scope > .job-item-head");
  if (!head) {
    head = document.createElement("div");
    head.className = "job-item-head";
    div.insertBefore(head, detail && div.contains(detail) ? detail : div.firstChild);
  }
  const html = jobItemHeadHtml(job);
  if (head._html !== html) {
    head.innerHTML = html;
    head._html = html;
  }
  const at = [...list.children].indexOf(div);
  const want = order.findIndex((j) => j.id === job.id);
  if (at !== want) {
    list.insertBefore(div, list.children[want] || null);
  }
}

function syncJobsList(liveJobs, finishedJobs) {
  const liveList = document.getElementById("jobs-live");
  const doneList = document.getElementById("jobs-list");
  const liveEmpty = document.getElementById("jobs-live-empty");
  const doneEmpty = document.getElementById("jobs-finished-empty");
  if (!liveList || !doneList) return;
  const detail = jobDetailEl();
  const visibleIds = new Set([...liveJobs, ...finishedJobs].map((j) => j.id));
  for (const job of liveJobs) upsertJobItem(liveList, job, liveJobs);
  for (const job of finishedJobs) upsertJobItem(doneList, job, finishedJobs);
  for (const list of [liveList, doneList]) {
    for (const child of [...list.children]) {
      if (visibleIds.has(child.dataset.id)) continue;
      if (detail && child.contains(detail)) parkJobDetail();
      child.remove();
    }
  }
  liveList.classList.toggle("hidden", liveJobs.length === 0);
  if (liveEmpty) liveEmpty.classList.toggle("hidden", liveJobs.length > 0);
  if (doneEmpty) doneEmpty.classList.toggle("hidden", finishedJobs.length > 0);
}

function updateFinishedPager(total) {
  const bar = document.getElementById("jobs-finished-bar");
  const prev = document.getElementById("jobs-prev");
  const next = document.getElementById("jobs-next");
  const label = document.getElementById("jobs-finished-label");
  const pageLabel = document.getElementById("jobs-page-label");
  if (!bar || !prev || !next || !label || !pageLabel) return;
  const pages = Math.max(1, Math.ceil(total / FINISHED_PAGE_SIZE) || 1);
  if (finishedPage > pages - 1) finishedPage = pages - 1;
  if (finishedPage < 0) finishedPage = 0;
  if (!total) {
    label.textContent = "Hotové";
    pageLabel.textContent = "";
    prev.classList.add("hidden");
    next.classList.add("hidden");
    return;
  }
  const from = finishedPage * FINISHED_PAGE_SIZE + 1;
  const to = Math.min(total, (finishedPage + 1) * FINISHED_PAGE_SIZE);
  label.textContent = `Hotové ${from}–${to} z ${total}`;
  pageLabel.textContent = pages > 1 ? `${finishedPage + 1} / ${pages}` : "";
  prev.disabled = finishedPage <= 0;
  next.disabled = finishedPage >= pages - 1;
  prev.classList.toggle("hidden", pages <= 1);
  next.classList.toggle("hidden", pages <= 1);
}

function initJobsPager() {
  const prev = document.getElementById("jobs-prev");
  const next = document.getElementById("jobs-next");
  if (prev) {
    prev.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (finishedPage <= 0) return;
      finishedPage -= 1;
      loadJobs();
    });
  }
  if (next) {
    next.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      finishedPage += 1;
      loadJobs();
    });
  }
}

async function loadJobs() {
  const data = await api("/api/jobs");
  updateWorkerStatus(data);

  let justPicked = false;
  if (!selectedJobId && data.jobs.length) {
    const running = data.jobs.find((j) => j.status === "running");
    selectedJobId = (running || data.jobs[0]).id;
    logAfter = 0;
    logSettledForJob = null;
    justPicked = true;
  }

  const { live, finished } = splitJobs(data.jobs);
  const focusId = focusFinishedJobId || (justPicked ? selectedJobId : null);
  if (focusId) {
    const i = finished.findIndex((j) => j.id === focusId);
    if (i >= 0) finishedPage = Math.floor(i / FINISHED_PAGE_SIZE);
    focusFinishedJobId = null;
  }
  const pages = Math.max(1, Math.ceil(finished.length / FINISHED_PAGE_SIZE) || 1);
  if (finishedPage > pages - 1) finishedPage = pages - 1;
  if (finishedPage < 0) finishedPage = 0;
  const start = finishedPage * FINISHED_PAGE_SIZE;
  const pageJobs = finished.slice(start, start + FINISHED_PAGE_SIZE);
  updateFinishedPager(finished.length);
  syncJobsList(live, pageJobs);

  let selected = data.jobs.find((j) => j.id === selectedJobId);
  // Privátní job není ve veřejném seznamu – drž detail přes /api/jobs/{id}.
  if (!selected && selectedJobId) {
    try {
      selected = await api(`/api/jobs/${selectedJobId}`);
    } catch (_) {
      selected = null;
    }
  }

  if (generateStartedJobId) {
    const started =
      data.jobs.find((j) => j.id === generateStartedJobId) ||
      (selected && selected.id === generateStartedJobId ? selected : null);
    if (!started || !jobIsLive(started.status)) {
      clearGenerateStarted();
    }
  }

  const selectedEl = selectedJobId ? jobItemEl(selectedJobId) : null;
  if (selected && (selectedEl || selected.private)) {
    if (selectedEl) {
      const alreadyOpen = jobDetailEl()?.parentElement === selectedEl;
      if (!alreadyOpen) attachJobDetail(selectedEl);
    } else {
      // Privátní: detail v holderu (není položka v seznamu).
      parkJobDetail();
      jobDetailEl().classList.remove("hidden");
    }
    selectedJobStatus = selected.status;
    const liveJob = jobIsLive(selected.status);
    if (liveJob || logSettledForJob !== selected.id) {
      document.getElementById("detail-status").textContent =
        `Stav: ${selected.status} · ${jobScaleLabel(selected)}` +
        (selected.error ? ` · ${selected.error}` : "");
      const timingEl = document.getElementById("detail-timing");
      if (timingEl) {
        timingEl.textContent = jobTimingText(selected);
        timingEl.classList.toggle("hidden", !timingEl.textContent);
      }
      const sourcesEl = document.getElementById("detail-sources");
      if (sourcesEl) {
        sourcesEl.textContent = jobSourcesText(selected);
        sourcesEl.classList.toggle("hidden", !sourcesEl.textContent);
      }
      setJobActionLinks(selected);
      const img = document.getElementById("detail-img");
      if (selected.has_preview) {
        if (img.classList.contains("hidden")) {
          img.src = `/api/jobs/${selected.id}/preview.png?t=${Date.now()}`;
        }
        img.classList.remove("hidden");
      } else {
        img.classList.add("hidden");
      }
      if (justPicked) {
        await fillJobDetail(selectedJobId, { applyForm: false });
      }
      await refreshLog();
    }
  } else {
    parkJobDetail();
    jobDetailEl().classList.add("hidden");
    if (!selected) {
      selectedJobId = null;
      selectedJobStatus = null;
    }
  }
}

function jobDetailEl() {
  return document.getElementById("job-detail");
}

function parkJobDetail() {
  const detail = jobDetailEl();
  const holder = document.getElementById("job-detail-holder");
  if (detail && holder && detail.parentElement !== holder) {
    holder.appendChild(detail);
  }
}

function attachJobDetail(jobEl) {
  const detail = jobDetailEl();
  if (!detail || !jobEl) return;
  jobEl.appendChild(detail);
  jobEl.classList.add("expanded");
  detail.classList.remove("hidden");
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

function formatDuration(seconds) {
  if (seconds == null || seconds < 0) return "";
  const s = Math.round(Number(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h} h ${m} min`;
  if (m) return sec ? `${m} min ${sec} s` : `${m} min`;
  return `${sec} s`;
}

function formatWhen(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 19).replace("T", " ");
  return d.toLocaleString("cs-CZ", { dateStyle: "short", timeStyle: "medium" });
}

function jobTimingText(job) {
  const start = formatWhen(job.started_at || job.created_at);
  const dur = formatDuration(job.duration_s);
  if (job.status === "done" || job.status === "failed") {
    return [start ? `Start ${start}` : "", dur ? `trvání ${dur}` : ""].filter(Boolean).join(" · ");
  }
  if (job.status === "running" && dur) {
    return `${start ? `Od ${start} · ` : ""}běží ${dur}`;
  }
  return start ? `Zařazeno ${start}` : "";
}

function jobSourcesText(job) {
  const meta = job.source_meta;
  if (!meta && !job.dmp_mode) return "";
  const mode = job.dmp_mode || (meta && meta.dmp_mode);
  let dmpLabel = "DMP ?";
  if (mode === "ok") dmpLabel = "DMP OK";
  else if (mode === "1g") dmpLabel = "DMP 1G";
  else if (mode === "mixed") dmpLabel = "DMP OK+1G";
  const parts = [dmpLabel];
  if (job.dmp_degraded || (meta && meta.dmp_degraded)) {
    parts.push("degradace 1G viditelná");
  }
  const sheets = (meta && meta.sheets) || [];
  const ages = [];
  for (const sheet of sheets) {
    const dmr = (sheet.dmr && sheet.dmr.cache_age_days) ?? null;
    const dmp = (sheet.dmp && sheet.dmp.cache_age_days) ?? null;
    if (dmr != null) ages.push(dmr);
    if (dmp != null) ages.push(dmp);
  }
  if (ages.length) {
    const maxAge = Math.max(...ages);
    parts.push(`cache ~${Math.round(maxAge)} d`);
  } else if (sheets.length) {
    parts.push(`${sheets.length} list${sheets.length === 1 ? "" : "y"} SM5`);
  }
  parts.push("epochy = stáří cache, ne pořízení ČÚZK");
  return parts.join(" · ");
}

function setJobActionLinks(job) {
  const dl = document.getElementById("detail-download");
  const prev = document.getElementById("detail-preview");
  const georefBtn = document.getElementById("detail-download-georef");
  if (job.private || (job.options && job.options.private)) {
    dl.classList.add("hidden");
    prev.classList.add("hidden");
    if (georefBtn) georefBtn.classList.add("hidden");
    return;
  }
  dl.href = `/api/jobs/${job.id}/download`;
  dl.classList.toggle("hidden", !job.has_output);
  if (georefBtn) {
    georefBtn.href = `/api/jobs/${job.id}/download/georef-previews`;
    georefBtn.classList.toggle("hidden", !job.has_georef_previews);
  }
  prev.href = `/api/jobs/${job.id}/preview.png`;
  prev.classList.toggle("hidden", !job.has_preview);
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

async function fillJobDetail(id, { applyForm = false } = {}) {
  const job = await api(`/api/jobs/${id}`);
  selectedJobStatus = job.status;
  const privateNote =
    job.private || (job.options && job.options.private)
      ? " · privátní (ZIP přijde e-mailem)"
      : "";
  document.getElementById("detail-title").textContent = job.name;
  document.getElementById("detail-status").textContent =
    `Stav: ${job.status} · ${jobScaleLabel(job)}` +
    privateNote +
    (job.error ? ` · ${job.error}` : "");
  const timingEl = document.getElementById("detail-timing");
  if (timingEl) {
    timingEl.textContent = jobTimingText(job);
    timingEl.classList.toggle("hidden", !timingEl.textContent);
  }
  const sourcesEl = document.getElementById("detail-sources");
  if (sourcesEl) {
    sourcesEl.textContent = jobSourcesText(job);
    sourcesEl.classList.toggle("hidden", !sourcesEl.textContent);
  }
  if (applyForm) applyJobToForm(job);
  setJobActionLinks(job);
  const img = document.getElementById("detail-img");
  if (job.has_preview) {
    img.src = `/api/jobs/${id}/preview.png?t=${Date.now()}`;
    img.classList.remove("hidden");
  } else {
    img.classList.add("hidden");
  }
  return job;
}

async function selectJob(id) {
  // Klik na job v seznamu (nebo přepnutí) zruší zámek „Generování spuštěno“.
  if (generateStartedJobId) clearGenerateStarted();
  const same = selectedJobId === id;
  selectedJobId = id;
  if (!same) {
    logAfter = 0;
    logSettledForJob = null;
    focusFinishedJobId = id;
    document.getElementById("detail-log").textContent = "";
  }
  await fillJobDetail(id, { applyForm: true });
  if (!same) await loadJobs();
}

async function refreshLog() {
  if (!selectedJobId) return;
  if (logSettledForJob === selectedJobId) return;
  const data = await api(`/api/jobs/${selectedJobId}/log?after=${logAfter}`);
  const pre = document.getElementById("detail-log");
  const hadNew = data.lines.length > 0;
  for (const line of data.lines) {
    pre.textContent += line.line + "\n";
    logAfter = line.id;
  }
  if (jobIsLive(selectedJobStatus)) {
    if (hadNew) pre.scrollTop = pre.scrollHeight;
    return;
  }
  logSettledForJob = selectedJobId;
}

let jobSubmitInFlight = false;
/** Po úspěšném startu: světle červené „Generování spuštěno“ dokud se formulář/job nezmění. */
let generateStartedJobId = null;

function clearGenerateStarted() {
  if (!generateStartedJobId) return;
  generateStartedJobId = null;
  const btn = document.getElementById("submit-btn");
  if (btn) {
    btn.disabled = false;
    btn.classList.remove("generate-started");
  }
  updateSubmitButtonLabel();
}

function markGenerateStarted(jobId) {
  generateStartedJobId = jobId;
  const btn = document.getElementById("submit-btn");
  if (!btn) return;
  btn.disabled = true;
  btn.classList.add("generate-started");
  btn.textContent = "Generování spuštěno";
}

document.getElementById("job-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (jobSubmitInFlight || generateStartedJobId) return;
  const form = e.target;
  const btn = document.getElementById("submit-btn");
  clearFormError();
  if (!form.map_scale || !form.map_scale.value) {
    showFormError("Vyberte měřítko.");
    if (form.map_scale) form.map_scale.focus();
    return;
  }
  if (!form.contour_interval || !form.contour_interval.value) {
    showFormError("Vyberte ekvidistanci.");
    if (form.contour_interval) form.contour_interval.focus();
    return;
  }
  if (!document.getElementById("bbox-input").value) {
    showFormError("Nakreslete výřez na mapě (dva protilehlé rohy).");
    return;
  }
  if (!bboxAllowed) {
    showFormError(
      (lastSheets && lastSheets.hint) ||
        "Výřez je moc velký nebo ještě není ověřený. Max cca 36 km² (např. 6×6 km)."
    );
    return;
  }
  const privateEl = document.getElementById("private");
  if (privateEl && privateEl.checked) {
    const emailEl = document.getElementById("notify_email");
    const email = (emailEl && emailEl.value || "").trim();
    if (!email || !email.includes("@") || !email.split("@").pop().includes(".")) {
      showFormError("Privátní režim vyžaduje platný e-mail pro odkaz ke stažení.");
      if (emailEl) emailEl.focus();
      return;
    }
  }
  jobSubmitInFlight = true;
  btn.disabled = true;
  btn.textContent = "Zakládám job…";
  let startedId = null;
  try {
    const fd = new FormData(form);
    // Checkbox: vždy pošli 0/1 (unchecked jinak zmizí a API by drželo default true).
    const useKpEl = document.getElementById("use_kp");
    fd.set("use_kp", useKpEl && useKpEl.checked ? "1" : "0");
    const knollsEl = document.getElementById("include_knolls");
    fd.set("include_knolls", knollsEl && knollsEl.checked ? "1" : "0");
    fd.set("private", privateEl && privateEl.checked ? "1" : "0");
    const job = await api("/api/jobs", { method: "POST", body: fd });
    if (job && job.duplicate_skipped) {
      const msg =
        job.duplicate_message ||
        "Nový job se nezaložil – stejný výřez už běží nebo čeká.";
      showFormError(msg);
      alert(msg);
    } else if (job && (job.private || (job.options && job.options.private))) {
      const mail =
        (job.options && job.options.notify_email) ||
        (document.getElementById("notify_email") || {}).value ||
        "";
      alert(
        `Privátní job založen. Po dokončení přijde odkaz na ${mail || "váš e-mail"} ` +
          "(platí 48 hodin). Ve veřejném seznamu jobů se neobjeví."
      );
      if (job.id) startedId = job.id;
    } else if (job && job.id) {
      startedId = job.id;
    }
    await selectJob(job.id);
  } catch (err) {
    showFormError(err.message);
    alert(err.message);
    await loadJobs();
  } finally {
    jobSubmitInFlight = false;
    if (startedId) {
      // selectJob výše mohl smazat zámek; nastav až po auto-výběru nového jobu.
      markGenerateStarted(startedId);
    } else {
      btn.disabled = false;
      btn.classList.remove("generate-started");
      updateSubmitButtonLabel();
    }
  }
});

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

const BBOX_MAP_HEIGHT_KEY = "podkladarna-bbox-map-height";
const BBOX_MAP_HEIGHT_MIN = 200;
const BBOX_MAP_HEIGHT_MAX = 1200;

function clampBboxMapHeight(px) {
  const n = Math.round(Number(px));
  if (!Number.isFinite(n)) return null;
  return Math.min(BBOX_MAP_HEIGHT_MAX, Math.max(BBOX_MAP_HEIGHT_MIN, n));
}

function applyBboxMapHeight(px, { persist = false } = {}) {
  const el = document.getElementById("bbox-map");
  if (!el) return;
  const height = clampBboxMapHeight(px);
  if (height == null) return;
  el.style.height = `${height}px`;
  if (bboxMap) bboxMap.invalidateSize({ animate: false });
  if (persist) {
    try {
      localStorage.setItem(BBOX_MAP_HEIGHT_KEY, String(height));
    } catch (_) {
      /* private mode / quota */
    }
  }
}

function restoreBboxMapHeight() {
  try {
    const raw = localStorage.getItem(BBOX_MAP_HEIGHT_KEY);
    if (raw == null || raw === "") return;
    applyBboxMapHeight(raw, { persist: false });
  } catch (_) {
    /* ignore */
  }
}

function initBboxMapResize() {
  const handle = document.getElementById("bbox-map-resize");
  const el = document.getElementById("bbox-map");
  if (!handle || !el) return;

  let dragging = false;
  let startY = 0;
  let startH = 0;

  const onMove = (clientY) => {
    if (!dragging) return;
    applyBboxMapHeight(startH + (clientY - startY), { persist: false });
  };

  const stopDrag = (clientY) => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove("is-dragging");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    if (clientY != null) onMove(clientY);
    const h = clampBboxMapHeight(el.getBoundingClientRect().height);
    if (h != null) applyBboxMapHeight(h, { persist: true });
  };

  handle.addEventListener("pointerdown", (e) => {
    if (e.button != null && e.button !== 0) return;
    e.preventDefault();
    dragging = true;
    startY = e.clientY;
    startH = el.getBoundingClientRect().height;
    handle.classList.add("is-dragging");
    document.body.style.cursor = "ns-resize";
    document.body.style.userSelect = "none";
    try {
      handle.setPointerCapture(e.pointerId);
    } catch (_) {
      /* older browsers */
    }
  });

  handle.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    e.preventDefault();
    onMove(e.clientY);
  });

  handle.addEventListener("pointerup", (e) => stopDrag(e.clientY));
  handle.addEventListener("pointercancel", () => stopDrag(null));
}

function initBboxMap() {
  const clearBtn = document.getElementById("bbox-clear");
  if (clearBtn) clearBtn.addEventListener("click", clearBbox);
  const el = document.getElementById("bbox-map");
  if (!el) return;
  if (typeof L === "undefined") {
    setSheetInfo("Mapová knihovna se nenačetla (Leaflet).", "err");
    return;
  }
  restoreBboxMapHeight();
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
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
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
  initBboxMapResize();
  // Po obnovení výšky z localStorage ještě jednou po layoutu.
  requestAnimationFrame(() => {
    if (bboxMap) bboxMap.invalidateSize({ animate: false });
  });
}

function onMapClick(e) {
  if (!inCzechia(e.latlng)) {
    setSheetInfo("Výřez jen na území Česka.", "err");
    return;
  }
  if (bboxCorners.length >= 2) {
    clearBbox();
  } else if (generateStartedJobId) {
    // Nový roh výřezu = změna mapy → zelené tlačítko zpět.
    clearGenerateStarted();
  }
  bboxCorners.push(e.latlng);
  if (bboxCorners.length === 1) {
    setSheetInfo("Druhý roh výřezu…", "");
    L.circleMarker(e.latlng, { radius: 5, color: "#cc00cc" }).addTo(bboxMap);
  }
  if (bboxCorners.length === 2) {
    const b = L.latLngBounds(bboxCorners[0], bboxCorners[1]);
    bboxRect = L.rectangle(b, { color: "#cc00cc", weight: 2, fillOpacity: 0.15 }).addTo(bboxMap);
    // Bez fitBounds – uživatel už vidí výřez; dřívější maxZoom:14 zbytečně odzoomovalo.
    const west = b.getWest();
    const south = b.getSouth();
    const east = b.getEast();
    const north = b.getNorth();
    document.getElementById("bbox-input").value = [west, south, east, north].join(",");
    setReuseJob("");
    lookupSheets();
  }
}

function styleBboxRect(tooLarge) {
  if (!bboxRect) return;
  bboxRect.setStyle({
    color: tooLarge ? "#ff6600" : "#cc00cc",
    weight: 2,
    fillOpacity: 0.15,
  });
}

function setReuseJob(id) {
  const el = document.getElementById("reuse-job-id");
  if (el) el.value = id || "";
}

function updateOsmHintsForScale(scale) {
  const hint = document.getElementById("osm-priority-hint");
  if (!hint) return;
  const s = Number(scale);
  if (s === 4000) {
    hint.textContent =
      "Sprintový výřez: stáhne ploty, zdi, brány, přístřešky, pomníky… Budovy ze ZABAGED.";
  } else if (s === 7500 || s === 10000 || s === 15000) {
    hint.textContent =
      "Les / MTBO: urban pack (ploty, brány…) často zbytečný – spíš vypnout. Studny a hřiště podle nastavení níže.";
  } else {
    hint.textContent =
      "Urban pack z OSM (ploty, zdi, brány, pomníky…). U sprintu užitečné; v lese často vypnout.";
  }
}

function updateCliffControls() {
  const cliff = document.getElementById("kp_cliff_symbol");
  const sens = document.getElementById("kp_cliff_sensitivity");
  const sensLabel = document.getElementById("cliff-sensitivity-label");
  const sensHint = document.getElementById("cliff-sensitivity-hint");
  const useKp = document.getElementById("use_kp");
  if (!cliff || !sens) return;
  const off = cliff.value === "off";
  sens.disabled = off;
  if (sensLabel) sensLabel.style.opacity = off ? "0.45" : "";
  if (sensHint) {
    if (off) {
      sensHint.textContent =
        "Citlivost se při „Nevykreslovat“ nepoužije – srázy se nepočítají.";
    } else if (useKp && !useKp.checked) {
      sensHint.textContent =
        "Bez KP: jak přísně hledat strmé skoky v DMR. Skála a zem se rozliší sklonem; knolly jsou samostatná volba.";
    } else {
      sensHint.textContent =
        "Jak přísně Karttapullautin hledá strmé skoky v DMR.";
    }
  }
  const symbolHint = document.getElementById("cliff-symbol-hint");
  if (symbolHint) {
    symbolHint.textContent =
      useKp && !useKp.checked
        ? "Bez KP: strmý schod = skála (201), mírnější = zem (104). „Vše jako…“ přebije detektor. Vypnuto = ani nepočítat. Knolly (109) jsou vedle, z DEM."
        : "S KP kreslí srázy Karttapullautin a tahle volba přebarví všechny čárky (104 / 201 / 206 / vypnuto). Automaticky u KP zůstane zem, dokud v temp není samostatný soubor skály.";
  }
}

function updateUseKpHints() {
  const useKp = document.getElementById("use_kp");
  const hint = document.getElementById("use-kp-hint");
  const outHint = document.getElementById("output-omap-hint");
  const outMode = document.getElementById("output_mode");
  if (useKp && !useKp.checked && outMode && outMode.value === "png") {
    outMode.value = "png_zip";
  }
  if (useKp && hint) {
    hint.innerHTML = useKp.checked
      ? "Výchozí stav formuláře: <strong>KP zapnuto</strong> (hybrid s náhledem). Odškrtni, nebo použij „Bez KP (experimentální)“ — globální default se sám nepřepíná."
      : "Bez KP: primární výstup je <strong>.omap</strong> v ZIPu (vegetace z hustoty LiDAR odrazů, srázy z DMR, vrstevnice jen GDAL). Webový náhled PNG se neskládá; chybějící KP vegetation.png job neshodí. ČÚZK reference v ZIPu zůstávají.";
  }
  if (useKp && outHint) {
    outHint.innerHTML = useKp.checked
      ? "S <strong>KP</strong>: PNG je rychlý rastrový náhled (pullautus), ne finální mapa. ZIP obsahuje <code>.omap</code> podle měřítka plus vektory pro OOM."
      : "Bez KP: primární výstup je <code>.omap</code> v ZIPu. Webový náhled PNG se neskládá. ČÚZK referenční PNG (orto, hillshade, …) v ZIPu zůstávají.";
  }
  updateCliffControls();
}

function applyBezKpPreset() {
  clearGenerateStarted();
  const useKp = document.getElementById("use_kp");
  const outMode = document.getElementById("output_mode");
  if (useKp) useKp.checked = false;
  if (outMode) outMode.value = "png_zip";
  updateUseKpHints();
}

function applyJobToForm(job) {
  const form = document.getElementById("job-form");
  if (!form) return;
  const opts = job.options || {};
  let scale = opts.map_scale != null ? Number(opts.map_scale) : NaN;
  if (!Number.isFinite(scale) && opts.scalefactor != null) {
    scale = Math.round(Number(opts.scalefactor) * 10000);
  }
  if (!Number.isFinite(scale) && job.preset_id) {
    const pid = String(job.preset_id);
    if (pid.startsWith("sprint")) scale = 4000;
    else if (pid === "forest_7500") scale = 7500;
    else if (pid.includes("15000")) scale = 15000;
    else scale = 10000;
  }
  const scaleSel = form.map_scale;
  if (scaleSel && Number.isFinite(scale)) {
    const match = [...scaleSel.options].find((o) => Number(o.value) === scale);
    if (match) scaleSel.value = match.value;
  }
  rebuildContourOptions(opts.contour_interval);
  updateOsmHintsForScale(scaleSel ? scaleSel.value : scale);
  const cliff = form.kp_cliff_symbol;
  if (cliff) {
    const cliffVal = (job.options || {}).kp_cliff_symbol || "auto";
    if ([...cliff.options].some((o) => o.value === cliffVal)) {
      cliff.value = cliffVal;
    }
  }
  const knolls = form.include_knolls;
  if (knolls) {
    knolls.checked =
      opts.include_knolls == null ? true : Boolean(opts.include_knolls);
  }
  const vege = form.kp_vege_height;
  if (vege) {
    const raw = (job.options || {}).kp_vege_height;
    const vegeVal = raw == null || raw === "" ? "2" : String(raw);
    const match = [...vege.options].find(
      (o) => Math.abs(parseFloat(o.value) - parseFloat(vegeVal)) < 1e-6
    );
    if (match) vege.value = match.value;
  }
  const sens = form.kp_cliff_sensitivity;
  if (sens) {
    const sensVal = (job.options || {}).kp_cliff_sensitivity || "low";
    if ([...sens.options].some((o) => o.value === sensVal)) {
      sens.value = sensVal;
    }
  }
  updateCliffControls();
  const benches = form.kp_osm_benches;
  if (benches) {
    benches.checked = Boolean(
      opts.kp_osm_benches || opts.kp_osm_furniture
    );
  }
  const lamps = form.kp_osm_lamps;
  if (lamps) {
    lamps.checked = Boolean(opts.kp_osm_lamps || opts.kp_osm_furniture);
  }
  const playEq = form.kp_osm_playground_equipment;
  if (playEq) {
    playEq.checked = Boolean(opts.kp_osm_playground_equipment);
  }
  const priority = form.kp_osm_priority;
  if (priority) {
    priority.checked =
      opts.kp_osm_priority == null ? true : Boolean(opts.kp_osm_priority);
  }
  const footwaySidewalk = form.kp_osm_footway_as_sidewalk;
  if (footwaySidewalk) {
    footwaySidewalk.checked = Boolean(opts.kp_osm_footway_as_sidewalk);
  }
  const courtyard = form.sprint_courtyard_olive;
  if (courtyard) {
    courtyard.checked =
      opts.sprint_courtyard_olive == null
        ? true
        : Boolean(opts.sprint_courtyard_olive);
  }
  const residualPaved = form.sprint_residual_paved;
  if (residualPaved) {
    residualPaved.checked = Boolean(opts.sprint_residual_paved);
  }
  const residualSize = form.sprint_residual_size;
  if (residualSize) {
    const sizeVal = opts.sprint_residual_size || "small";
    if ([...residualSize.options].some((o) => o.value === sizeVal)) {
      residualSize.value = sizeVal;
    }
  }
  const ostatni = form.ostatni_plocha;
  if (ostatni) {
    const ostatniVal = opts.ostatni_plocha || "small";
    if ([...ostatni.options].some((o) => o.value === ostatniVal)) {
      ostatni.value = ostatniVal;
    }
  }
  const ostatni403 = form.ostatni_plocha_as_403;
  if (ostatni403) {
    ostatni403.checked = Boolean(opts.ostatni_plocha_as_403);
  }
  const outMode = form.output_mode;
  if (outMode) {
    const wantZip = opts.output_zip == null ? true : Boolean(opts.output_zip);
    outMode.value = wantZip ? "png_zip" : "png";
  }
  const outRefs = form.output_references;
  if (outRefs) {
    outRefs.checked =
      opts.output_references == null ? true : Boolean(opts.output_references);
  }
  const oomDpi = form.oom_export_dpi;
  if (oomDpi) {
    const dpiVal = String(opts.oom_export_dpi == null ? 600 : opts.oom_export_dpi);
    if ([...oomDpi.options].some((o) => o.value === dpiVal)) {
      oomDpi.value = dpiVal;
    }
  }
  const bbox = opts.bbox_wgs84;
  if (Array.isArray(bbox) && bbox.length === 4) {
    applyBbox(bbox[0], bbox[1], bbox[2], bbox[3], {
      reuseJobId: job.has_reusable_lidar ? job.id : "",
    });
  } else {
    setReuseJob("");
  }
}

function applyBbox(west, south, east, north, extra = {}) {
  if (!bboxMap || typeof L === "undefined") return;
  clearBbox({ keepReuse: true });
  setReuseJob(extra.reuseJobId || "");
  const sw = L.latLng(south, west);
  const ne = L.latLng(north, east);
  bboxCorners = [sw, ne];
  const b = L.latLngBounds(sw, ne);
  bboxRect = L.rectangle(b, { color: "#cc00cc", weight: 2, fillOpacity: 0.15 }).addTo(bboxMap);
  bboxMap.fitBounds(b, { padding: [24, 24], maxZoom: 16 });
  document.getElementById("bbox-input").value = [west, south, east, north].join(",");
  lookupSheets();
}

function clearBbox(opts = {}) {
  // Programatický applyBbox (keepReuse) nesmí shodit zámek po odeslání;
  // uživatelské vymazání / překreslení výřezu ano.
  if (!opts.keepReuse) clearGenerateStarted();
  bboxCorners = [];
  bboxRect = null;
  bboxAllowed = false;
  lastSheets = null;
  document.getElementById("bbox-input").value = "";
  const sheetsInput = document.getElementById("sm5-sheets-input");
  if (sheetsInput) sheetsInput.value = "";
  if (!opts.keepReuse) setReuseJob("");
  if (bboxMap) {
    bboxMap.eachLayer((layer) => {
      if (layer instanceof L.Rectangle || layer instanceof L.CircleMarker) {
        bboxMap.removeLayer(layer);
      }
    });
  }
  setSheetInfo("Nakreslete obdélník dvěma kliknutími.", "");
  updateSubmitButtonLabel();
}

function setSheetInfo(text, cls) {
  const el = document.getElementById("sheet-info");
  el.textContent = text;
  el.className = "sheet-info" + (cls ? " " + cls : "");
}

function selectedEstimateMinutes() {
  if (!lastSheets || lastSheets.estimate_minutes == null) return null;
  const refs = document.getElementById("output_references");
  const wantRefs = !refs || refs.checked;
  if (wantRefs && lastSheets.estimate_minutes_with_refs != null) {
    return lastSheets.estimate_minutes_with_refs;
  }
  return lastSheets.estimate_minutes;
}

function updateSubmitButtonLabel() {
  const btn = document.getElementById("submit-btn");
  if (!btn || jobSubmitInFlight || generateStartedJobId) return;
  const mins = selectedEstimateMinutes();
  if (mins != null && bboxAllowed) {
    btn.textContent = `Spustit generování (~${mins} min)`;
  } else {
    btn.textContent = "Spustit generování";
  }
}

async function lookupSheets() {
  const bbox = document.getElementById("bbox-input").value;
  if (!bbox) return;
  bboxAllowed = false;
  lastSheets = null;
  updateSubmitButtonLabel();
  const sheetsInput = document.getElementById("sm5-sheets-input");
  if (sheetsInput) sheetsInput.value = "";
  setSheetInfo("Zjišťuji mapové listy SM5…", "");
  try {
    const data = await api(`/api/sheets?bbox=${encodeURIComponent(bbox)}`);
    lastSheets = data;
    if (data.too_large) {
      styleBboxRect(true);
      setSheetInfo(
        data.hint ||
          `Výřez je moc velký (max ${data.max_area_km2 || 36} km²). Zmenšete ho.`,
        "warn"
      );
      updateSubmitButtonLabel();
      return;
    }
    if (!data.count) {
      styleBboxRect(false);
      setSheetInfo(data.label, "err");
      updateSubmitButtonLabel();
      return;
    }
    styleBboxRect(false);
    bboxAllowed = true;
    if (sheetsInput && Array.isArray(data.sheets)) {
      sheetsInput.value = data.sheets.map((s) => s.mapnom).filter(Boolean).join(",");
    }
    const size = `${data.width_km} × ${data.height_km} km`;
    const area =
      data.area_km2 != null ? ` (${data.area_km2} km²)` : "";
    setSheetInfo(`${data.label} · ${size}${area}.`, "ok");
    updateSubmitButtonLabel();
  } catch (err) {
    styleBboxRect(true);
    setSheetInfo(err.message, "err");
    updateSubmitButtonLabel();
  }
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    await loadJobs();
  }, 2500);
}

async function loadWhatsNew() {
  const box = document.getElementById("whats-new");
  if (!box) return;
  try {
    const data = await api("/api/whats_new");
    const badge = document.getElementById("whats-new-badge");
    const title = document.getElementById("whats-new-title");
    const meta = document.getElementById("whats-new-meta");
    const lead = document.getElementById("whats-new-lead");
    const disclaimer = document.getElementById("whats-new-disclaimer");
    const list = document.getElementById("whats-new-list");
    const tone = data.tone || "calm";
    box.classList.remove("tone-hot", "tone-warm", "tone-mild", "tone-calm");
    box.classList.add(`tone-${tone}`);
    box.open = Boolean(data.open);
    if (badge) badge.textContent = data.label || "Novinky";
    if (title) {
      title.textContent = `v${data.version || "?"} · Co je nového`;
    }
    if (meta) {
      meta.textContent = data.age_label
        ? `nasazeno ${data.age_label}`
        : "";
    }
    if (disclaimer) {
      disclaimer.textContent =
        data.disclaimer ||
        "Jde o nový build s opravami a úpravami. Může se stát, že se při tom něco jiného rozbilo.";
    }
    if (lead) {
      lead.textContent =
        data.entries_lead ||
        ((data.entries || []).length
          ? "Nedávné změny (scrollujte pro další):"
          : "V přehledu nejsou žádné větší změny.");
    }
    if (list) {
      list.innerHTML = "";
      for (const entry of data.entries || []) {
        const li = document.createElement("li");
        const date = document.createElement("span");
        date.className = "whats-new-date";
        date.textContent = entry.date || "";
        const strong = document.createElement("strong");
        strong.textContent = entry.title || "";
        li.appendChild(date);
        li.appendChild(strong);
        list.appendChild(li);
      }
    }
    box.hidden = false;
  } catch (_) {
    box.hidden = true;
  }
}

loadWhatsNew();
loadMapOptions().then(loadJobs).then(startPolling);
initJobsPager();
initBboxMap();
(() => {
  const form = document.getElementById("job-form");
  if (form) {
    form.addEventListener("input", clearGenerateStarted);
    form.addEventListener("change", clearGenerateStarted);
  }
  const refs = document.getElementById("output_references");
  if (refs) refs.addEventListener("change", updateSubmitButtonLabel);
  updateSubmitButtonLabel();
})();
(() => {
  const scaleSel = document.getElementById("map_scale");
  if (!scaleSel) return;
  const sync = () => updateOsmHintsForScale(scaleSel.value);
  scaleSel.addEventListener("change", sync);
  sync();
})();
(() => {
  const cliff = document.getElementById("kp_cliff_symbol");
  if (!cliff) return;
  cliff.addEventListener("change", updateCliffControls);
  updateCliffControls();
})();
(() => {
  const useKp = document.getElementById("use_kp");
  if (useKp) {
    useKp.addEventListener("change", updateUseKpHints);
    updateUseKpHints();
  }
  const preset = document.getElementById("preset-bez-kp");
  if (preset) preset.addEventListener("click", applyBezKpPreset);
})();
(() => {
  const privateEl = document.getElementById("private");
  const wrap = document.getElementById("private-email-wrap");
  const emailEl = document.getElementById("notify_email");
  if (!privateEl || !wrap) return;
  const sync = () => {
    const on = privateEl.checked;
    wrap.classList.toggle("hidden", !on);
    if (emailEl) emailEl.required = on;
  };
  privateEl.addEventListener("change", sync);
  sync();
})();
