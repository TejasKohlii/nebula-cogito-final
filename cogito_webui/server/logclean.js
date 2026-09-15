'use strict';
/* Turn the raw runner log into something a person can follow.
 *
 * The Shinka runner writes a banner, rich tables and one INFO record per internal
 * step, hard-wrapped at 80 columns. Almost none of that is meaningful to the
 * operator: what matters is which generation was evaluated, whether it was
 * correct, what it scored, what it cost, and anything that failed. This module
 * rejoins the wrapped records, drops the decoration and the bookkeeping, and
 * rewrites the survivors as one short line each.
 */

const RECORD = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - ([\w.]+) - (\w+) - ([\s\S]*)$/;
// Box drawing, block elements, the banner's @-art: a line made only of these is decoration.
const DECOR = /^[\s@|+*=~^_'`.\-─-╿▀-▟░-▓■-◿⠀-⣿]*$/u;
const BOXY = /[─-╿▀-▟]/u;

// Bookkeeping the operator never needs to see.
const DROP_LOGGER = /^(shinka\.launch\.local|shinka\.database\.dbase|urllib3|httpx|asyncio|filelock)/;
const DROP_TEXT = [
  /^(🔄 SAFE PROCESSING|📂 RESULTS|✅ DB SUCCESS|💾 DB ADD|📊 JOB STATUS|🧹|🔍 CHECKING)/,
  /Monitoring local process/i,
  /^Pricing catalog:/i,
  /^sha256=/i,
  /^source=live/i,
  /Process \d+ completed with return code: 0/i,
  /^Starting job /i,
  /^Got results for/i,
  /^Running with \d+ parallel/i,
  /^Using (cached|existing)/i,
  /Ignoring invalid pricing snapshot/i,
  /^Loaded \d+ programs/i,
];

// Records worth keeping, rewritten into a short line.
const RULES = [
  { re: /New best program:\s*\S+\s*\(gen: (\d+) → (\d+), score: ([\d.]+) → ([\d.]+)/,
    out: (m) => ({ text: `new best  gen ${m[2]}  score ${Number(m[4]).toFixed(4)} (was ${Number(m[3]).toFixed(4)})`, cls: 'ok' }) },
  { re: /VALID RESULTS:.*correct=(\w+),\s*score=([\d.]+)/,
    out: (m) => ({ text: `evaluated  correct=${m[1]}  score=${Number(m[2]).toFixed(4)}`, cls: m[1] === 'True' ? 'ok' : 'warn' }) },
  { re: /completed jobs:\s*gens \[([^\]]*)\]\s*\(cost: \$([\d.]+),\s*([\d.]+)%\)/,
    out: (m) => ({ text: `generation ${m[1]} done   spent $${m[2]}  (${m[3]}% of budget)`, cls: 'head' }) },
  { re: /INVALID RESULTS|has invalid results|correct=False/,
    out: () => ({ text: 'candidate rejected: evaluation was not correct', cls: 'warn' }) },
  { re: /^(Budget|budget) (exhausted|reached|limit)/, out: (m, text) => ({ text, cls: 'warn' }) },
  { re: /Shutting down|Search (complete|finished)|Evolution (complete|finished)/,
    out: (m, text) => ({ text, cls: 'head' }) },
];

function classify(level) {
  if (level === 'ERROR' || level === 'CRITICAL') return 'crit';
  if (level === 'WARNING') return 'warn';
  return '';
}

class AgentLogCleaner {
  constructor() { this.pending = null; this.last = null; }

  // One raw line in, zero or one clean entries out.
  push(raw) {
    const line = String(raw).replace(/\s+$/, '');
    const match = RECORD.exec(line);
    if (match) {
      const done = this.flush();
      this.pending = { logger: match[2], level: match[3], text: match[4].trim() };
      return done;
    }
    if (!line || DECOR.test(line) || BOXY.test(line)) return null;   // banner, tables, rules
    if (this.pending) {
      // A wrapped continuation of the record above.
      this.pending.text = `${this.pending.text} ${line.trim()}`.replace(/\s+/g, ' ');
      return null;
    }
    return this.plain(line);
  }

  // Lines with no log record around them (evaluator stdout, tracebacks).
  plain(line) {
    const text = line.trim();
    if (!text || text.length > 400) return null;
    if (/^(Traceback|\s+File ")/.test(line)) return { text, cls: 'crit' };
    if (/error|failed|exception/i.test(text)) return { text, cls: 'crit' };
    return null;
  }

  flush() {
    const record = this.pending;
    this.pending = null;
    if (!record) return null;
    if (DROP_LOGGER.test(record.logger)) return null;
    if (record.level === 'DEBUG') return null;
    if (DROP_TEXT.some((re) => re.test(record.text))) return null;
    for (const rule of RULES) {
      const m = rule.re.exec(record.text);
      if (m) return rule.out(m, record.text);
    }
    if (record.level === 'ERROR' || record.level === 'CRITICAL' || record.level === 'WARNING') {
      return { text: record.text.slice(0, 300), cls: classify(record.level) };
    }
    return null;      // an INFO record that matched no rule is internal detail
  }

  // Feed a batch; returns the clean entries, minus immediate repeats.
  lines(list) {
    const out = [];
    for (const raw of list) {
      const entry = this.push(raw);
      if (!entry || entry.text === this.last) continue;
      this.last = entry.text;
      out.push(entry);
    }
    return out;
  }
}

// The orchestrator's own stderr: keep failures and drop library chatter.
function cleanStderr(line) {
  const text = String(line).replace(/\s+$/, '').trim();
  if (!text) return null;
  if (/^(warnings\.warn|\s*from |\s*import |[A-Za-z_.]+Warning:)/.test(text)) return null;
  if (/DeprecationWarning|FutureWarning|UserWarning|resource_tracker/.test(text)) return null;
  if (/^(Traceback|\s+File ")/.test(text)) return { text, cls: 'crit' };
  if (/error|failed|exception|cannot|unable/i.test(text)) return { text, cls: 'crit' };
  return { text, cls: 'dim' };
}

module.exports = { AgentLogCleaner, cleanStderr };
