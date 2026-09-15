'use strict';
/* Interactive timing-graph view over rtl_graph's graph.json. */
(function (global) {

  const SVG_NS = 'http://www.w3.org/2000/svg';
  const COLUMN = 320;
  // Rows used to sit at twice a register's height, which reads as a flat band once
  // the graph is zoomed out. Four times its height is what makes the columns
  // legible without pushing the whole picture off screen.
  const ROW = 118;
  const SEQ = { w: 170, h: 32 };
  const PORT = { w: 120, h: 28 };
  const COMB_R = 15;
  const LAYER_CAP = 14;
  const SUBCOLUMN = 232;

  // Reserved hues: the four categories that must stay recognisable everywhere.
  const HUE = { normal: 175, cdc: 268, fsm: 38, violating: 6 };

  function paint(hue, dark) {
    return dark
      ? { fill: `hsl(${hue} 42% 27%)`, stroke: `hsl(${hue} 62% 63%)`, text: `hsl(${hue} 55% 90%)` }
      : { fill: `hsl(${hue} 56% 87%)`, stroke: `hsl(${hue} 58% 34%)`, text: `hsl(${hue} 62% 20%)` };
  }

  // Distinct hues for critical paths, kept clear of the reserved category hues
  // so a path colour is never confused with CDC, FSM or a violating node.
  function pathHues(count) {
    const reserved = Object.values(HUE);
    const hues = [];
    let hue = 92;
    let guard = 0;
    while (hues.length < count && guard < 4000) {
      guard += 1;
      hue = (hue + 137.508) % 360;
      const clash = reserved.some((r) => Math.min(Math.abs(hue - r), 360 - Math.abs(hue - r)) < 16)
        || hues.some((h) => Math.min(Math.abs(hue - h), 360 - Math.abs(hue - h)) < 14);
      if (!clash) hues.push(Math.round(hue));
    }
    while (hues.length < count) hues.push(Math.round((hues.length * 47 + 92) % 360));
    return hues;
  }

  function el(name, attrs) {
    const node = document.createElementNS(SVG_NS, name);
    for (const key in attrs) if (attrs[key] !== null && attrs[key] !== undefined) node.setAttribute(key, attrs[key]);
    return node;
  }

  function shortLabel(name) {
    const tail = String(name || '').split('.').pop();
    return tail.length > 21 ? `${tail.slice(0, 20)}…` : tail;
  }

  function moduleOwners(node) {
    return node.modules?.length ? node.modules : [{ hier: node.hier || '', module: node.module || '' }];
  }

  function ownerLabel(owner) {
    return `${owner.hier || '(top)'}${owner.module ? ` : ${owner.module}` : ''}`;
  }

  function fmt(value, digits = 3) {
    if (value === null || value === undefined || value === '') return null;
    if (typeof value !== 'number') return String(value);
    return Number.isInteger(value) ? String(value) : value.toFixed(digits);
  }

  /* ---- layering ---------------------------------------------------------- */

  // `aspect` is the height/width of the pane the drawing has to live in. A netlist
  // is far wider than it is tall, so a fixed row pitch leaves a thin flat band with
  // unused space above and below it. Choosing the pitch from the layer count and
  // that aspect fills the pane, which is what makes the picture readable.
  const MIN_PITCH = 48;        // a register pill is 32 tall: this keeps them apart
  function rowPitch(width, rows, aspect) {
    if (!rows || rows < 2 || !Number.isFinite(aspect) || aspect <= 0) return ROW;
    return Math.max(MIN_PITCH, Math.min(ROW * 4.5, (width * aspect) / (rows - 1)));
  }

  function layout(nodes, edges, aspect = 0.42) {
    const index = new Map(nodes.map((node, i) => [node.id, i]));
    const succ = nodes.map(() => []);
    const pred = nodes.map(() => []);
    const links = [];
    for (const edge of edges) {
      const s = index.get(edge.src);
      const t = index.get(edge.dst);
      if (s === undefined || t === undefined || s === t) continue;
      links.push({ s, t, edge });
    }

    // Break cycles with an iterative DFS so combinational loops and feedback
    // registers cannot stall the longest-path layering below.
    const colour = new Uint8Array(nodes.length);
    const back = new Set();
    const adjacency = nodes.map(() => []);
    links.forEach((link, i) => adjacency[link.s].push(i));
    for (let root = 0; root < nodes.length; root += 1) {
      if (colour[root]) continue;
      const stack = [{ node: root, cursor: 0 }];
      colour[root] = 1;
      while (stack.length) {
        const frame = stack[stack.length - 1];
        const list = adjacency[frame.node];
        if (frame.cursor >= list.length) { colour[frame.node] = 2; stack.pop(); continue; }
        const linkId = list[frame.cursor++];
        const next = links[linkId].t;
        if (colour[next] === 1) back.add(linkId);
        else if (colour[next] === 0) { colour[next] = 1; stack.push({ node: next, cursor: 0 }); }
      }
    }

    const forward = links.filter((_, i) => !back.has(i));
    for (const link of forward) { succ[link.s].push(link.t); pred[link.t].push(link.s); }

    const indegree = nodes.map((_, i) => pred[i].length);
    const layer = new Int32Array(nodes.length);
    const queue = [];
    for (let i = 0; i < nodes.length; i += 1) if (!indegree[i]) queue.push(i);
    for (let head = 0; head < queue.length; head += 1) {
      const node = queue[head];
      for (const next of succ[node]) {
        if (layer[next] < layer[node] + 1) layer[next] = layer[node] + 1;
        if (--indegree[next] === 0) queue.push(next);
      }
    }

    const depth = Math.max(0, ...layer) + 1;
    const layers = Array.from({ length: depth }, () => []);
    nodes.forEach((_, i) => layers[layer[i]].push(i));

    // Barycentre ordering: a handful of sweeps is enough to make the edge
    // bundles readable without a full crossing-minimisation pass.
    const position = new Int32Array(nodes.length);
    const reindex = () => layers.forEach((list) => list.forEach((node, i) => { position[node] = i; }));
    reindex();
    const median = (list) => {
      if (!list.length) return -1;
      const sorted = list.map((n) => position[n]).sort((a, b) => a - b);
      return sorted[(sorted.length - 1) >> 1];
    };
    for (let sweep = 0; sweep < 8; sweep += 1) {
      const down = sweep % 2 === 0;
      const order = down ? [...layers.keys()] : [...layers.keys()].reverse();
      for (const li of order) {
        const neighbours = down ? pred : succ;
        layers[li] = layers[li]
          .map((node, i) => ({ node, key: median(neighbours[node]), i }))
          .sort((a, b) => (a.key < 0 || b.key < 0 ? a.i - b.i : a.key - b.key || a.i - b.i))
          .map((item) => item.node);
        reindex();
      }
    }

    // A netlist layer can hold every register in the design. Left unwrapped the
    // graph becomes one 8000px column, so tall layers are split into
    // sub-columns; the left-to-right layer order is preserved.
    const shape = layers.map((list) => {
      const chunks = Math.max(1, Math.ceil(list.length / LAYER_CAP));
      return { chunks, per: Math.ceil(list.length / chunks) };
    });
    const width = shape.reduce((sum, s) => sum + (s.chunks - 1) * SUBCOLUMN + COLUMN, 0);
    const tallest = Math.max(1, ...shape.map((s) => s.per));
    const pitch = rowPitch(width, tallest, aspect);

    const points = nodes.map(() => ({ x: 0, y: 0 }));
    let cursor = 0;
    layers.forEach((list, li) => {
      const { chunks, per } = shape[li];
      for (let c = 0; c < chunks; c += 1) {
        const slice = list.slice(c * per, (c + 1) * per);
        const offset = -((slice.length - 1) * pitch) / 2;
        slice.forEach((node, i) => { points[node] = { x: cursor + c * SUBCOLUMN, y: offset + i * pitch }; });
      }
      cursor += (chunks - 1) * SUBCOLUMN + COLUMN;
    });

    return { index, points, links, layerOf: layer, pitch };
  }

  /* ---- view -------------------------------------------------------------- */

  function create(container, options = {}) {
    let graph = null;
    let model = null;
    let shown = [];            // the nodes currently laid out (all, or critical only)
    let shownEdges = [];
    let onlyCritical = false;
    let chains = null;         // path index -> reconstructed chain, built on demand
    let orderedPaths = null;
    let adjacent = null;       // src id -> dst ids, for walking between waypoints
    let kinds = null;          // id -> kind, so a walk never crosses a register
    let segmentCache = null;   // path index -> node id -> its segment on that path
    let focus = -1;
    let selected = null;
    let view = { x: 0, y: 0, k: 1 };
    let userMoved = false;
    let bounds = { x: 0, y: 0, w: 1, h: 1 };

    container.innerHTML = '';
    const wrap = document.createElement('div');
    wrap.className = 'graph-wrap';
    container.appendChild(wrap);

    const svg = el('svg', { xmlns: SVG_NS });
    const viewport = el('g', {});
    const edgeLayer = el('g', { class: 'edges' });
    const nodeLayer = el('g', { class: 'nodes' });
    viewport.appendChild(edgeLayer);
    viewport.appendChild(nodeLayer);
    svg.appendChild(viewport);
    wrap.appendChild(svg);

    const empty = document.createElement('div');
    empty.className = 'graph-empty';
    empty.textContent = 'Waiting for the graph stage…';
    wrap.appendChild(empty);

    const toolbar = document.createElement('div');
    toolbar.className = 'graph-toolbar';
    toolbar.innerHTML = `
      <div class="tool-group">
        <button class="icon-btn" data-act="zoom-out" title="Zoom out"><svg><use href="#i-minus"/></svg></button>
        <button class="icon-btn" data-act="fit" title="Fit to view"><svg><use href="#i-fit"/></svg></button>
        <button class="icon-btn" data-act="zoom-in" title="Zoom in"><svg><use href="#i-plus"/></svg></button>
      </div>
      <div class="tool-group">
        <button class="icon-btn" data-act="path-prev" title="Previous critical path"><svg><use href="#i-prev"/></svg></button>
        <span class="path-label" data-role="path-label">no path focus</span>
        <button class="icon-btn" data-act="path-next" title="Next critical path"><svg><use href="#i-next"/></svg></button>
        <button class="icon-btn" data-act="path-clear" title="Clear focus"><svg><use href="#i-close"/></svg></button>
      </div>
      <div class="tool-group">
        <button class="icon-btn wide" data-act="only-critical" aria-pressed="false"
                title="Show only the critical paths"><span data-role="only-label">Critical only</span></button>
      </div>`;
    wrap.appendChild(toolbar);
    const pathLabel = toolbar.querySelector('[data-role="path-label"]');

    const legend = document.createElement('div');
    legend.className = 'graph-legend';
    wrap.appendChild(legend);

    let info = null;

    /* -- transform -- */

    function apply() {
      viewport.setAttribute('transform', `translate(${view.x} ${view.y}) scale(${view.k})`);
    }

    function fit(box = bounds, padding = 46, animate = true) {
      const rect = wrap.getBoundingClientRect();
      if (!rect.width || !rect.height || !box.w || !box.h) return;
      const k = Math.min((rect.width - padding * 2) / box.w, (rect.height - padding * 2) / box.h, 3);
      const target = {
        k: Math.max(k, 0.04),
        x: rect.width / 2 - (box.x + box.w / 2) * Math.max(k, 0.04),
        y: rect.height / 2 - (box.y + box.h / 2) * Math.max(k, 0.04),
      };
      if (!animate) { view = target; apply(); return; }
      const from = { ...view };
      const start = performance.now();
      const step = (now) => {
        const t = Math.min(1, (now - start) / 420);
        const e = 1 - Math.pow(1 - t, 3);
        view = { x: from.x + (target.x - from.x) * e, y: from.y + (target.y - from.y) * e, k: from.k + (target.k - from.k) * e };
        apply();
        if (t < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    }

    function zoomBy(factor, cx, cy) {
      const rect = wrap.getBoundingClientRect();
      const px = cx ?? rect.width / 2;
      const py = cy ?? rect.height / 2;
      const k = Math.min(4, Math.max(0.03, view.k * factor));
      view = { k, x: px - ((px - view.x) / view.k) * k, y: py - ((py - view.y) / view.k) * k };
      apply();
    }

    svg.addEventListener('wheel', (event) => {
      event.preventDefault();
      const rect = wrap.getBoundingClientRect();
      userMoved = true;
      zoomBy(Math.exp(-event.deltaY * 0.0015), event.clientX - rect.left, event.clientY - rect.top);
    }, { passive: false });

    let dragging = null;
    let pressedNode = null;
    let suppressNodeClick = false;
    svg.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      pressedNode = event.target.closest?.('.node') || null;
      dragging = { x: event.clientX, y: event.clientY, vx: view.x, vy: view.y, moved: false };
      svg.setPointerCapture(event.pointerId);
      svg.classList.add('panning');
    });
    svg.addEventListener('pointermove', (event) => {
      if (!dragging) return;
      const dx = event.clientX - dragging.x;
      const dy = event.clientY - dragging.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) { dragging.moved = true; userMoved = true; }
      view.x = dragging.vx + dx;
      view.y = dragging.vy + dy;
      apply();
    });
    const endDrag = (event) => {
      if (!dragging) return;
      const moved = dragging.moved;
      const node = pressedNode;
      dragging = null;
      pressedNode = null;
      svg.classList.remove('panning');
      try { svg.releasePointerCapture(event.pointerId); } catch { /* pointer already gone */ }
      if (!moved && node) {
        suppressNodeClick = true;
        select(selected === node.__index ? null : node.__index);
      } else if (!moved && event.target === svg) { select(null); setFocus(-1); }
    };
    svg.addEventListener('pointerup', endDrag);
    svg.addEventListener('pointercancel', endDrag);

    toolbar.addEventListener('click', (event) => {
      const button = event.target.closest('[data-act]');
      if (!button) return;
      const act = button.dataset.act;
      if (act === 'zoom-in') zoomBy(1.28);
      if (act === 'zoom-out') zoomBy(1 / 1.28);
      if (act === 'fit') { userMoved = false; setFocus(-1); fit(); }
      if (act === 'path-next') stepPath(1);
      if (act === 'path-prev') stepPath(-1);
      if (act === 'path-clear') setFocus(-1);
      if (act === 'only-critical') setOnlyCritical(!onlyCritical);
    });

    /* -- what counts as critical -- */

    // rtl_graph reports the worst path of every clock group, most of which meet
    // timing. Only the ones that actually violate belong in the path selector.
    function pathOrder() {
      if (orderedPaths) return orderedPaths;
      const paths = graph?.meta?.critical_paths || [];
      orderedPaths = paths
        .map((path, index) => ({ index, slack: Number(path.slack) }))
        .filter((entry) => Number.isFinite(entry.slack) && entry.slack < 0)
        .sort((a, b) => a.slack - b.slack)
        .map((entry) => entry.index);
      return orderedPaths;
    }

    // `meta.critical_paths[].nodes` lists only the points OpenSTA attributed on the
    // path, so consecutive entries are usually several hops apart in the netlist
    // (full -> C337 -> C335 -> C320 arrives as full -> C320). Treating that list as
    // the path leaves the launch register with no edge to draw, so the hops in
    // between are recovered from the edge list here.
    const HOP_LIMIT = 12;
    const key = (src, dst) => `${src} >> ${dst}`;

    function outgoing() {
      if (adjacent) return adjacent;
      adjacent = new Map();
      kinds = new Map((graph?.nodes || []).map((node) => [node.id, node.kind]));
      for (const edge of graph?.edges || []) {
        const list = adjacent.get(edge.src);
        if (list) list.push(edge.dst);
        else adjacent.set(edge.src, [edge.dst]);
      }
      return adjacent;
    }

    // Shortest hop route between two attributed points. The exact gate-level route
    // is not recorded anywhere, so the fewest-hops one stands in for it — but a
    // timing path runs launch register -> combinational cone -> capture register and
    // can never pass *through* a register or a port, so only combinational nodes are
    // legal intermediate hops. Without that rule a dense netlist offers shorter
    // routes straight through sequential elements, which are not timing paths at all.
    function route(from, to) {
      if (from === to) return [from];
      const next = outgoing();
      const previous = new Map([[from, null]]);
      const depth = new Map([[from, 0]]);
      const queue = [from];
      for (let head = 0; head < queue.length; head += 1) {
        const node = queue[head];
        if (depth.get(node) >= HOP_LIMIT) continue;
        for (const child of next.get(node) || []) {
          if (previous.has(child)) continue;
          if (child !== to && kinds.get(child) !== 'comb') continue;
          previous.set(child, node);
          depth.set(child, depth.get(node) + 1);
          if (child === to) {
            const hops = [];
            for (let at = to; at !== null && at !== undefined; at = previous.get(at)) hops.unshift(at);
            return hops;
          }
          queue.push(child);
        }
      }
      return null;
    }

    function chainOf(index) {
      if (!chains) chains = new Map();
      const cached = chains.get(index);
      if (cached) return cached;
      const path = graph?.meta?.critical_paths?.[index];
      if (path?.nodes_exact === false) {
        const cone = window.RtlPathGroups.approximateChain(graph, path);
        chains.set(index, cone);
        return cone;
      }
      const waypoints = graph?.meta?.critical_paths?.[index]?.nodes || [];
      const chain = { nodes: new Set(waypoints), edges: new Set(), waypoints };
      for (let i = 0; i + 1 < waypoints.length; i += 1) {
        const hops = route(waypoints[i], waypoints[i + 1]);
        if (!hops) {
          // Leave disconnected waypoints disconnected; never invent an edge.
          continue;
        }
        for (const id of hops) chain.nodes.add(id);
        for (let h = 0; h + 1 < hops.length; h += 1) chain.edges.add(key(hops[h], hops[h + 1]));
      }
      chains.set(index, chain);
      return chain;
    }

    function criticalIds() {
      const ids = new Set();
      const selectable = new Set(pathOrder());
      for (const index of selectable) {
        for (const id of chainOf(index).nodes) ids.add(id);
      }
      for (const node of graph.nodes || []) {
        const index = nodePathIndex(node);
        if (index !== null && selectable.has(index)) ids.add(node.id);
      }
      return ids;
    }

    // Isolating the critical paths re-lays out just those nodes, so the picture
    // grows into the space the rest of the design was using.
    function paintOnlyButton(button) {
      const any = Boolean(graph && pathOrder().length);
      button.disabled = !any;
      button.setAttribute('aria-pressed', String(onlyCritical && any));
      button.querySelector('[data-role="only-label"]').textContent = onlyCritical && any ? 'Show all' : 'Critical only';
      button.title = onlyCritical && any
        ? 'Show every node in the design' : 'Show only the paths that violate timing';
    }

    function setOnlyCritical(next) {
      if (!graph) return;
      onlyCritical = next;
      paintOnlyButton(toolbar.querySelector('[data-act="only-critical"]'));
      const keepFocus = focus;
      buildModel();
      render();
      focus = keepFocus >= 0 && pathOrder().includes(keepFocus) ? keepFocus : -1;
      applyFocus();
      userMoved = false;
      fit(bounds, 46, true);
    }

    function paneAspect() {
      const rect = wrap.getBoundingClientRect();
      if (!rect.width || !rect.height) return 0.42;
      return Math.max(0.16, Math.min(1.1, rect.height / rect.width));
    }

    function buildModel() {
      const all = graph.nodes || [];
      const edges = (graph.edges || []).filter(edge => !edge.implied && edge.style !== 'dotted');
      if (onlyCritical) {
        const ids = criticalIds();
        shown = all.filter((node) => ids.has(node.id));
        shownEdges = edges.filter((edge) => ids.has(edge.src) && ids.has(edge.dst));
        if (!shown.length) { shown = all; shownEdges = edges; }
      } else {
        shown = all;
        shownEdges = edges;
      }
      model = layout(shown, shownEdges, paneAspect());
    }

    /* -- rendering -- */

    function dark() { return document.documentElement.getAttribute('data-theme') === 'dark'; }

    // Membership comes from meta.critical_paths, which every build writes. Node
    // fields differ by build: current rtl_graph writes `critical_path` (1-based,
    // 0 = none), older ones wrote a `critical_paths` array of indices.
    function nodePathIndex(node) {
      if (typeof node.critical_path === 'number') return node.critical_path > 0 ? node.critical_path - 1 : null;
      const list = node.critical_paths;
      return Array.isArray(list) && list.length ? list[0] : null;
    }

    // Attributed points win over recovered hops, so a node that genuinely belongs to
    // one path is never coloured as an inferred hop of another.
    function pathMembership(nodes) {
      const membership = new Map();
      const order = pathOrder();
      const selectable = new Set(order);
      for (const index of order) {
        for (const id of chainOf(index).waypoints) if (!membership.has(id)) membership.set(id, index);
      }
      for (const node of nodes) {
        const index = nodePathIndex(node);
        if (index !== null && selectable.has(index) && !membership.has(node.id)) membership.set(node.id, index);
      }
      for (const index of order) {
        for (const id of chainOf(index).nodes) if (!membership.has(id)) membership.set(id, index);
      }
      return membership;
    }

    function classify(node, hueOf, membership) {
      const labels = new Set(node.labels || []);
      const index = membership.get(node.id);
      if (index !== undefined) return { cat: 'path', hue: hueOf.get(index), path: index };
      if (labels.has('CDC') || node.cdc) return { cat: 'cdc', hue: HUE.cdc };
      if (node.fsm || node.fsm_of) return { cat: 'fsm', hue: HUE.fsm };
      if (node.kind === 'comb' && (labels.has('violating') || node.violated)) return { cat: 'violating', hue: HUE.violating };
      return { cat: 'normal', hue: HUE.normal };
    }

    function render() {
      edgeLayer.textContent = '';
      nodeLayer.textContent = '';
      if (!graph) { empty.hidden = false; return; }
      empty.hidden = true;

      const isDark = dark();
      const nodes = shown;
      const order = pathOrder();
      const hues = pathHues(Math.max(order.length, 1));
      const hueOf = new Map(order.map((index, rank) => [index, hues[rank % hues.length]]));
      const membership = pathMembership(nodes);
      const colours = nodes.map((node) => {
        const kind = classify(node, hueOf, membership);
        return { ...kind, ...paint(kind.hue, isDark) };
      });

      const { index, points, links } = model;
      const seen = new Set();

      for (const link of links) {
        const a = points[link.s];
        const b = points[link.t];
        const sourceNode = nodes[link.s];
        const size = (node) => (node.kind === 'comb' ? COMB_R : (node.kind?.startsWith('port') ? PORT.w / 2 : SEQ.w / 2));
        const x1 = a.x + size(nodes[link.s]);
        const x2 = b.x - size(nodes[link.t]);
        const mid = Math.max(40, Math.abs(x2 - x1) * 0.5);
        let colour = colours[link.s];
        const sourcePath = colours[link.s].path;
        if (sourcePath !== undefined && sourcePath === colours[link.t].path) colour = colours[link.t];
        const isCdc = link.edge.cdc;
        const path = el('path', {
          class: 'edge',
          d: `M${x1.toFixed(1)},${a.y.toFixed(1)} C${(x1 + mid).toFixed(1)},${a.y.toFixed(1)} ${(x2 - mid).toFixed(1)},${b.y.toFixed(1)} ${x2.toFixed(1)},${b.y.toFixed(1)}`,
          stroke: isCdc ? paint(HUE.cdc, isDark).stroke : colour.stroke,
          'stroke-width': colour.cat === 'path' || colour.cat === 'violating' ? 1.5 : 1,
          'stroke-opacity': colour.cat === 'normal' ? 0.42 : 0.8,
          'stroke-dasharray': null,
        });
        path.__link = link;
        edgeLayer.appendChild(path);
        seen.add(sourceNode.id);
      }

      nodes.forEach((node, i) => {
        const point = points[i];
        const colour = colours[i];
        const group = el('g', { class: `node kind-${node.kind || 'unknown'}`, transform: `translate(${point.x.toFixed(1)} ${point.y.toFixed(1)})` });
        group.__node = node;
        group.__index = i;

        let shape;
        if (node.kind === 'comb') {
          shape = el('circle', { class: 'shape', r: COMB_R, cx: 0, cy: 0 });
        } else if (node.kind === 'port_in' || node.kind === 'port_out') {
          shape = el('rect', { class: 'shape', x: -PORT.w / 2, y: -PORT.h / 2, width: PORT.w, height: PORT.h, rx: 4, 'stroke-dasharray': '4 3' });
        } else {
          shape = el('rect', { class: 'shape', x: -SEQ.w / 2, y: -SEQ.h / 2, width: SEQ.w, height: SEQ.h, rx: SEQ.h / 2 });
        }
        shape.setAttribute('fill', colour.fill);
        shape.setAttribute('stroke', colour.stroke);
        shape.setAttribute('stroke-width', colour.cat === 'normal' ? 1.2 : 1.8);
        group.appendChild(shape);

        if (node.kind !== 'comb') {
          const text = el('text', { class: 'node-label', x: 0, y: 3.8, 'text-anchor': 'middle', fill: colour.text });
          text.textContent = shortLabel(node.name || node.id);
          group.appendChild(text);
        }

        const title = el('title', {});
        // Hover is a lightweight affordance; the full timing details belong to
        // the clicked, expanded node.
        title.textContent = node.name || node.id;
        group.__title = title;
        group.appendChild(title);

        group.addEventListener('click', (event) => {
          event.stopPropagation();
          if (suppressNodeClick) { suppressNodeClick = false; return; }
          select(selected === i ? null : i);
        });
        nodeLayer.appendChild(group);
      });

      const xs = points.map((p) => p.x);
      const ys = points.map((p) => p.y);
      const pad = (model.pitch || ROW) / 2;
      bounds = {
        x: Math.min(...xs) - SEQ.w, y: Math.min(...ys) - pad,
        w: (Math.max(...xs) - Math.min(...xs)) + SEQ.w * 2,
        h: (Math.max(...ys) - Math.min(...ys)) + pad * 2,
      };

      buildLegend(colours, order.length, isDark);
      applyFocus();
    }

    function buildLegend(colours, pathCount, isDark) {
      const present = new Set(colours.map((c) => c.cat));
      const rows = [];
      const swatch = (hue, label) => {
        const c = paint(hue, isDark);
        return `<span class="row"><i style="background:${c.fill};border-color:${c.stroke}"></i>${label}</span>`;
      };
      if (present.has('cdc')) rows.push(swatch(HUE.cdc, 'CDC'));
      if (present.has('fsm')) rows.push(swatch(HUE.fsm, 'FSM register'));
      if (present.has('violating')) rows.push(swatch(HUE.violating, 'Negative slack'));
      if (present.has('path')) rows.push(`<span class="row"><i style="background:linear-gradient(90deg,hsl(92 56% 70%),hsl(220 56% 70%),hsl(320 56% 70%));border-color:var(--line-strong)"></i>Critical paths (${pathCount})</span>`);
      if (present.has('normal')) rows.push(swatch(HUE.normal, 'Off critical path'));
      rows.push('<span class="row"><i class="port-swatch"></i>I/O port</span>');
      rows.push('<span class="row" style="color:var(--faint)">Pill = register &middot; circle = combinational</span>');
      if (onlyCritical) rows.push('<span class="row" style="color:var(--faint)">Showing the violating paths only</span>');
      legend.innerHTML = rows.join('');
    }

    /* -- focus & selection -- */

    function pathNodeIds(i) {
      return chainOf(i).nodes;
    }

    function chainPathOf(id) {
      for (const index of pathOrder()) if (chainOf(index).nodes.has(id)) return index;
      return null;
    }

    // Show path-specific increments only for combinational logic. A register's
    // segment may describe launch clock-to-Q or a zero-length capture point;
    // it is not a consistently defined per-register delay measurement.
    function nodeTitle(node) {
      const hidden = node.kind === 'comb' && (node.delay_exact === false || node.through_exact === false);
      const delay = hidden ? null : node.delay ?? (node.kind === 'comb' ? node.through_delay : null);
      const segment = focus >= 0 ? segmentsOf(focus).get(node.id) : null;
      const span = focus >= 0 && !segment ? gapHolderFor(focus, node.id) : null;
      const merged = segment && (segment.gap || (segment.merged && segment.merged.length));
      const delayLine = node.kind !== 'comb' || hidden || merged || span ? '' : segment && segment.through != null
        ? `\ndelay on this path ${Number(segment.through).toFixed(4)} ns`
          : delay != null ? `\ndelay ${Number(delay).toFixed(4)} ns`
            : '';
      const ownership = node.kind === 'comb' ? `\nModules:\n${moduleOwners(node).map(ownerLabel).join('\n')}` : '';
      return `${node.name || node.id}\n${node.kind}${ownership}`
        + `${node.worst_slack != null ? `\nworst slack ${node.worst_slack.toFixed(4)} ns` : ''}`
        + delayLine;
    }

    // Per (node, path) timing, for reading the path currently on screen.
    function segmentsOf(index) {
      if (!segmentCache) segmentCache = new Map();
      const cached = segmentCache.get(index);
      if (cached) return cached;
      const map = new Map();
      for (const segment of graph?.meta?.critical_paths?.[index]?.segments || []) {
        if (segment && segment.node) map.set(segment.node, segment);
      }
      segmentCache.set(index, map);
      return map;
    }

    // The node that carries a gap segment stands in for the hops inside it, so the
    // nodes the walk recovered there are labelled rather than left blank.
    function gapHolderFor(index, id) {
      const segments = segmentsOf(index);
      if (segments.has(id)) return null;
      const chain = chainOf(index);
      if (!chain.nodes.has(id)) return null;
      for (const [node, segment] of segments) {
        if (segment.gap || (segment.merged && segment.merged.length)) return { node, segment };
      }
      return null;
    }

    function pathRank(index) {
      const order = pathOrder();
      const rank = order.indexOf(index);
      return { rank, total: order.length };
    }

    function applyFocus() {
      const active = focus >= 0;
      wrap.classList.toggle('focusing', active);
      const chain = active ? chainOf(focus) : null;
      const ids = chain ? chain.nodes : null;
      for (const group of nodeLayer.children) {
        const on = active && ids.has(group.__node.id);
        group.classList.toggle('on', on);
        if (on) group.querySelector('.shape').setAttribute('stroke-width', '3.4');
        else group.querySelector('.shape').setAttribute('stroke-width', group.__node.labels?.length ? '1.8' : '1.2');
      }
      for (const edge of edgeLayer.children) {
        const link = edge.__link;
        // Only the hops the path actually runs through, not every edge that happens
        // to join two of its nodes.
        const on = active && chain.edges.has(key(shown[link.s].id, shown[link.t].id));
        edge.classList.toggle('on', on);
        edge.setAttribute('stroke-width', on ? '3' : (edge.getAttribute('stroke-dasharray') ? '1.4' : '1.2'));
      }
      if (info && selected !== null && selected !== undefined) renderInfo(selected);
      const { rank, total } = active ? pathRank(focus) : { rank: -1, total: pathOrder().length };
      if (!active) {
        const instances = pathOrder().reduce((sum, index) => sum + (graph.meta.critical_paths[index].instances ?? 1), 0);
        pathLabel.textContent = total ? `${total} critical path groups · ${instances} instances` : 'timing met on every path';
      } else {
        const path = graph.meta.critical_paths[focus];
        pathLabel.textContent = `path ${rank + 1}/${total} · instances ${path.instances ?? 1} · slack ${Number(path.slack).toFixed(4)} ns${path.nodes_exact === false ? ' · approximate cone' : ''}`;
      }
    }

    function setFocus(i) {
      const order = pathOrder();
      focus = order.includes(i) ? i : -1;
      applyFocus();
      if (focus >= 0) {
        const ids = pathNodeIds(focus);
        const pts = shown.map((n, k) => (ids.has(n.id) ? model.points[k] : null)).filter(Boolean);
        if (pts.length) {
          const xs = pts.map((p) => p.x);
          const ys = pts.map((p) => p.y);
          const pitch = model.pitch || ROW;
          fit({
            x: Math.min(...xs) - SEQ.w, y: Math.min(...ys) - pitch * 1.2,
            w: Math.max(Math.max(...xs) - Math.min(...xs) + SEQ.w * 2, COLUMN),
            h: Math.max(Math.max(...ys) - Math.min(...ys) + pitch * 2.4, pitch * 4),
          }, 60);
        }
      }
    }

    // Steps through the violating paths in slack order, worst first.
    function stepPath(direction) {
      const order = pathOrder();
      if (!order.length) return;
      const at = order.indexOf(focus);
      const next = at === -1 ? (direction > 0 ? 0 : order.length - 1)
        : ((at + direction) % order.length + order.length) % order.length;
      setFocus(order[next]);
    }

    function select(i) {
      if (info) { info.remove(); info = null; }
      for (const group of nodeLayer.children) group.classList.remove('selected');
      selected = i;
      for (const node of nodeLayer.children) {
        const label = node.querySelector('.node-label');
        if (label) {
          label.style.transform = '';
        }
      }
      if (i === null || i === undefined) return;
      const group = nodeLayer.children[i];
      if (group) group.classList.add('selected');
      for (const node of nodeLayer.children) {
        const label = node.querySelector('.node-label');
        if (label) {
          const enlarged = node.classList.contains('selected');
          label.style.transform = enlarged ? 'scale(2.2)' : '';
        }
      }
      renderInfo(i);
      recentre(i);
    }

    function recentre(i) {
      const point = model.points[i];
      if (!point) return;
      const rect = wrap.getBoundingClientRect();
      view.x = rect.width / 2 - point.x * view.k;
      view.y = rect.height / 2 - point.y * view.k;
      apply();
    }

    // Split out of select() so a focus change can refresh an open panel in place,
    // without yanking the view back to the selected node.
    function renderInfo(i) {
      if (info) { info.remove(); info = null; }
      const node = shown[i];
      if (!node) return;

      // rtl_graph's current node shape: comb nodes carry `delay` and `module`,
      // registers split across violating bits carry `split_of` and `bit_range`.
      const bits = Array.isArray(node.bit_range)
        ? (node.bit_range[0] === node.bit_range[1] ? `bit ${node.bit_range[0]}`
          : `bits ${node.bit_range[0]}:${node.bit_range[1]}`)
        : null;
      // A node can sit on a drawn path without STA having attributed it: those hops
      // are recovered from the netlist, so label them rather than implying a measure.
      const direct = nodePathIndex(node);
      const pathIndex = direct === null ? chainPathOf(node.id) : direct;
      const inferred = direct === null && pathIndex !== null;
      const { rank } = pathIndex === null ? { rank: -1 } : pathRank(pathIndex);
      const hidden = node.kind === 'comb' && (node.delay_exact === false || node.through_exact === false);
      const delay = hidden ? null : node.delay ?? (node.kind === 'comb' ? node.through_delay : null);

      // On a focused path the additive number is the segment's, not the node's.
      const onPath = focus >= 0 ? focus : pathIndex;
      const segment = onPath === null || onPath < 0 ? null : segmentsOf(onPath).get(node.id);
      const gap = onPath === null || onPath < 0 ? null : gapHolderFor(onPath, node.id);
      const pathRankOf = onPath === null || onPath < 0 ? -1 : pathRank(onPath).rank;
      const merged = segment && (segment.gap || segment.merged?.length);
      const pathDelay = node.kind === 'comb' && !hidden && !merged && !gap && segment && segment.through != null
        ? fmt(segment.through, 4) : null;
      const owners = moduleOwners(node);
      const owner = owners[0] || {};
      const width = bits ? `${bits} of ${node.declared_width ?? node.width} bits` : node.width == null ? null : `${node.width} bits`;
      const endpointDelay = hidden || merged || gap ? null : (pathDelay ?? (delay != null ? fmt(delay, 4) : fmt(node.worst_delay, 4)));
      const rows = [
        ['kind', node.kind],
        ['width', width],
        ...(node.kind === 'comb' ? [
          ['module', owner.module || null],
          ['hierarchy', owner.hier || null],
          ['endpoint path delay', endpointDelay == null ? null : `${endpointDelay} ns`],
          ['fanout', node.fanout == null ? null : `${fmt(node.fanout, 0)} loads`],
        ] : [
          ['hierarchy', owner.hier || null],
          ['worst slack', node.worst_slack == null ? null : `${fmt(node.worst_slack, 4)} ns`],
          ['fanout', node.fanout == null ? null : `${fmt(node.fanout, 0)} loads`],
          ['clocks', (node.clocks || []).join(', ') || null],
        ]),
      ].filter(([, value]) => value !== null && value !== undefined && value !== '');

      info = document.createElement('div');
      info.className = 'node-info';
      info.innerHTML = `
        <button class="icon-btn close" title="Close"><svg><use href="#i-close"/></svg></button>
        <h4>${(node.name || node.id).replace(/</g, '&lt;')}</h4>
        <div class="kind">${node.kind === 'comb' ? 'combinational cone' : node.kind === 'seq' ? 'register' : 'port'}</div>
        <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${String(v).replace(/</g, '&lt;')}</dd>`).join('')}</dl>`;
      info.querySelector('.close').addEventListener('click', () => select(null));
      wrap.appendChild(info);
    }

    /* -- api -- */

    let fitted = false;
    let lastSize = { w: 0, h: 0 };
    // Refit while the user has not taken over the view: the pane changes size
    // when other panes appear, collapse or go full screen.
    const sizeObserver = new ResizeObserver(() => {
      if (!graph) return;
      const rect = wrap.getBoundingClientRect();
      if (rect.width < 40 || rect.height < 40) return;
      const grew = Math.abs(rect.width - lastSize.w) > 8 || Math.abs(rect.height - lastSize.h) > 8;
      lastSize = { w: rect.width, h: rect.height };
      if (!fitted || (!userMoved && grew)) { fitted = true; fit(bounds, 46, false); }
    });
    sizeObserver.observe(wrap);

    function setGraph(next) {
      graph = window.RtlPathGroups.groupPaths(next, true);
      orderedPaths = null;
      focus = -1;
      selected = null;
      chains = null;
      adjacent = null;
      kinds = null;
      segmentCache = null;
      if (info) { info.remove(); info = null; }
      const button = toolbar.querySelector('[data-act="only-critical"]');
      if (!graph || !graph.nodes?.length) {
        onlyCritical = false;
        model = null; shown = []; shownEdges = []; fitted = false;
        paintOnlyButton(button);
        render();
        return;
      }
      onlyCritical = pathOrder().length > 0;
      paintOnlyButton(button);
      buildModel();
      render();
      fitted = false;
      userMoved = false;
      const rect = wrap.getBoundingClientRect();
      if (rect.width > 40 && rect.height > 40) { fitted = true; fit(bounds, 46, false); }
    }

    const themeObserver = new MutationObserver(() => { if (graph) { const keep = { ...view }; render(); view = keep; apply(); } });
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

    return {
      setGraph,
      fit: () => { userMoved = false; fitted = true; fit(); },
      relayout: () => {
        if (!graph || fitted) return;
        const rect = wrap.getBoundingClientRect();
        if (rect.width > 40 && rect.height > 40) { fitted = true; fit(bounds, 46, false); }
      },
      stepPath,
      destroy() { themeObserver.disconnect(); sizeObserver.disconnect(); container.innerHTML = ''; },
      get hasGraph() { return Boolean(graph); },
    };
  }

  global.CogitoGraph = { create };
})(window);
