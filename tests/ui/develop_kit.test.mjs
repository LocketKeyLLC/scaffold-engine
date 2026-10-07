// §17.1413 — the develop kit, tested once against the real Palworld template from VM 106.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';

const require = createRequire(import.meta.url);
const ue = require('../../app/modules/develop_kit/node/ue_settings.js');
const remote = require('../../app/modules/develop_kit/node/remote.js');
const TEMPLATE = readFileSync(new URL('./fixtures/DefaultPalWorldSettings.ini', import.meta.url), 'utf8');

test('the real template parses into all of its settings', () => {
  const doc = ue.parse(TEMPLATE);
  assert.equal(doc.header, '[/Script/Pal.PalGameWorldSettings]');
  assert.equal(Object.keys(doc.settings).length, 122);
  assert.equal(doc.settings.CrossplayPlatforms, '(Steam,Xbox,PS5,Mac)');   // a nested value stays one value
  assert.ok(doc.settings.ServerName.startsWith('"'));
});

test('the round trip is exact: parse(serialize(doc)) equals doc', () => {
  const doc = ue.parse(TEMPLATE);
  assert.deepEqual(ue.parse(ue.serialize(doc)), doc);
});

test('a change touches only its key, in place', () => {
  const doc = ue.parse(TEMPLATE);
  const order = Object.keys(doc.settings);
  doc.settings.ServerName = '"Changed"';
  const back = ue.parse(ue.serialize(doc));
  assert.equal(back.settings.ServerName, '"Changed"');
  assert.deepEqual(Object.keys(back.settings), order);
  assert.equal(back.settings.CrossplayPlatforms, '(Steam,Xbox,PS5,Mac)');
});

test('a file with no OptionSettings line is null (the live file is 1 byte)', () => {
  assert.equal(ue.parse('\n'), null);
  assert.equal(ue.parse(''), null);
});

test('quoted commas and parentheses stay inside their value', () => {
  const doc = ue.parse('[/Script/X]\nOptionSettings=(A="x, (y)",B=(1,2),C=3)\n');
  assert.deepEqual(doc.settings, { A: '"x, (y)"', B: '(1,2)', C: '3' });
});

test('remote quotes every argument for the remote shell', () => {
  assert.equal(remote._quote("a b"), "'a b'");
  assert.equal(remote._quote("it's"), "'it'\\''s'");
  assert.equal(remote._quote('$(rm -rf /)'), "'$(rm -rf /)'");
});

test('remote refuses a host without an address and user, and an unknown unit verb', () => {
  assert.throws(() => remote.readFile({}, '/x'), /needs \{address, user\}/);
  assert.throws(() => remote.unit({ address: 'h', user: 'u' }, 'disable', 'x'), /not allowed/);
});
