import json
import subprocess
import urllib.request
import urllib.error

PROWLARR_CT = 102
PROWLARR_URL = "http://<PROWLARR_IP>:9696"
RADARR_CT = 103
SONARR_CT = 104


def get_key(ctid, config_path):
    out = subprocess.run(
        ["pct", "exec", str(ctid), "--", "cat", config_path],
        capture_output=True, text=True, check=True
    ).stdout
    tag = "<ApiKey>"
    return out.split(tag)[1].split("<")[0].strip()


def api_request(url, key, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Key", key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw
        return e.code, parsed


def whose_fault(errors):
    errors = [e for e in errors if isinstance(e, dict)]
    msgs = " ".join(str(e.get("errorMessage", "")) for e in errors).lower()
    if "unique" in msgs or "already exists" in msgs:
        return "duplicate"
    named = [e for e in errors if str(e.get("propertyName") or "").strip()]
    if named and all(e.get("attemptedValue") == "" for e in named):
        return "needs_input"
    if named:
        return "bad_request"
    return "unreachable"


def add_application(prowlarr_key, name, url, app_key):
    body = {
        "name": name,
        "syncLevel": "fullSync",
        "implementation": "Radarr" if name == "Radarr" else "Sonarr",
        "configContract": "RadarrSettings" if name == "Radarr" else "SonarrSettings",
        "fields": [
            {"name": "baseUrl", "value": url},
            {"name": "apiKey", "value": app_key},
            {"name": "prowlarrUrl", "value": PROWLARR_URL},
        ],
        "tags": [],
    }
    try:
        status, resp = api_request(
            f"{PROWLARR_URL}/api/v1/applications", prowlarr_key,
            method="POST", body=body
        )
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"{name}: unreachable ({type(e).__name__})")
        return False

    if status in (200, 201):
        print(f"{name}: added")
        return True
    if status == 400:
        verdict = whose_fault(resp if isinstance(resp, list) else [resp])
        if verdict == "duplicate":
            print(f"{name}: already present")
            return True
        print(f"{name}: bad request:")
        print(json.dumps(resp, indent=2))
        return False
    print(f"{name}: HTTP {status}")
    print(json.dumps(resp, indent=2) if isinstance(resp, (dict, list)) else resp)
    return False


def main():
    prowlarr_key = get_key(PROWLARR_CT, "/var/lib/prowlarr/config.xml")
    radarr_key = get_key(RADARR_CT, "/var/lib/radarr/config.xml")
    sonarr_key = get_key(SONARR_CT, "/var/lib/sonarr/config.xml")

    ok = True
    ok = add_application(prowlarr_key, "Radarr", "http://<RADARR_IP>:7878", radarr_key) and ok
    ok = add_application(prowlarr_key, "Sonarr", "http://<SONARR_IP>:8989", sonarr_key) and ok

    if ok:
        try:
            status, _ = api_request(
                f"{PROWLARR_URL}/api/v1/command", prowlarr_key,
                method="POST", body={"name": "ApplicationIndexerSync"}
            )
            print(f"sync triggered: HTTP {status}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"sync trigger failed: {type(e).__name__}")
    else:
        print("application wiring incomplete; sync not triggered")


if __name__ == "__main__":
    main()
