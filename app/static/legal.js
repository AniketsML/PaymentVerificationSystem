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

  let state = { status: "all", q: "", scope: null };
  let charts = {};
  let allRows = [];
  const COL_FILTERS = { lead_id: null, account_no_lan: null, applicant_name: null, property_verification_status: null };
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

  const badge = (s) => {
    const st = String(s || "pending").toLowerCase();
    const map = {
      completed: "b-verified",
      partial: "b-warn",
      missing_documents: "b-warn",
      failed: "b-unverified",
      processing: "b-manual_review",
      pending: "b-manual_review"
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
    upload: ["Ingest", "Dossier Ingest", "Upload ZIP archives, direct PDF dossier files, or load instant demo"],
    observability: ["Operations", "Observability", "OCR engine routing, phase timings & circuit breaker resilience"],
  };

  function switchView(name) {
    $$(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === name));
    $$(".view").forEach(v => v.classList.toggle("hidden", v.id !== "view-" + name));
    const t = TITLES[name] || TITLES.dashboard;
    $("#pageEyebrow").textContent = t[0];
    $("#pageTitle").textContent = t[1];
    $("#pageSub").textContent = t[2];
    if (name === "dashboard") refreshAll();
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

  /* ── leads table ───────────────────────── */
  async function loadStats() {
    try {
      const resp = await fetch("/api/legal/stats?scope=" + state.scope);
      if (!resp.ok) return;
      const s = await resp.json();
      renderStats(s.counts || {});
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
    refreshAll();
    if (!$("#view-observability").classList.contains("hidden")) loadObservability();
    if (scope === "test" && !isInitialLoad) toast("Switched to Test Workspace — temporary sandbox data", "");
  }

  async function clearWorkspace() {
    if (!confirm("Clear the Test Workspace? This permanently deletes temporary legal leads. Live records are unaffected.")) return;
    try {
      const r = await (await fetch("/api/legal/clear_test", { method: "POST" })).json();
      toast(`Test workspace cleared — ${r.cleared || 0} lead(s) removed`, "ok");
      refreshAll();
      if (!$("#view-observability").classList.contains("hidden")) loadObservability();
    } catch (e) { toast("Clear failed", "bad"); }
  }

  async function loadLeads() {
    const url = `/api/legal/leads?status=${encodeURIComponent(state.status)}&q=${encodeURIComponent(state.q)}&scope=${state.scope}&limit=400`;
    let rows = [];
    try {
      const resp = await fetch(url);
      if (resp.ok) {
        const data = await resp.json();
        rows = Array.isArray(data) ? data : [];
      } else {
        toast(`Failed to load leads (HTTP ${resp.status})`, "bad");
      }
    } catch (e) {
      toast("Failed to load leads", "bad");
    }
    allRows = Array.isArray(rows) ? rows : [];
    renderLeadRows();
  }

  function passesColFilters(r) {
    return Object.entries(COL_FILTERS).every(([col, set]) => set === null || set.has(rowVal(r, col)));
  }

  function renderLeadRows() {
    const rows = (Array.isArray(allRows) ? allRows : []).filter(passesColFilters);
    const body = $("#leadsBody");
    const filtered = rows.length !== allRows.length;
    const anyFilter = Object.values(COL_FILTERS).some(s => s !== null);

    $("#tableCount").textContent = `${rows.length} lead${rows.length === 1 ? "" : "s"}` +
      (filtered ? ` of ${allRows.length}` : "");
    $("#clearFilters").classList.toggle("hidden", !anyFilter);

    const empty = $("#tableEmpty");
    empty.classList.toggle("hidden", rows.length > 0);

    body.innerHTML = rows.map(r => `
      <tr data-id="${esc(r.lead_id)}" class="clickable">
        <td class="lead-id mono">${esc(r.lead_id)}</td>
        <td class="mono">${esc(r.account_no_lan || "—")}</td>
        <td>${esc(r.applicant_name || "—")}</td>
        <td class="mono" style="text-align:right">${fmtINR(r.sanction_amount)}</td>
        <td class="mono" style="text-align:right;font-weight:600">${fmtINR(r.tos)}</td>
        <td>${propBadge(r.property_verification_status)}</td>
        <td>${badge(r.processing_status)}</td>
        <td class="cell-time mono">${fmtTime(r.updated_at)}</td>
      </tr>`).join("");

    $$("#leadsBody tr").forEach(tr => tr.onclick = () => openLead(tr.dataset.id));
    $$("th[data-col]").forEach(th => th.classList.toggle("filtered", COL_FILTERS[th.dataset.col] !== null));
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
    closeColFilter();
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
    return map[k] || k.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
  }

  function isFinancialField(k) {
    return ["sanction_amount", "future_principal", "principal_overdue", "interest_overdue",
            "interest_on_termination", "late_payment_penal", "cheque_bounce_inc_gst",
            "other_charges_inc_gst", "foreclosure_charges", "litigation_charges",
            "excess_amount", "tos"].includes(k);
  }

  function formatFieldValue(k, v) {
    if (v == null || v === "" || v === "—") return "—";
    if (isFinancialField(k) && typeof v === "number") return fmtINR(v);
    if (typeof v === "object") return JSON.stringify(v);
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
      const fieldCount = Object.keys(p.fields || {}).length;
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
                ${Object.entries(p.fields || {}).map(([fk, fv]) => `
                  <tr>
                    <td class="pft-k" style="padding:8px; border-bottom:1px solid var(--line-light); vertical-align:top;">${esc(formatFieldLabel(fk))}</td>
                    <td class="pft-v${isFinancialField(fk) ? ' mono' : ''}" style="padding:8px; border-bottom:1px solid var(--line-light); font-weight:500;">${esc(formatFieldValue(fk, fv))}</td>
                  </tr>`).join("")}
              </tbody>
            </table>` : `<div style="color:var(--ink-soft); font-size:13px; padding:12px; background:var(--surface); border-radius:4px; border:1px dashed var(--line-strong);">No specific key fields extracted from this page.</div>`}
        </div>
        ${p.snippet ? `
        <div>
          <h4 style="margin:0 0 8px 0; font-size:13px; color:var(--ink-faint); text-transform:uppercase; letter-spacing:0.05em;">Text Evidence</h4>
          <div class="evidence-snippet" style="background:var(--surface); padding:12px; border-radius:6px; font-family:'JetBrains Mono',monospace; font-size:12px; color:var(--ink-strong); border:1px solid var(--line); white-space:pre-wrap;">${esc(p.snippet)}</div>
        </div>` : ""}
        ${p.raw_ocr_text ? `
        <div style="margin-top:20px;">
          <h4 style="margin:0 0 8px 0; font-size:13px; color:var(--ink-faint); text-transform:uppercase; letter-spacing:0.05em;">Raw OCR Text Extraction</h4>
          <div style="background:var(--surface); padding:12px; border-radius:6px; font-family:'JetBrains Mono',monospace; font-size:11px; color:var(--ink-soft); border:1px solid var(--line); white-space:pre-wrap; max-height:400px; overflow-y:auto;">${esc(p.raw_ocr_text)}</div>
        </div>` : ""}
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

  /* ── lead drawer ───────────────────────── */
  function renderDrawer(j) {
    const f = j.final || {};
    const lead = j.lead || {};
    const status = f.processing_status || lead.status || "pending";
    $("#dLeadId").textContent = j.lead_id;
    const b = $("#dStatus"); b.className = "badge"; b.innerHTML = badge(status);
    $("#dRawLink").href = "/logs/legal/" + encodeURIComponent(j.lead_id);

    const raw = f.raw_extractions || {};
    const pageExtractions = Array.isArray(raw.page_extractions) ? raw.page_extractions : [];
    const fieldProv = (raw.field_provenance && typeof raw.field_provenance === "object") ? raw.field_provenance : {};
    const leadMeta = raw.lead_metadata || {};
    const docs = j.documents || [];

    // 1. High-Level Summary Banner
    const summary = `
      <div class="lead-summary">
        <div class="ls-cell"><div class="ls-k">Account LAN</div><div class="ls-v mono">${esc(f.account_no_lan || lead.account_lan || "—")}</div></div>
        <div class="ls-cell"><div class="ls-k">Primary Applicant</div><div class="ls-v">${esc(f.applicant_name || lead.lead_name || "—")}</div></div>
        <div class="ls-cell"><div class="ls-k">Sanction Amount</div><div class="ls-v mono">${fmtINR(f.sanction_amount)}</div></div>
        <div class="ls-cell"><div class="ls-k">Total Outstanding (TOS)</div><div class="ls-v mono" style="font-weight:700">${fmtINR(f.tos)}</div></div>
      </div>`;

    // 2. Comprehensive Lead Metadata Grid
    const pt = f.phase_timings || leadMeta.phase_timings || {};
    const timingStr = Object.entries(pt).map(([k, ms]) => `${k.replace('_ms','').replace('phase','')}: ${ms}ms`).join(" · ") || "—";
    const routesStr = (f.ocr_routes_used || leadMeta.ocr_routes_used || []).join(", ") || "RapidOCR + VLM";
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
    // Start with the standard fixed schema keys
    const baseKeys = [
      "account_no_lan", "applicant_name", "applicant_address",
      "co_applicant_1", "co_applicant_address_1", "co_applicant_2", "co_applicant_address_2",
      "guarantor_1", "guarantor_1_add",
      "sanction_amount", "sanction_date", "disbursal_date", "roi_in_number", "npa_date",
      "future_principal", "principal_overdue", "interest_overdue", "interest_on_termination",
      "late_payment_penal", "cheque_bounce_inc_gst", "other_charges_inc_gst", 
      "foreclosure_charges", "litigation_charges", "tos",
      "mortgaged_property_detail_1", "directions", "property_owner_mortgagor"
    ];
    
    // Add any custom fields extracted by the LLM that were placed in custom_fields
    const customFields = (f.extracted_data && f.extracted_data.custom_fields) ? f.extracted_data.custom_fields : {};
    const customKeys = Object.keys(customFields);
    const extractKeys = [...new Set([...baseKeys, ...customKeys])];
    
    // Create a flat dictionary combining base properties and custom_fields properties
    const flatF = { ...f, ...customFields };
    
    let consolidatedHtml = "";
    const hasData = extractKeys.some(k => flatF[k] != null && flatF[k] !== "");
    if (hasData) {
      consolidatedHtml = `
        <div class="d-section" style="margin-top:20px;">
          <h4>Consolidated Dossier Extractions <span class="n">Final merged payload</span></h4>
          <table class="page-fields-table" style="width:100%; border-collapse:collapse; margin-top:12px; background:var(--surface); border:1px solid var(--line); border-radius:6px; overflow:hidden;">
            <tbody>
              ${extractKeys.filter(k => flatF[k] != null && flatF[k] !== "").map(k => `
                <tr>
                  <td class="pft-k" style="padding:10px 14px; border-bottom:1px solid var(--line-light); vertical-align:top; width:220px;">${esc(formatFieldLabel(k))}</td>
                  <td class="pft-v${isFinancialField(k) ? ' mono' : ''}" style="padding:10px 14px; border-bottom:1px solid var(--line-light); font-weight:500;">${esc(formatFieldValue(k, flatF[k]))}</td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>`;
    }

    // 3. Document & Page-by-Page Extraction Provenance Section (ONLY PAGES WITH EXTRACTED DATA)
    const docsWithPages = docs.filter(d => {
      const dPages = pageExtractions.filter(p => p.document_id === d.document_id || p.filename === d.filename);
      if (dPages.some(p => Object.keys(p.fields || {}).length > 0)) return true;
      if (d.extracted_data && Array.isArray(d.extracted_data.pages) && d.extracted_data.pages.some(p => Object.keys(p.fields || {}).length > 0)) return true;
      return false;
    });
    const docsNoPages = docs.filter(d => !docsWithPages.includes(d));

    const docProvenanceSection = `
      <div class="d-section">
        <h4>Page Extraction Provenance <span class="n">${docsWithPages.length} document${docsWithPages.length === 1 ? '' : 's'} with extracted data</span></h4>
        <p class="lede" style="margin-bottom:14px">Exact pages where data was extracted — side-by-side visual evidence with the precise fields pulled from each page.</p>
          ${docsWithPages.map(d => {
          let dPages = pageExtractions.filter(p => (p.document_id === d.document_id || p.filename === d.filename) && Object.keys(p.fields || {}).length > 0);
          if (!dPages.length && d.extracted_data && Array.isArray(d.extracted_data.pages)) {
            dPages = d.extracted_data.pages.filter(p => Object.keys(p.fields || {}).length > 0);
          }
          
          dPages.forEach(p => {
            window.__pageData[`${d.document_id}-${p.page_number}`] = p;
          });

          return `
            <div class="doc-provenance-box" id="doc-${esc(d.document_id)}">
              <div class="doc-prov-head">
                <div class="doc-prov-title">
                  <span>📄 ${esc(d.filename)}</span>
                  <span class="tag" style="background:var(--surface);border:1px solid var(--line-strong)">${esc(d.document_type || "unclassified")}</span>
                  ${(d.metadata && d.metadata.shared_fcl) ? '<span class="tag warn" style="font-size:10.5px">SHARED FCL</span>' : ''}
                </div>
                <div class="doc-prov-meta">
                  <span>${formatBytes(d.file_size_bytes)}</span>
                  <span>·</span>
                  <span>${d.page_count || 1} page${(d.page_count || 1) === 1 ? "" : "s"} total</span>
                  <span>·</span>
                  <span class="mono">${esc(d.ocr_route || "—")}</span>
                  <a class="btn line small" href="/api/legal/document/${encodeURIComponent(d.document_id)}/file" target="_blank" style="margin-left:6px">View File ↗</a>
                </div>
              </div>
              <div class="page-cards-list">
                ${dPages.map(p => `
                  <div class="page-card" id="page-${esc(d.document_id)}-${p.page_number}">
                    <!-- Left: Page Preview -->
                    <div class="page-card-side">
                      <div class="page-num-badge">PAGE ${p.page_number} OF ${d.page_count || p.total_pages || 1}</div>
                      <div class="page-thumb-wrap" onclick="openPageModal('${d.document_id}', '${esc(d.filename)}', ${p.page_number}, ${d.page_count || 1})" title="Click to view full-resolution page">
                        <img class="page-thumb-img" src="/api/legal/document/${encodeURIComponent(d.document_id)}/page/${encodeURIComponent(p.page_number)}" loading="lazy" alt="Page ${p.page_number}" onerror="this.onerror=null;this.src='';this.alt='Preview unavailable'">
                      </div>
                      <a href="javascript:void(0)" onclick="openPageModal('${d.document_id}', '${esc(d.filename)}', ${p.page_number}, ${d.page_count || 1})" style="font-size:11px;font-family:'JetBrains Mono',monospace;color:var(--accent);text-decoration:none;font-weight:600">Enlarge Page ↗</a>
                      <span class="page-route-badge">${esc(p.ocr_route || p.phase || "Engine")}</span>
                    </div>
                    <!-- Right: Extracted Data from This Exact Page -->
                    <div class="page-card-main">
                      <div class="page-sec-title">DATA EXTRACTED FROM THIS PAGE (${Object.keys(p.fields || {}).length} field${Object.keys(p.fields || {}).length === 1 ? "" : "s"})</div>
                      ${Object.keys(p.fields || {}).length > 0 ? `
                        <table class="page-fields-table">
                          <tbody>
                            ${Object.entries(p.fields || {}).map(([fk, fv]) => `
                              <tr>
                                <td class="pft-k">${esc(formatFieldLabel(fk))}</td>
                                <td class="pft-v${isFinancialField(fk) ? ' mono' : ''}">${esc(formatFieldValue(fk, fv))}</td>
                              </tr>`).join("")}
                          </tbody>
                        </table>` : `<div style="font-size:12.5px;color:var(--ink-faint);padding:6px 0">No direct key fields extracted on this page.</div>`}
                      ${p.snippet ? `
                        <div>
                          <div class="page-sec-title" style="margin-top:6px">TEXT EVIDENCE ON PAGE</div>
                          <div class="evidence-snippet">${esc(p.snippet)}</div>
                        </div>` : ""}
                    </div>
                  </div>`).join("")}
              </div>
            </div>`;
        }).join("")}
        ${docsNoPages.length > 0 ? `
          <div style="margin-top:14px;padding:12px 16px;border:1px solid var(--line);background:var(--surface-2);border-radius:var(--radius-sm)">
            <div style="font-size:11px;font-weight:600;letter-spacing:.06em;color:var(--ink-faint);text-transform:uppercase;margin-bottom:8px">Ingested — No extractable data found</div>
            ${docsNoPages.map(d => `
              <div style="display:flex;align-items:center;gap:10px;padding:5px 0;border-bottom:1px solid var(--line)">
                <span style="font-size:13px">📄</span>
                <span style="font-size:12.5px;font-family:'JetBrains Mono',monospace;flex:1">${esc(d.filename)}</span>
                <span class="tag" style="font-size:10.5px">${esc(d.document_type || "unclassified")}</span>
                <span style="font-size:11.5px;color:var(--ink-faint)">${formatBytes(d.file_size_bytes)}</span>
                <a class="btn line small" href="/api/legal/document/${encodeURIComponent(d.document_id)}/file" target="_blank">View ↗</a>
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

    $("#drawerBody").innerHTML = summary + metaSection + consolidatedHtml + docProvenanceSection + eventLogsHtml;
  }

  async function openLead(id) {
    try {
      const r = await fetch("/api/legal/lead/" + encodeURIComponent(id));
      if (!r.ok) { toast("Lead not found: " + esc(id), "bad"); return; }
      renderDrawer(await r.json());
      $("#drawer").classList.add("open"); $("#scrim").classList.add("open");
    } catch (e) { toast("Failed to open lead", "bad"); }
  }

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
        const done = (p.leads_completed || 0) + (p.leads_partial || 0) + (p.leads_failed || 0);
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
          <div class="mini gray"><div class="mn">${(p.leads_pending || 0) + (p.leads_paused || 0)}</div><div class="ml">pending</div></div>`;
        if ($("#miniCounts").innerHTML !== newMiniCountsHTML) $("#miniCounts").innerHTML = newMiniCountsHTML;

        const liveList = $("#liveList");
        
        // Remove rows that no longer exist (optional, mostly they just get added or updated)
        const currentIds = new Set((p.leads || []).map(x => x.lead_id));
        $$(".live-row", liveList).forEach(el => {
            if (!currentIds.has(el.dataset.id)) el.remove();
        });

        (p.leads || []).forEach(x => {
          const st = x.result_status || x.status || "pending";
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
          toast(`✓ Dossier batch complete — ${p.leads_completed || 0} extracted`, "ok");
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
        if (xhr.status === 401) { window.location.href = "/login?next=" + encodeURIComponent(location.pathname); reject(new Error("unauthorized")); return; }
        try { resolve({ json: () => Promise.resolve(JSON.parse(xhr.responseText)) }); }
        catch (err) { reject(err); }
      };
      xhr.onerror = () => { btn.classList.remove("btn-progress"); reject(new Error("Network Error")); };
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
        } else if (selectedBatchType === "folder" && selectedFolderFiles.length) {
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
            toast(esc(r.error), "bad");
            runZip.disabled = false;
            runZip.textContent = "Process Ingest";
            return;
          }
          toast(`Enqueued ${r.leads_enqueued || 0} lead(s) with ${r.documents_enqueued || 0} document(s)`, "ok");
          startBatchPoll(r.batch_id);
        } catch (e) {
          toast("Upload failed", "bad");
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
          const r = await (await uploadWithProgress("/api/legal/upload_folder", fd, runDoc, "Ingesting…")).json();
          if (r.error) { toast(esc(r.error), "bad"); runDoc.disabled = false; runDoc.textContent = "Process Dossier"; return; }
          toast(`Enqueued ${r.leads_enqueued || 0} dossier(s)`, "ok");
          startBatchPoll(r.batch_id);
        } catch (e) { toast("Dossier upload failed", "bad"); runDoc.disabled = false; runDoc.textContent = "Process Dossier"; }
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

  /* ── observability view ─────────────────── */
  async function loadObservability() {
    try {
      const d = await (await fetch(`/api/legal/observability?scope=${state.scope}`)).json();
      const c = d.lead_counts || {};
      $("#obsKpis").innerHTML = [
        ["tot", "Total Dossiers", c.total || 0],
        ["ok", "Completed", c.completed || 0],
        ["warn", "Partial", c.partial || 0],
        ["bad", "Failed", c.failed || 0],
      ].map(([cls, lbl, n]) => `<div class="stat ${cls}"><div class="n">${n}</div><div class="l">${lbl}</div></div>`).join("");

      if (!window.Chart) return;

      // 1. OCR Route Distribution Chart
      const routes = d.ocr_routes || [];
      const ocrCvs = $("#obsOcrChart");
      if (ocrCvs) {
        if (!routes.length) {
          emptyCanvas("obsOcr", "obsOcrChart", "No documents routed yet");
          $("#obsOcrLegend").innerHTML = "";
        } else {
          const labels = routes.map(r => r.route || "unknown");
          const data = routes.map(r => r.n);
          const colors = [cssVar("--ch-ok"), cssVar("--accent"), cssVar("--ch-warn"), cssVar("--ch-neutral")];
          $("#obsOcrLegend").innerHTML = labels.map((l, i) =>
            `<span class="li"><span class="sw" style="background:${colors[i % colors.length]}"></span>${l} · <b>${data[i]}</b></span>`).join("");
          if (charts.obsOcr) {
            charts.obsOcr.data.labels = labels;
            charts.obsOcr.data.datasets[0].data = data;
            charts.obsOcr.update();
          } else {
            charts.obsOcr = new Chart(ocrCvs, {
              type: "doughnut",
              data: { labels, datasets: [{ data, backgroundColor: colors, borderWidth: 2, borderColor: cssVar("--surface") }] },
              options: { cutout: "65%", plugins: { legend: { display: false } } }
            });
          }
        }
      }

      // 2. Phase Timings Chart
      const phases = d.phase_timings || [];
      const phaseCvs = $("#obsPhaseChart");
      if (phaseCvs) {
        if (!phases.length) {
          emptyCanvas("obsPhase", "obsPhaseChart", "No phase timing data yet");
        } else {
          const labels = phases.map(p => p.stage);
          const data = phases.map(p => p.avg_ms);
          if (charts.obsPhase) {
            charts.obsPhase.data.labels = labels;
            charts.obsPhase.data.datasets[0].data = data;
            charts.obsPhase.update();
          } else {
            charts.obsPhase = new Chart(phaseCvs, {
              type: "bar",
              data: { labels, datasets: [{ data, backgroundColor: cssVar("--accent"), barThickness: 14 }] },
              options: {
                indexAxis: "y", plugins: { legend: { display: false } },
                scales: {
                  x: { grid: { color: cssVar("--line") }, border: { display: false }, ticks: { color: cssVar("--ink-faint"), font: { family: "JetBrains Mono", size: 11 } } },
                  y: { grid: { display: false }, border: { color: cssVar("--line-strong") }, ticks: { color: cssVar("--ink-soft"), font: { family: "Hanken Grotesk", size: 12 } } }
                }
              }
            });
          }
        }
      }

      // 3. Property Verification Status Chart
      const props = d.property_verification || [];
      const propCvs = $("#obsPropChart");
      if (propCvs) {
        if (!props.length) {
          emptyCanvas("obsProp", "obsPropChart", "No property verifications yet");
          $("#obsPropLegend").innerHTML = "";
        } else {
          const labels = props.map(p => (p.status || "").replace(/_/g, " "));
          const data = props.map(p => p.n);
          const colors = [cssVar("--ch-ok"), cssVar("--ch-warn"), cssVar("--ch-bad")];
          $("#obsPropLegend").innerHTML = labels.map((l, i) =>
            `<span class="li"><span class="sw" style="background:${colors[i % colors.length]}"></span>${l} · <b>${data[i]}</b></span>`).join("");
          if (charts.obsProp) {
            charts.obsProp.data.labels = labels;
            charts.obsProp.data.datasets[0].data = data;
            charts.obsProp.update();
          } else {
            charts.obsProp = new Chart(propCvs, {
              type: "doughnut",
              data: { labels, datasets: [{ data, backgroundColor: colors, borderWidth: 2, borderColor: cssVar("--surface") }] },
              options: { cutout: "65%", plugins: { legend: { display: false } } }
            });
          }
        }
      }

      // 4. Circuit Breaker & Health Box
      const cb = d.circuit_breaker || {};
      $("#obsBreakerBox").innerHTML = `
        <div class="obs-kv">
          <div class="obs-kv-cell"><div class="obs-kv-n">${badge(cb.state || "closed")}</div><div class="obs-kv-l">Breaker State</div></div>
          <div class="obs-kv-cell"><div class="obs-kv-n">${cb.total_trips || 0}</div><div class="obs-kv-l">Total Failovers</div></div>
          <div class="obs-kv-cell"><div class="obs-kv-n">${cb.total_successes || 0}</div><div class="obs-kv-l">Successful Calls</div></div>
        </div>`;
    } catch (e) {
      toast("Failed to load observability", "bad");
    }
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

  /* ── initialization ─────────────────────── */
  function init() {
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
    const exportBtn = $("#exportCsvBtn");
    if (exportBtn) {
      exportBtn.onclick = () => {
        const rows = (Array.isArray(allRows) ? allRows : []).filter(passesColFilters);
        if (!rows.length) return toast("No leads to export", "warn");
        const keys = ["lead_id", "batch_id", "processing_status", "account_no_lan", "applicant_name", "sanction_amount", "total_outstanding_amount", "npa_date", "property_verification_status", "total_documents", "failed_documents", "created_at"];
        let csv = keys.join(",") + "\\n";
        rows.forEach(r => {
          csv += keys.map(k => {
            let v = String(r[k] ?? "").replace(/"/g, '""');
            return v.includes(",") || v.includes("\\n") || v.includes('"') ? `"${v}"` : v;
          }).join(",") + "\\n";
        });
        const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8;' }));
        const a = document.createElement("a");
        a.href = url; a.download = `sarfaesi_leads_${new Date().toISOString().split("T")[0]}.csv`;
        document.body.appendChild(a); a.click(); a.remove();
        URL.revokeObjectURL(url);
      };
    }
    $("#clearFilters").onclick = resetColFilters;

    $$("#statusChips .chip").forEach(c => {
      c.onclick = () => {
        $$("#statusChips .chip").forEach(x => x.classList.remove("active"));
        c.classList.add("active");
        state.status = c.dataset.status;
        loadLeads();
      };
    });

    // Header column filter dropdowns
    $$("th.th-filter").forEach(th => th.onclick = (e) => {
      e.stopPropagation();
      openColFilter(th.dataset.col, th);
    });

    // Drawer close
    $("#drawerClose").onclick = closeDrawer;
    $("#scrim").onclick = closeDrawer;

    // Keydown escape listener
    document.addEventListener("keydown", e => {
      if (e.key === "Escape") {
        closeColFilter();
        closeDrawer();
        closePageModal();
        $("#cfgModal").classList.remove("open");
        $("#cfgScrim").classList.remove("open");
      }
    });

    // Upload & demo handlers
    initUploads();
    initModelConfig();

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
