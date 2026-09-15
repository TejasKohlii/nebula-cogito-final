'use strict';
const { spawn } = require('child_process');
const path = require('path');

const { ROOT, PYTHON, toolEnvironment } = require('./paths');

function prepareFolder(folder) {
  return new Promise((resolve, reject) => {
    const script = path.join(ROOT, 'sum_agent/design_prepare.py');
    const child = spawn(PYTHON, ['-B', script, path.resolve(folder)], {
      cwd: ROOT,
      shell: false,
      env: { ...process.env, ...toolEnvironment() },
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => { stdout += chunk.toString('utf8'); });
    child.stderr.on('data', (chunk) => { stderr += chunk.toString('utf8'); });
    child.on('error', (error) => reject(Object.assign(
      new Error(`Could not start design preparation: ${error.message}`), { status: 500 }
    )));
    child.on('close', (code) => {
      if (code !== 0) {
        const encoded = stderr.split('\n').find((line) => line.startsWith('COGITO_PREP_ERROR '));
        let message = 'RTL design preparation failed';
        if (encoded) {
          try { message = JSON.parse(encoded.slice('COGITO_PREP_ERROR '.length)); } catch { /* use fallback */ }
        } else if (stderr.trim()) {
          message = stderr.trim().split('\n').pop();
        }
        reject(Object.assign(new Error(message), { status: 400 }));
        return;
      }
      try {
        resolve(JSON.parse(stdout.trim()));
      } catch {
        reject(Object.assign(new Error('Design preparation returned an invalid result'), { status: 500 }));
      }
    });
  });
}

module.exports = { prepareFolder };
