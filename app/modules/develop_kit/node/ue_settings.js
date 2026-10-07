// §17.1413 — scaffold-engine's kit for Unreal-engine server settings files (Palworld's
// PalWorldSettings.ini): one `[/Script/…]` header and one `OptionSettings=(k=v,k=v,…)` line,
// where a value can itself be `(A,B,C)` and strings are quoted. Engine-owned and tested once:
// every ADD122 round that wrote its own parser split on commas or dropped the header.
//
//   const ue = require('./scaffold-kit/ue_settings');
//   const doc = ue.parse(text);            // { header, settings: {k: v, …} } or null if no OptionSettings line
//   doc.settings.ServerName = '"My server"';
//   const text2 = ue.serialize(doc);       // header line + OptionSettings line, keys in their order
//   ue.parse(ue.serialize(doc)) deep-equals doc   (the round trip)
//
// Values are kept as their raw text (`"Default Palworld Server"`, `1.000000`, `(Steam,Xbox)`),
// so what is not changed is written back byte for byte.
'use strict';

function parse(text) {
  const src = String(text || '');
  let header = '';
  for (const line of src.split(/\r?\n/)) {
    const t = line.trim();
    if (t.startsWith('[') && t.endsWith(']')) { header = t; break; }
  }
  const at = src.indexOf('OptionSettings=(');
  if (at < 0) return null;
  let i = at + 'OptionSettings=('.length;
  const start = i;
  let depth = 1, quoted = false;
  for (; i < src.length && depth > 0; i++) {
    const ch = src[i];
    if (ch === '"') quoted = !quoted;
    else if (!quoted && ch === '(') depth++;
    else if (!quoted && ch === ')') depth--;
  }
  const body = src.slice(start, i - 1);
  const settings = {};
  let buf = '', d = 0, qd = false;
  for (const ch of body + ',') {
    if (ch === '"') qd = !qd;
    if (!qd && ch === '(') d++;
    if (!qd && ch === ')') d--;
    if (!qd && d === 0 && ch === ',') {
      const eq = buf.indexOf('=');
      if (eq > 0) settings[buf.slice(0, eq).trim()] = buf.slice(eq + 1).trim();
      buf = '';
      continue;
    }
    buf += ch;
  }
  return { header: header || '[/Script/Pal.PalGameWorldSettings]', settings };
}

function serialize(doc) {
  const header = (doc && doc.header) || '[/Script/Pal.PalGameWorldSettings]';
  const pairs = Object.entries((doc && doc.settings) || {}).map(([k, v]) => `${k}=${v}`);
  return `${header}\nOptionSettings=(${pairs.join(',')})\n`;
}

module.exports = { parse, serialize };
