async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
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
    div.dataset.id = job.id;
    div.innerHTML = `
      <strong>${escapeHtml(job.name)}</strong>
      <div class="status status-${job.status}">${job.status}${job.phase ? " · " + job.phase : ""}</div>
      <div class="status">${job.preset_id} · ${job.created_at.slice(0, 19)}</div>
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
  btn.disabled = true;
  btn.textContent = "Odesílám…";
  try {
    const fd = new FormData(form);
    fd.set("run_vectors", form.run_vectors.checked ? "true" : "false");
    fd.set("output_png", form.output_png.checked ? "true" : "false");
    fd.set("output_dxf", form.output_dxf.checked ? "true" : "false");
    fd.set("output_zabaged_clean", form.output_zabaged_clean.checked ? "true" : "false");
    fd.set("savetempfolders", form.savetempfolders.checked ? "true" : "false");
    const job = await api("/api/jobs", { method: "POST", body: fd });
    await loadJobs();
    selectJob(job.id);
  } catch (err) {
    alert(err.message);
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
