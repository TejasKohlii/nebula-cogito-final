'use strict';
const fs = require('fs');
const path = require('path');
const { readJson } = require('./report');
const formalCache = new Map();

// Yosys writes "CPU: user 2.61s system 0.09s" at the end of every log and the
// exact reason on failure. Wall-clock per attempt comes from log mtimes, which
// is the only timing the flow records.
function parseFormalLog(file) {
  let text;
  let stamp;
  try {
    const stat = fs.statSync(file);
    stamp = `${stat.size}:${stat.mtimeMs}`;
    const cached = formalCache.get(file);
    if (cached?.stamp === stamp) return cached.value;
    // Formal traces can be tens of MB each. Poll summary-bearing head/tail
    // regions, not every solver line on the Node event loop every two seconds.
    const fd = fs.openSync(file, 'r');
    try {
      const limit = 512 * 1024;
      const tail = Buffer.alloc(Math.min(stat.size, limit));
      const n = fs.readSync(fd, tail, 0, tail.length, Math.max(0, stat.size - limit));
      text = tail.subarray(0, n).toString('utf8');
      if (stat.size > limit) {
        const head = Buffer.alloc(64 * 1024);
        const count = fs.readSync(fd, head, 0, head.length, 0);
        text = head.subarray(0, count).toString('utf8') + '\n' + text;
      }
    } finally { fs.closeSync(fd); }
  } catch { return null; }
  const cpu = text.match(/CPU: user ([\d.]+)s system ([\d.]+)s/);
  const error = text.match(/^ERROR: (.+)$/m);
  const unproven = [...text.matchAll(/Found (\d+) unproven \$equiv cells/g)].map((m) => Number(m[1]));
  const proven = text.match(/Of those cells (\d+) are proven and (\d+) are unproven/);
  const total = text.match(/Found (\d+) \$equiv cells in equiv/);
  const value = {
    cpuSeconds: cpu ? Number(cpu[1]) + Number(cpu[2]) : null,
    error: error ? error[1].trim() : null,
    unprovenFinal: unproven.length ? unproven[unproven.length - 1] : null,
    provenCells: proven ? Number(proven[1]) : null,
    unprovenCells: proven ? Number(proven[2]) : null,
    totalCells: total ? Number(total[1]) : null,
    success: /Equivalence successfully proven!/.test(text),
  };
  if (formalCache.size >= 256) formalCache.delete(formalCache.keys().next().value);
  formalCache.set(file, { stamp, value });
  return value;
}

function mtime(file) {
  try { return fs.statSync(file).mtimeMs; } catch { return null; }
}

function errorSummary(error) {
  if (!error) return null;
  const text = String(error);
  const errors = text.match(/ERROR:[^\n]*(?:\n>[^\n]*)*/g);
  const firstLine = text.split('\n')[0];
  return (errors ? [firstLine.startsWith('ERROR:') ? '' : firstLine, ...errors.slice(-2)]
    .filter(Boolean).join('\n') : text).slice(0, 1000);
}

function formalVerdict(proof, publicMetrics) {
  if (proof?.passed === false || publicMetrics.formal_equivalence === false) return false;
  if (proof?.passed === true || publicMetrics.formal_equivalence === true) return true;
  return null;
}

function readDesign(designDir) {
  if (fs.existsSync(path.join(designDir, 'equivalence'))) designDir = path.join(designDir, 'equivalence');
  const summary = readJson(path.join(designDir, 'proof_summary.json'));
  const attempts = [];
  const declared = summary?.attempts || [];
  const count = Math.max(declared.length, fs.existsSync(designDir)
    ? fs.readdirSync(designDir).filter((n) => /^formal_match_\d+\.log$/.test(n)).length : 0);

  let previousEnd = mtime(path.join(designDir, 'formal_match_0.ys'));
  for (let i = 0; i < count; i += 1) {
    const log = path.join(designDir, `formal_match_${i}.log`);
    const parsed = parseFormalLog(log);
    const meta = declared[i] || {};
    const end = mtime(log);
    const wall = end !== null && previousEnd !== null ? Math.max(0, (end - previousEnd) / 1000) : null;
    previousEnd = end ?? previousEnd;
    attempts.push({
      attempt: i,
      depth: meta.depth ?? null,
      passed: meta.passed ?? parsed?.success ?? null,
      excluded: meta.excluded_internal_names || [],
      cpuSeconds: parsed?.cpuSeconds ?? null,
      wallSeconds: wall,
      error: parsed?.error ?? null,
      unproven: parsed?.unprovenFinal ?? parsed?.unprovenCells ?? null,
      proven: parsed?.provenCells ?? null,
      total: parsed?.totalCells ?? null,
      hasLog: parsed !== null,
    });
  }
  const start = mtime(path.join(designDir, 'formal_match_0.ys'));
  const finish = mtime(path.join(designDir, 'proof_summary.json'));
  return {
    exists: summary !== null || attempts.length > 0,
    passed: summary?.passed ?? (attempts.length ? attempts.every((a) => a.passed !== false) && attempts.some((a) => a.passed) : null),
    method: summary?.method || null,
    attempts,
    totalSeconds: start !== null && finish !== null ? Math.max(0, (finish - start) / 1000) : null,
    cpuSeconds: attempts.reduce((sum, a) => (a.cpuSeconds === null ? sum : sum + a.cpuSeconds), 0) || null,
  };
}

function collect(shinkaDir) {
  if (!shinkaDir || !fs.existsSync(shinkaDir)) return null;
  const generations = [];
  const names = fs.readdirSync(shinkaDir).filter((n) => /^gen_\d+$/.test(n))
    .sort((a, b) => Number(a.slice(4)) - Number(b.slice(4)));

  for (const name of names) {
    const base = path.join(shinkaDir, name);
    const metrics = readJson(path.join(base, 'results/metrics.json'));
    const correct = readJson(path.join(base, 'results/correct.json'));
    const proof = readDesign(path.join(base, 'results/design'));
    // A failed proof attempt can still be retrying. The evaluator's final
    // verdict, rather than an intermediate proof_summary, ends a generation.
    if (!metrics && proof) proof.passed = null;
    const publicMetrics = metrics?.public || {};
    const work = path.join(base, 'results/design');
    const phase = metrics ? (correct?.correct ? 'accepted' : 'rejected')
      : ['sta/sta.log', 'sta.log', 'reports/sta_run.log', 'reports/timing.log']
        .some((file) => fs.existsSync(path.join(work, file))) ? 'timing analysis'
      : ['synthesis.ys', 'build/synth.ys', 'reports/synth.log', 'synthesis.log']
        .some((file) => fs.existsSync(path.join(work, file))) ? 'synthesis'
      : proof?.exists || fs.existsSync(path.join(work, 'equivalence')) ? 'equivalence proof'
      : fs.existsSync(path.join(work, 'syntax.ys')) ? 'checking RTL'
      : fs.existsSync(path.join(base, 'main.sv')) ? 'queued for evaluation' : 'generating candidate';
    generations.push({
      generation: Number(name.slice(4)),
      phase,
      evaluated: metrics !== null,
      correct: correct?.correct ?? null,
      error: correct?.error || null,
      errorSummary: errorSummary(correct?.error),
      failureStage: metrics?.private?.failure_stage ?? null,
      formalEquivalence: metrics ? formalVerdict(proof, publicMetrics) : null,
      wns: publicMetrics.wns_ns ?? null,
      tns: publicMetrics.tns_ns ?? null,
      violating: publicMetrics.violating_endpoints ?? null,
      timingClosed: publicMetrics.timing_closed ?? null,
      runtimeSeconds: metrics?.private?.runtime_seconds ?? null,
      proof,
    });
  }

  const winner = readJson(path.join(shinkaDir, 'winner.json'));
  let final = null;
  const verificationRoot = path.join(shinkaDir, 'verification');
  if (fs.existsSync(verificationRoot)) {
    const candidates = fs.readdirSync(verificationRoot).filter((n) => fs.statSync(path.join(verificationRoot, n)).isDirectory());
    const id = winner?.winner_id && candidates.includes(winner.winner_id) ? winner.winner_id : candidates[0];
    if (id) {
      final = {
        id,
        generation: winner?.generation ?? null,
        verified: winner?.verified ?? null,
        success: winner?.success ?? null,
        proof: readDesign(path.join(verificationRoot, id, 'results/design')),
        metrics: winner?.metrics || null,
      };
    }
  }

  const latest = [...generations].reverse().find((g) => g.evaluated) || null;
  return {
    generations,
    final,
    latest: latest ? { generation: latest.generation, wns: latest.wns, tns: latest.tns, violating: latest.violating, timingClosed: latest.timingClosed } : null,
    counts: {
      evaluated: generations.filter((g) => g.evaluated).length,
      passed: generations.filter((g) => g.formalEquivalence === true).length,
      failed: generations.filter((g) => g.formalEquivalence === false).length,
      rejected: generations.filter((g) => g.correct === false).length,
    },
  };
}

module.exports = { collect, parseFormalLog };
