#!/usr/bin/env bash
# Run this step's commands inside container 111 as root.
set -uo pipefail
GID=111
pct status "$GID" | grep -q running || pct start "$GID"
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do pct status "$GID" | grep -q running && break; sleep 5; done
cat > /tmp/in_ct_111_remote.sh <<'REMOTE'
set -e
export DEBIAN_FRONTEND=noninteractive
[ -e "/opt/control-panel-backend/routes/palworld-settings.js" ] && cp -a "/opt/control-panel-backend/routes/palworld-settings.js" "/opt/control-panel-backend/routes/palworld-settings.js.bak.$(date +%Y%m%d%H%M%S)"
cat > /opt/control-panel-backend/routes/palworld-settings.js <<'EOF'
const express = require('express');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const router = express.Router();
const PALWORLD_IP = '192.168.1.106';
const PALWORLD_USER = 'aedefruscio';
const PALWORLD_CONFIG_PATH = '/home/aedefruscio/Steam/steamapps/common/PalServer/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini';
function sshExec(args) {
  return execFileSync('ssh', [
    '-o', 'StrictHostKeyChecking=no',
    '-o', 'UserKnownHostsFile=/dev/null',
    `${PALWORLD_USER}@${PALWORLD_IP}`,
    ...args
  ], { encoding: 'utf8' });
}
function parseSettings(content) {
  const settings = {};
  const match = content.match(/\[\/Script\/Pal\.PalGameWorldSettings\]\s*\nOptionSettings=\((.*)\)/s);
  if (!match) return settings;
  const pairs = match[1].split(',');
  for (const pair of pairs) {
    const [key, ...valueParts] = pair.split('=');
    if (key && valueParts.length) {
      settings[key.trim()] = valueParts.join('=').trim();
    }
  }
  return settings;
}
function serializeSettings(settings) {
  const pairs = Object.entries(settings).map(([k, v]) => `${k}=${v}`).join(',');
  return `[/Script/Pal.PalGameWorldSettings]\nOptionSettings=(${pairs})\n`;
}
router.get('/', (req, res) => {
  try {
    const content = sshExec(['cat', PALWORLD_CONFIG_PATH]);
    res.json(parseSettings(content));
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});
router.put('/', (req, res) => {
  try {
    const newSettings = req.body;
    const content = sshExec(['cat', PALWORLD_CONFIG_PATH]);
    const current = parseSettings(content);
    const merged = { ...current, ...newSettings };
    const serialized = serializeSettings(merged);
    sshExec(['tee', PALWORLD_CONFIG_PATH], { input: serialized });
    res.json(merged);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});
module.exports = router;
EOF
[ -e "/opt/control-panel-backend/server.js" ] && cp -a "/opt/control-panel-backend/server.js" "/opt/control-panel-backend/server.js.bak.$(date +%Y%m%d%H%M%S)"
cat > /opt/control-panel-backend/server.js <<'EOF'
const express = require('express');
const fs = require('fs');
const path = require('path');
const app = express();
const PORT = process.env.PORT || 3001;
const CONFIG_PATH = path.join(__dirname, 'config', 'capabilities.json');
app.use(express.json());
function loadCapabilities() {
  try {
    const raw = fs.readFileSync(CONFIG_PATH, 'utf8');
    return JSON.parse(raw).capabilities || [];
  } catch (err) {
    return [];
  }
}
app.get('/api/capabilities', (req, res) => {
  res.json({ capabilities: loadCapabilities() });
});
app.get('/api/health', (req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
});
// Mount palworld-settings route if capability enabled
const capabilities = loadCapabilities();
if (capabilities.some(c => c.id === 'palworld-settings' && c.enabled)) {
  const palworldRouter = require('./routes/palworld-settings');
  app.use('/api/palworld-settings', palworldRouter);
}
app.listen(PORT, () => {
  console.log(`Control panel backend listening on port ${PORT}`);
});
EOF
mkdir -p /opt/control-panel-backend/routes
REMOTE

# §17.1317 — long work runs DETACHED inside the guest as a transient systemd unit and is
# waited on across several commands, each inside the runner's 180 s budget. Live, T23's
# SteamCMD install (several GB) could never fit one command; `qm guest exec --timeout 110`
# would have cut it the same way ssh did.
UNIT="scaffold-ADD122"
ginfo() { pct exec "$GID" -- bash -c "$1" 2>/dev/null; }
case "${1:-start}" in
  start)
    pct push "$GID" /tmp/in_ct_111_remote.sh /root/.scaffold_step.sh >/dev/null || { echo "FAILED: pct push into $GID"; exit 1; }
    ginfo "systemctl stop $UNIT 2>/dev/null; systemctl reset-failed $UNIT 2>/dev/null; rm -f /root/.scaffold_step.log /root/.scaffold_step.err /root/.scaffold_step.reported; true" >/dev/null
    ginfo "systemd-run --unit $UNIT --collect -p StandardOutput=append:/root/.scaffold_step.log -p StandardError=append:/root/.scaffold_step.err bash /root/.scaffold_step.sh" >/dev/null \
      || { echo "FAILED: could not start $UNIT inside $GID (is systemd running in the guest?)"; exit 1; }
    echo "started $UNIT inside $GID"
    ;;&
  start|wait|last)
    deadline=$(( SECONDS + 165 ))
    while [ "$SECONDS" -lt "$deadline" ]; do
      state="$(ginfo "systemctl is-active $UNIT" | tr -d '[:space:]')"
      case "$state" in
        active|activating|reloading|deactivating) sleep 5 ;;
        *) break ;;
      esac
    done
    state="$(ginfo "systemctl is-active $UNIT" | tr -d '[:space:]')"
    if [ "$state" = "active" ] || [ "$state" = "activating" ]; then
      if [ "${1:-start}" = "last" ]; then echo "FAILED: $UNIT is still running after the whole wait budget; the step did not finish"; ginfo "tail -n 20 /root/.scaffold_step.log"; exit 1; fi
      echo "STILL RUNNING: $UNIT inside $GID (waited 165 s more; the next command keeps waiting)"; ginfo "tail -n 3 /root/.scaffold_step.log"; exit 0
    fi
    result="$(ginfo "systemctl show -p Result --value $UNIT" | tr -d '[:space:]')"
    code="$(ginfo "systemctl show -p ExecMainStatus --value $UNIT" | tr -d '[:space:]')"
    # §17.1327 -- the log belongs to the phase that SAW the unit finish. Live, T23's eight
    # wait phases each re-printed the whole SteamCMD tail; the record carried it eight times.
    if [ -z "$(ginfo "cat /root/.scaffold_step.reported 2>/dev/null")" ]; then
      ginfo "echo done > /root/.scaffold_step.reported" >/dev/null
      ginfo "cat /root/.scaffold_step.log 2>/dev/null | tail -n 60"
      ginfo "cat /root/.scaffold_step.err 2>/dev/null | tail -n 20" >&2
    else
      echo "(the log was printed by the phase that saw $UNIT finish)"
    fi
    if [ "$result" = "success" ] || { [ -z "$result" ] && [ "$code" = "0" ]; }; then echo "$UNIT finished (exit ${code:-0})"; exit 0; fi
    echo "FAILED: $UNIT ended with Result=$result exit=${code:-?}"; exit "${code:-1}"
    ;;
  *) echo "unknown phase: $1"; exit 2;;
esac

