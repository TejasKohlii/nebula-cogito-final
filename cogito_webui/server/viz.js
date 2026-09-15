'use strict';
const { EventEmitter } = require('events');
const { spawn } = require('child_process');
const http = require('http');
const net = require('net');
const fs = require('fs');
const path = require('path');

const { ROOT, SHINKA_VISUALIZE, PYTHON, toolEnvironment } = require('./paths');

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function probe(port, timeoutMs = 1500) {
  return new Promise((resolve) => {
    const request = http.get({ host: '127.0.0.1', port, path: '/', timeout: timeoutMs }, (response) => {
      response.resume();
      resolve(response.statusCode > 0 && response.statusCode < 500);
    });
    request.on('timeout', () => { request.destroy(); resolve(false); });
    request.on('error', () => resolve(false));
  });
}

function programCount(port, database) {
  return new Promise((resolve) => {
    const request = http.get({ host: '127.0.0.1', port,
      path: `/get_programs_summary?db_path=${encodeURIComponent(database)}`, timeout: 2000 }, (response) => {
      let text = '';
      response.on('data', (chunk) => { text += chunk; });
      response.on('end', () => {
        try { const data = JSON.parse(text); resolve(response.statusCode === 200 && Array.isArray(data) ? data.length : 0); }
        catch { resolve(0); }
      });
      response.on('error', () => resolve(0));
    });
    request.on('timeout', () => { request.destroy(); resolve(0); });
    request.on('error', () => resolve(0));
  });
}

// The Shinka web visualizer is started as its own process and framed unchanged
// at its own port, so it stays exactly the UI that ships with Shinka.
class VizManager extends EventEmitter {
  constructor() {
    super();
    this.state = { state: 'idle', url: null, port: null, results: null, error: null };
    this.child = null;
    this.pending = null;
    this.epoch = 0;
  }

  push(patch) {
    this.state = { ...this.state, ...patch };
    this.emit('state', this.state);
  }

  async ensure(results) {
    if (!results) throw new Error('Shinka results directory is not available yet');
    if (this.state.state === 'ready' && this.state.results === results && await probe(this.state.port)) return this.state;
    if (this.pending && this.state.results === results) return this.pending;
    this.stop();
    const epoch = this.epoch;
    this.push({ state: 'starting', results, error: null });
    this.pending = this.start(results, epoch).catch((error) => {
      if (epoch === this.epoch) this.push({ state: 'error', error: error.message });
      return this.state;
    }).finally(() => { if (epoch === this.epoch) this.pending = null; });
    return this.pending;
  }

  async start(results, epoch) {
    const database = path.join(results, 'programs.sqlite');
    const databaseDeadline = Date.now() + 120000;
    while (!fs.existsSync(database)) {
      if (epoch !== this.epoch) return this.state;
      if (Date.now() >= databaseDeadline) throw new Error('Shinka has not created programs.sqlite after 120s; see the evaluator log.');
      await new Promise((resolve) => setTimeout(resolve, 400));
    }

    const port = await freePort();
    if (epoch !== this.epoch) return this.state;
    this.push({ state: 'starting', url: null, port, results, error: null });

    // The copied venv console script may carry another workspace's shebang.
    // Run the default visualizer through this workspace's selected Python.
    const customVisualizer = SHINKA_VISUALIZE && SHINKA_VISUALIZE !== path.join(ROOT, '.venv/bin/shinka_visualize');
    const [command, args] = customVisualizer
      ? [SHINKA_VISUALIZE, [results, '-p', String(port), '--db', database]]
      : [PYTHON, ['-m', 'shinka.webui.visualization', results, '-p', String(port), '--db', database]];

    const child = spawn(command, args, { cwd: ROOT, shell: false, env: { ...process.env, ...toolEnvironment() } });
    this.child = child;
    let diagnostic = '';
    const capture = (chunk) => { diagnostic = (diagnostic + chunk.toString()).slice(-4000); };
    child.stdout.on('data', capture);
    child.stderr.on('data', capture);
    child.on('error', (error) => {
      if (epoch === this.epoch) this.push({ state: 'error', error: error.message });
    });
    child.on('close', (code) => {
      if (epoch !== this.epoch || this.child !== child) return;
      this.child = null;
      if (this.state.state !== 'idle') {
        this.push({ state: 'error', url: null, error: `visualizer exited with code ${code}: ${diagnostic.trim()}` });
      }
    });

    // A SQLite file exists before gen_0 has finished evaluation. The upstream
    // page stops auto-refresh if its first query is empty, so do not expose it
    // until the very endpoint it loads can return a program.
    const deadline = Date.now() + 660000;
    let reachable = false;
    while (Date.now() < deadline) {
      if (epoch !== this.epoch) return this.state;
      if (this.child !== child) break;
      if (await probe(port)) {
        reachable = true;
        const count = await programCount(port, database);
        if (!count) {
          if (epoch === this.epoch && this.state.message !== 'Waiting for the first evaluated program…') {
            this.push({ message: 'Waiting for the first evaluated program…' });
          }
          await new Promise((r) => setTimeout(r, 500));
          continue;
        }
        if (epoch !== this.epoch || this.child !== child) return this.state;
        this.push({ state: 'ready', programs: count, message: null,
          url: `http://127.0.0.1:${port}/viz_tree.html?db_path=${encodeURIComponent(database)}`, error: null });
        return this.state;
      }
      if (!reachable && Date.now() > deadline - 660000 + 25000) break;
      await new Promise((r) => setTimeout(r, 400));
    }
    if (this.state.state !== 'error') this.push({ state: 'error', error: reachable
      ? 'No evaluated program is available yet. Check the evaluator log, then reopen the visualizer.'
      : `Visualizer did not become reachable in 25s. ${diagnostic.trim()}` });
    return this.state;
  }

  stop() {
    this.epoch += 1;
    this.pending = null;
    if (this.child) { try { this.child.kill('SIGTERM'); } catch { /* already gone */ } this.child = null; }
    this.push({ state: 'idle', url: null, port: null, results: null, error: null, message: null, programs: 0 });
  }
}

module.exports = { VizManager };
