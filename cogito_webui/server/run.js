'use strict';
const { EventEmitter } = require('events');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const { ROOT, PYTHON, ORCHESTRATOR, toolEnvironment } = require('./paths');
const { Tail } = require('./tail');
const { digest } = require('./heuristics');
const { loadReport, readJson } = require('./report');
const { collect: collectEquivalence } = require('./equivalence');
const { AgentLogCleaner, cleanStderr } = require('./logclean');
const { connectionEnvironment } = require('./connection');
const { collect: collectToolReports, LABELS: REPORT_LABELS } = require('./toolreports');

const LOG_CAP = 6000;
const STAGES = ['graph', 'heuristics', 'final_graph'];
const STAGE_LABEL = {
  graph: 'Building the timing graph',
  heuristics: 'Running the RTL heuristics',
  baseline: 'Building initial mapping and timing constraints',
  final_graph: 'Graphing the verified winner',
};

function emptyState() {
  return {
    status: 'idle',            // idle | running | finished | failed
    mode: null,                // live | analysis | replay | dev
    replay: false,
    dev: false,
    logsKept: false,
    runDir: null,
    designId: null,
    design: null,
    budgetUsd: null,
    startedAt: null,
    finishedAt: null,
    stages: { graph: { state: 'pending' }, heuristics: { state: 'pending' }, final_graph: { state: 'pending' } },
    graph: { initial: false, final: false, generation: 0, active: null },
    shinka: { launched: false, pid: null, results: null, log: null, status: null, progress: null, vizCapable: false, equivalence: null },
    report: null,
    cost: null,
    error: null,
    exitCode: null,
  };
}

class RunManager extends EventEmitter {
  constructor() {
    super();
    this.state = emptyState();
    this.logs = { heuristics: [], agent: [] };
    this.child = null;
    this.tails = [];
    this.pollTimer = null;
    this.replayTimer = null;
    this.cleaner = new AgentLogCleaner();
    this.generationPhases = new Map();
    this.reportPaths = new Set();
    // Run logs are large. They are deleted when the next run starts unless the
    // operator pressed Save, which is also what happens if the UI is just closed.
    this.logsKept = false;
    this.previousRunDir = null;
  }

  // ---- plumbing -----------------------------------------------------------

  push() { this.emit('state', this.state); }

  log(pane, lines, cls = '') {
    const list = (Array.isArray(lines) ? lines : [lines]).map(
      (line) => (typeof line === 'string' ? { text: line, cls } : line)
    );
    if (!list.length) return;
    const buffer = this.logs[pane];
    buffer.push(...list);
    if (buffer.length > LOG_CAP) buffer.splice(0, buffer.length - LOG_CAP);
    this.emit('log', { pane, lines: list });
  }

  clearLogs() {
    this.cleaner = new AgentLogCleaner();
    this.generationPhases.clear();
    this.reportPaths.clear();
    this.lastProgressState = null;
    this.logs.heuristics = [];
    this.logs.agent = [];
    this.emit('reset');
  }

  stopWatchers() {
    for (const tail of this.tails) tail.stop();
    this.tails = [];
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.pollTimer = null;
    if (this.replayTimer) clearTimeout(this.replayTimer);
    this.replayTimer = null;
  }

  get busy() { return this.state.status === 'running'; }

  // ---- starting a live run ------------------------------------------------

  start({ designId, design, config, budget, analysisOnly = false, maxGenerations, maxHours, provider, apiKey }) {
    if (this.busy) throw new Error('A run is already in progress');
    const baseEnv = { ...process.env, ...toolEnvironment() };
    const env = analysisOnly ? baseEnv : connectionEnvironment({ provider, apiKey }, baseEnv);
    const designPath = path.resolve(ROOT, design);
    if (!fs.existsSync(designPath)) throw new Error(`Design path does not exist: ${designPath}`);

    this.purgeLogs();
    this.stopWatchers();
    this.clearLogs();
    this.state = emptyState();
    this.state.status = 'running';
    this.state.mode = analysisOnly ? 'analysis' : 'live';
    this.state.designId = designId || null;
    this.state.provider = analysisOnly ? null : provider;
    this.state.design = designPath;
    this.state.budgetUsd = budget ?? null;
    this.state.startedAt = Date.now();
    this.logsKept = false;

    const args = ['-B', '-u', ORCHESTRATOR, '--design', designPath, '--budget', String(budget)];
    if (config) args.push('--config', path.resolve(ROOT, config));
    if (analysisOnly) args.push('--analysis-only');
    if (maxGenerations) args.push('--max-generations', String(maxGenerations));
    if (maxHours) args.push('--max-hours', String(maxHours));

    this.log('agent', analysisOnly
      ? 'Analysis only: graph and heuristics run, no model calls and no Shinka campaign.'
      : `Starting optimization on ${path.basename(designPath)}, total budget $${budget}.`, 'head');

    if (!analysisOnly) this.log('agent', provider === 'api' ? 'Using Anthropic API.' : 'Using Claude Code CLI.', 'dim');
    const child = spawn(PYTHON, args, { cwd: ROOT, shell: false, detached: process.platform !== 'win32', env });
    this.child = child;

    let stdoutBuffer = '';
    child.stdout.on('data', (chunk) => {
      stdoutBuffer += chunk.toString('utf8');
      const lines = stdoutBuffer.split('\n');
      stdoutBuffer = lines.pop();
      for (const line of lines) this.handleNdjson(line);
    });

    let stderrBuffer = '';
    child.stderr.on('data', (chunk) => {
      stderrBuffer += chunk.toString('utf8');
      const lines = stderrBuffer.split('\n');
      stderrBuffer = lines.pop();
      for (const line of lines) {
        const entry = cleanStderr(line.replace(/\r$/, ''));
        if (entry) this.log('agent', [entry]);
      }
    });

    child.on('error', (error) => this.fail(`Could not start orchestrator: ${error.message}`));
    child.on('close', (code) => {
      if (stdoutBuffer.trim()) this.handleNdjson(stdoutBuffer);
      if (stderrBuffer.trim()) this.log('agent', stderrBuffer, 'dim');
      this.child = null;
      this.state.exitCode = code;
      if (this.state.status === 'running') {
        if (code === 0) this.finish();
        else this.fail(this.state.error || `Orchestrator exited with code ${code}`);
      } else {
        this.push();
      }
    });

    this.push();
    return this.state;
  }

  handleNdjson(line) {
    const text = line.trim();
    if (!text) return;
    if (!text.startsWith('{')) { this.log('agent', text, 'dim'); return; }
    let event;
    try { event = JSON.parse(text); } catch { this.log('agent', text, 'dim'); return; }
    this.applyEvent(event);
  }

  // ---- orchestrator event semantics -------------------------

  applyEvent(event) {
    const type = event.event || event.type;
    switch (type) {
      case 'run_created':
        this.state.runDir = event.run_dir;
        this.state.design = event.design || this.state.design;
        this.state.budgetUsd = event.budget_usd ?? this.state.budgetUsd;
        this.log('agent', `run directory  ${path.basename(event.run_dir)}`, 'dim');
        this.watchRun(event.run_dir);
        break;

      case 'stage_started':
        this.setStage(event.stage, { state: 'running', startedAt: Date.now() });
        this.log('agent', `${STAGE_LABEL[event.stage] || event.stage}…`, 'dim');
        break;

      case 'stage_completed':
        this.setStage(event.stage, {
          state: 'done', files: event.files || null,
          summary: event.summary || null, runtime: event.runtime_seconds ?? null,
        });
        this.log('agent', `${STAGE_LABEL[event.stage] || event.stage} done in ${Number(event.runtime_seconds || 0).toFixed(1)}s`, 'ok');
        this.onStageDone(event.stage);
        break;

      case 'stage_failed':
        this.setStage(event.stage, { state: 'failed', error: event.error, log: event.log });
        this.log('agent', `${STAGE_LABEL[event.stage] || event.stage} FAILED: ${event.error}`, 'crit');
        if (event.stage === 'heuristics') this.log('heuristics', `stage failed: ${event.error}`, 'crit');
        if (event.log) this.attachTail(event.log, 'agent');
        break;

      case 'analysis_ready':
        this.log('agent', 'initial analysis ready', 'ok');
        break;

      case 'launch_started':
        this.log('agent', 'starting Shinka runner', 'head');
        break;

      case 'shinka_launched':
        this.state.shinka.launched = true;
        this.state.shinka.pid = event.pid ?? null;
        this.state.shinka.results = event.results || null;
        this.state.shinka.log = event.log || null;
        this.state.shinka.vizCapable = Boolean(event.results);
        this.log('agent', 'Shinka started. Evaluating the seed; the tree opens when the first program is ready.', 'head');
        if (event.log) this.attachTail(event.log, 'agent');
        this.startShinkaPolling();
        break;

      case 'final_graph_ready':
        this.setStage('final_graph', { state: 'done', files: event.files || null, summary: event.summary || null });
        this.state.graph.final = true;
        this.state.graph.active = 'final';
        this.state.graph.generation += 1;
        this.log('agent', `final graph ready (timing_closed: ${event.timing_closed})`, 'ok');
        break;

      case 'final_graph_skipped':
        this.setStage('final_graph', { state: 'skipped', reason: event.reason });
        this.log('agent', `final graph skipped: ${event.reason}`, 'warn');
        break;

      case 'report_ready':
        this.loadReportFile();
        this.log('agent', `report ready  state ${event.state}  timing_closed ${event.timing_closed}`, 'ok');
        break;

      case 'tool_reports_ready':
        this.logToolReports(event);
        break;

      case 'artifact_ready':
        this.log('agent', `${event.label}: ${event.path}${event.error ? ` · ${event.error}` : ''}`, event.error ? 'warn report' : 'report');
        break;

      case 'finished':
        this.log('agent', 'Run finished.', 'head');
        break;

      case 'failed':
        this.state.error = event.error || 'run failed';
        this.log('agent', `FAILED: ${this.state.error}`, 'crit');
        break;

      default:
        break;      // an event the UI has no panel for is not worth a log line
    }
    this.push();
  }

  setStage(name, patch) {
    if (!STAGES.includes(name)) return;
    this.state.stages[name] = { ...this.state.stages[name], ...patch };
  }

  onStageDone(stage) {
    if (stage === 'graph') {
      this.state.graph.initial = true;
      if (!this.state.graph.active) this.state.graph.active = 'initial';
      this.state.graph.generation += 1;
    }
    if (stage === 'heuristics') this.emitHeuristics();
    if (stage === 'final_graph') { this.state.graph.final = true; this.state.graph.active = 'final'; }
  }

  emitHeuristics() {
    if (!this.state.runDir) return;
    const result = digest(this.state.runDir);
    if (!result) { this.log('heuristics', 'analysis.json unavailable', 'crit'); return; }
    this.logs.heuristics = [];
    this.emit('reset-pane', 'heuristics');
    this.log('heuristics', result.lines);
  }

  // ---- run-directory watchers --------------------------------------------

  watchRun(runDir) {
    const heuristicsLog = path.join(runDir, 'heuristics.log');
    const graphLog = path.join(runDir, 'graph.log');
    // Stage logs only matter when something goes wrong; the digest replaces the
    // raw heuristics log in the pane once the stage succeeds.
    this.attachTail(graphLog, 'agent', 'stage');
    this.attachTail(heuristicsLog, 'agent', 'stage');
  }

  // `mode` decides how much of a log file reaches the pane:
  //   runner : the Shinka log, condensed to one line per real event
  //   stage  : a stage log, which only matters when it reports a failure
  attachTail(file, pane, mode = 'runner') {
    if (!file) return;
    if (this.tails.some((t) => t.file === file)) return;
    const tail = new Tail(file, (lines) => {
      const entries = mode === 'stage'
        ? lines.map((text) => cleanStderr(text)).filter((e) => e && e.cls === 'crit')
        : this.cleaner.lines(lines);
      if (entries.length) this.log(pane, entries);
    });
    this.tails.push(tail.start());
  }

  startShinkaPolling() {
    const results = this.state.shinka.results;
    if (!results || this.pollTimer) return;
    const read = () => {
      this.collectReports();
      const status = readJson(path.join(results, 'status.json'));
      const progress = readJson(path.join(results, 'live_progress.json'));
      let changed = false;
      if (status && JSON.stringify(status) !== JSON.stringify(this.state.shinka.status)) {
        this.state.shinka.status = status;
        const costs = [status.budget_accounted_cost, status.reported_api_cost, status.native_reported_cost]
          .filter((value) => typeof value === 'number' && Number.isFinite(value));
        const cost = costs.length ? Math.max(...costs) : null;
        if (cost !== null) this.state.cost = cost;
        changed = true;
      }
      if (progress && JSON.stringify(progress) !== JSON.stringify(this.state.shinka.progress)) {
        this.state.shinka.progress = progress;
        if (progress.state && progress.state !== this.lastProgressState) {
          this.lastProgressState = progress.state;
          this.log('agent', `${progress.state}${progress.proposal ? `  ${progress.proposal}` : ''}`, 'dim');
        }
        changed = true;
      }
      const equivalence = collectEquivalence(results);
      if (equivalence && JSON.stringify(equivalence) !== JSON.stringify(this.state.shinka.equivalence)) {
        for (const generation of equivalence.generations) {
          if (this.generationPhases.get(generation.generation) === generation.phase) continue;
          this.generationPhases.set(generation.generation, generation.phase);
          let text = `Generation ${generation.generation}: ${generation.phase}`;
          if (generation.evaluated && generation.correct) text += ` · WNS ${generation.wns} ns · TNS ${generation.tns} ns`;
          if (generation.evaluated && !generation.correct) text += ` · ${generation.error || 'see evaluator details'}`;
          this.log('agent', text, generation.correct === false ? 'warn' : generation.correct ? 'ok' : 'dim');
        }
        this.state.shinka.equivalence = equivalence;
        changed = true;
      }
      if (changed) this.push();
    };
    read();
    this.pollTimer = setInterval(read, 2000);
  }

  loadReportFile() {
    if (!this.state.runDir) return;
    const report = loadReport(this.state.runDir);
    if (report) {
      this.state.report = report;
      if (report.cost?.accounted !== null && report.cost?.accounted !== undefined) this.state.cost = report.cost.accounted;
      else if (report.cost?.reported != null) this.state.cost = report.cost.reported;
    }
  }

  logToolReports({ label, files }) {
    for (const [key, file] of Object.entries(files || {})) {
      if (!REPORT_LABELS[key] || typeof file !== 'string' || this.reportPaths.has(file) || !fs.existsSync(file)) continue;
      this.reportPaths.add(file);
      this.log('agent', `${label || 'Report'} · ${REPORT_LABELS[key]}: ${file}`, 'report');
    }
  }

  collectReports() {
    for (const entry of collectToolReports(this.state.shinka.results)) this.logToolReports(entry);
    if (this.state.runDir) this.logToolReports({ label: 'Run reports', files: readJson(path.join(this.state.runDir, 'reports/index.json'))?.files });
  }

  // ---- terminal states ----------------------------------------------------

  finish() {
    this.collectReports();
    this.stopWatchers();
    this.state.logsKept = this.logsKept;
    if (this.state.shinka.results) {
      const equivalence = collectEquivalence(this.state.shinka.results);
      if (equivalence) this.state.shinka.equivalence = equivalence;
    }
    if (!this.state.report) this.loadReportFile();
    this.state.status = 'finished';
    this.state.finishedAt = Date.now();
    this.push();
  }

  fail(message) {
    this.collectReports();
    this.stopWatchers();
    this.state.status = 'failed';
    this.state.error = message;
    this.state.finishedAt = Date.now();
    this.log('agent', message, 'crit');
    this.push();
  }

  cancel() {
    // The orchestrator starts Shinka in its own session. Interrupting only the
    // orchestrator leaves the optimizer running (and spending) after Stop.
    const pid = this.state.shinka.pid;
    if (this.busy && Number.isInteger(pid) && pid > 1) {
      try { process.kill(-pid, 'SIGINT'); this.log('agent', 'Cancel requested for the Shinka process group.', 'warn'); }
      catch (error) { if (error.code !== 'ESRCH') this.log('agent', `Could not stop Shinka: ${error.message}`, 'crit'); }
    }
    if (this.child) {
      try {
        if (process.platform !== 'win32' && Number.isInteger(this.child.pid) && this.child.pid > 1) process.kill(-this.child.pid, 'SIGINT');
        else this.child.kill('SIGINT');
      } catch (error) { if (error.code !== 'ESRCH') this.log('agent', `Could not stop analysis: ${error.message}`, 'crit'); }
      this.log('agent', 'cancel requested (SIGINT sent to orchestrator)', 'warn');
    }
    if (this.state.replay || this.state.dev) { this.stopWatchers(); this.finish(); }
  }

  // ---- run logs -----------------------------------------------------------

  // Only `.log` files: the JSON artifacts the report and panels read stay.
  logFiles(runDir) {
    const dir = runDir || this.state.runDir;
    if (!dir || !fs.existsSync(dir)) return [];
    const out = [];
    const walk = (base, depth) => {
      let entries = [];
      try { entries = fs.readdirSync(base, { withFileTypes: true }); } catch { return; }
      for (const entry of entries) {
        const full = path.join(base, entry.name);
        if (entry.isDirectory() && depth > 0) walk(full, depth - 1);
        else if (entry.isFile() && /\.log$/.test(entry.name)) out.push(full);
      }
    };
    walk(dir, 3);
    return out;
  }

  // Writes what the panes are showing next to the run, and keeps the raw logs.
  keepLogs() {
    const dir = this.state.runDir;
    if (!dir) throw new Error('No run to save logs for');
    const render = (rows) => rows.map((line) => line.text).join('\n');
    const file = path.join(dir, 'cogito-ui-log.txt');
    fs.writeFileSync(file, [
      `Cogito run log  ${dir}`,
      `design: ${this.state.design || 'n/a'}    status: ${this.state.status}`,
      '', '==== Heuristics ====', render(this.logs.heuristics),
      '', '==== Shinka ====', render(this.logs.agent), '',
    ].join('\n'));
    this.logsKept = true;
    this.state.logsKept = true;
    this.push();
    const bytes = this.logFiles(dir).reduce((sum, f) => sum + (fs.statSync(f).size || 0), 0);
    return { file, keptBytes: bytes };
  }

  // The default outcome: discard the raw logs of the finished run. Saving protects
  // them from the automatic sweep, but never from an explicit Discard.
  purgeLogs(runDir, { force = false } = {}) {
    const dir = runDir || this.state.runDir;
    if (!dir || (this.logsKept && !force)) return { removed: 0, bytes: 0 };
    let removed = 0;
    let bytes = 0;
    for (const file of this.logFiles(dir)) {
      try { bytes += fs.statSync(file).size; fs.unlinkSync(file); removed += 1; } catch { /* already gone */ }
    }
    if (removed && dir === this.state.runDir) {
      this.logsKept = false;
      this.state.logsKept = false;
      this.push();
    }
    return { removed, bytes };
  }

  // ---- replay -------------------------------------------------------------

  // Streams a completed run directory through the identical event path so every
  // pane can be exercised without starting a paid campaign. No data is invented:
  // anything the old run did not produce simply does not appear.
  replayRun(runDir) {
    if (this.busy) throw new Error('A run is already in progress');
    const dir = path.resolve(runDir);
    if (!fs.existsSync(dir)) throw new Error(`No such run directory: ${dir}`);

    this.stopWatchers();
    this.clearLogs();
    this.state = emptyState();
    this.state.status = 'running';
    this.state.mode = 'replay';
    this.state.replay = true;
    this.state.startedAt = Date.now();

    const handoff = readJson(path.join(dir, 'handoff.json')) || {};
    const ui = readJson(path.join(dir, 'ui_outputs.json')) || {};
    const outputs = ui.outputs || {};
    const shinkaDir = path.join(dir, 'shinka');
    const script = [];

    script.push({ event: 'run_created', run_dir: dir, design: handoff.design || null, budget_usd: handoff.total_budget_usd || null });
    if (fs.existsSync(path.join(dir, 'graph/graph.json'))) {
      script.push({ event: 'stage_started', stage: 'graph' });
      script.push({ event: 'stage_completed', stage: 'graph', files: outputs.graph?.files, summary: outputs.graph?.summary, runtime_seconds: outputs.graph?.runtime_seconds });
    }
    if (fs.existsSync(path.join(dir, 'heuristics/analysis.json'))) {
      script.push({ event: 'stage_started', stage: 'heuristics' });
      script.push({ event: 'stage_completed', stage: 'heuristics', files: outputs.heuristics?.files, summary: outputs.heuristics?.summary, runtime_seconds: outputs.heuristics?.runtime_seconds });
    }
    script.push({ event: 'analysis_ready', manifest: path.join(dir, 'ui_outputs.json') });

    if (fs.existsSync(shinkaDir)) {
      const status = readJson(path.join(shinkaDir, 'status.json')) || {};
      script.push({ event: 'launch_started' });
      script.push({ event: 'shinka_launched', pid: status.pid ?? null, results: shinkaDir, log: path.join(dir, 'shinka.launch.log') });
    }
    if (fs.existsSync(path.join(dir, 'final/graph/graph.json'))) {
      script.push({ event: 'stage_started', stage: 'final_graph' });
      script.push({ event: 'final_graph_ready', files: { json: path.join(dir, 'final/graph/graph.json') }, timing_closed: null });
    } else {
      script.push({ event: 'final_graph_skipped', reason: 'this run predates the final-graph stage', state: 'replay' });
    }
    if (loadReport(dir)) script.push({ event: 'report_ready', report: path.join(dir, 'final_report.json'), state: 'replay', timing_closed: null });
    script.push({ event: 'finished', launched: fs.existsSync(shinkaDir) });

    this.log('agent', `REPLAY  ${dir}`, 'head');
    this.log('agent', 'Replaying a completed run. No process is started and nothing is billed.', 'warn');

    let index = 0;
    const step = () => {
      if (index >= script.length) {
        // The shinka log tail runs from the start of the file in replay, so give
        // it a moment to flush before the run is marked finished.
        this.replayTimer = setTimeout(() => { this.state.exitCode = 0; this.finish(); }, 1200);
        return;
      }
      this.applyEvent(script[index++]);
      this.replayTimer = setTimeout(step, 450);
    };
    this.replayTimer = setTimeout(step, 200);
    this.push();
    return this.state;
  }
}

module.exports = { RunManager };
