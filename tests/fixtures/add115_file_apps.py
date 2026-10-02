import json, urllib.request, urllib.error, subprocess, sys

def get_key(ctid, cfg_path):
    out = subprocess.check_output(["pct", "exec", str(ctid), "--", "cat", cfg_path], text=True)
    for line in out.splitlines():
        if "<ApiKey>" in line:
            return line.split("<ApiKey>")[1].split("<")[0].strip()
    raise RuntimeError(f"No ApiKey found in {cfg_path}")

def api_get(url, key):
    req = urllib.request.Request(url, headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def api_post(url, key, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={
        "X-Api-Key": key,
        "Content-Type": "application/json"
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        return None, (e.code, err_body)

prowlarr_key = get_key(102, "/var/lib/prowlarr/config.xml")
radarr_key = get_key(103, "/var/lib/radarr/config.xml")
sonarr_key = get_key(104, "/var/lib/sonarr/config.xml")
base = "http://192.168.1.21:9696"

# Fetch existing applications
existing_apps = api_get(f"{base}/api/v1/applications", prowlarr_key)
existing_names = {a["name"] for a in existing_apps}

apps = [
    {
        "name": "Radarr",
        "syncLevel": "fullSync",
        "implementation": "Radarr",
        "configContract": "RadarrSettings",
        "fields": [
            {"name": "prowlarrUrl", "value": "http://192.168.1.21:9696"},
            {"name": "baseUrl", "value": "http://192.168.1.22:7878"},
            {"name": "apiKey", "value": radarr_key},
            {"name": "syncCategories", "value": [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080, 2090]}
        ]
    },
    {
        "name": "Sonarr",
        "syncLevel": "fullSync",
        "implementation": "Sonarr",
        "configContract": "SonarrSettings",
        "fields": [
            {"name": "prowlarrUrl", "value": "http://192.168.1.21:9696"},
            {"name": "baseUrl", "value": "http://192.168.1.23:8989"},
            {"name": "apiKey", "value": sonarr_key},
            {"name": "syncCategories", "value": [5000, 5010, 5020, 5030, 5040, 5045, 5050, 5060, 5070, 5080]}
        ]
    }
]

for app in apps:
    if app["name"] in existing_names:
        print(f"already present: {app['name']}")
        continue
    result, err = api_post(f"{base}/api/v1/applications", prowlarr_key, app)
    if err is None:
        print(f"added application: {app['name']}")
    else:
        code, err_body = err
        print(f"FAILED to add {app['name']}: HTTP {code}")
        print(f"  {err_body}")

# Trigger sync
sync_result, sync_err = api_post(f"{base}/api/v1/applications/sync", prowlarr_key, {})
if sync_err is None:
    print("sync triggered successfully")
else:
    code, err_body = sync_err
    print(f"sync FAILED: HTTP {code}")
    print(f"  {err_body}")
