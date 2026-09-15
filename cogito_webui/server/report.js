'use strict';
const fs = require('fs');
const path = require('path');

function readJson(file) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; }
}

function pick(object, ...keys) {
  if (!object) return null;
  for (const key of keys) {
    const value = object[key];
    if (value !== undefined && value !== null) return value;
  }
  return null;
}

function countGenerations(shinkaDir) {
  try {
    return fs.readdirSync(shinkaDir).filter((n) => /^gen_\d+$/.test(n)).length || null;
  } catch { return null; }
}

// The backend rewrite introduced final_report.json. Runs created before it still
// hold every number in attribution.json / winner.json / status.json, so the UI
// reconstructs the same shape rather than showing an empty report for them.
function reconstruct(runDir) {
  const shinkaDir = path.join(runDir, 'shinka');
  const attribution = readJson(path.join(shinkaDir, 'attribution.json'));
  const winner = readJson(path.join(shinkaDir, 'winner.json'));
  const status = readJson(path.join(shinkaDir, 'status.json'));
  if (!attribution && !winner) return null;
  const measurements = attribution?.measurements || {};
  const initial = measurements.original_rtl_original_flow || readJson(path.join(shinkaDir, 'gen_0/results/metrics.json'))?.public || null;
  const final = measurements.winner_rtl_winner_flow || winner?.metrics || null;
  return {
    schema_version: 1,
    state: status?.state || (winner?.success ? 'success_verified' : null),
    timing_closed: final?.timing_closed ?? null,
    ppa_optimization: readJson(path.join(shinkaDir, 'task_manifest.json'))?.task?.optimization?.mode === 'timing_then_ppa',
    optimization_phase: status?.phase, stop_reason: status?.stop_reason,
    initial,
    final,
    generations: countGenerations(shinkaDir),
    elapsed_seconds: pick(status, 'elapsed_seconds'),
    total_elapsed_seconds: null,
    reported_api_cost_usd: pick(status, 'reported_api_cost'),
    native_reported_cost_usd: pick(attribution?.cost, 'native_reported_cost'),
    budget_accounted_cost_usd: pick(attribution?.cost, 'budget_accounted_cost'),
    power: attribution?.power || null,
    winner: fs.existsSync(path.join(shinkaDir, 'winner.json')) ? path.join(shinkaDir, 'winner.json') : null,
    final_rtl: winner?.candidate || null,
    final_graph: null,
    _reconstructed: true,
  };
}

const METRIC_SPECS = [
  { key: 'wns_ns', label: 'WNS', unit: 'ns', digits: 4, better: 'higher' },
  { key: 'tns_ns', label: 'TNS', unit: 'ns', digits: 2, better: 'higher' },
  { key: 'violating_endpoints', label: 'Violating endpoints', unit: '', digits: 0, better: 'lower' },
  { key: 'standard_cells', label: 'Standard cells', unit: '', digits: 0, better: 'lower' },
  { key: 'cell_area_um2', label: 'Cell area', unit: 'um2', digits: 1, better: 'lower' },
];

function normalize(raw) {
  if (!raw) return null;
  const initial = raw.initial || null;
  const final = raw.final || null;
  const metrics = METRIC_SPECS.map((spec) => ({
    ...spec,
    initial: pick(initial, spec.key),
    final: pick(final, spec.key),
  })).filter((m) => m.initial !== null || m.final !== null);

  const power = raw.power || null;
  const powerInitial = pick(power?.baseline?.watts || null, 'total') ?? pick(initial, 'power_w');
  const powerFinal = pick(power?.winner?.watts || null, 'total') ?? pick(final, 'power_w');
  if (powerInitial !== null || powerFinal !== null) {
    metrics.push({
      key: 'power_w', label: 'Power (vectorless)', unit: 'W', digits: 4, better: 'lower',
      initial: powerInitial, final: powerFinal,
    });
  }

  const notes = [];
  if (raw._reconstructed) notes.push('Report assembled from this run’s attribution/winner artifacts (run predates final_report.json).');
  if (power || powerInitial !== null || powerFinal !== null) notes.push('Power is a vectorless uniform-activity estimate, not workload or signoff power.');
  if (raw.timing_closed === false) notes.push('Search ended without timing closure; the graph shown is the best verified candidate, not a closed design.');

  if (raw.ppa_optimization && raw.stop_reason) notes.push(`Timing-first PPA search stopped: ${raw.stop_reason}. This does not establish a global optimum.`);

  return {
    optimizationPhase: raw.optimization_phase ?? null,
    stopReason: raw.stop_reason ?? null,
    state: raw.state ?? null,
    timingClosed: raw.timing_closed ?? null,
    ppaOptimization: raw.ppa_optimization ?? false,
    metrics,
    generations: raw.generations ?? null,
    equivalence: pick(final, 'formal_equivalence'),
    cost: {
      reported: raw.reported_api_cost_usd ?? null,
      native: raw.native_reported_cost_usd ?? null,
      accounted: raw.budget_accounted_cost_usd ?? null,
    },
    elapsedSeconds: raw.elapsed_seconds ?? null,
    totalElapsedSeconds: raw.total_elapsed_seconds ?? null,
    winner: raw.winner ?? null,
    finalRtl: raw.final_rtl ?? null,
    finalGraph: raw.final_graph ?? null,
    notes,
  };
}

function loadReport(runDir) {
  const direct = readJson(path.join(runDir, 'final_report.json'));
  return normalize(direct || reconstruct(runDir));
}

module.exports = { loadReport, normalize, reconstruct, readJson };
