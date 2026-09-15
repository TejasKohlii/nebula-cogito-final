'use strict';
const http = require('http');
const fs = require('fs');
const path = require('path');
const { URL } = require('url');

const { ROOT, UI_ROOT, PYTHON, SHINKA_VISUALIZE, ORCHESTRATOR, contains } = require('./paths');
const { RunManager } = require('./run');
const { VizManager } = require('./viz');
const { browse, allowed } = require('./fsbrowse');
const { prepareFolder } = require('./designprep');
const { validateConnection } = require('./connection');
const { groupPaths } = require('../public/path-groups');
const graphViewCache = new Map();

const PUBLIC = path.join(UI_ROOT, 'public');
const PRESETS = path.join(UI_ROOT, 'presets/designs.json');

function parseArgs(argv) {
  const options = {
    port: Number(process.env.PORT) || 5173, host: '127.0.0.1',
    replay: null, open: false,

  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--port' || arg === '-p') options.port = Number(argv[++i]);
    else if (arg === '--host') options.host = argv[++i];
    else if (arg === '--replay') options.replay = argv[++i] || 'latest';
    else if (arg === '--open') options.open = true;
  }
  return options;
}

const options = parseArgs(process.argv.slice(2));
const runs = new RunManager();
const viz = new VizManager();

// ---- presets ---------------------------------------------------------------

function loadDesigns() {
  const presets = JSON.parse(fs.readFileSync(PRESETS, 'utf8'));
  return presets.map((preset) => {
    const designPath = path.resolve(ROOT, preset.design);
    return { ...preset, designPath, available: fs.existsSync(designPath) };
  });
}

function latestRunDir() {
  const base = path.join(ROOT, 'sum_agent/runs');
  let best = null;
  const walk = (dir, depth) => {
    let entries = [];
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const full = path.join(dir, entry.name);
      if (/^launch_/.test(entry.name)) {
        const stat = fs.statSync(full);
        if (!best || stat.mtimeMs > best.mtime) best = { path: full, mtime: stat.mtimeMs };
      } else if (depth > 0) walk(full, depth - 1);
    }
  };
  walk(base, 0);
  // Runs now live under the task workspace, so also look one level in from root.
  walk(path.join(ROOT, 'shinka_optimizer'), 3);
  return best?.path || null;
}

// ---- http helpers ----------------------------------------------------------

const MIME = {
  '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg',
  '.md': 'text/plain; charset=utf-8', '.txt': 'text/plain; charset=utf-8',
};

function sendJson(response, status, body) {
  const payload = JSON.stringify(body);
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  response.end(payload);
}

function sendError(response, error) {
  const status = error.status || 500;
  sendJson(response, status, { error: error.message || String(error) });
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    request.on('data', (chunk) => {
      size += chunk.length;
      if (size > 1_000_000) { reject(Object.assign(new Error('Body too large'), { status: 413 })); request.destroy(); return; }
      chunks.push(chunk);
    });
    request.on('end', () => {
      const text = Buffer.concat(chunks).toString('utf8');
      if (!text.trim()) { resolve({}); return; }
      try { resolve(JSON.parse(text)); } catch { reject(Object.assign(new Error('Invalid JSON body'), { status: 400 })); }
    });
    request.on('error', reject);
  });
}

function serveStatic(pathname, response) {
  const relative = pathname === '/' ? 'index.html' : pathname.replace(/^\/+/, '');
  const file = path.resolve(PUBLIC, relative);
  if (!contains(PUBLIC, file) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
    response.writeHead(404, { 'Content-Type': 'text/plain' });
    response.end('Not found');
    return;
  }
  response.writeHead(200, {
    'Content-Type': MIME[path.extname(file).toLowerCase()] || 'application/octet-stream',
    'Cache-Control': 'no-cache',
  });
  fs.createReadStream(file).pipe(response);
}

// Artifacts are read only from inside the current run directory; absolute paths
// coming back from the orchestrator are never turned into browser URLs directly.
function runFile(relative) {
  const runDir = runs.state.runDir;
  if (!runDir) throw Object.assign(new Error('No run in progress'), { status: 409 });
  const file = path.resolve(runDir, relative);
  if (!contains(runDir, file)) throw Object.assign(new Error('Path escapes the run directory'), { status: 403 });
  if (!fs.existsSync(file)) throw Object.assign(new Error(`Not produced yet: ${relative}`), { status: 404 });
  return file;
}

// ---- SSE -------------------------------------------------------------------

const clients = new Set();

function broadcast(message) {
  const payload = `data: ${JSON.stringify(message)}\n\n`;
  for (const client of clients) {
    try { client.write(payload); } catch { clients.delete(client); }
  }
}

let autoVizResults = null;
runs.on('state', (state) => {
  broadcast({ t: 'state', state, viz: viz.state });
  if (state.mode === 'live' && state.shinka.launched && state.shinka.results
      && autoVizResults !== state.shinka.results) {
    autoVizResults = state.shinka.results;
    viz.ensure(autoVizResults).catch((error) => runs.log('agent', `Visualizer: ${error.message}`, 'warn'));
  }
});
runs.on('log', ({ pane, lines }) => broadcast({ t: 'log', pane, lines }));
runs.on('reset', () => broadcast({ t: 'reset' }));
runs.on('reset-pane', (pane) => broadcast({ t: 'reset-pane', pane }));
viz.on('state', (state) => {
  broadcast({ t: 'viz', viz: state });
  if (state.message) runs.log('agent', `Visualizer: ${state.message}`, 'dim');
  if (state.state === 'error') runs.log('agent', `Visualizer: ${state.error}`, 'warn');
});

// ---- routes ----------------------------------------------------------------

async function route(request, response, url) {
  const { pathname } = url;

  if (pathname === '/api/health') {
    return sendJson(response, 200, {
      ok: true, root: ROOT, python: PYTHON, orchestrator: ORCHESTRATOR,
      orchestratorPresent: fs.existsSync(ORCHESTRATOR),
      shinkaVisualize: SHINKA_VISUALIZE, replayDefault: options.replay ? latestRunDir() : null,
    });
  }

  if (pathname === '/api/designs') {
    return sendJson(response, 200, {
      designs: loadDesigns(),
      replayEnabled: Boolean(options.replay),
    });
  }

  if (pathname === '/api/state') {
    return sendJson(response, 200, { state: runs.state, viz: viz.state, logs: runs.logs });
  }

  if (pathname === '/api/events') {
    response.writeHead(200, {
      'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache',
      Connection: 'keep-alive', 'X-Accel-Buffering': 'no',
    });
    response.write(`data: ${JSON.stringify({ t: 'snapshot', state: runs.state, viz: viz.state, logs: runs.logs })}\n\n`);
    clients.add(response);
    const keepAlive = setInterval(() => { try { response.write(': ping\n\n'); } catch { /* closed */ } }, 15000);
    request.on('close', () => { clearInterval(keepAlive); clients.delete(response); });
    return undefined;
  }

  if (pathname === '/api/run' && request.method === 'POST') {
    const body = await readBody(request);
    if (!body.analysisOnly) validateConnection(body);
    const budget = Number(body.budget);
    if (!Number.isFinite(budget) || budget < 0.5) {
      throw Object.assign(new Error('Budget must be a number of at least $0.50'), { status: 400 });
    }
    let design = null;
    let config = null;
    let designId = null;
    if (body.folder) {
      const folder = path.resolve(body.folder);
      if (!allowed(folder)) throw Object.assign(new Error('Selected folder is outside the project and home directory'), { status: 403 });
      if (runs.busy) throw Object.assign(new Error('A run is already in progress'), { status: 409 });
      const prepared = await prepareFolder(folder);
      design = prepared.source;
      config = prepared.config;
      designId = 'folder';
    } else {
      const preset = loadDesigns().find((d) => d.id === body.designId);
      if (!preset) throw Object.assign(new Error('Unknown design'), { status: 400 });
      if (preset.locked) throw Object.assign(new Error(preset.lockReason || 'Design is locked'), { status: 400 });
      design = preset.designPath;
      config = preset.config ? path.resolve(ROOT, preset.config) : null;
      designId = preset.id;
    }
    const state = runs.start({
      designId, design, config, budget,
      analysisOnly: Boolean(body.analysisOnly),
      provider: body.provider, apiKey: body.apiKey,
      maxGenerations: body.maxGenerations, maxHours: body.maxHours,
    });
    return sendJson(response, 200, { state });
  }

  if (pathname === '/api/replay' && request.method === 'POST') {
    if (!options.replay) throw Object.assign(new Error('Replay is not enabled; start the server with --replay'), { status: 403 });
    const body = await readBody(request);
    const dir = body.runDir && body.runDir !== 'latest' ? body.runDir
      : (options.replay !== 'latest' ? options.replay : latestRunDir());
    if (!dir) throw Object.assign(new Error('No completed run found to replay'), { status: 404 });
    return sendJson(response, 200, { state: runs.replayRun(dir) });
  }

  if (pathname === '/api/logs/save' && request.method === 'POST') {
    return sendJson(response, 200, { ok: true, ...runs.keepLogs() });
  }

  if (pathname === '/api/logs/discard' && request.method === 'POST') {
    return sendJson(response, 200, { ok: true, ...runs.purgeLogs(null, { force: true }) });
  }

  if (pathname === '/api/cancel' && request.method === 'POST') {
    runs.cancel();
    return sendJson(response, 200, { ok: true });
  }

  if (pathname === '/api/graph') {
    const which = url.searchParams.get('which') === 'final' ? 'final' : 'initial';
    const relative = which === 'final' ? 'final/graph/graph.json' : 'graph/graph.json';
    const file = runFile(relative);
    if (url.searchParams.get('full') === '1') {
      response.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
      return fs.createReadStream(file).pipe(response);
    }
    const stat = fs.statSync(file);
    const version = `${stat.mtimeMs}:${stat.ctimeMs}:${stat.size}`;
    let cached = graphViewCache.get(file);
    if (!cached || cached.version !== version) {
      cached = { version, body: JSON.stringify(groupPaths(JSON.parse(fs.readFileSync(file, 'utf8')), true)) };
      if (graphViewCache.size >= 2) graphViewCache.clear();
      graphViewCache.set(file, cached);
    }
    response.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
    return response.end(cached.body);
  }

  if (pathname === '/api/artifact') {
    const name = url.searchParams.get('name');
    if (!name) throw Object.assign(new Error('name is required'), { status: 400 });
    const file = runFile(name);
    response.writeHead(200, {
      'Content-Type': MIME[path.extname(file).toLowerCase()] || 'text/plain; charset=utf-8',
      'Cache-Control': 'no-store',
    });
    return fs.createReadStream(file).pipe(response);
  }

  if (pathname === '/api/fs') {
    return sendJson(response, 200, browse(url.searchParams.get('path')));
  }

  if (pathname === '/api/design/prepare' && request.method === 'POST') {
    const body = await readBody(request);
    if (!body.folder) throw Object.assign(new Error('folder is required'), { status: 400 });
    const folder = path.resolve(body.folder);
    if (!allowed(folder)) throw Object.assign(new Error('Selected folder is outside the project and home directory'), { status: 403 });
    const design = await prepareFolder(folder);
    runs.logToolReports({ label: 'Upload preflight', files: design.reports });
    return sendJson(response, 200, { design });
  }

  if (pathname === '/api/viz/start' && request.method === 'POST') {
    const results = runs.state.shinka.results;
    if (!results) throw Object.assign(new Error('Shinka has not produced a results directory yet'), { status: 409 });
    const state = await viz.ensure(results);
    return sendJson(response, 200, { viz: state });
  }

  if (pathname === '/api/viz/stop' && request.method === 'POST') {
    viz.stop();
    return sendJson(response, 200, { viz: viz.state });
  }

  if (pathname.startsWith('/api/')) {
    throw Object.assign(new Error('Unknown endpoint'), { status: 404 });
  }

  return serveStatic(pathname, response);
}

const server = http.createServer((request, response) => {
  const url = new URL(request.url, `http://${request.headers.host || 'localhost'}`);
  Promise.resolve()
    .then(() => route(request, response, url))
    .catch((error) => { if (!response.headersSent) sendError(response, error); else response.end(); });
});

server.on('error', (error) => {
  if (error.code === 'EADDRINUSE') {
    process.stderr.write(
      `\n  Port ${options.port} is already in use on ${options.host}.\n` +
      `  Another Cogito UI is probably still running \u2014 open http://${options.host}:${options.port}/ to check.\n` +
      `  To stop Cogito from this checkout:  python3 cogito stop\n` +
      `  Or use another port:  python3 cogito start --port 5174\n\n`);
    process.exit(1);
  }
  process.stderr.write(`\n  Could not start the server: ${error.message}\n\n`);
  process.exit(1);
});

server.listen(options.port, options.host, () => {
  const address = `http://${options.host}:${options.port}/`;
  process.stdout.write(`\n  Cogito UI   ${address}\n`);
  process.stdout.write(`  project     ${ROOT}\n`);
  process.stdout.write(`  python      ${PYTHON}\n`);
  if (options.replay) {
    const dir = options.replay === 'latest' ? latestRunDir() : path.resolve(options.replay);
    process.stdout.write(`  replay      ${dir || 'no completed run found'}\n`);
  }
  process.stdout.write('\n');
  if (options.open) {
    const { spawn } = require('child_process');
    spawn('xdg-open', [address], { stdio: 'ignore', detached: true }).unref();
  }
});

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => { viz.stop(); runs.cancel(); process.exit(0); });
}
