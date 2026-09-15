'use strict';
/* The pipeline diagram, drawn once and lit one stage at a time.
 *
 * Fixed viewBox, scaled to whatever width the stage leaves: the layout never
 * reflows, only shrinks. Colours come from CSS variables so the light and dark
 * themes are one definition each, not two copies of this file.
 */
(function (global) {

  const NS = 'http://www.w3.org/2000/svg';

  // id, label, x of the box's left edge (the viewBox is 1520 x 300)
  const BOXES = [
    { id: 'initial', label: 'Initial RTL', x: 8, w: 150, tone: 'blue' },
    { id: 'analysis', label: 'Static Analysis', x: 208, w: 232, tone: 'green', tall: true },
    { id: 'orchestrator', label: 'Orchestrator', x: 500, w: 168, tone: 'violet' },
    { id: 'shinka', label: 'ShinkaEvolve', x: 728, w: 196, tone: 'rose' },
    { id: 'evaluation', label: 'Evaluation', x: 984, w: 176, tone: 'amber' },
    { id: 'optimized', label: 'Optimized RTL', x: 1220, w: 192, tone: 'green' },
  ];
  const TOP = 54;
  const H = 132;

  function el(name, attrs, parent) {
    const node = document.createElementNS(NS, name);
    for (const key in attrs) if (attrs[key] !== null && attrs[key] !== undefined) node.setAttribute(key, attrs[key]);
    if (parent) parent.appendChild(node);
    return node;
  }

  function icon(group, kind, cx, cy, tone) {
    const stroke = `var(--flow-${tone}-ink)`;
    const common = { fill: 'none', stroke, 'stroke-width': 1.9, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' };
    if (kind === 'graph') {
      el('circle', { cx: cx - 8, cy, r: 3.4, fill: stroke }, group);
      el('circle', { cx: cx + 7, cy: cy - 8, r: 3.4, fill: stroke }, group);
      el('circle', { cx: cx + 7, cy: cy + 8, r: 3.4, fill: stroke }, group);
      el('path', { d: `M${cx - 5},${cy - 1.4} ${cx + 4},${cy - 7} M${cx - 5},${cy + 1.4} ${cx + 4},${cy + 7}`, ...common }, group);
      return;
    }
    if (kind === 'gear') {
      el('circle', { cx, cy, r: 5.2, ...common }, group);
      for (let i = 0; i < 8; i += 1) {
        const a = (i * Math.PI) / 4;
        el('path', {
          d: `M${(cx + Math.cos(a) * 6.6).toFixed(1)},${(cy + Math.sin(a) * 6.6).toFixed(1)} `
            + `L${(cx + Math.cos(a) * 9.6).toFixed(1)},${(cy + Math.sin(a) * 9.6).toFixed(1)}`,
          ...common, 'stroke-width': 2.6,
        }, group);
      }
      return;
    }
    if (kind === 'brain') {
      el('path', {
        d: `M${cx - 1.5},${cy - 11} c-4,-1 -8,1.5 -8,5.5 c-3,1 -3.5,5 -1,6.8 c-1,3 1.5,6 4.5,6 c1,2 4,2.4 5,0.4 z`
          + `M${cx + 1.5},${cy - 11} c4,-1 8,1.5 8,5.5 c3,1 3.5,5 1,6.8 c1,3 -1.5,6 -4.5,6 c-1,2 -4,2.4 -5,0.4 z`,
        fill: stroke, stroke: 'none',
      }, group);
      return;
    }
    if (kind === 'check') {
      el('circle', { cx, cy, r: 10, ...common, 'stroke-width': 2.4 }, group);
      el('path', { d: `M${cx - 4.6},${cy + 0.4} l3.4,3.6 l6,-7.4`, ...common, 'stroke-width': 2.4 }, group);
      return;
    }
    if (kind === 'doc') {
      el('path', {
        d: `M${cx - 7},${cy - 11} h9 l5,5 v17 h-14 z M${cx + 2},${cy - 11} v5 h5`
          + ` M${cx - 3.5},${cy - 1} h7 M${cx - 3.5},${cy + 3.5} h7 M${cx - 3.5},${cy + 8} h4`,
        ...common,
      }, group);
      return;
    }
    if (kind === 'code' || kind === 'spark') {
      el('path', { d: `M${cx - 7},${cy - 11} h9 l5,5 v17 h-14 z M${cx + 2},${cy - 11} v5 h5`, ...common }, group);
      if (kind === 'code') {
        el('path', { d: `M${cx - 2.5},${cy + 1} l-2.4,2.6 l2.4,2.6 M${cx + 2.5},${cy + 1} l2.4,2.6 l-2.4,2.6`, ...common }, group);
      } else {
        el('path', {
          d: `M${cx + 1},${cy - 1} l1.5,3.6 l3.6,1.5 l-3.6,1.5 l-1.5,3.6 l-1.5,-3.6 l-3.6,-1.5 l3.6,-1.5 z`,
          fill: stroke, stroke: 'none',
        }, group);
      }
      return;
    }
    // every kind is handled above; nothing is drawn for an unknown one
  }

  function box(root, spec) {
    const group = el('g', { class: `flow-box tone-${spec.tone}`, 'data-stage': spec.id }, root);
    el('rect', {
      class: 'flow-shell', x: spec.x, y: TOP, width: spec.w, height: H, rx: 16,
      fill: `var(--flow-${spec.tone}-bg)`, stroke: `var(--flow-${spec.tone}-line)`, 'stroke-width': 2,
    }, group);
    const cx = spec.x + spec.w / 2;
    if (!spec.tall) {
      icon(group, spec.icon, cx, TOP + 48, spec.tone);
      const text = el('text', { class: 'flow-label', x: cx, y: TOP + 96, 'text-anchor': 'middle' }, group);
      text.textContent = spec.label;
    }
    return group;
  }

  function create(host) {
    host.innerHTML = '';
    const svg = el('svg', {
      class: 'flow-svg', viewBox: '0 0 1520 300', preserveAspectRatio: 'xMidYMid meet',
      role: 'img', 'aria-label': 'Pipeline stages',
    }, host);

    const defs = el('defs', {}, svg);
    const marker = el('marker', {
      id: 'flow-arrow', viewBox: '0 0 10 10', refX: 8, refY: 5,
      markerWidth: 8, markerHeight: 8, orient: 'auto-start-reverse',
    }, defs);
    el('path', { d: 'M0,1 L9,5 L0,9 z', fill: 'var(--flow-arrow)' }, marker);
    const feedMarker = el('marker', {
      id: 'flow-arrow-feedback', viewBox: '0 0 10 10', refX: 8, refY: 5,
      markerWidth: 8, markerHeight: 8, orient: 'auto-start-reverse',
    }, defs);
    el('path', { d: 'M0,1 L9,5 L0,9 z', fill: 'var(--flow-rose-ink)' }, feedMarker);

    const edges = el('g', { class: 'flow-edges' }, svg);
    const nodes = el('g', { class: 'flow-nodes' }, svg);

    const specs = BOXES.map((b) => ({ ...b }));
    specs[0].icon = 'code';
    specs[2].icon = 'doc';
    specs[3].icon = 'brain';
    specs[4].icon = 'check';
    specs[5].icon = 'spark';

    // straight connectors between consecutive boxes
    for (let i = 0; i < specs.length - 1; i += 1) {
      const from = specs[i];
      const to = specs[i + 1];
      // Static Analysis has two internal sources; the dedicated merge below
      // provides the only connector into Orchestrator.
      if (from.id === 'analysis') continue;
      const y = TOP + H / 2;
      el('path', {
        class: 'flow-edge', 'data-from': from.id, 'data-to': to.id,
        d: `M${from.x + from.w + 6},${y} H${to.x - 10}`,
        stroke: 'var(--flow-arrow)', 'stroke-width': 2.2, fill: 'none', 'marker-end': 'url(#flow-arrow)',
      }, edges);
    }

    for (const spec of specs) box(nodes, spec);

    // Static Analysis: a titled container holding the two analyses that feed the
    // orchestrator, exactly as the pipeline runs them — concurrently.
    const analysis = nodes.querySelector('[data-stage="analysis"]');
    const title = el('text', { class: 'flow-title', x: 208 + 232 / 2, y: TOP + 26, 'text-anchor': 'middle' }, analysis);
    title.textContent = 'Static Analysis';
    const sub = [
      { id: 'graph', label: 'R2R Graph', icon: 'graph', tone: 'blue', y: TOP + 38 },
      { id: 'heuristics', label: 'RTL Heuristics', icon: 'gear', tone: 'green', y: TOP + 84 },
    ];
    for (const item of sub) {
      const g = el('g', { class: `flow-sub tone-${item.tone}`, 'data-stage': item.id }, analysis);
      el('rect', {
        class: 'flow-shell', x: 226, y: item.y, width: 196, height: 38, rx: 9,
        fill: 'var(--flow-card)', stroke: `var(--flow-${item.tone}-line)`, 'stroke-width': 1.6,
      }, g);
      icon(g, item.icon, 252, item.y + 19, item.tone);
      const text = el('text', { class: 'flow-sublabel', x: 276, y: item.y + 24 }, g);
      text.textContent = item.label;
    }

    // One centered connector leaves Static Analysis for Orchestrator. The
    // internal R2R/heuristics cards remain informational and do not create
    // duplicate outgoing arrows.
    el('path', {
      class: 'flow-edge', 'data-from': 'analysis', 'data-to': 'orchestrator',
      d: `M446,${TOP + H / 2} H${500 - 10}`,
      stroke: 'var(--flow-arrow)', 'stroke-width': 2.2, fill: 'none', 'marker-end': 'url(#flow-arrow)',
    }, edges);

    // Feedback: evaluation returns to ShinkaEvolve.
    const feedback = el('g', { class: 'flow-feedback', 'data-stage': 'feedback' }, svg);
    const loopY = TOP + H + 52;
    el('path', {
      class: 'flow-edge feedback',
      d: `M1072,${TOP + H + 4} V${loopY} H1010`,
      stroke: 'var(--flow-rose-ink)', 'stroke-width': 2.4, fill: 'none',
    }, feedback);
    el('rect', {
      x: 880, y: loopY - 19, width: 130, height: 38, rx: 9,
      fill: 'var(--flow-rose-bg)', stroke: 'var(--flow-rose-ink)', 'stroke-width': 1.8,
    }, feedback);
    const flabel = el('text', { class: 'flow-sublabel feedback', x: 945, y: loopY + 5, 'text-anchor': 'middle' }, feedback);
    flabel.textContent = 'Feedback';
    el('path', {
      class: 'flow-edge feedback',
      d: `M880,${loopY} H826 V${TOP + H + 4}`,
      stroke: 'var(--flow-rose-ink)', 'stroke-width': 2.4, fill: 'none', 'marker-end': 'url(#flow-arrow-feedback)',
    }, feedback);

    let current = null;

    return {
      el: svg,
      // Light exactly one stage (plus its parent, for the two analyses).
      set(stage) {
        svg.classList.toggle('idle', !stage);
        if (stage === current) return;
        current = stage;
        const active = new Set();
        if (stage) {
          active.add(stage);
          if (stage === 'graph' || stage === 'heuristics') active.add('analysis');
          if (stage === 'evaluation') active.add('feedback');
        }
        svg.classList.toggle('has-active', active.size > 0);
        for (const node of svg.querySelectorAll('[data-stage]')) {
          node.classList.toggle('on', active.has(node.dataset.stage));
        }
        for (const edge of svg.querySelectorAll('.flow-edge')) {
          const to = edge.dataset.to;
          edge.classList.toggle('on', Boolean(to && active.has(to)));
        }
      },
      get stage() { return current; },
    };
  }

  global.CogitoFlow = { create };
})(window);
