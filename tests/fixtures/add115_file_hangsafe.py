import json
import subprocess
import urllib.request
import urllib.error
import socket

PROWLARR_URL = "http://192.168.1.21:9696"
RADARR_URL = "http://192.168.1.22:7878"
SONARR_URL = "http://192.168.1.23:8989"


def get_key(ctid, config_path):
    """Read the ApiKey from a container's config.xml."""
    try:
        out = subprocess.run(
            ["pct", "exec", str(ctid), "--", "cat", config_path],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return None
        content = out.stdout
        start = content.find("<ApiKey>")
        if start == -1:
            return None
        start += len("<ApiKey>")
        end = content.find("<", start)
        if end == -1:
            return None
        return content[start:end].strip()
    except Exception:
        return None


def api_get(url, key):
    req = urllib.request.Request(url, headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def api_post(url, key, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"X-Api-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw
        return e.code, parsed


def main():
    prowlarr_key = get_key(102, "/var/lib/prowlarr/config.xml")
    if not prowlarr_key:
        print("FATAL: could not read Prowlarr API key from container 102")
        return 1

    # Fetch schema
    try:
        schema = api_get(f"{PROWLARR_URL}/api/v1/indexer/schema", prowlarr_key)
    except Exception as e:
        print(f"FATAL: schema fetch failed: {e}")
        return 1

    public = [d for d in schema if d.get("privacy") == "public"]
    print(f"Public indexer definitions in schema: {len(public)}")

    # Fetch app profiles
    try:
        profiles = api_get(f"{PROWLARR_URL}/api/v1/appprofile", prowlarr_key)
    except Exception as e:
        print(f"FATAL: app profile fetch failed: {e}")
        return 1

    if not profiles:
        print("FATAL: no app profiles returned")
        return 1

    app_profile_id = profiles[0].get("id")
    print(f"Using appProfileId: {app_profile_id}")

    added = 0
    already_present = 0
    unreachable = []
    validation_failed = False

    for entry in public:
        name = entry.get("name", "unknown")
        body = dict(entry)
        body["priority"] = 25
        body["enable"] = True
        body["appProfileId"] = app_profile_id

        try:
            status, resp = api_post(
                f"{PROWLARR_URL}/api/v1/indexer", prowlarr_key, body
            )
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as e:
            unreachable.append(f"{name} (connection error: {e})")
            continue

        if status in (200, 201):
            added += 1
            print(f"added: {name}")
        elif status == 400:
            # Distinguish validation vs availability
            msg = ""
            if isinstance(resp, dict):
                msg = json.dumps(resp)
            elif isinstance(resp, str):
                msg = resp
            low = msg.lower()
            if ("unable to connect" in low
                    or "server is unavailable" in low
                    or "bad gateway" in low
                    or "timed out" in low
                    or "dns" in low
                    or "502" in low
                    or "503" in low):
                unreachable.append(f"{name} ({msg[:200]})")
                print(f"unreachable: {name}")
            elif "should be unique" in low or "already exists" in low:
                already_present += 1
                print(f"already present: {name}")
            else:
                # Validation failure — stop and show body
                print(f"VALIDATION FAILURE on {name}:")
                print(json.dumps(resp, indent=2))
                validation_failed = True
                break
        else:
            unreachable.append(f"{name} (HTTP {status}: {str(resp)[:200]})")
            print(f"unreachable: {name} (HTTP {status})")

    if validation_failed:
        print("Stopped: validation failure — fix body before retrying.")
        return 1

    print(f"\nIndexer summary: added={added}, already_present={already_present}, unreachable={len(unreachable)}")
    if unreachable:
        print("Unreachable indexers:")
        for u in unreachable:
            print(f"  - {u}")

    # Connect Radarr and Sonarr
    radarr_key = get_key(103, "/var/lib/radarr/config.xml")
    sonarr_key = get_key(104, "/var/lib/sonarr/config.xml")

    if not radarr_key:
        print("WARNING: could not read Radarr API key from container 103")
    if not sonarr_key:
        print("WARNING: could not read Sonarr API key from container 104")

    # Check existing applications
    try:
        existing_apps = api_get(f"{PROWLARR_URL}/api/v1/applications", prowlarr_key)
    except Exception as e:
        print(f"WARNING: could not fetch existing applications: {e}")
        existing_apps = []

    existing_names = {a.get("name", "") for a in existing_apps}

    if radarr_key:
        if "Radarr" in existing_names:
            print("Radarr already present as application")
        else:
            radarr_body = {
                "name": "Radarr",
                "syncLevel": "fullSync",
                "implementation": "Radarr",
                "configContract": "RadarrSettings",
                "fields": [
                    {"name": "baseUrl", "value": RADARR_URL},
                    {"name": "apiKey", "value": radarr_key},
                    {"name": "prowlarrUrl", "value": PROWLARR_URL},
                ],
                "tags": [],
            }
            try:
                status, resp = api_post(
                    f"{PROWLARR_URL}/api/v1/applications", prowlarr_key, radarr_body
                )
                if status in (200, 201):
                    print("added application: Radarr")
                else:
                    print(f"Radarr application add returned HTTP {status}: {json.dumps(resp)[:300]}")
            except Exception as e:
                print(f"Radarr application add failed: {e}")

    if sonarr_key:
        if "Sonarr" in existing_names:
            print("Sonarr already present as application")
        else:
            sonarr_body = {
                "name": "Sonarr",
                "syncLevel": "fullSync",
                "implementation": "Sonarr",
                "configContract": "SonarrSettings",
                "fields": [
                    {"name": "baseUrl", "value": SONARR_URL},
                    {"name": "apiKey", "value": sonarr_key},
                    {"name": "prowlarrUrl", "value": PROWLARR_URL},
                ],
                "tags": [],
            }
            try:
                status, resp = api_post(
                    f"{PROWLARR_URL}/api/v1/applications", prowlarr_key, sonarr_body
                )
                if status in (200, 201):
                    print("added application: Sonarr")
                else:
                    print(f"Sonarr application add returned HTTP {status}: {json.dumps(resp)[:300]}")
            except Exception as e:
                print(f"Sonarr application add failed: {e}")

    # Trigger sync
    try:
        status, resp = api_post(
            f"{PROWLARR_URL}/api/v1/command",
            prowlarr_key,
            {"name": "ApplicationSync"},
        )
        print(f"Sync triggered: HTTP {status}")
    except Exception as e:
        print(f"Sync trigger failed: {e}")

    return 0


if __name__ == "__main__":
    exit(main())
