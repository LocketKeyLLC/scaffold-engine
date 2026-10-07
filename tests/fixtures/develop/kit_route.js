// A feature written ON the kit -- used only to prove template + kit + sandbox compose.
const express = require('express');
const remote = require('/opt/control-panel-backend/scaffold-kit/remote');
const ue = require('/opt/control-panel-backend/scaffold-kit/ue_settings');
const host = { address: '192.168.1.106', user: 'aedefruscio' };
const LIVE = '/opt/palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini';
const TEMPLATE = '/opt/palworld/DefaultPalWorldSettings.ini';
const read = () => ue.parse(remote.readFile(host, LIVE)) || ue.parse(remote.readFile(host, TEMPLATE));
module.exports = function (app) {
  app.get('/api/palworld-settings', (req, res) => res.json(read()));
  app.put('/api/palworld-settings', express.json(), (req, res) => {
    const cur = read();
    const next = { header: cur.header, settings: { ...cur.settings, ...((req.body && req.body.settings) || {}) } };
    remote.unit(host, 'stop', 'palworld.service');
    remote.writeFile(host, LIVE, ue.serialize(next), { owner: 'steam:steam' });
    remote.unit(host, 'start', 'palworld.service');
    res.json(read());
  });
};
