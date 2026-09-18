/* SARFAESI Recovery Console — Single-Page Application.
   Strictly adheres to the Swiss typographic design system and interaction patterns of Payment Verification. */
(() => {
  "use strict";

  const _fetch = window.fetch.bind(window);
  window.fetch = (...a) => _fetch(...a).then(r => {
    if (r.status === 401) {
      window.location.href = "/login?next=" + encodeURIComponent(location.pathname);
      throw new Error("unauthorized");
    }
    return r;
  });

  const CFG = window.__CFG__ || { model: {}, deepLead: "", wsId: "legal", wsName: "SARFAESI Recovery Console" };
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim() || "#888";

  const palette = () => ({
    completed: cssVar("--ch-ok"),
    partial: cssVar("--ch-warn"),
    missing_documents: cssVar("--ch-warn"),
    failed: cssVar("--ch-bad"),
    processing: cssVar("--accent"),
    pending: cssVar("--ch-neutral")
  });

  const fmtTime = (t) => (t || "").replace("T", " ").slice(0, 19);
  const fmtINR = (v) => {
    if (v == null || v === "" || isNaN(v)) return "—";
    return "₹ " + Number(v).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  };
  const debounce = (fn, ms = 280) => {
    let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); };
  };

  let state = { status: "all", q: "", scope: null, batchId: null, script: "all", missing: null };
  let charts = {};
  let allRows = [];
  const COL_FILTERS = {};        // filled from the columns the extraction actually produced
  const rowVal = (r, col) => String(r[col] ?? "") || "—";

  /* ── toast ─────────────────────────────── */
  function toast(msg, kind = "", ms = 3200) {
    const t = document.createElement("div");
    t.className = "toast " + kind;
    t.innerHTML = msg;
    const cont = $("#toasts") || document.body;
    cont.appendChild(t);
    setTimeout(() => { t.style.opacity = "0"; t.style.transform = "translateX(20px)"; t.style.transition = ".3s"; }, ms);
    setTimeout(() => t.remove(), ms + 400);
  }

  // The lead row is the live state; a result row can be left over from an earlier run of the
  // same dossier. So the lead's own status always wins ('draft' is just "queued").
  const liveStatus = (leadStatus, resultStatus) => {
    const s = leadStatus || resultStatus || "pending";
    return s === "draft" ? "pending" : s;
  };

  const badge = (s) => {
    const st = String(s || "pending").toLowerCase();
    const map = {
      completed: "b-verified",
      partial: "b-warn",
      missing_documents: "b-warn",
      failed: "b-unverified",
      processing: "b-manual_review",
      pending: "b-manual_review",
      paused: "b-non_document"
    };
    return `<span class="badge ${map[st] || "b-non_document"}">${esc(s || "pending")}</span>`;
  };

  const propBadge = (s) => {
    if (!s || s === "pending") return `<span class="badge b-non_document">Pending</span>`;
    const str = String(s).toLowerCase();
    if (str.includes("cross") || str.includes("tier1")) return `<span class="badge b-verified">Cross-verified</span>`;
    if (str.includes("single") || str.includes("tier2")) return `<span class="badge b-verified">Single source</span>`;
    if (str.includes("n/a") || str.includes("not_found") || str.includes("tier3")) return `<span class="badge b-unverified">${esc(s)}</span>`;
    return `<span class="badge b-warn">${esc(s)}</span>`;
  };

  /* ── canvas fallback helper ────────────── */
  function emptyCanvas(key, canvasId, msg) {
    if (charts[key]) { charts[key].destroy(); delete charts[key]; }
    const cvs = document.getElementById(canvasId);
    if (!cvs) return;
    const ctx = cvs.getContext("2d");
    ctx.clearRect(0, 0, cvs.width, cvs.height);
    ctx.save();
    ctx.font = '500 12.5px "Space Grotesk", sans-serif';
    ctx.fillStyle = cssVar("--ink-faint");
    ctx.textAlign = "center";
    ctx.fillText(msg, cvs.width / 2, cvs.height / 2);
    ctx.restore();
  }

  /* ── view switching ────────────────────── */
  const TITLES = {
    dashboard: ["Overview", "Dashboard", "SARFAESI Section 13(2) Loan Document Dossier Processing"],
    runs: ["History", "Runs History", "Review previous dossier uploads and extraction prompts"],
    observability: ["Operations", "Observability", "Tokens, model time, coverage and where the extracted values came from"],
  };

  function switchView(name) {
    $$(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === name));
    $$(".view").forEach(v => v.classList.toggle("hidden", v.id !== "view-" + name));
    const t = TITLES[name] || TITLES.dashboard;
    $("#pageEyebrow").textContent = t[0];
    $("#pageTitle").textContent = t[1];
    $("#pageSub").textContent = t[2];
    
    // Clear batchId filter if not on dashboard
    if (name !== "dashboard") {
      state.batchId = null;
      updateBatchFilterUI();
    }
    // the runs view owns sideways gestures and hides the dashboard-only header tools
    document.documentElement.classList.toggle("runs-mode", name === "runs");
    document.documentElement.classList.toggle("obs-mode", name === "observability");
    if (name !== "runs") stopRunsPoll();

    if (name === "dashboard") refreshAll();
    if (name === "runs") loadRuns();
    if (name === "observability") loadObservability();
  }

  /* ── theme ─────────────────────────────── */
  function setTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    localStorage.setItem("pv-theme", t);
    const lbl = $("#themeLabel");
    if (lbl) lbl.textContent = t === "dark" ? "Light" : "Dark";
    Object.values(charts).forEach(c => c.destroy());
    charts = {};
    if (!$("#view-dashboard").classList.contains("hidden")) loadStats();
    if (!$("#view-observability").classList.contains("hidden")) loadObservability();
  }

  /* ── stat cards + charts ───────────────── */
  let lastStatCounts = {};
  function renderStats(counts) {
    const total = counts.total || 0;
    const cards = [
      ["tot", "Total Dossiers", total, "total"],
      ["ok", "Completed", counts.completed || 0, "completed"],
      ["warn", "Partial/Missing", (counts.partial || 0) + (counts.missing_documents || 0), "partial"],
      ["bad", "Failed", counts.failed || 0, "failed"],
      ["gray", "Dossier Documents", counts.total_documents || 0, "total_documents"],
    ];
    
    const container = $("#statCards");
    if (!container.firstElementChild) {
      container.innerHTML = cards.map(([c, l, n, k]) => {
        const pct = total ? Math.round(n / total * 100) : 0;
        const showPct = (c !== "tot" && c !== "gray") ? `<div class="pct">${pct}%</div>` : "";
        return `<div class="stat ${c}" id="stat-${k}">${showPct}<div class="n" data-n="${n}">${n.toLocaleString()}</div><div class="l">${l}</div></div>`;
      }).join("");
      $$("#statCards .n").forEach(e => animateNum(e, +e.dataset.n, 0));
    } else {
      cards.forEach(([c, l, n, k]) => {
        const pct = total ? Math.round(n / total * 100) : 0;
        const statDiv = $(`#stat-${k}`);
        if (statDiv) {
           const pctEl = statDiv.querySelector(".pct");
           if (pctEl) pctEl.textContent = `${pct}%`;
           const nEl = statDiv.querySelector(".n");
           const from = lastStatCounts[k] || 0;
           if (from !== n) {
             animateNum(nEl, n, from);
             nEl.dataset.n = n;
           }
        }
      });
    }
    
    cards.forEach(([c, l, n, k]) => { lastStatCounts[k] = n; });
  }

  function animateNum(elm, to, from = 0) {
    if (from === to) { elm.textContent = to.toLocaleString(); return; }
    const dur = Math.min(600, Math.abs(to - from) * 20 + 200), t0 = performance.now();
    const step = (now) => {
      const p = Math.min(1, (now - t0) / dur);
      elm.textContent = Math.round(from + (to - from) * (1 - Math.pow(1 - p, 3))).toLocaleString();
      if (p < 1) requestAnimationFrame(step);
      else elm.textContent = to.toLocaleString();
    };
    requestAnimationFrame(step);
  }

  function renderDonut(counts) {
    const C = palette();
    const total = counts.total || 0;
    $("#donutTotal").textContent = total.toLocaleString();
    const keys = ["completed", "partial", "missing_documents", "failed", "pending"];
    const data = keys.map(k => counts[k] || 0);

    $("#donutLegend").innerHTML = keys.map((k, i) =>
      `<span class="li"><span class="sw" style="background:${C[k]}"></span>${k} · <b>${data[i]}</b></span>`).join("");

    if (!window.Chart) return;
    const cvs = $("#donut");
    if (!cvs) return;

    if (charts.donut) {
      charts.donut.data.datasets[0].data = data.some(v => v > 0) ? data : [1];
      charts.donut.data.datasets[0].backgroundColor = data.some(v => v > 0) ? keys.map(k => C[k]) : [cssVar("--line")];
      charts.donut.update();
      return;
    }

    const hasData = data.some(v => v > 0);
    charts.donut = new Chart(cvs, {
      type: "doughnut",
      data: {
        labels: hasData ? keys : ["No data"],
        datasets: [{
          data: hasData ? data : [1],
          backgroundColor: hasData ? keys.map(k => C[k]) : [cssVar("--line")],
          borderWidth: 2,
          borderColor: cssVar("--surface"),
          hoverOffset: 4
        }]
      },
      options: { cutout: "70%", plugins: { legend: { display: false } }, animation: { animateRotate: true } }
    });
  }

  function renderDocTypes(types) {
    if (!window.Chart) return;
    if (!types || !types.length) {
      emptyCanvas("docTypes", "docTypeBar", "No dossier documents classified yet");
      return;
    }
    const top = types.slice(0, 8);
    const labels = top.map(m => m.document_type), data = top.map(m => m.n);
    if (charts.docTypes) {
      charts.docTypes.data.labels = labels;
      charts.docTypes.data.datasets[0].data = data;
      charts.docTypes.update();
      return;
    }
    const cvs = $("#docTypeBar");
    if (!cvs) return;
    charts.docTypes = new Chart(cvs, {
      type: "bar",
      data: { labels, datasets: [{ data, backgroundColor: cssVar("--accent"), barThickness: 15 }] },
      options: {
        indexAxis: "y", plugins: { legend: { display: false } },
        scales: {
          x: { grid: { color: cssVar("--line") }, border: { display: false }, ticks: { color: cssVar("--ink-faint"), font: { family: "JetBrains Mono", size: 11 } } },
          y: { grid: { display: false }, border: { color: cssVar("--line-strong") }, ticks: { color: cssVar("--ink-soft"), font: { family: "Hanken Grotesk", size: 12 } } }
        }
      }
    });
  }

  /* ── batch filtering UI ───────────────── */
  function updateBatchFilterUI() {
    let filterBanner = $("#batchFilterBanner");
    if (state.batchId) {
      if (!filterBanner) {
        filterBanner = document.createElement("div");
        filterBanner.id = "batchFilterBanner";
        filterBanner.className = "scope-banner";
        filterBanner.style.background = "var(--surface-2)";
        filterBanner.style.border = "1px solid var(--line)";
        filterBanner.style.color = "var(--ink)";
        filterBanner.style.marginBottom = "24px";
        $("#view-dashboard").insertBefore(filterBanner, $("#view-dashboard").firstChild);
      }
      filterBanner.innerHTML = `
        <span class="sb-text">Dashboard is filtered to a specific Run <b>(${state.batchId.substring(0,8)})</b></span>
        <button class="btn line small" id="clearBatchFilterBtn">Clear filter</button>
      `;
      $("#clearBatchFilterBtn").onclick = () => {
        state.batchId = null;
        updateBatchFilterUI();
        refreshAll();
      };
      filterBanner.classList.remove("hidden");
    } else {
      if (filterBanner) filterBanner.classList.add("hidden");
    }
  }

  /* ── leads table ───────────────────────── */
  async function loadStats() {
    try {
      const bq = state.batchId ? `&batch_id=${state.batchId}` : "";
      const resp = await fetch("/api/legal/stats?scope=" + state.scope + bq);
      if (!resp.ok) return;
      const s = await resp.json();
      renderStats(s.counts || {});
      renderStatusCounts(s.counts || {});
      renderDonut(s.counts || {});
      renderDocTypes(s.document_types || []);
      if (s.model) {
        $("#mcModel").textContent = s.model.model || "Medha";
        $("#mcUrl").textContent = (s.model.url || "").replace(/^https?:\/\//, "").split("/")[0] || "—";
      }
    } catch (e) {
      // transient poll error ignored
    }
  }

  function setScope(scope) {
    if (state.scope === scope) return;
    const isInitialLoad = state.scope === null;
    state.scope = scope;
    localStorage.setItem("legal-scope", scope);
    document.body.classList.toggle("scope-test", scope === "test");
    $("#scopeBanner").classList.toggle("hidden", scope !== "test");
    $$("#wsSwitch .ws-opt").forEach(b => b.classList.toggle("active", b.dataset.scope === scope));
    
    // Always clear run filter when switching scopes
    state.batchId = null;
    updateBatchFilterUI();
    
    refreshAll();
    if (!$("#view-observability").classList.contains("hidden")) loadObservability();
    if (!$("#view-runs").classList.contains("hidden")) loadRuns();
    if (scope === "test" && !isInitialLoad) toast("Switched to Test Workspace — temporary sandbox data", "");
  }

  async function clearWorkspace() {
    try {
      const r = await (await fetch("/api/legal/clear_test", { method: "POST" })).json();
      toast(`Test workspace cleared — ${r.cleared || 0} lead(s) removed`, "ok");
      
      state.batchId = null;
      updateBatchFilterUI();
      
      refreshAll();
      if (!$("#view-observability").classList.contains("hidden")) loadObservability();
      if (!$("#view-runs").classList.contains("hidden")) loadRuns();
    } catch (e) { toast("Clear failed", "bad"); }
  }

  let dynamicColumns = [];

  function formatColName(c) {
    const customMap = {
      borrower_name: "Borrower Name",
      borrower_address: "Borrower Address",
      co_borrower_1_name: "Co-Borrower 1 Name",
      co_borrower_1_address: "Co-Borrower 1 Address",
      co_borrower_2_name: "Co-Borrower 2 Name",
      co_borrower_2_address: "Co-Borrower 2 Address",
      co_borrower_3_name: "Co-Borrower 3 Name",
      co_borrower_3_address: "Co-Borrower 3 Address",
      applicant_name: "Borrower / Applicant",
      applicant_address: "Applicant Address",
      account_no_lan: "Account LAN",
      sanction_amount: "Sanction Amount",
      tos: "Total Outstanding (TOS)",
    };
    if (customMap[c]) return customMap[c];
    const co = c.match(/^co_borrower_(\d+)_(name|address)$/);   // any number of co-borrowers
    if (co) return `Co-Borrower ${co[1]} ${co[2] === "name" ? "Name" : "Address"}`;
    return c.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
  }

  async function loadLeads() {
    const bq = state.batchId ? `&batch_id=${state.batchId}` : "";
    const url = `/api/legal/leads?status=${encodeURIComponent(state.status)}&q=${encodeURIComponent(state.q)}&scope=${state.scope}&limit=400${bq}`;
    try {
      const resp = await fetch(url);
      if (resp.ok) {
        const data = await resp.json();
        allRows = Array.isArray(data) ? data : [];
      } else {
        toast(`Failed to load leads (HTTP ${resp.status})`, "bad");
        allRows = [];
      }
    } catch (e) {
      toast("Failed to load leads", "bad");
      allRows = [];
    }
    
    // Determine dynamic columns
    const excludeKeys = new Set([
      "lead_id", "batch_id", "folder_name", "folder_path", "dossier_type",
      "total_documents", "processed_documents", "failed_documents", "status",
      "processing_status", "is_test", "extraction_prompt", "created_at",
      "updated_at", "account_lan", "lead_name", "telemetry", "_raw_ocr_text",
      "_page_extractions", "page_extractions", "_cited_pages", "_telemetry", "_phase_timings",
      "borrower_details", "co_borrower_details", "details_of_borrower", "details_of_co_borrower",
      "details_of_the_borrower", "borrowers", "co_borrowers", "co_applicants",
      "applicant_name", "applicant_address", "script_tag"
    ]);
    const keys = new Set();
    allRows.forEach(r => {
       Object.keys(r).forEach(k => {
           if (!excludeKeys.has(k) && !k.startsWith("_") && !k.startsWith("telemetry_") && typeof r[k] !== 'object') {
             keys.add(k);
           }
       });
    });

    const priorityCols = [
      "borrower_name", "borrower_address",
      "co_borrower_1_name", "co_borrower_1_address",
      "co_borrower_2_name", "co_borrower_2_address",
      "co_borrower_3_name", "co_borrower_3_address",
      "co_borrower_4_name", "co_borrower_4_address",
      "account_no_lan",
      "sanction_amount", "tos"
    ];

    dynamicColumns = Array.from(keys).sort((a, b) => {
      const idxA = priorityCols.indexOf(a);
      const idxB = priorityCols.indexOf(b);
      if (idxA !== -1 && idxB !== -1) return idxA - idxB;
      if (idxA !== -1) return -1;
      if (idxB !== -1) return 1;
      return a.localeCompare(b);
    });
    
    // Rebuild thead
    const theadRow = $("#dynamicTheadRow");
    if (theadRow) {
       theadRow.innerHTML = `
          <th class="th-filter" data-col="lead_id">Lead ID<span class="th-caret" aria-hidden="true">▾</span></th>
          <th class="th-filter" data-col="processing_status">Status<span class="th-caret" aria-hidden="true">▾</span></th>
          <th class="th-filter" data-col="script_tag" title="Whether values were typed, read off a scan, or handwritten">Typed / Handwritten<span class="th-caret" aria-hidden="true">▾</span></th>
          ${dynamicColumns.map(c => {
            const isAddr = c.toLowerCase().includes("address");
            const style = isAddr ? 'style="min-width: 280px; max-width: 420px;"' : '';
            return `<th class="th-filter" data-col="${c}" ${style}>${formatColName(c)}<span class="th-caret" aria-hidden="true">▾</span></th>`;
          }).join("")}
       `;
    }

    syncColFilters();
    renderFacets();
    renderLeadRows();
    scanMissingScriptTags();
  }

  /* ── dashboard filters ───────────────────────
     Column filters follow whatever columns the extraction produced; the facet bar adds the
     cuts that are actually useful on this data: script type and "which field is missing". */
  function syncColFilters() {
    const live = new Set(["lead_id", "processing_status", "script_tag", ...dynamicColumns]);
    Object.keys(COL_FILTERS).forEach(k => { if (!live.has(k)) delete COL_FILTERS[k]; });
    live.forEach(k => { if (!(k in COL_FILTERS)) COL_FILTERS[k] = null; });
  }

  const SCRIPT_FACETS = [
    ["all", "All"], ["handwritten", "Handwritten"], ["scanned", "Scanned"],
    ["typed", "Typed"], ["none", "No values"], ["unknown", "Unchecked"],
  ];
  const scriptOf = (r) => r.script_tag || "unknown";
  const hasValue = (r, col) => r[col] !== undefined && r[col] !== null && String(r[col]).trim() !== "" && r[col] !== "—";

  function facetRows() {
    let rows = Array.isArray(allRows) ? allRows : [];
    if (state.script !== "all") rows = rows.filter(r => scriptOf(r) === state.script);
    if (state.missing) rows = rows.filter(r => !hasValue(r, state.missing));
    return rows;
  }

  function applyFilters() {
    renderFacets();
    renderLeadRows();
  }

  function renderFacets() {
    // typed / handwritten — a compact segmented control, only the tags present (plus the active one)
    const host = $("#scriptFacets");
    if (host) {
      const counts = {};
      (allRows || []).forEach(r => { const s = scriptOf(r); counts[s] = (counts[s] || 0) + 1; });
      host.innerHTML = SCRIPT_FACETS
        .filter(([k]) => k === "all" || counts[k] || state.script === k)
        .map(([k, label]) => {
          const on = state.script === k;
          const n = k === "all" ? (allRows || []).length : (counts[k] || 0);
          return `<button type="button" class="chip seg-b${on ? " active" : ""}" role="radio" aria-checked="${on}"
              data-script="${k}" title="${esc(k === "all" ? "Every dossier" : (SCRIPT_LABEL[k] || SCRIPT_LABEL.unknown)[1])}">
              ${k === "all" ? "" : `<i class="sdot d-${k}"></i>`}${label}<span class="seg-n">${n}</span></button>`;
        }).join("");
      $$("[data-script]", host).forEach(b => b.onclick = () => { state.script = b.dataset.script; applyFilters(); });
    }

    // field coverage — lives in a popover; the button carries a one-line summary
    const cov = $("#fieldCoverage");
    if (cov) {
      const rows = allRows || [];
      const stats = dynamicColumns.map(c => {
        const filled = rows.filter(r => hasValue(r, c)).length;
        return { col: c, filled, missing: rows.length - filled, pct: rows.length ? Math.round((100 * filled) / rows.length) : 0 };
      });
      const gaps = stats.filter(s => s.pct < 50).length;
      const sum = $("#coverageSummary");
      if (sum) sum.textContent = stats.length ? `${stats.length} fields${gaps ? ` · ${gaps} under 50%` : ""}` : "";
      cov.innerHTML = !rows.length || !stats.length
        ? `<div class="covlist-empty">No extracted fields in this view yet.</div>`
        : stats.map(s => `
          <button type="button" class="covrow${state.missing === s.col ? " on" : ""}" data-missing="${esc(s.col)}">
            <span class="covrow-l">${esc(formatColName(s.col))}
              <small>${s.missing ? `${s.missing} dossier${s.missing === 1 ? "" : "s"} missing it` : "found in every dossier"}</small></span>
            <span class="covrow-bar"><span class="${s.pct < 50 ? "low" : ""}" style="width:${Math.max(2, s.pct)}%"></span></span>
            <span class="covrow-v">${s.pct}%</span>
          </button>`).join("");
      $$("[data-missing]", cov).forEach(b => b.onclick = () => {
        state.missing = state.missing === b.dataset.missing ? null : b.dataset.missing;
        closeMenus();
        applyFilters();
      });
    }
    renderActiveFilters();
  }

  // what is filtering the table right now, each removable on its own
  function renderActiveFilters() {
    const host = $("#activeFilters");
    if (!host) return;
    const chips = [];
    if (state.script !== "all") {
      const label = (SCRIPT_FACETS.find(([k]) => k === state.script) || [, state.script])[1];
      chips.push(["script", `Typed / Handwritten: ${label}`]);
    }
    if (state.missing) chips.push(["missing", `Missing: ${formatColName(state.missing)}`]);
    const colN = Object.values(COL_FILTERS).filter(s => s !== null).length;
    if (colN) chips.push(["cols", `${colN} column filter${colN === 1 ? "" : "s"}`]);
    host.innerHTML = chips.map(([k, text]) =>
      `<span class="af-chip" data-k="${k}">${esc(text)}<button type="button" aria-label="Remove ${esc(text)}">✕</button></span>`).join("");
    $$(".af-chip button", host).forEach(btn => btn.onclick = () => {
      const k = btn.parentElement.dataset.k;
      if (k === "script") state.script = "all";
      if (k === "missing") state.missing = null;
      if (k === "cols") for (const c in COL_FILTERS) COL_FILTERS[c] = null;
      applyFilters();
    });
  }

  // status segments carry live counts; empty statuses stay but step back visually
  function renderStatusCounts(counts) {
    $$("#statusChips [data-count]").forEach(el => {
      const key = el.dataset.count;
      const n = key === "total" ? (counts.total || 0) : (counts[key] || 0);
      el.textContent = n ? n.toLocaleString("en-IN") : "";
      el.closest(".seg-b").classList.toggle("is-zero", key !== "total" && !n);
    });
  }

  /* ── small popovers (export menu, field coverage) ── */
  function closeMenus() {
    $$(".fpop").forEach(p => p.classList.add("hidden"));
    $$(".fmenu [aria-expanded]").forEach(b => b.setAttribute("aria-expanded", "false"));
  }

  function wireMenu(menuSel, btnSel) {
    const menu = $(menuSel), btn = $(btnSel);
    if (!menu || !btn) return;
    const pop = $(".fpop", menu);
    btn.onclick = (e) => {
      e.stopPropagation();
      const opening = pop.classList.contains("hidden");
      closeMenus();
      if (opening) { pop.classList.remove("hidden"); btn.setAttribute("aria-expanded", "true"); }
    };
    pop.addEventListener("click", (e) => { if (e.target.closest("[role=menuitem]")) closeMenus(); });
  }

  // Leads whose script tag hasn't been worked out yet get one in the background.
  async function scanMissingScriptTags() {
    // untagged, or tagged under older rules ("unknown" is re-checked; it recomputes if stale)
    const pending = (allRows || []).filter(r => !r.script_tag || r.script_tag === "unknown").map(r => r.lead_id);
    for (let i = 0; i < pending.length; i += 25) {
      const chunk = pending.slice(i, i + 25);
      try {
        const r = await fetch("/api/legal/provenance/scan", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ lead_ids: chunk }),
        });
        if (!r.ok) return;
        const { tags } = await r.json();
        let touched = false;
        (allRows || []).forEach(row => {
          if (tags && tags[row.lead_id]) { row.script_tag = tags[row.lead_id]; touched = true; }
        });
        if (touched) { renderFacets(); renderLeadRows(); }
      } catch (e) { return; }
    }
  }

  /* ── runs view ─────────────────────────────────────────────────────────────
     One run per page. Scrolling down walks that run's dossiers; a sideways gesture
     (trackpad swipe, touch swipe, shift+wheel, ← →) or the spine moves to the
     neighbouring run with a directional slide. Runs are tracked by run_id, so a new
     upload landing at the front of the list never changes the run being viewed. */
  const RUN_EXCLUDE = new Set([
    "lead_id", "batch_id", "batch_name", "folder_name", "folder_path", "dossier_type",
    "total_documents", "processed_documents", "failed_documents", "status",
    "processing_status", "is_test", "extraction_prompt", "created_at",
    "updated_at", "account_lan", "lead_name", "telemetry", "_raw_ocr_text",
    "_page_extractions", "page_extractions", "_cited_pages", "_telemetry", "_phase_timings",
    "borrower_details", "co_borrower_details", "details_of_borrower", "details_of_co_borrower",
    "details_of_the_borrower", "borrowers", "co_borrowers", "co_applicants",
    "applicant_name", "applicant_address", "script_tag"
  ]);
  const RUN_PRIORITY = [
    "borrower_name", "borrower_address",
    "co_borrower_1_name", "co_borrower_1_address",
    "co_borrower_2_name", "co_borrower_2_address",
    "co_borrower_3_name", "co_borrower_3_address",
    "co_borrower_4_name", "co_borrower_4_address",
    "account_no_lan",
    "sanction_amount", "tos"
  ];
  const RUN_LIVE = new Set(["processing", "pending"]);
  const RV_COMMIT = 120;   // drag distance (px) that switches run mid-gesture
  const RV_RELEASE = 44;   // drag distance (px) that switches run when the gesture ends
  const EASE_OUT = "cubic-bezier(.16,1,.3,1)";
  const EASE_IN = "cubic-bezier(.55,0,.75,.2)";
  const EASE_SPRING = "cubic-bezier(.34,1.56,.64,1)";
  const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const pad2 = (n) => String(n).padStart(2, "0");

  let runsData = [];
  let runsPollTimer = null;
  const RV = {
    scope: null, activeId: null, index: 0, cache: new Map(),
    leads: [], columns: [], query: "", promptOpen: false,
    busy: false, queued: null, token: 0,
    drag: 0, pull: 0, pan: 0, suppressClick: 0,
  };

  const isRunsVisible = () => !$("#view-runs").classList.contains("hidden");
  const overlayOpen = () => !!colPopup || ["#drawer", "#pageModal", "#cfgModal"].some(s => $(s)?.classList.contains("open"));

  function stopRunsPoll() {
    if (runsPollTimer) { clearInterval(runsPollTimer); runsPollTimer = null; }
  }

  function ensureRunsPoll() {
    const live = runsData.some(r => RUN_LIVE.has(r.status));
    if (!live || !isRunsVisible()) { stopRunsPoll(); return; }
    if (!runsPollTimer) {
      runsPollTimer = setInterval(() => (isRunsVisible() ? loadRuns({ quiet: true }) : stopRunsPoll()), 3000);
    }
  }

  async function loadRuns({ quiet = false } = {}) {
    if (RV.scope !== state.scope) {            // new scope: start from its newest run
      RV.scope = state.scope;
      RV.activeId = null;
      RV.index = 0;
      RV.cache.clear();
    }
    if (quiet && (RV.busy || RV.pull)) return; // never repaint mid-transition
    let data;
    try {
      const resp = await fetch("/api/legal/runs?scope=" + encodeURIComponent(state.scope));
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      data = await resp.json();
    } catch (e) {
      if (!quiet) toast("Failed to load runs history", "bad");
      return;
    }

    const prevStatus = new Map(runsData.map(r => [r.run_id, r.status]));
    runsData = Array.isArray(data) ? data : [];
    runsData.forEach(r => {                    // a run whose status moved has stale cached rows
      const hit = RV.cache.get(r.run_id);
      if (hit && hit.status !== r.status) RV.cache.delete(r.run_id);
    });

    const has = runsData.length > 0;
    $("#runsEmpty").classList.toggle("hidden", has);
    $("#rvBar").classList.toggle("hidden", !has);
    $("#rvStage").classList.toggle("hidden", !has);
    if (!has) { RV.activeId = null; stopRunsPoll(); return; }

    let idx = runsData.findIndex(r => r.run_id === RV.activeId);
    const sameRun = idx !== -1;
    if (!sameRun) {
      idx = Math.min(RV.index, runsData.length - 1);
      RV.promptOpen = false;
    }
    RV.index = idx;
    const run = runsData[idx];
    RV.activeId = run.run_id;

    renderSpine();
    renderRunHeader(run, 0);
    measureRunsChrome();
    ensureRunsPoll();

    const wasLive = RUN_LIVE.has(prevStatus.get(run.run_id));
    if (quiet && sameRun && !wasLive && !RUN_LIVE.has(run.status) && RV.cache.has(run.run_id)) return;
    await loadActiveRun(run, { stagger: !quiet, keepQuery: quiet && sameRun });
    prefetchNeighbours();
  }

  async function fetchRunLeads(run) {
    const resp = await fetch(`/api/legal/leads?batch_id=${encodeURIComponent(run.run_id)}&limit=1000`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const leads = Array.isArray(data) ? data : [];
    RV.cache.set(run.run_id, { leads, status: run.status });
    return leads;
  }

  function prefetchNeighbours() {
    [RV.index - 1, RV.index + 1].forEach(i => {
      const r = runsData[i];
      if (r && !RV.cache.has(r.run_id)) fetchRunLeads(r).catch(() => {});
    });
  }

  // Paints cached rows (or a skeleton) synchronously, then refreshes from the server when
  // there is no cache yet or the run is still being processed.
  async function loadActiveRun(run, { stagger = false, keepQuery = false } = {}) {
    const token = ++RV.token;
    if (!keepQuery) { RV.query = ""; $("#rvSearch").value = ""; }
    const hit = RV.cache.get(run.run_id);
    if (hit) renderRunBody(run, hit.leads, stagger);
    else renderRunSkeleton(run);
    if (hit && !RUN_LIVE.has(run.status)) return;
    try {
      const leads = await fetchRunLeads(run);
      if (token !== RV.token) return;          // the user already moved to another run
      renderRunBody(run, leads, stagger && !hit);
    } catch (e) {
      if (token !== RV.token || hit) return;
      renderRunError(run, e);
    }
  }

  function runColumns(leads) {
    const keys = new Set();
    leads.forEach(r => Object.keys(r).forEach(k => {
      if (!RUN_EXCLUDE.has(k) && !k.startsWith("_") && !k.startsWith("telemetry_") && typeof r[k] !== "object") keys.add(k);
    }));
    return [...keys].sort((a, b) => {
      const ia = RUN_PRIORITY.indexOf(a), ib = RUN_PRIORITY.indexOf(b);
      if (ia !== -1 && ib !== -1) return ia - ib;
      if (ia !== -1) return -1;
      if (ib !== -1) return 1;
      return a.localeCompare(b);
    });
  }

  function colClass(col) {
    if (isFinancialField(col)) return "c-num";
    if (/address|_add$|property|detail|description|direction/i.test(col)) return "c-long";
    return "";
  }

  function fmtRunDate(iso, short = false) {
    const d = iso ? new Date(iso) : null;
    if (!d || isNaN(d)) return "Unknown date";
    return d.toLocaleString("en-IN", short
      ? { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }
      : { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  /* ── header, spine ── */
  function rollText(host, text, dir) {
    const cur = host.lastElementChild;
    if (cur && cur.textContent === text) return;
    const next = document.createElement("span");
    next.textContent = text;
    host.appendChild(next);
    host.title = text;
    if (!cur) return;
    if (!dir || reducedMotion()) { [...host.children].forEach(c => c !== next && c.remove()); return; }
    next.animate([{ transform: `translateY(${dir * 80}%)`, opacity: 0 }, { transform: "none", opacity: 1 }],
      { duration: 480, easing: EASE_OUT });
    cur.animate([{ transform: "none", opacity: 1 }, { transform: `translateY(${-dir * 80}%)`, opacity: 0 }],
      { duration: 260, easing: EASE_IN, fill: "forwards" }).finished.then(() => cur.remove(), () => cur.remove());
  }

  function renderRunHeader(run, dir) {
    rollText($("#rvNow"), pad2(RV.index + 1), dir);
    rollText($("#rvTitle"), run.run_name || "Untitled run", dir);
    $("#rvTotal").textContent = pad2(runsData.length);
    $("#rvMeta").innerHTML = `${badge(run.status)}${run.is_test ? '<span class="rv-pill">Test</span>' : ""}
      <span>${esc(fmtRunDate(run.started_at))}</span><span class="rv-sep">·</span>
      <span>${run.leads} dossier${run.leads === 1 ? "" : "s"}</span>`;
    $("#rvCsv").href = `/api/legal/download/${encodeURIComponent(run.run_id)}`;
    $("#rvXlsx").href = `/api/legal/download/${encodeURIComponent(run.run_id)}/excel`;
    $("#rvPrev").disabled = RV.index <= 0;
    $("#rvNext").disabled = RV.index >= runsData.length - 1;
  }

  function renderSpine() {
    const spine = $("#rvSpine");
    const ids = runsData.map(r => r.run_id).join("|");
    if (spine.dataset.ids !== ids) {
      spine.dataset.ids = ids;
      $$(".rv-tick", spine).forEach(t => t.remove());
      const n = runsData.length;
      spine.insertAdjacentHTML("beforeend", runsData.map((r, i) => {
        const edge = n > 3 && i < n * 0.25 ? " tip-l" : (n > 3 && i >= n * 0.75 ? " tip-r" : "");
        return `<button type="button" class="rv-tick${edge}" role="tab" data-i="${i}"
          data-tip="${esc(r.run_name || "Untitled run")} · ${esc(fmtRunDate(r.started_at, true))}"
          aria-label="${esc(r.run_name || "Untitled run")}"></button>`;
      }).join(""));
      $$(".rv-tick", spine).forEach(t => { t.onclick = () => goToRun(+t.dataset.i); });
    }
    $$(".rv-tick", spine).forEach((t, i) => {
      const r = runsData[i];
      t.classList.toggle("is-live", RUN_LIVE.has(r.status));
      t.classList.toggle("is-failed", r.status === "failed");
      t.setAttribute("aria-selected", i === RV.index ? "true" : "false");
      t.tabIndex = i === RV.index ? 0 : -1;
    });
    placeThumb(0);
  }

  // progress: -1..1, how far the thumb leans toward the neighbouring run while dragging
  function placeThumb(progress) {
    const ticks = $$("#rvSpine .rv-tick");
    const t = ticks[RV.index];
    if (!t) return;
    const pitch = ticks.length > 1 ? ticks[1].offsetLeft - ticks[0].offsetLeft : 0;
    const thumb = $("#rvThumb");
    thumb.style.width = t.offsetWidth + "px";
    thumb.style.transform = `translateX(${t.offsetLeft + progress * pitch}px)`;
  }

  function measureRunsChrome() {
    const view = $("#view-runs");
    const rail = $(".rail");
    const top = window.matchMedia("(min-width: 1000px)").matches || !rail ? 0 : rail.offsetHeight;
    view.style.setProperty("--rv-top", `${top}px`);
    view.style.setProperty("--rv-bar-h", `${$("#rvBar").offsetHeight}px`);
  }

  /* ── body: summary + table ── */
  function runSummaryHTML(run, leads) {
    const count = (...keys) => leads ? leads.filter(l => keys.includes(l.processing_status || "pending")).length : null;
    const total = leads ? leads.length : (run.leads || 0);
    const completed = leads ? count("completed") : (run.completed || 0);
    const attention = count("partial", "missing_documents");
    const failed = leads ? count("failed") : (run.failed || 0);
    const queued = leads ? count("processing", "pending", "paused") : (run.processing || 0) + (run.pending || 0);
    const docs = leads ? leads.reduce((a, l) => a + (l.total_documents || 0), 0) : null;
    const stat = (cls, label, v) =>
      `<div class="rv-stat ${cls}"><div class="n${v === 0 ? " zero" : ""}">${v == null ? "—" : v}</div><div class="l">${label}</div></div>`;
    const seg = (cls, v) => (v ? `<span class="${cls}" style="flex:${v}"></span>` : "");
    const prompt = (run.prompt || "").trim();
    const long = prompt.length > 200 || prompt.split("\n").length > 3;
    const open = RV.promptOpen && long;
    return `
      <div class="rv-stats-wrap">
        <div class="rv-stats">
          ${stat("tot", "Dossiers", total)}
          ${stat("ok", "Completed", completed)}
          ${stat("warn", "Partial / missing", attention)}
          ${stat("bad", "Failed", failed)}
          ${stat("live", "In queue", queued)}
        </div>
        <div class="rv-mix" aria-hidden="true">${seg("ok", completed)}${seg("warn", attention)}${seg("bad", failed)}${seg("live", queued)}</div>
        ${docs != null ? `<div class="rv-docs-l">${docs} document${docs === 1 ? "" : "s"} across ${total} dossier${total === 1 ? "" : "s"}</div>` : ""}
      </div>
      <div class="rv-prompt${prompt ? "" : " is-empty"}${open ? " open" : ""}">
        <div class="rv-prompt-k"><span>Extraction prompt</span>${prompt ? '<button type="button" class="rv-link" data-act="copy">Copy</button>' : ""}</div>
        <div class="rv-prompt-t">${prompt ? esc(prompt) : "No prompt given for this run."}</div>
        ${long ? `<button type="button" class="rv-link" data-act="more">${open ? "Show less" : "Show full prompt"}</button>` : ""}
      </div>`;
  }

  function renderRunSkeleton(run) {
    RV.leads = [];
    RV.columns = [];
    $("#rvSummary").innerHTML = runSummaryHTML(run, null);
    $("#rvThead").innerHTML = `<th class="c-dossier">Dossier</th><th class="c-status">Status</th><th>Extracted fields</th>`;
    const n = Math.min(6, Math.max(3, run.leads || 3));
    $("#rvTbody").innerHTML = Array.from({ length: n }, (_, i) => `
      <tr class="rv-skel" aria-hidden="true">
        <td><i style="width:${68 + (i * 13) % 26}%"></i><i style="width:42%"></i></td>
        <td><i style="width:64%"></i></td>
        <td><i style="width:${58 + (i * 17) % 34}%"></i><i style="width:${34 + (i * 11) % 30}%"></i></td>
      </tr>`).join("");
    $("#rvRunEmpty").classList.add("hidden");
    $("#rvCount").textContent = "Loading dossiers…";
  }

  function renderRunError(run, err) {
    $("#rvTbody").innerHTML = "";
    const empty = $("#rvRunEmpty");
    empty.innerHTML = `Couldn't load this run's dossiers (${esc(err.message || "error")}).
      <button type="button" class="rv-link" id="rvRetry">Try again</button>`;
    empty.classList.remove("hidden");
    $("#rvCount").textContent = "";
    $("#rvRetry").onclick = () => loadActiveRun(run, { stagger: true });
  }

  function renderRunBody(run, leads, stagger) {
    RV.leads = leads;
    RV.columns = runColumns(leads);
    $("#rvSummary").innerHTML = runSummaryHTML(run, leads);
    $("#rvThead").innerHTML = `<th class="c-dossier">Dossier</th><th class="c-status">Status</th><th class="c-script">Typed / Handwritten</th>` +
      RV.columns.map(c => `<th class="${colClass(c)}">${esc(formatColName(c))}</th>`).join("");
    renderRunRows(stagger);
  }

  function runCell(col, val) {
    const label = esc(formatColName(col));
    const cls = colClass(col);
    if (val == null || val === "") return `<td class="${cls} c-empty" data-label="${label}">—</td>`;
    const shown = esc(isFinancialField(col) && typeof val === "number" ? fmtINR(val) : String(val));
    return `<td class="${cls}" data-label="${label}">${cls === "c-long" ? `<div class="rv-long">${shown}</div>` : shown}</td>`;
  }

  function renderRunRows(stagger = false) {
    const q = RV.query.toLowerCase();
    const cols = RV.columns;
    const rows = !q ? RV.leads : RV.leads.filter(r =>
      [r.lead_id, r.folder_name, r.processing_status, ...cols.map(c => r[c])]
        .some(v => v != null && String(v).toLowerCase().includes(q)));
    const total = RV.leads.length;
    $("#rvCount").textContent = q
      ? `${rows.length} of ${total} dossier${total === 1 ? "" : "s"}`
      : `${total} dossier${total === 1 ? "" : "s"}`;
    const empty = $("#rvRunEmpty");
    empty.textContent = q ? "No dossiers in this run match your search." : "This run has no dossiers.";
    empty.classList.toggle("hidden", rows.length > 0);

    const animate = stagger && !reducedMotion();
    $("#rvTbody").innerHTML = rows.map((r, i) => {
      const enter = animate && i < 24 ? ` class="rv-enter" style="--d:${80 + i * 28}ms"` : "";
      const docs = r.total_documents
        ? `<div class="rv-sub">${r.processed_documents || 0}/${r.total_documents} docs read</div>` : "";
      return `<tr tabindex="0" data-id="${esc(r.lead_id)}"${enter}>
        <td class="c-dossier" data-label="Dossier"><div class="rv-name">${esc(r.folder_name || r.lead_name || r.lead_id)}</div><div class="rv-id">${esc(r.lead_id)}</div></td>
        <td class="c-status" data-label="Status">${badge(r.processing_status)}${docs}</td>
        <td class="c-script" data-label="Typed / Handwritten">${scriptChip(r.script_tag)}</td>
        ${cols.map(c => runCell(c, r[c])).join("")}
      </tr>`;
    }).join("");
    fitRunTable();
  }

  // Long text columns get a readable share of the width; many columns tighten the table and,
  // past that, the card itself pans sideways rather than hiding anything.
  function fitRunTable() {
    const table = $("#rvTable");
    if (window.matchMedia("(max-width: 760px)").matches) { table.classList.remove("rv-dense"); return; }
    const longCols = RV.columns.filter(c => colClass(c) === "c-long").length;
    const width = $("#rvStage").clientWidth;
    const min = longCols ? Math.round(Math.max(150, Math.min(260, (width * 0.55) / longCols))) : 0;
    table.style.setProperty("--rv-long-min", `${min}px`);
    table.classList.toggle("rv-dense", RV.columns.length > 8);
    setTablePan(0);
    // when the table itself pans, say so — the carousel then lives outside it (and on ← →)
    const pans = tablePanMax() > 1;
    const hint = $("#rvLegend .rv-hint");
    if (hint) {
      hint.textContent = pans
        ? "Swipe over the table for more columns · swipe anywhere else, or ← →, to change run"
        : "Swipe sideways or press ← → to change run";
      $("#rvLegend").classList.toggle("hint-done", !pans && localStorage.getItem("legal-runs-hint") === "1");
    }
  }

  /* ── panning wide extractions ──────────────
     The card clips instead of scrolling (so the table head can stay pinned to the page), so
     the columns are moved with a transform. A sideways gesture over the table pans it; once
     it reaches the end, the same gesture moves to the neighbouring run. */
  const tablePanMax = () => Math.max(0, $("#rvTable").offsetWidth - $("#rvTablecard").clientWidth);

  function setTablePan(px) {
    const max = tablePanMax();
    RV.pan = Math.max(0, Math.min(max, px));
    $("#rvTablepan").style.transform = RV.pan ? `translateX(${-RV.pan}px)` : "";
    $("#rvTablecard").classList.toggle("can-pan", RV.pan < max - 1);
  }

  // A sideways gesture over a table that can still pan belongs to the table, not the carousel.
  function tableCanPan(target, dx) {
    if (!(target && target.closest && target.closest(".rv-tablecard"))) return false;
    const max = tablePanMax();
    if (max <= 1) return false;
    return dx > 0 ? RV.pan < max - 1 : RV.pan > 1;
  }

  /* ── moving between runs ── */
  function scrollRunsToTop() {
    const view = $("#view-runs");
    const top = view.getBoundingClientRect().top + window.scrollY -
      (parseFloat(view.style.getPropertyValue("--rv-top")) || 0) - 12;
    if (window.scrollY > top) window.scrollTo({ top: Math.max(0, top), behavior: "instant" });
  }

  const dragOpacity = (x) => 1 - Math.min(Math.abs(x) / 480, 0.3);
  const clearSlideInline = (slide) => { slide.style.transform = ""; slide.style.opacity = ""; };

  function bumpEdge(dir) {
    if (reducedMotion()) return;
    $("#rvSlide").animate(
      [{ transform: "none" }, { transform: `translateX(${-dir * 18}px)` }, { transform: "none" }],
      { duration: 380, easing: EASE_SPRING });
  }

  async function goToRun(target) {
    if (!runsData.length) return;
    if (target < 0 || target >= runsData.length) {
      if (RV.pull) springBack(); else bumpEdge(target < 0 ? -1 : 1);
      return;
    }
    if (RV.busy) { RV.queued = target; return; }
    if (target === RV.index) { springBack(); return; }

    RV.busy = true;
    const dir = target > RV.index ? 1 : -1;
    const run = runsData[target];
    const slide = $("#rvSlide");
    const motion = !reducedMotion();
    const fromX = RV.drag;
    RV.drag = 0;
    RV.pull = 0;
    hidePeek();
    $("#rvBar").classList.remove("dragging");
    $("#rvLegend").classList.add("hint-done");
    try { localStorage.setItem("legal-runs-hint", "1"); } catch (_) {}

    RV.index = target;
    RV.activeId = run.run_id;
    RV.promptOpen = false;
    renderSpine();
    renderRunHeader(run, dir);

    if (motion) {
      const exit = slide.animate([
        { transform: `translateX(${fromX}px)`, opacity: dragOpacity(fromX) },
        { transform: `translateX(${-dir * 90}px) scale(.985)`, opacity: 0 },
      ], { duration: 230, easing: EASE_IN, fill: "forwards" });
      clearSlideInline(slide);
      await exit.finished.catch(() => {});
    } else {
      clearSlideInline(slide);
    }

    scrollRunsToTop();
    loadActiveRun(run, { stagger: motion });   // paints cached rows or a skeleton right away
    slide.getAnimations().forEach(a => a.cancel());
    if (motion) {
      await slide.animate([
        { transform: `translateX(${dir * 120}px)`, opacity: 0 },
        { transform: "none", opacity: 1 },
      ], { duration: 560, easing: EASE_OUT }).finished.catch(() => {});
    }

    RV.busy = false;
    measureRunsChrome();
    prefetchNeighbours();
    ensureRunsPoll();
    if (RV.queued != null) {
      const q = RV.queued;
      RV.queued = null;
      goToRun(q);
    }
  }

  function rubber(x) {
    return Math.sign(x) * 90 * (1 - 1 / (Math.abs(x) / 180 + 1));
  }

  // raw: signed pull in px. Negative pulls content left, revealing the next (older) run.
  function setDrag(raw) {
    const toNext = raw < 0;
    const atEdge = toNext ? RV.index >= runsData.length - 1 : RV.index <= 0;
    const x = atEdge ? rubber(raw) : raw;
    RV.pull = raw;
    RV.drag = x;
    const slide = $("#rvSlide");
    slide.style.transform = x ? `translateX(${x}px)` : "";
    slide.style.opacity = String(dragOpacity(x));
    $("#rvBar").classList.toggle("dragging", !!x);
    placeThumb(atEdge ? 0 : Math.max(-1, Math.min(1, -raw / RV_COMMIT)) * 0.6);
    showPeek(toNext, Math.abs(raw) / RV_COMMIT, atEdge);
  }

  function showPeek(toNext, amount, atEdge) {
    const el = $(toNext ? "#rvPeekNext" : "#rvPeekPrev");
    const other = $(toNext ? "#rvPeekPrev" : "#rvPeekNext");
    other.style.opacity = "0";
    other.classList.remove("armed");
    const a = Math.max(0, Math.min(1, amount));
    const target = runsData[RV.index + (toNext ? 1 : -1)];
    $(".rv-peek-k", el).textContent = atEdge ? (toNext ? "Oldest run" : "Newest run") : (toNext ? "Older run" : "Newer run");
    $(".rv-peek-v", el).textContent = atEdge ? "Nothing further" : (target.run_name || "Untitled run");
    el.classList.toggle("edge", atEdge);
    el.classList.toggle("armed", !atEdge && a >= RV_RELEASE / RV_COMMIT);
    el.style.opacity = String(Math.min(1, a * 1.8));
    el.style.transform = `translateY(-50%) translateX(${(toNext ? 1 : -1) * (1 - a) * 28}px) scale(${0.9 + a * 0.1})`;
  }

  function hidePeek() {
    ["#rvPeekPrev", "#rvPeekNext"].forEach(s => {
      const el = $(s);
      el.style.opacity = "0";
      el.style.transform = "";
      el.classList.remove("armed");
    });
  }

  function springBack() {
    const slide = $("#rvSlide");
    const x = RV.drag;
    RV.drag = 0;
    RV.pull = 0;
    hidePeek();
    $("#rvBar").classList.remove("dragging");
    placeThumb(0);
    if (x && !reducedMotion()) {
      slide.animate([{ transform: `translateX(${x}px)`, opacity: dragOpacity(x) }, { transform: "none", opacity: 1 }],
        { duration: 440, easing: EASE_SPRING });
    }
    clearSlideInline(slide);
  }

  function releaseDrag(velocity = 0, threshold = RV_RELEASE) {
    if (!RV.pull) return;
    const flick = Math.abs(velocity) > 0.45;
    const dir = (flick ? velocity : RV.pull) < 0 ? 1 : -1;
    const target = RV.index + dir;
    if (target >= 0 && target < runsData.length && (flick || Math.abs(RV.pull) >= threshold)) goToRun(target);
    else springBack();
  }

  function initRunsView() {
    const view = $("#view-runs");
    if (!view || !$("#rvBar")) return;          // markup absent (e.g. an older page) — skip, don't break

    // trackpad / horizontal wheel: the content follows the fingers, then commits or springs back
    let wheelIdle = null;
    let wheelLocked = false;
    view.addEventListener("wheel", (e) => {
      if (!runsData.length || overlayOpen()) return;
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? window.innerWidth : 1;
      let dx = e.deltaX * unit, dy = e.deltaY * unit;
      if (e.shiftKey && Math.abs(dx) < Math.abs(dy)) { dx = dy; dy = 0; }
      if (Math.abs(dx) < 2 || Math.abs(dx) <= Math.abs(dy)) return;   // vertical intent: page scrolls as usual
      e.preventDefault();
      if (!RV.pull && tableCanPan(e.target, dx)) {                     // pan the columns first
        setTablePan(RV.pan + dx);
        clearTimeout(wheelIdle);
        wheelIdle = setTimeout(() => { wheelLocked = false; }, 160);
        return;
      }
      clearTimeout(wheelIdle);
      wheelIdle = setTimeout(() => { wheelLocked = false; releaseDrag(); }, 160);
      if (wheelLocked || RV.busy) return;
      if (e.shiftKey || e.deltaMode !== 0) {   // a mouse-wheel notch is one step
        wheelLocked = true;
        goToRun(RV.index + (dx > 0 ? 1 : -1));
        return;
      }
      setDrag(RV.pull - dx * 0.7);
      if (Math.abs(RV.pull) >= RV_COMMIT) { wheelLocked = true; releaseDrag(); }
    }, { passive: false });

    // touch / pen: follow the finger horizontally, leave vertical panning to the browser
    let touch = null;
    view.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse" || !runsData.length || RV.busy || overlayOpen()) return;
      touch = { id: e.pointerId, x0: e.clientX, y0: e.clientY, axis: null, lx: e.clientX, lt: e.timeStamp, v: 0 };
    });
    view.addEventListener("pointermove", (e) => {
      if (!touch || e.pointerId !== touch.id) return;
      const dx = e.clientX - touch.x0, dy = e.clientY - touch.y0;
      if (!touch.axis) {
        if (Math.hypot(dx, dy) < 10) return;
        touch.axis = Math.abs(dx) > Math.abs(dy) * 1.2 ? "x" : "y";
        if (touch.axis === "x" && tableCanPan(e.target, -dx)) {
          touch.axis = "pan";
          touch.pan0 = RV.pan;
          try { view.setPointerCapture(e.pointerId); } catch (_) {}
        } else if (touch.axis === "x") {
          try { view.setPointerCapture(e.pointerId); } catch (_) {}
        }
      }
      if (touch.axis === "pan") { setTablePan(touch.pan0 - dx); return; }
      if (touch.axis !== "x") return;
      const dt = Math.max(1, e.timeStamp - touch.lt);
      touch.v = (e.clientX - touch.lx) / dt;
      touch.lx = e.clientX;
      touch.lt = e.timeStamp;
      setDrag(dx);
    });
    const endTouch = (e) => {
      if (!touch || e.pointerId !== touch.id) return;
      const swiped = touch.axis === "x";
      const panned = touch.axis === "pan";
      const v = touch.v;
      touch = null;
      if (panned) { RV.suppressClick = Date.now() + 350; return; }
      if (!swiped) return;
      RV.suppressClick = Date.now() + 350;     // the lift after a swipe must not open a lead
      releaseDrag(v, Math.max(RV_RELEASE, $("#rvStage").clientWidth * 0.15));
    };
    view.addEventListener("pointerup", endTouch);
    view.addEventListener("pointercancel", endTouch);

    document.addEventListener("keydown", (e) => {
      if (!isRunsVisible() || !runsData.length || overlayOpen() || e.altKey || e.ctrlKey || e.metaKey) return;
      if (e.target.closest && e.target.closest("input, textarea, select, [contenteditable]")) return;
      if (e.key === "ArrowRight") { e.preventDefault(); goToRun(RV.index + 1); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); goToRun(RV.index - 1); }
    });

    $("#rvPrev").onclick = () => goToRun(RV.index - 1);
    $("#rvNext").onclick = () => goToRun(RV.index + 1);
    $("#rvDash").onclick = () => {
      const run = runsData[RV.index];
      if (!run) return;
      state.batchId = run.run_id;
      updateBatchFilterUI();
      switchView("dashboard");
    };

    const tbody = $("#rvTbody");
    tbody.addEventListener("click", (e) => {
      if (Date.now() < RV.suppressClick) return;
      const tr = e.target.closest("tr[data-id]");
      if (tr) openLead(tr.dataset.id);
    });
    tbody.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      const tr = e.target.closest("tr[data-id]");
      if (tr) { e.preventDefault(); openLead(tr.dataset.id); }
    });

    $("#rvSummary").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (!btn) return;
      if (btn.dataset.act === "copy") {
        navigator.clipboard.writeText(runsData[RV.index]?.prompt || "")
          .then(() => toast("Extraction prompt copied", "ok"), () => toast("Copy failed", "bad"));
      } else if (btn.dataset.act === "more") {
        RV.promptOpen = btn.closest(".rv-prompt").classList.toggle("open");
        btn.textContent = RV.promptOpen ? "Show less" : "Show full prompt";
      }
    });

    $("#rvSearch").oninput = debounce(() => {
      RV.query = $("#rvSearch").value.trim();
      renderRunRows(false);
    }, 150);

    try { if (localStorage.getItem("legal-runs-hint")) $("#rvLegend").classList.add("hint-done"); } catch (_) {}

    let stuckRaf = 0;
    window.addEventListener("scroll", () => {
      if (stuckRaf || !isRunsVisible()) return;
      stuckRaf = requestAnimationFrame(() => {
        stuckRaf = 0;
        const top = parseFloat(view.style.getPropertyValue("--rv-top")) || 0;
        const bar = $("#rvBar");
        bar.classList.toggle("is-stuck", window.scrollY > 0 && bar.getBoundingClientRect().top <= top + 0.5);
      });
    }, { passive: true });

    window.addEventListener("resize", debounce(() => {
      if (!isRunsVisible() || !runsData.length) return;
      measureRunsChrome();
      placeThumb(0);
      fitRunTable();
      setTablePan(RV.pan);
    }, 120));
    if (window.ResizeObserver) new ResizeObserver(() => isRunsVisible() && measureRunsChrome()).observe($("#rvBar"));
  }

  function passesColFilters(r) {
    return Object.entries(COL_FILTERS).every(([col, set]) => set === null || set.has(rowVal(r, col)));
  }

  function renderLeadRows() {
    const rows = facetRows().filter(passesColFilters);
    const body = $("#leadsBody");
    const total = (allRows || []).length;
    const filtered = rows.length !== total;
    const anyFilter = Object.values(COL_FILTERS).some(s => s !== null) || state.script !== "all" || !!state.missing;

    $("#tableCount").textContent = `${rows.length} lead${rows.length === 1 ? "" : "s"}` +
      (filtered ? ` of ${total}` : "");
    $("#clearFilters").classList.toggle("hidden", !anyFilter);

    const empty = $("#tableEmpty");
    empty.classList.toggle("hidden", rows.length > 0);

    body.innerHTML = rows.map(r => {
      let tds = `
        <td class="lead-id mono nowrap">${esc(r.lead_id)}</td>
        <td>${badge(r.processing_status)}</td>
        <td class="c-script">${scriptChip(r.script_tag)}</td>
      `;
      dynamicColumns.forEach(col => {
         let val = r[col];
         if (val === undefined || val === null) val = "—";
         else if (typeof val === 'number') val = val.toString();
         const isAddr = col.toLowerCase().includes("address");
         const cellStyle = isAddr
           ? 'style="min-width: 280px; max-width: 420px; white-space: normal; word-break: break-word; line-height: 1.45; font-size: 12.5px;"'
           : '';
         tds += `<td ${cellStyle}>${esc(val)}</td>`;
      });
      
      return `<tr data-id="${esc(r.lead_id)}" class="clickable">${tds}</tr>`;
    }).join("");

    $$("#leadsBody tr").forEach(tr => tr.onclick = () => openLead(tr.dataset.id));
    $$("th[data-col]").forEach(th => th.classList.toggle("filtered", COL_FILTERS[th.dataset.col] != null));
    renderActiveFilters();
  }

  /* ── column filters ────────────────────── */
  let colPopup = null;
  function closeColFilter() {
    if (!colPopup) return;
    colPopup.remove(); colPopup = null;
    document.removeEventListener("mousedown", colDocDown, true);
  }
  function resetColFilters() {
    for (const k in COL_FILTERS) COL_FILTERS[k] = null;
    state.script = "all";
    state.missing = null;
    closeColFilter();
    renderFacets();
    renderLeadRows();
  }
  function colDocDown(e) {
    if (colPopup && !colPopup.contains(e.target) && !e.target.closest("th[data-col]")) closeColFilter();
  }
  function openColFilter(col, th) {
    const reopen = colPopup && colPopup.dataset.col === col;
    closeColFilter();
    if (reopen) return;

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
    colPopup = pop;

    const rect = th.getBoundingClientRect();
    pop.style.top = (rect.bottom + window.scrollY + 4) + "px";
    pop.style.left = Math.max(10, Math.min(window.innerWidth - pop.offsetWidth - 10, rect.left + window.scrollX)) + "px";

    const filterList = () => {
      const q = pop.querySelector(".cf-search input").value.toLowerCase();
      $$(".cf-item", pop).forEach(it => {
        const txt = it.querySelector(".cf-v").textContent.toLowerCase();
        it.classList.toggle("hidden", !txt.includes(q));
      });
    };
    pop.querySelector(".cf-search input").oninput = filterList;
    pop.querySelector('[data-act="all"]').onclick = () => { $$(".cf-item input", pop).forEach(c => c.checked = true); apply(); };
    pop.querySelector('[data-act="none"]').onclick = () => { $$(".cf-item input", pop).forEach(c => c.checked = false); apply(); };

    const apply = () => {
      const checked = $$(".cf-item input:checked", pop).map(c => c.value);
      COL_FILTERS[col] = (checked.length === values.length) ? null : new Set(checked);
      renderLeadRows();
    };
    $$(".cf-item input", pop).forEach(c => c.onchange = apply);
    setTimeout(() => document.addEventListener("mousedown", colDocDown, true), 50);
  }

  /* ── field & page provenance helpers ───────── */
  function formatFieldLabel(k) {
    const map = {
      borrower_name: "Borrower Name",
      borrower_address: "Borrower Address",
      co_borrower_1_name: "Co-Borrower 1 Name",
      co_borrower_1_address: "Co-Borrower 1 Address",
      co_borrower_2_name: "Co-Borrower 2 Name",
      co_borrower_2_address: "Co-Borrower 2 Address",
      co_borrower_3_name: "Co-Borrower 3 Name",
      co_borrower_3_address: "Co-Borrower 3 Address",
      account_no_lan: "Account LAN",
      sanction_amount: "Sanction Amount",
      sanction_amount_in_words: "Sanction in Words",
      sanction_date: "Sanction Date",
      disbursal_date: "Disbursal Date",
      roi_in_number: "Rate of Interest (ROI)",
      applicant_name: "Primary Applicant / Borrower",
      applicant_address: "Applicant Address",
      co_applicant_1: "Co-Applicant 1",
      co_applicant_address_1: "Co-Applicant 1 Address",
      co_applicant_2: "Co-Applicant 2",
      co_applicant_address_2: "Co-Applicant 2 Address",
      co_applicant_3: "Co-Applicant 3",
      co_applicant_address_3: "Co-Applicant 3 Address",
      guarantor_1: "Guarantor 1",
      guarantor_1_add: "Guarantor 1 Address",
      guarantor_2: "Guarantor 2",
      guarantor_2_add: "Guarantor 2 Address",
      npa_date: "NPA Classification Date",
      future_principal: "Future Principal",
      principal_overdue: "Principal Overdue",
      interest_overdue: "Interest Overdue",
      interest_on_termination: "Interest on Termination",
      late_payment_penal: "Late Payment Penal Charges",
      cheque_bounce_inc_gst: "Cheque Bounce Charges (Inc. GST)",
      other_charges_inc_gst: "Other Charges (Inc. GST)",
      foreclosure_charges: "Foreclosure Charges",
      litigation_charges: "Litigation Charges",
      excess_amount: "Excess Amount",
      tos: "Total Outstanding Sum (TOS)",
      mortgaged_property_detail_1: "Mortgaged Property Description",
      directions: "4-Way Boundary Compass",
      property_owner_mortgagor: "Property Owner / Mortgagor",
      third_party_mortgagor_flag: "Third-Party Mortgagor Flag",
      property_verification_status: "Title Verification Status",
    };
    if (map[k]) return map[k];
    const m = k.match(/^co_borrower_(\d+)_(name|address)$/i);
    if (m) {
      return `Co-Borrower ${m[1]} ${m[2] === 'name' ? 'Name' : 'Address'}`;
    }
    return k.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
  }

  function isFinancialField(k) {
    return ["sanction_amount", "future_principal", "principal_overdue", "interest_overdue",
            "interest_on_termination", "late_payment_penal", "cheque_bounce_inc_gst",
            "other_charges_inc_gst", "foreclosure_charges", "litigation_charges",
            "excess_amount", "tos"].includes(k);
  }

  function _renderObject(v) {
    if (Array.isArray(v)) {
      if (v.length === 0) return "—";
      // If array of objects (like borrower_details)
      if (typeof v[0] === "object" && v[0] !== null) {
         let html = '<div style="display:flex; flex-direction:column; gap:8px; margin-top:4px;">';
         v.forEach((item, idx) => {
            html += '<div style="background:var(--bg); border:1px solid var(--line-light); padding:8px 12px; border-radius:4px;">';
            html += '<div style="font-size:10px; color:var(--text-light); margin-bottom:4px; text-transform:uppercase; letter-spacing:0.5px;">Item ' + (idx+1) + '</div>';
            Object.entries(item).forEach(([ik, iv]) => {
                html += '<div style="margin-bottom:4px; display:flex;"><span style="color:var(--text-light); font-weight:500; width:120px; flex-shrink:0;">' + formatFieldLabel(ik) + ':</span> <span style="font-weight:500;">' + esc(String(iv)) + '</span></div>';
            });
            html += '</div>';
         });
         html += '</div>';
         return html;
      }
      // If array of strings
      return '<ul style="margin:4px 0 0 0; padding-left:20px; color:var(--text);">' + v.map(item => '<li style="margin-bottom:2px;">' + esc(String(item)) + '</li>').join('') + '</ul>';
    }
    // If it's a plain object
    let html = '<div style="background:var(--bg); border:1px solid var(--line-light); padding:8px 12px; border-radius:4px; margin-top:4px;">';
    Object.entries(v).forEach(([ik, iv]) => {
        html += '<div style="margin-bottom:4px; display:flex;"><span style="color:var(--text-light); font-weight:500; width:120px; flex-shrink:0;">' + formatFieldLabel(ik) + ':</span> <span style="font-weight:500;">' + esc(String(iv)) + '</span></div>';
    });
    html += '</div>';
    return html;
}

function formatFieldValue(k, v) {
    if (v == null || v === "" || v === "—") return "—";
    if (isFinancialField(k) && typeof v === "number") return fmtINR(v);
    if (typeof v === "object") return _renderObject(v);
    return String(v);
  }

  function formatBytes(bytes) {
    if (!bytes || bytes <= 0) return "—";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(2) + " MB";
  }

  window.__pageData = {};
  function openPageModal(docId, filename, pageNum, totalPages) {
    if (!docId) return;
    const url = `/api/legal/document/${encodeURIComponent(docId)}/page/${encodeURIComponent(pageNum)}`;
    const ttl = $("#pageModalTitle");
    if (ttl) ttl.textContent = `${filename} — Page ${pageNum}${totalPages ? ` of ${totalPages}` : ""}`;
    const img = $("#pageModalImg");
    if (img) img.src = url;
    const link = $("#pageModalOpenNew");
    if (link) link.href = url;
    
    const p = window.__pageData[`${docId}-${pageNum}`] || {};
    const dataBox = $("#pageModalData");
    if (dataBox) {
      const realFields = {};
      for (const [k, v] of Object.entries(p.fields || {})) {
        if (!k.startsWith("_") && v != null && v !== "" && v !== "—") {
          realFields[k] = v;
        }
      }
      const fieldCount = Object.keys(realFields).length;
      dataBox.innerHTML = `
        <div style="margin-bottom:20px;">
          <h4 style="margin:0 0 4px 0; font-size:13px; color:var(--ink-faint); text-transform:uppercase; letter-spacing:0.05em;">Extraction Metadata</h4>
          <div style="display:flex; gap:8px; flex-wrap:wrap; margin-top:8px;">
            <span class="tag" style="background:var(--surface); border:1px solid var(--line-strong);">Engine: ${esc(p.ocr_route || p.phase || "Vision Model")}</span>
            ${p.model ? `<span class="tag" style="background:var(--surface); border:1px solid var(--line-strong);">Model: ${esc(p.model)}</span>` : ''}
          </div>
        </div>
        <div style="margin-bottom:20px;">
          <h4 style="margin:0 0 12px 0; font-size:13px; color:var(--ink-faint); text-transform:uppercase; letter-spacing:0.05em;">Extracted Fields (${fieldCount})</h4>
          ${fieldCount > 0 ? `
            <table class="page-fields-table" style="width:100%; border-collapse:collapse;">
              <tbody>
                ${Object.entries(realFields).map(([fk, fv]) => `
                  <tr>
                    <td class="pft-k" style="padding:8px; border-bottom:1px solid var(--line-light); vertical-align:top;">${esc(formatFieldLabel(fk))}</td>
                    <td class="pft-v${isFinancialField(fk) ? ' mono' : ''}" style="padding:8px; border-bottom:1px solid var(--line-light); font-weight:500;">${formatFieldValue(fk, fv)}</td>
                  </tr>`).join("")}
              </tbody>
            </table>` : `<div style="color:var(--ink-soft); font-size:13px; padding:12px; background:var(--surface); border-radius:4px; border:1px dashed var(--line-strong);">No specific key fields extracted from this page.</div>`}
        </div>
        ${p.snippet ? `
        <div>
          <h4 style="margin:0 0 8px 0; font-size:13px; color:var(--ink-faint); text-transform:uppercase; letter-spacing:0.05em;">Text Evidence</h4>
          <div class="evidence-snippet" style="background:var(--surface); padding:12px; border-radius:6px; font-family:'JetBrains Mono',monospace; font-size:12px; color:var(--ink-strong); border:1px solid var(--line); white-space:pre-wrap;">${esc(p.snippet)}</div>
        </div>` : ""}
        <div style="margin-top:20px;">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
            <h4 style="margin:0; font-size:13px; color:var(--ink-faint); text-transform:uppercase; letter-spacing:0.05em;">Raw Extraction from Page ${p.page_number || ''}</h4>
            <button class="btn line small" style="padding:2px 8px; font-size:11px;" onclick="navigator.clipboard.writeText(this.closest('div').nextElementSibling.textContent); toast('Raw extraction copied', 'ok');">📋 Copy JSON</button>
          </div>
          <pre style="margin:0; background:var(--surface); padding:12px; border-radius:6px; font-family:'JetBrains Mono',monospace; font-size:11.5px; color:var(--ink); border:1px solid var(--line); white-space:pre-wrap; word-break:break-word; max-height:350px; overflow-y:auto; line-height:1.45;">${esc(
            (typeof p.raw_response === 'string' && p.raw_response.trim())
              ? p.raw_response.trim()
              : (typeof p.raw_ocr_text === 'string' && p.raw_ocr_text.trim()
                  ? p.raw_ocr_text.trim()
                  : JSON.stringify(p.fields || {}, null, 2))
          )}</pre>
        </div>
      `;
    }

    const modal = $("#pageModal"), scrim = $("#pageModalScrim");
    if (modal) modal.classList.add("open");
    if (scrim) scrim.classList.add("open");
  }
  window.openPageModal = openPageModal;

  function closePageModal() {
    const modal = $("#pageModal"), scrim = $("#pageModalScrim");
    if (modal) modal.classList.remove("open");
    if (scrim) scrim.classList.remove("open");
  }

  /* ── "why are there no values?" ─────────────
     Runs extracted since per-batch recording show every model call and what it returned.
     Older runs only kept documents and timings, so the panel says what can still be
     inferred — and that re-running the dossier will record the model's reply. */
  function emptyDiagnosis(j, docs, f) {
    const batches = (j.journey || []).filter(e => e.stage === "vlm_batch");
    const read = docs.filter(d => ["processed", "failed"].includes(d.processing_status));
    const prompt = ((j.lead || {}).extraction_prompt || "").trim();
    const heading = (prompt.match(/under\s+(?:the\s+)?heading\s+["“']?([\w\s-]{2,40}?)["”']?(?:[.,;]|$)/i) || [])[1];
    const pt = f.phase_timings || {};
    const extractMs = pt.document_extraction_ms || 0;
    const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

    let body;
    if (batches.length) {
      body = `<ul class="why-list">${batches.map(e => {
        const m = e.metrics || {}, d = e.data || {};
        const tokens = (m.prompt_tokens || 0) + (m.completion_tokens || 0);
        const [where, what] = String(e.reason || "").split(": ");
        return `<li class="why-${esc(String(e.status).toLowerCase())}">
          <span class="why-dot" aria-hidden="true"></span>
          <div class="why-main">
            <b>${esc(where || "Model call")}</b>
            <span>${esc(what || e.reason || "")}${e.ms ? ` · ${fmtDur(e.ms)}` : ""}${tokens ? ` · ${tokens.toLocaleString("en-IN")} tokens` : ""}</span>
            ${d.raw_response ? `<details><summary>Model reply</summary><pre>${esc(d.raw_response)}</pre></details>` : ""}
          </div></li>`;
      }).join("")}</ul>`;
    } else {
      const hints = [];
      const tiny = read.find(d => (d.page_count || 0) <= 2);
      if (tiny) hints.push(`“${esc(tiny.filename)}” has only ${plural(tiny.page_count || 1, "page")} — it may not be the full document the prompt is looking for.`);
      if (heading) hints.push(`The prompt limits extraction to pages under the heading “${esc(heading.trim())}”. If no page carries that heading, the model is told to return nothing.`);
      const batchesEst = read.reduce((a, d) => a + Math.max(1, Math.ceil((d.page_count || 1) / 20)), 0);
      if (batchesEst && extractMs / batchesEst > 100000) {
        hints.push(`Reading took ${fmtDur(extractMs)} for about ${plural(batchesEst, "model call")} — at least one call probably hit the model's 120 s time limit.`);
      }
      body = `
        <ul class="why-list">${read.length ? read.map(d => `
          <li class="why-${d.processing_status === "failed" ? "fail" : "empty"}"><span class="why-dot" aria-hidden="true"></span>
            <div class="why-main"><b>${esc(d.filename)}</b>
              <span>${d.processing_status === "failed" ? `failed — ${esc(d.error_message || "model error")}` : "sent to the model — no values came back"} · ${plural(d.page_count || 1, "page")}</span></div></li>`).join("")
          : `<li class="why-empty"><span class="why-dot"></span><div class="why-main"><b>No document matched the prompt</b><span>nothing was sent to the model</span></div></li>`}
        </ul>
        ${hints.length ? `<div class="why-hints"><b>Likely reasons</b><ul>${hints.map(h => `<li>${h}</li>`).join("")}</ul></div>` : ""}
        <div class="why-note">This dossier was extracted before each model call's result was recorded, so the model's reply for it wasn't kept.
          Re-running it will record exactly what came back.</div>`;
    }
    return `
      <div class="why">
        <div class="why-head"><span class="why-ic" aria-hidden="true">!</span>
          <div><b>No values were extracted from this dossier</b>
            <span>${read.length ? `${plural(read.length, "document")} ${read.length === 1 ? "was" : "were"} sent to the model` : "No document was read"}${extractMs ? ` · ${fmtDur(extractMs)} reading` : ""}</span></div></div>
        ${body}
      </div>`;
  }

  /* ── lead drawer ───────────────────────── */
  function renderDrawer(j) {
    const f = j.final || {};
    const lead = j.lead || {};
    const status = liveStatus(lead.status, f.processing_status);
    $("#dLeadId").textContent = j.lead_id;
    const b = $("#dStatus"); b.className = "badge"; b.innerHTML = badge(status);
    $("#dRawLink").href = "/logs/legal/" + encodeURIComponent(j.lead_id);

    const raw = f.raw_extractions || {};
    const pageExtractions = Array.isArray(raw.page_extractions) ? raw.page_extractions : (Array.isArray(raw._page_extractions) ? raw._page_extractions : []);
    const leadMeta = raw.lead_metadata || {};
    const docs = j.documents || [];

    // 1. High-Level Summary Banner
    const summary = `
      <div class="lead-summary">
        <div class="ls-cell"><div class="ls-k">Account LAN</div><div class="ls-v mono">${esc(f.account_no_lan || lead.account_lan || "—")}</div></div>
        <div class="ls-cell"><div class="ls-k">Primary Borrower</div><div class="ls-v">${esc(f.borrower_name || f.applicant_name || lead.lead_name || "—")}</div></div>
        <div class="ls-cell"><div class="ls-k">Sanction Amount</div><div class="ls-v mono">${fmtINR(f.sanction_amount)}</div></div>
        <div class="ls-cell"><div class="ls-k">Total Outstanding (TOS)</div><div class="ls-v mono" style="font-weight:700">${fmtINR(f.tos)}</div></div>
      </div>`;

    // 1.5 Model Execution & Token Telemetry Banner
    const tel = f.telemetry || raw.telemetry || raw._telemetry || lead.telemetry || {};
    const totalTokens = (tel.total_tokens != null) ? tel.total_tokens.toLocaleString() : (tel.prompt_tokens != null && tel.completion_tokens != null ? (tel.prompt_tokens + tel.completion_tokens).toLocaleString() : "—");
    const promptTokens = (tel.prompt_tokens != null) ? tel.prompt_tokens.toLocaleString() : "—";
    const completionTokens = (tel.completion_tokens != null) ? tel.completion_tokens.toLocaleString() : "—";
    const pt = f.phase_timings || leadMeta.phase_timings || {};
    const vlmTime = tel.vlm_latency_ms ? `${(tel.vlm_latency_ms / 1000).toFixed(2)}s (${tel.vlm_latency_ms}ms)` : (pt.document_extraction_ms ? `${(pt.document_extraction_ms / 1000).toFixed(2)}s` : "—");
    const totalTime = tel.pipeline_latency_ms ? `${(tel.pipeline_latency_ms / 1000).toFixed(2)}s` : (pt.total_pipeline_ms ? `${(pt.total_pipeline_ms / 1000).toFixed(2)}s` : "—");
    const modelName = tel.model || "Medha VLM";

    const telemetryBanner = `
      <div class="d-section" style="margin-top: 16px;">
        <div style="background: linear-gradient(135deg, var(--surface), var(--surface-2)); border: 1px solid var(--line); border-radius: 8px; padding: 14px 18px;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; border-bottom: 1px solid var(--line-light); padding-bottom: 8px;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 15px;">⚡</span>
              <span style="font-size: 12px; font-weight: 700; color: var(--ink); text-transform: uppercase; letter-spacing: 0.05em;">Model Execution &amp; Token Telemetry</span>
            </div>
            <span class="tag ok" style="font-size: 11px; font-weight: 600; padding: 3px 8px;">API Engine: ${esc(modelName)}</span>
          </div>
          <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px;">
            <div style="background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 10px 12px;">
              <div style="font-size: 10.5px; color: var(--ink-faint); text-transform: uppercase; font-weight: 600; margin-bottom: 4px;">VLM Latency</div>
              <div class="mono" style="font-size: 13.5px; font-weight: 700; color: var(--ink);">${esc(vlmTime)}</div>
            </div>
            <div style="background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 10px 12px;">
              <div style="font-size: 10.5px; color: var(--ink-faint); text-transform: uppercase; font-weight: 600; margin-bottom: 4px;">Total Tokens</div>
              <div class="mono" style="font-size: 13.5px; font-weight: 700; color: var(--accent);">${esc(String(totalTokens))}</div>
            </div>
            <div style="background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 10px 12px;">
              <div style="font-size: 10.5px; color: var(--ink-faint); text-transform: uppercase; font-weight: 600; margin-bottom: 4px;">Prompt Tokens</div>
              <div class="mono" style="font-size: 13px; font-weight: 600; color: var(--ink-soft);">${esc(String(promptTokens))}</div>
            </div>
            <div style="background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 10px 12px;">
              <div style="font-size: 10.5px; color: var(--ink-faint); text-transform: uppercase; font-weight: 600; margin-bottom: 4px;">Completion Tokens</div>
              <div class="mono" style="font-size: 13px; font-weight: 600; color: var(--ink-soft);">${esc(String(completionTokens))}</div>
            </div>
          </div>
        </div>
      </div>`;

    // 2. Comprehensive Lead Metadata Grid
    const timingStr = Object.entries(pt).map(([k, ms]) => `${k.replace('_ms','').replace('phase','')}: ${ms}ms`).join(" · ") || "—";
    const routesStr = (f.ocr_routes_used || leadMeta.ocr_routes_used || []).join(", ") || "Medha VLM";
    const totalPgs = docs.reduce((acc, d) => acc + (d.page_count || 1), 0);

    const metaSection = `
      <div class="d-section">
        <h4>Lead Metadata &amp; Pipeline Performance <span class="n">Dossier Telemetry</span></h4>
        <div class="meta-grid">
          <div class="meta-card"><div class="mc-k">Lead ID</div><div class="mc-v mono">${esc(j.lead_id)}</div></div>
          <div class="meta-card"><div class="mc-k">Discovered LAN</div><div class="mc-v mono">${esc(f.account_no_lan || lead.account_lan || "—")}</div></div>
          <div class="meta-card"><div class="mc-k">Ingest Batch</div><div class="mc-v mono">${esc(lead.batch_id || "—")}</div></div>
          <div class="meta-card"><div class="mc-k">Scope</div><div class="mc-v">${lead.is_test ? '<span class="tag warn">TEST</span>' : '<span class="tag ok">LIVE</span>'}</div></div>
          <div class="meta-card"><div class="mc-k">Dossier Documents</div><div class="mc-v">${docs.length} files (${totalPgs} total pages)</div></div>
          <div class="meta-card"><div class="mc-k">Title Verification</div><div class="mc-v">${propBadge(f.property_verification_status)}</div></div>
          <div class="meta-card"><div class="mc-k">OCR Routing Engines</div><div class="mc-v mono" style="font-size:11.5px">${esc(routesStr)}</div></div>
          <div class="meta-card"><div class="mc-k">Phase Timings</div><div class="mc-v mono" style="font-size:11px">${esc(timingStr)}</div></div>
        </div>
      </div>`;

    // 2.5 Consolidated Dossier Extractions
    const priorityCols = [
      "borrower_name", "borrower_address",
      "co_borrower_1_name", "co_borrower_1_address",
      "co_borrower_2_name", "co_borrower_2_address",
      "co_borrower_3_name", "co_borrower_3_address",
      "applicant_name", "applicant_address",
      "account_no_lan", "sanction_amount", "sanction_date", "disbursal_date", "roi_in_number", "npa_date",
      "future_principal", "principal_overdue", "interest_overdue", "interest_on_termination",
      "late_payment_penal", "cheque_bounce_inc_gst", "other_charges_inc_gst", 
      "foreclosure_charges", "litigation_charges", "tos",
      "mortgaged_property_detail_1", "directions", "property_owner_mortgagor"
    ];

    const customFields = (f.extracted_data && f.extracted_data.custom_fields) ? f.extracted_data.custom_fields : {};
    const flatF = { ...f, ...customFields };
    // internal bookkeeping never belongs in the value table — the page sections below show
    // the per-page extractions, and the raw model output lives in the page preview modal
    const NOT_A_VALUE = [
      "lead_id", "status", "processing_status", "raw_extractions", "extracted_data", "telemetry",
      "phase_timings", "updated_at", "created_at", "page_extractions", "cited_pages",
      "field_scripts", "ocr_routes_used", "flags", "confidence_score", "is_test", "summary",
      "raw_response", "raw_ocr_text", "ocr_route", "ocr_confidence", "page_number", "mismatches",
    ];
    const allExtractKeys = Object.keys(flatF).filter(k =>
      !k.startsWith("_") &&
      !NOT_A_VALUE.includes(k) &&
      !k.endsWith("_page_sources") &&
      typeof flatF[k] !== "object" &&
      flatF[k] != null && flatF[k] !== "" && flatF[k] !== "—"
    );

    const sortedExtractKeys = allExtractKeys.sort((a, b) => {
      const idxA = priorityCols.indexOf(a);
      const idxB = priorityCols.indexOf(b);
      if (idxA !== -1 && idxB !== -1) return idxA - idxB;
      if (idxA !== -1) return -1;
      if (idxB !== -1) return 1;
      return a.localeCompare(b);
    });

    let consolidatedHtml = "";
    if (sortedExtractKeys.length > 0) {
      consolidatedHtml = `
        <div class="d-section" style="margin-top:20px;">
          <h4>Consolidated Dossier Extractions <span class="n">${sortedExtractKeys.length} fields resolved</span></h4>
          <table class="page-fields-table" style="width:100%; border-collapse:collapse; margin-top:12px; background:var(--surface); border:1px solid var(--line); border-radius:6px; overflow:hidden;">
            <tbody>
              ${sortedExtractKeys.map(k => `
                <tr>
                  <td class="pft-k" style="padding:10px 14px; border-bottom:1px solid var(--line-light); vertical-align:top; width:220px; font-weight:600;">${esc(formatFieldLabel(k))}</td>
                  <td class="pft-v${isFinancialField(k) ? ' mono' : ''}" style="padding:10px 14px; border-bottom:1px solid var(--line-light); font-weight:500; ${k.toLowerCase().includes('address') ? 'white-space:normal; line-height:1.45;' : ''}">${formatFieldValue(k, flatF[k])}</td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>`;
    }

    // 3. Document & Page-by-Page Extraction Provenance Section (ONLY EXACT PAGES WITH EXTRACTED DATA)
    const docMap = new Map();
    docs.forEach(d => docMap.set(d.document_id, { ...d }));
    pageExtractions.forEach(p => {
      if (p.document_id && !docMap.has(p.document_id)) {
        docMap.set(p.document_id, {
          document_id: p.document_id,
          filename: p.filename || "Document",
          document_type: "document",
          page_count: p.total_pages || 1
        });
      }
    });
    const allDocs = Array.from(docMap.values());

    function getPageRealFields(p) {
      if (!p || !p.fields || typeof p.fields !== 'object') return {};
      const res = {};
      for (const [k, v] of Object.entries(p.fields)) {
        if (!k.startsWith("_") && v != null && v !== "" && v !== "—") {
          res[k] = v;
        }
      }
      return res;
    }

    const docsWithPages = allDocs.filter(d => {
      const dPages = pageExtractions.filter(p => {
        const isDoc = p.document_id === d.document_id || p.filename === d.filename;
        return isDoc && Object.keys(getPageRealFields(p)).length > 0;
      });
      return dPages.length > 0;
    });
    const docsNoPages = allDocs.filter(d => !docsWithPages.includes(d));

    // Each cited page is shown as the page itself next to exactly what was read off it.
    // The raw model output stays one click away, in the page preview modal.
    const docProvenanceSection = `
      <div class="d-section">
        <h4>Pages &amp; what was read from them
          <span class="n" id="provSummary">${docsWithPages.length} document${docsWithPages.length === 1 ? '' : 's'} cited</span></h4>
        <p class="lede" style="margin-bottom:14px">Every value sits beside the page it came from. Click a page to open it full size with the raw model output.</p>
        ${docsWithPages.length === 0 ? emptyDiagnosis(j, docs, f) : ""}
        ${docsWithPages.map(d => {
          const dPages = pageExtractions.filter(p => {
            const isDoc = p.document_id === d.document_id || p.filename === d.filename;
            return isDoc && Object.keys(getPageRealFields(p)).length > 0;
          });

          dPages.forEach(p => {
            window.__pageData[`${d.document_id}-${p.page_number}`] = p;
          });

          return `
            <div class="doc-provenance-box" id="doc-${esc(d.document_id)}">
              <div class="doc-prov-head">
                <div class="doc-prov-title">
                  <span class="dp-file">${esc(d.filename)}</span>
                  <span class="tag" style="background:var(--surface);border:1px solid var(--line-strong)">${esc(d.document_type || "document")}</span>
                </div>
                <div class="doc-prov-meta">
                  <span>${dPages.length} of ${d.page_count || 1} page${(d.page_count || 1) === 1 ? "" : "s"} cited</span>
                  <span>·</span>
                  <span>${formatBytes(d.file_size_bytes)}</span>
                  <a class="btn line small" href="/api/legal/document/${encodeURIComponent(d.document_id)}/file" target="_blank" style="margin-left:6px">Open file ↗</a>
                </div>
              </div>
              ${dPages.map(p => {
                const realFields = getPageRealFields(p);
                const keys = Object.keys(realFields);
                return `
                <div class="pg-row" id="page-${esc(d.document_id)}-${p.page_number}">
                  <button type="button" class="pg-shot" data-open-page="1" data-doc="${esc(d.document_id)}"
                          data-page="${p.page_number}" data-pages="${d.page_count || 1}" data-file="${esc(d.filename)}"
                          title="Open page ${p.page_number} full size with the raw extraction">
                    <span class="pg-shot-badge">Page ${p.page_number}<span class="of"> / ${d.page_count || 1}</span></span>
                    <img class="pg-shot-img" loading="lazy" alt="Page ${p.page_number} of ${esc(d.filename)}"
                         src="/api/legal/document/${encodeURIComponent(d.document_id)}/page/${encodeURIComponent(p.page_number)}"
                         onerror="this.closest('.pg-shot').classList.add('no-img')">
                    <span class="pg-shot-hint">Open full page ↗</span>
                  </button>
                  <div class="pg-panel">
                    <div class="pg-panel-head">
                      <h5>Read from this page</h5>
                      <span class="pg-count">${keys.length} field${keys.length === 1 ? "" : "s"}</span>
                    </div>
                    <table class="pg-kv">
                      <tbody>
                        ${keys.map(fk => `
                          <tr>
                            <td class="pg-k">${esc(formatFieldLabel(fk))}</td>
                            <td class="pg-v${isFinancialField(fk) ? " mono" : ""}">${formatFieldValue(fk, realFields[fk])}<span class="s-chip" data-prov="${p.page_number}|${esc(fk)}" hidden></span></td>
                          </tr>`).join("")}
                      </tbody>
                    </table>
                    <div class="pg-panel-foot">
                      <span class="page-route-badge">${esc(p.ocr_route || p.phase || "Vision model")}</span>
                      <span class="pg-foot-hint">Raw output in the page preview</span>
                    </div>
                  </div>
                </div>`;
              }).join("")}
            </div>`;
        }).join("")}
        ${docsNoPages.length > 0 ? `
          <div class="pg-unread">
            <div class="pg-unread-k">In the dossier — not cited by this extraction</div>
            ${docsNoPages.map(d => `
              <div class="pg-unread-row">
                <span class="pg-unread-file">${esc(d.filename)}</span>
                <span class="tag">${esc(d.document_type || "document")}</span>
                <span class="pg-unread-meta">${d.page_count || 1} page${(d.page_count || 1) === 1 ? "" : "s"} · ${formatBytes(d.file_size_bytes)}</span>
                <a class="btn line small" href="/api/legal/document/${encodeURIComponent(d.document_id)}/file" target="_blank">Open ↗</a>
              </div>`).join("")}
          </div>` : ""}
      </div>`;

    // 4. Execution Event Logs
    let eventLogsHtml = "";
    const events = j.journey || [];
    if (events.length > 0) {
      eventLogsHtml = `
        <div class="d-section" style="margin-top:20px;">
          <h4>Pipeline Event Logs <span class="n">Detailed execution trace</span></h4>
          <div style="background:var(--surface); border:1px solid var(--line); border-radius:6px; margin-top:12px; max-height:400px; overflow-y:auto;">
            <table class="page-fields-table" style="width:100%; border-collapse:collapse; font-size:12.5px;">
              <thead style="background:var(--surface-2); position:sticky; top:0; box-shadow:0 1px 0 var(--line);">
                <tr>
                  <th style="text-align:left; padding:8px 12px; font-weight:600; color:var(--ink-faint); border-bottom:1px solid var(--line);">Time</th>
                  <th style="text-align:left; padding:8px 12px; font-weight:600; color:var(--ink-faint); border-bottom:1px solid var(--line);">Stage</th>
                  <th style="text-align:left; padding:8px 12px; font-weight:600; color:var(--ink-faint); border-bottom:1px solid var(--line);">Status</th>
                  <th style="text-align:left; padding:8px 12px; font-weight:600; color:var(--ink-faint); border-bottom:1px solid var(--line);">Duration</th>
                  <th style="text-align:left; padding:8px 12px; font-weight:600; color:var(--ink-faint); border-bottom:1px solid var(--line);">Details</th>
                </tr>
              </thead>
              <tbody>
                ${events.map(e => `
                  <tr>
                    <td class="mono" style="padding:8px 12px; border-bottom:1px solid var(--line-light); color:var(--ink-soft);">${e.ts ? e.ts.split("T")[1].substring(0, 12) : "—"}</td>
                    <td style="padding:8px 12px; border-bottom:1px solid var(--line-light); font-weight:500;">${esc(e.stage)}</td>
                    <td style="padding:8px 12px; border-bottom:1px solid var(--line-light);">
                      <span class="tag ${e.status === 'PASS' || e.status === 'DONE' ? 'ok' : (e.status === 'FAIL' || e.status === 'ERROR' ? 'bad' : '')}" style="font-size:10px;">${esc(e.status)}</span>
                    </td>
                    <td class="mono" style="padding:8px 12px; border-bottom:1px solid var(--line-light); color:var(--ink-soft);">${e.ms ? e.ms + 'ms' : '—'}</td>
                    <td style="padding:8px 12px; border-bottom:1px solid var(--line-light); color:var(--ink-strong); word-break:break-word;">
                      ${e.document_id ? `<span class="mono" style="font-size:10.5px; background:var(--surface-2); padding:2px 4px; border-radius:3px; margin-right:4px;">doc:${e.document_id.substring(0,6)}</span>` : ""}
                      ${esc(e.reason || "—")}
                    </td>
                  </tr>
                `).join("")}
              </tbody>
            </table>
          </div>
        </div>
      `;
    }

    $("#drawerBody").innerHTML = summary + telemetryBanner + metaSection + consolidatedHtml + docProvenanceSection + eventLogsHtml;
  }

  /* ── typed / handwritten chips ───────────────
     Worked out at read time from the model's own per-field flag (runs extracted since it was
     asked for) and, failing that, the document's text layer. Loaded after the drawer paints
     so the dossier never waits on it. */
  const SCRIPT_LABEL = {
    handwritten: ["Handwritten", "The model read this value as handwriting"],
    scanned: ["Scanned", "Not in the document's text layer — read from the page image, so it may be handwritten"],
    typed: ["Typed", "Found in the document's own text layer"],
    printed: ["Typed", "The model read this value as machine print"],
    unknown: ["Unchecked", "The page could not be checked"],
    none: ["No values", "The extraction returned no values for this dossier — open it to see why"],
  };

  async function loadLeadProvenance(leadId) {
    let data;
    try {
      const r = await fetch(`/api/legal/lead/${encodeURIComponent(leadId)}/provenance`);
      if (!r.ok) return;
      data = await r.json();
    } catch (e) { return; }
    if ($("#dLeadId").textContent !== leadId) return;      // the drawer moved on
    const fields = data.fields || {};
    $$("#drawerBody .s-chip[data-prov]").forEach(chip => {
      const info = fields[chip.dataset.prov];
      const key = (info && info.script) || "unknown";
      const [label, why] = SCRIPT_LABEL[key] || SCRIPT_LABEL.unknown;
      chip.className = `s-chip s-${key === "printed" ? "typed" : key}`;
      chip.textContent = label;
      chip.title = info && info.source === "model" ? `${why} (model)` : why;
      chip.hidden = false;
    });
    const c = data.counts || {};
    const parts = [];
    if (c.handwritten) parts.push(`${c.handwritten} handwritten`);
    if (c.scanned) parts.push(`${c.scanned} scanned`);
    const typed = (c.typed || 0) + (c.printed || 0);
    if (typed) parts.push(`${typed} typed`);
    const sum = $("#provSummary");
    if (sum && parts.length) sum.textContent = parts.join(" · ");
    const badgeHost = $("#dScriptTag");
    if (badgeHost) badgeHost.innerHTML = scriptChip(data.tag);
  }

  const scriptChip = (tag) => {
    const key = tag && SCRIPT_LABEL[tag] ? tag : "unknown";
    const [label, why] = SCRIPT_LABEL[key];
    return `<span class="s-chip s-${key === "printed" ? "typed" : key}" title="${esc(why)}">${label}</span>`;
  };

  async function openLead(id) {
    try {
      const r = await fetch("/api/legal/lead/" + encodeURIComponent(id));
      if (!r.ok) { toast("Lead not found: " + esc(id), "bad"); return; }
      renderDrawer(await r.json());
      $("#drawer").classList.add("open"); $("#scrim").classList.add("open");
      loadLeadProvenance(id);
    } catch (e) { toast("Failed to open lead", "bad"); }
  }
  window.openLead = openLead;

  function closeDrawer() {
    $("#drawer").classList.remove("open"); $("#scrim").classList.remove("open");
  }

  /* ── dropzone helper ─────────────────────── */
  function wireDrop(zoneSel, inputSel, onPick) {
    const zone = $(zoneSel), input = $(inputSel);
    if (!zone || !input) return;
    zone.onclick = () => input.click();
    input.onchange = () => input.files && input.files.length && onPick(input.files);
    ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, e => {
      e.preventDefault(); zone.classList.add("drag");
    }));
    ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, e => {
      e.preventDefault(); zone.classList.remove("drag");
    }));
    zone.addEventListener("drop", e => {
      const f = e.dataTransfer.files;
      if (f && f.length) {
        input.files = f;
        onPick(f);
      }
    });
  }

  /* ── batch polling & ingestion ──────────── */
  let pollTimer = null;

  let lastCompleted = -1;

  function startBatchPoll(batchId) {
    $("#runPanel").classList.remove("hidden");
    $("#doneBanner").classList.add("hidden");
    $("#liveList").innerHTML = "";
    $("#progBar").style.width = "0%";
    $("#dlBtn").href = `/api/legal/download/${encodeURIComponent(batchId)}`;
    $("#excelDlBtn").href = `/api/legal/download/${encodeURIComponent(batchId)}/excel`;

    const btnPause = $("#btnBatchPause"), btnResume = $("#btnBatchResume"), btnCancel = $("#btnBatchCancel");
    const doAction = async (action) => {
      try {
        await fetch(`/api/legal/batch/${encodeURIComponent(batchId)}/action`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action })
        });
        toast(`Batch ${action}d`, "ok");
      } catch (e) { toast(`Failed to ${action} batch`, "bad"); }
    };
    if (btnPause) btnPause.onclick = () => doAction("pause");
    if (btnResume) btnResume.onclick = () => doAction("resume");
    if (btnCancel) btnCancel.onclick = () => doAction("cancel");

    if (pollTimer) clearInterval(pollTimer);
    const poll = async () => {
      try {
        const p = await (await fetch(`/api/legal/batch/${encodeURIComponent(batchId)}`)).json();
        const total = p.total_leads || 0;
        // finished = every end state, including "missing documents" — otherwise the bar
        // never reaches 100% for a batch where one dossier had nothing to read
        const done = p.leads_finished != null ? p.leads_finished
          : (p.leads_completed || 0) + (p.leads_partial || 0) + (p.leads_missing || 0) + (p.leads_failed || 0);
        $("#progBar").style.width = (total ? (done / total * 100) : 0) + "%";
        
        const isPaused = (p.leads_paused || 0) > 0;
        if (btnPause) btnPause.classList.toggle("hidden", isPaused);
        if (btnResume) btnResume.classList.toggle("hidden", !isPaused);

        let progStatus = "Queued";
        if (isPaused) progStatus = `Paused...`;
        else if (p.leads_processing > 0) progStatus = `Extracting ${p.leads_processing} dossier(s)...`;
        else if (done < total && p.leads_pending > 0) progStatus = `Pending ${p.leads_pending} dossier(s)...`;
        else if (done === total && total > 0) progStatus = "Finalizing data...";
        
        $("#progText").textContent = `${done} / ${total} dossiers · ${progStatus}`;
        $("#progElapsed").textContent = p.leads_failed ? `${p.leads_failed} failed` : "";

        // Compute average processing time from completed leads
        let totalMs = 0;
        let completedCount = 0;
        (p.leads || []).forEach(x => {
            if (x.phase_timings && typeof x.phase_timings === 'object') {
                let ms = Object.values(x.phase_timings).reduce((sum, val) => sum + (Number(val) || 0), 0);
                if (ms > 0) {
                    totalMs += ms;
                    completedCount++;
                }
            }
        });
        const avgMs = completedCount > 0 ? (totalMs / completedCount).toFixed(0) : 0;
        const avgText = avgMs > 0 ? ` · Avg: ${avgMs}ms / dossier` : "";
        
        $("#progText").textContent = `${done} / ${total} dossiers · ${progStatus}${avgText}`;
        $("#progElapsed").textContent = p.leads_failed ? `${p.leads_failed} failed` : "";

        const newMiniCountsHTML = `
          <div class="mini ok"><div class="mn">${p.leads_completed || 0}</div><div class="ml">completed</div></div>
          <div class="mini warn"><div class="mn">${p.leads_partial || 0}</div><div class="ml">partial</div></div>
          <div class="mini bad"><div class="mn">${p.leads_failed || 0}</div><div class="ml">failed</div></div>
          <div class="mini warn"><div class="mn">${p.leads_missing || 0}</div><div class="ml">missing docs</div></div>
          <div class="mini gray"><div class="mn">${(p.leads_pending || 0) + (p.leads_paused || 0) + (p.leads_processing || 0)}</div><div class="ml">in progress</div></div>`;
        if ($("#miniCounts").innerHTML !== newMiniCountsHTML) $("#miniCounts").innerHTML = newMiniCountsHTML;

        const liveList = $("#liveList");
        
        // Remove rows that no longer exist (optional, mostly they just get added or updated)
        const currentIds = new Set((p.leads || []).map(x => x.lead_id));
        $$(".live-row", liveList).forEach(el => {
            if (!currentIds.has(el.dataset.id)) el.remove();
        });

        (p.leads || []).forEach(x => {
          const st = liveStatus(x.status, x.result_status);
          const totalDocs = x.total_documents || 0;
          const processedDocs = x.processed_documents || 0;
          
          let pipelineProg = "";
          let stageLabel = "Waiting in queue";
          
          if (st === "processing") {
              if (processedDocs < totalDocs) {
                  stageLabel = `Extracting doc ${processedDocs + 1} of ${totalDocs} (Vision & OCR)...`;
              } else {
                  stageLabel = `Merging data & validation pass...`;
              }
              pipelineProg = `<span class="spinner" style="width:12px;height:12px;margin-right:4px;border-width:2px;vertical-align:-1px;"></span><span style="color:var(--text);font-weight:500;">${stageLabel}</span>`;
          } else if (st === "pending") {
              pipelineProg = `<span style="color:var(--text-muted);">Waiting in queue...</span>`;
          } else if (st === "paused") {
              pipelineProg = `<span style="color:var(--text-muted);">Paused by user</span>`;
          } else {
              // Finished
              let timings = [];
              if (x.phase_timings && typeof x.phase_timings === 'object') {
                  for (const [k, v] of Object.entries(x.phase_timings)) {
                      timings.push(`${k.replace('_ms', '')}: ${v}ms`);
                  }
              }
              const timingStr = timings.join(" · ");
              const reviewStr = (x.flags && x.flags.length > 0) ? `<span style="color:var(--ch-warn); margin-left:6px;">(${x.flags.length} flag${x.flags.length>1?'s':''})</span>` : "";
              pipelineProg = `<span style="color:var(--text-muted); font-size:11px;">${timingStr ? 'Pipeline: ' + timingStr : 'Pipeline complete'}</span>${reviewStr}`;
          }
          
          const docProg = totalDocs > 0 ? `<span style="font-size: 11px; color: var(--text-muted); margin-left: 8px;">[${processedDocs}/${totalDocs} docs OCR'd]</span>` : "";
          
          let existingRow = liveList.querySelector(`[data-id="${x.lead_id}"]`);
          if (!existingRow) {
              existingRow = document.createElement("div");
              existingRow.className = "live-row";
              existingRow.dataset.id = x.lead_id;
              existingRow.style.cssText = "display: flex; align-items: center; gap: 12px; padding: 12px; border-bottom: 1px solid var(--line-light); cursor: pointer;";
              existingRow.onclick = () => openLead(x.lead_id);
              liveList.appendChild(existingRow);
          }
          
          const newHtml = `
              ${badge(st)}
              <div style="flex: 1;">
                <div style="display: flex; align-items: baseline;">
                  <span class="lr-id mono" style="font-weight:600; margin-right: 8px;">${esc(x.lead_id)}</span>
                  <span class="lr-out" style="color: var(--text-strong);">${esc(x.applicant_name || x.folder_name || "")} ${x.tos ? `· TOS: ${fmtINR(x.tos)}` : ""}</span>
                  ${docProg}
                </div>
                ${pipelineProg ? `<div style="margin-top: 4px; font-size: 12px;">${pipelineProg}</div>` : ""}
              </div>`;
              
          if (existingRow.innerHTML !== newHtml) {
              existingRow.innerHTML = newHtml;
          }
        });

        // update dashboard stats to reflect progress
        loadStats();
        // only reload the big table if the number of completed leads changed
        if (done !== lastCompleted) {
            loadLeads();
            lastCompleted = done;
        }

        if (total > 0 && p.leads_pending === 0 && p.leads_processing === 0 && (p.leads_paused || 0) === 0) {
          clearInterval(pollTimer); pollTimer = null;
          lastCompleted = -1;
          $("#progBar").style.width = "100%";
          $("#doneBanner").classList.remove("hidden");
          if (btnPause) btnPause.classList.add("hidden");
          if (btnResume) btnResume.classList.add("hidden");
          if (btnCancel) btnCancel.classList.add("hidden");
          const runZip = $("#runZip"), runDoc = $("#runDoc");
          if (runZip) { runZip.disabled = false; runZip.textContent = "Process Ingest"; }
          if (runDoc) { runDoc.disabled = false; runDoc.textContent = "Process Dossier"; }
          const notClean = (p.leads_partial || 0) + (p.leads_missing || 0) + (p.leads_failed || 0);
          toast(`✓ Batch finished — ${p.leads_completed || 0} completed` +
                (notClean ? ` · ${notClean} need a look` : ""), notClean ? "" : "ok");
        }
      } catch (e) {}
    };
    poll();
    pollTimer = setInterval(poll, 1500);
  }

  /* ── recursive directory traversal for folder drag-and-drop ── */
  async function getFilesFromDataTransfer(dataTransfer) {
    const items = dataTransfer.items;
    const filesWithPaths = [];

    async function traverseEntry(entry, path = "") {
      if (entry.isFile) {
        const file = await new Promise((res, rej) => entry.file(res, rej));
        filesWithPaths.push({ file, path: path + file.name });
      } else if (entry.isDirectory) {
        const dirReader = entry.createReader();
        const entries = await new Promise((res, rej) => {
          const results = [];
          function readBatch() {
            dirReader.readEntries(batch => {
              if (!batch.length) {
                res(results);
              } else {
                results.push(...batch);
                readBatch();
              }
            }, rej);
          }
          readBatch();
        });
        for (const child of entries) {
          await traverseEntry(child, path + entry.name + "/");
        }
      }
    }

    if (items && items.length > 0 && items[0].webkitGetAsEntry) {
      for (let i = 0; i < items.length; i++) {
        const entry = items[i].webkitGetAsEntry();
        if (entry) {
          await traverseEntry(entry);
        }
      }
    } else if (dataTransfer.files && dataTransfer.files.length > 0) {
      for (let i = 0; i < dataTransfer.files.length; i++) {
        const f = dataTransfer.files[i];
        filesWithPaths.push({ file: f, path: f.webkitRelativePath || f.name });
      }
    }
    return filesWithPaths;
  }

  function uploadWithProgress(url, fd, btn, baseText) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url);
      xhr.timeout = 600000; // 10 minute timeout for large legal batch archives
      xhr.upload.onprogress = e => {
        if (e.lengthComputable) {
          const pct = Math.round((e.loaded / e.total) * 100);
          btn.style.setProperty("--progress", pct + "%");
          btn.classList.add("btn-progress");
          if (pct < 100) {
            btn.innerHTML = `<span class="spinner"></span> Uploading ${pct}%`;
          } else {
            btn.innerHTML = `<span class="spinner"></span> ${baseText}`;
          }
        }
      };
      xhr.onload = () => {
        btn.classList.remove("btn-progress");
        if (xhr.status === 401) {
          window.location.href = "/login?next=" + encodeURIComponent(location.pathname);
          reject(new Error("Session expired. Please log in again."));
          return;
        }
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch (_) {}
        if (xhr.status >= 400) {
          const msg = (data && (data.error || data.detail)) || `Server error (${xhr.status}): ${xhr.statusText || "Upload failed"}`;
          reject(new Error(msg));
          return;
        }
        if (data !== null) {
          resolve({ json: () => Promise.resolve(data) });
        } else {
          reject(new Error(`Invalid server response: ${xhr.responseText.slice(0, 120)}`));
        }
      };
      xhr.onerror = () => {
        btn.classList.remove("btn-progress");
        reject(new Error("Connection error during upload. Check network connection or server status."));
      };
      xhr.ontimeout = () => {
        btn.classList.remove("btn-progress");
        reject(new Error("Upload timed out. Try uploading a smaller zip or check server load."));
      };
      xhr.send(fd);
    });
  }

  function initUploads() {
    let selectedBatchType = null; // "zip" | "folder"
    let selectedZipFile = null;
    let selectedFolderFiles = []; // array of { file, path }

    const zipDrop = $("#zipDrop");
    const zipInput = $("#zipFile");
    const folderInput = $("#folderInput");
    const runZip = $("#runZip");

    // 1. Pick Folder button
    const btnPickFolder = $("#btnPickFolder");
    if (btnPickFolder && folderInput) {
      btnPickFolder.onclick = () => folderInput.click();
      folderInput.onchange = () => {
        if (!folderInput.files || !folderInput.files.length) return;
        selectedFolderFiles = Array.from(folderInput.files).map(f => ({
          file: f,
          path: f.webkitRelativePath || f.name
        }));
        selectedZipFile = null;
        selectedBatchType = "folder";
        const leadFolders = new Set(selectedFolderFiles.map(x => x.path.split('/')[0]).filter(Boolean));
        if ($("#zipName")) $("#zipName").textContent = `📁 Folder: ${selectedFolderFiles.length} file(s) selected`;
        if ($("#zipSub")) $("#zipSub").textContent = `Spans ${leadFolders.size || 1} directory folder(s)`;
        if (runZip) { runZip.disabled = false; runZip.textContent = "Process Ingest"; }
      };
    }

    // 2. Pick ZIP button
    const btnPickZip = $("#btnPickZip");
    if (btnPickZip && zipInput) {
      btnPickZip.onclick = () => zipInput.click();
      zipInput.onchange = () => {
        const f = zipInput.files && zipInput.files[0];
        if (!f) return;
        selectedZipFile = f;
        selectedFolderFiles = [];
        selectedBatchType = "zip";
        if ($("#zipName")) $("#zipName").textContent = `🗜️ ZIP: ${f.name} (${(f.size / 1024 / 1024).toFixed(2)} MB)`;
        if ($("#zipSub")) $("#zipSub").textContent = "Archive selected — ready to extract and process";
        if (runZip) { runZip.disabled = false; runZip.textContent = "Process Ingest"; }
      };
    }

    // 3. Batch Dropzone (supports both dropped folders and dropped ZIPs)
    if (zipDrop) {
      zipDrop.addEventListener("click", e => {
        // Don't trigger if clicking buttons/inputs inside the dropzone
        if (e.target.closest("button") || e.target.closest("input")) return;
        if (folderInput) folderInput.click();
      });
      ["dragenter", "dragover"].forEach(ev => zipDrop.addEventListener(ev, e => {
        e.preventDefault(); zipDrop.classList.add("drag");
      }));
      ["dragleave", "drop"].forEach(ev => zipDrop.addEventListener(ev, e => {
        e.preventDefault(); zipDrop.classList.remove("drag");
      }));
      zipDrop.addEventListener("drop", async e => {
        e.preventDefault(); zipDrop.classList.remove("drag");
        try {
          const items = await getFilesFromDataTransfer(e.dataTransfer);
          if (!items || !items.length) return;
          if (items.length === 1 && items[0].file.name.toLowerCase().endsWith(".zip")) {
            selectedZipFile = items[0].file;
            selectedFolderFiles = [];
            selectedBatchType = "zip";
              if ($("#zipName")) $("#zipName").textContent = `🗜️ Dropped ZIP: ${selectedZipFile.name} (${(selectedZipFile.size / 1024 / 1024).toFixed(2)} MB)`;
              if ($("#zipSub")) $("#zipSub").textContent = "Archive dropped — ready to extract and process";
          } else {
            selectedFolderFiles = items;
            selectedZipFile = null;
            selectedBatchType = "folder";
            const leadFolders = new Set(selectedFolderFiles.map(x => x.path.split('/')[0]).filter(Boolean));
          if ($("#zipName")) $("#zipName").textContent = `📁 Dropped Folder: ${selectedFolderFiles.length} file(s)`;
          if ($("#zipSub")) $("#zipSub").textContent = `Spans ${leadFolders.size || 1} directory folder(s)`;
          }
          if (runZip) { runZip.disabled = false; runZip.textContent = "Process Ingest"; }
        } catch (err) {
          toast("Failed to read dropped files: " + err, "bad");
        }
      });
    }

    // 4. Run Ingest
    if (runZip) {
      runZip.onclick = async () => {
        if (!selectedBatchType) return;
        const fd = new FormData();
        if (selectedBatchType === "zip" && selectedZipFile) {
          fd.append("zip_file", selectedZipFile);
          const zipBase = selectedZipFile.name.replace(/\.zip$/i, "");
          fd.append("batch_name", zipBase);
        } else if (selectedBatchType === "folder" && selectedFolderFiles.length) {
          const firstPath = selectedFolderFiles[0].path || "";
          const parts = firstPath.split(/[\/\\]/).filter(Boolean);
          const folderName = parts.length > 1 ? parts[0] : (selectedFolderFiles[0].file && selectedFolderFiles[0].file.webkitRelativePath ? selectedFolderFiles[0].file.webkitRelativePath.split(/[\/\\]/)[0] : "");
          if (folderName) fd.append("batch_name", folderName);
          for (let i = 0; i < selectedFolderFiles.length; i++) {
            fd.append("files", selectedFolderFiles[i].file);
            fd.append("paths", selectedFolderFiles[i].path);
          }
        } else {
          return;
        }
        if (state.scope === "test") fd.append("test", "on");
        
        const ep = document.getElementById("extractionPrompt");
        if (ep && ep.value.trim()) {
            fd.append("extraction_prompt", ep.value.trim());
        }

        runZip.disabled = true;
        runZip.innerHTML = `<span class="spinner"></span> Starting...`;
        try {
          const resp = await uploadWithProgress("/api/legal/upload_folder", fd, runZip, "Extracting...");
          const r = await resp.json();
          if (r.error) {
            toast(esc(r.error), "bad", 8000);
            runZip.disabled = false;
            runZip.textContent = "Process Ingest";
            return;
          }
          if ((r.leads_enqueued || 0) === 0) {
            toast("No leads found to process. Ensure subfolders match Loan Account Numbers.", "bad", 8000);
            runZip.disabled = false;
            runZip.textContent = "Process Ingest";
            return;
          }
          toast(`Enqueued ${r.leads_enqueued} lead(s) with ${r.documents_enqueued || 0} document(s)`, "ok");
          startBatchPoll(r.batch_id);
        } catch (e) {
          console.error("Upload error:", e);
          toast(e.message || "Upload failed", "bad", 8000);
          runZip.disabled = false;
          runZip.textContent = "Process Ingest";
        }
      };
    }

    // Direct Dossier Dropzone (individual PDFs / images)
    wireDrop("#docDrop", "#docFiles", files => {
      if (!files || !files.length) return;
      $("#docName").textContent = files.length === 1 ? files[0].name : `${files.length} loan document files selected`;
      $("#docSummary").textContent = Array.from(files).map(f => f.name).slice(0, 3).join(", ") + (files.length > 3 ? "…" : "");
      $("#runDoc").disabled = false;
    });

    const runDoc = $("#runDoc");
    if (runDoc) {
      runDoc.onclick = async () => {
        const docFiles = $("#docFiles");
        if (!docFiles || !docFiles.files || !docFiles.files.length) return;
        const fd = new FormData();
        for (let i = 0; i < docFiles.files.length; i++) {
          fd.append("files", docFiles.files[i]);
        }
        const leadLabel = ($("#docLeadName") && $("#docLeadName").value.trim()) || "";
        if (leadLabel) fd.append("lead_name", leadLabel);
        if (state.scope === "test") fd.append("test", "on");
        runDoc.disabled = true; runDoc.innerHTML = `<span class="spinner"></span> Starting…`;
        try {
          const resp = await uploadWithProgress("/api/legal/upload_folder", fd, runDoc, "Ingesting…");
          const r = await resp.json();
          if (r.error) { toast(esc(r.error), "bad", 8000); runDoc.disabled = false; runDoc.textContent = "Process Dossier"; return; }
          if ((r.leads_enqueued || 0) === 0) {
            toast("No dossiers could be enqueued.", "bad", 8000);
            runDoc.disabled = false;
            runDoc.textContent = "Process Dossier";
            return;
          }
          toast(`Enqueued ${r.leads_enqueued} dossier(s)`, "ok");
          startBatchPoll(r.batch_id);
        } catch (e) {
          console.error("Dossier upload error:", e);
          toast(e.message || "Dossier upload failed", "bad", 8000);
          runDoc.disabled = false;
          runDoc.textContent = "Process Dossier";
        }
      };
    }

    // Local Directory Path
    const runDir = $("#runDir");
    if (runDir) {
      runDir.onclick = async () => {
        const pth = ($("#dirLocalPath") && $("#dirLocalPath").value.trim()) || "";
        if (!pth) { toast("Please specify a server local directory path", "bad"); return; }
        const fd = new FormData();
        fd.append("folder_path", pth);
        if (state.scope === "test") fd.append("test", "on");
        runDir.disabled = true; runDir.innerHTML = `<span class="spinner"></span> Ingesting…`;
        try {
          const r = await (await fetch("/api/legal/upload_folder", { method: "POST", body: fd })).json();
          if (r.error) { toast(esc(r.error), "bad"); runDir.disabled = false; runDir.textContent = "Process Local Path"; return; }
          toast(`Enqueued ${r.leads_enqueued || 0} leads from folder`, "ok");
          startBatchPoll(r.batch_id);
        } catch (e) { toast("Path ingest failed", "bad"); runDir.disabled = false; runDir.textContent = "Process Local Path"; }
      };
    }

    // Instant Demo Dossier
    const triggerDemo = async () => {
      const btn1 = $("#runDemo"), btn2 = $("#tableDemoBtn");
      if (btn1) { btn1.disabled = true; btn1.innerHTML = `<span class="spinner"></span> Synthesizing…`; }
      if (btn2) { btn2.disabled = true; btn2.innerHTML = `<span class="spinner"></span> Synthesizing…`; }
      try {
        const fd = new FormData();
        if (state.scope === "test") fd.append("test", "on");
        const r = await (await fetch("/api/legal/load_demo_dossier", { method: "POST", body: fd })).json();
        toast("⚡ Loaded 3-document SARFAESI Demo Dossier! Extracting…", "ok");
        switchView("upload");
        startBatchPoll(r.batch_id);
      } catch (e) {
        toast("Failed to load demo dossier", "bad");
      } finally {
        if (btn1) { btn1.disabled = false; btn1.textContent = "⚡ Load Demo"; }
        if (btn2) { btn2.disabled = false; btn2.textContent = "⚡ Load Demo Dossier"; }
      }
    };

    if ($("#runDemo")) $("#runDemo").onclick = triggerDemo;
    if ($("#tableDemoBtn")) $("#tableDemoBtn").onclick = triggerDemo;
  }

  /* ── observability view ──────────────────────────────────────────────────────
     Five questions, in the order someone running extractions asks them (see
     workspaces/legal/metrics.py). Charts are plain HTML/SVG on the theme tokens, so they
     need no CDN and follow light/dark. Every mark has a hover tooltip; every value is
     also in the per-dossier table at the end. */
  const OB = { days: 30, batchId: "", data: null, sort: { key: "updated_at", dir: -1 },
               q: "", onlyIds: null, onlyLabel: "", timer: null };

  const fmtInt = (n) => (n == null ? "—" : Number(n).toLocaleString("en-IN"));
  const fmtCompact = (n) => {
    if (n == null) return "—";
    const v = Number(n);
    if (v >= 1e7) return (v / 1e7).toFixed(2).replace(/\.?0+$/, "") + " Cr";
    if (v >= 1e5) return (v / 1e5).toFixed(2).replace(/\.?0+$/, "") + " L";
    if (v >= 1000) return (v / 1000).toFixed(v >= 10000 ? 0 : 1).replace(/\.0$/, "") + "k";
    return String(Math.round(v));
  };
  const fmtDur = (ms) => {
    if (ms == null || !isFinite(ms)) return "—";
    const s = ms / 1000;
    if (s < 1) return `${Math.round(ms)} ms`;
    if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
    return `${Math.floor(s / 3600)}h ${String(Math.round((s % 3600) / 60)).padStart(2, "0")}m`;
  };
  const relTime = (iso) => {
    if (!iso) return "never";
    const mins = (Date.now() - new Date(iso).getTime()) / 60000;
    if (mins < 1) return "just now";
    if (mins < 60) return `${Math.round(mins)} min ago`;
    if (mins < 60 * 24) return `${Math.round(mins / 60)} h ago`;
    return `${Math.round(mins / 1440)} d ago`;
  };
  const pctOf = (n, d) => (d ? Math.round((100 * n) / d) : 0);
  // data-tk / data-tv carry the tooltip (label / value); they are set as text, never HTML
  const tipAttrs = (label, value) => `data-tk="${esc(label)}" data-tv="${esc(value)}"`;

  const OUTCOME = [
    ["completed", "Completed", "var(--ch-ok)"],
    ["missing_documents", "Missing documents", "var(--ch-warn)"],
    ["partial", "Partial", "var(--viz-serious)"],
    ["failed", "Failed", "var(--ch-bad)"],
  ];
  const SCRIPTS = [
    ["typed", "Typed", "var(--ch-ok)"],
    ["scanned", "Scanned · possibly handwritten", "var(--ch-warn)"],
    ["handwritten", "Handwritten", "var(--ch-bad)"],
    ["unknown", "Unchecked page", "var(--ch-neutral)"],
    ["none", "No values extracted", "var(--ink-faint)"],
  ];

  function cardHead(title, sub, extra = "") {
    return `<div class="ob-card-head"><div><h3>${esc(title)}</h3>${sub ? `<p>${esc(sub)}</p>` : ""}</div>${extra}</div>`;
  }

  // one stacked bar: segments with 2px surface gaps, rounded outer ends, tooltip per segment
  function stackedBar(parts, total, extraClass = "") {
    const live = parts.filter(p => p.n > 0);
    if (!total || !live.length) return `<div class="ob-stack empty ${extraClass}"><span></span></div>`;
    return `<div class="ob-stack ${extraClass}">${live.map(p =>
      `<span class="ob-seg-part" style="flex:${p.n};background:${p.color}" ${tipAttrs(p.label, `${fmtInt(p.n)} · ${pctOf(p.n, total)}%`)}></span>`).join("")}</div>`;
  }

  function legend(parts, total, withCounts = true) {
    return `<div class="ob-legend">${parts.map(p => `
      <span class="ob-lg${p.n ? "" : " zero"}"><i style="background:${p.color}"></i>${esc(p.label)}
        ${withCounts ? `<b>${fmtInt(p.n)}</b><em>${total ? pctOf(p.n, total) + "%" : ""}</em>` : ""}</span>`).join("")}</div>`;
  }

  /* ── 0. live strip ── */
  function renderLive(d) {
    const L = d.live || {};
    const busy = (L.queued || 0) + (L.processing || 0) > 0;
    const days = d.per_day || [];
    const max = Math.max(1, ...days.map(x => x.n));
    const w = 132, h = 30, gap = 2, bw = days.length ? (w - gap * (days.length - 1)) / days.length : 0;
    const spark = days.map((x, i) => {
      const bh = x.n ? Math.max(3, (x.n / max) * h) : 1.5;
      return `<rect x="${(i * (bw + gap)).toFixed(1)}" y="${(h - bh).toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}"
        rx="${Math.min(2, bw / 2).toFixed(1)}" class="${x.n ? "on" : "off"}${i === days.length - 1 ? " last" : ""}"
        ${tipAttrs(new Date(x.day).toLocaleDateString("en-IN", { day: "numeric", month: "short" }), `${x.n} finished`)}></rect>`;
    }).join("");
    const item = (key, label, n, tone) => `
      <div class="ob-li ${tone}${n ? " has" : ""}" data-k="${key}">
        <span class="ob-li-n">${fmtInt(n || 0)}</span><span class="ob-li-l">${label}</span></div>`;
    $("#obLive").innerHTML = `
      <div class="ob-state ${busy ? "busy" : "idle"}">
        <span class="ob-pulse" aria-hidden="true"></span>
        <div><b>${busy ? "Extracting" : "Idle"}</b><span>Last dossier finished ${esc(relTime(L.last_finished))}</span></div>
      </div>
      <div class="ob-li-row">
        ${item("queued", "In queue", L.queued, "neutral")}
        ${item("processing", "Processing", L.processing, "accent")}
        ${item("stuck", `Stuck <small>no progress ${L.stuck_after_min || 20} min</small>`, L.stuck, L.stuck ? "bad" : "neutral")}
        ${item("paused", "Paused", L.paused, "neutral")}
      </div>
      <div class="ob-spark" aria-label="Dossiers finished per day">
        <svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" preserveAspectRatio="none">${spark}</svg>
        <span>Finished / day · ${days.length} d</span>
      </div>`;
  }

  /* ── headline ── */
  function renderKpis(d) {
    const o = d.outcomes || {}, t = d.tokens || {}, tm = d.timing || {};
    const finished = o.finished || 0;
    const clean = Math.max(0, (o.completed || 0) - (o.completed_empty || 0));
    const needLook = finished - clean;
    const vals = (d.per_dossier || []).filter(r => ["completed", "partial", "missing_documents", "failed"].includes(r.status))
      .map(r => r.values || 0).sort((a, b) => a - b);
    const medVals = vals.length ? vals[Math.floor((vals.length - 1) / 2)] : null;
    const maxVals = vals.length ? vals[vals.length - 1] : null;
    const tiles = [
      ["hero", "Dossiers finished", fmtInt(finished), OB.batchId ? "in this run" : (OB.days ? `in the last ${OB.days === 1 ? "24 hours" : OB.days + " days"}` : "all time")],
      [needLook ? "warn" : "ok", "Finished with data", finished ? `${pctOf(clean, finished)}%` : "—",
        needLook ? `${fmtInt(needLook)} need a look` : "every dossier returned values"],
      ["", "Values per dossier", medVals == null ? "—" : String(medVals), maxVals != null ? `median · up to ${maxVals}` : ""],
      ["", "Time per dossier", fmtDur(tm.all_p50), tm.all_p95 ? `median · p95 ${fmtDur(tm.all_p95)}` : ""],
      ["", "Tokens per dossier", fmtCompact(t.p50), t.p95 ? `median · p95 ${fmtCompact(t.p95)}` : ""],
    ];
    $("#obKpis").innerHTML = tiles.map(([cls, label, value, sub]) => `
      <div class="ob-kpi ${cls}"><div class="ob-kpi-l">${esc(label)}</div>
        <div class="ob-kpi-v">${esc(value)}</div><div class="ob-kpi-s">${esc(sub)}</div></div>`).join("");
  }

  /* ── 01 output quality ── */
  function renderOutcomes(d) {
    const o = d.outcomes || {};
    const parts = OUTCOME.map(([k, label, color]) => ({ key: k, label, color, n: o[k] || 0 }));
    const empty = o.completed_empty || 0;
    $("#obOutcomes").innerHTML = cardHead("How dossiers finished", `${fmtInt(o.finished || 0)} finished dossiers`) +
      stackedBar(parts, o.finished, "lg") + legend(parts, o.finished) +
      (empty ? `
        <div class="ob-callout">
          <span class="ob-callout-ic" aria-hidden="true">!</span>
          <div><b>${fmtInt(empty)} finished as "completed" with nothing cited.</b>
            The model returned no usable values for ${empty === 1 ? "this dossier" : "these dossiers"} — worth re-running or checking the prompt.</div>
          <button type="button" class="btn line small" id="obShowEmpty">Show ${empty === 1 ? "it" : "them"}</button>
        </div>` : `<div class="ob-note ok">Every completed dossier returned at least one cited page.</div>`);
    const btn = $("#obShowEmpty");
    if (btn) btn.onclick = () => {
      OB.onlyIds = new Set(o.completed_empty_ids || []);
      OB.onlyLabel = `${fmtInt(empty)} completed with nothing cited`;
      renderTable();
      $(".ob-tablecard").scrollIntoView({ behavior: "smooth", block: "start" });
    };
  }

  function renderValues(d) {
    const bins = d.values_histogram || [];
    const max = Math.max(1, ...bins.map(b => b.n));
    const total = bins.reduce((a, b) => a + b.n, 0);
    $("#obValues").innerHTML = cardHead("Values found per dossier", "How many fields each finished dossier returned") +
      `<div class="ob-cols" role="list">${bins.map(b => `
        <div class="ob-col${b.bin === "0" ? " zero-bin" : ""}" role="listitem" ${tipAttrs(`${b.bin} values`, `${fmtInt(b.n)} dossier${b.n === 1 ? "" : "s"} · ${pctOf(b.n, total)}%`)}>
          <span class="ob-col-v">${b.n ? fmtInt(b.n) : ""}</span>
          <span class="ob-col-bar" style="height:${b.n ? Math.max(4, (b.n / max) * 100) : 0}%"></span>
          <span class="ob-col-l">${esc(b.bin)}</span>
        </div>`).join("")}</div>
      <div class="ob-axis-t">values extracted</div>`;
  }

  function renderCoverage(d) {
    const cov = d.field_coverage || [];
    const finished = (d.outcomes || {}).finished || 0;
    $("#obCoverage").innerHTML = cardHead("Field coverage",
      "Share of finished dossiers where each field was found — click one to see the dossiers missing it") +
      (cov.length ? `<div class="ob-rank">${cov.map(c => `
        <button type="button" class="ob-rank-row" data-field="${esc(c.field)}" ${tipAttrs(formatColName(c.field), `found in ${fmtInt(c.n)} of ${fmtInt(finished)}`)}>
          <span class="ob-rank-l">${esc(formatColName(c.field))}</span>
          <span class="ob-rank-track"><span style="width:${Math.max(1, c.pct)}%"></span></span>
          <span class="ob-rank-v">${Math.round(c.pct)}%</span>
          <span class="ob-rank-n">${fmtInt(c.n)}/${fmtInt(finished)}</span>
        </button>`).join("")}</div>` : `<div class="ob-empty">No values extracted in this slice yet.</div>`);
    $$("#obCoverage [data-field]").forEach(b => b.onclick = () => {
      state.missing = b.dataset.field;
      state.batchId = OB.batchId || null;
      switchView("dashboard");
      updateBatchFilterUI();
      toast(`Showing dossiers missing ${formatColName(b.dataset.field)}`, "ok");
    });
  }

  /* ── 02 where values came from ── */
  function renderScript(d) {
    const m = d.script_mix || { values: {}, dossiers: {} };
    const v = m.values || {}, ds = m.dossiers || {};
    const vParts = SCRIPTS.map(([k, label, color]) => ({ label, color, n: v[k] || 0 }));
    const dParts = SCRIPTS.map(([k, label, color]) => ({ label, color, n: ds[k] || 0 }));
    const vTotal = vParts.reduce((a, p) => a + p.n, 0);
    const dTotal = dParts.reduce((a, p) => a + p.n, 0);
    const pending = ds.not_checked || 0;
    $("#obScript").innerHTML = cardHead("Typed, scanned or handwritten",
      "A dossier takes the strongest tag among its values: handwritten › scanned › typed",
      pending ? `<button type="button" class="btn line small" id="obCheckScripts">Check ${fmtInt(pending)} dossier${pending === 1 ? "" : "s"}</button>` : "") +
      `<div class="ob-script">
        <div class="ob-script-row"><span class="ob-script-k">Values<b>${fmtInt(vTotal)}</b></span>${stackedBar(vParts, vTotal)}</div>
        <div class="ob-script-row"><span class="ob-script-k">Dossiers<b>${fmtInt(dTotal)}</b></span>${stackedBar(dParts, dTotal)}</div>
      </div>` + legend(vParts, vTotal) +
      `<div class="ob-note">Handwriting is flagged by the model on runs extracted since this was added. Older runs can only show
        whether a value appears in the PDF's own text (Typed) or was read off the page image (Scanned — which may be handwriting).</div>`;
    const chk = $("#obCheckScripts");
    if (chk) chk.onclick = async () => {
      chk.disabled = true; chk.textContent = "Checking…";
      const ids = (d.per_dossier || []).filter(r => !r.script_tag).map(r => r.lead_id);
      try {
        for (let i = 0; i < ids.length; i += 25) {
          await fetch("/api/legal/provenance/scan", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ lead_ids: ids.slice(i, i + 25) }) });
        }
      } catch (e) { toast("Check failed", "bad"); }
      loadObservability();
    };
  }

  /* ── 03 cost ── */
  function renderTokens(d) {
    const t = d.tokens || {};
    const share = t.prompt_share;
    const parts = [
      { label: "Prompt (page images + instructions)", color: "var(--viz-1)", n: t.prompt || 0 },
      { label: "Completion (model output)", color: "var(--viz-2)", n: t.completion || 0 },
    ];
    $("#obTokens").innerHTML = cardHead("Tokens", `${fmtInt(t.measured || 0)} dossiers with token telemetry`) + `
      <div class="ob-bigstat"><span class="v">${esc(fmtCompact(t.total))}</span><span class="l">tokens in total</span></div>
      <div class="ob-mini-grid">
        <div><span class="v">${esc(fmtCompact(t.p50))}</span><span class="l">median / dossier</span></div>
        <div><span class="v">${esc(fmtCompact(t.p95))}</span><span class="l">p95 / dossier</span></div>
        <div><span class="v">${esc(fmtCompact(t.per_page))}</span><span class="l">per page read</span></div>
        <div><span class="v">${esc(fmtCompact(t.max))}</span><span class="l">heaviest dossier</span></div>
      </div>
      <div class="ob-split-l">Where the tokens go${share != null ? ` — <b>${share}%</b> is the prompt` : ""}</div>
      ${stackedBar(parts, t.total)}${legend(parts, t.total)}`;
  }

  function renderRunTokens(d) {
    const runs = d.per_run || [];
    const max = Math.max(1, ...runs.map(r => r.prompt + r.completion));
    const legendParts = [
      { label: "Prompt", color: "var(--viz-1)", n: 0 }, { label: "Completion", color: "var(--viz-2)", n: 0 },
    ];
    $("#obRunTokens").innerHTML = cardHead("Tokens per run", "Newest first") +
      (runs.length ? legend(legendParts, 0, false) + `<div class="ob-hbars">${runs.map(r => {
        const tot = r.prompt + r.completion;
        return `
          <div class="ob-hbar">
            <div class="ob-hbar-l"><b title="${esc(r.name)}">${esc(r.name)}</b>
              <span>${fmtInt(r.dossiers)} dossier${r.dossiers === 1 ? "" : "s"} · ${fmtCompact(r.dossiers ? tot / r.dossiers : 0)}/dossier · median ${fmtDur(r.p50_ms)}</span></div>
            <div class="ob-hbar-track">
              <div class="ob-hbar-fill" style="width:${Math.max(1, (tot / max) * 100)}%">
                <span style="flex:${r.prompt || 0};background:var(--viz-1)" ${tipAttrs(`${r.name} · prompt`, fmtInt(r.prompt) + " tokens")}></span>
                <span style="flex:${r.completion || 0};background:var(--viz-2)" ${tipAttrs(`${r.name} · completion`, fmtInt(r.completion) + " tokens")}></span>
              </div>
              <span class="ob-hbar-v">${fmtCompact(tot)}</span>
            </div>
          </div>`;
      }).join("")}</div>` : `<div class="ob-empty">No runs in this slice.</div>`);
  }

  /* ── 04 time & reading ── */
  function niceStep(maxMs) {
    const steps = [5, 10, 15, 30, 60, 120, 180, 300, 600, 900, 1800, 3600, 7200].map(s => s * 1000);
    return steps.find(s => maxMs / s <= 5) || steps[steps.length - 1];
  }

  function renderTiming(d) {
    const tm = d.timing || {};
    const rows = [
      ["Model time", "time the vision model spent on the dossier", tm.vlm_min, tm.vlm_p50, tm.vlm_p95, tm.vlm_max],
      ["Total time", "end to end, from pick-up to saved", tm.all_min, tm.all_p50, tm.all_p95, tm.all_max],
    ];
    const top = Math.max(1, tm.all_max || 0, tm.vlm_max || 0);
    const step = niceStep(top);
    const axisMax = Math.ceil(top / step) * step;
    const x = (ms) => `${Math.max(0, Math.min(100, (100 * (ms || 0)) / axisMax)).toFixed(2)}%`;
    const ticks = [];
    for (let t = 0; t <= axisMax; t += step) ticks.push(t);
    $("#obTiming").innerHTML = cardHead("Time per dossier", `${fmtInt(tm.measured || 0)} dossiers timed`) +
      (tm.measured ? `<div class="ob-range">
        ${rows.map(([label, sub, mn, p50, p95, mx]) => `
          <div class="ob-range-row">
            <div class="ob-range-l"><b>${label}</b><span>${sub}</span></div>
            <div class="ob-range-plot">
              <span class="ob-range-line" style="left:${x(mn)};width:calc(${x(mx)} - ${x(mn)})" ${tipAttrs(label + " · fastest → slowest", `${fmtDur(mn)} → ${fmtDur(mx)}`)}></span>
              <span class="ob-range-band" style="left:${x(p50)};width:calc(${x(p95)} - ${x(p50)})" ${tipAttrs(label + " · typical → slow", `p50 ${fmtDur(p50)} · p95 ${fmtDur(p95)}`)}></span>
              <span class="ob-range-dot p50" style="left:${x(p50)}" ${tipAttrs(label + " · median", fmtDur(p50))}></span>
              <span class="ob-range-dot p95" style="left:${x(p95)}" ${tipAttrs(label + " · p95", fmtDur(p95))}></span>
              <span class="ob-range-lab p50" style="left:${x(p50)}">p50 ${fmtDur(p50)}</span>
              <span class="ob-range-lab p95" style="left:${x(p95)}">p95 ${fmtDur(p95)}</span>
            </div>
          </div>`).join("")}
        <div class="ob-range-axis">${ticks.map(t => `<span style="left:${x(t)}">${fmtDur(t)}</span>`).join("")}</div>
        <div class="ob-range-key"><span><i class="k-line"></i>fastest → slowest</span><span><i class="k-band"></i>median → p95</span><span><i class="k-dot"></i>median</span></div>
      </div>` : `<div class="ob-empty">No timed dossiers in this slice.</div>`);
  }

  function renderPerDay(d) {
    const days = d.per_day || [];
    const max = Math.max(1, ...days.map(x => x.n));
    const total = days.reduce((a, x) => a + x.n, 0);
    const peakIdx = days.reduce((bi, x, i, arr) => (x.n > arr[bi].n ? i : bi), 0);
    const every = Math.ceil(days.length / 7);
    $("#obPerDay").innerHTML = cardHead("Dossiers finished per day", `${fmtInt(total)} in the last ${days.length} days`) +
      `<div class="ob-days">${days.map((x, i) => {
        const dt = new Date(x.day);
        const lab = dt.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
        const showLab = i === days.length - 1 || (days.length - 1 - i) % every === 0;
        return `<div class="ob-day" ${tipAttrs(lab, `${fmtInt(x.n)} finished`)}>
          <span class="ob-day-v">${i === peakIdx && x.n ? fmtInt(x.n) : ""}</span>
          <span class="ob-day-bar${x.n ? "" : " nil"}" style="height:${x.n ? Math.max(4, (x.n / max) * 100) : 2}%"></span>
          <span class="ob-day-l">${showLab ? esc(lab) : ""}</span></div>`;
      }).join("")}</div>`;
  }

  function renderReading(d) {
    const r = d.reading || {};
    const funnel = (title, stages) => {
      const first = stages[0][1] || 0;
      return `<div class="ob-funnel"><div class="ob-funnel-t">${title}</div>${stages.map(([label, n, note]) => `
        <div class="ob-fstage" ${tipAttrs(label, `${fmtInt(n)} · ${first ? ((100 * n) / first).toFixed(n && (100 * n) / first < 1 ? 1 : 0) : 0}% of ${fmtInt(first)}`)}>
          <span class="ob-fstage-l">${label}${note ? `<small>${note}</small>` : ""}</span>
          <span class="ob-fstage-track"><span style="width:${first ? Math.max(0.6, (100 * n) / first) : 0}%"></span></span>
          <span class="ob-fstage-v"><b>${fmtInt(n)}</b><em>${first ? ((100 * n) / first).toFixed(n && (100 * n) / first < 1 ? 1 : 0) + "%" : ""}</em></span>
        </div>`).join("")}</div>`;
    };
    $("#obReading").innerHTML = cardHead("How much of each dossier was read",
      `${fmtInt(r.docs_total || 0)} documents · ${fmtInt(r.mb || 0)} MB in this slice`) +
      `<div class="ob-funnels">
        ${funnel("Documents", [["In the dossiers", r.docs_total], ["Read by the model", r.docs_read, "chosen by the prompt"], ["Cited as a source", r.docs_cited]])}
        ${funnel("Pages", [["In the dossiers", r.pages_total], ["In documents that were read", r.pages_read], ["Cited as a source", r.pages_cited, "values came from these"]])}
      </div>`;
  }

  /* ── 05 the table ── */
  const OB_COLS = [
    ["name", "Dossier", false], ["run", "Run", false], ["status", "Status", false],
    ["script_tag", "Typed / Handwritten", false], ["values", "Values", true], ["cited_pages", "Pages cited", true],
    ["documents_read", "Docs read", true], ["tokens", "Tokens", true], ["vlm_ms", "Model time", true],
    ["pipeline_ms", "Total time", true], ["updated_at", "Finished", false],
  ];

  function renderTable() {
    const d = OB.data || {};
    let rows = (d.per_dossier || []).slice();
    if (OB.onlyIds) rows = rows.filter(r => OB.onlyIds.has(r.lead_id));
    const q = OB.q.toLowerCase();
    if (q) rows = rows.filter(r => [r.name, r.lead_id, r.run, r.status, r.script_tag].some(v => v && String(v).toLowerCase().includes(q)));
    const { key, dir } = OB.sort;
    rows.sort((a, b) => {
      const av = a[key], bv = b[key];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      return (typeof av === "number" ? av - bv : String(av).localeCompare(String(bv))) * dir;
    });
    const peak = Math.max(1, ...rows.map(r => r.tokens || 0));
    $("#obHead").innerHTML = OB_COLS.map(([k, label, num]) =>
      `<th class="${num ? "c-num " : ""}sortable${k === key ? (dir > 0 ? " asc" : " desc") : ""}" data-k="${k}" tabindex="0" aria-sort="${k === key ? (dir > 0 ? "ascending" : "descending") : "none"}">${label}</th>`).join("");
    $("#obRows").innerHTML = rows.length ? rows.map(r => `
      <tr data-id="${esc(r.lead_id)}" tabindex="0">
        <td><div class="rv-name">${esc(r.name)}</div><div class="rv-id">${esc(r.lead_id)}</div></td>
        <td class="ob-run" title="${esc(r.run)}">${esc(r.run)}</td>
        <td>${badge(r.status)}</td>
        <td>${scriptChip(r.script_tag)}</td>
        <td class="c-num">${r.values ? fmtInt(r.values) : '<span class="ob-zero">0</span>'}</td>
        <td class="c-num">${fmtInt(r.cited_pages)}</td>
        <td class="c-num">${fmtInt(r.documents_read)}/${fmtInt(r.documents)}</td>
        <td class="c-num"><span class="ob-tok"><span style="width:${Math.round((100 * (r.tokens || 0)) / peak)}%"></span></span>${fmtInt(r.tokens)}</td>
        <td class="c-num">${fmtDur(r.vlm_ms)}</td>
        <td class="c-num">${fmtDur(r.pipeline_ms)}</td>
        <td class="ob-when">${r.updated_at ? esc(new Date(r.updated_at).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })) : "—"}</td>
      </tr>`).join("") : `<tr><td colspan="${OB_COLS.length}" class="ob-empty">No dossiers match.</td></tr>`;
    const total = (d.per_dossier || []).length;
    $("#obCount").textContent = rows.length === total ? `${fmtInt(total)} dossiers` : `${fmtInt(rows.length)} of ${fmtInt(total)} dossiers`;
    const chip = $("#obTableFilter");
    chip.classList.toggle("hidden", !OB.onlyIds);
    chip.innerHTML = OB.onlyIds ? `${esc(OB.onlyLabel)} <button type="button" aria-label="Clear filter">✕</button>` : "";
    const clr = $("button", chip);
    if (clr) clr.onclick = () => { OB.onlyIds = null; renderTable(); };
  }

  function renderObservability(d) {
    OB.data = d;
    const sel = $("#obRun");
    const runs = d.runs || [];
    sel.innerHTML = `<option value="">All runs</option>` + runs.map(r =>
      `<option value="${esc(r.batch_id)}"${r.batch_id === OB.batchId ? " selected" : ""}>${esc(r.name)} · ${fmtInt(r.dossiers)}</option>`).join("");
    if (OB.batchId && !runs.some(r => r.batch_id === OB.batchId)) { OB.batchId = ""; sel.value = ""; }
    $$("#obPeriod [data-days]").forEach(b => {
      const on = Number(b.dataset.days) === (OB.days || 0);
      b.classList.toggle("on", on);
      b.setAttribute("aria-checked", on ? "true" : "false");
    });
    renderLive(d);
    renderKpis(d);
    renderOutcomes(d);
    renderValues(d);
    renderCoverage(d);
    renderScript(d);
    renderTokens(d);
    renderRunTokens(d);
    renderTiming(d);
    renderPerDay(d);
    renderReading(d);
    renderTable();
    $("#obUpdated").textContent = `Updated ${new Date().toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}`;
  }

  async function loadObservability() {
    const view = $("#view-observability");
    if (!view) return;
    view.classList.add("ob-loading");             // keep the previous render, dimmed — no layout jump
    const qs = new URLSearchParams({ scope: state.scope || "real" });
    if (OB.batchId) qs.set("batch_id", OB.batchId);
    if (OB.days) qs.set("days", String(OB.days));
    try {
      const r = await fetch(`/api/legal/observability?${qs}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      renderObservability(await r.json());
    } catch (e) {
      toast("Failed to load observability", "bad");
    } finally {
      view.classList.remove("ob-loading");
    }
    // follow the work while something is queued or being read
    clearTimeout(OB.timer);
    const L = (OB.data && OB.data.live) || {};
    if ((L.queued || L.processing) && !view.classList.contains("hidden")) {
      OB.timer = setTimeout(() => { if (!view.classList.contains("hidden")) loadObservability(); }, 10000);
    }
  }

  function initObservability() {
    const view = $("#view-observability");
    if (!view || !$("#obRun")) return;
    $("#obRun").onchange = (e) => { OB.batchId = e.target.value; OB.onlyIds = null; loadObservability(); };
    $$("#obPeriod [data-days]").forEach(b => b.onclick = () => {
      OB.days = Number(b.dataset.days) || 0;
      OB.onlyIds = null;
      loadObservability();
    });
    $("#obSearch").oninput = debounce(() => { OB.q = $("#obSearch").value.trim(); renderTable(); }, 150);
    $("#obHead").addEventListener("click", (e) => {
      const th = e.target.closest("th[data-k]");
      if (!th) return;
      const k = th.dataset.k;
      OB.sort = OB.sort.key === k ? { key: k, dir: -OB.sort.dir } : { key: k, dir: OB_COLS.find(c => c[0] === k)[2] ? -1 : 1 };
      renderTable();
    });
    $("#obHead").addEventListener("keydown", (e) => {
      if ((e.key === "Enter" || e.key === " ") && e.target.matches("th[data-k]")) { e.preventDefault(); e.target.click(); }
    });
    const openRow = (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) openLead(tr.dataset.id); };
    $("#obRows").addEventListener("click", openRow);
    $("#obRows").addEventListener("keydown", (e) => { if (e.key === "Enter") openRow(e); });

    // one tooltip for every mark on the page; content is set as text
    const tip = $("#obTip");
    const show = (el, x, y) => {
      tip.replaceChildren();
      const v = document.createElement("b"); v.textContent = el.dataset.tv || "";
      const k = document.createElement("span"); k.textContent = el.dataset.tk || "";
      tip.append(v, k);
      tip.hidden = false;
      const w = tip.offsetWidth, h = tip.offsetHeight;
      tip.style.left = `${Math.min(window.innerWidth - w - 12, Math.max(12, x + 14))}px`;
      tip.style.top = `${Math.max(12, y - h - 12)}px`;
    };
    view.addEventListener("pointermove", (e) => {
      const el = e.target.closest("[data-tk]");
      if (!el || !view.contains(el)) { tip.hidden = true; return; }
      show(el, e.clientX, e.clientY);
    });
    view.addEventListener("pointerleave", () => { tip.hidden = true; });
    view.addEventListener("focusin", (e) => {
      const el = e.target.closest("[data-tk]");
      if (!el) return;
      const r = el.getBoundingClientRect();
      show(el, r.left + r.width / 2, r.top);
    });
    view.addEventListener("focusout", () => { tip.hidden = true; });
    window.addEventListener("scroll", () => { tip.hidden = true; }, { passive: true });
  }

  /* ── model config modal ─────────────────── */
  function initModelConfig() {
    $("#modelChip").onclick = () => {
      fetch("/api/config/model").then(r => r.json()).then(c => {
        $("#cfgUrl").value = c.url || "";
        $("#cfgModel").value = c.model || "";
        $("#cfgKey").value = "";
        $("#cfgKeyHint").textContent = c.has_key ? `current: ${c.key_masked}` : "none set";
        $("#cfgTestResult").classList.add("hidden");
        $("#cfgModal").classList.add("open"); $("#cfgScrim").classList.add("open");
      }).catch(() => toast("Failed to load model config", "bad"));
    };
    $("#cfgClose").onclick = () => { $("#cfgModal").classList.remove("open"); $("#cfgScrim").classList.remove("open"); };
    $("#cfgScrim").onclick = () => { $("#cfgModal").classList.remove("open"); $("#cfgScrim").classList.remove("open"); };
    $("#cfgTest").onclick = async () => {
      const btn = $("#cfgTest"); btn.disabled = true; btn.textContent = "Testing…";
      const body = { url: $("#cfgUrl").value.trim(), key: $("#cfgKey").value, model: $("#cfgModel").value.trim() };
      const box = $("#cfgTestResult"); box.classList.remove("hidden"); box.className = "cfg-test"; box.textContent = "…";
      try {
        const r = await (await fetch("/api/config/model/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
        if (r.ok) {
          box.className = "cfg-test ok"; box.textContent = `✓ Reachable — HTTP ${r.status} · ${r.ms} ms`;
        } else {
          box.className = "cfg-test bad"; box.textContent = `✗ ${r.error || "unreachable"}`;
        }
      } catch (e) { box.className = "cfg-test bad"; box.textContent = "✗ test failed"; }
      btn.disabled = false; btn.textContent = "Test connection";
    };
    $("#cfgSave").onclick = async () => {
      const body = { url: $("#cfgUrl").value.trim(), key: $("#cfgKey").value, model: $("#cfgModel").value.trim() };
      if (!body.url) { toast("Endpoint URL is required", "bad"); return; }
      try {
        const c = await (await fetch("/api/config/model", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
        $("#mcModel").textContent = c.model || "Medha";
        $("#mcUrl").textContent = (c.url || "").replace(/^https?:\/\//, "").split("/")[0] || "—";
        toast("Endpoint saved — applies to the next lead", "ok");
        $("#cfgModal").classList.remove("open"); $("#cfgScrim").classList.remove("open");
      } catch (e) { toast("Save failed", "bad"); }
    };
  }

  /* ── refresh all ────────────────────────── */
  function refreshAll() { loadStats(); loadLeads(); }

  /* ── prompt history dropdown & persistence ── */
  async function initPromptHistory() {
    const btn = $("#btnPromptHistory");
    const dropdown = $("#promptHistoryDropdown");
    const list = $("#promptHistoryList");
    const ep = $("#extractionPrompt");
    
    // Auto-restore prompt from localStorage or server history if textarea is empty
    if (ep) {
      const saved = localStorage.getItem("legal-last-prompt");
      if (saved && !ep.value.trim()) {
        ep.value = saved;
      } else if (!ep.value.trim()) {
        fetch("/api/legal/prompt_history")
          .then(r => r.ok ? r.json() : [])
          .then(prompts => {
            if (prompts && prompts.length > 0 && !ep.value.trim()) {
              ep.value = prompts[0].prompt;
              localStorage.setItem("legal-last-prompt", prompts[0].prompt);
            }
          })
          .catch(() => {});
      }
      
      ep.addEventListener("input", () => {
        const val = ep.value.trim();
        if (val) localStorage.setItem("legal-last-prompt", val);
      });
    }

    if (!btn || !dropdown || !list) return;

    btn.onclick = async (e) => {
      e.stopPropagation();
      const isHidden = dropdown.classList.contains("hidden");
      if (isHidden) {
        try {
          const resp = await fetch("/api/legal/prompt_history");
          if (resp.ok) {
            const prompts = await resp.json();
            if (prompts.length > 0) {
              list.innerHTML = prompts.map(p => `
                <div class="ph-item" style="padding: 10px 12px; font-size: 12px; color: var(--ink); border-bottom: 1px solid var(--line); cursor: pointer; transition: background 0.15s; font-family: 'JetBrains Mono', monospace; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;" title="Click to apply">
                  ${esc(p.prompt)}
                </div>
              `).join("");
              
              $$(".ph-item", list).forEach(el => {
                el.onmouseenter = () => el.style.background = "var(--surface-2)";
                el.onmouseleave = () => el.style.background = "transparent";
                el.onclick = () => {
                  const pText = el.textContent.trim();
                  if (ep) {
                    ep.value = pText;
                    localStorage.setItem("legal-last-prompt", pText);
                  }
                  dropdown.classList.add("hidden");
                };
              });
            } else {
              list.innerHTML = `<div style="padding: 12px; font-size: 12px; color: var(--ink-faint); text-align: center;">No history found.</div>`;
            }
          }
        } catch (err) {}
      }
      dropdown.classList.toggle("hidden");
    };

    document.addEventListener("click", (e) => {
      if (!dropdown.contains(e.target) && e.target !== btn) {
        dropdown.classList.add("hidden");
      }
    });
  }

  /* ── export table data ───────────────────── */
  function exportTableData(format = 'csv') {
    const rows = facetRows().filter(passesColFilters);
    if (!rows.length) return toast("No leads to export", "warn");

    const baseCols = ["lead_id", "processing_status", "script_tag"];
    const exportCols = [...baseCols, ...dynamicColumns];
    const headerLabels = exportCols.map(c => {
      if (c === "lead_id") return "Lead ID";
      if (c === "processing_status") return "Status";
      if (c === "script_tag") return "Typed / Handwritten";
      return formatColName(c);
    });

    const dateStr = new Date().toISOString().slice(0, 10);

    if (format === 'excel') {
      let tableHtml = `
        <html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:x="urn:schemas-microsoft-com:office:excel" xmlns="http://www.w3.org/TR/REC-html40">
        <head>
          <meta charset="utf-8">
          <!--[if gte mso 9]><xml><x:ExcelWorkbook><x:ExcelWorksheets><x:ExcelWorksheet><x:Name>Leads Extraction</x:Name><x:WorksheetOptions><x:DisplayGridlines/></x:WorksheetOptions></x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml><![endif]-->
          <style>
            th { background-color: #0d9488; color: #ffffff; font-weight: bold; border: 1px solid #cbd5e1; padding: 8px 12px; }
            td { border: 1px solid #e2e8f0; padding: 6px 10px; vertical-align: top; mso-number-format: "\\@"; }
          </style>
        </head>
        <body>
          <table>
            <thead>
              <tr>${headerLabels.map(h => `<th>${esc(h)}</th>`).join("")}</tr>
            </thead>
            <tbody>
              ${rows.map(r => `
                <tr>
                  ${exportCols.map(col => {
                    let v = r[col];
                    if (v === undefined || v === null) v = "";
                    else if (typeof v === "object") v = JSON.stringify(v);
                    else v = String(v);
                    return `<td>${esc(v)}</td>`;
                  }).join("")}
                </tr>
              `).join("")}
            </tbody>
          </table>
        </body>
        </html>
      `;
      const blob = new Blob([tableHtml], { type: "application/vnd.ms-excel;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `extracted_leads_${dateStr}.xls`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast("Excel downloaded successfully", "ok");
    } else {
      let csv = "\uFEFF" + headerLabels.map(h => `"${String(h).replace(/"/g, '""')}"`).join(",") + "\r\n";
      rows.forEach(r => {
        const line = exportCols.map(col => {
          let v = r[col];
          if (v === undefined || v === null) v = "";
          else if (typeof v === "object") v = JSON.stringify(v);
          else v = String(v);
          return `"${v.replace(/"/g, '""')}"`;
        }).join(",");
        csv += line + "\r\n";
      });
      const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `extracted_leads_${dateStr}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast("CSV downloaded successfully", "ok");
    }
  }

  /* ── initialization ─────────────────────── */
  function init() {
    initPromptHistory();
    // Nav tabs
    $$(".rail-nav .nav-item").forEach(b => b.onclick = () => switchView(b.dataset.view));
    const nrb = $("#newRunBtn");
    if (nrb) nrb.onclick = () => switchView("upload");

    // Scope switcher
    $$("#wsSwitch .ws-opt").forEach(b => b.onclick = () => setScope(b.dataset.scope));
    $("#clearWsBtn").onclick = clearWorkspace;

    // Theme toggle
    $("#themeToggle").onclick = () => setTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");

    // Search and filters
    $("#globalSearch").oninput = debounce((e) => { state.q = e.target.value.trim(); loadLeads(); }, 300);
    $("#refreshBtn").onclick = refreshAll;
    
    // Export buttons
    const exportBtn = $("#exportCsvBtn");
    if (exportBtn) exportBtn.onclick = () => exportTableData('csv');
    const tableCsvBtn = $("#tableExportCsvBtn");
    if (tableCsvBtn) tableCsvBtn.onclick = () => exportTableData('csv');
    const tableExcelBtn = $("#tableExportExcelBtn");
    if (tableExcelBtn) tableExcelBtn.onclick = () => exportTableData('excel');

    $("#clearFilters").onclick = resetColFilters;

    // export menu + field-coverage popover; any click elsewhere closes them
    wireMenu("#exportMenu", "#exportMenuBtn");
    wireMenu("#coverageMenu", "#coverageBtn");
    document.addEventListener("click", (e) => { if (!e.target.closest(".fmenu")) closeMenus(); });

    $$("#statusChips .chip").forEach(c => {
      c.onclick = () => {
        $$("#statusChips .chip").forEach(x => x.classList.remove("active"));
        c.classList.add("active");
        state.status = c.dataset.status;
        loadLeads();
      };
    });

    // Header column filter dropdowns — delegated, because the head is rebuilt per load
    const thead = $("#dynamicTheadRow");
    if (thead && thead.parentElement) {
      thead.parentElement.addEventListener("click", (e) => {
        const th = e.target.closest("th.th-filter");
        if (!th) return;
        e.stopPropagation();
        openColFilter(th.dataset.col, th);
      });
    }

    // Drawer close
    $("#drawerClose").onclick = closeDrawer;
    $("#scrim").onclick = closeDrawer;

    // a page preview opens that page full size, with its raw extraction
    $("#drawerBody").addEventListener("click", (e) => {
      const shot = e.target.closest("[data-open-page]");
      if (!shot) return;
      openPageModal(shot.dataset.doc, shot.dataset.file, +shot.dataset.page, +shot.dataset.pages || 1);
    });

    // Keydown escape listener
    document.addEventListener("keydown", e => {
      if (e.key === "Escape") {
        closeMenus();
        closeColFilter();
        closeDrawer();
        closePageModal();
        $("#cfgModal").classList.remove("open");
        $("#cfgScrim").classList.remove("open");
      }
    });

    // Upload & demo handlers
    // each section wires itself independently: one missing piece of markup must never stop
    // the rest of the console (scope, data load, deep links) from starting
    for (const [name, fn] of [["uploads", initUploads], ["model config", initModelConfig], ["runs view", initRunsView], ["observability", initObservability]]) {
      try { fn(); } catch (e) { console.error(`[legal] ${name} setup failed:`, e); }
    }

    // Page modal close handlers
    const pmClose = $("#pageModalClose");
    if (pmClose) pmClose.onclick = closePageModal;
    const pmScrim = $("#pageModalScrim");
    if (pmScrim) pmScrim.onclick = closePageModal;

    // Set initial scope
    setScope(localStorage.getItem("legal-scope") || "real");

    // Initial load
    refreshAll();

    // Deep lead inspection if URL contains lead ID
    if (CFG.deepLead) {
      setTimeout(() => openLead(CFG.deepLead), 400);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
