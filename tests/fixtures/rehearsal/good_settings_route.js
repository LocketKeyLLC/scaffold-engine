// A CORRECT settings route, used only to prove the rehearsal says yes to good work.
const { execFileSync } = require('child_process');
const TARGET = 'aedefruscio@192.168.1.106';
const LIVE = '/opt/palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini';
const TEMPLATE = '/opt/palworld/DefaultPalWorldSettings.ini';
const HEADER = '[/Script/Pal.PalGameWorldSettings]';
const ssh = (cmd, input) => execFileSync('ssh', ['-o', 'BatchMode=yes', TARGET, cmd], { input, encoding: 'utf8' });

function parse(text) {
  const m = text.indexOf('OptionSettings=(');
  if (m < 0) return null;
  let i = m + 'OptionSettings=('.length, depth = 1, start = i;
  while (i < text.length && depth) { if (text[i] === '(') depth++; else if (text[i] === ')') depth--; i++; }
  const body = text.slice(start, i - 1), out = {};
  let buf = '', d = 0, q = false;
  for (const ch of body + ',') {
    if (ch === '"') q = !q;
    if (!q && ch === '(') d++;
    if (!q && ch === ')') d--;
    if (!q && d === 0 && ch === ',') { const e = buf.indexOf('='); if (e > 0) out[buf.slice(0, e).trim()] = buf.slice(e + 1).trim(); buf = ''; continue; }
    buf += ch;
  }
  return out;
}
const read = () => parse(ssh(`cat ${LIVE}`)) || parse(ssh(`cat ${TEMPLATE}`)) || {};
module.exports = function (app) {
  app.get('/api/palworld/settings', (req, res) => res.json({ settings: read() }));
  app.put('/api/palworld/settings', (req, res) => {
    const next = { ...read(), ...((req.body && req.body.settings) || {}) };
    const body = `${HEADER}\nOptionSettings=(${Object.entries(next).map(([k, v]) => `${k}=${v}`).join(',')})\n`;
    ssh('sudo systemctl stop palworld.service');
    ssh(`sudo tee ${LIVE} >/dev/null`, body);
    ssh('sudo systemctl start palworld.service');
    res.json({ settings: read() });
  });
};
