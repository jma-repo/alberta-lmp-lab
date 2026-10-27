/* Alberta LMP Lab dashboard.
 * Reads the JSON embedded in the page. Every data-derived string goes in via textContent, never innerHTML. */
(() => {
  'use strict';

  const DATA = JSON.parse(document.getElementById('dashboard-data').textContent);
  const A = DATA.analytics;
  const F = DATA.forecast;
  const S = DATA.sced;
  const P = DATA.pdup;
  const G = DATA.agent;
  const state = { hub: 'clustered-2', area: null, hour: 0, project: 0 };

  /* ---------- helpers ------------------------------------------------------------------------- */
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'style') node.setAttribute('style', value);
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }
  const svg = (tag, attrs = {}) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    return node;
  };

  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const dollars = (value, digits = 2) => `${value < 0 ? '−' : ''}$${Math.abs(value).toFixed(digits)}`;
  const signed = (value, digits = 2) => `${value > 0 ? '+' : value < 0 ? '−' : ''}$${Math.abs(value).toFixed(digits)}`;
  const millions = (value, digits = 1) => `${value < 0 ? '−' : ''}$${Math.abs(value).toFixed(digits)}M`;
  const pct = (value, digits = 0) => `${(value * 100).toFixed(digits)}%`;
  const num = (value, digits = 0) => Number(value).toLocaleString('en-CA', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const monthName = (ym) => new Date(`${ym}-01T00:00:00`).toLocaleString('en-CA', { month: 'short' });

  function hexToRgb(hex) {
    const value = hex.replace('#', '');
    const full = value.length === 3 ? value.split('').map((c) => c + c).join('') : value;
    return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16));
  }
  const alpha = (hex, a) => `rgba(${hexToRgb(hex).join(',')},${a})`;

  function divergingStops() {
    return ['--div-neg-3', '--div-neg-2', '--div-neg-1', '--mid', '--div-pos-1', '--div-pos-2', '--div-pos-3'].map(cssVar);
  }
  function diverging(value, max) {
    const stops = divergingStops().map(hexToRgb);
    const t = Math.max(0, Math.min(1, (value / max + 1) / 2)) * (stops.length - 1);
    const i = Math.min(stops.length - 2, Math.floor(t));
    const f = t - i;
    const rgb = stops[i].map((c, k) => Math.round(c + (stops[i + 1][k] - c) * f));
    return `rgb(${rgb.join(',')})`;
  }
  function divergingScale() {
    const stops = divergingStops();
    return stops.map((color, i) => [i / (stops.length - 1), color]);
  }

  const ICONS = {
    good: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm-4 9 3 3 5-6',
    warning: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 5v5m0 3v.5',
    serious: 'M12 3 2 20h20L12 3Zm0 6v5m0 3v.5',
    critical: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm-3.5 5.5 7 7m0-7-7 7',
  };
  function status(kind, text) {
    const icon = svg('svg', { viewBox: '0 0 24 24', 'aria-hidden': 'true' });
    icon.append(svg('path', { d: ICONS[kind], fill: 'none', stroke: 'currentColor', 'stroke-width': '2',
      'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
    return el('span', { class: `status status-${kind}` }, icon, text);
  }

  function tile(label, value, note, tone) {
    const delta = note ? el('div', { class: 'tile-delta' }) : null;
    if (note && tone) delta.append(el('span', { class: tone, text: note[0] }), ` ${note[1] || ''}`);
    else if (note) delta.textContent = Array.isArray(note) ? note.join(' ') : note;
    return el('div', { class: 'tile' }, el('div', { class: 'tile-label', text: label }),
      el('div', { class: 'tile-value', text: value }), delta);
  }

  function table(columns, rows, options = {}) {
    const thead = el('thead', {}, el('tr', {}, columns.map((c) => el('th', { scope: 'col', class: c.num ? 'num' : null, text: c.label }))));
    const body = [];
    for (const row of rows) {
      if (row.group) {
        body.push(el('tr', { class: 'group' }, el('td', { colspan: String(columns.length), text: row.group })));
        continue;
      }
      body.push(el('tr', { class: options.emph && options.emph(row) ? 'emph' : null }, columns.map((c) => {
        const value = c.render ? c.render(row) : row[c.key];
        return el('td', { class: c.num ? 'num' : null }, value instanceof Node ? value : String(value ?? '—'));
      })));
    }
    return el('table', { class: 'data-table' }, thead, el('tbody', {}, body));
  }

  function segmented(container, options, current, onChange) {
    container.replaceChildren(...options.map(([value, label]) => el('button', {
      type: 'button', role: 'radio', 'aria-checked': String(value === current), text: label,
      onclick: () => {
        for (const button of $$('button', container)) button.setAttribute('aria-checked', 'false');
        container.querySelector(`[data-value="${CSS.escape(String(value))}"]`).setAttribute('aria-checked', 'true');
        onChange(value);
      },
      'data-value': String(value),
    })));
  }

  /* ---------- Plotly theme --------------------------------------------------------------------- */
  const PLOT_CONFIG = { displayModeBar: false, responsive: true };
  const charts = new Map();

  function layout(extra = {}) {
    const text2 = cssVar('--text-secondary');
    const axisBase = {
      gridcolor: cssVar('--grid'), gridwidth: 1, linecolor: cssVar('--axis'), linewidth: 1, zeroline: false, ticks: '',
      tickfont: { color: cssVar('--axis-muted'), size: 11.5 }, title: { font: { color: text2, size: 12 } }, automargin: true,
    };
    const base = {
      paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
      font: { family: cssVar('--font') || 'system-ui', color: text2, size: 12 },
      margin: { l: 56, r: 16, t: 10, b: 44 },
      hoverlabel: { bgcolor: cssVar('--surface-2'), bordercolor: cssVar('--border-strong'),
        font: { color: cssVar('--text-primary'), family: cssVar('--font'), size: 12 } },
      legend: { orientation: 'h', x: 0, y: 1.14, xanchor: 'left', font: { color: text2, size: 12 }, bgcolor: 'rgba(0,0,0,0)' },
      showlegend: false,
    };
    const merged = { ...base, ...extra };
    for (const key of ['xaxis', 'yaxis']) {
      const own = extra[key] || {};
      merged[key] = { ...axisBase, ...own, title: { ...axisBase.title, ...(own.title || {}) } };
    }
    if (merged.showlegend && window.innerWidth < 640) {
      merged.legend = { ...merged.legend, y: -0.24, yanchor: 'top' };
      merged.margin = { ...merged.margin, t: 12, b: (merged.margin.b || 44) + 60 };
    }
    return merged;
  }

  function chart(id, render) {
    charts.set(id, render);
    const node = document.getElementById(id);
    if (node && node.offsetParent !== null) render(node);
  }
  function rerenderVisible() {
    for (const [id, render] of charts) {
      const node = document.getElementById(id);
      if (node && node.offsetParent !== null) render(node);
    }
  }
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(rerenderVisible, 150);
  });

  /* ---------- theme and tabs ---------------------------------------------------------------- */
  const THEME_KEY = 'lmplab-theme';
  const effectiveDark = () => {
    const forced = document.documentElement.dataset.theme;
    return forced ? forced === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
  };
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === 'light' || saved === 'dark') document.documentElement.dataset.theme = saved;
  } catch (_) { /* storage may be unavailable */ }
  $('#theme-toggle').addEventListener('click', () => {
    const next = effectiveDark() ? 'light' : 'dark';
    try { localStorage.setItem(THEME_KEY, next); } catch (_) { /* ignore */ }
    document.documentElement.dataset.theme = next;
    rerenderVisible();
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (!document.documentElement.dataset.theme) rerenderVisible();
  });

  const TABS = $$('.tabs [role="tab"]');
  function showTab(name, push = true) {
    if (!document.getElementById(name)) name = 'overview';
    for (const tab of TABS) {
      const active = tab.dataset.tab === name;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      document.getElementById(tab.dataset.tab).hidden = !active;
    }
    if (push) {
      history.replaceState(null, '', `#${name}`);
      window.scrollTo(0, 0);
    }
    if (document.hidden) setTimeout(rerenderVisible, 50);
    else requestAnimationFrame(rerenderVisible);
  }
  TABS.forEach((tab, index) => {
    tab.addEventListener('click', () => showTab(tab.dataset.tab));
    tab.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
      const next = TABS[(index + (event.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length];
      next.focus();
      showTab(next.dataset.tab);
    });
  });
  const goto = (tab) => el('button', { class: 'link', type: 'button', text: 'See it →', onclick: () => showTab(tab) });

  const AREA = new Map(DATA.areas.map((a) => [a.area_id, a]));
  const STATS = new Map(A.areas.map((a) => [a.area_id, a]));
  const scenario = (name) => A.hubs.scenarios.find((s) => s.name === name);

  /* ---------- Overview --------------------------------------------------------------------------- */
  function renderOverview() {
    const s = A.summary;
    $('#hero-value').textContent = pct(s.share_any_separation);
    $('#hero-note').textContent = `${num(s.hours_any_separation)} of ${num(s.hours)} hours. On average the dearest area priced `
      + `${dollars(s.mean_spread)}/MWh above the cheapest.`;
    const conformal = F.intervals.conformal;
    const lgb = F.point['LightGBM (P50)'];
    const uniform = F.point['Uniform price (basis = 0)'];
    const reviews = P.reviews;
    const wind = reviews.find((r) => r.errors > 0) || reviews[0];
    const clean = reviews.find((r) => r !== wind);
    const examples = S.aeso_examples;
    $('#kpis').replaceChildren(
      tile('Alberta Load Price, 2025 backcast', `${dollars(s.annual_alp)}/MWh`,
        `${s.cheapest_area.name} averaged ${dollars(s.cheapest_area.mean_lmp)}, ${s.dearest_area.name} ${dollars(s.dearest_area.mean_lmp)}`),
      tile('Uniform-price surplus', millions(s.annual_surplus_musd),
        `a year from paying generators their own LMP; AESO reports $${s.aeso_reported_congestion_rent_musd}M congestion rent`),
      tile('Day-ahead basis forecast', pct(conformal.coverage, 1),
        `inside 80% bands; error ${dollars(lgb.mae)} vs ${dollars(uniform.mae)} for a uniform price`),
      tile('Separation risk model', F.separation.auc.toFixed(2),
        `AUC on the Oct–Dec test months; ${pct(F.separation.base_rate)} of area-hours separated`),
      tile('SCED engine vs AESO examples', `${examples.filter((e) => e.matches_published).length} of ${examples.length}`,
        'matched exactly; in Example 2 bus C prices at $70, not the $40 on the slide'),
      tile('PDUP first-pass review', `${wind.errors + wind.warnings} findings`,
        `${wind.errors} deficiencies and ${wind.warnings} clarifications on the wind farm; ${clean ? `the clean project is ${clean.outcome.toLowerCase()}` : ''}`),
    );

    const fit = [
      ['Grid Analytics', [
        ['Congestion modelling and assessment; LMP analysis', 'Separation, basis and uniform-price surplus for 42 areas × 8,760 hours, spread events and duration curves.', 'separation'],
        ['Probabilistic engineering and market assessments', 'Monte Carlo SCED with N-1: congestion frequency, curtailment and production cost for +400 MW of south-east wind and upgrade options.', 'sced'],
        ['Market simulation and operational forecasts', 'SCED engine with LMPs from the LP duals; day-ahead basis forecast for every area.', 'sced'],
        ['ML applications; probabilistic forecasting and uncertainty quantification', `LightGBM quantile models with rolling conformal calibration (${pct(conformal.coverage, 1)} coverage), SHAP drivers, separation-risk classifier.`, 'forecast'],
        ['Agentic AI for workflow automation', 'Local-LLM agent with six tools, read-only SQL and every number traced to a tool result.', 'agent'],
        ['BI platforms, data visualization, relational databases', 'DuckDB warehouse behind a data-quality gate, MLflow tracking, this dashboard.', 'separation'],
        ['LMP analysis: pricing hub design', 'Contiguous clustered hubs beat predefined regions on both AESO hub metrics.', 'hubs'],
      ]],
      ['Model Validation', [
        ['Validate line R, X, B, thermal ratings and seasonal limits', 'Per-km screening by voltage class and construction, surge impedance, seasonal order, rating vs generation carried; flags ohms typed into p.u. fields.', 'pdup'],
        ['Transformer impedances, tap changers, grounding', 'Impedance on own base with base mix-up diagnosis, X/R, tap data, vector group and collector grounding.', 'pdup'],
        ['Static and dynamic generator and plant models', 'MBASE, Pmax and reactive capability; REGCA1/REECA1/REPCA1 ranges, current limit, droop and deadband, static vs dynamic limits.', 'pdup'],
        ['Convergence, numerical stability, sensitivity', 'AC power flow with root-cause isolation of non-convergence, time-step check on time constants, reactive-margin tornado.', 'pdup'],
        ['Short-circuit and EMT study needs', 'Short-circuit ratio at the POI as the EMT-study trigger; collector fault level vs switchgear.', 'pdup'],
        ['Validation frameworks, checklists, peer review', '30 rules with IDs and YAML thresholds, unit-tested; an anomaly model trained on accepted models.', 'pdup'],
        ['Communicate risks to market participants', 'Review letter drafted from the findings: probable cause, required action and study impact.', 'pdup'],
        ['Validate model logic and results', `The SCED reproduces AESO's examples and found a pricing error in Example 2.`, 'sced'],
      ]],
    ];
    $('#fit').replaceChildren(...fit.map(([title, rows]) => el('div', { class: 'fit-col' }, el('h3', { text: title }),
      rows.map(([duty, built, tab]) => el('div', { class: 'fit-row' }, el('div', { class: 'duty', text: duty }), goto(tab),
        el('div', { class: 'built', text: built }))))));

    const flow = [
      ['Fetch', 'httpx', 'Four AESO public files, about 23 MB, never committed.'],
      ['Warehouse', 'DuckDB', `${num(DATA.quality.warehouse_rows)} area-hours behind a data-quality gate.`],
      ['Analytics', 'pandas · scikit-learn', 'Separation, uniform-price surplus, contiguous hub clustering.'],
      ['AI forecast', 'LightGBM · conformal · MLflow', 'Day-ahead basis with 80% bands that hold, SHAP drivers.'],
      ['SCED', 'SciPy HiGHS', 'DC-OPF with N-1 and losses; LMP components from the duals.'],
      ['Model validation', 'pandapower · isolation forest', '30-rule PDUP checklist, AC power flow, short circuit, anomalies.'],
      ['AI agent', 'LM Studio (local LLM)', 'Six tools, read-only SQL, numbers traced to tool results.'],
      ['Dashboard', 'Plotly · static HTML', 'Self-contained page, ready for GitHub Pages.'],
    ];
    $('#flow').replaceChildren(...flow.map(([title, tool, text]) => el('li', {}, el('h3', { text: title }),
      el('span', { class: 'tool', text: tool }), el('p', { text: text }))));
  }

  /* ---------- Price separation --------------------------------------------------------------------- */
  function linkTrace() {
    const x = [];
    const y = [];
    for (const [a, b] of DATA.links) {
      x.push(AREA.get(a).x, AREA.get(b).x, null);
      y.push(AREA.get(a).y, AREA.get(b).y, null);
    }
    return { x, y, mode: 'lines', type: 'scatter', line: { color: cssVar('--grid'), width: 1 }, hoverinfo: 'skip', showlegend: false };
  }
  const MAP_LABELS = new Set([4, 18, 6, 60, 25, 54, 20]);
  function mapAxes() {
    const hidden = { visible: false, showgrid: false, zeroline: false };
    return { xaxis: { ...hidden, range: [90, 600] }, yaxis: { ...hidden, range: [80, 920], scaleanchor: 'x', scaleratio: 1 } };
  }

  function renderSeparation() {
    const areas = A.areas;
    const max = Math.max(...areas.map((a) => Math.abs(a.mean_basis)));
    chart('chart-map', (node) => {
      const loads = areas.map((a) => Math.max(a.mean_load_mw, 1));
      const biggest = Math.max(...loads);
      const surface = cssVar('--surface-1');
      Plotly.react(node, [linkTrace(), {
        x: areas.map((a) => AREA.get(a.area_id).x), y: areas.map((a) => AREA.get(a.area_id).y), type: 'scatter', mode: 'markers+text',
        text: areas.map((a) => (MAP_LABELS.has(a.area_id) ? a.name : '')), textposition: 'middle right',
        textfont: { size: 11, color: cssVar('--text-secondary') },
        marker: { size: loads.map((l) => 9 + 22 * Math.sqrt(l / biggest)), color: areas.map((a) => a.mean_basis), cmin: -max, cmax: max,
          colorscale: divergingScale(), line: { color: surface, width: 2 },
          colorbar: { title: { text: 'Mean basis<br>$/MWh', font: { size: 11.5, color: cssVar('--text-secondary') } }, thickness: 10,
            len: 0.6, outlinewidth: 0, tickfont: { size: 11, color: cssVar('--axis-muted') } } },
        customdata: areas.map((a) => [a.name, a.region, a.mean_lmp, a.mean_basis, pct(a.separated_share), pct(a.below_share), pct(a.above_share), Math.round(a.mean_net_gen_mw)]),
        hovertemplate: '<b>%{customdata[0]}</b> · %{customdata[1]}<br>Mean LMP $%{customdata[2]:.2f} · basis $%{customdata[3]:.2f}'
          + '<br>Separated %{customdata[4]} of hours (below %{customdata[5]}, above %{customdata[6]})<br>Mean net generation %{customdata[7]} MW<extra></extra>',
      }], layout({ margin: { l: 4, r: 4, t: 4, b: 4 }, ...mapAxes(), hovermode: 'closest' }), PLOT_CONFIG);
    });
    $('#table-map').replaceChildren(table([
      { label: 'Area', render: (r) => `${r.area_id} ${r.name}` }, { label: 'Region', key: 'hub_region' },
      { label: 'Mean LMP', num: true, render: (r) => dollars(r.mean_lmp) }, { label: 'Mean basis', num: true, render: (r) => signed(r.mean_basis) },
      { label: 'Separated', num: true, render: (r) => pct(r.separated_share) }, { label: 'Below ALP', num: true, render: (r) => pct(r.below_share) },
      { label: 'Above ALP', num: true, render: (r) => pct(r.above_share) }, { label: 'Net gen MW', num: true, render: (r) => num(r.mean_net_gen_mw) },
    ], [...areas].sort((a, b) => a.mean_basis - b.mean_basis)));

    const areaName = (label) => label.replace(/^\d+\s+/, '');
    $('#events').replaceChildren(table([
      { label: 'Hour (MST)', key: 'ts' }, { label: 'Spread', num: true, render: (r) => dollars(r.spread, 0) },
      { label: 'Cheapest', render: (r) => el('span', {}, areaName(r.low_area), el('span', { class: 'sub', text: dollars(r.low_lmp, 0) })) },
      { label: 'Dearest', render: (r) => el('span', {}, areaName(r.high_area), el('span', { class: 'sub', text: dollars(r.high_lmp, 0) })) },
    ], A.events.slice(0, 8)));

    const q = DATA.quality;
    $('#quality').replaceChildren(...[
      ['Backcast hours', num(q.hours)], ['Planning areas', String(q.areas)], ['Area-hours joined', num(q.warehouse_rows)],
      ['Join coverage', pct(q.join_coverage, 1)], ['LMP range', `${dollars(q.lmp_min, 0)} to ${dollars(q.lmp_max, 0)}`],
    ].flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
    $('#quality-findings').replaceChildren(...q.findings.map((f) => el('li', { text: f })));

    chart('chart-daily', (node) => {
      const d = A.daily;
      const blue = cssVar('--series-1');
      Plotly.react(node, [
        { x: d.date, y: d.min, type: 'scatter', mode: 'lines', line: { width: 0 }, hoverinfo: 'skip', showlegend: false },
        { x: d.date, y: d.max.map((v) => Math.min(v, 260)), type: 'scatter', mode: 'lines', fill: 'tonexty', fillcolor: alpha(blue, 0.18),
          line: { width: 0 }, name: 'Lowest to highest area LMP', customdata: d.max, hovertemplate: 'Highest area $%{customdata:.0f}<extra></extra>' },
        { x: d.date, y: d.alp, type: 'scatter', mode: 'lines', line: { color: blue, width: 2 }, name: 'Alberta Load Price (daily mean)',
          hovertemplate: 'ALP $%{y:.2f}<extra></extra>' },
      ], layout({ showlegend: true, hovermode: 'x unified', margin: { l: 52, r: 12, t: 30, b: 36 },
        xaxis: { showgrid: false, hoverformat: '%b %d' }, yaxis: { range: [-20, 260], tickprefix: '$' } }), PLOT_CONFIG);
    });
    $('#table-daily').replaceChildren(table([{ label: 'Date', key: 'date' }, { label: 'ALP', num: true, key: 'alp' },
      { label: 'Lowest area', num: true, key: 'min' }, { label: 'Highest area', num: true, key: 'max' }],
    A.daily.date.map((date, i) => ({ date, alp: dollars(A.daily.alp[i]), min: dollars(A.daily.min[i]), max: dollars(A.daily.max[i]) }))));

    const months = Object.keys(A.monthly_surplus_musd);
    const values = months.map((m) => A.monthly_surplus_musd[m]);
    $('#surplus-caption').textContent = `Generation valued at the ALP minus at its own LMP: ${millions(A.summary.annual_surplus_musd)} in 2025. `
      + `AESO's published congestion rent is $${A.summary.aeso_reported_congestion_rent_musd}M; this area-level figure also includes loss rent.`;
    chart('chart-surplus', (node) => {
      const top = [...values].sort((a, b) => b - a).slice(0, 3);
      Plotly.react(node, [{ x: months.map(monthName), y: values, type: 'bar', marker: { color: cssVar('--series-1') },
        width: 0.55, text: values.map((v) => (top.includes(v) ? `$${v.toFixed(1)}M` : '')), textposition: 'outside',
        textfont: { size: 11.5, color: cssVar('--text-secondary') }, cliponaxis: false,
        hovertemplate: '%{x}: $%{y:.1f}M<extra></extra>' }],
      layout({ margin: { l: 48, r: 12, t: 18, b: 36 }, bargap: 0.35, yaxis: { tickprefix: '$', ticksuffix: 'M' }, xaxis: { showgrid: false } }), PLOT_CONFIG);
    });
    $('#table-surplus').replaceChildren(table([{ label: 'Month', key: 'm' }, { label: 'Surplus', num: true, key: 'v' }],
      months.map((m, i) => ({ m: monthName(m), v: millions(values[i], 2) }))));

    chart('chart-duration', (node) => {
      const duration = A.basis_duration;
      const p = duration.percentiles;
      const highlight = [[4, '--series-1'], [18, '--series-2'], [6, '--series-3']];
      const traces = [];
      for (const area of DATA.areas) {
        if (highlight.some(([id]) => id === area.area_id)) continue;
        traces.push({ x: p, y: duration.areas[area.area_id], type: 'scatter', mode: 'lines', line: { color: cssVar('--deemph-line'), width: 1 },
          hoverinfo: 'skip', showlegend: false });
      }
      for (const [id, color] of highlight) {
        traces.push({ x: p, y: duration.areas[id], type: 'scatter', mode: 'lines', line: { color: cssVar(color), width: 2 },
          name: AREA.get(id).name, hovertemplate: `${AREA.get(id).name}: basis $%{y:.1f} or more in %{x}% of hours<extra></extra>` });
      }
      const band = A.summary.threshold;
      Plotly.react(node, traces, layout({ showlegend: true, hovermode: 'x unified', margin: { l: 52, r: 12, t: 30, b: 44 },
        xaxis: { title: { text: 'Share of hours with basis at or above the value (%)' }, range: [0, 100], showgrid: false },
        yaxis: { range: [-45, 45], tickprefix: '$', title: { text: 'Basis ($/MWh)' } },
        shapes: [{ type: 'rect', xref: 'paper', x0: 0, x1: 1, y0: -band, y1: band, fillcolor: alpha(cssVar('--axis-muted'), 0.12), line: { width: 0 }, layer: 'below' }],
        annotations: [{ xref: 'paper', x: 0.99, y: band, text: `±$${band} band: not separated`, showarrow: false, xanchor: 'right', yanchor: 'bottom',
          font: { size: 11, color: cssVar('--text-secondary') } }] }), PLOT_CONFIG);
    });
    const shown = [4, 18, 6];
    const p = A.basis_duration.percentiles;
    $('#table-duration').replaceChildren(table([{ label: 'Share of hours', key: 'share' },
      ...shown.map((id) => ({ label: AREA.get(id).name, num: true, render: (row) => signed(A.basis_duration.areas[id][row.i], 1) }))],
    p.map((share, i) => ({ share: `${share}%`, i })).filter((_, i) => i % 5 === 0)));
  }

  /* ---------- Pricing hubs ---------------------------------------------------------------------- */
  function renderHubs() {
    const all = A.hubs.scenarios;
    chart('chart-hubs', (node) => {
      const predefined = all.filter((s) => s.kind === 'predefined');
      const clustered = all.filter((s) => s.kind === 'clustered');
      const surface = cssVar('--surface-1');
      const reference = Object.entries(A.hubs.aeso_reference);
      Plotly.react(node, [
        { x: predefined.map((s) => s.min_separation), y: predefined.map((s) => s.basis_deviation), type: 'scatter', mode: 'markers+text',
          name: 'Predefined (planning regions)', text: predefined.map((s) => `${s.hubs} predefined`), textposition: 'bottom center',
          textfont: { size: 11, color: cssVar('--text-secondary') },
          marker: { size: 11, color: cssVar('--axis-muted'), line: { color: surface, width: 2 } },
          customdata: predefined.map((s) => s.name), hovertemplate: '<b>%{customdata}</b><br>Min separation $%{x:.2f} · basis deviation $%{y:.2f}<extra></extra>' },
        { x: clustered.map((s) => s.min_separation), y: clustered.map((s) => s.basis_deviation), type: 'scatter', mode: 'lines+markers+text',
          name: 'Clustered (this project)', text: clustered.map((s) => `k=${s.k}`), textposition: 'top right',
          textfont: { size: 11, color: cssVar('--text-secondary') }, line: { color: alpha(cssVar('--series-1'), 0.35), width: 1.5 },
          marker: { size: 11, color: cssVar('--series-1'), line: { color: surface, width: 2 } },
          customdata: clustered.map((s) => s.name), hovertemplate: '<b>%{customdata}</b><br>Min separation $%{x:.2f} · basis deviation $%{y:.2f}<extra></extra>' },
        { x: reference.map(([, v]) => v.min_separation), y: reference.map(([, v]) => v.basis_deviation), type: 'scatter', mode: 'markers+text',
          name: 'AESO, bus level (for reference)', text: reference.map(([k]) => `AESO ${k.replace(' hubs', '')}`), textposition: 'middle right',
          textfont: { size: 11, color: cssVar('--text-secondary') },
          marker: { size: 11, symbol: 'diamond-open', color: cssVar('--series-2'), line: { width: 2 } },
          customdata: reference.map(([k]) => k), hovertemplate: '<b>AESO: %{customdata}</b><br>Min separation $%{x:.2f} · HV basis deviation $%{y:.2f}<extra></extra>' },
      ], layout({ showlegend: true, hovermode: 'closest', margin: { l: 56, r: 16, t: 34, b: 48 },
        xaxis: { title: { text: 'Minimum separation between hubs ($/MWh) — higher is clearer' }, tickprefix: '$', rangemode: 'tozero' },
        yaxis: { title: { text: 'Basis deviation ($/MWh) — lower fits better' }, tickprefix: '$' } }), PLOT_CONFIG);
    });
    $('#table-hubs').replaceChildren(table([
      { label: 'Design', key: 'name' }, { label: 'Hubs', num: true, key: 'hubs' },
      { label: 'Basis deviation', num: true, render: (r) => dollars(r.basis_deviation) },
      { label: 'Min separation', num: true, render: (r) => dollars(r.min_separation) },
    ], all));

    const choices = [['predefined-2', '2 predefined', '2 predefined hubs (North / South)'],
      ['clustered-2', '2 clustered', '2 clustered hubs'], ['clustered-3', '3 clustered', '3 clustered hubs']];
    segmented($('#hub-choice'), choices.map(([v, l]) => [v, l]), state.hub, (value) => { state.hub = value; drawHubMap(); });
    function drawHubMap() {
      const [, , name] = choices.find(([v]) => v === state.hub);
      const design = scenario(name);
      const hubs = Object.entries(design.hub_mean_price).sort((a, b) => a[1] - b[1]);
      $('#hubmap-caption').textContent = `${design.name}: basis deviation ${dollars(design.basis_deviation)}, minimum separation ${dollars(design.min_separation)}/MWh.`;
      chart('chart-hubmap', (node) => {
        const colors = ['--series-1', '--series-2', '--series-3'].map(cssVar);
        const surface = cssVar('--surface-1');
        const traces = [linkTrace()];
        hubs.forEach(([hub, price], i) => {
          const members = DATA.areas.filter((a) => design.assignment[String(a.area_id)] === hub);
          traces.push({ x: members.map((a) => a.x), y: members.map((a) => a.y), type: 'scatter', mode: 'markers',
            name: `${hub} · ${dollars(price)}`, marker: { size: 15, color: colors[i], line: { color: surface, width: 2 } },
            customdata: members.map((a) => a.name), hovertemplate: `<b>%{customdata}</b><br>${hub}: hub price ${dollars(price)}<extra></extra>` });
        });
        Plotly.react(node, traces, layout({ showlegend: true, margin: { l: 4, r: 4, t: 40, b: 4 }, ...mapAxes(), hovermode: 'closest',
          legend: { orientation: 'h', x: 0, y: 1.1, font: { size: 12, color: cssVar('--text-secondary') } } }), PLOT_CONFIG);
      });
      $('#table-hubmap').replaceChildren(table([{ label: 'Hub', key: 'hub' }, { label: 'Hub price', num: true, key: 'price' },
        { label: 'Areas', key: 'members' }], hubs.map(([hub, price]) => ({ hub, price: dollars(price),
        members: DATA.areas.filter((a) => design.assignment[String(a.area_id)] === hub).map((a) => a.name).join(', ') }))));
    }
    drawHubMap();

    const two = scenario('2 predefined hubs (North / South)');
    const twoC = scenario('2 clustered hubs');
    const sixP = scenario('6 planning-region hubs');
    const sixC = scenario('6 clustered hubs');
    const ref = A.hubs.aeso_reference;
    $('#hub-note').replaceChildren(
      el('h2', { text: 'Reading the result' }),
      el('p', {}, el('strong', { text: 'Clustering wins on both criteria. ' }),
        `With two hubs, the clustered split has basis deviation ${dollars(twoC.basis_deviation)} against ${dollars(two.basis_deviation)} for the predefined North/South split, `
        + `and a larger minimum separation (${dollars(twoC.min_separation)} vs ${dollars(two.min_separation)}). With six, ${dollars(sixC.basis_deviation)} vs ${dollars(sixP.basis_deviation)} `
        + `and ${dollars(sixC.min_separation)} vs ${dollars(sixP.min_separation)}: two planning regions price almost the same, so a region-based design wastes a hub.`),
      el('p', {}, el('strong', { text: 'Consistent with AESO, at a coarser grain. ' }),
        `AESO's optimized two-hub design also fits better than its predefined one (HV basis deviation $${ref['2 optimized hubs'].basis_deviation} vs $${ref['2 predefined hubs'].basis_deviation}). `
        + 'Deviation here runs higher because each "node" is a whole planning area including remote ones, while AESO averages its most stable 500/240 kV buses.'),
      el('p', {}, el('strong', { text: 'Liquidity guardrail. ' }),
        `Hubs need at least ${A.hubs.min_hub_areas} areas; without it the algorithm happily makes High Level a one-area hub, which maximizes separation but cannot trade.`),
    );
  }

  /* ---------- Forecast ------------------------------------------------------------------------- */
  function renderForecast() {
    const lgb = F.point['LightGBM (P50)'];
    const uniform = F.point['Uniform price (basis = 0)'];
    $('#forecast-kpis').replaceChildren(
      tile('Test error (MAE)', `${dollars(lgb.mae)}/MWh`, `vs ${dollars(uniform.mae)} if every area priced at the ALP`),
      tile('Skill over a uniform price', pct(lgb.skill_vs_uniform), 'lower error than ignoring location'),
      tile('80% band coverage', pct(F.intervals.conformal.coverage, 1),
        `rolling recalibration; one fixed calibration held ${pct(F.intervals.fixed.coverage, 1)}`),
      tile('Separation-risk AUC', F.separation.auc.toFixed(3), `Brier score ${F.separation.brier.toFixed(3)}`),
    );

    const methods = Object.keys(F.point);
    const short = { 'Uniform price (basis = 0)': 'Uniform price', 'Climatology (area x hour median)': 'Climatology',
      'Persistence (basis 24 h earlier)': 'Persistence (24 h)', 'Linear, per area': 'Linear per area', 'LightGBM (P50)': 'LightGBM P50' };
    chart('chart-methods', (node) => {
      const names = methods.map((m) => short[m] || m).reverse();
      const all = methods.map((m) => F.point[m].mae).reverse();
      const sep = methods.map((m) => F.point[m].mae_separated).reverse();
      Plotly.react(node, [
        { y: names, x: all, type: 'bar', orientation: 'h', name: 'All area-hours', marker: { color: cssVar('--series-1') },
          text: all.map((v) => dollars(v)), textposition: 'outside', textfont: { size: 11, color: cssVar('--text-secondary') }, cliponaxis: false,
          hovertemplate: '%{y}: $%{x:.2f}<extra>All area-hours</extra>' },
        { y: names, x: sep, type: 'bar', orientation: 'h', name: 'Hours that separated', marker: { color: cssVar('--series-2') },
          text: sep.map((v) => dollars(v)), textposition: 'outside', textfont: { size: 11, color: cssVar('--text-secondary') }, cliponaxis: false,
          hovertemplate: '%{y}: $%{x:.2f}<extra>Separated hours</extra>' },
      ], layout({ showlegend: true, barmode: 'group', bargap: 0.3, bargroupgap: 0.12, margin: { l: 120, r: 48, t: 30, b: 40 },
        xaxis: { title: { text: 'Mean absolute error ($/MWh)' }, tickprefix: '$' }, yaxis: { showgrid: false } }), PLOT_CONFIG);
    });
    $('#table-methods').replaceChildren(table([
      { label: 'Method', render: (r) => r.m }, { label: 'MAE', num: true, render: (r) => dollars(F.point[r.m].mae) },
      { label: 'MAE, separated hours', num: true, render: (r) => dollars(F.point[r.m].mae_separated) },
      { label: 'Skill vs uniform', num: true, render: (r) => pct(F.point[r.m].skill_vs_uniform, 1) },
    ], methods.map((m) => ({ m }))));

    const months = Object.keys(F.intervals.monthly);
    chart('chart-coverage', (node) => {
      const series = [['raw', 'Raw quantiles', '--deemph'], ['fixed', 'Fixed calibration (Sep)', '--series-2'], ['rolling', 'Rolling 30-day calibration', '--series-1']];
      const traces = series.map(([key, name, color]) => ({
        x: months.map(monthName), y: months.map((m) => F.intervals.monthly[m][key]), type: 'bar', name, marker: { color: cssVar(color) },
        hovertemplate: `${name}: %{y:.1%}<extra>%{x}</extra>` }));
      Plotly.react(node, traces, layout({ showlegend: true, barmode: 'group', bargap: 0.35, bargroupgap: 0.1, margin: { l: 48, r: 12, t: 30, b: 36 },
        yaxis: { range: [0.4, 1], tickformat: '.0%' }, xaxis: { showgrid: false },
        shapes: [{ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: F.intervals.target, y1: F.intervals.target, line: { color: cssVar('--text-secondary'), width: 1 } }],
        annotations: [{ xref: 'paper', x: 1, y: F.intervals.target, text: 'target 80%', showarrow: false, xanchor: 'right', yanchor: 'bottom',
          font: { size: 11, color: cssVar('--text-secondary') } }] }), PLOT_CONFIG);
    });
    $('#table-coverage').replaceChildren(table([{ label: 'Month', key: 'm' }, { label: 'Raw', num: true, key: 'raw' },
      { label: 'Fixed', num: true, key: 'fixed' }, { label: 'Rolling', num: true, key: 'rolling' }],
    months.map((m) => ({ m: monthName(m), raw: pct(F.intervals.monthly[m].raw, 1), fixed: pct(F.intervals.monthly[m].fixed, 1), rolling: pct(F.intervals.monthly[m].rolling, 1) }))));

    const show = F.showcase;
    const ids = Object.keys(show.areas);
    state.area = state.area || ids[0];
    segmented($('#showcase-choice'), ids.map((id) => [id, show.areas[id].name]), state.area, (value) => { state.area = value; drawShowcase(); });
    function drawShowcase() {
      const s = show.areas[state.area];
      const skill = F.per_area.find((a) => String(a.area_id) === state.area);
      $('#showcase-caption').textContent = `${s.name}, ${show.ts[0].slice(0, 10)} to ${show.ts.at(-1).slice(0, 10)}, the test window with the most price movement. `
        + `Test-period error ${dollars(skill.mae)} vs ${dollars(skill.mae_uniform)} for a uniform price; ${pct(skill.coverage)} of hours inside the band.`;
      chart('chart-showcase', (node) => {
        const blue = cssVar('--series-1');
        Plotly.react(node, [
          { x: show.ts, y: s.p10, type: 'scatter', mode: 'lines', line: { width: 0 }, hoverinfo: 'skip', showlegend: false },
          { x: show.ts, y: s.p90, type: 'scatter', mode: 'lines', fill: 'tonexty', fillcolor: alpha(blue, 0.14), line: { width: 0 },
            name: '80% band (P10–P90)', customdata: s.p10, hovertemplate: 'Band $%{customdata:.1f} to $%{y:.1f}<extra></extra>' },
          { x: show.ts, y: s.p50, type: 'scatter', mode: 'lines', line: { color: blue, width: 2 }, name: 'Forecast (P50)',
            hovertemplate: 'Forecast $%{y:.1f}<extra></extra>' },
          { x: show.ts, y: s.basis, type: 'scatter', mode: 'lines', line: { color: cssVar('--text-primary'), width: 1.25 }, name: 'Actual basis',
            hovertemplate: 'Actual $%{y:.1f}<extra></extra>' },
        ], layout({ showlegend: true, hovermode: 'x unified', margin: { l: 52, r: 12, t: 30, b: 36 },
          xaxis: { showgrid: false, hoverformat: '%b %d %H:00' }, yaxis: { tickprefix: '$', title: { text: 'Basis ($/MWh)' } } }), PLOT_CONFIG);
      });
    }
    drawShowcase();

    chart('chart-drivers', (node) => {
      const entries = Object.entries(F.drivers.overall).slice(0, 12).reverse();
      const names = entries.map(([k]) => (k === 'area_id' ? 'Area (location)' : F.drivers.labels[k] || k));
      Plotly.react(node, [{ y: names, x: entries.map(([, v]) => v), type: 'bar', orientation: 'h', marker: { color: cssVar('--series-1') },
        width: 0.6, hovertemplate: '%{y}: $%{x:.2f}<extra></extra>' }],
      layout({ margin: { l: 190, r: 16, t: 8, b: 40 }, xaxis: { title: { text: 'Mean |SHAP| ($/MWh)' }, tickprefix: '$' }, yaxis: { showgrid: false } }), PLOT_CONFIG);
    });

    const dep = F.drivers.dependence;
    chart('chart-dependence', (node) => {
      const series = [['Southeast', '--series-1'], ['Southwest', '--series-2']];
      const surface = cssVar('--surface-1');
      const traces = series.map(([region, color]) => ({
        x: dep[region].x, y: dep[region].shap, type: 'scatter', mode: 'lines+markers', name: `${region} areas vs ${region.toLowerCase()} net generation`,
        line: { color: cssVar(color), width: 2 }, marker: { size: 8, color: cssVar(color), line: { color: surface, width: 2 } },
        hovertemplate: `${region}: %{x:.0f} MW → $%{y:.2f}<extra></extra>` }));
      const se = dep.Southeast;
      const knee = se.x[se.shap.findIndex((v, i) => i > 0 && v < 0)];
      Plotly.react(node, traces, layout({ showlegend: true, hovermode: 'closest', margin: { l: 56, r: 16, t: 40, b: 48 },
        xaxis: { title: { text: 'Regional net generation (MW)' } }, yaxis: { title: { text: 'Effect on basis ($/MWh)' }, tickprefix: '$' },
        shapes: [{ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: 0, y1: 0, line: { color: cssVar('--axis'), width: 1 } }],
        annotations: knee ? [{ x: knee, y: 0, text: `knee ≈ ${Math.round(knee / 100) * 100} MW`, showarrow: true, arrowhead: 0, ax: -40, ay: 40,
          arrowcolor: cssVar('--axis-muted'), font: { size: 11.5, color: cssVar('--text-secondary') } }] : [] }), PLOT_CONFIG);
    });
    $('#table-dependence').replaceChildren(table([{ label: 'Southeast net gen (MW)', num: true, key: 'x' },
      { label: 'SHAP effect', num: true, key: 's' }, { label: 'Mean actual basis', num: true, key: 'b' }],
    dep.Southeast.x.map((x, i) => ({ x: num(x), s: signed(dep.Southeast.shap[i]), b: signed(dep.Southeast.basis[i]) }))));

    const rel = F.separation.reliability;
    const low = rel.predicted.map((p, i) => [p, rel.observed[i]]).filter(([p]) => p < 0.01);
    const lowObserved = low.length ? low.reduce((sum, [, o]) => sum + o, 0) / low.length : null;
    $('#reliability-caption').textContent = `Predicted probability that an area's basis exceeds $${F.threshold}/MWh vs how often it did, test months in deciles. `
      + (lowObserved === null ? '' : `Where the model says under 1%, separation still happened in ${pct(lowObserved, 1)} of hours: it is overconfident about calm hours.`);
    chart('chart-reliability', (node) => {
      const surface = cssVar('--surface-1');
      Plotly.react(node, [
        { x: [0, 1], y: [0, 1], type: 'scatter', mode: 'lines', line: { color: cssVar('--axis'), width: 1 }, name: 'Perfect calibration', hoverinfo: 'skip' },
        { x: rel.predicted, y: rel.observed, type: 'scatter', mode: 'lines+markers', name: 'LightGBM separation risk',
          line: { color: cssVar('--series-1'), width: 2 }, marker: { size: 8, color: cssVar('--series-1'), line: { color: surface, width: 2 } },
          hovertemplate: 'Predicted %{x:.1%} → observed %{y:.1%}<extra></extra>' },
      ], layout({ showlegend: true, margin: { l: 52, r: 16, t: 30, b: 44 }, xaxis: { title: { text: 'Predicted probability' }, tickformat: '.0%', range: [0, 1] },
        yaxis: { title: { text: 'Observed frequency' }, tickformat: '.0%', range: [0, 1] } }), PLOT_CONFIG);
    });

    $('#forecast-areas').replaceChildren(table([
      { label: 'Area', render: (r) => el('span', {}, `${r.area_id} ${r.name}`, el('span', { class: 'sub', text: r.hub_region })) },
      { label: 'MAE', num: true, render: (r) => dollars(r.mae) }, { label: 'Uniform', num: true, render: (r) => dollars(r.mae_uniform) },
      { label: 'Coverage', num: true, render: (r) => pct(r.coverage) }, { label: 'AUC', num: true, render: (r) => (r.auc === null ? '—' : r.auc.toFixed(2)) },
    ], [...F.per_area].sort((a, b) => b.mae - a.mae)));
  }

  /* ---------- SCED --------------------------------------------------------------------------------- */
  function renderSced() {
    $('#examples').replaceChildren(...S.aeso_examples.map((e) => {
      const match = e.matches_published;
      const rows = e.prices.map((p) => ({ ...p, published: e.published[p.bus], brute: e.incremental_cost[p.bus] }));
      const verdict = el('p', { class: 'verdict' });
      if (match) verdict.append(status('good', 'Matches AESO'), ' Dispatch, flows and every LMP agree with the slides.');
      else {
        const wrong = rows.filter((r) => Math.abs(r.lmp - r.published) > 0.01).map((r) => `bus ${r.bus}`).join(', ');
        verdict.append(status('serious', 'Differs from AESO'), ` At ${wrong} the engine finds `,
          el('strong', { text: `$${rows.find((r) => Math.abs(r.lmp - r.published) > 0.01).lmp.toFixed(0)}/MWh` }),
          '. With line Y at its limit, one more MW at C needs G2 +2 MW and G1 −1 MW: 2 × $40 − $10 = $70. Re-dispatching by brute force gives the same, and dispatch and flows match the slides.');
      }
      const setup = `Line Y limit ${e.line_y_limit} MW${e.contingencies.length ? `; N-1 for loss of line ${e.contingencies.join(', ')}` : ''}. `
        + `Dispatch ${Object.entries(e.dispatch).map(([u, mw]) => `${u} ${mw} MW`).join(', ')}.`;
      return el('div', { class: 'card example' }, el('h3', { text: e.name }), el('p', { class: 'setup', text: setup }),
        table([{ label: 'Bus', key: 'bus' }, { label: 'LMP', num: true, render: (r) => `$${r.lmp.toFixed(0)}` },
          { label: 'MCE', num: true, render: (r) => `$${r.mce.toFixed(0)}` }, { label: 'MCC', num: true, render: (r) => `$${r.mcc.toFixed(0)}` },
          { label: 'Brute force', num: true, render: (r) => `$${r.brute.toFixed(0)}` }, { label: 'AESO slide', num: true, render: (r) => `$${r.published.toFixed(0)}` }],
        rows, { emph: (r) => Math.abs(r.lmp - r.published) > 0.01 }), verdict);
    }));

    const hours = S.showcase;
    segmented($('#network-choice'), hours.map((h, i) => [i, h.name]), state.hour, (value) => { state.hour = Number(value); drawNetwork(); });
    function drawNetwork() {
      const hour = hours[state.hour];
      const h = hour.hour;
      $('#network-caption').textContent = `Load at ${pct(h.load)} of typical, south-west wind ${pct(h.wind_sw)}, south-east wind ${pct(h.wind_se)}, solar ${pct(h.solar)}. `
        + `${hour.contingencies} outages studied, losses ${num(hour.losses_mw)} MW.`;
      chart('network', (node) => {
        const mce = hour.prices[0].mce;
        const lmp = Object.fromEntries(hour.prices.map((p) => [p.bus, p.lmp]));
        const spread = Math.max(10, ...hour.prices.map((p) => Math.abs(p.lmp - mce)));
        const W = 560;
        const H = 470;
        const X = (x) => 60 + (x / 5.2) * (W - 120);
        const Y = (y) => H - 40 - (y / 7) * (H - 80);
        const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
          'aria-label': hour.prices.map((p) => `${p.bus} ${dollars(p.lmp)}`).join(', ') });
        const bindingLines = new Map(hour.binding.map((b) => [b.line, b]));
        const pairs = new Map();
        for (const line of S.case.lines) {
          const key = [line.from, line.to].join('-');
          pairs.set(key, [...(pairs.get(key) || []), line]);
        }
        for (const [, group] of pairs) {
          group.forEach((line, i) => {
            const [a, b] = [DATA.zone_xy[line.from], DATA.zone_xy[line.to]];
            const [x1, y1, x2, y2] = [X(a[0]), Y(a[1]), X(b[0]), Y(b[1])];
            const len = Math.hypot(x2 - x1, y2 - y1);
            const offset = (i - (group.length - 1) / 2) * 6;
            const [nx, ny] = [-(y2 - y1) / len, (x2 - x1) / len];
            const bound = bindingLines.get(line.name);
            const segment = svg('line', { x1: x1 + nx * offset, y1: y1 + ny * offset, x2: x2 + nx * offset, y2: y2 + ny * offset,
              stroke: bound ? cssVar('--serious') : cssVar('--axis'), 'stroke-width': bound ? 4 : 2, 'stroke-linecap': 'round' });
            const tip = svg('title');
            tip.textContent = `${line.name}: ${num(Math.abs(hour.flows[line.name]))} MW${bound ? ', binding' : ''}`;
            segment.append(tip);
            root.append(segment);
            if (bound) {   // label only what matters: the binding limit, on the side facing away from the network
              const [mx, my] = [(x1 + x2) / 2, (y1 + y2) / 2];
              const side = (nx * (mx - W / 2) + ny * (my - H / 2)) >= 0 ? 1 : -1;
              const lx = mx + nx * 16 * side;
              const label = svg('text', { x: lx, y: my + ny * 16 * side + 4, 'text-anchor': lx < mx ? 'end' : 'start', class: 'line-label' });
              label.textContent = `${line.name} binding`;
              root.append(label);
            }
          });
        }
        for (const [zone, [x, y]] of Object.entries(DATA.zone_xy)) {
          const value = lmp[zone];
          const fill = diverging(value - mce, spread);
          const [r, g, b] = fill.match(/\d+/g).map(Number);
          const ink = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 > 0.55 ? '#0b0b0b' : '#f0efec';   // text by fill luminance
          const circle = svg('circle', { cx: X(x), cy: Y(y), r: 26, fill, stroke: cssVar('--surface-1'), 'stroke-width': 3 });
          const tip = svg('title');
          tip.textContent = `${S.case.zones[zone].name}: ${dollars(value)}/MWh`;
          circle.append(tip);
          const name = svg('text', { x: X(x), y: Y(y) - 2, 'text-anchor': 'middle', class: 'zone-label', fill: ink, style: `fill:${ink}` });
          name.textContent = zone;
          const price = svg('text', { x: X(x), y: Y(y) + 13, 'text-anchor': 'middle', class: 'zone-price', style: `fill:${ink}` });
          price.textContent = dollars(value, 0);
          root.append(circle, name, price);
        }
        const legend = el('div', { class: 'legend-row' },
          el('span', { class: 'scale' }, 'below the energy price', el('span', { style: `background: linear-gradient(90deg, ${divergingStops().join(',')})` }), 'above'),
          el('span', {}, el('i', { style: `background:${cssVar('--serious')}; height:4px` }), 'binding limit (status)'));
        node.replaceChildren(root, legend);
      });
      $('#components').replaceChildren(table([{ label: 'Zone', render: (r) => `${r.bus} ${S.case.zones[r.bus].name}` },
        { label: 'LMP', num: true, render: (r) => dollars(r.lmp) }, { label: 'MCE', num: true, render: (r) => dollars(r.mce) },
        { label: 'MCL', num: true, render: (r) => signed(r.mcl) }, { label: 'MCC', num: true, render: (r) => signed(r.mcc) }], hour.prices));
      $('#binding').replaceChildren(el('h3', { text: 'Binding limits', style: 'margin-top:14px' }),
        ...(hour.binding.length ? hour.binding.map((b) => el('p', { class: 'muted', style: 'margin-top:6px;font-size:13px' },
          status('serious', 'Binding'), ` ${b.line}${b.contingency ? ` after loss of ${b.contingency}` : ''}: ${num(Math.abs(b.flow))} of ${num(b.limit)} MW, `
          + `shadow price ${dollars(b.shadow_price)}/MWh.`)) : [el('p', { class: 'muted', text: 'None.' })]));
    }
    drawNetwork();

    const names = Object.keys(S.scenarios);
    const [today, wind] = names.map((n) => S.scenarios[n]);
    const d = S.deltas;
    $('#mc-caption').textContent = `${today.samples} sampled hours (load ±6%, south-west and south-east wind correlated at 0.6, solar by daylight), each dispatched with every single-circuit outage studied and losses on. `
      + 'The same hours run in every scenario.';
    $('#whatif-kpis').replaceChildren(
      tile('South-east price with +400 MW wind', signed(-d.se_price_drop_with_wind), `/MWh on average (${dollars(today.zones.SE.mean)} → ${dollars(wind.zones.SE.mean)})`),
      tile('Renewable curtailment added', `+${num(d.curtailment_added_gwh_per_year, 1)} GWh/yr`, 'trapped behind south-west and south-east limits'),
      tile('Production cost saved by the wind', millions(d.wind_production_cost_savings_musd_per_year), 'a year, annualized from the sample'),
      tile(`Top-ranked upgrade: ${d.top_ranked_line}`, `${millions(d.upgrade_savings_musd_per_year)}/yr`,
        `duals estimated ${millions(d.dual_estimate_musd_per_year)}; uprating ${d.other_upgrade_line} instead saves ${millions(d.other_upgrade_savings_musd_per_year)}`),
    );

    const zones = Object.keys(today.zones);
    chart('chart-zones', (node) => {
      const surface = cssVar('--surface-1');
      const traces = [[today, 'Today', '--series-1', -0.15], [wind, '+400 MW SE wind', '--series-2', 0.15]].map(([sc, name, color, shift]) => ({
        x: zones.map((_, i) => i + shift), y: zones.map((z) => sc.zones[z].mean), type: 'scatter', mode: 'markers', name,
        marker: { size: 10, color: cssVar(color), line: { color: surface, width: 2 } },
        error_y: { type: 'data', symmetric: false, array: zones.map((z) => sc.zones[z].p90 - sc.zones[z].mean),
          arrayminus: zones.map((z) => sc.zones[z].mean - sc.zones[z].p10), color: cssVar(color), thickness: 2, width: 0 },
        customdata: zones.map((z) => [S.case.zones[z].name, sc.zones[z].p10, sc.zones[z].p90]),
        hovertemplate: `<b>%{customdata[0]}</b> · ${name}<br>Mean $%{y:.2f} · P10 $%{customdata[1]:.0f} · P90 $%{customdata[2]:.0f}<extra></extra>` }));
      Plotly.react(node, traces, layout({ showlegend: true, hovermode: 'closest', margin: { l: 52, r: 12, t: 30, b: 40 },
        xaxis: { tickvals: zones.map((_, i) => i), ticktext: zones, showgrid: false, range: [-0.6, zones.length - 0.4] },
        yaxis: { tickprefix: '$', title: { text: 'LMP ($/MWh)' } } }), PLOT_CONFIG);
    });
    $('#table-zones').replaceChildren(table([{ label: 'Zone', key: 'z' }, { label: 'Today mean', num: true, key: 'a' },
      { label: 'P10–P90', num: true, key: 'ar' }, { label: '+wind mean', num: true, key: 'b' }, { label: 'P10–P90', num: true, key: 'br' }],
    zones.map((z) => ({ z: `${z} ${S.case.zones[z].name}`, a: dollars(today.zones[z].mean), ar: `${dollars(today.zones[z].p10, 0)}–${dollars(today.zones[z].p90, 0)}`,
      b: dollars(wind.zones[z].mean), br: `${dollars(wind.zones[z].p10, 0)}–${dollars(wind.zones[z].p90, 0)}` }))));

    chart('chart-ratings', (node) => {
      const corridors = Array.from(new Set([...Object.keys(today.rating_value_kusd_per_mw_year), ...Object.keys(wind.rating_value_kusd_per_mw_year)]));
      const ordered = corridors.sort((a, b) => (wind.rating_value_kusd_per_mw_year[a] || 0) - (wind.rating_value_kusd_per_mw_year[b] || 0));
      const traces = [[today, 'Today', '--series-1'], [wind, '+400 MW SE wind', '--series-2']].map(([sc, name, color]) => ({
        y: ordered, x: ordered.map((c) => sc.rating_value_kusd_per_mw_year[c] || 0), type: 'bar', orientation: 'h', name, marker: { color: cssVar(color) },
        hovertemplate: `%{y} · ${name}: $%{x:.1f}k per MW-year<extra></extra>` }));
      Plotly.react(node, traces, layout({ showlegend: true, barmode: 'group', bargap: 0.3, bargroupgap: 0.12, margin: { l: 72, r: 16, t: 30, b: 44 },
        xaxis: { title: { text: '$k per MW of extra rating per year' }, tickprefix: '$' }, yaxis: { showgrid: false } }), PLOT_CONFIG);
    });
    $('#table-ratings').replaceChildren(table([{ label: 'Corridor', key: 'c' }, { label: 'Today', num: true, key: 'a' }, { label: '+400 MW SE wind', num: true, key: 'b' }],
      Object.keys(wind.rating_value_kusd_per_mw_year).map((c) => ({ c, a: `$${(today.rating_value_kusd_per_mw_year[c] || 0).toFixed(1)}k`, b: `$${wind.rating_value_kusd_per_mw_year[c].toFixed(1)}k` }))));

    $('#scenario-table').replaceChildren(table([
      { label: 'Scenario', key: 'name' }, { label: 'SE price', num: true, render: (r) => dollars(r.s.zones.SE.mean) },
      { label: 'SW price', num: true, render: (r) => dollars(r.s.zones.SW.mean) }, { label: 'ALP', num: true, render: (r) => dollars(r.s.alp_mean) },
      { label: 'Curtailment', num: true, render: (r) => `${num(r.s.curtailment_mw, 1)} MW` },
      { label: 'Congestion rent', num: true, render: (r) => `$${num(r.s.congestion_rent_per_hour)}/h` },
      { label: 'Production cost', num: true, render: (r) => millions(r.s.production_cost_per_hour * 8760 / 1e6, 0) + '/yr' },
      { label: 'Most frequent binding limit', render: (r) => { const [k, v] = Object.entries(r.s.binding_share)[0] || ['none', 0]; return `${k} (${pct(v)})`; } },
    ], names.map((name) => ({ name, s: S.scenarios[name] })), { emph: (r) => r.name === names[2] }));
  }

  /* ---------- PDUP ------------------------------------------------------------------------------------ */
  const SEVERITY = { error: ['critical', 'Deficiency'], warning: ['warning', 'Clarification'], info: ['good', 'Info'] };
  function renderPdup() {
    const reviews = P.reviews;
    // Open on the submission with the most to show.
    state.project = reviews.reduce((best, r, i) => (r.findings.length > reviews[best].findings.length ? i : best), 0);
    segmented($('#project-choice'), reviews.map((r, i) => [i, r.project.name.replace(' (synthetic)', '')]), state.project,
      (value) => { state.project = Number(value); drawProject(); });

    function drawProject() {
      const r = reviews[state.project];
      const p = r.project;
      const kind = r.errors ? ['critical', r.outcome] : r.warnings ? ['warning', r.outcome] : ['good', r.outcome];
      $('#outcome').replaceChildren(
        el('div', {}, el('div', { class: 'project-name', text: p.name }),
          el('div', { class: 'meta', text: `${p.id} · Stage ${p.stage} · ${p.capacity_mw} MW ${p.technology} · POI ${p.poi} · submitted by ${p.submitted_by}` }),
          el('div', { style: 'margin-top:8px' }, status(...kind))),
        el('div', { class: 'counts' }, el('div', {}, el('b', { text: String(r.errors) }), el('span', { text: 'deficiencies' })),
          el('div', {}, el('b', { text: String(r.warnings) }), el('span', { text: 'clarifications' })),
          el('div', {}, el('b', { text: String(r.checklist.filter((c) => c.status === 'pass').length) }), el('span', { text: 'rules passed' }))),
      );
      $('#findings').replaceChildren(...(r.findings.length ? r.findings.map((f) => el('div', { class: 'finding' },
        el('div', { class: 'finding-head' }, status(...SEVERITY[f.severity]), el('code', { text: f.rule }), el('span', { class: 'element', text: f.element })),
        el('div', { text: f.message }),
        f.probable_cause ? el('div', { class: 'cause' }, el('b', { text: 'Probable cause: ' }), f.probable_cause) : null,
        el('div', { class: 'action' }, el('b', { text: 'Action: ' }), f.action, f.impact ? ` Impact: ${f.impact}` : ''))) : [el('p', { class: 'muted', text: 'No findings: every rule passed.' })]));
      drawLetter(r.letter);
      drawSensitivity(r);
    }

    const categories = [];
    for (const row of reviews[0].checklist) if (!categories.includes(row.category)) categories.push(row.category);
    const statusOf = (review, rule) => review.checklist.find((c) => c.rule === rule).status;
    const STATUS = { pass: ['good', 'Pass'], review: ['warning', 'Review'], fail: ['critical', 'Fail'] };
    const rows = [];
    for (const category of categories) {
      rows.push({ group: category });
      for (const row of reviews[0].checklist.filter((c) => c.category === category)) rows.push(row);
    }
    $('#checklist').replaceChildren(table([{ label: 'Rule', render: (row) => el('code', { text: row.rule }) }, { label: 'Check', key: 'title' },
      ...reviews.map((review) => ({ label: review.project.name.replace(' (synthetic)', ''), render: (row) => status(...STATUS[statusOf(review, row.rule)]) }))], rows));

    chart('chart-pq', (node) => {
      const names = reviews.map((r) => r.project.name.replace(' (synthetic)', ''));
      const req = reviews.map((r) => r.power_flow.requirement_mvar);
      const exp = reviews.map((r) => r.power_flow.export.q_poi_mvar);
      const abs = reviews.map((r) => r.power_flow.absorb.q_poi_mvar);
      const traces = [];
      reviews.forEach((r, i) => {
        traces.push({ x: [-req[i], req[i]], y: [names[i], names[i]], type: 'scatter', mode: 'lines', line: { color: alpha(cssVar('--axis-muted'), 0.35), width: 16 },
          name: 'Required (0.95 power factor)', showlegend: i === 0, legendgroup: 'req', hovertemplate: `Required ±${req[i].toFixed(0)} Mvar<extra></extra>` });
        traces.push({ x: [abs[i], exp[i]], y: [names[i], names[i]], type: 'scatter', mode: 'lines+markers', line: { color: cssVar('--series-1'), width: 3 },
          marker: { size: 9, color: cssVar('--series-1'), line: { color: cssVar('--surface-1'), width: 2 } }, name: 'Delivered at the POI',
          showlegend: i === 0, legendgroup: 'got', hovertemplate: '%{x:.0f} Mvar<extra></extra>' });
      });
      Plotly.react(node, traces, layout({ showlegend: true, hovermode: 'closest', margin: { l: 120, r: 16, t: 34, b: 44 },
        xaxis: { title: { text: 'Reactive power at the POI (Mvar): absorb ← → export' }, zeroline: false },
        yaxis: { showgrid: false, range: [-0.6, reviews.length - 0.4] },
        shapes: [{ type: 'line', x0: 0, x1: 0, yref: 'paper', y0: 0, y1: 1, line: { color: cssVar('--axis'), width: 1 } }] }), PLOT_CONFIG);
    });
    $('#table-pq').replaceChildren(table([{ label: 'Project', render: (r) => r.project.name }, { label: 'P at POI', num: true, render: (r) => `${num(r.power_flow.unity.p_poi_mw)} MW` },
      { label: 'Export', num: true, render: (r) => `${num(r.power_flow.export.q_poi_mvar)} Mvar` }, { label: 'Absorb', num: true, render: (r) => `${num(r.power_flow.absorb.q_poi_mvar)} Mvar` },
      { label: 'Required', num: true, render: (r) => `±${num(r.power_flow.requirement_mvar)} Mvar` }], reviews));

    function drawSensitivity(r) {
      const sens = r.sensitivity;
      $('#sensitivity-caption').textContent = sens ? `${r.project.name.replace(' (synthetic)', '')}: POI reactive export margin above the requirement is `
        + `${sens.base >= 0 ? '+' : ''}${sens.base.toFixed(1)} Mvar at base values; bars show where each input's range moves it.` : 'Power flow did not solve.';
      chart('chart-sensitivity', (node) => {
        if (!sens) { node.replaceChildren(); return; }
        const rows = [...sens.rows].reverse();
        const names = rows.map((row) => `${row.parameter} (${row.range})`);
        Plotly.react(node, [
          { y: names, x: rows.map((row) => (row.low ?? sens.base) - sens.base), base: sens.base, type: 'bar', orientation: 'h', name: 'Low end of range',
            marker: { color: cssVar('--series-1') }, customdata: rows.map((row) => row.low), hovertemplate: '%{y}: %{customdata:.1f} Mvar<extra>low end</extra>' },
          { y: names, x: rows.map((row) => (row.high ?? sens.base) - sens.base), base: sens.base, type: 'bar', orientation: 'h', name: 'High end of range',
            marker: { color: cssVar('--series-2') }, customdata: rows.map((row) => row.high), hovertemplate: '%{y}: %{customdata:.1f} Mvar<extra>high end</extra>' },
        ], layout({ showlegend: true, barmode: 'overlay', bargap: 0.45, margin: { l: 250, r: 16, t: 34, b: 44 },
          xaxis: { title: { text: 'Reactive export margin (Mvar)' } }, yaxis: { showgrid: false },
          shapes: [{ type: 'line', x0: 0, x1: 0, yref: 'paper', y0: 0, y1: 1, line: { color: cssVar('--text-secondary'), width: 1 } }],
          annotations: [{ x: 0, y: 0, yref: 'paper', text: 'requirement', showarrow: false, xanchor: 'right', yanchor: 'bottom', xshift: -4,
            font: { size: 11, color: cssVar('--text-secondary') } }] }), PLOT_CONFIG);
      });
      $('#table-sensitivity').replaceChildren(sens ? table([{ label: 'Input', key: 'parameter' }, { label: 'Range', key: 'range' },
        { label: 'Low end', num: true, render: (row) => `${row.low?.toFixed(1)} Mvar` }, { label: 'High end', num: true, render: (row) => `${row.high?.toFixed(1)} Mvar` }], sens.rows) : '');
    }

    chart('chart-anomaly', (node) => {
      const lib = DATA.reference_library;
      const surface = cssVar('--surface-1');
      const traces = [{ x: lib.log_x.map(Math.exp), y: lib.log_r.map(Math.exp), type: 'scatter', mode: 'markers', name: `Accepted models (${lib.log_x.length})`,
        marker: { size: 5, color: alpha(cssVar('--axis-muted'), 0.45) }, hoverinfo: 'skip' }];
      const colors = ['--series-2', '--series-1'];
      const annotations = [];
      reviews.forEach((r, i) => {
        const cables = r.anomaly.lines.filter((l) => l.group === lib.group);
        traces.push({ x: cables.map((c) => c.per_km.x), y: cables.map((c) => c.per_km.r), type: 'scatter', mode: 'markers',
          name: r.project.name.replace(' (synthetic)', ''), marker: { size: 11, color: cssVar(colors[i % 2]), line: { color: surface, width: 2 } },
          customdata: cables.map((c) => [c.element, c.per_km.b, c.flagged ? 'outside the accepted cloud' : 'typical']),
          hovertemplate: '<b>%{customdata[0]}</b><br>R %{y:.3f} Ω/km · X %{x:.3f} Ω/km · B %{customdata[1]:.0f} µS/km<br>%{customdata[2]}<extra></extra>' });
        const flagged = r.findings.filter((f) => f.rule === 'ML-01' || f.rule === 'LINE-01').map((f) => f.element);
        for (const c of cables.filter((c) => flagged.includes(c.element))) {
          const rule = r.findings.find((f) => f.element === c.element && (f.rule === 'ML-01' || f.rule === 'LINE-01')).rule;
          annotations.push({ x: Math.log10(c.per_km.x), y: Math.log10(c.per_km.r), text: `${c.element} (${rule})`, showarrow: true, arrowhead: 0,
            ax: rule === 'LINE-01' ? -70 : 50, ay: -32, arrowcolor: cssVar('--axis-muted'), font: { size: 11.5, color: cssVar('--text-primary') } });
        }
      });
      Plotly.react(node, traces, layout({ showlegend: true, hovermode: 'closest', margin: { l: 60, r: 16, t: 34, b: 48 }, annotations,
        xaxis: { type: 'log', title: { text: 'X per km (Ω/km, log)' } }, yaxis: { type: 'log', title: { text: 'R per km (Ω/km, log)' } } }), PLOT_CONFIG);
    });

    function drawLetter(markdown) {
      const root = $('#letter');
      root.replaceChildren();
      const inline = (text) => {
        const out = [];
        const pattern = /(\*\*[^*]+\*\*|\*[^*]+\*|_[^_]+_)/g;
        let last = 0;
        for (const match of text.matchAll(pattern)) {
          if (match.index > last) out.push(text.slice(last, match.index));
          const token = match[0];
          out.push(token.startsWith('**') ? el('strong', { text: token.slice(2, -2) }) : el('em', { text: token.slice(1, -1) }));
          last = match.index + token.length;
        }
        if (last < text.length) out.push(text.slice(last));
        return out;
      };
      const lines = markdown.split('\n');
      let tableRows = null;
      let list = null;
      const flush = () => {
        if (tableRows) {
          const [head, ...body] = tableRows;
          root.append(el('table', {}, el('thead', {}, el('tr', {}, head.map((c) => el('th', {}, inline(c))))),
            el('tbody', {}, body.map((row) => el('tr', {}, row.map((c) => el('td', {}, inline(c))))))));
          tableRows = null;
        }
        if (list) { root.append(list); list = null; }
      };
      for (const line of lines) {
        if (line.startsWith('|')) {
          const cells = line.split('|').slice(1, -1).map((c) => c.trim());
          if (cells.every((c) => /^-+$/.test(c))) continue;
          tableRows = tableRows || [];
          tableRows.push(cells);
          continue;
        }
        if (line.startsWith('- ')) {
          if (tableRows) flush();
          list = list || el('ul');
          list.append(el('li', {}, inline(line.slice(2))));
          continue;
        }
        flush();
        if (line.startsWith('# ')) root.append(el('h1', { text: line.slice(2) }));
        else if (line.startsWith('## ')) root.append(el('h2', { text: line.slice(3) }));
        else if (line.trim()) root.append(el('p', {}, inline(line)));
      }
      flush();
    }

    drawProject();
  }

  /* ---------- Agent -------------------------------------------------------------------------------- */
  function renderAgent() {
    const transcripts = G.transcripts || [];
    $('#transcripts').replaceChildren(...transcripts.map((t) => {
      const g = t.grounding || { numbers: 0, traced: 0, untraced: [] };
      const traced = g.untraced.length === 0
        ? status('good', `${g.traced} of ${g.numbers} numbers traced to tool results`)
        : status('warning', `${g.untraced.length} of ${g.numbers} numbers not traced: ${g.untraced.join(', ')}`);
      return el('div', { class: 'card' },
        el('div', { class: 'question', text: t.question }),
        el('div', { class: 'steps' }, t.steps.map((s) => el('div', { class: 'step' }, el('code', { text: s.tool }),
          el('span', { class: 'args', text: JSON.stringify(s.arguments) })))),
        el('div', { class: 'answer', text: t.answer }),
        el('div', { class: 'answer-meta' }, traced, el('span', { text: `${t.mode} · ${t.seconds}s` }),
          t.answer_source && t.answer_source !== 'model' ? el('span', { text: `answer from ${t.answer_source}` }) : null));
    }));
    $('#tools').replaceChildren(...(G.tools || []).map((tool) => el('li', {}, el('code', { text: tool.name }), el('span', { text: tool.description }))));
    const modes = Array.from(new Set(transcripts.map((t) => t.mode)));
    const minutes = transcripts.filter((t) => t.mode.startsWith('local LLM')).map((t) => t.seconds / 60);
    const span = minutes.length ? `${Math.max(1, Math.round(Math.min(...minutes)))} to ${Math.round(Math.max(...minutes))} minutes` : 'minutes';
    $('#model-notes').replaceChildren(
      el('li', {}, el('strong', { text: 'A 3B model is fast but misreads. ' }),
        'qwen2.5-3b-instruct answered in seconds, but labelled a 0.67 AUC as "67% separation" and counted area-hours as hours per area. Number tracing passed because every figure existed in the tool output; the errors were semantic.'),
      el('li', {}, el('strong', { text: 'Verified facts fixed most of it. ' }),
        'Giving each tool result pre-written sentences turned the model from reading tables into paraphrasing facts.'),
      el('li', {}, el('strong', { text: `Recorded with ${modes.join(', ') || 'the deterministic planner'}. ` }),
        `A 27B model on a local GPU wrote correct SQL, checked its own totals and answered accurately, taking ${span} per question: `
        + 'fine for a nightly brief, too slow for chat.'),
    );
  }

  /* ---------- About -------------------------------------------------------------------------------- */
  function renderAbout() {
    $('#sources').replaceChildren(...Object.values(DATA.sources).map((s) => el('li', {}, s.title, el('span', {}, el('a', { href: s.url, text: s.url, rel: 'noopener' })))));
    const stack = [
      ['uv · Python 3.12', 'reproducible environment, one CLI'], ['DuckDB', 'warehouse, SQL views, read-only agent queries'],
      ['pandas · NumPy', 'panel data and features'], ['scikit-learn', 'hub clustering, isolation forest, metrics'],
      ['LightGBM', 'quantile forecasts, classifier, tree SHAP'], ['MLflow', 'experiment tracking'],
      ['SciPy HiGHS', 'SCED linear program and duals'], ['pandapower', 'AC power flow and short circuit'],
      ['LM Studio', 'local LLM with tool calling'], ['Plotly', 'charts in a static page'],
      ['pytest · ruff', 'tests and lint'], ['GitHub Actions', 'CI and Pages (on publication)'],
    ];
    $('#stack').replaceChildren(...stack.map(([name, what]) => el('div', {}, el('b', { text: name }), el('span', { text: what }))));
  }

  renderOverview();
  renderSeparation();
  renderHubs();
  renderForecast();
  renderSced();
  renderPdup();
  renderAgent();
  renderAbout();
  showTab((location.hash || '#overview').slice(1), false);
  window.addEventListener('hashchange', () => showTab((location.hash || '#overview').slice(1), false));
})();
