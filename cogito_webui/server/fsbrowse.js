'use strict';
const fs = require('fs');
const path = require('path');
const { ROOT, HOME, contains } = require('./paths');

const ALLOWED = [ROOT, HOME];
const REAL_ALLOWED = ALLOWED.map((base) => fs.realpathSync(base));
const RTL_EXTENSIONS = new Set(['.v', '.sv']);
const IGNORED = new Set(['.git', '.hg', '.svn', 'node_modules', 'build', 'out', 'outputs',
  'reports', 'runs', 'results', 'obj', 'dist', '__pycache__', 'generated_designs']);
const scanCache = new Map();

function allowed(target) {
  let real;
  try { real = fs.realpathSync(target); } catch { return false; }
  return REAL_ALLOWED.some((base) => contains(base, real));
}

function rtlCount(directory, limit = 513) {
  let stat;
  try { stat = fs.statSync(directory); } catch { return 0; }
  const cached = scanCache.get(directory);
  if (cached && cached.mtime === stat.mtimeMs) return cached.count;
  let count = 0;
  const stack = [directory];
  let visited = 0;
  while (stack.length && count < limit && visited < 10000) {
    const current = stack.pop();
    let entries = [];
    try { entries = fs.readdirSync(current, { withFileTypes: true }); } catch { continue; }
    for (const entry of entries) {
      visited += 1;
      if (entry.name.startsWith('.') || IGNORED.has(entry.name.toLowerCase())) continue;
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) stack.push(full);
      else if (entry.isFile() && RTL_EXTENSIONS.has(path.extname(entry.name).toLowerCase())) count += 1;
      if (count >= limit || visited >= 10000) break;
    }
  }
  scanCache.set(directory, { mtime: stat.mtimeMs, count });
  return count;
}

// Browsing is restricted to the project and the user's home directory: the UI
// only ever needs a local design folder, and this keeps the endpoint from
// turning into a whole-filesystem reader.
function browse(requested) {
  const requestedPath = path.resolve(requested || ROOT);
  if (!allowed(requestedPath)) throw Object.assign(new Error('Path is outside the project and home directory'), { status: 403 });
  const target = fs.realpathSync(requestedPath);
  const stat = fs.statSync(target);
  if (!stat.isDirectory()) throw Object.assign(new Error('Not a directory'), { status: 400 });

  const entries = [];
  for (const item of fs.readdirSync(target, { withFileTypes: true })) {
    if (item.name.startsWith('.')) continue;
    let isDir = item.isDirectory();
    if (item.isSymbolicLink()) {
      try { isDir = fs.statSync(path.join(target, item.name)).isDirectory(); } catch { continue; }
    }
    if (!isDir) continue;
    const full = path.join(target, item.name);
    const hasConfig = fs.existsSync(path.join(full, 'cogito.json'));
    const count = hasConfig ? 0 : rtlCount(full, 2);
    entries.push({ name: item.name, path: full, hasConfig, hasRtl: count > 0 });
  }
  entries.sort((a, b) => a.name.localeCompare(b.name));

  const parent = path.dirname(target);
  const hasConfig = fs.existsSync(path.join(target, 'cogito.json'));
  const count = hasConfig ? 0 : rtlCount(target);
  return {
    path: target,
    parent: parent !== target && allowed(parent) ? parent : null,
    hasConfig,
    rtlCount: count,
    rtlLimitReached: count >= 513,
    entries,
    roots: [{ name: 'Project', path: ROOT }, { name: 'Home', path: HOME }],
  };
}

module.exports = { browse, allowed };
