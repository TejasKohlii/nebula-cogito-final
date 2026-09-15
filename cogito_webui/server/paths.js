'use strict';
const path = require('path');
const fs = require('fs');
const os = require('os');

const UI_ROOT = path.resolve(__dirname, '..');
const ROOT = path.resolve(UI_ROOT, '..');
const HOME = os.homedir();

function firstExisting(...candidates) {
  for (const c of candidates) if (c && fs.existsSync(c)) return c;
  return null;
}

const PYTHON = firstExisting(
  process.env.COGITO_PYTHON,
  path.join(ROOT, '.venv/bin/python'),
  '/usr/bin/python3'
) || 'python3';

const SHINKA_VISUALIZE = firstExisting(
  process.env.SHINKA_VISUALIZE,
  path.join(ROOT, '.venv/bin/shinka_visualize')
);

const ORCHESTRATOR = path.join(ROOT, 'sum_agent/orchestrator.py');

// Optional local tool settings; environment variables take precedence.
function toolEnvironment() {
  const environment = {};
  let config = {};
  for (const name of ['tools.json', 'tools.local.json']) {
    try { config = { ...config, ...JSON.parse(fs.readFileSync(path.join(UI_ROOT, name), 'utf8')) }; }
    catch { /* Machine-local settings are optional. */ }
  }
  for (const [key, variable] of [['yosys', 'YOSYS'], ['sta', 'STA'], ['verilator', 'VERILATOR']]) {
    const configured = config[key];
    if (process.env[variable] || typeof configured !== 'string' || !configured) continue;
    if (fs.existsSync(configured)) environment[variable] = configured;
  }
  return environment;
}

// `path.resolve` normalises away `..`, so a prefix test after resolution is a
// genuine containment check rather than a string comparison on user input.
function contains(parent, child) {
  const p = path.resolve(parent);
  const c = path.resolve(child);
  return c === p || c.startsWith(p.endsWith(path.sep) ? p : p + path.sep);
}

module.exports = { UI_ROOT, ROOT, HOME, PYTHON, SHINKA_VISUALIZE, ORCHESTRATOR, contains, firstExisting, toolEnvironment };
