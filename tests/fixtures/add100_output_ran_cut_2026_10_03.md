<!-- runbook-template: run_in_container -->

## Run commands inside container 111

## Risk

Runs this step's commands as root inside container 111.

## Write these files

### /tmp/in_ct_111.sh
```bash
#!/usr/bin/env bash
# Run this step's commands inside container 111 as root.
set -uo pipefail
GID=111
pct status "$GID" | grep -q running || pct start "$GID"
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do pct status "$GID" | grep -q running && break; sleep 5; done
cat > /tmp/in_ct_111_remote.sh <<'REMOTE'
set -e
export DEBIAN_FRONTEND=noninteractive
tee /opt/control-panel-backend/package.json <<'EOF'
{
  "name": "control-panel-backend",
  "version": "1.0.0",
  "main": "server.js",
  "scripts": {
    "start": "node server.js"
  },
  "dependencies": {
    "express": "^4.18.2",
    "cors": "^2.8.5",
    "axios": "^1.6.0",
    "dotenv": "^16.3.1"
  }
}
EOF
tee /opt/control-panel-backend/server.js <<'EOF'
const express = require('express');
const cors = require('cors');
const axios = require('axios');
const fs = require('fs');
const path = require('path');
require('dotenv').config();
const app = express();
app.use(cors());
app.use(express.json());
app.use(express.static('/opt/control-panel-ui'));
const PALWORLD_IP = '192.168.1.106';
const PROWLARR_IP = '192.168.1.21';
const RADARR_IP = '192.168.1.22';
const SONARR_IP = '192.168.1.23';
const PIHOLE_IP = '192.168.1.130';
const SCAFFOLD_ENGINE_URL = process.env.SCAFFOLD_ENGINE_URL || 'http://192.168.1.110:8080';
const PIHOLE_API_KEY = process.env.PIHOLE_API_KEY || '';
// Palworld settings file path (mounted or accessible via SSH)
const PALWORLD_SETTINGS_PATH = process.env.PALWORLD_SETTINGS_PATH || '/mnt/palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini';
// Read Palworld settings
app.get('/api/palworld/settings', async (req, res) => {
  try {
    const content = fs.readFileSync(PALWORLD_SETTINGS_PATH, 'utf8');
    res.json({ content });
  } catch (err) {
    res.status(500).json({ error: 'Failed to read Palworld settings: ' + err.message });
  }
});
// Write Palworld settings
app.post('/api/palworld/settings', async (req, res) => {
  try {
    const { content } = req.body;
    if (!content) return res.status(400).json({ error: 'No content provided' });
    fs.writeFileSync(PALWORLD_SETTINGS_PATH, content, 'utf8');
    res.json({ success: true });
  } catch (err) {
    res.status(500).json({ error: 'Failed to write Palworld settings: ' + err.message });
  }
});
// Media request endpoint
app.post('/api/media/request', async (req, res) => {
  try {
    const { title, type } = req.body;
    if (!title) return res.status(400).json({ error: 'Title is required' });
    const targetIp = type === 'tv' ? SONARR_IP : RADARR_IP;
    const targetPort = type === 'tv' ? 8989 : 7878;
    const apiKey = type === 'tv' ? process.env.SONARR_API_KEY : process.env.RADARR_API_KEY;
    if (!apiKey) return res.status(500).json({ error: 'API key not configured for ' + type });
    const lookupUrl = `http://${targetIp}:${targetPort}/api/v3/lookup?term=${encodeURIComponent(title)}`;
    const lookupRes = await axios.get(lookupUrl, { headers: { 'X-Api-Key': apiKey } });
    if (!lookupRes.data || lookupRes.data.length === 0) {
      return res.status(404).json({ error: 'No results found for: ' + title });
    }
    const item = lookupRes.data[0];
    const addUrl = `http://${targetIp}:${targetPort}/api/v3/movie`;
    const addRes = await axios.post(addUrl, {
      title: item.title,
      qualityProfileId: 1,
      titleSlug: item.titleSlug,
      tmdbId: item.tmdbId,
      year: item.year,
      monitored: true,
      rootFolderPath: '/media/' + (type === 'tv' ? 'tv' : 'movies'),
      addOptions: { searchForMovie: true }
    }, { headers: { 'X-Api-Key': apiKey } });
    res.json({ success: true, added: addRes.data });
  } catch (err) {
    res.status(500).json({ error: 'Media request failed: ' + err.message });
  }
});
// Pi-hole stats
app.get('/api/pihole/stats', async (req, res) => {
  try {
    const summaryUrl = `http://${PIHOLE_IP}/admin/api.php?summary`;
    const summaryRes = await axios.get(summaryUrl);
    const topBlockedUrl = `http://${PIHOLE_IP}/admin/api.php?topBlocked&count=10`;
    const topBlockedRes = await axios.get(topBlockedUrl);
    const topClientsUrl = `http://${PIHOLE_IP}/admin/api.php?topClients&count=10`;
    const topClientsRes = await axios.get(topClientsUrl);
    res.json({
      summary: summaryRes.data,
      topBlocked: topBlockedRes.data,
      topClients: topClientsRes.data
    });
  } catch (err) {
    res.status(500).json({ error: 'Failed to fetch Pi-hole stats: ' + err.message });
  }
});
// Pi-hole per-client data
app.get('/api/pihole/clients', async (req, res) => {
  try {
    const clientsUrl = `http://${PIHOLE_IP}/admin/api.php?getClientNames`;
    const clientsRes = await axios.get(clientsUrl);
    res.json(clientsRes.data);
  } catch (err) {
    res.status(500).json({ error: 'Failed to fetch Pi-hole clients: ' + err.message });
  }
});
// Scaffold-engine proxy
app.get('/api/scaffold/status', async (req, res) => {
  try {
    const statusRes = await axios.get(SCAFFOLD_ENGINE_URL + '/api/status', { timeout: 5000 });
    res.json(statusRes.data);
  } catch (err) {
    res.status(502).json({ error: 'Scaffold engine unreachable: ' + err.message });
  }
});
app.post('/api/scaffold/run', async (req, res) => {
  try {
    const runRes = await axios.post(SCAFFOLD_ENGINE_URL
REMOTE
pct push "$GID" /tmp/in_ct_111_remote.sh /root/.scaffold_step.sh >/dev/null || { echo "FAILED: pct push into $GID"; exit 1; }
pct exec "$GID" -- bash /root/.scaffold_step.sh
```

## Run this

```bash
bash /tmp/in_ct_111.sh
```

## Verify

- `pct exec 111 -- bash -c "curl -s http://localhost:3001/api/capabilities"`

## Executed on pve-runner (supervised, operator-approved)
$ write /tmp/in_ct_111.sh (5404 bytes)
wrote /tmp/in_ct_111.sh (5404 bytes, 139 lines)
$ bash /tmp/in_ct_111.sh
{
  "name": "control-panel-backend",
  "version": "1.0.0",
  "main": "server.js",
  "scripts": {
    "start": "node server.js"
  },
  "dependencies": {
    "express": "^4.18.2",
    "cors": "^2.8.5",
    "axios": "^1.6.0",
    "dotenv": "^16.3.1"
  }
}
/root/.scaffold_step.sh: line 129: warning: here-document at line 19 delimited by end-of-file (wanted `EOF')
const express = require('express');
const cors = require('cors');
const axios = require('axios');
const fs = require('fs');
const path = require('path');
require('dotenv').config();
const app = express();
app.use(cors());
app.use(express.json());
app.use(express.static('/opt/control-panel-ui'));
const PALWORLD_IP = '192.168.1.106';
const PROWLARR_IP = '192.168.1.21';
const RADARR_IP = '192.168.1.22';
const SONARR_IP = '192.168.1.23';
const PIHOLE_IP = '192.168.1.130';
const SCAFFOLD_ENGINE_URL = process.env.SCAFFOLD_ENGINE_URL || 'http://192.168.1.110:8080';
const PIHOLE_API_KEY = process.env.PIHOLE_API_KEY || '';
// Palworld settings file path (mounted or accessible via SSH)
const PALWORLD_SETTINGS_PATH = process.env.PALWORLD_SETTINGS_PATH || '/mnt/palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini';
// Read Palworld settings
app.get('/api/palworld/settings', async (req, res) => {
  try {
    const content = fs.readFileSync(PALWORLD_SETTINGS_PATH, 'utf8');
    res.json({ content });
  } catch (err) {
    res.status(500).json({ error: 'Failed to read Palworld settings: ' + err.message });
  }
});
// Write Palworld settings
app.post('/api/palworld/settings', async (req, res) => {
  try {
    const { content } = req.body;
    if (!content) return res.status(400).json({ error: 'No content provided' });
    fs.writeFileSync(PALWORLD_SETTINGS_PATH, content, 'utf8');
    res.json({ success: true });
  } catch (err) {
    res.status(500).json({ error: 'Failed to write Palworld settings: ' + err.message });
  }
});
// Media request endpoint
app.post('/api/media/request', async (req, res) => {
  try {
    const { title, type } = req.body;
    if (!title) return res.status(400).json({ error: 'Title is required' });
    const targetIp = type === 'tv' ? SONARR_IP : RADARR_IP;
    const targetPort = type === 'tv' ? 8989 : 7878;
    const apiKey = type === 'tv' ? process.env.SONARR_API_KEY : process.env.RADARR_API_KEY;
    if (!apiKey) return res.status(500).json({ error: 'API key not configured for ' + type });
    const lookupUrl = `http://${targetIp}:${targetPort}/api/v3/lookup?term=${encodeURIComponent(title)}`;
    const lookupRes = await axios.get(lookupUrl, { headers: { 'X-Api-Key': apiKey } });
    if (!lookupRes.data || lookupRes.data.length === 0) {
      return res.status(404).json({ error: 'No results found for: ' + title });
    }
    const item = lookupRes.data[0];
    const addUrl = `http://${targetIp}:${targetPort}/api/v3/movie`;
    const addRes = await axios.post(addUrl, {
      title: item.title,
      qualityProfileId: 1,
      titleSlug: item.titleSlug,
      tmdbId: item.tmdbId,
      year: item.year,
      monitored: true,
      rootFolderPath: '/media/' + (type === 'tv' ? 'tv' : 'movies'),
      addOptions: { searchForMovie: true }
    }, { headers: { 'X-Api-Key': apiKey } });
    res.json({ success: true, added: addRes.data });
  } catch (err) {
    res.status(500).json({ error: 'Media request failed: ' + err.message });
  }
});
// Pi-hole stats
app.get('/api/pihole/stats', async (req, res) => {
  try {
    const summaryUrl = `http://${PIHOLE_IP}/admin/api.php?summary`;
    const summaryRes = await axios.get(summaryUrl);
    const topBlockedUrl = `http://${PIHOLE_IP}/admin/api.php?topBlocked&count=10`;
    const topBlockedRes = await axios.get(topBlockedUrl);
    const topClientsUrl = `http://${PIHOLE_IP}/admin/api.php?topClients&count=10`;
    const topClientsRes = await axios.get(topClientsUrl);
    res.json({
      summary: summaryRes.data,
      topBlocked: topBlockedRes.data,
      topClients: topClientsRes.data
    });
  } catch (err) {
    res.status(500).json({ error: 'Failed to fetch Pi-hole stats: ' + err.message });
  }
});
// Pi-hole per-client data
app.get('/api/pihole/clients', async (req, res) => {
  try {
    const clientsUrl = `http://${PIHOLE_IP}/admin/api.php?getClientNames`;
    const clientsRes = await axios.get(clientsUrl);
    res.json(clientsRes.data);
  } catch (err) {
    res.status(500).json({ error: 'Failed to fetch Pi-hole clients: ' + err.message });
  }
});
// Scaffold-engine proxy
app.get('/api/scaffold/status', async (req, res) => {
  try {
    const statusRes = await axios.get(SCAFFOLD_ENGINE_URL + '/api/status', { timeout: 5000 });
    res.json(statusRes.data);
  } catch (err) {
    res.status(502).json({ error: 'Scaffold engine unreachable: ' + err.message });
  }
});
app.post('/api/scaffold/run', async (req, res) => {
  try {
    const runRes = await axios.post(SCAFFOLD_ENGINE_URL

## Verify results
$ pct exec 111 -- bash -c "curl -s http://localhost:3001/api/capabilities"
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Error</title>
</head>
<body>
<pre>Cannot GET /api/capabilities</pre>
</body>
</html>

## What went wrong, and what to try

## 👉 Do this next

**Run this now:**

```bash
pct exec 111 -- ls -la /opt/control-panel-backend
```

Then tell me what it shows — specifically whether `server.js` exists and its size.

---

## Diagnosis

The script you ran (`/tmp/in_ct_111.sh`) did not write the backend code into the container. Instead, it printed the JavaScript code to your screen — that's why you saw all those `const`, `app.get`, and `res.json` lines. The file `/opt/control-panel-backend/server.js` inside CT 111 is either missing or incomplete, so the panel's backend never started, and the verify check got an error page.

There's a second, bigger problem: **the technology for the control panel was never actually chosen.** The script was a guess (Node.js/Express), and it failed before we could confirm that's what you want. Before I give you the exact commands to fix it, we need to settle that choice — otherwise we'll just build another guess.

---

## Fix

### First, answer this one question (plain words, no tech jargon)

The control panel needs a small program running on the server that:
- reads and writes the Palworld settings file,
- talks to Radarr/Sonarr to request movies and TV shows,
- talks to Pi-hole to show network traffic,
- talks to scaffold-engine to let you run AI models.

That program is called the **backend**. It also needs a **frontend** — the web page you open in your browser.

There are three common ways to build this. Each has a trade-off:

1. **Node.js + Express** (what the failed script tried)  
   - *What it is:* a JavaScript program that runs on the server and serves a web page.  
   - *Good:* very common, lots of examples, easy to add features later.  
   - *Bad:* you have to keep a separate program running; if it crashes, the panel goes down.

2. **Python + Flask**  
   - *What it is:* a Python program that does the same job.  
   - *Good:* Python is often easier to read and modify if you ever want to tweak it yourself.  
   - *Bad:* same as above — a separate program to keep running.

3. **A static web page + small helper scripts**  
   - *What it is:* the page itself is just HTML/JavaScript, and it calls small one-off commands on the server when you click a button.  
   - *Good:* nothing extra to keep running; the page is always available.  
   - *Bad:* harder to make secure for outside access, and more work to build the first time.

**Which one do you want?** Reply with just the number (1, 2, or 3). If you're not sure, tell me what you want to be able to *do* with the panel (for example, "I just want to type a movie name and have it download"), and I'll pick the simplest option that does that.

---

## Then

Once you tell me your choice, I'll give you the exact copy-paste commands to:
1. Write the backend file correctly (in small pieces, so it doesn't get mangled again).
2. Start the backend and confirm it's running.
3. Open the panel in your browser and test each of the four things you asked for.

---

## If that fails

If the `ls` command above shows **no such directory** or **no `server.js`**, that confirms the file was never written. That's fine — we'll create it from scratch after you answer the question. If the command shows a `server.js` but it's tiny (a few bytes), it's a partial write; we'll overwrite it the same way.
