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

  let state = { status: "all", q: "", scope: "real" };
  let charts = {};

  // per-column filters for the leads table. null = no filter; a Set = allowed values.
  let allRows = [];
  const COL_FILTERS = { lead_id: null, lender: null, payment_method: null, outcome_text: null };
  const rowVal = (r, col) => String(r[col] ?? "") || "—";

  /* ── toast ─────────────────────────────── */
  function toast(msg, kind = "", ms = 3200) {
    const t = document.createElement("div");
    t.className = "toast " + kind; t.innerHTML = msg;
    $("#toasts").appendChild(t);
    setTimeout(() => { t.style.opacity = "0"; t.style.transform = "translateX(20px)"; t.style.transition = ".3s"; }, ms);
    setTimeout(() => t.remove(), ms + 400);
  }
  const badge = (s) => `<span class="badge b-${esc(s)}">${esc(s)}</span>`;

  /* ── view switching ────────────────────── */
  const TITLES = {
    dashboard: ["Overview", "Dashboard", "Live verification overview"],
    verify: ["Run", "Verify", "Enqueue a batch or test a single receipt"],
    observability: ["Operations", "Observability", "Latency, throughput, quality & drift"],
  };
  let liveTimer = null;
  function startLive(name) {
    if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
    // Observability polls live so it updates during a run (charts/KPIs redraw smoothly,
    // no filter state to lose). The dashboard is left on manual/whole-view refresh to
    // avoid re-animating the stat cards and resetting the leads-table column filters.
    if (name === "observability") liveTimer = setInterval(loadObservability, 4000);
  }

  function switchView(name) {
    $$(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === name));
    $$(".view").forEach(v => v.classList.toggle("hidden", v.id !== "view-" + name));
    $("#pageEyebrow").textContent = TITLES[name][0];
    $("#pageTitle").textContent = TITLES[name][1];
    $("#pageSub").textContent = TITLES[name][2];
    if (name === "dashboard") refreshAll();
    if (name === "observability") loadObservability();
    startLive(name);
  }

  /* ── theme ─────────────────────────────── */
  function setTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    localStorage.setItem("pv-theme", t);
    const lbl = $("#themeLabel"); if (lbl) lbl.textContent = t === "dark" ? "Light" : "Dark";
    // charts bake in colors at creation — rebuild them for the new palette
    Object.values(charts).forEach(c => c.destroy()); charts = {};
    if (!$("#view-dashboard").classList.contains("hidden")) loadStats();
    if (!$("#view-observability").classList.contains("hidden")) loadObservability();
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
    const keys = ["verified", "unverified", "duplicate", "non_document"];
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
      const s = await (await fetch("/api/stats?scope=" + state.scope)).json();
      renderStats(s.counts); renderDonut(s.counts); renderMethods(s.methods || []);
    } catch (e) { toast("Failed to load stats", "bad"); }
  }

  // The Test Workspace: one switch flips the ENTIRE app — uploads, dashboard, donut,
  // methods, leads table AND observability — between real ("Live") and sandbox data.
  function setScope(scope) {
    if (state.scope === scope) return;
    state.scope = scope;
    document.body.classList.toggle("scope-test", scope === "test");
    $("#scopeBanner").classList.toggle("hidden", scope !== "test");
    $$("#wsSwitch .ws-opt").forEach(b => b.classList.toggle("active", b.dataset.scope === scope));
    refreshAll();
    if (!$("#view-observability").classList.contains("hidden")) loadObservability();
    if (scope === "test") toast("Entered Test Workspace — sandbox data only", "");
  }

  async function clearWorkspace() {
    if (!confirm("Clear the Test Workspace? This deletes all sandbox data. Your live records and the OCR cache are untouched.")) return;
    try {
      const r = await (await fetch("/api/clear_test", { method: "POST" })).json();
      toast(`Workspace cleared — ${r.results || 0} sandbox lead${r.results === 1 ? "" : "s"} removed`, "ok");
      refreshAll();
      if (!$("#view-observability").classList.contains("hidden")) loadObservability();
    } catch (e) { toast("Clear failed", "bad"); }
  }

  async function loadLeads() {
    const url = `/api/leads?status=${encodeURIComponent(state.status)}&q=${encodeURIComponent(state.q)}&scope=${state.scope}&limit=400`;
    let rows = [];
    try { rows = await (await fetch(url)).json(); } catch (e) { toast("Failed to load leads", "bad"); }
    allRows = rows;
    for (const k in COL_FILTERS) COL_FILTERS[k] = null;   // fresh view -> reset column filters
    closeColFilter();
    renderLeadRows();
  }

  function passesColFilters(r) {
    return Object.entries(COL_FILTERS).every(([col, set]) => set === null || set.has(rowVal(r, col)));
  }

  function renderLeadRows() {
    const rows = allRows.filter(passesColFilters);
    const body = $("#leadsBody");
    const filtered = rows.length !== allRows.length;
    $("#tableCount").textContent = `${rows.length} lead${rows.length === 1 ? "" : "s"}` +
      (filtered ? ` of ${allRows.length}` : "");
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
    $$("th[data-col]").forEach(th => th.classList.toggle("filtered", COL_FILTERS[th.dataset.col] !== null));
  }

  /* ── per-column value filter (Excel-style dropdown on a header) ─────────── */
  let colPopup = null;
  function closeColFilter() {
    if (!colPopup) return;
    colPopup.remove(); colPopup = null;
    document.removeEventListener("mousedown", colDocDown, true);
  }
  function colDocDown(e) {
    if (colPopup && !colPopup.contains(e.target) && !e.target.closest("th[data-col]")) closeColFilter();
  }
  function openColFilter(col, th) {
    const reopen = colPopup && colPopup.dataset.col === col;
    closeColFilter();
    if (reopen) return;                       // clicking the same header again closes it

    const counts = new Map();
    allRows.forEach(r => { const v = rowVal(r, col); counts.set(v, (counts.get(v) || 0) + 1); });
    const values = [...counts.keys()].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
    const active = COL_FILTERS[col];

    const pop = document.createElement("div");
    pop.className = "colfilter"; pop.dataset.col = col;
    pop.innerHTML = `
      <div class="cf-search"><input type="text" placeholder="Filter ${values.length} values…" autocomplete="off"></div>
      <div class="cf-actions"><button data-act="all">Select all</button><button data-act="none">Clear</button></div>
      <div class="cf-list">${values.map(v => `
        <label class="cf-item"><input type="checkbox" value="${esc(v)}" ${active === null || active.has(v) ? "checked" : ""}>
          <span class="cf-v" title="${esc(v)}">${esc(v)}</span><span class="cf-n">${counts.get(v)}</span></label>`).join("")}</div>`;
    document.body.appendChild(pop);

    const rect = th.getBoundingClientRect();
    pop.style.top = `${Math.round(rect.bottom + 4)}px`;
    pop.style.left = `${Math.round(Math.min(rect.left, window.innerWidth - pop.offsetWidth - 12))}px`;
    colPopup = pop;

    const apply = () => {
      const boxes = $$(".cf-item input", pop);
      const checked = boxes.filter(b => b.checked).map(b => b.value);
      COL_FILTERS[col] = (checked.length === values.length) ? null : new Set(checked);
      renderLeadRows();
    };
    $$(".cf-item input", pop).forEach(b => b.onchange = apply);
    pop.querySelector('[data-act="all"]').onclick = () => { $$(".cf-item input", pop).forEach(b => b.checked = true); apply(); };
    pop.querySelector('[data-act="none"]').onclick = () => { $$(".cf-item input", pop).forEach(b => b.checked = false); apply(); };
    const search = pop.querySelector(".cf-search input");
    search.oninput = () => {
      const q = search.value.toLowerCase();
      $$(".cf-item", pop).forEach(it => it.classList.toggle("hidden", !it.textContent.toLowerCase().includes(q)));
    };
    search.focus();
    document.addEventListener("mousedown", colDocDown, true);
    window.addEventListener("resize", closeColFilter, { once: true });
    const tw = document.querySelector(".tablewrap");
    if (tw) tw.addEventListener("scroll", closeColFilter, { once: true });
  }

  function refreshAll() { loadStats(); loadLeads(); }

  /* ── observability ─────────────────────────────────────────────────────── */
  const fmtMs = (ms) => ms == null ? "—" :
    ms >= 1000 ? (ms / 1000).toFixed(ms >= 10000 ? 0 : 1) + "s" : Math.round(ms) + "ms";
  const STAGE_SHORT = {
    lead_received: "Received", stage_dedup: "Dedup", stage0_load_image: "Load image",
    stage1_image_qc: "Image QC", stage2_ocr_classify: "OCR + model", stage3_extract: "Extract",
    stage4_verify: "Verify", lead_closed: "Closed",
  };

  async function loadObservability() {
    let d;
    try { d = await (await fetch("/api/observability?scope=" + state.scope)).json(); }
    catch (e) { toast("Failed to load observability", "bad"); return; }
    renderObsKpis(d);
    renderLatChart(d.latency_series || []);
    renderLatNote(d.latency || {}, d.ocr_cache || {});
    renderTputChart(d.throughput_series || []);
    renderHBar("stage", "stageChart",
      (d.stage_timings || []).filter(r => r.avg_ms != null).slice(0, 8)
        .map(r => STAGE_SHORT[r.stage] || r.stage),
      (d.stage_timings || []).filter(r => r.avg_ms != null).slice(0, 8)
        .map(r => Math.round(r.avg_ms || 0)), cssVar("--accent"));
    renderHBar("reason", "reasonChart",
      (d.unverified_reasons || []).slice(0, 8).map(r => r.field),
      (d.unverified_reasons || []).slice(0, 8).map(r => r.n), cssVar("--bad"),
      (field) => openDetail("unverified_field", { field }));
    renderAccuracy(d.accuracy || {}, d.accuracy_series || [], d.confusion || []);
    renderFillRates(d.fillrates || {}, d.fillrate_series || []);
    renderQueue(d.queue || {}, d.errors || {}, d.ocr_cache || {});
    renderFunnel(d.lender_funnel || []);
    renderCoverage(d.config_coverage || []);
  }

  const pctStr = (v) => v == null ? "—" : v + "%";
  function renderAccuracy(a, series, confusion) {
    const reviewed = a.reviewed || 0;
    $("#accTag").textContent = reviewed
      ? `${reviewed} REVIEWED · ${pctStr(a.coverage_pct)} OF ${a.total_results || 0}`
      : "NO REVIEWS YET";
    if (!reviewed) {
      $("#accKpis").innerHTML = `<p class="lede">No verdicts reviewed yet. Open any lead and use the <b>Review</b> panel to confirm or correct its verdict — accuracy, precision and drift populate here as reviews come in. <span class="muted">This is the system's only measure of whether it's actually right.</span></p>`;
      if (charts.acc) { charts.acc.destroy(); charts.acc = null; }
      $("#confusionBox").innerHTML = `<p class="muted">Nothing overturned yet — review some leads to surface where the system is wrong.</p>`;
      return;
    }
    // headline correctness KPIs — verified precision is the star metric
    const cards = [
      ["ok", "Verified precision", pctStr(a.verified_precision_pct), null,
        `${a.verified_overturned || 0}/${a.verified_reviewed || 0} overturned`],
      ["tot", "Agreement", pctStr(a.agreement_pct), null,
        `${a.confirmed || 0}/${reviewed} confirmed`],
      ["warn", "Unverified over-flag", pctStr(a.unverified_overturn_pct), null,
        `${a.unverified_overturned || 0}/${a.unverified_reviewed || 0} were fine`],
      ["bad", "Overturned", a.overturned || 0, "overturned", "click to inspect"],
    ];
    $("#accKpis").innerHTML = `<div class="acc-kpis">` + cards.map(([c, l, v, det, sub]) =>
      `<div class="acc-kpi ${c}${det ? " clickable" : ""}"${det ? ` data-detail="${det}"` : ""}>
        <div class="acc-n">${esc(String(v))}</div><div class="acc-l">${l}</div>
        <div class="acc-sub">${esc(sub)}</div></div>`).join("") + `</div>`;
    $$("#accKpis [data-detail]").forEach(el => el.onclick = () => openDetail(el.dataset.detail));

    // drift line: agreement % per day
    if (window.Chart) {
      if (charts.acc) charts.acc.destroy();
      charts.acc = new Chart($("#accChart"), {
        type: "line",
        data: { labels: series.map(r => r.t), datasets: [{
          label: "agreement %", data: series.map(r => r.agreement_pct),
          borderColor: cssVar("--ok"), backgroundColor: "transparent", tension: 0.3 }] },
        options: {
          plugins: { legend: { display: false } },
          elements: { point: { radius: 2 }, line: { borderWidth: 2 } },
          scales: {
            x: { grid: { display: false }, border: { color: cssVar("--line-strong") }, ticks: { ...AX(), maxTicksLimit: 8 } },
            y: { grid: { color: cssVar("--line") }, border: { display: false }, ticks: { ...AX(), callback: v => v + "%" }, min: 0, max: 100 },
          },
        },
      });
    }

    // confusion: system verdict -> corrected verdict, for overturned cases
    if (!confusion.length) {
      $("#confusionBox").innerHTML = `<p class="muted">No overturned verdicts — everything reviewed so far was confirmed. ✓</p>`;
    } else {
      const max = Math.max(...confusion.map(r => r.n));
      $("#confusionBox").innerHTML =
        `<p class="lede" style="margin-bottom:14px">Every reviewer correction, most common first. <b>verified → unverified</b> is a false positive the system let through; <b>unverified → verified</b> is over-flagging.</p>` +
        confusion.map(r => {
          const w = Math.round((r.n / max) * 100);
          const bad = r.system_status === "verified";
          return `<div class="conf-row">
            <span class="conf-from ${bad ? "bad" : "warn"}">${esc(r.system_status)}</span>
            <span class="conf-arrow">→</span>
            <span class="conf-to">${esc(r.corrected_status)}</span>
            <div class="conf-track"><div class="conf-bar ${bad ? "bad" : "warn"}" style="width:${w}%"></div></div>
            <span class="conf-n mono">${r.n}</span></div>`;
        }).join("");
    }
  }

  function renderLatNote(lat, cache) {
    const el = $("#latNote"); if (!el) return;
    const calls = lat.n || 0;
    if (calls) {
      el.textContent = `${calls} model call${calls === 1 ? "" : "s"} measured` +
        (cache.served_from_cache ? ` · ${cache.served_from_cache} more served from cache (skipped the model)` : "");
    } else if (cache.stage2_total) {
      el.innerHTML = `No model calls to measure — all <b>${cache.stage2_total}</b> lead${cache.stage2_total === 1 ? " was" : "s were"} served from the extraction cache. ` +
        `Percentiles need real model calls: clear the OCR cache (or run new images) to populate them.`;
    } else {
      el.textContent = "Run a batch to populate latency.";
    }
  }

  function renderObsKpis(d) {
    const lat = d.latency || {}, q = d.queue || {};
    const tail = (d.throughput_series || []).slice(-5);
    const rate = tail.length ? Math.round(tail.reduce((a, b) => a + (b.n || 0), 0) / tail.length) : 0;
    const cards = [
      ["tot", "Model p50", fmtMs(lat.p50)],
      ["warn", "Model p95", fmtMs(lat.p95)],
      ["bad", "Model p99", fmtMs(lat.p99)],
      ["ok", "Leads / min", rate],
      ["gray", "Retried", q.retried || 0],
      ["bad", "Failed", q.failed || 0, "failed_jobs"],
    ];
    $("#obsKpis").innerHTML = cards.map(([c, l, v, det]) =>
      `<div class="stat ${c}${det ? " clickable" : ""}"${det ? ` data-detail="${det}"` : ""}><div class="n">${esc(v)}</div><div class="l">${l}</div></div>`).join("");
    $$("#obsKpis [data-detail]").forEach(el => el.onclick = () => openDetail(el.dataset.detail));
  }

  const AX = (mono = true) => ({ color: cssVar("--ink-faint"),
    font: { family: mono ? "JetBrains Mono" : "Hanken Grotesk", size: mono ? 10 : 12 } });

  function renderLatChart(series) {
    if (!window.Chart) return;
    if (charts.lat) charts.lat.destroy();
    charts.lat = new Chart($("#latChart"), {
      type: "line",
      data: {
        labels: series.map(r => r.t),
        datasets: [
          { label: "p50", data: series.map(r => Math.round(r.p50 || 0)), borderColor: cssVar("--accent"), backgroundColor: "transparent" },
          { label: "p95", data: series.map(r => Math.round(r.p95 || 0)), borderColor: cssVar("--bad"), backgroundColor: "transparent" },
        ],
      },
      options: {
        plugins: { legend: { display: true, labels: { color: cssVar("--ink-soft"), boxWidth: 10, font: { size: 11 } } } },
        elements: { point: { radius: 0 }, line: { tension: 0.3, borderWidth: 2 } },
        scales: {
          x: { grid: { display: false }, border: { color: cssVar("--line-strong") }, ticks: { ...AX(), maxTicksLimit: 8 } },
          y: { grid: { color: cssVar("--line") }, border: { display: false }, ticks: AX(), beginAtZero: true },
        },
      },
    });
  }

  function renderTputChart(series) {
    if (!window.Chart) return;
    if (charts.tput) charts.tput.destroy();
    charts.tput = new Chart($("#tputChart"), {
      type: "bar",
      data: { labels: series.map(r => r.t), datasets: [{ data: series.map(r => r.n || 0), backgroundColor: cssVar("--ok"), maxBarThickness: 14 }] },
      options: {
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { display: false }, border: { color: cssVar("--line-strong") }, ticks: { ...AX(), maxTicksLimit: 8 } },
          y: { grid: { color: cssVar("--line") }, border: { display: false }, ticks: { ...AX(), precision: 0 }, beginAtZero: true },
        },
      },
    });
  }

  function renderHBar(key, canvasId, labels, data, color, onBarClick) {
    if (!window.Chart) return;
    if (charts[key]) charts[key].destroy();
    charts[key] = new Chart($("#" + canvasId), {
      type: "bar",
      data: { labels, datasets: [{ data, backgroundColor: color, barThickness: 15 }] },
      options: {
        indexAxis: "y", plugins: { legend: { display: false } },
        onClick: onBarClick ? (e, els) => { if (els.length) onBarClick(labels[els[0].index]); } : undefined,
        onHover: onBarClick ? (e, els) => { e.native.target.style.cursor = els.length ? "pointer" : "default"; } : undefined,
        scales: {
          x: { grid: { color: cssVar("--line") }, border: { display: false }, ticks: AX(), beginAtZero: true },
          y: { grid: { display: false }, border: { color: cssVar("--line-strong") }, ticks: AX(false) },
        },
      },
    });
  }

  function sparkline(values, color, w = 66, h = 18) {
    const v = (values || []).filter(x => x != null);
    if (v.length < 2) return `<span class="spark-empty">—</span>`;
    const min = Math.min(...v), max = Math.max(...v), rng = (max - min) || 1;
    const pts = v.map((x, i) => `${(i / (v.length - 1) * w).toFixed(1)},${(h - ((x - min) / rng) * (h - 2) - 1).toFixed(1)}`).join(" ");
    const trend = v[v.length - 1] - v[0];
    const tc = trend < -2 ? "var(--bad)" : trend > 2 ? "var(--ok)" : "var(--ink-faint)";
    return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5"/></svg>`
      + `<span class="spark-d" style="color:${tc}">${trend > 0 ? "▲" : trend < 0 ? "▼" : "→"}${Math.abs(Math.round(trend))}</span>`;
  }

  function renderFillRates(f, series) {
    const n = f.n || 0;
    if (!n) { $("#fillRates").innerHTML = `<p class="muted" style="padding:6px 0">No extracted leads yet.</p>`; return; }
    const fields = [["amount", f.amount], ["date", f.date], ["receiver", f.receiver], ["lan", f.lan]];
    const seriesFor = (k) => (series || []).map(r => r[k]);
    const multiDay = (series || []).length >= 2;
    $("#fillRates").innerHTML =
      `<p class="lede" style="margin-bottom:14px">Of ${n.toLocaleString()} leads that reached extraction — how often each field was read.${multiDay ? " Sparkline shows the per-day trend — a falling line is model/format drift." : " A drop over time signals model/format drift."} <span class="muted">Click a row to see the leads it was missing on.</span></p>` +
      fields.map(([k, v]) => {
        const pct = Math.round((v || 0) / n * 100);
        const spark = multiDay ? `<span class="fill-spark">${sparkline(seriesFor(k), cssVar("--accent"))}</span>` : "";
        return `<div class="fill-row clickable" data-field="${k}"><span class="fill-k">${k}</span>
          <div class="fill-track"><div class="fill-bar" style="width:${pct}%"></div></div>
          ${spark}<span class="fill-v mono">${pct}%</span></div>`;
      }).join("");
    $$("#fillRates [data-field]").forEach(el => el.onclick = () => openDetail("missing_field", { field: el.dataset.field }));
  }

  function renderQueue(q, err, cache) {
    const cells = [["done", q.done || 0], ["pending", q.pending || 0], ["in progress", q.in_progress || 0],
                   ["failed", q.failed || 0, "failed_jobs"], ["retried", q.retried || 0],
                   ["model errors", err.model_errors || 0, "model_errors"]];
    let html = `<div class="obs-kv">` + cells.map(([k, v, det]) =>
      `<div class="obs-kv-cell${det ? " clickable" : ""}"${det ? ` data-detail="${det}"` : ""}><div class="obs-kv-n">${v}</div><div class="obs-kv-l">${k}</div></div>`).join("") + `</div>`;
    if (cache && (cache.stage2_total || cache.entries)) {
      html += `<div class="cache-line"><span class="tag">EXTRACTION CACHE</span>` +
        `<span><b>${cache.hit_rate_pct == null ? "—" : cache.hit_rate_pct + "%"}</b> hit-rate</span>` +
        `<span class="muted">${cache.served_from_cache || 0} served from ${cache.entries || 0} cached images · saved model calls</span></div>`;
    }
    const fj = err.failed_jobs || [];
    if (fj.length) {
      html += `<div style="margin-top:16px"><div class="tag" style="margin-bottom:10px">TOP FAILURES</div>` +
        fj.map(f => `<div class="cov-item"><span class="mono" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.err)}</span><span class="count">${f.n}</span></div>`).join("") + `</div>`;
    }
    $("#queueBox").innerHTML = html;
    $$("#queueBox [data-detail]").forEach(el => el.onclick = () => openDetail(el.dataset.detail));
  }

  function renderFunnel(rows) {
    $("#funnelCount").textContent = `${rows.length} lender${rows.length === 1 ? "" : "s"}`;
    $("#funnelEmpty").classList.toggle("hidden", rows.length > 0);
    $("#funnelBody").innerHTML = rows.map(r => {
      const rate = r.total ? Math.round(r.verified / r.total * 100) : 0;
      return `<tr>
        <td>${esc(r.lender)}</td>
        <td class="mono">${r.total}</td>
        <td class="mono" style="color:var(--ok)">${r.verified}</td>
        <td class="mono" style="color:var(--bad)">${r.unverified}</td>
        <td class="mono" style="color:var(--dup)">${r.duplicate}</td>
        <td class="mono" style="color:var(--neutral)">${r.non_document}</td>
        <td class="mono">${rate}%</td></tr>`;
    }).join("");
  }

  function renderCoverage(rows) {
    $("#coverageTag").textContent = rows.length ? `${rows.length} NEED CONFIG` : "ALL CONFIGURED";
    if (!rows.length) {
      $("#coverageBox").innerHTML = `<p class="muted">Every lender in the data has an explicit rule and receiver allowlist.</p>`;
      return;
    }
    $("#coverageBox").innerHTML =
      `<p class="lede" style="margin-bottom:12px">These lenders appear in the data but lack explicit config. They still work (defaults + receiver fallback), but adding config tightens matching. <span class="muted">Click a lender to see its leads.</span></p>` +
      rows.map(r => `<div class="cov-item clickable" data-lender="${esc(r.lender)}">
        <span style="flex:1">${esc(r.lender)} <span class="count">· ${r.n}</span></span>
        ${r.explicit_rule ? "" : `<span class="fchip bad">no rule</span>`}
        ${r.receiver_list ? "" : `<span class="fchip bad">no receiver list</span>`}
      </div>`).join("");
    $$("#coverageBox [data-lender]").forEach(el => el.onclick = () => openDetail("lender", { lender: el.dataset.lender }));
  }

  /* ── drill-down modal (a clicked metric -> the underlying leads) ────────── */
  function closeObsModal() {
    $("#obsModal").classList.remove("open"); $("#obsScrim").classList.remove("open");
  }
  async function openDetail(kind, params = {}) {
    const qs = new URLSearchParams({ kind, scope: state.scope, ...params }).toString();
    $("#obsModalTitle").textContent = "Loading…";
    $("#obsModalBody").innerHTML = `<div class="empty">Loading…</div>`;
    $("#obsModal").classList.add("open"); $("#obsScrim").classList.add("open");
    let d;
    try { d = await (await fetch("/api/observability/detail?" + qs)).json(); }
    catch (e) { $("#obsModalBody").innerHTML = `<div class="empty">Failed to load detail.</div>`; return; }
    const cols = d.columns || [], rows = d.rows || [];
    $("#obsModalTitle").textContent = `${d.title || "Detail"} · ${rows.length}`;
    if (!rows.length) { $("#obsModalBody").innerHTML = `<div class="empty">No matching leads.</div>`; return; }
    $("#obsModalBody").innerHTML = `<table><thead><tr>${cols.map(c => `<th>${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${rows.map(r => `<tr data-id="${esc(r.lead_id || "")}"${r.lead_id ? ' class="clickable"' : ""}>
        ${cols.map(c => `<td class="${c === "lead_id" ? "lead-id mono" : (c === "error" ? "cell-outcome" : "")}">${esc(r[c] ?? "")}</td>`).join("")}
      </tr>`).join("")}</tbody></table>`;
    $$("#obsModalBody tr[data-id]").forEach(tr => {
      if (tr.dataset.id) tr.onclick = () => { closeObsModal(); openLead(tr.dataset.id); };
    });
  }

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

  /* ── human-review loop ─────────────────────────────────────────────────── */
  const STATUS_OPTS = ["verified", "unverified", "non_document", "duplicate"];
  const savedReviewer = () => { try { return localStorage.getItem("pv_reviewer") || ""; } catch (e) { return ""; } };

  function reviewHtml(status, review) {
    let state = "";
    if (review) {
      const who = esc(review.reviewer || "—"), when = fmtTime(review.ts) || "";
      if (review.decision === "overturned") {
        state = `<div class="rv-state overturned">
          <span class="rv-badge over">OVERTURNED</span>
          system said <b>${esc(review.system_status)}</b> → reviewer set
          <b>${esc(review.corrected_status)}</b>
          <div class="rv-meta">by ${who} · ${when}${review.note ? ` · “${esc(review.note)}”` : ""}</div></div>`;
      } else {
        state = `<div class="rv-state confirmed">
          <span class="rv-badge ok">CONFIRMED</span> reviewer agreed with <b>${esc(review.system_status)}</b>
          <div class="rv-meta">by ${who} · ${when}${review.note ? ` · “${esc(review.note)}”` : ""}</div></div>`;
      }
    }
    const opts = STATUS_OPTS.filter(s => s !== status)
      .map(s => `<option value="${s}">${s}</option>`).join("");
    return `<div class="d-section rv-section">
      <h4>Review <span class="n">confirm or correct the verdict</span></h4>
      ${state}
      <div class="rv-form">
        <input id="rvReviewer" class="rv-input" placeholder="your name / email" value="${esc(savedReviewer())}" autocomplete="off">
        <input id="rvNote" class="rv-input" placeholder="note (optional)" autocomplete="off">
        <div class="rv-actions">
          <button id="rvConfirm" class="rv-btn ok">✓ Confirm “${esc(status)}”</button>
          <span class="rv-or">or correct to</span>
          <select id="rvStatus" class="rv-input rv-sel">${opts}</select>
          <button id="rvOverturn" class="rv-btn bad">Overturn</button>
        </div>
      </div></div>`;
  }

  async function submitReview(leadId, status, decision) {
    const reviewer = ($("#rvReviewer").value || "").trim();
    const note = ($("#rvNote").value || "").trim();
    try { if (reviewer) localStorage.setItem("pv_reviewer", reviewer); } catch (e) {}
    const body = { decision, reviewer, note };
    if (decision === "overturned") body.corrected_status = $("#rvStatus").value;
    try {
      const r = await fetch(`/api/lead/${encodeURIComponent(leadId)}/review`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!r.ok) { const e = await r.json().catch(() => ({})); toast(e.error || "Review failed", "bad"); return; }
      toast(decision === "overturned" ? "Verdict overturned" : "Verdict confirmed", "ok");
      openLead(leadId);            // refresh the drawer to show the new review state
    } catch (e) { toast("Review failed", "bad"); }
  }

  function wireReview(leadId, status) {
    const c = $("#rvConfirm"), o = $("#rvOverturn");
    if (c) c.onclick = () => submitReview(leadId, status, "confirmed");
    if (o) o.onclick = () => submitReview(leadId, status, "overturned");
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
      ${status !== "unknown" ? reviewHtml(status, j.review) : ""}
      ${lifecycleHtml(j.journey)}`;
    if (status !== "unknown") wireReview(j.lead_id, status);
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
    const isTest = state.scope === "test";      // uploads follow the active workspace
    const fd = new FormData();
    fd.append("file", file);
    fd.append("image_col", $("#imageCol").value || "payment_document");
    if ($("#idCol").value) fd.append("id_col", $("#idCol").value);
    if ($("#imageRoot").value) fd.append("image_root", $("#imageRoot").value);
    if (isTest) fd.append("test", "on");

    const btn = $("#runCsv"); btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Enqueuing…`;
    let r;
    try { r = await (await fetch("/api/enqueue", { method: "POST", body: fd })).json(); }
    catch (e) { toast("Upload failed", "bad"); btn.disabled = false; btn.textContent = "Run batch"; return; }
    if (r.error) { toast(esc(r.error), "bad"); btn.disabled = false; btn.textContent = "Run batch"; return; }

    const rst = $("#runScopeTag"); if (rst) rst.textContent = isTest ? "TEST WORKSPACE" : "LIVE";
    // loud warning when nothing (or everything) was skipped as "already done" — the
    // classic footgun of re-uploading with matching lead IDs.
    if (r.enqueued === 0 && r.total > 0) {
      toast(`⚠ 0 of ${r.total} enqueued — all rows already exist (same lead IDs). Nothing was processed. Use a unique Lead-ID column or new IDs to re-run.`, "bad", 9000);
      $("#runPanel").classList.add("hidden");
      btn.disabled = false; btn.textContent = "Run batch";
      return;
    }
    if (r.skipped > 0) toast(`⚠ ${r.skipped} of ${r.total} row${r.total === 1 ? "" : "s"} skipped (already processed)`, "warn", 7000);
    $("#runPanel").classList.remove("hidden"); $("#doneBanner").classList.add("hidden");
    $("#liveList").innerHTML = ""; $("#progBar").style.width = "0%"; miniCounts({});
    $("#dlBtn").href = "/download/" + encodeURIComponent(r.batch_id);
    toast(`${isTest ? "TEST · " : ""}Enqueued ${r.enqueued} lead${r.enqueued === 1 ? "" : "s"}`, "ok");
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

  /* ── single image (ephemeral test — nothing saved) ─────────────────────── */
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
      renderSingleResult(r);
      toast(`Result: ${badge(r.verification_status)}`, r.verification_status === "verified" ? "ok" : "");
    } catch (e) { toast("Test failed: " + esc(e.message || e), "bad"); }
    btn.disabled = false; btn.textContent = "Run test";
  }

  function renderSingleResult(r) {
    const box = $("#imgResult");
    const status = r.verification_status || "unknown";
    box.innerHTML = `
      <div class="sr-head">${badge(status)}<span class="sr-method">${esc(r.payment_method || "—")}</span>
        <span class="sr-eph">ephemeral · not saved</span></div>
      <div class="d-section"><h4>Verification <span class="n">expected ↔ document</span></h4>
        ${comparisonHtml(r.csv_row, r.extracted, verifyOutcome(r.outcome))}
        ${extraExtractedHtml(r.extracted || {})}</div>
      ${outcomeHtml(status, r.outcome)}`;
    box.classList.remove("hidden");
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
    $("#refreshBtn").onclick = () =>
      $("#view-observability").classList.contains("hidden") ? refreshAll() : loadObservability();
    $$("#wsSwitch .ws-opt").forEach(b => b.onclick = () => setScope(b.dataset.scope));
    $("#clearWsBtn").onclick = clearWorkspace;
    $("#drawerClose").onclick = closeDrawer;
    $("#scrim").onclick = closeDrawer;
    $("#obsModalClose").onclick = closeObsModal;
    $("#obsScrim").onclick = closeObsModal;
    document.addEventListener("keydown", e => { if (e.key === "Escape") { closeColFilter(); closeObsModal(); closeDrawer(); } });

    $$("#statusChips .chip").forEach(c => c.onclick = () => {
      $$("#statusChips .chip").forEach(x => x.classList.remove("active"));
      c.classList.add("active"); state.status = c.dataset.status; loadLeads();
    });
    $$("th[data-col]").forEach(th => th.onclick = () => openColFilter(th.dataset.col, th));
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
