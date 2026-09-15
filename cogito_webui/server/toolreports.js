'use strict';
const fs = require('fs');
const path = require('path');
const { readJson } = require('./report');

const LABELS = { yosys: 'Yosys synthesis', opensta: 'OpenSTA timing', equivalence: 'Formal equivalence (Yosys)', failures: 'Failure analysis' };

function collect(results) {
  if (!results) return [];
  const directories = (base) => {
    try { return fs.readdirSync(base, { withFileTypes: true }).filter((e) => e.isDirectory()).map((e) => e.name); }
    catch { return []; }
  };
  const entries = directories(results).filter((name) => /^gen_\d+$/.test(name))
    .sort((a, b) => Number(a.slice(4)) - Number(b.slice(4)))
    .map((name) => ({ label: `Generation ${name.slice(4)}`, directory: path.join(results, name, 'results') }));
  for (const name of directories(path.join(results, 'verification'))) {
    entries.push({ label: 'Winner verification', directory: path.join(results, 'verification', name, 'results') });
  }
  return entries.map(({ label, directory }) => ({ label, files: readJson(path.join(directory, 'reports/index.json'))?.files })).filter((entry) => entry.files);
}

module.exports = { collect, LABELS };
