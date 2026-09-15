'use strict';
const fs = require('fs');
const path = require('path');

const SEVERITY_CLASS = { critical: 'crit', high: 'crit', medium: 'warn', low: 'dim', info: 'dim' };
const MAX_ISSUES = 8;
const MAX_ACTIONS = 5;

function num(value, digits = 3) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return 'n/a';
  const n = Number(value);
  return Number.isInteger(n) ? String(n) : n.toFixed(digits);
}

// analysis.json is a few thousand lines of evidence. The pane shows the shape of
// the design in one line, then the ranked issues with the single fix each one
// suggests: enough to know what the optimizer is about to be told, nothing more.
// The full analysis stays on disk at heuristics/analysis.json.
function digest(runDir) {
  const file = path.join(runDir, 'heuristics/analysis.json');
  let analysis;
  try { analysis = JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; }

  const out = [];
  const push = (text = '', cls = '') => out.push({ text, cls });

  const meta = analysis.meta || {};
  const profile = analysis.design_profile || {};
  const domains = profile.clock_domains || [];
  push(`${meta.top || 'design'}  ·  ${num(profile.registers, 0)} registers (${num(profile.register_bits, 0)} bits)`
    + `  ·  ${num(profile.cells_word_level, 0)} cells  ·  ${domains.length} clock domain${domains.length === 1 ? '' : 's'}`, 'head');

  const issues = (analysis.issues || []);
  const shown = issues.slice(0, MAX_ISSUES);
  push();
  push(`${issues.length} ranked issue${issues.length === 1 ? '' : 's'}`
    + (issues.length > shown.length ? `  (top ${shown.length} shown)` : '')
    + '  — heuristic estimates, not STA', 'head');
  push();

  for (const issue of shown) {
    const severity = String(issue.severity || 'info').toLowerCase();
    const where = [issue.module, issue.signal].filter(Boolean).join('.');
    push(`${String(issue.rank ?? '-').padStart(2)}. ${severity.toUpperCase().padEnd(8)} ${issue.title || issue.id}`,
      SEVERITY_CLASS[severity] || '');
    if (where) push(`    ${where}`, 'dim');
    const slack = issue.evidence?.est_slack_ns;
    const fix = (issue.fixes || [])[0];
    const bits = [];
    if (slack !== undefined && slack !== null) bits.push(`est slack ${num(slack, 3)} ns`);
    if (fix?.estimated_gain_ns !== undefined && fix?.estimated_gain_ns !== null) {
      bits.push(`fix gains ~${num(fix.estimated_gain_ns, 3)} ns`);
    }
    if (bits.length) push(`    ${bits.join('   ')}`, 'dim');
    if (fix) push(`    → ${fix.algorithm || fix.title || ''}`, 'ok');
    push();
  }

  const actions = (analysis.feedback?.priority_actions || []).slice(0, MAX_ACTIONS);
  if (actions.length) {
    push('Where to start', 'head');
    actions.forEach((action, index) => {
      const text = typeof action === 'string' ? action : (action.action || action.title || '');
      if (text) push(`  ${index + 1}. ${text}`);
    });
    push();
  }
  push('Full analysis: heuristics/analysis.json in the run directory.', 'dim');

  return { lines: out, issueCount: issues.length };
}

module.exports = { digest };
