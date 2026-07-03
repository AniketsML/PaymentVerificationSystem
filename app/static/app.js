/* Payment Verification Console — single-page app (vanilla JS). */
(() => {
  const CFG = window.__CFG__ || { model: {}, deepLead: "" };
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim() || "#888";
  const palette = () => ({ verified: cssVar("--ok"), unverified: cssVar("--bad"),
                           manual_review: cssVar("--warn"), duplicate: cssVar("--dup"),
                           non_document: cssVar("--neutral"), error: cssVar("--warn") });
  const fmtTime = (t) => (t || "").replace("T", " ").slice(0, 19);
  const debounce = (fn, ms = 280) => { let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); }; };

  let state = { status: "all", q: "" };
  let charts = {};

  /* ── toast ─────────────────────────────── */
  function toast(msg, kind = "") {
    const t = document.createElement("div");
    t.className = "toast " + kind; t.innerHTML = msg;
    $("#toasts").appendChild(t);
    setTimeout(() => { t.style.opacity = "0"; t.style.transform = "translateX(20px)"; t.style.transition = ".3s"; }, 3200);
    setTimeout(() => t.remove(), 3600);
  }
  const badge = (s) => `<span class="badge b-${esc(s)}">${esc(s)}</span>`;

  /* ── view switching ────────────────────── */
  const TITLES = {
    dashboard: ["Overview", "Dashboard", "Live verification overview"],
    verify: ["Run", "Verify", "Enqueue a batch or test a single receipt"],
  };
  function switchView(name) {
    $$(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === name));
    $$(".view").forEach(v => v.classList.toggle("hidden", v.id !== "view-" + name));
    $("#pageEyebrow").textContent = TITLES[name][0];
    $("#pageTitle").textContent = TITLES[name][1];
    $("#pageSub").textContent = TITLES[name][2];
    if (name === "dashboard") refreshAll();
  }

  /* ── theme ─────────────────────────────── */
  function setTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    localStorage.setItem("pv-theme", t);
    const lbl = $("#themeLabel"); if (lbl) lbl.textContent = t === "dark" ? "Light" : "Dark";
    // charts bake in colors at creation — rebuild them for the new palette
    Object.values(charts).forEach(c => c.destroy()); charts = {};
    if (!$("#view-dashboard").classList.contains("hidden")) loadStats();
  }

  /* ── stat cards + charts ───────────────── */
  function animateNum(elm, to) {
    const from = 0, dur = 650, t0 = performance.now();
    const step = (now) => {
      const p = Math.min(1, (now - t0) / dur);
      elm.textContent = Math.round(from + (to - from) * (1 - Math.pow(1 - p, 3))).toLocaleString();
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function renderStats(counts) {
    const total = counts.total || 0;
    const cards = [
      ["tot", "Total leads", total],
      ["ok", "Verified", counts.verified || 0],
      ["bad", "Unverified", counts.unverified || 0],
      ["warn", "Manual review", counts.manual_review || 0],
      ["dup", "Duplicate", counts.duplicate || 0],
      ["gray", "Non-document", counts.non_document || 0],
    ];
    $("#statCards").innerHTML = cards.map(([c, l, n]) => {
      const pct = total ? Math.round(n / total * 100) : 0;
      const showPct = c !== "tot" ? `<div class="pct">${pct}%</div>` : "";
      return `<div class="stat ${c}">${showPct}<div class="n" data-n="${n}">0</div><div class="l">${l}</div></div>`;
    }).join("");
    $$("#statCards .n").forEach(e => animateNum(e, +e.dataset.n));
  }

  function renderDonut(counts) {
    const C = palette();
    $("#donutTotal").textContent = (counts.total || 0).toLocaleString();
    const keys = ["verified", "unverified", "manual_review", "duplicate", "non_document"];
    const data = keys.map(k => counts[k] || 0);
    $("#donutLegend").innerHTML = keys.map((k, i) =>
      `<span class="li"><span class="sw" style="background:${C[k]}"></span>${k} · <b>${data[i]}</b></span>`).join("");
    if (!window.Chart) return;
    if (charts.donut) { charts.donut.data.datasets[0].data = data; charts.donut.update(); return; }
    charts.donut = new Chart($("#donut"), {
      type: "doughnut",
      data: { labels: keys, datasets: [{ data, backgroundColor: keys.map(k => C[k]),
              borderWidth: 2, borderColor: cssVar("--surface"), hoverOffset: 5 }] },
      options: { cutout: "70%", plugins: { legend: { display: false } }, animation: { animateRotate: true } }
    });
  }

  function renderMethods(methods) {
    const top = methods.slice(0, 8);
    const labels = top.map(m => m.method), data = top.map(m => m.n);
    if (!window.Chart) return;
    if (charts.method) { charts.method.data.labels = labels; charts.method.data.datasets[0].data = data; charts.method.update(); return; }
    charts.method = new Chart($("#methodBar"), {
      type: "bar",
      data: { labels, datasets: [{ data, backgroundColor: cssVar("--accent"), barThickness: 15 }] },
      options: {
        indexAxis: "y", plugins: { legend: { display: false } },
        scales: {
          x: { grid: { color: cssVar("--line") }, border: { display: false },
               ticks: { color: cssVar("--ink-faint"), font: { family: "JetBrains Mono", size: 11 } } },
          y: { grid: { display: false }, border: { color: cssVar("--line-strong") },
               ticks: { color: cssVar("--ink-soft"), font: { family: "Hanken Grotesk", size: 12 } } }
        }
      }
    });
  }

  /* ── leads table ───────────────────────── */
  async function loadStats() {
    try {
      const s = await (await fetch("/api/stats")).json();
      renderStats(s.counts); renderDonut(s.counts); renderMethods(s.methods || []);
    } catch (e) { toast("Failed to load stats", "bad"); }
  }

  async function loadLeads() {
    const url = `/api/leads?status=${encodeURIComponent(state.status)}&q=${encodeURIComponent(state.q)}&limit=400`;
    let rows = [];
    try { rows = await (await fetch(url)).json(); } catch (e) { toast("Failed to load leads", "bad"); }
    const body = $("#leadsBody");
    $("#tableCount").textContent = `${rows.length} lead${rows.length === 1 ? "" : "s"}`;
    $("#tableEmpty").classList.toggle("hidden", rows.length > 0);
    body.innerHTML = rows.map(r => `
      <tr data-id="${esc(r.lead_id)}">
        <td class="lead-id mono">${esc(r.lead_id)}</td>
        <td>${esc(r.lender || "—")}</td>
        <td>${badge(r.verification_status)}</td>
        <td>${esc(r.payment_method || "—")}</td>
        <td class="cell-outcome">${esc(r.outcome_text || "")}</td>
        <td class="cell-time mono">${fmtTime(r.updated_at)}</td>
      </tr>`).join("");
    $$("#leadsBody tr").forEach(tr => tr.onclick = () => openLead(tr.dataset.id));
  }

  function refreshAll() { loadStats(); loadLeads(); }

  /* ── lead drawer ───────────────────────── */
  const STAGE_LABEL = {
    lead_received: "Received", stage0_load_image: "Load image", stage1_image_qc: "Image QC",
    stage2_ocr_classify: "OCR + classify", stage3_extract: "Extract",
    stage4_duplicate_check: "Duplicate check", stage4_verify: "Verify", lead_closed: "Closed",
  };
  function evHtml(e, i) {
    const cls = e.status === "PASS" ? "pass" : "fail";
    const ms = (e.ms != null && e.ms > 0) ? `<span class="ev-ms">${Math.round(e.ms)} ms</span>` : "";
    const d = e.data || {}, m = e.metrics || {};
    let extra = "";
    if (Object.keys(m).length) extra += `<details><summary>metrics</summary><pre>${esc(JSON.stringify(m, null, 2))}</pre></details>`;
    if (d.raw_model_response) extra += `<details><summary>raw model response</summary><pre>${esc(d.raw_model_response)}</pre></details>`;
    if (d.full_text) extra += `<details><summary>full OCR text</summary><pre>${esc(d.full_text)}</pre></details>`;
    if (d.model_meta) extra += `<details><summary>model call meta</summary><pre>${esc(JSON.stringify(d.model_meta, null, 2))}</pre></details>`;
    const skip = new Set(["raw_model_response", "full_text", "model_meta", "extracted", "image_source", "system_record"]);
    const rest = Object.fromEntries(Object.entries(d).filter(([k]) => !skip.has(k)));
    if (Object.keys(rest).length) extra += `<details><summary>data</summary><pre>${esc(JSON.stringify(rest, null, 2))}</pre></details>`;
    return `<div class="ev ${cls}"><span class="ev-node"></span>
      <div class="ev-head">
        <span class="ev-idx">${String(i + 1).padStart(2, "0")}</span>
        <span class="ev-stage">${esc(STAGE_LABEL[e.stage] || e.stage)}</span>
        <span class="ev-flag ${cls}">${esc(e.status)}</span>${ms}</div>
      ${e.reason ? `<div class="ev-reason">${esc(e.reason)}</div>` : ""}
      ${extra}
      <div class="ev-ts">${fmtTime(e.ts)}</div></div>`;
  }

  // split an append-only event log into separate processing runs (each begins at lead_received)
  function groupRuns(events) {
    const runs = []; let cur = null;
    for (const e of events) {
      if (e.stage === "lead_received" || !cur) { cur = []; runs.push(cur); }
      cur.push(e);
    }
    return runs;
  }

  const hasVal = (v) => v !== undefined && v !== null && String(v).trim() !== "";
  function cvVal(v) {
    const s = String(v ?? "");
    if (/^https?:\/\//i.test(s)) return `<a href="${esc(s)}" target="_blank">${esc(s.slice(0, 58))}${s.length > 58 ? "…" : ""}</a>`;
    return esc(s.length > 200 ? s.slice(0, 200) + "…" : s);
  }

  // per-field ✓/✗ taken straight from the verdict verify.py already produced (no new logic)
  function fieldVerdict(outcome) {
    const ok = new Set([...(outcome.verified_fields || []), ...(outcome.matched_fields || [])]);
    const bad = new Set(outcome.failed_fields || []);
    return (k) => ok.has(k) ? "ok" : (bad.has(k) ? "bad" : "na");
  }

  function comparisonHtml(csv, ex, outcome) {
    csv = csv || {}; ex = ex || {};
    const vf = fieldVerdict(outcome || {});
    const rows = [
      ["Amount", csv.payment_amount, ex.amount, "amount"],
      ["Date", csv.payment_date, ex.date, "date"],
      ["Receiver", csv.institute_name, ex.receiver_name, "receiver"],
      ["Loan a/c", csv.loan_account_number, ex.loan_account_number, "loan_account_number"],
      ["Reference", csv.transaction_id, ex.reference_id, null],
    ];
    const mark = { ok: "✓", bad: "✗", na: "—" };
    const body = rows.map(([label, exp, ext, key]) => {
      const v = key ? vf(key) : "na";
      return `<div class="cmp-row${v === "bad" ? " miss" : ""}">
        <div class="cmp-f">${label}</div>
        <div class="cmp-v exp${hasVal(exp) ? "" : " empty"}">${hasVal(exp) ? cvVal(exp) : "—"}</div>
        <div class="cmp-v ext${hasVal(ext) ? "" : " empty"}">${hasVal(ext) ? cvVal(ext) : "—"}</div>
        <div class="cmp-m ${v}">${mark[v]}</div></div>`;
    }).join("");
    return `<div class="cmp">
        <div class="cmp-row head"><div>Field</div><div>Expected · CSV</div><div>Document</div><div></div></div>
        ${body}</div>`;
  }

  function allColsHtml(csv) {
    if (!csv) return "";
    const shown = new Set(["payment_amount", "payment_date", "institute_name", "loan_account_number", "transaction_id"]);
    const rest = Object.entries(csv).filter(([k, v]) => !shown.has(k) && hasVal(v));
    if (!rest.length) return "";
    return `<details><summary>all ${rest.length} CSV columns</summary>
      <div class="kv" style="margin-top:12px">${rest.map(([k, v]) =>
        `<div class="k">${esc(k)}</div><div class="v${/^https?:/i.test(String(v)) ? " mono" : ""}">${cvVal(v)}</div>`).join("")}</div></details>`;
  }

  function extraExtractedHtml(ex) {
    const extra = ["document_type", "payment_method", "time", "payer_name"].filter(k => hasVal(ex[k]));
    const fl = ex.field_labels && Object.keys(ex.field_labels).length;
    if (!extra.length && !fl) return "";
    const kv = extra.map(k => `<div class="k">${esc(k)}</div><div class="v">${esc(ex[k])}</div>`).join("");
    return `<details><summary>other extracted fields${fl ? " + labels" : ""}</summary>
      ${kv ? `<div class="kv" style="margin-top:12px">${kv}</div>` : ""}
      ${fl ? `<pre style="margin-top:10px">${esc(JSON.stringify(ex.field_labels, null, 2))}</pre>` : ""}</details>`;
  }

  const verifyOutcome = (o) => (o && o.verification) || o || {};   // manual_review nests the verify result
  function chipsFrom(o) {
    const mf = o.verified_fields || o.matched_fields || [], ff = o.failed_fields || [];
    if (!mf.length && !ff.length) return "";
    return `<div class="fchips">${mf.map(f => `<span class="fchip ok">${esc(f)}</span>`).join("")}${ff.map(f => `<span class="fchip bad">${esc(f)}</span>`).join("")}</div>`;
  }
  function outcomeHtml(status, outcome) {
    outcome = outcome || {};
    let note, cls = "", chips = "";
    if (status === "verified") {
      cls = "ok"; note = "All mandatory fields matched."; chips = chipsFrom(outcome);
    } else if (status === "non_document") {
      note = outcome.describes || "Not a valid payment document.";
    } else if (status === "duplicate") {
      cls = "dup"; note = outcome.reason || "Duplicate submission — already processed.";
    } else if (status === "manual_review") {
      cls = "warn"; note = outcome.reason || "Flagged for manual review."; chips = chipsFrom(verifyOutcome(outcome));
    } else {
      cls = "bad"; note = outcome.reason || "Not verified."; chips = chipsFrom(outcome);
    }
    return `<div class="d-section"><h4>Outcome</h4>
      <div class="outcome-note ${cls}">${esc(note)}</div>${chips}
      <details><summary>raw outcome</summary><pre>${esc(JSON.stringify(outcome, null, 2))}</pre></details></div>`;
  }

  function renderDrawer(j) {
    const status = (j.final && j.final.verification_status) || "unknown";
    $("#dLeadId").textContent = j.lead_id;
    const b = $("#dStatus"); b.className = "badge b-" + status; b.textContent = status;
    $("#dRawLink").href = "/logs/" + encodeURIComponent(j.lead_id);
    const ex = j.extracted || {}, fin = j.final || {};
    const summary = `<div class="lead-summary">
      <div class="ls-cell"><div class="ls-k">Status</div><div class="ls-v">${badge(status)}</div></div>
      <div class="ls-cell"><div class="ls-k">Lender</div><div class="ls-v">${esc(fin.lender || "—")}</div></div>
      <div class="ls-cell"><div class="ls-k">Method</div><div class="ls-v">${esc(fin.payment_method || "—")}</div></div>
      <div class="ls-cell"><div class="ls-k">Updated · UTC</div><div class="ls-v mono">${fmtTime(fin.updated_at) || "—"}</div></div>
    </div>`;
    const imgHtml = j.image_url ? `<div class="d-section"><h4>Document</h4><a href="${esc(j.image_url)}" target="_blank"><img class="d-img" src="${esc(j.image_url)}"></a></div>` : "";
    const compare = `<div class="d-section"><h4>Verification <span class="n">expected ↔ document</span></h4>
      ${comparisonHtml(j.csv_row, ex, verifyOutcome(j.outcome))}
      ${allColsHtml(j.csv_row)}
      ${extraExtractedHtml(ex)}</div>`;
    $("#drawerBody").innerHTML = `
      ${summary}
      ${imgHtml}
      ${compare}
      ${outcomeHtml(status, j.outcome)}
      ${lifecycleHtml(j.journey)}`;
  }

  function lifecycleHtml(journey) {
    const runs = groupRuns(journey || []);
    const latest = runs[runs.length - 1] || [];
    const meta = runs.length > 1 ? `latest of ${runs.length} runs` : `${latest.length} events`;
    let html = `<div class="d-section"><h4>Lifecycle <span class="n">${meta}</span></h4>
      <div class="tl">${latest.map((e, i) => evHtml(e, i)).join("")}</div>`;
    if (runs.length > 1) {
      html += `<div class="runs-more">`;
      for (let i = runs.length - 2; i >= 0; i--) {
        const run = runs[i], ts = fmtTime(run[0] && run[0].ts);
        html += `<details style="margin-bottom:8px"><summary>Run ${i + 1} · ${ts} · ${run.length} events</summary>
          <div class="tl" style="margin-top:14px">${run.map((e, k) => evHtml(e, k)).join("")}</div></details>`;
      }
      html += `</div>`;
    }
    return html + `</div>`;
  }

  async function openLead(id) {
    try {
      const r = await fetch("/api/lead/" + encodeURIComponent(id));
      if (!r.ok) { toast("Lead not found: " + esc(id), "bad"); return; }
      renderDrawer(await r.json());
      $("#drawer").classList.add("open"); $("#scrim").classList.add("open");
      history.replaceState({}, "", "/lead/" + encodeURIComponent(id));
    } catch (e) { toast("Failed to open lead", "bad"); }
  }
  function closeDrawer() {
    $("#drawer").classList.remove("open"); $("#scrim").classList.remove("open");
    history.replaceState({}, "", "/");
  }

  /* ── batch: enqueue + poll (durable job queue) ─────────────────────────── */
  function miniCounts(v) {
    const cells = [["ok", "verified", v.verified || 0], ["bad", "unverified", v.unverified || 0],
                   ["gray", "non_document", v.non_document || 0]];
    $("#miniCounts").innerHTML = cells.map(([cl, l, n]) =>
      `<div class="mini ${cl}"><div class="mn">${n}</div><div class="ml">${l}</div></div>`).join("");
  }

  let pollTimer = null;
  async function runCsvStream() {
    const file = $("#csvFile").files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    fd.append("image_col", $("#imageCol").value || "payment_document");
    if ($("#idCol").value) fd.append("id_col", $("#idCol").value);
    if ($("#imageRoot").value) fd.append("image_root", $("#imageRoot").value);
    if ($("#precomputed").checked) fd.append("precomputed", "on");

    const btn = $("#runCsv"); btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Enqueuing…`;
    let r;
    try { r = await (await fetch("/api/enqueue", { method: "POST", body: fd })).json(); }
    catch (e) { toast("Upload failed", "bad"); btn.disabled = false; btn.textContent = "Run batch"; return; }
    if (r.error) { toast(esc(r.error), "bad"); btn.disabled = false; btn.textContent = "Run batch"; return; }

    $("#runPanel").classList.remove("hidden"); $("#doneBanner").classList.add("hidden");
    $("#liveList").innerHTML = ""; $("#progBar").style.width = "0%"; miniCounts({});
    $("#dlBtn").href = "/download/" + encodeURIComponent(r.batch_id);
    toast(`Enqueued ${r.enqueued} lead${r.enqueued === 1 ? "" : "s"}${r.skipped ? ` · ${r.skipped} already done (skipped)` : ""}`, "ok");
    btn.innerHTML = `<span class="spinner"></span> Processing…`;

    if (pollTimer) clearInterval(pollTimer);
    const poll = async () => {
      let p;
      try { p = await (await fetch("/api/batch/" + encodeURIComponent(r.batch_id))).json(); }
      catch { return; }
      const total = p.total || 0, done = p.done || 0, failed = p.failed || 0;
      const finished = done + failed;
      $("#progBar").style.width = (total ? (finished / total * 100) : 0) + "%";
      $("#progText").textContent = `${finished} / ${total}` + (p.in_progress ? ` · ${p.in_progress} running` : "");
      $("#progElapsed").textContent = failed ? `${failed} failed` : "";
      miniCounts(p.verdicts || {});
      const list = $("#liveList");
      list.innerHTML = (p.leads || []).map(x => {
        const st = x.verification_status || (x.job_status === "failed" ? "error" :
                   x.job_status === "done" ? "done" : "pending");
        const cls = ["verified", "unverified", "non_document", "error"].includes(st) ? st : "pending";
        const label = x.job_status === "pending" ? "queued" : x.job_status === "in_progress" ? "running…" : (x.outcome_text || x.last_error || "");
        return `<div class="live-row" data-id="${esc(x.lead_id)}">
          <span class="badge b-${cls}">${esc(st)}</span>
          <span class="lr-id mono">${esc(x.lead_id)}</span>
          <span class="lr-out">${esc(label)}</span></div>`;
      }).join("");
      $$("#liveList .live-row").forEach(row => row.onclick = () => openLead(row.dataset.id));

      if (total > 0 && p.pending === 0 && p.in_progress === 0) {
        clearInterval(pollTimer); pollTimer = null;
        $("#progBar").style.width = "100%";
        $("#doneBanner").classList.remove("hidden");
        btn.disabled = false; btn.textContent = "Run batch";
        toast(`✓ Batch complete — ${done} done${failed ? `, ${failed} failed` : ""}`, "ok");
        loadStats();
      }
    };
    poll();
    pollTimer = setInterval(poll, 1500);
  }

  /* ── single image ──────────────────────── */
  async function runImage() {
    const file = $("#imgFile").files[0]; if (!file) return;
    const fd = new FormData();
    fd.append("image", file);
    fd.append("institute_name", $("#siLender").value);
    fd.append("payment_amount", $("#siAmount").value);
    fd.append("payment_date", $("#siDate").value);
    fd.append("loan_account_number", $("#siLan").value);
    const btn = $("#runImg"); btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Reading with model…`;
    try {
      const r = await (await fetch("/api/verify_image", { method: "POST", body: fd })).json();
      if (r.error) throw new Error(r.error);
      toast(`Result: ${badge(r.verification_status)}`, r.verification_status === "verified" ? "ok" : "");
      openLead(r.lead_id);
    } catch (e) { toast("Extraction failed: " + esc(e.message || e), "bad"); }
    btn.disabled = false; btn.textContent = "Extract & verify";
  }

  /* ── dropzone wiring ───────────────────── */
  function wireDrop(zoneSel, inputSel, onPick) {
    const zone = $(zoneSel), input = $(inputSel);
    zone.onclick = () => input.click();
    input.onchange = () => input.files[0] && onPick(input.files[0]);
    ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add("drag"); }));
    ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.remove("drag"); }));
    zone.addEventListener("drop", e => {
      const f = e.dataTransfer.files[0]; if (f) { input.files = e.dataTransfer.files; onPick(f); }
    });
  }

  /* ── init ──────────────────────────────── */
  function init() {
    $("#mcModel").textContent = CFG.model.model || "model";
    $("#mcUrl").textContent = (CFG.model.url || "") + (CFG.model.stream ? " · stream" : "");

    // theme toggle (initial theme was set pre-paint by the inline script)
    const cur = document.documentElement.getAttribute("data-theme") || "light";
    $("#themeLabel").textContent = cur === "dark" ? "Light" : "Dark";
    $("#themeToggle").onclick = () =>
      setTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");

    $$(".nav-item").forEach(b => b.onclick = () => switchView(b.dataset.view));
    $("#newRunBtn").onclick = () => switchView("verify");
    $("#refreshBtn").onclick = refreshAll;
    $("#drawerClose").onclick = closeDrawer;
    $("#scrim").onclick = closeDrawer;
    document.addEventListener("keydown", e => { if (e.key === "Escape") closeDrawer(); });

    $$("#statusChips .chip").forEach(c => c.onclick = () => {
      $$("#statusChips .chip").forEach(x => x.classList.remove("active"));
      c.classList.add("active"); state.status = c.dataset.status; loadLeads();
    });
    $("#globalSearch").addEventListener("input", debounce(e => {
      state.q = e.target.value.trim(); switchView("dashboard"); loadLeads();
    }));

    // CSV
    wireDrop("#csvDrop", "#csvFile", f => { $("#csvName").textContent = f.name; $("#runCsv").disabled = false; });
    $("#runCsv").onclick = runCsvStream;
    // image
    wireDrop("#imgDrop", "#imgFile", f => {
      $("#imgName").textContent = f.name; $("#runImg").disabled = false;
      const p = $("#imgPreview"); p.src = URL.createObjectURL(f); p.classList.remove("hidden");
    });
    $("#runImg").onclick = runImage;

    refreshAll();
    if (CFG.deepLead) openLead(CFG.deepLead);
  }
  document.addEventListener("DOMContentLoaded", init);
})();
