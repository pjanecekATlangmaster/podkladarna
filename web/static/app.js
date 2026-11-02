function parseApiError(res, text) {
  let detail = null;
  try {
    const data = JSON.parse(text);
    if (Array.isArray(data.detail)) {
      detail = data.detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
    } else if (typeof data.detail === "string") {
      detail = data.detail;
    }
  } catch (_) {
    /* not JSON */
  }

  if (res.status === 413) {
    return (
      "Soubor je příliš velký pro reverse proxy (HTTP 413). " +
      "Na Synology zvyšte client_max_body_size v nginx (viz DEPLOY.md), " +
      "nebo nahrajte soubory přes http://IP_NAS:8672 bez HTTPS proxy."
    );
  }
  // 503 u create-job = fronta; u feedback/SMTP tohle nesmí přepsat detail.
  if (res.status === 503) {
    return (
      detail ||
      "Server je zaneprázdněn – fronta generování je plná. " +
        "Počkejte na dokončení běžících jobů a zkuste to znovu."
    );
  }
  if (res.status === 429) {
    return (
      detail ||
      "Limit jobů z vaší sítě – počkejte na dokončení běžících generování."
    );
  }
  if (res.status === 502 || res.status === 504) {
    return (
      detail ||
      `Proxy timeout (HTTP ${res.status}). LAZ soubory jsou velké – ` +
        "prodlužte timeout reverse proxy nebo uploadujte přímo na port 8672."
    );
  }
  if (detail) return detail;
  const trimmed = (text || "").trim();
  if (trimmed.includes("Request Entity Too Large")) {
    return parseApiError({ status: 413 }, text);
  }
  return trimmed.slice(0, 800) || res.statusText || `HTTP ${res.status}`;
}

function showFormError(message) {
  clearFormNotice();
  const el = document.getElementById("form-error");
  el.textContent = message;
  el.classList.remove("hidden");
}

function clearFormError() {
  const el = document.getElementById("form-error");
  el.textContent = "";
  el.classList.add("hidden");
}

function showFormNotice(message) {
  clearFormError();
  const el = document.getElementById("form-notice");
  if (!el) return;
  el.textContent = message;
  el.classList.remove("hidden");
}

function clearFormNotice() {
  const el = document.getElementById("form-notice");
  if (!el) return;
  el.textContent = "";
  el.classList.add("hidden");
}

function jobIsPrivate(job) {
  return Boolean(job && (job.private || (job.options && job.options.private)));
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
const FINISHED_PAGE_SIZE = 8;
let finishedShown = FINISHED_PAGE_SIZE;
let bboxMap = null;
let bboxCorners = [];
let bboxRect = null;
let bboxHandles = [];
let bboxFirstCorner = null;
let bboxMaxAreaKm2 = 25;
// Sprint (1:4000) má přísnější strop plochy (server: SPRINT_MAX_BBOX_AREA_KM2, vrací /api/sheets).
let sprintMaxAreaKm2 = 9;
let sheetsLookupSeq = 0;
let bboxAllowed = false;
let lastSheets = null;
// Stav odznaku výřezu na mapě: rozpracovaný (tažení) a chyba dotazu na listy.
let bboxDraft = null;
let sheetsError = null;
let bboxBadgeEl = null;
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

/** Zaškrtnuté typy map (les / mtbo) v pořadí les, mtbo. */
function selectedMapTypes() {
  const out = [];
  for (const t of ["les", "mtbo"]) {
    const el = document.getElementById(`map_type_${t}`);
    if (el && el.checked) out.push(t);
  }
  return out;
}

/** Karty „Typ mapy“: povolená měřítka a výchozí měřítko podle typu. */
const MAP_KIND_SCALES = { sprint: [4000], les: [7500, 10000, 15000], mtbo: [10000, 15000] };
const MAP_KIND_DEFAULT_SCALE = { sprint: 4000, les: 10000, mtbo: 15000 };
let allScaleOptions = null;

function currentMapKind() {
  const el = document.querySelector('input[name="map_kind_ui"]:checked');
  return el ? el.value : "les";
}

/** Typ mapy podle měřítka a typů (4000 → sprint; jen mtbo → MTBO; jinak les). */
function mapKindFor(scale, types) {
  if (Number(scale) === 4000) return "sprint";
  if (Array.isArray(types) && types.length === 1 && types[0] === "mtbo") return "mtbo";
  return "les";
}

/** Nabídka měřítek jen pro zvolený typ; zachová měřítko, je-li povolené, jinak výchozí. */
function filterScaleOptions(kind, preferred) {
  const sel = document.getElementById("map_scale");
  if (!sel) return;
  if (!allScaleOptions) allScaleOptions = [...sel.options].filter((o) => o.value);
  const allowed = MAP_KIND_SCALES[kind] || MAP_KIND_SCALES.les;
  let want = Number(preferred != null ? preferred : sel.value);
  if (!allowed.includes(want)) want = MAP_KIND_DEFAULT_SCALE[kind];
  sel.innerHTML = "";
  for (const o of allScaleOptions) {
    if (allowed.includes(Number(o.value))) sel.appendChild(o);
  }
  sel.value = String(want);
}

/** Viditelnost a popisky checkboxů: ten odpovídající kartě je skrytý a zaškrtnutý, druhý je „Navíc“. */
function updateMapKindUi() {
  const kind = currentMapKind();
  for (const t of ["les", "mtbo"]) {
    document.getElementById(`map_type_${t}_row`)?.classList.toggle("hidden", t === kind);
  }
}

/** Přepnutí karty: přefiltruje měřítka a spustí stejnou logiku jako ruční změna měřítka. */
function setMapKind(kind, preferredScale) {
  const radio = document.querySelector(`input[name="map_kind_ui"][value="${kind}"]`);
  if (radio) radio.checked = true;
  filterScaleOptions(kind, preferredScale);
  const scale = Number(document.getElementById("map_scale").value);
  rebuildContourOptions(mapOptions.default_contour_by_scale[scale]);
  applyMapTypeDefaults(scale);
}

function setMapTypes(types) {
  for (const t of ["les", "mtbo"]) {
    const el = document.getElementById(`map_type_${t}`);
    if (el) el.checked = types.includes(t);
  }
}

/** Při změně měřítka: výchozí typy map a zobrazení bloku (sprint ho nemá). */
function applyMapTypeDefaults(scale) {
  const wrap = document.getElementById("map-types-wrap");
  if (wrap) wrap.classList.toggle("hidden", !(Number(scale) > 5000));
  // Typ mapy z karty má přednost před výchozím podle měřítka; druhý typ zůstává, jak je.
  if (Number(scale) > 5000) {
    const kind = currentMapKind();
    const el = document.getElementById(`map_type_${kind === "mtbo" ? "mtbo" : "les"}`);
    if (el) el.checked = true;
  }
  updateMapKindUi();
  // Mezery v residential kreslí jen sprint (les a MTBO je nepoužijí).
  const residualWrap = document.getElementById("residual-paved-wrap");
  if (residualWrap) residualWrap.classList.toggle("hidden", Number(scale) !== 4000);
  updateOutputHints();
  updateSubmitButtonLabel();
  // Sprint má menší strop plochy: ověřený výřez znovu posoudit podle měřítka.
  applySheetsLimit();
}

function disciplinesForScale(scale) {
  const s = Number(scale);
  if (s === 4000) {
    return {
      count: 1,
      text: "1× .omap: jen sprint (ISSprOM)",
    };
  }
  if (s === 7500 || s === 10000 || s === 15000) {
    const types = selectedMapTypes();
    if (!types.length) {
      return { count: 0, text: "Vyberte alespoň jeden typ mapy (les nebo MTBO)." };
    }
    const names = types.map((t) => (t === "mtbo" ? "MTBO" : "les")).join(" + ");
    const tail = s === 7500 && types.length === 2 ? " (oba 1:7500) — bez sprintu" : ` (1:${s})`;
    return { count: types.length, text: `${types.length}× .omap: ${names}${tail}` };
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
    const mt = Array.isArray(opts.map_types) && scale > 5000
      ? ` · ${opts.map_types.map((t) => (t === "mtbo" ? "MTBO" : "les")).join(" + ")}`
      : "";
    return `1:${scale} · ${cTxt} m${mt}`;
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
  // Výchozí karta podle dnešního výchozího měřítka (10000 → Les).
  filterScaleOptions(currentMapKind(), scaleSel ? scaleSel.value : null);
  rebuildContourOptions();
  if (scaleSel) applyMapTypeDefaults(scaleSel.value);
  for (const r of document.querySelectorAll('input[name="map_kind_ui"]')) {
    r.addEventListener("change", () => {
      if (r.checked) {
        setMapTypes([]);
        // Klik na kartu = výchozí měřítko typu (MTBO 1:15 000, les 1:10 000).
        setMapKind(r.value, MAP_KIND_DEFAULT_SCALE[r.value]);
      }
    });
  }
  if (scaleSel) {
    scaleSel.addEventListener("change", () => {
      const scale = Number(scaleSel.value);
      rebuildContourOptions(mapOptions.default_contour_by_scale[scale]);
      applyMapTypeDefaults(scale);
    });
  }
  for (const t of ["les", "mtbo"]) {
    const el = document.getElementById(`map_type_${t}`);
    if (el) {
      el.addEventListener("change", () => {
        updateOutputHints();
        updateSubmitButtonLabel();
      });
    }
  }
}

/** Log úspěšně dokončeného jobu (i s opakovanými pokusy o stažení) se neukazuje; u chyby a běhu ano. */
function syncDetailLogVisibility(job) {
  const box = document.getElementById("detail-log-box");
  if (!box) return;
  box.classList.toggle("hidden", job.status === "done");
  // U neúspěšného jobu je technický log hned vidět.
  if (job.status === "failed" && box.dataset.autoOpened !== job.id) {
    box.open = true;
    box.dataset.autoOpened = job.id;
  }
}

// --- Paměť v prohlížeči (localStorage může v anonymním okně házet) ---
const MY_JOBS_KEY = "podkladarna-my-jobs";
const JOBS_TAB_KEY = "podkladarna-jobs-tab";
const LAST_OPTIONS_KEY = "podkladarna-last-options";
const MY_JOBS_MAX = 30;

function storageGet(key) {
  try {
    return localStorage.getItem(key);
  } catch (_) {
    return null;
  }
}

function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (_) {
    /* bez paměti se stránka chová jako dřív */
  }
}

function storageRemove(key) {
  try {
    localStorage.removeItem(key);
  } catch (_) {
    /* viz výše */
  }
}

function loadMyJobs() {
  try {
    const arr = JSON.parse(storageGet(MY_JOBS_KEY) || "[]");
    if (!Array.isArray(arr)) return [];
    return arr.filter((r) => r && typeof r.id === "string" && r.id).slice(0, MY_JOBS_MAX);
  } catch (_) {
    return [];
  }
}

let myJobs = loadMyJobs();

function saveMyJobs() {
  storageSet(MY_JOBS_KEY, JSON.stringify(myJobs));
}

function myJobRecord(id) {
  return myJobs.find((r) => r.id === id) || null;
}

function isMyJob(id) {
  return Boolean(id && myJobRecord(id));
}

function addMyJob(job, estimateMinutes) {
  if (!job || !job.id) return;
  const rec = {
    id: job.id,
    name: job.name || "",
    created_at: job.created_at || new Date().toISOString(),
    estimate_minutes: Number.isFinite(estimateMinutes) ? estimateMinutes : null,
  };
  if (jobIsPrivate(job)) rec.private = true;
  myJobs = [rec, ...myJobs.filter((r) => r.id !== job.id)].slice(0, MY_JOBS_MAX);
  saveMyJobs();
}

function removeMyJob(id) {
  myJobs = myJobs.filter((r) => r.id !== id);
  saveMyJobs();
}

let jobsTab = "all";

function initJobsTab() {
  const stored = storageGet(JOBS_TAB_KEY);
  jobsTab = stored === "mine" || stored === "all" ? stored : myJobs.length ? "mine" : "all";
}

function syncJobsTabs() {
  const mine = document.getElementById("jobs-tab-mine");
  const all = document.getElementById("jobs-tab-all");
  if (mine) mine.textContent = `Moje mapy (${myJobs.length})`;
  for (const tab of [mine, all]) {
    if (!tab) continue;
    const on = tab.dataset.tab === jobsTab;
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
    tab.classList.toggle("active", on);
  }
}

function setJobsTab(tab, { focus = false } = {}) {
  jobsTab = tab;
  storageSet(JOBS_TAB_KEY, tab);
  syncJobsTabs();
  if (focus) document.getElementById(`jobs-tab-${tab}`)?.focus();
  loadJobs();
}

function initJobsTabs() {
  initJobsTab();
  const tabs = ["mine", "all"].map((t) => document.getElementById(`jobs-tab-${t}`));
  tabs.forEach((el) => {
    if (!el) return;
    el.addEventListener("click", () => setJobsTab(el.dataset.tab));
    el.addEventListener("keydown", (e) => {
      let next = null;
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        next = el.dataset.tab === "mine" ? "all" : "mine";
      } else if (e.key === "Home") next = "mine";
      else if (e.key === "End") next = "all";
      if (next) {
        e.preventDefault();
        setJobsTab(next, { focus: true });
      }
    });
  });
  syncJobsTabs();
}

/** Záznam z „Mojí mapy“, který není ve veřejném seznamu (soukromá mapa), jako položka seznamu. */
function savedRecordAsJob(rec) {
  return {
    id: rec.id,
    name: rec.name || "Mapa",
    created_at: rec.created_at,
    status: "saved",
    _saved: true,
    _private: Boolean(rec.private),
  };
}

let savedJobsVerified = false;

/** Jednou za načtení stránky: záznamy, které server už nezná (404/410), tiše zahodit. */
async function verifySavedJobs(publicJobs) {
  if (savedJobsVerified) return;
  savedJobsVerified = true;
  const known = new Set(publicJobs.map((j) => j.id));
  const missing = myJobs.filter((r) => !known.has(r.id));
  const gone = [];
  await Promise.all(
    missing.map(async (rec) => {
      try {
        const res = await fetch(`/api/jobs/${encodeURIComponent(rec.id)}`);
        if (res.status === 404 || res.status === 410) gone.push(rec.id);
      } catch (_) {
        /* síť: záznam necháme */
      }
    })
  );
  if (gone.length) {
    myJobs = myJobs.filter((r) => !gone.includes(r.id));
    saveMyJobs();
  }
}

// --- Lidská věta k chybě jobu ---
/** Stav v detailu: přepiš jen při změně, ať aria-live neohlašuje každý polling. */
function setDetailStatus(text) {
  const el = document.getElementById("detail-status");
  if (el && el.textContent !== text) el.textContent = text;
}

function friendlyJobError(errorText, { short = false } = {}) {
  const text = String(errorText || "");
  if (/ČÚZK|ATOM|[Tt]ime ?out|Overpass|HTTP (?:Error )?(?:429|5\d\d)|Connection(?:Error| reset| refused)/.test(text)) {
    return "Stahování dat ze serveru se nepovedlo (výpadek nebo pomalá odpověď). Zkuste to prosím za chvíli znovu.";
  }
  return short
    ? "Generování se nepovedlo."
    : "Generování se nepovedlo. Technický popis je níže; můžete to zkusit znovu nebo nám napsat.";
}

// --- Průběh mapy po krocích (z hlaviček v logu, viz app/pipeline/job_progress.py) ---
const PROGRESS_LINE_RE = /^\s*(Plán \(|\[\d+\/\d+\]|Hotovo za )/;

function capitalizeCs(text) {
  const t = String(text || "").trim();
  return t.charAt(0).toUpperCase() + t.slice(1);
}

/** Čas „m:ss“ nebo „h:mm:ss“ na sekundy. */
function clockToSeconds(txt) {
  const parts = String(txt).split(":").map(Number);
  if (parts.some((n) => !Number.isFinite(n))) return null;
  return parts.reduce((acc, n) => acc * 60 + n, 0);
}

/**
 * Z řádků logu poskládá stav kroků:
 * {steps:[{label, state:'done'|'now'|'skipped'|'todo', note}], current, total, elapsed, finished}.
 * Detailní (odsazené) řádky se ignorují, protože hlavičku nepřipomínají.
 */
function parseJobProgress(lines) {
  let steps = [];
  let finished = false;
  let elapsed = null;
  let currentIdx = -1;
  const ensure = (total) => {
    while (steps.length < total) steps.push({ label: "", state: "todo", note: "" });
  };
  for (const raw of lines) {
    const line = String(raw || "").trim();
    let m = line.match(/^Plán \((\d+) krok[^)]*\):\s*(.+)$/);
    if (m) {
      const labels = m[2].split(" · ").map(capitalizeCs);
      steps = labels.map((label) => ({ label, state: "todo", note: "" }));
      ensure(Number(m[1]));
      continue;
    }
    m = line.match(/^\[(\d+)\/(\d+)\]\s+(.+)$/);
    if (m) {
      const n = Number(m[1]);
      const total = Number(m[2]);
      if (n < 1 || n > total) continue;
      ensure(total);
      let body = m[3];
      for (let i = 0; i < n - 1; i++) {
        if (steps[i].state === "now" || steps[i].state === "todo") steps[i].state = "done";
      }
      const skip = body.match(/^(.+?)\s+–\s+přeskočeno(?:\s*\((.+)\))?\s*$/);
      if (skip) {
        steps[n - 1] = { label: capitalizeCs(skip[1]), state: "skipped", note: skip[2] || "" };
        continue;
      }
      const timed = body.match(/^(.+?)\s+·\s+(\d+(?::\d{2}){1,2})\s*$/);
      if (timed) {
        body = timed[1];
        elapsed = clockToSeconds(timed[2]);
      }
      steps[n - 1] = { label: capitalizeCs(body), state: "now", note: "" };
      currentIdx = n - 1;
      continue;
    }
    if (line.startsWith("Hotovo za ")) {
      finished = true;
      for (const st of steps) if (st.state === "now" || st.state === "todo") st.state = "done";
    }
  }
  const nowIdx = steps.findIndex((st) => st.state === "now");
  const doneCount = steps.filter((st) => st.state === "done" || st.state === "skipped").length;
  return {
    steps,
    current: finished ? steps.length : nowIdx >= 0 ? nowIdx + 1 : Math.max(currentIdx + 1, doneCount),
    total: steps.length,
    elapsed,
    finished,
  };
}

/** Odhad zbývajícího času podle uloženého odhadu mapy a uplynulých sekund. */
function remainingText(estimateMinutes, elapsedSec) {
  if (!Number.isFinite(estimateMinutes) || estimateMinutes <= 0 || elapsedSec == null) return "";
  const left = estimateMinutes * 60 - elapsedSec;
  if (left <= 0) return "déle než odhad";
  if (left < 60) return "zbývá méně než minuta";
  return `zbývá asi ${Math.ceil(left / 60)} min`;
}

function jobIsLive(status) {
  return status === "running" || status === "queued" || status === "pending";
}

/** Stav jobu česky pro uživatele (technický kód zůstává v třídě CSS). */
const JOB_STATUS_LABELS = {
  pending: "čeká",
  queued: "ve frontě",
  running: "běží",
  done: "hotovo",
  done_with_errors: "hotovo s upozorněním",
  failed: "nepodařilo se",
};

/** Krátký štítek stavu v seznamu map (kromě barvy i slovo). */
const JOB_STATUS_TAGS = {
  pending: "ve frontě",
  queued: "ve frontě",
  running: "běží",
  done: "hotovo",
  done_with_errors: "hotovo s chybami",
  failed: "chyba",
  saved: "soukromá",
};

function jobStatusTag(status) {
  return JOB_STATUS_TAGS[status] || status || "";
}

function jobStatusLabel(status) {
  return JOB_STATUS_LABELS[status] || status;
}

/** Doběhl (i s chybami nebo neúspěšně) – má konečné trvání. */
function jobIsFinished(status) {
  return status === "done" || status === "done_with_errors" || status === "failed";
}

/** Stavový řádek bez úvodního slova stavu, které už ukazuje štítek (fáze, fronta, trvání, privátní zůstanou). */
function statusLineText(job, tail) {
  const lead = jobStatusLabel(job.status) === jobStatusTag(job.status) ? "" : jobStatusLabel(job.status);
  const phase = job.phase && !jobIsFinished(job.status) ? " · " + job.phase : "";
  return (lead + phase + tail).replace(/^ · /, "");
}

function jobItemHeadHtml(job) {
  let queueLabel = "";
  if (job.phase === "waiting_server") {
    queueLabel = " · čeká na uvolnění serveru";
  } else if (job.queue_position === 0) {
    queueLabel = " · právě běží";
  } else if (job.queue_position) {
    queueLabel = ` · fronta #${job.queue_position}`;
  }
  let timing = "";
  if (jobIsFinished(job.status)) {
    const dur = formatDuration(job.duration_s);
    if (dur) timing = ` · ${dur}`;
  }
  const privateLabel = jobIsPrivate(job) ? " · privátní session" : "";
  if (job._saved) {
    return `
    <strong>${escapeHtml(job.name)}</strong> <span class="status-tag status-tag-saved">${job._private ? "soukromá" : "uložená"}</span>
    <div class="status">${job._private ? "soukromá – odkaz přijde e-mailem" : "mapa už není ve veřejném seznamu"}</div>
    <div class="status">${escapeHtml(formatWhen(job.created_at))}</div>
  `;
  }
  const mineTag =
    jobsTab === "all" && isMyJob(job.id) ? ' <span class="mine-tag">moje</span>' : "";
  return `
    <strong>${escapeHtml(job.name)}</strong>${mineTag} <span class="status-tag status-tag-${escapeHtml(job.status || "")}">${escapeHtml(jobStatusTag(job.status))}</span>
    <div class="status status-${job.status}">${escapeHtml(statusLineText(job, queueLabel + timing + privateLabel))}</div>
    <div class="status">${escapeHtml(jobScaleLabel(job))} · ${escapeHtml(formatWhen(job.created_at))}</div>
    ${job.error ? `<div class="status error">${escapeHtml(friendlyJobError(job.error, { short: true }))}</div>` : ""}
  `;
}

/** Panel Joby má dva pohledy: seznam a detail jednoho jobu (bez vnořování). */
let jobsView = "list";

function setJobsView(view) {
  jobsView = view;
  document.getElementById("jobs-view-list")?.classList.toggle("hidden", view !== "list");
  document.getElementById("jobs-view-detail")?.classList.toggle("hidden", view !== "detail");
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
  let div = jobItemEl(job.id);
  if (!div) {
    div = document.createElement("div");
    div.className = "job-item";
    div.dataset.id = job.id;
    div.onclick = () => selectJob(job.id);
    div.tabIndex = 0;
    div.setAttribute("role", "button");
    div.addEventListener("keydown", (e) => {
      if (e.target !== div) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        selectJob(job.id);
      }
    });
  }
  div.classList.toggle("selected", job.id === selectedJobId);
  div.classList.toggle("private-session", jobIsPrivate(job));
  div.dataset.status = job.status || "";
  div.dataset.private = jobIsPrivate(job) ? "1" : "0";
  const html = jobItemHeadHtml(job);
  if (div._html !== html) {
    div.innerHTML = html;
    div._html = html;
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
  const visibleIds = new Set([...liveJobs, ...finishedJobs].map((j) => j.id));
  for (const job of liveJobs) upsertJobItem(liveList, job, liveJobs);
  for (const job of finishedJobs) upsertJobItem(doneList, job, finishedJobs);
  for (const list of [liveList, doneList]) {
    for (const child of [...list.children]) {
      if (child.id === "private-live-placeholder") continue;
      if (visibleIds.has(child.dataset.id)) continue;
      child.remove();
    }
  }
  const hasPrivatePlaceholder = Boolean(
    document.getElementById("private-live-placeholder")
  );
  const liveVisible = liveJobs.length > 0 || hasPrivatePlaceholder;
  liveList.classList.toggle("hidden", !liveVisible);
  if (liveEmpty) liveEmpty.classList.toggle("hidden", liveVisible);
  if (doneEmpty) doneEmpty.classList.toggle("hidden", finishedJobs.length > 0);
}

function updateFinishedMore(total) {
  const label = document.getElementById("jobs-finished-label");
  const more = document.getElementById("jobs-more");
  if (!label || !more) return;
  const shown = Math.min(total, finishedShown);
  label.textContent = total > shown ? `Hotové (${shown} z ${total})` : "Hotové";
  more.classList.toggle("hidden", total <= shown);
}

function initJobsPager() {
  document.getElementById("jobs-more")?.addEventListener("click", () => {
    finishedShown += FINISHED_PAGE_SIZE;
    loadJobs();
  });
  document.getElementById("jobs-back")?.addEventListener("click", () => {
    setJobsView("list");
    loadJobs();
  });
}

async function loadJobs() {
  const data = await api("/api/jobs");
  updateWorkerStatus(data);
  await verifySavedJobs(data.jobs);
  syncJobsTabs();

  let selected = data.jobs.find((j) => j.id === selectedJobId);
  // Privátní job není ve veřejném seznamu – drž detail přes /api/jobs/{id}.
  if (!selected && selectedJobId) {
    try {
      selected = await api(`/api/jobs/${selectedJobId}`);
    } catch (_) {
      selected = null;
    }
  }

  // „Moje mapy“: jen mé joby z veřejného seznamu + uložené záznamy, které v něm nejsou (soukromé).
  let listJobs = data.jobs;
  if (jobsTab === "mine") {
    const known = new Set(data.jobs.map((j) => j.id));
    const saved = myJobs.filter((r) => !known.has(r.id)).map(savedRecordAsJob);
    listJobs = [...data.jobs.filter((j) => isMyJob(j.id)), ...saved];
  }
  let { live, finished } = splitJobs(listJobs);
  if (finished.some((j) => j._saved)) {
    finished.sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  }
  const doneEmptyEl = document.getElementById("jobs-finished-empty");
  if (doneEmptyEl) {
    doneEmptyEl.textContent =
      jobsTab === "mine"
        ? "V tomto prohlížeči zatím nemáte žádné mapy – objeví se tu po spuštění generování."
        : "Zatím žádné hotové mapy.";
  }
  // Privátní joby do veřejného panelu neinjektovat (bez jména/logů).
  // Místo toho anonymní placeholder „Běží privátní job“ + stopky.
  let privateLive = data.private_live || null;
  if (
    !privateLive &&
    selected &&
    jobIsPrivate(selected) &&
    selected.status === "running"
  ) {
    privateLive = {
      status: selected.status,
      started_at: selected.started_at || selected.created_at,
    };
  }

  // Vybraný hotový job musí být v seznamu vidět.
  const selIdx = finished.findIndex((j) => j.id === selectedJobId);
  if (selIdx >= finishedShown) finishedShown = selIdx + 1;
  updateFinishedMore(finished.length);
  syncJobsList(live, finished.slice(0, finishedShown));
  syncPrivateLivePlaceholder(privateLive);
  await updateMobileJobBar(data, selected);

  if (generateStartedJobId) {
    const started =
      data.jobs.find((j) => j.id === generateStartedJobId) ||
      (selected && selected.id === generateStartedJobId ? selected : null);
    if (!started || !jobIsLive(started.status)) {
      clearGenerateStarted();
    }
  }

  if (!selected) {
    selectedJobId = null;
    selectedJobStatus = null;
    resetDetailProgress();
    syncFeedbackForJob(null);
    setJobsView("list");
    return;
  }
  selectedJobStatus = selected.status;
  if (jobsView !== "detail") return;
  syncDetailLogVisibility(selected);
  const liveJob = jobIsLive(selected.status);
  if (liveJob || logSettledForJob !== selected.id) {
    const privateNote = jobIsPrivate(selected)
      ? liveJob
        ? " · privátní (log v této session; ZIP e-mailem)"
        : " · privátní (ZIP přijde e-mailem)"
      : "";
    document.getElementById("detail-title").textContent = selected.name;
    setDetailStatus(
      `Stav: ${jobStatusLabel(selected.status)} · ${jobScaleLabel(selected)}` + privateNote
    );
    const timingEl = document.getElementById("detail-timing");
    if (timingEl) {
      timingEl.textContent = jobTimingText(selected);
      timingEl.classList.toggle("hidden", !timingEl.textContent);
    }
    renderJobSources(document.getElementById("detail-sources"), selected);
    setJobActionLinks(selected);
    renderJobDetailExtras(selected);
    const img = document.getElementById("detail-img");
    // Privátní: náhled PNG jen e-mailem, na webu nikdy.
    if (!jobIsPrivate(selected) && selected.has_preview) {
      if (img.classList.contains("hidden") && img.dataset.failedFor !== selected.id) {
        img.src = `/api/jobs/${selected.id}/preview.png?t=${Date.now()}`;
      }
      if (img.dataset.failedFor !== selected.id) img.classList.remove("hidden");
    } else {
      img.classList.add("hidden");
      if (jobIsPrivate(selected)) img.removeAttribute("src");
    }
    await refreshLog();
    renderJobProgress(selected);
  }
}

function updateWorkerStatus(data) {
  const el = document.getElementById("worker-status");
  if (!el) return;
  if (data.busy) {
    const q = data.queue_size || 0;
    const maxQ = data.max_queue_size || "?";
    el.textContent =
      `Generování běží` +
      (q ? ` · ${q} ${q === 1 ? "mapa" : q < 5 ? "mapy" : "map"} ve frontě (max ${maxQ})` : "");
    el.classList.add("busy");
  } else if (data.shared_busy) {
    // Těžký job právě počítá jiná instance serveru – detail se nezveřejňuje.
    el.textContent = "Server je právě vytížený – nová mapa chvíli počká na uvolnění";
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

function elapsedSinceIso(iso) {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.max(0, (Date.now() - t) / 1000);
}

let privateLiveStartedAt = null;
let privateLiveTickTimer = null;

function stopPrivateLiveTick() {
  if (privateLiveTickTimer) {
    clearInterval(privateLiveTickTimer);
    privateLiveTickTimer = null;
  }
}

function privateLivePlaceholderHtml(startedAt) {
  const elapsed = formatDuration(elapsedSinceIso(startedAt));
  const clock = elapsed || "…";
  return `
    <strong>Běží privátní job</strong>
    <div class="status status-running">${escapeHtml(clock)}</div>
  `;
}

function syncPrivateLivePlaceholder(privateLive) {
  const liveList = document.getElementById("jobs-live");
  const liveEmpty = document.getElementById("jobs-live-empty");
  if (!liveList) return;
  let el = document.getElementById("private-live-placeholder");
  if (!privateLive) {
    privateLiveStartedAt = null;
    stopPrivateLiveTick();
    if (el) el.remove();
    const hasJobs = liveList.querySelector(".job-item:not(#private-live-placeholder)");
    liveList.classList.toggle("hidden", !hasJobs);
    if (liveEmpty) liveEmpty.classList.toggle("hidden", Boolean(hasJobs));
    return;
  }
  privateLiveStartedAt = privateLive.started_at || null;
  if (!el) {
    el = document.createElement("div");
    el.id = "private-live-placeholder";
    el.className = "job-item private-live-placeholder";
    el.setAttribute("aria-live", "polite");
    el.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
    };
  }
  const html = privateLivePlaceholderHtml(privateLiveStartedAt);
  if (el._html !== html) {
    el.innerHTML = html;
    el._html = html;
  }
  if (el.parentElement !== liveList || liveList.firstChild !== el) {
    liveList.insertBefore(el, liveList.firstChild);
  }
  liveList.classList.remove("hidden");
  if (liveEmpty) liveEmpty.classList.add("hidden");
  if (!privateLiveTickTimer) {
    privateLiveTickTimer = setInterval(() => {
      const node = document.getElementById("private-live-placeholder");
      if (!node || !privateLiveStartedAt) {
        stopPrivateLiveTick();
        return;
      }
      const next = privateLivePlaceholderHtml(privateLiveStartedAt);
      if (node._html !== next) {
        node.innerHTML = next;
        node._html = next;
      }
    }, 1000);
  }
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
  if (jobIsFinished(job.status)) {
    return [start ? `Start ${start}` : "", dur ? `trvání ${dur}` : ""].filter(Boolean).join(" · ");
  }
  if (job.status === "running" && dur) {
    return `${start ? `Od ${start} · ` : ""}běží ${dur}`;
  }
  return start ? `Zařazeno ${start}` : "";
}

/** „před 61 dny“ – stáří stažení podkladů v době zpracování jobu. */
function daysAgoText(days) {
  const d = Math.round(days);
  if (d <= 0) return "v den zpracování";
  if (d === 1) return "den před zpracováním";
  return `${d} dní před zpracováním`;
}

/**
 * Výšková data (LiDAR ČÚZK) u jobu srozumitelně pro uživatele.
 * Vrací { level: "ok" | "warn", lines: [...], detail } nebo null;
 * detail je původní technický popis (rozbalovací blok pro diagnostiku).
 */
function jobSourcesInfo(job) {
  const meta = job.source_meta;
  if (!meta && !job.dmp_mode) return null;
  const mode = job.dmp_mode || (meta && meta.dmp_mode);
  const degraded = !!(job.dmp_degraded || (meta && meta.dmp_degraded));
  let dmpLabel = "DMP ?";
  if (mode === "ok") dmpLabel = "DMP OK";
  else if (mode === "1g") dmpLabel = "DMP 1G";
  else if (mode === "mixed") dmpLabel = "DMP OK+1G";
  const sheets = (meta && meta.sheets) || [];
  const ages = [];
  for (const sheet of sheets) {
    const dmr = (sheet.dmr && sheet.dmr.cache_age_days) ?? null;
    const dmp = (sheet.dmp && sheet.dmp.cache_age_days) ?? null;
    if (dmr != null) ages.push(dmr);
    if (dmp != null) ages.push(dmp);
  }
  const maxAge = ages.length ? Math.max(...ages) : null;
  const limit = Number(job.lidar_cache_max_age_days) || 180;

  let level = "ok";
  const lines = [
    "Výška terénu, stromů a budov je z leteckého laserového skenování ČÚZK.",
  ];
  if (degraded || mode === "1g" || mode === "mixed") {
    level = "warn";
    lines.unshift(
      (mode === "mixed" ? "Pro část území" : "Pro toto území") +
        " nebyla při zpracování dostupná přesná data o výšce stromů a budov," +
        " použila se starší hrubší náhrada. Porosty a průchodnost mohou být méně přesné." +
        " Doporučení: vygenerujte mapu znovu za pár dní."
    );
  }
  if (maxAge != null && (level === "warn" || maxAge > limit)) {
    lines.push(
      `Data byla stažena ${daysAgoText(maxAge)}.` +
        (maxAge > limit
          ? " Příští mapa tohoto území si je stáhne znovu automaticky."
          : "")
    );
  } else if (maxAge != null) {
    lines.push(
      `Data jsou aktuální (stažena ${daysAgoText(maxAge)}; ČÚZK území` +
        " přeskenovává jen jednou za několik let). Nic není potřeba dělat."
    );
  } else if (level === "ok") {
    lines.push("Data jsou v pořádku, nic není potřeba dělat.");
  }

  const detail = [dmpLabel];
  if (degraded) detail.push("degradace 1G viditelná");
  if (maxAge != null) detail.push(`cache ~${Math.round(maxAge)} d (obnova po ${limit} d)`);
  if (sheets.length) {
    detail.push(`listy SM5: ${sheets.map((s) => s.mapnom).filter(Boolean).join(", ")}`);
  }
  detail.push("stáří = stažení do cache, ne datum pořízení ČÚZK");
  return { level, lines, detail: detail.join(" · ") };
}

function renderJobSources(el, job) {
  if (!el) return;
  const info = jobSourcesInfo(job);
  el.classList.toggle("hidden", !info);
  el.classList.toggle("warn", !!info && info.level === "warn");
  if (!info) {
    el.innerHTML = "";
    return;
  }
  const icon = info.level === "warn" ? "⚠ " : "";
  el.innerHTML =
    `<div>${icon}${info.lines.map(escapeHtml).join(" ")}</div>` +
    `<details class="sources-detail"><summary>Technické podrobnosti</summary>` +
    `<code>${escapeHtml(info.detail)}</code></details>`;
}

function setJobActionLinks(job) {
  const dl = document.getElementById("detail-download");
  const prev = document.getElementById("detail-preview");
  const georefBtn = document.getElementById("detail-download-georef");
  syncFeedbackForJob(job);
  // Privátní: ZIP/PNG jen e-mailem — na stránce jobu žádné download odkazy.
  if (jobIsPrivate(job)) {
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

/** Feedback u jobu — lze odeslat i během běhu; vždy s job_id. */
function syncFeedbackForJob(job) {
  const box = document.getElementById("job-feedback");
  const feedbackJob = document.getElementById("feedback-job-id");
  const submitBtn = document.getElementById("feedback-submit");
  const contactEl = document.getElementById("feedback-contact");
  const commentEl = document.getElementById("feedback-comment");
  const hasJob = !!(job && job.id);
  if (box) box.classList.toggle("hidden", !hasJob);
  if (feedbackJob) feedbackJob.value = hasJob ? job.id : "";
  // Nikdy disabled kvůli statusu — odeslat jde i u running/queued.
  if (submitBtn) submitBtn.disabled = false;
  if (contactEl) contactEl.disabled = false;
  if (commentEl) commentEl.disabled = false;
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

let detailJob = null;

async function fillJobDetail(id, { applyForm = false } = {}) {
  const job = await api(`/api/jobs/${id}`);
  detailJob = job;
  selectedJobStatus = job.status;
  syncDetailLogVisibility(job);
  const privateNote = jobIsPrivate(job)
    ? jobIsLive(job.status)
      ? " · privátní (log v této session; ZIP e-mailem)"
      : " · privátní (ZIP přijde e-mailem)"
    : "";
  document.getElementById("detail-title").textContent = job.name;
  setDetailStatus(`Stav: ${jobStatusLabel(job.status)} · ${jobScaleLabel(job)}` + privateNote);
  const timingEl = document.getElementById("detail-timing");
  if (timingEl) {
    timingEl.textContent = jobTimingText(job);
    timingEl.classList.toggle("hidden", !timingEl.textContent);
  }
  renderJobSources(document.getElementById("detail-sources"), job);
  if (applyForm) applyJobToForm(job);
  setJobActionLinks(job);
  renderJobDetailExtras(job);
  const img = document.getElementById("detail-img");
  // Privátní: náhled PNG jen e-mailem.
  if (!jobIsPrivate(job) && job.has_preview) {
    img.dataset.failedFor = "";
    img.src = `/api/jobs/${id}/preview.png?t=${Date.now()}`;
    img.classList.remove("hidden");
  } else {
    img.classList.add("hidden");
    if (jobIsPrivate(job)) img.removeAttribute("src");
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
    document.getElementById("detail-log").textContent = "";
    resetDetailProgress();
  }
  setJobsView("detail");
  await fillJobDetail(id, { applyForm: false });
  await loadJobs();
  const card = document.querySelector(".jobs-card");
  if (card) card.scrollTop = 0;
}

/** Průběh vybraného jobu: jen řádky s hlavičkami kroků (plán, [n/N], Hotovo). */
let detailProgressLines = [];
let detailProgress = null;

function resetDetailProgress() {
  detailProgressLines = [];
  detailProgress = null;
}

/** Stavy hotového / neúspěšného jobu: lidská chyba, Zkusit znovu, Co dál, věta o zavření stránky. */
function renderJobDetailExtras(job) {
  const priv = jobIsPrivate(job);
  const live = jobIsLive(job.status);
  const failed = job.status === "failed";
  const errBox = document.getElementById("detail-error");
  const hasErr = Boolean(job.error) && (failed || job.status === "done_with_errors");
  if (errBox) {
    errBox.classList.toggle("hidden", !hasErr);
    if (hasErr) {
      document.getElementById("detail-error-human").textContent = friendlyJobError(job.error);
      document.getElementById("detail-error-text").textContent = job.error;
    }
  }
  const retry = document.getElementById("detail-retry");
  if (retry) retry.classList.toggle("hidden", !(failed && !priv));
  const tplBtn = document.getElementById("detail-use-template");
  if (tplBtn) tplBtn.classList.toggle("hidden", priv || failed);
  const dl = document.getElementById("detail-download");
  const done = job.status === "done" || job.status === "done_with_errors";
  if (dl) dl.classList.toggle("download-main", done && !priv && Boolean(job.has_output));
  const next = document.getElementById("detail-next");
  if (next) next.classList.toggle("hidden", !(done && !priv && job.has_output));
  const closeHint = document.getElementById("detail-close-hint");
  if (closeHint) closeHint.classList.toggle("hidden", !live || priv);
  const prog = document.getElementById("detail-progress");
  if (prog) prog.classList.toggle("hidden", !live);
}

/** Překreslí průběh po krocích (jen live joby). Volá se při každém pollu, čas tedy běží. */
function renderJobProgress(job) {
  const box = document.getElementById("detail-progress");
  if (!box || !job || !jobIsLive(job.status)) return;
  const text = document.getElementById("detail-progress-text");
  const meter = document.getElementById("detail-meter");
  const list = document.getElementById("detail-steps");
  if (job.status !== "running") {
    const pos = job.queue_position;
    text.textContent = `Ve frontě${pos ? `, pořadí #${pos}` : ""} – začne, až doběhnou předchozí.`;
    list.innerHTML = "";
    meter.hidden = true;
    return;
  }
  meter.hidden = false;
  const prog = detailProgress;
  const steps = prog ? prog.steps : [];
  const startedElapsed = elapsedSinceIso(job.started_at);
  const elapsed = startedElapsed != null ? startedElapsed : prog ? prog.elapsed : null;
  const est = (myJobRecord(job.id) || {}).estimate_minutes;
  const parts = [];
  if (prog && prog.total) parts.push(`Krok ${Math.max(1, prog.current)} z ${prog.total}`);
  else parts.push("Připravuji kroky…");
  if (elapsed != null) parts.push(`běží ${formatDuration(elapsed)}`);
  const left = remainingText(Number(est), elapsed);
  if (left) parts.push(left);
  text.textContent = parts.join(" · ");
  const doneCount = steps.filter((st) => st.state === "done" || st.state === "skipped").length;
  const hasNow = steps.some((st) => st.state === "now");
  const pct = steps.length
    ? Math.min(100, Math.round(((doneCount + (hasNow ? 0.5 : 0)) / steps.length) * 100))
    : 0;
  meter.setAttribute("aria-valuenow", String(pct));
  meter.querySelector("i").style.width = `${pct}%`;
  const html = steps
    .map((st) => {
      const mark = st.state === "done" ? "✓" : st.state === "now" ? "●" : st.state === "skipped" ? "–" : "○";
      const note = st.state === "skipped" ? ` <span class="step-note">přeskočeno${st.note ? ` (${escapeHtml(st.note)})` : ""}</span>` : "";
      const cur = st.state === "now" ? ' aria-current="step"' : "";
      return `<li class="step-${st.state}"${cur}><span class="step-mark" aria-hidden="true">${mark}</span> ${escapeHtml(st.label || "…")}${note}</li>`;
    })
    .join("");
  if (list._html !== html) {
    list.innerHTML = html;
    list._html = html;
  }
}

async function refreshLog() {
  if (!selectedJobId) return;
  if (logSettledForJob === selectedJobId) return;
  const jobId = selectedJobId;
  const after = logAfter;
  const data = await api(`/api/jobs/${jobId}/log?after=${after}`);
  // Souběžné volání (výběr jobu + polling) mezitím log už připsalo nebo se změnil job.
  if (selectedJobId !== jobId || logAfter !== after) return;
  const pre = document.getElementById("detail-log");
  const hadNew = data.lines.length > 0;
  for (const line of data.lines) {
    pre.textContent += line.line + "\n";
    logAfter = line.id;
    if (PROGRESS_LINE_RE.test(line.line)) detailProgressLines.push(line.line);
  }
  if (hadNew) detailProgress = parseJobProgress(detailProgressLines);
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
  if (form.name && !form.name.value.trim()) {
    showFormError("Zadejte název mapy.");
    form.name.focus();
    return;
  }
  if (!form.map_scale || !form.map_scale.value) {
    showFormError("Vyberte měřítko.");
    if (form.map_scale) form.map_scale.focus();
    return;
  }
  if (Number(form.map_scale.value) > 5000 && !selectedMapTypes().length) {
    showFormError("Vyberte alespoň jeden typ mapy (les nebo MTBO).");
    document.getElementById("map_type_les")?.focus();
    return;
  }
  if (!form.contour_interval || !form.contour_interval.value) {
    showFormError("Vyberte ekvidistanci.");
    if (form.contour_interval) form.contour_interval.focus();
    return;
  }
  if (!document.getElementById("bbox-input").value) {
    showFormError("Nakreslete výřez na mapě (dva protilehlé rohy).");
    document.getElementById("bbox-map")?.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  if (!bboxAllowed) {
    showFormError(
      (lastSheets && lastSheets.hint) ||
        (lastSheets && isSprintSelected() && lastSheets.sprint_hint) ||
        `Výřez je moc velký nebo ještě není ověřený. Les a MTBO max ${bboxMaxAreaKm2} km² (např. 5 × 5 km), sprint 1:4000 max ${sprintMaxAreaKm2} km² (např. 3 × 3 km).`
    );
    document.getElementById("bbox-map")?.scrollIntoView({ behavior: "smooth", block: "center" });
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
  const estimateAtSubmit = selectedEstimateMinutes();
  jobSubmitInFlight = true;
  btn.disabled = true;
  btn.textContent = "Zakládám…";
  let startedId = null;
  try {
    const fd = new FormData(form);
    fd.set("private", privateEl && privateEl.checked ? "1" : "0");
    const job = await api("/api/jobs", { method: "POST", body: fd });
    if (job && job.duplicate_skipped) {
      const msg =
        job.duplicate_message ||
        "Nová mapa se nezaložila – stejný výřez už běží nebo čeká.";
      showFormError(msg);
    } else if (job && jobIsPrivate(job)) {
      const mail =
        (job.options && job.options.notify_email) ||
        (document.getElementById("notify_email") || {}).value ||
        "";
      showFormNotice(
        `Privátní mapa se generuje – průběh uvidíte níže v detailu. ` +
          `ZIP/náhled nepřijdou do veřejného seznamu; odkazy přijdou na ${mail || "váš e-mail"} (platí 48 hodin).`
      );
      if (job.id) startedId = job.id;
    } else if (job && job.id) {
      clearFormNotice();
      startedId = job.id;
    }
    if (startedId) {
      addMyJob(job, estimateAtSubmit);
      saveLastOptions();
      // První mapa v tomto prohlížeči: rovnou ukaž „Moje mapy“ (uložená volba záložky má přednost).
      if (!storageGet(JOBS_TAB_KEY)) jobsTab = "mine";
      syncJobsTabs();
    }
    await selectJob(job.id);
    if (startedId && MOBILE_MQ.matches) {
      document.querySelector(".jobs-card")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  } catch (err) {
    showFormError(err.message);
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
  initBboxBadge();
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
    setSheetInfo("", "");
    refreshBboxBadge();
    bboxFirstCorner = L.circleMarker(e.latlng, { radius: 5, color: "#cc00cc" }).addTo(bboxMap);
  }
  if (bboxCorners.length === 2) {
    const b = L.latLngBounds(bboxCorners[0], bboxCorners[1]);
    // Bez fitBounds – uživatel už vidí výřez; dřívější maxZoom:14 zbytečně odzoomovalo.
    drawBboxRect(b);
    document.getElementById("bbox-input").value = bboxToInputValue(b);
    setReuseJob("");
    lookupSheets();
  }
}

function bboxToInputValue(b) {
  return [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].join(",");
}

function drawBboxRect(b) {
  if (bboxFirstCorner) {
    bboxMap.removeLayer(bboxFirstCorner);
    bboxFirstCorner = null;
  }
  bboxRect = L.rectangle(b, { color: "#cc00cc", weight: 2, fillOpacity: 0.15 }).addTo(bboxMap);
  addBboxHandles();
}

// Úchyty hotového výřezu: roh mění velikost (protější roh drží),
// křížek uprostřed posouvá celý obdélník. Listy SM5 se ověří po puštění.
const BBOX_CORNER_NAMES = ["sw", "nw", "ne", "se"];
const BBOX_MOVE_ICON =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ' +
  'd="M12 3v18M3 12h18M12 3l-3 3M12 3l3 3M12 21l-3-3M12 21l3-3M3 12l3-3M3 12l3 3M21 12l-3-3M21 12l-3 3"/></svg>';

function bboxCornerLatLngs(b) {
  return [b.getSouthWest(), b.getNorthWest(), b.getNorthEast(), b.getSouthEast()];
}

function clampToCz(ll) {
  return L.latLng(
    Math.min(CZ.north, Math.max(CZ.south, ll.lat)),
    Math.min(CZ.east, Math.max(CZ.west, ll.lng))
  );
}

function removeBboxHandles() {
  if (bboxMap) bboxHandles.forEach((m) => bboxMap.removeLayer(m));
  bboxHandles = [];
}

function addBboxHandles() {
  removeBboxHandles();
  if (!bboxRect) return;
  const corners = bboxCornerLatLngs(bboxRect.getBounds());
  corners.forEach((ll, i) => {
    const handle = L.marker(ll, {
      draggable: true,
      autoPan: true,
      keyboard: false,
      title: "Táhnutím změníte velikost výřezu",
      icon: L.divIcon({
        className: `bbox-handle bbox-handle-corner bbox-handle-${BBOX_CORNER_NAMES[i]}`,
        iconSize: null,
      }),
    });
    let fixed = null;
    handle.on("dragstart", () => {
      fixed = bboxCornerLatLngs(bboxRect.getBounds())[(i + 2) % 4];
      onBboxEditStart();
    });
    handle.on("drag", () => {
      const ll2 = clampToCz(handle.getLatLng());
      updateBboxDraft(L.latLngBounds(fixed, ll2), handle);
    });
    handle.on("dragend", commitBboxEdit);
    bboxHandles.push(handle.addTo(bboxMap));
  });
  const move = L.marker(bboxRect.getBounds().getCenter(), {
    draggable: true,
    autoPan: true,
    keyboard: false,
    title: "Táhnutím posunete výřez",
    icon: L.divIcon({
      className: "bbox-handle bbox-handle-move",
      html: BBOX_MOVE_ICON,
      iconSize: null,
    }),
  });
  let start = null;
  let startBounds = null;
  move.on("dragstart", () => {
    start = move.getLatLng();
    startBounds = bboxRect.getBounds();
    onBboxEditStart();
  });
  move.on("drag", () => {
    const cur = move.getLatLng();
    // Posun omezit tak, aby obdélník zůstal celý v obálce Česka.
    const dLat = Math.min(
      CZ.north - startBounds.getNorth(),
      Math.max(CZ.south - startBounds.getSouth(), cur.lat - start.lat)
    );
    const dLng = Math.min(
      CZ.east - startBounds.getEast(),
      Math.max(CZ.west - startBounds.getWest(), cur.lng - start.lng)
    );
    const sw = startBounds.getSouthWest();
    const ne = startBounds.getNorthEast();
    updateBboxDraft(
      L.latLngBounds([sw.lat + dLat, sw.lng + dLng], [ne.lat + dLat, ne.lng + dLng]),
      move
    );
  });
  move.on("dragend", commitBboxEdit);
  bboxHandles.push(move.addTo(bboxMap));
}

function positionBboxHandles(skip) {
  if (!bboxRect || bboxHandles.length !== 5) return;
  const b = bboxRect.getBounds();
  const pts = [...bboxCornerLatLngs(b), b.getCenter()];
  bboxHandles.forEach((m, i) => {
    if (m !== skip) m.setLatLng(pts[i]);
  });
}

// Hrubý rozměr pro živou odezvu při tažení; přesnou plochu (S-JTSK) vrátí /api/sheets.
function approxBboxSizeKm(b) {
  const rad = (((b.getSouth() + b.getNorth()) / 2) * Math.PI) / 180;
  const kmPerDegLat = 111.13295 - 0.55982 * Math.cos(2 * rad);
  const kmPerDegLng = 111.41284 * Math.cos(rad) - 0.0935 * Math.cos(3 * rad);
  const w = (b.getEast() - b.getWest()) * kmPerDegLng;
  const h = (b.getNorth() - b.getSouth()) * kmPerDegLat;
  return { w, h, area: w * h };
}

function onBboxEditStart() {
  // Změna výřezu = nová mapa: zelené tlačítko zpět, LiDAR ze starého jobu nesedí.
  if (generateStartedJobId) clearGenerateStarted();
  setReuseJob("");
  sheetsLookupSeq++;
  bboxAllowed = false;
  lastSheets = null;
  sheetsError = null;
  const sheetsInput = document.getElementById("sm5-sheets-input");
  if (sheetsInput) sheetsInput.value = "";
  updateSubmitButtonLabel();
}

function isSprintSelected() {
  const scale = document.getElementById("map_scale");
  return Boolean(scale && scale.value && Number(scale.value) <= 5000);
}

function currentMaxAreaKm2() {
  return isSprintSelected() ? Math.min(sprintMaxAreaKm2, bboxMaxAreaKm2) : bboxMaxAreaKm2;
}

function updateBboxDraft(b, skip) {
  bboxRect.setBounds(b);
  positionBboxHandles(skip);
  const { w, h, area } = approxBboxSizeKm(b);
  const limit = currentMaxAreaKm2();
  const tooLarge = area > limit;
  styleBboxRect(tooLarge);
  setSheetInfo("", "");
  bboxDraft = tooLarge
    ? { cls: "err", text: `✕ Příliš velký výřez: cca ${formatAreaKm2(area)} (limit ${formatNumCz(limit)} km²)` }
    : { cls: "", text: `cca ${formatSizeKm(w, h)} (${formatAreaKm2(area)}) – po puštění ověřím` };
  refreshBboxBadge();
}

function commitBboxEdit() {
  if (!bboxRect) return;
  const b = bboxRect.getBounds();
  bboxCorners = [b.getSouthWest(), b.getNorthEast()];
  positionBboxHandles(null);
  document.getElementById("bbox-input").value = bboxToInputValue(b);
  lookupSheets();
}

function styleBboxRect(tooLarge) {
  const el = document.getElementById("bbox-map");
  if (el) el.classList.toggle("bbox-too-large", Boolean(tooLarge && bboxRect));
  if (!bboxRect) return;
  bboxRect.setStyle({
    color: tooLarge ? "#c00" : "#cc00cc",
    weight: 2,
    fillOpacity: 0.15,
  });
}

function setReuseJob(id) {
  const el = document.getElementById("reuse-job-id");
  if (el) el.value = id || "";
}

function updateResidualPavedControls() {
  const cb = document.getElementById("sprint_residual_paved");
  const opts = document.getElementById("residual-paved-options");
  if (!cb || !opts) return;
  opts.classList.toggle("hidden", !cb.checked);
}

function updateCliffControls() {
  const cliff = document.getElementById("kp_cliff_symbol");
  const sens = document.getElementById("kp_cliff_sensitivity");
  const sensLabel = document.getElementById("cliff-sensitivity-label");
  const sensHint = document.getElementById("cliff-sensitivity-hint");
  if (!cliff || !sens) return;
  const off = cliff.value === "off";
  sens.disabled = off;
  if (sensLabel) sensLabel.style.opacity = off ? "0.45" : "";
  if (sensHint) {
    const txt = document.getElementById("cliff-sensitivity-text");
    if (txt) {
      txt.textContent = off
        ? "Citlivost se při „Nevykreslovat“ nepoužije – srázy se nepočítají."
        : "Méně citlivé = jen výrazné srázy, méně falešných.";
    }
  }
  const symbolTxt = document.getElementById("cliff-symbol-text");
  if (symbolTxt) {
    symbolTxt.textContent =
      "Strmý schod = skalní plocha (206), mírnější = zem (104).";
  }
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
  const jobTypes = Array.isArray(opts.map_types)
    ? opts.map_types.filter((t) => t === "les" || t === "mtbo")
    : [];
  if (scaleSel && Number.isFinite(scale)) {
    filterScaleOptions(mapKindFor(scale, jobTypes), scale);
    const kindRadio = document.querySelector(
      `input[name="map_kind_ui"][value="${mapKindFor(scale, jobTypes)}"]`
    );
    if (kindRadio) kindRadio.checked = true;
  }
  rebuildContourOptions(opts.contour_interval);
  // Typy map: starší joby bez klíče stavěly obě; programové nastavení `change` nevyvolá.
  applyMapTypeDefaults(scale);
  if (scale > 5000) {
    const saved = Array.isArray(opts.map_types)
      ? opts.map_types.filter((t) => t === "les" || t === "mtbo")
      : [];
    setMapTypes(saved.length ? saved : ["les", "mtbo"]);
    updateOutputHints();
    updateSubmitButtonLabel();
  }
  const formlines = form.contour_formlines;
  if (formlines) {
    const flVal = opts.contour_formlines || "off";
    formlines.value = [...formlines.options].some((o) => o.value === flVal) ? flVal : "off";
  }
  const contourSmooth = form.contour_smooth;
  if (contourSmooth) {
    const csVal = String(opts.contour_smooth ?? "1");
    contourSmooth.value = [...contourSmooth.options].some((o) => o.value === csVal) ? csVal : "1";
  }
  const vegSmooth = form.veg_smooth;
  if (vegSmooth) {
    const vsVal = String(opts.veg_smooth ?? "1");
    vegSmooth.value = [...vegSmooth.options].some((o) => o.value === vsVal) ? vsVal : "1";
  }
  const dtmSel = form.dtm;
  if (dtmSel) {
    // Jedna volba pro sprint i les + MTBO; starší joby měly dtm_sprint / dtm_forest zvlášť.
    const hasS = opts.dtm_sprint !== undefined;
    const hasF = opts.dtm_forest !== undefined;
    const s = hasS ? !!opts.dtm_sprint : true;
    const f = hasF ? !!opts.dtm_forest : false;
    if (s && f) dtmSel.value = "1";
    else if (!s && !f) dtmSel.value = "0";
    else dtmSel.value = "auto";
  }
  const cliff = form.kp_cliff_symbol;
  if (cliff) {
    let cliffVal = (job.options || {}).kp_cliff_symbol || "auto";
    // Legacy „rock_face“ (force linear 201) → auto; skály jsou plochy 206.
    if (cliffVal === "rock_face") cliffVal = "auto";
    if ([...cliff.options].some((o) => o.value === cliffVal)) {
      cliff.value = cliffVal;
    }
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
  const osmObjects = form.kp_osm_objects;
  if (osmObjects) {
    // Starší joby: kterákoli dřívější dílčí volba (lavičky, lampy, herní prvky).
    osmObjects.checked = Boolean(
      opts.kp_osm_objects != null
        ? opts.kp_osm_objects
        : opts.kp_osm_benches ||
            opts.kp_osm_lamps ||
            opts.kp_osm_playground_equipment ||
            opts.kp_osm_furniture
    );
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
  updateResidualPavedControls();
  const outGeoref = form.output_georef;
  if (outGeoref) {
    outGeoref.checked = Boolean(opts.output_georef);
  }
  const outRefs = form.output_references;
  if (outRefs) {
    outRefs.checked =
      opts.output_references == null ? true : Boolean(opts.output_references);
  }
  refreshSettingsSummary();
  refreshSubmitChecklist();
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
  drawBboxRect(b);
  bboxMap.fitBounds(b, { padding: [24, 24], maxZoom: 16 });
  document.getElementById("bbox-input").value = [west, south, east, north].join(",");
  lookupSheets();
}

function clearBbox(opts = {}) {
  // Programatický applyBbox (keepReuse) nesmí shodit zámek po odeslání;
  // uživatelské vymazání / překreslení výřezu ano.
  if (!opts.keepReuse) clearGenerateStarted();
  removeBboxHandles();
  styleBboxRect(false);
  bboxCorners = [];
  bboxRect = null;
  bboxFirstCorner = null;
  sheetsLookupSeq++;
  bboxAllowed = false;
  lastSheets = null;
  bboxDraft = null;
  sheetsError = null;
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
  setSheetInfo("", "");
  updateSubmitButtonLabel();
}

// --- Česky formátovaná čísla a odznak stavu výřezu přímo na mapě ---
function formatNumCz(x, digits = 1) {
  return Number(x).toLocaleString("cs-CZ", { minimumFractionDigits: 0, maximumFractionDigits: digits });
}

/** Rozměr: pod 1 km v metrech (na 10 m), jinak km na 1 desetinné místo. */
function formatLengthKm(km) {
  if (km < 1) return `${formatNumCz(Math.max(10, Math.round(km * 100) * 10), 0)} m`;
  return `${formatNumCz(km, 1)} km`;
}

function formatSizeKm(w, h) {
  const a = formatLengthKm(w);
  const b = formatLengthKm(h);
  // „800 × 600 m“ – stejnou jednotku řekni jen jednou.
  const unitA = a.split(" ").pop();
  return unitA === b.split(" ").pop() ? `${a.replace(` ${unitA}`, "")} × ${b}` : `${a} × ${b}`;
}

/** Plocha: pod 1 km² v ha, jinak km² na 1 desetinné místo. */
function formatAreaKm2(km2) {
  if (km2 < 1) return `${formatNumCz(Math.round(km2 * 100), 0)} ha`;
  return `${formatNumCz(km2, 1)} km²`;
}

function sheetNamesText(data) {
  const names = (data.sheets || []).map((x) => x.mapnom).filter(Boolean);
  return names.length ? `Listy SM5: ${names.join(", ")}` : "";
}

function setBboxBadge(cls, text) {
  if (!bboxBadgeEl) return;
  bboxBadgeEl.className = "bbox-badge" + (cls ? " " + cls : "");
  bboxBadgeEl.textContent = text;
}

function refreshBboxBadge() {
  if (!bboxBadgeEl) return;
  const hasBbox = Boolean((document.getElementById("bbox-input") || {}).value);
  if (!hasBbox) {
    setBboxBadge("", bboxCorners.length === 1 ? "Klikněte na druhý roh výřezu" : "Klikněte na dva protilehlé rohy výřezu");
    return;
  }
  if (bboxDraft && !lastSheets) {
    setBboxBadge(bboxDraft.cls, bboxDraft.text);
    return;
  }
  if (sheetsError) {
    setBboxBadge("err", `✕ ${sheetsError}`);
    return;
  }
  const data = lastSheets;
  if (!data) {
    setBboxBadge("", "Ověřuji výřez…");
    return;
  }
  const area = data.area_km2 != null ? formatAreaKm2(data.area_km2) : "";
  if (data.too_large) {
    if (data.too_large_reason === "sheets") {
      setBboxBadge("err", `✕ Příliš velký výřez: ${data.count} listů SM5 (max ${data.max_sheets})`);
    } else {
      setBboxBadge("err", `✕ Příliš velký výřez: ${area} (limit ${formatNumCz(data.max_area_km2 || bboxMaxAreaKm2)} km²)`);
    }
    return;
  }
  if (!data.count) {
    setBboxBadge("err", "✕ Výřez je mimo data (jen Česko)");
    return;
  }
  if (isSprintSelected() && data.sprint_hint) {
    setBboxBadge("err", `✕ Příliš velký výřez pro sprint: ${area} (limit ${formatNumCz(sprintMaxAreaKm2)} km²)`);
    return;
  }
  const limit = isSprintSelected() ? Math.min(sprintMaxAreaKm2, bboxMaxAreaKm2) : bboxMaxAreaKm2;
  const mins = selectedEstimateMinutes();
  setBboxBadge(
    "ok",
    `✓ ${formatSizeKm(data.width_km, data.height_km)} (${area}) · limit ${formatNumCz(limit)} km²` +
      (mins != null ? ` · odhad ~${mins} min` : "")
  );
}

function initBboxBadge() {
  if (!bboxMap || typeof L === "undefined") return;
  const ctl = L.control({ position: "bottomleft" });
  ctl.onAdd = () => {
    const div = L.DomUtil.create("div", "bbox-badge");
    L.DomEvent.disableClickPropagation(div);
    bboxBadgeEl = div;
    return div;
  };
  ctl.addTo(bboxMap);
  refreshBboxBadge();
}

// --- Kontrolní seznam u tlačítka Spustit ---
function refreshSubmitChecklist() {
  const el = document.getElementById("submit-checklist");
  const form = document.getElementById("job-form");
  if (!el || !form) return;
  const scale = form.map_scale ? Number(form.map_scale.value) : NaN;
  const items = [
    ["Název", Boolean(form.name && form.name.value.trim())],
    ["Typ mapy a měřítko", Number.isFinite(scale) && scale > 0],
  ];
  items.push(["Výřez", Boolean(document.getElementById("bbox-input").value) && bboxAllowed]);
  const privateEl = document.getElementById("private");
  if (privateEl && privateEl.checked) {
    const email = ((document.getElementById("notify_email") || {}).value || "").trim();
    items.push(["E-mail", email.includes("@") && email.split("@").pop().includes(".")]);
  }
  el.innerHTML = items
    .map(([label, ok]) => `<span class="${ok ? "done" : "todo"}">${ok ? "✓" : "○"} ${label}</span>`)
    .join(" · ");
}

// --- Souhrn pokročilého nastavení (chips) ---
const SETTINGS_VALUE_NAMES = {
  veg_smooth: { 0: "kostičky", 1: "hladké", 2: "zjednodušené do 6 m", 3: "zjednodušené do 12 m" },
  kp_cliff_symbol: { auto: "automaticky", earth_bank: "zemní sráz", symbol_206: "skála", off: "vypnuto" },
  dtm: { auto: "podle presetu", 1: "všude", 0: "vypnuto" },
};

const SETTINGS_CHIPS = [
  { id: "contour_formlines", label: "Pomocné vrstevnice" },
  { id: "contour_smooth", label: "Vyhlazení vrstevnic" },
  { id: "kp_vege_height", label: "Výška vegetace" },
  { id: "veg_smooth", label: "Tvar porostů" },
  { id: "kp_cliff_symbol", label: "Srázy (symbol)" },
  { id: "kp_cliff_sensitivity", label: "Citlivost srázů", skipWhen: () => (document.getElementById("kp_cliff_symbol") || {}).value === "off" },
  { id: "dtm", label: "DTM" },
  { id: "kp_osm_objects", label: "Objekty OSM", bool: true },
  { id: "output_georef", label: "GeoTIFF", bool: true },
  { id: "output_references", label: "PNG šablony", bool: true },
  {
    id: "sprint_residual_paved",
    label: "Zpevněné mezery",
    bool: true,
    skipWhen: () => {
      const wrap = document.getElementById("residual-paved-wrap");
      return !wrap || wrap.classList.contains("hidden");
    },
  },
];

/** Krátká hodnota pole: vlastní název, nebo text option před „–“ / „(“. */
function settingsChipValue(chip, el) {
  if (chip.bool) return el.checked ? "ano" : "ne";
  const names = SETTINGS_VALUE_NAMES[chip.id];
  if (names && names[el.value] != null) return names[el.value];
  const opt = el.options[el.selectedIndex];
  const text = (opt ? opt.textContent : el.value).split(/\s+–\s+|\s*\(/)[0].trim();
  return text.charAt(0).toLowerCase() + text.slice(1);
}

/** Výchozí hodnota = jak je pole v HTML (selected / checked), nezávisle na obnově formuláře prohlížečem. */
function settingsDefaultValue(chip, el) {
  if (chip.bool) return el.defaultChecked;
  const opt = [...el.options].find((o) => o.defaultSelected) || el.options[0];
  return opt ? opt.value : "";
}

function refreshSettingsSummary() {
  const box = document.getElementById("settings-summary");
  if (!box) return;
  let changed = 0;
  const parts = [];
  for (const chip of SETTINGS_CHIPS) {
    const el = document.getElementById(chip.id);
    if (!el || (chip.skipWhen && chip.skipWhen())) continue;
    const isChanged = chip.bool
      ? el.checked !== settingsDefaultValue(chip, el)
      : el.value !== settingsDefaultValue(chip, el);
    if (isChanged) changed++;
    parts.push(
      `<span class="chip${isChanged ? " changed" : ""}">${chip.label}: ${escapeHtml(settingsChipValue(chip, el))}</span>`
    );
  }
  box.innerHTML = parts.join("");
  const mark = document.getElementById("advanced-changed");
  if (mark) mark.textContent = changed ? ` (změněno: ${changed})` : "";
}

function setSheetInfo(text, cls) {
  const el = document.getElementById("sheet-info");
  if (!el) return;
  el.textContent = text;
  el.className = "sheet-info" + (cls ? " " + cls : "");
}

function selectedEstimateMinutes() {
  if (!lastSheets || lastSheets.estimate_minutes == null) return null;
  const refs = document.getElementById("output_references");
  const wantRefs = !refs || refs.checked;
  let mins =
    wantRefs && lastSheets.estimate_minutes_with_refs != null
      ? lastSheets.estimate_minutes_with_refs
      : lastSheets.estimate_minutes;
  const scale = document.getElementById("map_scale");
  // Skládání .omap podle měřítka a volby DTM (sprint 1 mapa, les + MTBO 2) – nejdelší část jobu.
  const base = lastSheets.estimate_base_minutes;
  const maps = lastSheets.estimate_map_minutes;
  if (base && maps && scale && maps[scale.value]) {
    const dtmSel = document.getElementById("dtm");
    const dtm = dtmSel ? dtmSel.value : "auto";
    const sprint = Number(scale.value) <= 5000;
    const useDtm = dtm === "1" || (dtm === "auto" && sprint);
    // Serverový odhad počítá u lesních měřítek obě mapy; platí lineárně v počtu map.
    const share = sprint ? 1 : selectedMapTypes().length / 2;
    mins = Math.ceil(
      (wantRefs ? base.refs : base.plain) + maps[scale.value][useDtm ? "dtm" : "nodtm"] * share
    );
  }
  // Georef GeoTIFF @ 300 DPI podle měřítka (sprint 1 varianta, les + MTBO 2).
  const georef = document.getElementById("output_georef");
  const byScale = lastSheets.estimate_georef_minutes;
  if (georef && georef.checked && byScale && scale && byScale[scale.value] != null) {
    mins += Math.ceil(byScale[scale.value] * (Number(scale.value) > 5000 ? selectedMapTypes().length / 2 : 1));
  }
  return mins;
}

function updateSubmitButtonLabel() {
  refreshBboxBadge();
  refreshSubmitChecklist();
  refreshSettingsSummary();
  const btn = document.getElementById("submit-btn");
  if (!btn || jobSubmitInFlight || generateStartedJobId) return;
  const mins = selectedEstimateMinutes();
  if (mins != null && bboxAllowed) {
    btn.textContent = `Spustit generování (~${mins} min)`;
  } else {
    btn.textContent = "Spustit generování";
  }
}

/** Ověřený výřez (listy OK) proti limitu zvoleného měřítka; volá se i při změně měřítka. */
function applySheetsLimit() {
  const data = lastSheets;
  if (!data || data.too_large || !data.count) return;
  if (isSprintSelected() && data.sprint_hint) {
    bboxAllowed = false;
    styleBboxRect(true);
    setSheetInfo(data.sprint_hint, "warn");
  } else {
    bboxAllowed = true;
    styleBboxRect(false);
    setSheetInfo(sheetNamesText(data), "");
  }
  updateSubmitButtonLabel();
}

async function lookupSheets() {
  const bbox = document.getElementById("bbox-input").value;
  if (!bbox) return;
  bboxAllowed = false;
  lastSheets = null;
  bboxDraft = null;
  sheetsError = null;
  updateSubmitButtonLabel();
  const sheetsInput = document.getElementById("sm5-sheets-input");
  if (sheetsInput) sheetsInput.value = "";
  setSheetInfo("Zjišťuji mapové listy SM5…", "");
  // Po tažení úchytu může dorazit starší odpověď až po novější – tu zahodit.
  const seq = ++sheetsLookupSeq;
  try {
    const data = await api(`/api/sheets?bbox=${encodeURIComponent(bbox)}`);
    if (seq !== sheetsLookupSeq) return;
    lastSheets = data;
    if (data.max_area_km2) bboxMaxAreaKm2 = data.max_area_km2;
    if (data.sprint_max_area_km2) sprintMaxAreaKm2 = data.sprint_max_area_km2;
    if (data.too_large) {
      styleBboxRect(true);
      setSheetInfo(
        data.hint ||
          `Výřez je moc velký (max ${data.max_area_km2 || 25} km², např. 5 × 5 km; sprint 1:4000 max ${data.sprint_max_area_km2 || 9} km²). Zmenšete ho.`,
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
    if (sheetsInput && Array.isArray(data.sheets)) {
      sheetsInput.value = data.sheets.map((s) => s.mapnom).filter(Boolean).join(",");
    }
    applySheetsLimit();
  } catch (err) {
    if (seq !== sheetsLookupSeq) return;
    styleBboxRect(true);
    sheetsError = err.message;
    setSheetInfo(err.message, "err");
    updateSubmitButtonLabel();
  }
}

// --- Mobil: lišta s běžící mapou nahoře ---
const MOBILE_MQ = window.matchMedia("(max-width: 700px)");
const mobileBarDismissed = new Set();
let mobileBarJobId = null;
const barTracker = { id: null, after: 0, lines: [], progress: null };

/** Průběh jobu, který není vybraný v detailu (vlastní přírůstkové čtení logu, jen na mobilu). */
async function barProgressFor(id) {
  if (id === selectedJobId) return detailProgress;
  if (barTracker.id !== id) Object.assign(barTracker, { id, after: 0, lines: [], progress: null });
  try {
    const d = await api(`/api/jobs/${id}/log?after=${barTracker.after}`);
    for (const l of d.lines) {
      barTracker.after = l.id;
      if (PROGRESS_LINE_RE.test(l.line)) barTracker.lines.push(l.line);
    }
    if (d.lines.length) barTracker.progress = parseJobProgress(barTracker.lines);
  } catch (_) {
    /* lišta zůstane bez kroků */
  }
  return barTracker.progress;
}

async function updateMobileJobBar(data, selected) {
  const bar = document.getElementById("mobile-job-bar");
  if (!bar) return;
  if (!MOBILE_MQ.matches) {
    bar.hidden = true;
    return;
  }
  const pool = [...data.jobs];
  if (selected && !pool.some((j) => j.id === selected.id)) pool.push(selected);
  const mine = pool.filter((j) => isMyJob(j.id));
  const live = mine.filter((j) => jobIsLive(j.status));
  let job = live.find((j) => j.status === "running") || live[0] || null;
  if (job) mobileBarJobId = job.id;
  else if (mobileBarJobId) job = mine.find((j) => j.id === mobileBarJobId) || null;
  if (!job || mobileBarDismissed.has(job.id)) {
    bar.hidden = true;
    return;
  }
  const text = document.getElementById("mobile-job-text");
  const dl = document.getElementById("mobile-job-download");
  const close = document.getElementById("mobile-job-close");
  bar.dataset.jobId = job.id;
  let canDownload = false;
  if (jobIsLive(job.status)) {
    const parts = [`⏳ ${job.name}`];
    if (job.status === "running") {
      const prog = await barProgressFor(job.id);
      if (prog && prog.total) parts.push(`krok ${Math.max(1, prog.current)}/${prog.total}`);
      else parts.push("běží");
      const est = Number((myJobRecord(job.id) || {}).estimate_minutes);
      const el = elapsedSinceIso(job.started_at);
      if (est > 0 && el != null) {
        const left = Math.ceil((est * 60 - el) / 60);
        parts.push(left > 0 ? `~${left} min` : "déle než odhad");
      }
    } else {
      parts.push(job.queue_position ? `ve frontě #${job.queue_position}` : "ve frontě");
    }
    text.textContent = parts.join(" · ");
    close.hidden = true;
  } else if (job.status === "failed") {
    text.textContent = `✗ ${job.name} se nepovedla`;
    close.hidden = false;
  } else {
    canDownload = Boolean(job.has_output) && !jobIsPrivate(job);
    text.textContent = `✓ ${job.name} je hotová${canDownload ? " –" : ""}`;
    close.hidden = false;
  }
  dl.hidden = !canDownload;
  if (canDownload) dl.href = `/api/jobs/${job.id}/download`;
  bar.hidden = false;
}

function initMobileJobBar() {
  const bar = document.getElementById("mobile-job-bar");
  if (!bar) return;
  document.getElementById("mobile-job-detail")?.addEventListener("click", async (e) => {
    e.preventDefault();
    const id = bar.dataset.jobId;
    if (!id) return;
    await selectJob(id);
    document.querySelector(".jobs-card")?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  document.getElementById("mobile-job-close")?.addEventListener("click", () => {
    if (bar.dataset.jobId) mobileBarDismissed.add(bar.dataset.jobId);
    bar.hidden = true;
  });
  const onChange = () => {
    if (!MOBILE_MQ.matches) bar.hidden = true;
    else loadJobs().catch(() => {});
  };
  if (MOBILE_MQ.addEventListener) MOBILE_MQ.addEventListener("change", onChange);
}

// --- Paměť posledních voleb formuláře ---
/** Pole, která se nepamatují: identita mapy, výřez, kontakt, soukromí a jednorázové obnovení dat. */
const LAST_OPTIONS_SKIP = new Set([
  "name", "bbox", "reuse_job_id", "sm5_sheets", "notify_email", "private",
  "map_types_form", "force_refresh", "map_kind_ui",
]);
let factoryOptions = null;

function collectFormOptions() {
  const form = document.getElementById("job-form");
  const out = {};
  if (!form) return out;
  for (const el of form.elements) {
    const key = el.name;
    if (!key || LAST_OPTIONS_SKIP.has(key) || key === "map_types") continue;
    if (["hidden", "submit", "button", "text", "email"].includes(el.type)) continue;
    out[key] = el.type === "checkbox" ? el.checked : el.value;
  }
  out.map_types = selectedMapTypes();
  return out;
}

/** Pořadí je důležité: měřítko přestaví ekvidistance a typy map, teprve pak ostatní pole. */
function applyFormOptions(opts) {
  const form = document.getElementById("job-form");
  if (!form || !opts) return;
  const scaleSel = form.map_scale;
  if (scaleSel && opts.map_scale != null) {
    const kind = mapKindFor(opts.map_scale, opts.map_types);
    const kindRadio = document.querySelector(`input[name="map_kind_ui"][value="${kind}"]`);
    if (kindRadio) kindRadio.checked = true;
    filterScaleOptions(kind, opts.map_scale);
  }
  rebuildContourOptions(opts.contour_interval);
  const scale = scaleSel ? Number(scaleSel.value) : NaN;
  applyMapTypeDefaults(scale);
  if (scale > 5000 && Array.isArray(opts.map_types)) {
    const types = opts.map_types.filter((t) => t === "les" || t === "mtbo");
    if (types.length) setMapTypes(types.includes(currentMapKind()) ? types : [...types, currentMapKind()]);
  }
  for (const [key, value] of Object.entries(opts)) {
    if (["map_scale", "contour_interval", "map_types"].includes(key)) continue;
    if (LAST_OPTIONS_SKIP.has(key)) continue;
    const el = form.elements[key];
    if (!el || !el.tagName) continue;
    if (el.type === "checkbox") {
      el.checked = Boolean(value);
    } else if (el.tagName === "SELECT") {
      if ([...el.options].some((o) => o.value === String(value))) el.value = String(value);
    }
  }
  updateCliffControls();
  updateResidualPavedControls();
  updateOutputHints();
  updateSubmitButtonLabel();
}

function saveLastOptions() {
  storageSet(LAST_OPTIONS_KEY, JSON.stringify(collectFormOptions()));
}

/** Po načtení voleb měřítek: zapamatuj tovární hodnoty a obnov poslední použité. */
function initLastOptions() {
  factoryOptions = collectFormOptions();
  try {
    const saved = JSON.parse(storageGet(LAST_OPTIONS_KEY) || "null");
    if (saved && typeof saved === "object") applyFormOptions(saved);
  } catch (_) {
    /* poškozená paměť: zůstanou výchozí hodnoty */
  }
  document.getElementById("reset-options")?.addEventListener("click", () => {
    storageRemove(LAST_OPTIONS_KEY);
    applyFormOptions(factoryOptions);
    showFormNotice("Nastavení je vrácené na výchozí hodnoty.");
  });
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
    // Seznam změn se sám nerozbaluje – rozbalí ho jen klik na „Co je nového“.
    box.open = false;
    const warning = document.getElementById("whats-new-warning");
    if (warning) {
      const fresh = tone === "hot" || tone === "warm";
      warning.textContent = fresh
        ? data.disclaimer || "Nový build – něco nemusí fungovat."
        : "";
      warning.hidden = !fresh;
    }
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
          ? "Nedávné změny:"
          : "V přehledu nejsou žádné větší změny.");
    }
    const fillList = (ul, entries) => {
      if (!ul) return;
      ul.innerHTML = "";
      for (const entry of entries || []) {
        const li = document.createElement("li");
        const date = document.createElement("span");
        date.className = "whats-new-date";
        date.textContent = entry.date || "";
        const strong = document.createElement("strong");
        strong.textContent = entry.title || "";
        li.appendChild(date);
        li.appendChild(strong);
        ul.appendChild(li);
      }
    };
    fillList(list, data.entries || []);
    const msLead = document.getElementById("whats-new-milestones-lead");
    const msList = document.getElementById("whats-new-milestones");
    const milestones = data.milestones || [];
    if (msLead) {
      if (milestones.length) {
        msLead.hidden = false;
        msLead.textContent = data.milestones_lead || "Dřívější milníky:";
      } else {
        msLead.hidden = true;
        msLead.textContent = "";
      }
    }
    fillList(msList, milestones);
    box.hidden = false;
  } catch (_) {
    box.hidden = true;
  }
}

loadWhatsNew();
initJobsTabs();
initMobileJobBar();
loadMapOptions().then(initLastOptions).then(loadJobs).then(startPolling);
initJobsPager();
initBboxMap();
(() => {
  const form = document.getElementById("job-form");
  if (form) {
    form.addEventListener("input", clearGenerateStarted);
    form.addEventListener("change", clearGenerateStarted);
    const refreshForm = () => {
      refreshSubmitChecklist();
      refreshSettingsSummary();
    };
    form.addEventListener("input", refreshForm);
    form.addEventListener("change", refreshForm);
    refreshForm();
  }
  // „Zkusit znovu“ i „Použít nastavení a výřez“: vyplní formulář, spustí až uživatel.
  const fillFromDetail = () => {
    if (!detailJob) return;
    applyJobToForm(detailJob);
    showFormNotice(
      `Formulář je vyplněný podle mapy „${detailJob.name}“. Upravte, co potřebujete, a spusťte.`
    );
    const formCard = document.querySelector(".job-form-card");
    if (formCard) formCard.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  for (const id of ["detail-use-template", "detail-retry"]) {
    document.getElementById(id)?.addEventListener("click", fillFromDetail);
  }
  // Rozbitý náhled neukazuj (žádná rozbitá ikona).
  const previewImg = document.getElementById("detail-img");
  if (previewImg) {
    previewImg.addEventListener("error", () => {
      previewImg.classList.add("hidden");
      previewImg.dataset.failedFor = selectedJobId || "";
    });
  }
  const refs = document.getElementById("output_references");
  if (refs) refs.addEventListener("change", updateSubmitButtonLabel);
  for (const id of ["output_georef", "map_scale", "dtm"]) {
    const el = document.getElementById(id);
    if (el) el.addEventListener("change", updateSubmitButtonLabel);
  }
  updateSubmitButtonLabel();
})();
(() => {
  const cliff = document.getElementById("kp_cliff_symbol");
  if (!cliff) return;
  cliff.addEventListener("change", updateCliffControls);
  updateCliffControls();
})();
(() => {
  const cb = document.getElementById("sprint_residual_paved");
  if (!cb) return;
  cb.addEventListener("change", updateResidualPavedControls);
  updateResidualPavedControls();
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
(() => {
  const form = document.getElementById("feedback-form");
  if (!form) return;
  const statusEl = document.getElementById("feedback-status");
  const submitBtn = document.getElementById("feedback-submit");
  const setStatus = (msg, kind) => {
    if (!statusEl) return;
    statusEl.textContent = msg || "";
    statusEl.classList.toggle("ok", kind === "ok");
    statusEl.classList.toggle("err", kind === "err");
  };
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const contact = (document.getElementById("feedback-contact")?.value || "").trim();
    const comment = (document.getElementById("feedback-comment")?.value || "").trim();
    const jobId = (document.getElementById("feedback-job-id")?.value || "").trim();
    const website = (document.getElementById("feedback-website")?.value || "").trim();
    if (!contact) {
      setStatus("Vyplňte kontakt (e-mail nebo telefon).", "err");
      return;
    }
    if (!comment) {
      setStatus("Napište komentář.", "err");
      return;
    }
    if (!jobId) {
      setStatus("Vyberte mapu v seznamu — zpětná vazba patří ke konkrétní mapě.", "err");
      return;
    }
    if (submitBtn) submitBtn.disabled = true;
    setStatus("Odesílám…", "");
    try {
      const res = await fetch("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          contact,
          comment,
          job_id: jobId,
          website,
        }),
      });
      const text = await res.text();
      if (!res.ok) {
        setStatus(parseApiError(res, text), "err");
        return;
      }
      setStatus("Díky — zpráva je u Petra.", "ok");
      form.reset();
      const jobField = document.getElementById("feedback-job-id");
      if (jobField && selectedJobId) jobField.value = selectedJobId;
    } catch (err) {
      setStatus(err.message || "Odeslání selhalo.", "err");
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  });
})();
