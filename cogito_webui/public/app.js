'use strict';
(function () {

  const $ = (selector, root = document) => root.querySelector(selector);
  const loggedErrors = new Set();
  function reportError(error, context = 'Error', notify = true) {
    let detail = String(error?.stack || error?.message || error || 'Unknown error');
    const key = document.querySelector('#api-key')?.value?.trim();
    if (key) detail = detail.split(key).join('[redacted]');
    detail = detail.replace(/sk-ant-[A-Za-z0-9_-]+/g, '[redacted API key]');
    if (!loggedErrors.has(detail)) {
      if (loggedErrors.size >= 256) loggedErrors.delete(loggedErrors.values().next().value);
      loggedErrors.add(detail);
      console.error(`[Cogito] ${context}`, detail);
    }
    if (notify) toast('Action failed. See the browser console for details.');
  }
  window.addEventListener('error', (event) => reportError(event.error || event.message, 'Browser error', false));
  window.addEventListener('unhandledrejection', (event) => reportError(event.reason, 'Unhandled promise rejection', false));
  const api = (path, init) => fetch(path, init).then(async (response) => {
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `${response.status} ${response.statusText}`);
    return body;
  }).catch((error) => { reportError(error, `Request ${path}`, false); throw error; });

  /* ---- theme ------------------------------------------------------------- */

  const media = window.matchMedia('(prefers-color-scheme: dark)');
  let themeChoice = localStorage.getItem('cogito-theme') || 'system';

  function applyTheme() {
    const resolved = themeChoice === 'system' ? (media.matches ? 'dark' : 'light') : themeChoice;
    document.documentElement.setAttribute('data-theme', resolved);
    for (const button of $('#theme').children) {
      button.setAttribute('aria-checked', String(button.dataset.themeChoice === themeChoice));
    }
  }
  $('#theme').addEventListener('click', (event) => {
    const button = event.target.closest('[data-theme-choice]');
    if (!button) return;
    themeChoice = button.dataset.themeChoice;
    localStorage.setItem('cogito-theme', themeChoice);
    applyTheme();
  });
  media.addEventListener('change', () => { if (themeChoice === 'system') applyTheme(); });
  applyTheme();

  /* ---- logo -------------------------------------------------------------- */

  fetch('cogito-logo.svg').then((r) => r.text()).then((markup) => {
    $('#brand-mark').innerHTML = markup;
    $('#watermark-mark').innerHTML = markup;
    for (const svg of document.querySelectorAll('#brand-mark svg, #watermark-mark svg')) {
      svg.removeAttribute('width');
      svg.removeAttribute('height');
      svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    }
  }).catch((error) => reportError(error, 'Loading logo', false));

  /* ---- toast ------------------------------------------------------------- */

  let toastTimer = null;
  function toast(message) {
    const node = $('#toast');
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, 5200);
  }

  /* ---- settings state ---------------------------------------------------- */

  const ui = {
    designId: null,
    folder: null,
    preparedDesign: null,
    budget: 10,
    provider: null,
    starting: false,
    run: null,
    viz: { state: 'idle', url: null },
    replayEnabled: false,
    designs: [],
  };

  function icon(name) { return `<svg><use href="#i-${name}"/></svg>`; }

  function renderCards() {
    const host = $('#design-cards');
    host.innerHTML = '';
    for (const design of ui.designs) {
      const card = document.createElement('button');
      card.className = 'card';
      card.type = 'button';
      const usable = design.available && !design.locked;
      card.disabled = !usable;
      card.setAttribute('aria-pressed', String(ui.designId === design.id && !ui.folder));
      const lock = design.locked ? `<span class="sub lock">${icon('lock')} ${design.lockReason}</span>` : '';
      card.innerHTML = `
        <div class="row">
          <span class="name">${design.name}</span>
          <span class="tick">${icon('check')}</span>
        </div>
        <span class="sub">${design.subtitle || ''}</span>
        ${lock}`;
      if (design.locked && design.lockDetail) card.title = design.lockDetail;
      if (!design.available) card.title = 'RTL file not found on disk';
      card.addEventListener('click', () => {
        ui.designId = design.id;
        ui.folder = null;
        ui.preparedDesign = null;
        renderCards();
        syncControls();
      });
      host.appendChild(card);
    }

    const upload = document.createElement('button');
    upload.className = 'card upload';
    upload.type = 'button';
    upload.setAttribute('aria-pressed', String(Boolean(ui.folder)));
    const prepared = ui.preparedDesign;
    const uploadSummary = prepared
      ? `${esc(prepared.top)} · ${prepared.rtlFiles} RTL file${prepared.rtlFiles === 1 ? '' : 's'} · ready${prepared.editScope ? `<br>${esc(prepared.editScope)} (${prepared.editableFiles} files)` : ''}`
      : 'Pick a local folder containing Verilog or SystemVerilog';
    upload.innerHTML = `
      <div class="row">
        <span class="name">${icon('folder')} Upload your own</span>
        <span class="tick">${icon('check')}</span>
      </div>
      <span class="sub">${uploadSummary}</span>
      ${ui.folder ? `<span class="path">${esc(ui.folder)}</span>` : ''}`;
    upload.addEventListener('click', openFolderPicker);
    host.appendChild(upload);
  }

  /* ---- budget ------------------------------------------------------------ */

  const budgetInput = $('#budget');
  budgetInput.addEventListener('input', () => {
    ui.budget = Number(budgetInput.value);
    syncBudgetPresets();
    syncControls();
  });
  $('#budget-presets').addEventListener('click', (event) => {
    const button = event.target.closest('[data-amount]');
    if (!button) return;
    budgetInput.value = button.dataset.amount;
    ui.budget = Number(button.dataset.amount);
    syncBudgetPresets();
    syncControls();
  });
  function syncBudgetPresets() {
    for (const button of $('#budget-presets').children) {
      button.classList.toggle('on', Number(button.dataset.amount) === ui.budget);
    }
  }

  function syncCost() {
    const run = ui.run;
    const spent = run?.cost;
    const row = $('#cost-spent');
    if (spent === null || spent === undefined) { row.classList.add('idle'); $('#cost-meter').style.width = '0%'; return; }
    row.classList.remove('idle');
    $('#cost-value').textContent = `$${Number(spent).toFixed(4)}`;
    const budget = Number(run?.budgetUsd || ui.budget) || 1;
    $('#cost-meter').style.width = `${Math.min(100, (spent / budget) * 100).toFixed(1)}%`;
    row.firstElementChild.textContent = run?.status === 'finished' ? 'Actual cost' : 'Spent so far';
  }

  /* ---- Claude connection ------------------------------------------------- */

  const apiKeyInput = $('#api-key');
  for (const option of document.querySelectorAll('input[name="connection"]')) {
    option.addEventListener('change', () => {
      ui.provider = option.value;
      $('#api-key-field').hidden = ui.provider !== 'api';
      $('#connection-note').textContent = ui.provider === 'cli'
        ? 'Uses your authenticated Claude Code CLI.' : 'Connects directly to Anthropic.';
      if (ui.provider === 'api') apiKeyInput.focus();
      else apiKeyInput.value = '';
      syncControls();
    });
  }
  apiKeyInput.addEventListener('input', syncControls);

  /* ---- controls ---------------------------------------------------------- */

  function running() { return ui.run?.status === 'running'; }

  function syncControls() {
    const optimize = $('#optimize');
    const label = $('.lbl', optimize);
    const slot = $('.ico', optimize);
    const budgetOk = Number.isFinite(ui.budget) && ui.budget >= 0.5;
    const connectionOk = ui.provider === 'cli' || (ui.provider === 'api'
      && /^[\x21-\x7e]+$/.test(apiKeyInput.value.trim()));
    const designOk = Boolean((ui.folder && ui.preparedDesign) || ui.designId === 'dev-mode'
      || ui.designs.some((d) => d.id === ui.designId && !d.locked && d.available));

    if (running()) {
      optimize.disabled = false;
      optimize.classList.add('ghost');
      slot.innerHTML = '<span class="spinner"></span>';
      label.textContent = ui.run.mode === 'replay' ? 'Replaying… stop'
        : ui.run.mode === 'dev' ? 'Simulating… stop' : 'Running… stop';
    } else {
      optimize.disabled = ui.starting || !(budgetOk && designOk && connectionOk);
      optimize.classList.remove('ghost');
      slot.innerHTML = '<svg><use href="#i-play"/></svg>';
      label.textContent = ui.starting ? 'Starting…' : 'Optimize';
    }
    for (const option of document.querySelectorAll('input[name="connection"]')) option.disabled = running() || ui.starting;
    apiKeyInput.disabled = running() || ui.starting;

    const vizButton = $('#viz-open');
    const vizReady = Boolean(ui.run?.shinka.launched && ui.run?.shinka.results);
    vizButton.disabled = !vizReady && ui.viz.state !== 'ready';
    $('.lbl', vizButton).textContent = ui.viz.state === 'starting'
      ? (ui.viz.message ? 'Waiting for first program…' : 'Starting visualizer…') : 'Shinka Visualizer';

    const hint = $('#hint');
    if (running()) hint.textContent = '';
    else if (!designOk) hint.textContent = 'Select a design to begin.';
    else if (!budgetOk) hint.textContent = 'Set a budget of at least $0.50.';
    else if (!ui.provider) hint.textContent = 'Choose Claude Code CLI or Anthropic API.';
    else if (!connectionOk) hint.textContent = 'Enter your Anthropic API key.';
    else hint.textContent = 'Ready.';
    hint.hidden = !designOk && !ui.run;

    syncCost();
  }

  $('#optimize').addEventListener('click', async () => {
    if (running()) {
      await api('/api/cancel', { method: 'POST' }).catch((error) => reportError(error));
      return;
    }
    if ($('#optimize').disabled) return;
    ui.starting = true;
    syncControls();
    try {
      const body = ui.folder ? { folder: ui.folder, budget: ui.budget } : { designId: ui.designId, budget: ui.budget };
      body.provider = ui.provider;
      if (ui.provider === 'api') body.apiKey = apiKeyInput.value.trim();
      await api('/api/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      apiKeyInput.value = '';
    } catch (error) { reportError(error); }
    finally { ui.starting = false; syncControls(); }
  });

  /* ---- folder picker ----------------------------------------------------- */

  let folderCurrent = null;
  async function openFolderPicker(startPath) {
    $('#folder-scrim').hidden = false;
    await loadFolder(typeof startPath === 'string' ? startPath : ui.folder);
  }
  async function loadFolder(path) {
    try {
      const data = await api(`/api/fs${path ? `?path=${encodeURIComponent(path)}` : ''}`);
      folderCurrent = data;
      $('#folder-crumb').textContent = data.path;
      const list = $('#folder-list');
      list.innerHTML = '';
      if (data.parent) {
        const up = document.createElement('button');
        up.innerHTML = `${icon('up')}<span>..</span>`;
        up.addEventListener('click', () => loadFolder(data.parent));
        list.appendChild(up);
      }
      for (const entry of data.entries) {
        const row = document.createElement('button');
        const tag = entry.hasConfig ? '<span class="chip tag">Configured</span>'
          : entry.hasRtl ? '<span class="chip tag">RTL</span>' : '';
        row.innerHTML = `${icon('folder')}<span>${esc(entry.name)}</span>${tag}`;
        row.addEventListener('click', () => loadFolder(entry.path));
        list.appendChild(row);
      }
      const usable = data.hasConfig || data.rtlCount > 0;
      $('#folder-pick').disabled = !usable;
      $('#folder-note').textContent = data.hasConfig ? 'Cogito configuration found'
        : data.rtlCount > 0
          ? `${data.rtlLimitReached ? '512+' : data.rtlCount} RTL file${data.rtlCount === 1 ? '' : 's'} found · setup is automatic`
          : 'No Verilog or SystemVerilog found in this folder';
    } catch (error) { reportError(error); }
  }
  $('#folder-close').addEventListener('click', () => { $('#folder-scrim').hidden = true; });
  $('#folder-cancel').addEventListener('click', () => { $('#folder-scrim').hidden = true; });
  $('#folder-pick').addEventListener('click', async () => {
    const button = $('#folder-pick');
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> Preparing…';
    $('#folder-note').textContent = 'Preparing and checking equivalence, synthesis and timing. This can take a few minutes…';
    const selectedFolder = folderCurrent.path;
    try {
      const { design } = await api('/api/design/prepare', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder: selectedFolder }),
      });
      ui.folder = selectedFolder;
      ui.preparedDesign = design;
      ui.designId = null;
      $('#folder-scrim').hidden = true;
      renderCards();
      syncControls();
      toast(`${design.top} is ready to optimize.`);
    } catch (error) {
      reportError(error, 'Folder preparation', false);
      await loadFolder(folderCurrent.path);
      $('#folder-note').textContent = 'Preparation failed. See the browser console for details.';
    } finally {
      button.textContent = 'Use this folder';
      button.disabled = !(folderCurrent?.hasConfig || folderCurrent?.rtlCount > 0);
    }
  });

  /* ---- shinka visualizer ------------------------------------------------- */
  function visualizerDashboard(url) {
    const dashboard = new URL(url, window.location.href);
    dashboard.pathname = '/';
    return dashboard.toString();
  }
  function closeVisualizer() {
    $('#viz-scrim').hidden = true;
    // Drop the child page so reopening always starts at a clean dashboard. The
    // visualizer process/database stays alive and is reused by the next click.
    $('#viz-frame').src = 'about:blank';
  }

  $('#viz-open').addEventListener('click', async () => {
    const frame = $('#viz-frame');
    if (ui.viz.state === 'ready' && ui.viz.url) {
      const dashboard = visualizerDashboard(ui.viz.url);
      if (frame.src !== dashboard) frame.src = dashboard;
      $('#viz-scrim').hidden = false;
      return;
    }
    try {
      $('#viz-open').disabled = true;
      const { viz } = await api('/api/viz/start', { method: 'POST' });
      ui.viz = viz;
      if (viz.state === 'ready') {
        frame.src = visualizerDashboard(viz.url);
        $('#viz-scrim').hidden = false;
      } else {
        reportError(viz.error || 'Visualizer did not start', 'Visualizer');
      }
    } catch (error) { reportError(error); }
    finally { syncControls(); }
  });
  $('#viz-close').addEventListener('click', closeVisualizer);
  $('#viz-scrim').addEventListener('click', (event) => { if (event.target === $('#viz-scrim')) closeVisualizer(); });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (!$('#viz-scrim').hidden) closeVisualizer();
    else if (!$('#folder-scrim').hidden) $('#folder-scrim').hidden = true;
    else for (const pane of Object.values(panes)) if (pane?.full) setFullscreen(pane, false);
  });

  /* ---- panes ------------------------------------------------------------- */

  // Each pane has exactly one collapse control and one full-screen control.
  //   collapsed : header bar only
  //   full      : fixed over the whole window, always open
  // Leaving full screen restores the pane open, never collapsed.
  const stage = $('#stage');
  const PANE_ORDER = ['graph', 'logs', 'eqy', 'report'];
  const panes = { graph: null, logs: null, eqy: null, report: null };
  let graphView = null;
  let filler = null;
  let flowView = null;
  let stageLayoutAnimation = null;
  let stageLayoutAnimationFrame = null;

  function makePane(id, title, { tabs = null } = {}) {
    const pane = document.createElement('section');
    pane.className = 'pane';
    pane.dataset.pane = id;
    pane.innerHTML = `
      <header>
        <h3>${title}</h3>
        <span class="meta" data-role="meta"></span>
        <span class="grow"></span>
        ${tabs ? `<div class="tabs" data-role="tabs">${tabs.map((t, i) => `<button role="tab" data-tab="${t.id}" aria-selected="${i === 0}">${t.label}</button>`).join('')}</div>` : ''}
        <button class="icon-btn" data-act="collapse" title="Collapse" aria-pressed="false">${icon('chevron-up')}</button>
        <button class="icon-btn" data-act="full" title="Full screen" aria-pressed="false">${icon('maximize')}</button>
      </header>
      <div class="body"></div>`;
    const handle = { id, el: pane, body: $('.body', pane), meta: $('[data-role="meta"]', pane), collapsed: false, full: false };
    $('[data-act="collapse"]', pane).addEventListener('click', () => setCollapsed(handle, !handle.collapsed));
    $('[data-act="full"]', pane).addEventListener('click', () => setFullscreen(handle, !handle.full));
    return handle;
  }

  function paintPaneControls(pane) {
    const collapse = $('[data-act="collapse"]', pane.el);
    const full = $('[data-act="full"]', pane.el);
    collapse.innerHTML = icon(pane.collapsed ? 'chevron-down' : 'chevron-up');
    collapse.title = pane.collapsed ? 'Expand' : 'Collapse';
    collapse.setAttribute('aria-pressed', String(pane.collapsed));
    collapse.disabled = pane.full;
    full.innerHTML = icon(pane.full ? 'restore' : 'maximize');
    full.title = pane.full ? 'Exit full screen' : 'Full screen';
    full.setAttribute('aria-pressed', String(pane.full));
    pane.el.classList.toggle('collapsed', pane.collapsed && !pane.full);
    pane.el.classList.toggle('fullscreen', pane.full);
  }

  function setCollapsed(pane, collapsed) {
    if (pane.full) return;
    const before = captureStageLayout();
    pane.collapsed = collapsed;
    paintPaneControls(pane);
    layoutStage({ before });
  }

  function setFullscreen(pane, full) {
    const before = captureStageLayout();
    for (const other of Object.values(panes)) {
      if (other && other !== pane && other.full) { other.full = false; paintPaneControls(other); }
    }
    pane.full = full;
    if (full) pane.collapsed = false;   // full screen always shows the body
    paintPaneControls(pane);
    layoutStage({ before });
    // Fullscreen uses width/height/position interpolation, so its contents
    // retain their native size while the pane grows into the viewport.
    if (full && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      animateFullscreenGeometry(pane, before);
    }
    if (pane.id === 'graph' && graphView) setTimeout(() => graphView.fit(), 460);
  }

  function ensurePanes() {
    if (!filler) {
      filler = document.createElement('div');
      filler.className = 'filler';
      const host = document.createElement('div');
      host.className = 'flow-host';
      filler.appendChild(host);
      filler.hidden = true;
      stage.appendChild(filler);
      flowView = window.CogitoFlow.create(host);
    }
    if (!panes.graph) {
      panes.graph = makePane('graph', 'Timing graph');
      graphView = window.CogitoGraph.create(panes.graph.body);
      stage.insertBefore(panes.graph.el, filler);
    }
    if (!panes.logs) {
      panes.logs = makePane('logs', 'Run log', {
        tabs: [{ id: 'heuristics', label: 'Heuristics' }, { id: 'agent', label: 'Shinka' }],
      });
      const term = document.createElement('div');
      term.className = 'term';
      panes.logs.body.appendChild(term);
      panes.logs.term = term;
      panes.logs.tab = 'heuristics';
      $('[data-role="tabs"]', panes.logs.el).addEventListener('click', (event) => {
        const button = event.target.closest('[data-tab]');
        if (!button) return;
        panes.logs.tab = button.dataset.tab;
        for (const b of button.parentElement.children) b.setAttribute('aria-selected', String(b === button));
        paintTerminal(true);
      });
      const actions = document.createElement('div');
      actions.className = 'log-actions';
      actions.innerHTML = `
        <button class="chip-btn" data-log="save">${icon('save')}<span>Save log</span></button>
        <button class="chip-btn ghost" data-log="discard">${icon('trash')}<span>Discard</span></button>`;
      $('[data-role="tabs"]', panes.logs.el).before(actions);
      panes.logs.actions = actions;
      actions.addEventListener('click', (event) => {
        const button = event.target.closest('[data-log]');
        if (button) saveOrDiscardLogs(button.dataset.log);
      });
      stage.insertBefore(panes.logs.el, filler);
    }
    if (!panes.eqy) {
      panes.eqy = makePane('eqy', 'Equivalence');
      panes.eqy.body.innerHTML = '<div class="eqy"><div class="empty">Waiting for the first evaluated generation…</div></div>';
      panes.eqy.el.hidden = true;
      stage.insertBefore(panes.eqy.el, filler);
    }
    if (!panes.report) {
      panes.report = makePane('report', 'Result');
      panes.report.body.innerHTML = '<div class="report"></div>';
      panes.report.el.hidden = true;
      stage.insertBefore(panes.report.el, filler);
    }
  }

  // Grid rows: collapsed panes are the fixed header height, open panes share what
  // is left, and when nothing is open the watermark filler takes the remainder.
  function captureStageLayout() {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return null;
    const rects = new Map();
    for (const child of stage.children) {
      // Keep fullscreen panes in the snapshot too. On restore they become grid
      // items again, and FLIP can then animate that move back into the layout.
      if (!child.hidden) rects.set(child, child.getBoundingClientRect());
    }
    return rects;
  }

  function animateStageLayout(before) {
    if (!before || !before.size || !stage.animate) return;
    if (stageLayoutAnimation) stageLayoutAnimation.forEach((animation) => animation.cancel());
    if (stageLayoutAnimationFrame) cancelAnimationFrame(stageLayoutAnimationFrame);
    stageLayoutAnimation = [];
    stageLayoutAnimationFrame = requestAnimationFrame(() => {
      stageLayoutAnimationFrame = null;
      for (const [element, oldRect] of before) {
        if (element.hidden || element.classList.contains('fullscreen') || !element.animate) continue;
        const newRect = element.getBoundingClientRect();
        if (!newRect.width || !newRect.height) continue;
        const dx = oldRect.left - newRect.left;
        const dy = oldRect.top - newRect.top;
        const widthChanged = Math.abs(oldRect.width - newRect.width) > 1;
        const heightChanged = Math.abs(oldRect.height - newRect.height) > 1;
        if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5 && !widthChanged && !heightChanged) continue;
        const animation = element.animate([
          {
            transformOrigin: 'top left', transform: `translate(${dx}px, ${dy}px)`,
            width: `${oldRect.width}px`, height: `${oldRect.height}px`,
          },
          {
            transformOrigin: 'top left', transform: 'translate(0, 0)',
            width: `${newRect.width}px`, height: `${newRect.height}px`,
          },
        ], { duration: 420, easing: 'cubic-bezier(0.22, 0.61, 0.36, 1)' });
        stageLayoutAnimation.push(animation);
      }
    });
  }

  function animateFullscreenGeometry(pane, before) {
    if (!before || !before.has(pane.el) || !pane.el.animate) return;
    requestAnimationFrame(() => {
      const oldRect = before.get(pane.el);
      const newRect = pane.el.getBoundingClientRect();
      if (!oldRect || !newRect.width || !newRect.height) return;
      const dx = oldRect.left - newRect.left;
      const dy = oldRect.top - newRect.top;
      pane.el.animate([
        {
          transformOrigin: 'top left', transform: `translate(${dx}px, ${dy}px)`,
          width: `${oldRect.width}px`, height: `${oldRect.height}px`,
        },
        {
          transformOrigin: 'top left', transform: 'translate(0, 0)',
          width: `${newRect.width}px`, height: `${newRect.height}px`,
        },
      ], { duration: 420, easing: 'cubic-bezier(0.22, 0.61, 0.36, 1)' });
    });
  }

  function layoutStage({ before = null } = {}) {
    const visible = [];
    for (const id of PANE_ORDER) {
      const pane = panes[id];
      if (!pane) continue;
      const show = id === 'graph' ? Boolean(ui.run?.graph.initial || ui.run?.graph.final)
        : id === 'report' ? Boolean(ui.run?.report)
          : id === 'eqy' ? Boolean(ui.run?.shinka?.equivalence)
            : Boolean(ui.run && ui.run.status !== 'idle'
                && (buffers.heuristics.length || buffers.agent.length));
      pane.el.hidden = !show;
      if (show) visible.push(pane);
    }
    const inGrid = visible.filter((p) => !p.full);
    const open = inGrid.filter((p) => !p.collapsed);
    // The graph is the pane that suffers most from being short: a netlist drawn
    // into a 250px band is unreadable whatever the zoom. It gets a taller floor
    // and a larger share of whatever is left.
    const floor = open.length > 2 ? 300 : 0;
    const rows = inGrid.map((pane) => {
      // A numeric collapsed track lets CSS interpolate the row while the pane
      // opens/closes; `auto` would make that part of the resize jump.
      if (pane.collapsed) return '48px';
      const isGraph = pane.id === 'graph';
      return `minmax(${isGraph ? Math.max(floor, 430) : floor}px, ${isGraph && open.length > 1 ? '1.9fr' : '1fr'})`;
    });
    // Keep the system diagram mounted at the bottom of the stage. If panes take
    // more room than the viewport, the stage itself becomes the scroll container
    // so the diagram remains reachable instead of disappearing.
    stage.classList.toggle('scrolls', open.length > 2 || Boolean(filler));
    if (filler) {
      // The flowchart is persistent once panes exist. It gets a useful minimum
      // height and scrolls below the panes when the available viewport is short.
      filler.hidden = false;
      if (open.length === 0) rows.push('minmax(0, 1fr)'); // idle watermark
      rows.push('minmax(220px, 280px)');
    }
    stage.style.gridTemplateRows = rows.length ? rows.join(' ') : '1fr';
    animateStageLayout(before);
    if (graphView) requestAnimationFrame(() => {
      graphView.relayout();
      if (panes.graph) panes.graph.el.classList.toggle('short', !panes.graph.full && panes.graph.body.getBoundingClientRect().height < 380);
    });
  }

  // The diagram lights the stage the run is actually in, read from the same state
  // the panes use. Nothing is inferred from timing.
  function flowStage() {
    const run = ui.run;
    if (!run || run.status === 'idle') return null;
    const stages = run.stages || {};
    if (run.status === 'finished' || run.report) return 'optimized';
    if (stages.final_graph?.state === 'running' || run.graph?.final) return 'optimized';
    if (run.shinka?.launched) {
      const evaluated = run.shinka.equivalence?.counts?.evaluated || 0;
      return evaluated > 0 ? 'evaluation' : 'shinka';
    }
    if (stages.graph?.state === 'running') return 'graph';
    if (stages.heuristics?.state === 'running') return 'heuristics';
    if (stages.graph?.state === 'done' && stages.heuristics?.state === 'done') return 'orchestrator';
    return 'initial';
  }

  function updateFlow() {
    if (flowView) flowView.set(flowStage());
  }

  window.addEventListener('resize', () => { if (graphView) graphView.relayout(); layoutStage(); });

  /* ---- terminal ---------------------------------------------------------- */

  const buffers = { heuristics: [], agent: [] };

  function appendLines(pane, lines) {
    for (const line of lines) {
      if (line.cls === 'crit' || /\b(?:ERROR|FAILED)\b/.test(line.text || '')) reportError(line.text, `${pane} log`, false);
    }
    buffers[pane].push(...lines);
    if (buffers[pane].length > 6000) buffers[pane].splice(0, buffers[pane].length - 6000);
    if (panes.logs && panes.logs.tab === pane) paintTerminal(false, lines);
    layoutStage();
  }

  function paintTerminal(full, incremental) {
    if (!panes.logs) return;
    const term = panes.logs.term;
    const atBottom = term.scrollTop + term.clientHeight >= term.scrollHeight - 40;
    const rows = full ? buffers[panes.logs.tab] : incremental;
    if (full) term.textContent = '';
    if (full && !rows.length) {
      const idle = document.createElement('span');
      idle.className = 'l empty';
      idle.textContent = panes.logs.tab === 'heuristics'
        ? 'Heuristic analysis has not finished yet.' : 'Waiting for Shinka…';
      term.appendChild(idle);
    }
    const fragment = document.createDocumentFragment();
    for (const line of rows) {
      const span = document.createElement('span');
      span.className = `l ${line.cls || ''}`.trim();
      span.textContent = line.text || ' ';
      fragment.appendChild(span);
    }
    term.appendChild(fragment);
    panes.logs.meta.textContent = `${buffers[panes.logs.tab].length} lines`;
    if (atBottom || full) term.scrollTop = term.scrollHeight;
  }

  /* ---- run log retention -------------------------------------------------- */

  // Run logs are deleted when the next run starts unless they were saved here,
  // so the buttons only appear once there is a finished run to act on.
  function syncLogActions() {
    if (!panes.logs?.actions) return;
    const run = ui.run;
    const done = run && (run.status === 'finished' || run.status === 'failed') && run.runDir;
    panes.logs.actions.hidden = !done;
    if (!done) return;
    const saved = Boolean(run.logsKept);
    const save = $('[data-log="save"]', panes.logs.actions);
    save.classList.toggle('done', saved);
    $('span', save).textContent = saved ? 'Log saved' : 'Save log';
    $('[data-log="discard"]', panes.logs.actions).disabled = false;
  }

  async function saveOrDiscardLogs(action) {
    try {
      if (action === 'save') {
        const result = await api('/api/logs/save', { method: 'POST' });
        toast('Log saved next to the run, and kept on disk.');
        const link = document.createElement('a');
        link.href = '/api/artifact?name=cogito-ui-log.txt';
        link.download = 'cogito-run-log.txt';
        document.body.appendChild(link);
        link.click();
        link.remove();
        return result;
      }
      const result = await api('/api/logs/discard', { method: 'POST' });
      toast(result.removed ? `Deleted ${result.removed} log file${result.removed === 1 ? '' : 's'} (${Math.round(result.bytes / 1024)} kB).`
        : 'No log files left to delete.');
      return result;
    } catch (error) { reportError(error); return null; }
  }

  /* ---- report ------------------------------------------------------------ */

  function formatNumber(value, digits) {
    if (value === null || value === undefined) return '—';
    const n = Number(value);
    if (!Number.isFinite(n)) return String(value);
    return n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function duration(seconds) {
    if (seconds === null || seconds === undefined) return '—';
    const total = Math.round(Number(seconds));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
  }

  function renderReport(report) {
    const host = $('.report', panes.report.body);
    const cost = report.cost.accounted ?? report.cost.native ?? report.cost.reported;
    const tat = report.totalElapsedSeconds ?? report.elapsedSeconds;

    const facts = [
      ['Generations', report.generations ?? '—', ''],
      ['Equivalence', report.equivalence === true ? 'pass' : report.equivalence === false ? 'fail' : '—',
        report.equivalence === true ? 'pass' : report.equivalence === false ? 'fail' : ''],
      ['Timing closed', report.timingClosed === true ? 'yes' : report.timingClosed === false ? 'no' : '—',
        report.timingClosed === true ? 'pass' : report.timingClosed === false ? 'fail' : ''],
      ['Total cost', cost === null || cost === undefined ? '—' : `$${Number(cost).toFixed(4)}`, ''],
      ['Turnaround', duration(tat), ''],
    ];

    const rows = report.metrics.map((metric) => {
      const initial = formatNumber(metric.initial, metric.digits);
      const final = formatNumber(metric.final, metric.digits);
      let delta = '';
      if (typeof metric.initial === 'number' && typeof metric.final === 'number') {
        const change = metric.final - metric.initial;
        const improved = metric.better === 'higher' ? change > 0 : change < 0;
        const percent = metric.initial !== 0 ? ` (${(change / Math.abs(metric.initial) * 100).toFixed(1)}%)` : '';
        if (change !== 0) {
          delta = `<span class="delta ${improved ? 'good' : 'bad'}">${change > 0 ? '+' : ''}${formatNumber(change, metric.digits)}${percent}</span>`;
        }
      }
      const unit = metric.unit ? ` <span style="color:var(--faint)">${metric.unit}</span>` : '';
      return `<tr><td>${metric.label}${unit}</td><td class="num">${initial}</td><td class="num">${final}${delta}</td></tr>`;
    }).join('');

    host.innerHTML = `
      <dl class="facts">${facts.map(([k, v, cls]) => `<div><dt>${k}</dt><dd class="${cls}">${v}</dd></div>`).join('')}</dl>
      <table>
        <thead><tr><th>Metric</th><th>Initial</th><th>Final</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      `;
    panes.report.meta.textContent = report.state || '';
  }

  /* ---- live timing strip ------------------------------------------------- */

  function signed(value, digits) {
    if (value === null || value === undefined) return '—';
    const n = Number(value);
    return (n > 0 ? '+' : '') + n.toFixed(digits);
  }

  // Counts from the moment the run started until it stops, so the strip always
  // answers "how long has this been going" without waiting for the final report.
  let elapsedTimer = null;
  function paintElapsed() {
    const run = ui.run;
    const node = $('#live-elapsed');
    if (!node) return;
    if (!run?.startedAt) { node.textContent = '—'; return; }
    const end = run.status === 'running' ? Date.now() : (run.finishedAt || Date.now());
    node.textContent = duration((end - run.startedAt) / 1000);
  }
  function syncElapsedTimer() {
    const live = ui.run?.status === 'running';
    if (live && !elapsedTimer) elapsedTimer = setInterval(paintElapsed, 1000);
    if (!live && elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
    paintElapsed();
  }

  const generationMetrics = new Map();
  let metricsRequestToken = 0;
  function generationCell(kind, metric) {
    return document.querySelector(`[data-generation-column="${kind}"][data-metric="${metric}"]`);
  }
  function paintGenerationColumn(kind, generation) {
    const metrics = generation ? generationMetrics.get(generation.generation) : null;
    const publicMetrics = metrics?.public || {};
    const heading = document.querySelector(`.generation-heading[data-generation-column="${kind}"] b`);
    if (heading) heading.textContent = generation ? `#${generation.generation}` : '—';
    const set = (metric, value) => { const cell = generationCell(kind, metric); if (cell) cell.textContent = value; };
    set('wns', generation?.wns == null ? '—' : signed(generation.wns, 3));
    set('tns', generation?.tns == null ? '—' : signed(generation.tns, 1));
    set('power', publicMetrics.power_w == null ? '—' : Number(publicMetrics.power_w).toFixed(4));
    set('area', publicMetrics.cell_area_um2 == null ? '—' : Math.round(Number(publicMetrics.cell_area_um2)).toLocaleString());
  }
  async function loadGenerationMetrics(generations) {
    const token = ++metricsRequestToken;
    await Promise.all(generations.filter((gen) => gen.evaluated && !generationMetrics.has(gen.generation)).map(async (gen) => {
      try {
        const response = await fetch(`/api/artifact?name=${encodeURIComponent(`shinka/gen_${gen.generation}/results/metrics.json`)}`);
        if (response.ok) generationMetrics.set(gen.generation, await response.json());
      } catch (error) { reportError(error, 'Generation metrics', false); }
    }));
    if (token !== metricsRequestToken) return;
    const evaluated = generations.filter((gen) => gen.evaluated);
    // Generation zero is the measured seed and is the baseline shown in the
    // Initial column. Keep it tied to its generation rather than to the
    // current/best selection, which can change as the search progresses.
    const initial = evaluated.find((gen) => gen.generation === 0) || null;
    const current = evaluated.at(-1) || null;
    const best = evaluated.filter((gen) => gen.correct !== false)
      .sort((a, b) => Number(generationMetrics.get(b.generation)?.combined_score ?? -Infinity)
        - Number(generationMetrics.get(a.generation)?.combined_score ?? -Infinity))[0] || current;
    paintGenerationColumn('initial', initial);
    paintGenerationColumn('current', current);
    paintGenerationColumn('best', best);
  }
  function renderLive(equivalence) {
    const section = $('#live');
    const latest = equivalence?.latest;
    // The strip is also the run clock, so it appears as soon as a run starts.
    if (!latest && !ui.run?.startedAt) { section.hidden = true; return; }
    section.hidden = false;
    paintElapsed();
    const generations = equivalence?.generations || [];
    const evaluated = generations.filter((gen) => gen.evaluated);
    paintGenerationColumn('initial', evaluated.find((gen) => gen.generation === 0) || null);
    paintGenerationColumn('current', evaluated.at(-1) || null);
    paintGenerationColumn('best', evaluated.at(-1) || null);
    if (generations.length) loadGenerationMetrics(generations);
  }

  /* ---- equivalence panel ------------------------------------------------- */

  function seconds(value) {
    if (value === null || value === undefined) return '—';
    const n = Number(value);
    return n >= 60 ? `${Math.floor(n / 60)}m ${Math.round(n % 60)}s` : `${n.toFixed(1)}s`;
  }

  function esc(text) { return String(text ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;'); }

  // Proof bookkeeping (induction depth, attempt numbering) is engine detail: the
  // panel reports the verdict, how long it took, and why a failure failed.
  function attemptsHtml(proof) {
    if (!proof?.exists || !proof.attempts.length) return '';
    const failed = proof.attempts.filter((a) => a.passed === false);
    if (proof.passed === true || !failed.length) return '';
    return failed.map((a) => {
      const bits = ['not proven'];
      if (a.unproven !== null) bits.push(`${a.unproven} unproven cells`);
      if (a.excluded.length) bits.push(`excluded ${a.excluded.join(', ')}`);
      return `<span class="attempt fail">${esc(bits.join(' · '))}</span>`;
    }).join('');
  }

  function failureReason(gen) {
    if (gen.correct === false && gen.error) return gen.errorSummary || gen.error;
    const proof = gen.proof;
    if (proof?.passed === false) {
      const last = [...proof.attempts].reverse().find((a) => a.error) || proof.attempts[proof.attempts.length - 1];
      return last?.error || 'proof did not pass';
    }
    if (gen.formalEquivalence === false) return 'evaluator reported formal_equivalence = false';
    return null;
  }

  function evaluationLabel(gen) {
    if (!gen.evaluated) return gen.phase || 'evaluating';
    if (gen.correct === true) return 'Accepted';
    if (gen.correct !== false) return 'pending';
    const error = gen.error || '';
    if (/reports\/synth\.log|synthesis\.log|synthesis\.ys/.test(error)) return 'Synthesis failed';
    if (/reports\/sta_run\.log|sta\.log/.test(error)) return 'Timing analysis failed';
    const stages = {
      contract: 'Input validation failed', experiment_policy: 'Experiment rejected',
      syntax_or_elaboration: 'RTL check failed', formal_inconclusive: 'Equivalence not proven',
      formal_equivalence: 'Equivalence not proven',
      implementation: 'Implementation failed', synthesis: 'Synthesis failed',
      timing_analysis: 'Timing analysis failed',
    };
    return stages[gen.failureStage] || 'Rejected';
  }

  function renderEquivalence(equivalence) {
    const host = $('.eqy', panes.eqy.body);
    if (!equivalence) return;
    const { generations, final, counts } = equivalence;

    let verdict;
    if (final?.proof?.exists) {
      const ok = final.proof.passed === true && final.verified !== false;
      verdict = `
        <div class="verdict">
          <span class="badge ${ok ? 'pass' : 'fail'}">${ok ? 'ACCEPTED' : 'REJECTED'}</span>
          <span class="text">Final candidate${final.generation !== null ? ` from generation ${final.generation}` : ''} ${ok ? 'passed' : 'failed'} fresh equivalence verification
            <small>${esc(final.proof.method || '')}${final.proof.totalSeconds !== null ? ` · ${seconds(final.proof.totalSeconds)}` : ''}</small>
            ${attemptsHtml(final.proof)}
          </span>
        </div>`;
    } else {
      verdict = `
        <div class="verdict">
          <span class="badge wait">PENDING</span>
          <span class="text">Final verification has not run yet
            <small>${counts.evaluated} generation${counts.evaluated === 1 ? '' : 's'} evaluated · ${counts.passed} proven · ${counts.failed} proofs failed · ${counts.rejected || 0} evaluations rejected</small>
          </span>
        </div>`;
    }

    const rows = generations.map((gen) => {
      const proof = gen.proof;
      const failed = proof?.passed === false || gen.formalEquivalence === false;
      const passed = !failed && (proof?.passed === true || gen.formalEquivalence === true);
      const status = !gen.evaluated ? 'wait' : failed ? 'fail' : passed ? 'pass' : 'wait';
      const label = !gen.evaluated ? 'pending' : failed ? 'FAIL' : passed ? 'PASS' : 'not run';
      const reason = gen.correct === false || failed ? failureReason(gen) : null;
      const evalStatus = gen.correct === false ? 'fail' : gen.correct === true ? 'pass' : 'wait';
      const isWinner = final?.generation === gen.generation;
      return `
        <tr class="${isWinner ? 'winner' : ''}">
          <td class="mono n">${gen.generation}</td>
          <td><span class="st ${status}">${label}</span>${attemptsHtml(proof)}</td>
          <td><span class="st ${evalStatus}">${esc(evaluationLabel(gen))}</span>${reason ? `<span class="why"><b>why:</b> ${esc(reason)}</span>` : ''}</td>
          <td class="mono n">${proof?.totalSeconds !== null && proof?.totalSeconds !== undefined ? seconds(proof.totalSeconds) : '—'}</td>
          <td class="mono n">${gen.wns === null ? '—' : signed(gen.wns, 4)}</td>
          <td class="mono n">${gen.tns === null ? '—' : signed(gen.tns, 2)}</td>
          <td class="mono n">${seconds(gen.runtimeSeconds)}</td>
        </tr>`;
    }).join('');

    host.innerHTML = `
      ${verdict}
      ${generations.length ? `
      <table>
        <thead><tr>
          <th class="n">Gen</th><th>Equivalence</th><th>Evaluation</th><th class="n">Proof time</th>
          <th class="n">WNS ns</th><th class="n">TNS ns</th><th class="n">Eval time</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>` : '<div class="empty">No generations evaluated yet.</div>'}`;
    panes.eqy.meta.textContent = `${counts.passed}/${counts.evaluated} proven${counts.failed ? ` · ${counts.failed} proofs failed` : ''}`;
  }

  /* ---- graph loading ----------------------------------------------------- */

  let graphToken = 0;
  async function loadGraph(which) {
    const token = ++graphToken;
    try {
      const response = await fetch(`/api/graph?which=${which}`);
      if (!response.ok) return;
      const graph = window.RtlPathGroups.groupPaths(await response.json(), true);
      if (token !== graphToken) return;
      ensurePanes();
      graphView.setGraph(graph);
      const stats = graph.stats || {};
      const violatingPaths = (graph.meta?.critical_paths || []).filter((path) => Number(path.slack) < 0);
      const instances = violatingPaths.reduce((sum, path) => sum + (path.instances ?? 1), 0);
      panes.graph.meta.textContent =
        `${which}  ${stats.seq_nodes ?? 0} reg  ${stats.comb_nodes ?? 0} comb  ${graph.edges?.length ?? 0} edges  ${violatingPaths.length} path groups · ${instances} instances`;
      $('h3', panes.graph.el).textContent = 'R2R timing graph';
    } catch (error) { reportError(error, 'Timing graph', false); }
  }

  /* ---- state sync -------------------------------------------------------- */

  let lastGraphKey = '';
  let lastReportKey = '';
  let lastEqyKey = '';

  function applyState(state) {
    const first = !ui.run;
    ui.run = state;
    if (state.error) reportError(state.error, 'Run', false);
    for (const generation of state.shinka?.equivalence?.generations || []) {
      if (generation.error) reportError(generation.error, `Generation ${generation.generation}`, false);
    }
    if (state.status !== 'idle') {
      ensurePanes();
      $('#watermark').classList.add('gone');
      setTimeout(() => { const w = $('#watermark'); if (ui.run?.status !== 'idle') w.style.display = 'none'; }, 420);
    }

    const which = state.graph.final ? 'final' : state.graph.initial ? 'initial' : null;
    const key = `${which}:${state.graph.generation}:${state.runDir}`;
    if (which && key !== lastGraphKey) { lastGraphKey = key; loadGraph(which); }

    if (state.report) {
      const reportKey = `${state.runDir}:${state.report.state}:${state.report.metrics.length}`;
      ensurePanes();
      panes.report.el.hidden = false;
      if (reportKey !== lastReportKey) { lastReportKey = reportKey; renderReport(state.report); }
    }

    const equivalence = state.shinka?.equivalence || null;
    renderLive(equivalence);
    if (equivalence) {
      ensurePanes();
      const eqKey = JSON.stringify([state.runDir, equivalence.counts, equivalence.latest, Boolean(equivalence.final)]);
      if (eqKey !== lastEqyKey) { lastEqyKey = eqKey; renderEquivalence(equivalence); }
    }

    if (first && state.budgetUsd) {
      budgetInput.value = state.budgetUsd;
      ui.budget = Number(state.budgetUsd);
      syncBudgetPresets();
    }

    layoutStage();
    syncControls();
    syncElapsedTimer();
    updateFlow();
    syncLogActions();
  }

  /* ---- event stream ------------------------------------------------------ */

  function connect() {
    const source = new EventSource('/api/events');
    source.onmessage = (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch (error) { reportError(error, 'Event stream message', false); return; }
      if (message.t === 'snapshot') {
        buffers.heuristics = message.logs.heuristics.slice();
        buffers.agent = message.logs.agent.slice();
        ui.viz = message.viz;
        for (const [pane, lines] of Object.entries(message.logs)) {
          for (const line of lines) {
            if (line.cls === 'crit' || /\b(?:ERROR|FAILED)\b/.test(line.text || '')) reportError(line.text, `${pane} log`, false);
          }
        }
        if (message.viz?.error) reportError(message.viz.error, 'Visualizer', false);
        applyState(message.state);
        if (panes.logs) paintTerminal(true);
        return;
      }
      if (message.t === 'state') { ui.viz = message.viz || ui.viz; applyState(message.state); return; }
      if (message.t === 'viz') {
        ui.viz = message.viz;
        if (message.viz.state === 'error' && message.viz.error) reportError(message.viz.error, 'Visualizer');
        syncControls();
        return;
      }
      if (message.t === 'log') { ensurePanes(); appendLines(message.pane, message.lines); return; }
      if (message.t === 'reset') {
        buffers.heuristics = []; buffers.agent = [];
        lastGraphKey = ''; lastReportKey = '';
        lastEqyKey = '';
        generationMetrics.clear();
        metricsRequestToken += 1;
        if (panes.report) panes.report.el.hidden = true;
        if (panes.eqy) panes.eqy.el.hidden = true;
        layoutStage();
        renderLive(null);
        if (graphView) graphView.setGraph(null);
        if (panes.logs) paintTerminal(true);
        return;
      }
      if (message.t === 'reset-pane') {
        buffers[message.pane] = [];
        if (panes.logs && panes.logs.tab === message.pane) paintTerminal(true);
      }
    };
    source.onerror = () => { reportError('Connection lost; reconnecting automatically.', 'Event stream', false); };
  }

  /* ---- boot -------------------------------------------------------------- */

  (async function boot() {
    try {
      const data = await api('/api/designs');
      ui.designs = data.designs;
      ui.replayEnabled = data.replayEnabled;
      renderCards();
      syncBudgetPresets();
      syncControls();
      // Mount the system flow on the idle screen too. Once run panes appear,
      // layoutStage keeps this same section at the bottom of the scrollable stage.
      ensurePanes();
      layoutStage();
      updateFlow();
      if (ui.replayEnabled) {
        window.cogitoReplay = (runDir) => api('/api/replay', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ runDir: runDir || 'latest' }),
        }).then(() => toast('Replaying a completed run')).catch((error) => reportError(error));
        toast('Replay enabled: call cogitoReplay() in the console');
      }
    } catch (error) { reportError(error); }
    connect();
  })();

})();
