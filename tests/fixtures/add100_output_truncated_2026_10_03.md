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
```bash
apt-get update && apt-get install -y python3-pip python3-venv nginx
python3 -m venv /opt/control-panel/venv
/opt/control-panel/venv/bin/pip install flask flask-cors requests gunicorn
mkdir -p /opt/control-panel/backend /opt/control-panel/frontend
tee /opt/control-panel/backend/app.py <<'EOF'
import os
import json
import subprocess
import requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
app = Flask(__name__, static_folder='/opt/control-panel/frontend', static_url_path='')
CORS(app)
PALWORLD_CONF = '/opt/palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini'
RADARR_URL = 'http://192.168.1.22:7878'
SONARR_URL = 'http://192.168.1.23:8989'
PIHOLE_URL = 'http://192.168.1.130'
PIHOLE_API_TOKEN = os.environ.get('PIHOLE_API_TOKEN', '')
SCAFFOLD_ENGINE_URL = 'http://192.168.1.110:8080'
def read_palworld_settings():
    if not os.path.exists(PALWORLD_CONF):
        return {}
    settings = {}
    with open(PALWORLD_CONF, 'r') as f:
        content = f.read()
    if 'OptionSettings=' in content:
        opts = content.split('OptionSettings=')[1].split(')')[0]
        for pair in opts.split(','):
            if '=' in pair:
                k, v = pair.split('=', 1)
                settings[k.strip()] = v.strip()
    return settings
def write_palworld_settings(settings):
    os.makedirs(os.path.dirname(PALWORLD_CONF), exist_ok=True)
    content = '[/Script/Pal.PalGameWorldSettings]\nOptionSettings=('
    content += ','.join(f'{k}={v}' for k, v in settings.items())
    content += ')\n'
    with open(PALWORLD_CONF, 'w') as f:
        f.write(content)
@app.route('/api/palworld/settings', methods=['GET'])
def get_palworld_settings():
    return jsonify(read_palworld_settings())
@app.route('/api/palworld/settings', methods=['POST'])
def update_palworld_settings():
    settings = read_palworld_settings()
    settings.update(request.json)
    write_palworld_settings(settings)
    return jsonify({'status': 'ok', 'settings': settings})
@app.route('/api/media/request', methods=['POST'])
def media_request():
    data = request.json
    title = data.get('title', '')
    media_type = data.get('type', 'movie')
    if media_type == 'movie':
        url = f'{RADARR_URL}/api/v3/movie'
        payload = {'title': title, 'qualityProfileId': 1, 'tmdbId': 0, 'rootFolderPath': '/movies', 'monitored': True, 'addOptions': {'searchForMovie': True}}
    else:
        url = f'{SONARR_URL}/api/v3/series'
        payload = {'title': title, 'qualityProfileId': 1, 'tvdbId': 0, 'rootFolderPath': '/tv', 'monitored': True, 'addOptions': {'searchForMissingEpisodes': True}}
    try:
        r = requests.post(url, json=payload, headers={'X-Api-Key': os.environ.get('RADARR_API_KEY', '')}, timeout=10)
        return jsonify({'status': 'ok', 'response': r.json()}), r.status_code
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
@app.route('/api/network/stats', methods=['GET'])
def network_stats():
    try:
        r = requests.get(f'{PIHOLE_URL}/admin/api.php?summaryRaw&auth={PIHOLE_API_TOKEN}', timeout=10)
        summary = r.json()
        r2 = requests.get(f'{PIHOLE_URL}/admin/api.php?topClients&auth={PIHOLE_API_TOKEN}', timeout=10)
        top_clients = r2.json()
        r3 = requests.get(f'{PIHOLE_URL}/admin/api.php?topBlocked&auth={PIHOLE_API_TOKEN}', timeout=10)
        top_blocked = r3.json()
        return jsonify({
            'queries_total': summary.get('dns_queries_today', 0),
            'queries_blocked': summary.get('ads_blocked_today', 0),
            'blocked_percent': summary.get('ads_percentage_today', 0),
            'top_clients': top_clients.get('top_sources', {}),
            'top_blocked': top_blocked.get('top_ads', {})
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
@app.route('/api/scaffold-engine', methods=['GET'])
def scaffold_engine_proxy():
    try:
        r = requests.get(f'{SCAFFOLD_ENGINE_URL}/', timeout=10)
        return jsonify({'status': 'ok', 'reachable': True, 'response': r.text[:200]})
    except Exception as e:
        return jsonify({'status': 'error', 'reachable': False, 'message': str(e)}), 500
@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=3001)
EOF
tee /opt/control-panel/frontend/index.html <<'EOF'
<!DOCTYPE html>
<html>
<head>
    <title>Home Lab Control Panel</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: sans-serif; margin: 20px; background: #f5f5f5; }
        .card { background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        h2 { margin-top: 0; }
        input, select, button { padding: 8px; margin: 4px; }
        button { background: #0066cc; color: white; border: none; border-radius: 4px; cursor: pointer; }
        button:hover { background: #0052a3; }
        table { width: 100%; border-collapse
REMOTE
pct push "$GID" /tmp/in_ct_111_remote.sh /root/.scaffold_step.sh >/dev/null || { echo "FAILED: pct push into $GID"; exit 1; }
pct exec "$GID" -- bash /root/.scaffold_step.sh
```

## Run this

```bash
bash /tmp/in_ct_111.sh
```

## Verify

- `pct exec 111 -- bash -c "curl -s http://localhost:3001/api/health"`

## Executed on pve-runner (supervised, operator-approved)
$ write /tmp/in_ct_111.sh (345 bytes)
wrote /tmp/in_ct_111.sh (345 bytes, 9 lines)
$ bash /tmp/in_ct_111.sh
/tmp/in_ct_111.sh: line 9: warning: here-document at line 7 delimited by end-of-file (wanted `REMOTE')

## Verify results
$ pct exec 111 -- bash -c "curl -s http://localhost:3001/api/health"
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Error</title>
</head>
<body>
<pre>Cannot GET /api/health</pre>
</body>
</html>
