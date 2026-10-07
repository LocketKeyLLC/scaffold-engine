const express = require('express');
const { execSync } = require('child_process');
const fs = require('fs');

const PALWORLD_IP = '192.168.1.106';
const PALWORLD_USER = 'aedefruscio';
const PALWORLD_CONFIG = '/opt/palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini';
const PALWORLD_DEFAULTS = '/opt/palworld/DefaultPalWorldSettings.ini';

function sshExec(cmd) {
  return execSync(`ssh -o BatchMode=yes -o StrictHostKeyChecking=yes ${PALWORLD_USER}@${PALWORLD_IP} '${cmd}'`, {
    encoding: 'utf8',
    timeout: 30000
  });
}

function ensureConfigExists() {
  const remoteCheck = `[ -s ${PALWORLD_CONFIG} ] || cp ${PALWORLD_DEFAULTS} ${PALWORLD_CONFIG}`;
  sshExec(remoteCheck);
}

function writeSettings(settings) {
  const serialized = serializeSettings(settings);
  const escaped = serialized.replace(/'/g, `'\\''`);
  sshExec(`systemctl stop palworld.service`);
  sshExec(`printf '%s' '${escaped}' > ${PALWORLD_CONFIG}`);
  sshExec(`systemctl start palworld.service`);
}
