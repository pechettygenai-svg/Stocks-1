/* Stock Forecasting Agent web UI. Talks only to the local /v1 API. */
(() => {
  const $ = (sel, el = document) => el.querySelector(sel);
  const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const STAGES = ["queued", "collecting", "validating", "modeling", "reviewing", "complete"];

  let current = null;
  let pollTimer = null;

  // ---------- formatting ----------
  function fmtNum(v, unit) {
    if (v === null || v === undefined || v === "") return "—";
    if (typeof v !== "number") return esc(v);
    if (unit === "ratio" || unit === "pct" || unit === "%") return (v * 100).toFixed(1) + "%";
    const a = Math.abs(v);
    if (a >= 1e12) return (v / 1e12).toFixed(2) + "T";
    if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(1) + "M";
    if (a >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 0 });
    return Number.isInteger(v) ? String(v) : v.toFixed(2);
  }
  const pct = (v) => (v === null || v === undefined ? "—" : (v * 100).toFixed(1) + "%");
  const money = (v) => (v === null || v === undefined ? "—" : v.toFixed(2));

  // ---------- minimal markdown ----------
  function inline(s) {
    s = esc(s);
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    s = s.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    s = s.replace(/\[(ev-[0-9a-z-]+)\]/g, '<span class="cite" data-ev="$1">[$1]</span>');
    return s;
  }
  function markdown(md) {
    const lines = md.split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^\s*$/.test(line)) { i++; continue; }
      let m;
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) { out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); i++; continue; }
      if (line.startsWith(">")) {
        const buf = [];
        while (i < lines.length && lines[i].startsWith(">")) buf.push(lines[i].replace(/^>\s?/, "")), i++;
        out.push(`<blockquote>${inline(buf.join(" "))}</blockquote>`); continue;
      }
      if (line.startsWith("|")) {
        const rows = [];
        while (i < lines.length && lines[i].startsWith("|")) rows.push(lines[i]), i++;
        const cells = (r) => r.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        const head = cells(rows[0]);
        const body = rows.slice(1).filter((r) => !/^\|\s*:?-{2,}/.test(r));
        out.push("<table><thead><tr>" + head.map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>" +
          body.map((r) => "<tr>" + cells(r).map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("") + "</tbody></table>");
        continue;
      }
      if (/^\s*[-*]\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) items.push(lines[i].replace(/^\s*[-*]\s+/, "")), i++;
        out.push("<ul>" + items.map((t) => `<li>${inline(t)}</li>`).join("") + "</ul>"); continue;
      }
      if (/^\s*\d+\.\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) items.push(lines[i].replace(/^\s*\d+\.\s+/, "")), i++;
        out.push("<ol>" + items.map((t) => `<li>${inline(t)}</li>`).join("") + "</ol>"); continue;
      }
      const buf = [];
      while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^(#|>|\||\s*[-*]\s|\s*\d+\.\s)/.test(lines[i])) buf.push(lines[i]), i++;
      out.push(`<p>${inline(buf.join(" "))}</p>`);
    }
    return out.join("\n");
  }

  // ---------- API ----------
  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}: ${await r.text()}`);
    return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
  }

  // ---------- form ----------
  const form = $("#run-form");
  const statusEl = $("#status");
  const ufRows = $("#uf-rows");

  function addUfRow() {
    const row = document.createElement("div");
    row.className = "uf-row";
    row.innerHTML = `
      <select name="uf-source"><option>Fidelity</option><option>MSN Money</option><option>Other</option></select>
      <input name="uf-as-of" type="date" title="as-of date">
      <input name="uf-low" type="number" step="any" placeholder="low">
      <input name="uf-mean" type="number" step="any" placeholder="mean / target">
      <input name="uf-high" type="number" step="any" placeholder="high">
      <input name="uf-analysts" type="number" min="0" placeholder="# analysts">
      <input name="uf-url" class="full" placeholder="page URL you read it from (optional)">
      <label class="inline full"><input type="checkbox" name="uf-method"> methodology disclosed on the page</label>`;
    ufRows.appendChild(row);
  }
  $("#uf-add").addEventListener("click", addUfRow);

  function readUserForecasts() {
    return $$(".uf-row", ufRows).map((row) => {
      const g = (n) => $(`[name=${n}]`, row);
      const num = (n) => (g(n).value === "" ? null : Number(g(n).value));
      return {
        source: g("uf-source").value,
        url: g("uf-url").value || null,
        horizon: $("#horizon").value,
        value_low: num("uf-low"), value_mean: num("uf-mean"), value_high: num("uf-high"),
        as_of: g("uf-as-of").value || null,
        analyst_count: num("uf-analysts"),
        method_disclosed: g("uf-method").checked,
      };
    }).filter((f) => f.value_low !== null || f.value_mean !== null || f.value_high !== null);
  }

  function renderStatus(status, error) {
    if (error) { statusEl.innerHTML = `<span class="error">${esc(error)}</span>`; return; }
    if (!status) { statusEl.textContent = ""; return; }
    const idx = STAGES.indexOf(status);
    statusEl.innerHTML = STAGES.filter((s) => s !== "queued").map((s, k) => {
      const j = k + 1;
      const cls = status === "failed" ? "" : j < idx || status === "complete" ? "done" : j === idx ? "now" : "";
      return `<span class="step ${cls}">${s}</span>`;
    }).join("") + (status === "failed" ? '<span class="error"> failed</span>' : "");
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const ticker = $("#ticker").value.trim().toUpperCase();
    if (!ticker) return;
    const body = {
      ticker,
      horizon: $("#horizon").value,
      include_sources: $$("input[name=src]:checked").map((c) => c.value),
      user_forecasts: readUserForecasts(),
    };
    $("#run-btn").disabled = true;
    renderStatus("queued");
    try {
      const run = await api("/v1/analyses", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
      await poll(run.run_id);
    } catch (err) {
      renderStatus(null, err.message);
      $("#run-btn").disabled = false;
    }
    loadHistory();
  });

  async function poll(runId) {
    clearTimeout(pollTimer);
    const s = await api(`/v1/analyses/${runId}`);
    renderStatus(s.status, s.error);
    if (s.status === "complete" || s.status === "failed") {
      $("#run-btn").disabled = false;
      await show(runId);
      loadHistory();
      return;
    }
    pollTimer = setTimeout(() => poll(runId).catch((e) => renderStatus(null, e.message)), 1200);
  }

  // ---------- history ----------
  async function loadHistory() {
    const list = $("#history");
    try {
      const runs = await api("/v1/analyses");
      if (!runs.length) { list.innerHTML = '<li class="s">No runs yet (history is in-memory; it clears on server restart).</li>'; return; }
      list.innerHTML = runs.map((r) => `
        <li data-run="${esc(r.run_id)}">
          <span><span class="t">${esc(r.ticker)}</span> <span class="s">${esc(r.horizon)}</span></span>
          <span class="s">${esc(r.status)}${r.quality_gate_failures ? ` · ${r.quality_gate_failures} gate` : ""}</span>
        </li>`).join("");
      $$("li[data-run]", list).forEach((li) => li.addEventListener("click", () => show(li.dataset.run)));
    } catch (e) { list.innerHTML = `<li class="error">${esc(e.message)}</li>`; }
  }

  // ---------- result rendering ----------
  async function show(runId) {
    const res = await api(`/v1/analyses/${runId}/full`);
    current = res;
    $("#empty").hidden = true;
    $("#result").hidden = false;
    const req = res.request || {};
    $("#title").textContent = `${res.company_name || req.ticker} (${req.ticker})`;
    res.as_of = (res.started_at || "").slice(0, 16).replace("T", " ") + " UTC";
    $("#meta").textContent = `run ${res.run_id} · horizon ${req.horizon} · run started ${res.as_of} · status ${res.status}` +
      (res.revision_count ? ` · revised ${res.revision_count}×` : "");
    const gates = res.quality_gate_failures || [];
    $("#gates").innerHTML = gates.length
      ? `<span class="badge bad">${gates.length} quality gate failure(s)</span>` + gates.map((g) => `<span class="badge muted">${esc(g)}</span>`).join("")
      : '<span class="badge ok">all quality gates passed</span>';
    $("#report").innerHTML = res.report_markdown ? markdown(res.report_markdown) : `<p class="error">${esc(res.error || "no report")}</p>`;
    $$(".cite", $("#report")).forEach((c) => c.addEventListener("click", () => jumpToEvidence(c.dataset.ev)));
    renderScenarios(res);
    renderForecasts(res);
    renderFundamentals(res);
    renderValuation(res);
    renderEvidence(res);
    renderReview(res);
    renderStatus(res.status, res.error);
  }

  function calc(res, name) { return (res.calculations || []).find((c) => c.name === name); }

  // Mirrors stock_forecaster.analytics.scenarios.implied_price so sliders recompute locally.
  function impliedPrice(d, inp, mode) {
    const T = inp.horizon_years;
    if (mode === "earnings") {
      if (inp.revenue && inp.shares) {
        const ni = inp.revenue * Math.pow(1 + d.g, T) * d.m;
        const sh = inp.shares * Math.pow(1 + d.sc, T);
        return sh > 0 && ni > 0 ? (ni / sh) * d.x : null;
      }
      if (inp.eps) {
        const eps = (inp.eps * Math.pow(1 + d.g, T)) / Math.pow(1 + d.sc, T);
        return eps > 0 ? eps * d.x : null;
      }
      return null;
    }
    if (inp.revenue && inp.shares) {
      const sh = inp.shares * Math.pow(1 + d.sc, T);
      return sh > 0 ? ((inp.revenue * Math.pow(1 + d.g, T)) / sh) * d.x : null;
    }
    return null;
  }

  function renderScenarios(res) {
    const el = $("#tab-scenarios");
    const sc = res.scenarios || [];
    const c = calc(res, "scenarios");
    if (!sc.length) { el.innerHTML = '<p class="warn-list">Scenarios unavailable: ' + esc((c?.warnings || []).join("; ") || "insufficient inputs") + "</p>"; return; }
    const price = c?.inputs?.price;
    let html = `<div class="callout">Probabilities are neutral starting assumptions (25/50/25), not estimates of likelihood. Ranges are model output, not predictions.${price ? ` Current price ${money(price)} (${esc(res.as_of || "")}).` : ""}</div>`;
    html += "<table><thead><tr><th>Scenario</th><th class=num>Prob.</th><th class=num>Rev growth/yr</th><th class=num>Net margin</th><th class=num>Share chg/yr</th><th class=num>Exit multiple</th><th class=num>Implied range</th><th class=num>vs price</th><th>What must be true</th></tr></thead><tbody>";
    for (const s of sc) {
      const d = s.drivers;
      const up = c?.outputs?.upside_downside_vs_price?.[s.name];
      html += `<tr><td><strong>${esc(s.name)}</strong></td><td class=num>${pct(s.probability)}</td><td class=num>${pct(d.revenue_growth)}</td><td class=num>${pct(d.operating_margin)}</td><td class=num>${pct(d.share_change)}</td><td class=num>${d.exit_multiple.toFixed(1)}x</td>
        <td class=num>${money(s.implied_price_low)} – ${money(s.implied_price_high)}<br><small>mid ${money(s.implied_price_mid)}</small></td><td class=num>${up == null ? "—" : pct(up)}</td>
        <td><ul>${(s.what_must_be_true || []).map((t) => `<li>${esc(t)}</li>`).join("")}</ul></td></tr>`;
    }
    html += "</tbody></table>";
    const sens = c?.outputs?.sensitivity_base;
    if (sens && Object.keys(sens).length) {
      const cols = Object.keys(Object.values(sens)[0]);
      html += '<h3 class="sub">Sensitivity (base case): growth × exit multiple → implied price</h3><table><thead><tr><th>growth \\ multiple</th>' + cols.map((k) => `<th class=num>${esc(k)}</th>`).join("") + "</tr></thead><tbody>";
      for (const [g, row] of Object.entries(sens)) html += `<tr><td>${esc(g)}</td>` + cols.map((k) => `<td class=num>${row[k] == null ? "—" : money(row[k])}</td>`).join("") + "</tr>";
      html += "</tbody></table>";
    }
    if (c?.inputs) {
      const base = sc.find((s) => s.name.toLowerCase() === "base") || sc[0];
      html += `<h3 class="sub">Scenario sliders (recomputed locally, formula ${esc(c.formula_version)})</h3>
        <div class="sliders">
          <label>Revenue growth / yr <output id="sl-g-out"></output><input type="range" id="sl-g" min="-30" max="60" step="0.5"></label>
          <label>Net margin <output id="sl-m-out"></output><input type="range" id="sl-m" min="0" max="60" step="0.5"></label>
          <label>Share count change / yr <output id="sl-sc-out"></output><input type="range" id="sl-sc" min="-10" max="10" step="0.25"></label>
          <label>Exit multiple <output id="sl-x-out"></output><input type="range" id="sl-x" min="2" max="80" step="0.5"></label>
        </div>
        <div class="kv"><div><div class="k">Implied price (${esc(c.inputs.mode)} mode, ${esc(c.inputs.horizon_years)}y)</div><div class="v" id="sl-price"></div></div>
        <div><div class="k">vs current price</div><div class="v" id="sl-up"></div></div></div>
        <div class="callout">Slider output is a deterministic model result from your own assumptions, not a forecast.</div>`;
      el.innerHTML = html;
      const d0 = base.drivers;
      $("#sl-g").value = d0.revenue_growth * 100; $("#sl-m").value = d0.operating_margin * 100;
      $("#sl-sc").value = d0.share_change * 100; $("#sl-x").value = d0.exit_multiple;
      const upd = () => {
        const d = { g: +$("#sl-g").value / 100, m: +$("#sl-m").value / 100, sc: +$("#sl-sc").value / 100, x: +$("#sl-x").value };
        $("#sl-g-out").textContent = pct(d.g); $("#sl-m-out").textContent = pct(d.m); $("#sl-sc-out").textContent = pct(d.sc); $("#sl-x-out").textContent = d.x.toFixed(1) + "x";
        const p = impliedPrice(d, c.inputs, c.inputs.mode);
        $("#sl-price").textContent = p == null ? "n/a (non-positive earnings or shares)" : money(p);
        $("#sl-up").textContent = p == null || !price ? "—" : pct(p / price - 1);
      };
      $$("#tab-scenarios input[type=range]").forEach((r) => r.addEventListener("input", upd));
      upd();
      return;
    }
    el.innerHTML = html;
  }

  function renderForecasts(res) {
    const el = $("#tab-forecasts");
    const fc = res.forecasts || [];
    if (!fc.length) { el.innerHTML = "<p>No external forecasts collected.</p>"; return; }
    let html = '<div class="callout">External forecasts are source opinions shown for comparison; they are not averaged and carry no weight beyond context. Duplicates, stale entries, and outliers are flagged rather than removed.</div>';
    html += "<table><thead><tr><th>Source</th><th>Type</th><th>Horizon</th><th class=num>Low</th><th class=num>Mean</th><th class=num>High</th><th>As of</th><th class=num>Analysts</th><th>Method</th><th>Weight</th><th>Flags</th><th>Evidence</th></tr></thead><tbody>";
    for (const f of fc) {
      const unavailable = f.value_mean == null && f.value_low == null && f.value_high == null;
      const flags = [];
      if (f.duplicate_of) flags.push(`<span class="badge warn">duplicate of ${esc(f.duplicate_of)}</span>`);
      if (f.stale) flags.push('<span class="badge warn">stale</span>');
      if (f.outlier) flags.push('<span class="badge warn">outlier</span>');
      if (unavailable) flags.push('<span class="badge muted">unavailable</span>');
      if ((f.notes || []).some((n) => /user/i.test(n))) flags.push('<span class="badge muted">user-supplied</span>');
      html += `<tr class="${unavailable ? "unavailable" : ""}"><td>${f.url ? `<a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.source)}</a>` : esc(f.source)}</td><td>${esc(f.forecast_type)}</td><td>${esc(f.horizon)}</td>
        <td class=num>${money(f.value_low)}</td><td class=num>${money(f.value_mean)}</td><td class=num>${money(f.value_high)}</td><td>${esc(f.as_of || "—")}</td><td class=num>${f.analyst_count ?? "—"}</td>
        <td>${f.method_disclosed ? "disclosed" : "opaque"}</td><td>${esc(f.weight)}</td><td>${flags.join(" ")}${(f.notes || []).length ? `<br><small>${esc(f.notes.join("; "))}</small>` : ""}</td>
        <td>${(f.evidence_ids || []).map((id) => `<span class="cite" data-ev="${esc(id)}">[${esc(id)}]</span>`).join(" ")}</td></tr>`;
    }
    el.innerHTML = html + "</tbody></table>";
    $$(".cite", el).forEach((c) => c.addEventListener("click", () => jumpToEvidence(c.dataset.ev)));
  }

  function kvBlock(title, obj, fmt) {
    const entries = Object.entries(obj || {}).filter(([, v]) => v !== null && typeof v !== "object");
    if (!entries.length) return "";
    return `<h3 class="sub">${esc(title)}</h3><div class="kv">` + entries.map(([k, v]) => `<div><div class="k">${esc(k)}</div><div class="v">${fmt(k, v)}</div></div>`).join("") + "</div>";
  }
  const ratioish = (k) => /margin|growth|yield|roe|roa|return|ratio|change|rate|drawdown|volatility/i.test(k) && !/pe|ps|pb|multiple|current_ratio/i.test(k);

  function renderFundamentals(res) {
    const el = $("#tab-fundamentals");
    const f = calc(res, "fundamentals");
    const t = calc(res, "technicals");
    let html = "";
    if (f) {
      html += `<div class="callout">Recomputed from ledger values (formula ${esc(f.formula_version)}); inputs cite ${(f.evidence_ids || []).length} evidence records. Missing inputs are left missing, never zero-filled.</div>`;
      html += kvBlock("Ratios", f.outputs, (k, v) => (typeof v === "number" ? (ratioish(k) ? pct(v) : fmtNum(v)) : esc(v)));
      html += kvBlock("Inputs", f.inputs, (k, v) => fmtNum(v));
      if (f.warnings?.length) html += `<ul class="warn-list">${f.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>`;
    } else html += "<p>Fundamentals calculation unavailable.</p>";
    if (t) {
      html += `<div class="callout">Technical indicators are context, not predictions (formula ${esc(t.formula_version)}).</div>`;
      html += kvBlock("Technicals", t.outputs, (k, v) => (typeof v === "number" ? (ratioish(k) ? pct(v) : fmtNum(v)) : esc(v)));
      if (t.warnings?.length) html += `<ul class="warn-list">${t.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>`;
    }
    el.innerHTML = html;
  }

  function renderValuation(res) {
    const el = $("#tab-valuation");
    const v = calc(res, "valuation") || calc(res, "dcf");
    if (!v) { el.innerHTML = "<p>Valuation calculation unavailable.</p>"; return; }
    let html = `<div class="callout">DCF / multiples are deterministic outputs of explicit assumptions (formula ${esc(v.formula_version)}); every assumption is listed below.</div>`;
    html += kvBlock("Assumptions and inputs", v.inputs, (k, v2) => (typeof v2 === "number" ? (ratioish(k) ? pct(v2) : fmtNum(v2)) : esc(v2)));
    html += kvBlock("Outputs", v.outputs, (k, v2) => (typeof v2 === "number" ? (ratioish(k) ? pct(v2) : fmtNum(v2)) : esc(v2)));
    for (const [k, grid] of Object.entries(v.outputs || {})) {
      if (grid && typeof grid === "object") {
        html += `<h3 class="sub">${esc(k)}</h3><table><tbody>` + Object.entries(grid).map(([a, b]) => `<tr><td>${esc(a)}</td><td class=num>${typeof b === "number" ? money(b) : esc(JSON.stringify(b))}</td></tr>`).join("") + "</tbody></table>";
      }
    }
    if (v.warnings?.length) html += `<ul class="warn-list">${v.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>`;
    el.innerHTML = html;
  }

  function renderEvidence(res) {
    const el = $("#evidence");
    const q = $("#ev-filter").value.toLowerCase();
    const showUn = $("#ev-unavailable").checked;
    const rows = (res.evidence || []).filter((e) => (showUn || e.retrieval_status !== "unavailable") &&
      (!q || [e.id, e.field, e.source_name, e.claim, e.period].join(" ").toLowerCase().includes(q)));
    const counts = (res.evidence || []).reduce((a, e) => ((a[e.retrieval_status] = (a[e.retrieval_status] || 0) + 1), a), {});
    let html = `<div class="callout">${(res.evidence || []).length} records · ${Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(", ")}. Tier 1 = regulatory filing … tier 5 = user-supplied/snippet.</div>`;
    html += "<table><thead><tr><th>ID</th><th>Field</th><th class=num>Value</th><th>Period / as-of</th><th>Source</th><th>Type</th><th class=num>Tier</th><th>Status</th><th class=num>Conf.</th><th>Notes</th></tr></thead><tbody>";
    for (const e of rows) {
      html += `<tr id="ev-${esc(e.id)}" class="${e.retrieval_status === "unavailable" ? "unavailable" : ""}"><td class="ev-id">${esc(e.id)}</td><td>${esc(e.field || "")}<br><small>${esc(e.claim)}</small></td>
        <td class=num>${fmtNum(e.value, e.unit)}${e.unit && typeof e.value === "number" && !/ratio|pct/.test(e.unit) ? ` <small>${esc(e.unit)}</small>` : ""}</td>
        <td>${esc(e.period || "")}<br><small>${esc(e.as_of || "")}</small></td>
        <td>${e.source_url ? `<a href="${esc(e.source_url)}" target="_blank" rel="noopener">${esc(e.source_name)}</a>` : esc(e.source_name)}</td>
        <td>${esc(e.source_type)}</td><td class="num tier">${esc(e.tier)}</td><td>${esc(e.retrieval_status)}</td><td class=num>${e.confidence?.toFixed(2) ?? "—"}</td>
        <td><small>${esc(e.notes || "")}${e.contradicts?.length ? ` <span class="badge warn">conflicts with ${esc(e.contradicts.join(", "))}</span>` : ""}</small></td></tr>`;
    }
    el.innerHTML = html + "</tbody></table>";
  }
  $("#ev-filter").addEventListener("input", () => current && renderEvidence(current));
  $("#ev-unavailable").addEventListener("change", () => current && renderEvidence(current));

  function jumpToEvidence(id) {
    if (!current) return;
    $("#ev-filter").value = "";
    $("#ev-unavailable").checked = true;
    renderEvidence(current);
    activateTab("evidence");
    const row = document.getElementById(`ev-${id}`);
    if (row) { row.scrollIntoView({ block: "center" }); row.style.outline = "1px solid var(--accent)"; setTimeout(() => (row.style.outline = ""), 1500); }
  }

  function renderReview(res) {
    const el = $("#tab-review");
    const rv = res.reviews || [];
    let html = `<div class="callout">Critic/auditor pass over the first draft: ${rv.length} finding(s), report revised ${res.revision_count || 0} time(s). Findings marked resolved were applied mechanically; the rest are disclosed in Limitations.</div>`;
    if (rv.length) {
      html += "<table><thead><tr><th>Reviewer</th><th>Check</th><th>Severity</th><th>Finding</th><th>Evidence</th><th>Resolution</th></tr></thead><tbody>";
      for (const r of rv) {
        const cls = r.severity === "blocking" ? "bad" : r.severity === "warning" ? "warn" : "muted";
        html += `<tr><td>${esc(r.reviewer)}</td><td>${esc(r.check)}</td><td><span class="badge ${cls}">${esc(r.severity)}</span></td><td>${esc(r.message)}</td>
          <td>${(r.evidence_ids || []).map((id) => `<span class="cite" data-ev="${esc(id)}">[${esc(id)}]</span>`).join(" ")}</td><td>${esc(r.resolution || "unresolved — disclosed")}</td></tr>`;
      }
      html += "</tbody></table>";
    }
    const roles = res.role_outputs || [];
    if (roles.length) {
      html += '<h3 class="sub">Specialist role outputs</h3>';
      for (const ro of roles) {
        html += `<details><summary><strong>${esc(ro.role)}</strong> · ${esc(ro.model || "deterministic")} · ${(ro.findings || []).length} findings</summary><table><thead><tr><th>Type</th><th>Statement</th><th>Evidence</th><th class=num>Conf.</th><th>Caveat</th></tr></thead><tbody>` +
          (ro.findings || []).map((f) => `<tr><td><span class="badge muted">${esc(f.type)}</span></td><td>${esc(f.statement)}</td><td>${(f.evidence_ids || []).map((id) => `<span class="cite" data-ev="${esc(id)}">[${esc(id)}]</span>`).join(" ")}</td><td class=num>${f.confidence?.toFixed(2) ?? "—"}</td><td><small>${esc(f.caveat || "")}</small></td></tr>`).join("") +
          "</tbody></table>" +
          (ro.open_questions?.length ? `<p><strong>Open questions:</strong> ${ro.open_questions.map(esc).join(" · ")}</p>` : "") +
          (ro.invalidated_if?.length ? `<p><strong>Invalidated if:</strong> ${ro.invalidated_if.map(esc).join(" · ")}</p>` : "") + "</details>";
      }
    }
    el.innerHTML = html;
    $$(".cite", el).forEach((c) => c.addEventListener("click", () => jumpToEvidence(c.dataset.ev)));
  }

  // ---------- tabs ----------
  function activateTab(name) {
    $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  }
  $$("#tabs button").forEach((b) => b.addEventListener("click", () => activateTab(b.dataset.tab)));

  loadHistory();
})();
