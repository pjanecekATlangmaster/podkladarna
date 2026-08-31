function parseApiError(res, text) {
  if (res.status === 413) {
    return (
      "Soubor je příliš velký pro reverse proxy (HTTP 413). " +
      "Na Synology zvyšte client_max_body_size v nginx (viz DEPLOY.md), " +
      "nebo nahrajte soubory přes http://IP_NAS:8672 bez HTTPS proxy."
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
  for (const [id, p] of Object.entries(data)) {
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = p.label || id;
    sel.appendChild(opt);
  }
}

async function loadJobs() {
  const data = await api("/api/jobs");
  const list = document.getElementById("jobs-list");
  list.innerHTML = "";
  for (const job of data.jobs) {
    const div = document.createElement("div");
    div.className = "job-item";
    if (job.id === selectedJobId) div.classList.add("selected");
    div.dataset.id = job.id;
    div.innerHTML = `
      <strong>${escapeHtml(job.name)}</strong>
      <div class="status status-${job.status}">${job.status}${job.phase ? " · " + job.phase : ""}</div>
      <div class="status">${job.preset_id} · ${job.created_at.slice(0, 19)}</div>
      ${job.error ? `<div class="status error">${escapeHtml(job.error)}</div>` : ""}
    `;
    div.onclick = () => selectJob(job.id);
    list.appendChild(div);
  }
  if (selectedJobId) await refreshLog();
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
  btn.disabled = true;
  btn.textContent = "Nahrávám soubory… (může trvat minuty)";
  try {
    const fd = new FormData(form);
    fd.set("run_vectors", form.run_vectors.checked ? "true" : "false");
    fd.set("output_png", form.output_png.checked ? "true" : "false");
    fd.set("output_dxf", form.output_dxf.checked ? "true" : "false");
    fd.set("output_zabaged_clean", form.output_zabaged_clean.checked ? "true" : "false");
    fd.set("savetempfolders", form.savetempfolders.checked ? "true" : "false");
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

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    await loadJobs();
  }, 2500);
}

loadPresets().then(loadJobs).then(startPolling);
