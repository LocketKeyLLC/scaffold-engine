// §17.1413 — scaffold-engine's remote kit: read, write and restart things on ANOTHER machine
// over ssh, from inside a service. Engine-owned and tested once, so a developed feature never
// rebuilds this with nested shell quoting (every ADD122 round broke it a new way).
//
//   const remote = require('./scaffold-kit/remote');
//   const host = { address: '192.168.1.106', user: 'aedefruscio' };
//   const text = remote.readFile(host, '/path/on/that/machine');          // sudo cat
//   remote.writeFile(host, '/path/on/that/machine', text, { owner: 'steam:steam' });   // sudo tee, content on STDIN
//   remote.unit(host, 'restart', 'palworld.service');                      // sudo systemctl
//
// Every argument is quoted for the remote shell here; content travels on stdin, never in a
// command line. Errors throw with the remote stderr.
'use strict';
const { execFileSync } = require('child_process');

function q(arg) {
  return "'" + String(arg).replace(/'/g, "'\\''") + "'";
}

function run(host, argv, input, timeoutMs) {
  if (!host || !host.address || !host.user) throw new Error('remote: host needs {address, user}');
  const target = `${host.user}@${host.address}`;
  try {
    return execFileSync('ssh', ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', target,
                                argv.map(q).join(' ')],
                        { input: input === undefined ? '' : input, encoding: 'utf8', timeout: timeoutMs || 30000 });
  } catch (err) {
    const why = (err.stderr && String(err.stderr).trim()) || err.message;
    throw new Error(`remote ${argv.slice(0, 3).join(' ')} on ${target} failed: ${why}`);
  }
}

function readFile(host, path) {
  return run(host, ['sudo', 'cat', path]);
}

function exists(host, path) {
  try { run(host, ['sudo', 'test', '-e', path]); return true; } catch (e) { return false; }
}

function writeFile(host, path, content, opts) {
  run(host, ['sudo', 'tee', path], String(content));
  if (opts && opts.owner) run(host, ['sudo', 'chown', opts.owner, path]);
}

function unit(host, verb, name) {
  if (!['start', 'stop', 'restart', 'is-active'].includes(verb)) throw new Error(`remote.unit: ${verb} not allowed`);
  return run(host, ['sudo', 'systemctl', verb, name]).trim();
}

module.exports = { readFile, writeFile, exists, unit, _quote: q };
