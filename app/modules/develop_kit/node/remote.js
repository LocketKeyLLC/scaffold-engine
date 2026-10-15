// §17.1413 — scaffold-engine's remote kit: read, write and restart things on ANOTHER machine
// over ssh, from inside a service. Engine-owned and tested once, so a developed feature never
// rebuilds this with nested shell quoting (every ADD122 round broke it a new way).
//
//   const remote = require('./scaffold-kit/remote');
//   const host = { address: '10.0.0.6', user: 'operator' };
//   // in a request handler -- ALWAYS the promises form (§17.1598):
//   const text = await remote.promises.readFile(host, '/path/on/that/machine');      // sudo cat
//   await remote.promises.writeFile(host, '/path', text, { owner: 'steam:steam' });   // sudo tee, content on STDIN
//   const state = await remote.promises.unit(host, 'is-active', 'palworld.service');  // sudo systemctl
//   const out = await remote.promises.run(host, ['pct', 'list']);                     // a read, no sudo
//
// §17.1598 — the plain forms (`remote.readFile(...)`) block the whole Node process until ssh
// returns: every other request the service holds waits behind it. Live (c2792d4b), a route made
// three blocking ssh calls to an address with no ssh, 15 s each, and every section of the
// control panel sat on "Loading…" for 45 s on every page load. They stay only so code already
// delivered keeps working; the develop flow refuses a NEW use of them in a server.
//
// Every argument is quoted for the remote shell here; content travels on stdin, never in a
// command line. Errors throw (or reject) with the remote stderr. A machine that does not answer
// fails in CONNECT_TIMEOUT_S seconds, not in the whole call's timeout.
'use strict';
const { execFileSync, execFile } = require('child_process');

const CONNECT_TIMEOUT_S = 5;

function q(arg) {
  return "'" + String(arg).replace(/'/g, "'\\''") + "'";
}

function sshArgs(host, argv) {
  if (!host || !host.address || !host.user) throw new Error('remote: host needs {address, user}');
  return ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', `ConnectTimeout=${CONNECT_TIMEOUT_S}`,
          `${host.user}@${host.address}`, argv.map(q).join(' ')];
}

function failure(host, argv, stderr, err) {
  const why = (stderr && String(stderr).trim()) || (err && err.message) || 'failed';
  return new Error(`remote ${argv.slice(0, 3).join(' ')} on ${host.user}@${host.address} failed: ${why}`);
}

// ── the blocking forms: kept for code already delivered ──

function run(host, argv, input, timeoutMs) {
  const args = sshArgs(host, argv);
  try {
    return execFileSync('ssh', args, { input: input === undefined ? '' : input, encoding: 'utf8',
                                       timeout: timeoutMs || 30000 });
  } catch (err) {
    throw failure(host, argv, err.stderr, err);
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

function checkVerb(verb) {
  if (!['start', 'stop', 'restart', 'is-active'].includes(verb)) throw new Error(`remote.unit: ${verb} not allowed`);
}

function unit(host, verb, name) {
  checkVerb(verb);
  return run(host, ['sudo', 'systemctl', verb, name]).trim();
}

// ── §17.1598: the non-blocking forms, for anything a request handler calls ──

function runAsync(host, argv, opts) {
  const o = opts || {};
  return new Promise((resolve, reject) => {
    let args;
    try { args = sshArgs(host, argv); } catch (e) { reject(e); return; }
    const child = execFile('ssh', args, { encoding: 'utf8', timeout: o.timeoutMs || 30000, maxBuffer: 16 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) reject(failure(host, argv, stderr, err));
        else resolve(stdout);
      });
    child.stdin.on('error', () => {});            // ssh may exit before reading its input
    child.stdin.end(o.input === undefined ? '' : String(o.input));
  });
}

const promises = {
  run: (host, argv, opts) => runAsync(host, argv, opts),
  readFile: (host, path) => runAsync(host, ['sudo', 'cat', path]),
  exists: (host, path) => runAsync(host, ['sudo', 'test', '-e', path]).then(() => true, () => false),
  writeFile: async (host, path, content, opts) => {
    await runAsync(host, ['sudo', 'tee', path], { input: String(content) });
    if (opts && opts.owner) await runAsync(host, ['sudo', 'chown', opts.owner, path]);
  },
  unit: async (host, verb, name) => {
    checkVerb(verb);
    return (await runAsync(host, ['sudo', 'systemctl', verb, name])).trim();
  },
};

module.exports = { readFile, writeFile, exists, unit, promises, _quote: q, CONNECT_TIMEOUT_S };
