'use strict';
const fs = require('fs');

// Polling tailer: the orchestrator and Shinka write their logs from separate
// processes, so inotify via fs.watch is unreliable across filesystems here.
// Reading from a byte offset every interval is slower but never misses a write.
class Tail {
  constructor(file, onLines, { interval = 400, fromStart = true } = {}) {
    this.file = file;
    this.onLines = onLines;
    this.interval = interval;
    this.offset = fromStart ? 0 : null;
    this.partial = '';
    this.stopped = false;
    this.timer = null;
  }

  start() {
    if (this.timer) return this;
    const tick = () => {
      if (this.stopped) return;
      this.read().catch(() => {}).finally(() => {
        if (!this.stopped) this.timer = setTimeout(tick, this.interval);
      });
    };
    tick();
    return this;
  }

  async read() {
    let stat;
    try { stat = await fs.promises.stat(this.file); } catch { return; }
    if (this.offset === null) this.offset = stat.size;
    if (stat.size < this.offset) { this.offset = 0; this.partial = ''; }  // truncated/rotated
    if (stat.size === this.offset) return;
    const handle = await fs.promises.open(this.file, 'r');
    try {
      const length = stat.size - this.offset;
      const buffer = Buffer.alloc(length);
      const { bytesRead } = await handle.read(buffer, 0, length, this.offset);
      this.offset += bytesRead;
      const text = this.partial + buffer.subarray(0, bytesRead).toString('utf8');
      const lines = text.split('\n');
      this.partial = lines.pop();
      const clean = lines.map((l) => l.replace(/\r$/, ''));
      if (clean.length) this.onLines(clean);
    } finally {
      await handle.close();
    }
  }

  stop() {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }
}

module.exports = { Tail };
