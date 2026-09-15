/* Shared by the graph API and viewer, including graphs from older builds. */
(function (root) {
  'use strict';
  // An inexact node list is an unordered candidate cone, not serial waypoints.
  // Keep only real edges on launch-to-capture routes within that cone. Registers
  // and ports are boundaries, never intermediate timing hops.
  function approximateChain(doc, path) {
    const waypoints = path.nodes || [];
    const nodes = new Set(waypoints);
    const kinds = new Map((doc.nodes || []).map(node => [node.id, node.kind]));
    const start = path.start_node, end = path.end_node;
    const edges = new Set();
    if (!start || !end) return { nodes, edges, waypoints };
    nodes.add(start); nodes.add(end);
    const candidates = (doc.edges || []).filter(edge => !edge.implied && edge.style !== 'dotted'
      && nodes.has(edge.src) && nodes.has(edge.dst)
      && (edge.src === start || (edge.src !== end && kinds.get(edge.src) === 'comb'))
      && (edge.dst === end || (edge.dst !== start && kinds.get(edge.dst) === 'comb')));
    function reachable(origin, reverse) {
      const adjacency = new Map();
      for (const edge of candidates) {
        const from = reverse ? edge.dst : edge.src, to = reverse ? edge.src : edge.dst;
        if (!adjacency.has(from)) adjacency.set(from, []);
        adjacency.get(from).push(to);
      }
      const reached = new Set([origin]), queue = [origin];
      for (let i = 0; i < queue.length; i++) {
        for (const child of adjacency.get(queue[i]) || []) {
          if (!reached.has(child)) { reached.add(child); queue.push(child); }
        }
      }
      return reached;
    }
    const forward = reachable(start, false), backward = reachable(end, true);
    for (const edge of candidates) {
      if (forward.has(edge.src) && backward.has(edge.dst)) edges.add(`${edge.src} >> ${edge.dst}`);
    }
    return { nodes, edges, waypoints };
  }
  function groupPaths(doc, compact = false) {
    if (!doc) return doc;
    const groups = new Map();
    const paths = [...(doc.meta?.critical_paths || [])].sort((a, b) => a.slack - b.slack || a.index - b.index);
    for (const path of paths) {
      const nodes = path.nodes || [];
      const identity = nodes.length && path.start_node && path.end_node
        ? nodes : [nodes, path.startpoint, path.endpoint];
      const signature = JSON.stringify([identity, path.launch_clock ?? null, path.capture_clock ?? null,
        path.path_group ?? null, path.check ?? null, Boolean(path.nodes_exact), path.slack < 0]);
      if (!groups.has(signature)) groups.set(signature, { ...path, index: groups.size, instances: 0, members: [] });
      const group = groups.get(signature);
      group.instances += path.instances ?? 1;
      if (!compact) group.members.push(...(path.members || [{ startpoint: path.startpoint, endpoint: path.endpoint, slack: path.slack }]));
    }
    const critical_paths = [...groups.values()];
    const node_paths = {};
    const first = new Map();
    for (const path of critical_paths) {
      for (const id of new Set(path.nodes || [])) {
        (node_paths[id] ||= []).push(path.index);
        if (!first.has(id)) first.set(id, path);
      }
      if (compact) { delete path.points; delete path.members; }
    }
    const meta = { ...doc.meta, critical_paths, node_paths };
    if (compact) delete meta.timing_endpoints; // Full records remain available via ?full=1 and artifacts.
    const instances = critical_paths.reduce((sum, path) => sum + path.instances, 0);
    const nodes = (doc.nodes || []).map(node => ({ ...node,
      critical_path: first.has(node.id) ? first.get(node.id).index + 1 : 0,
      instances: first.get(node.id)?.instances || 0 }));
    return { ...doc, meta, nodes,
      edges: (doc.edges || []).filter(edge => !edge.implied && edge.style !== 'dotted'),
      stats: { ...doc.stats, critical_path_groups: critical_paths.length, critical_path_instances: instances } };
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = { groupPaths, approximateChain };
  else root.RtlPathGroups = { groupPaths, approximateChain };
})(typeof window !== 'undefined' ? window : globalThis);
