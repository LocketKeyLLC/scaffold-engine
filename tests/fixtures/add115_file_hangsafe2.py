import json
import urllib.request
import urllib.error
import subprocess
import sys
import socket

PROWLARR_URL = "http://192.168.1.21:9696"
RADARR_URL = "http://192.168.1.22:7878"
SONARR_URL = "http://192.168.1.23:8989"

def get_key(ctid, config_path):
    """Read the ApiKey from a container's config.xml."""
    try:
        result = subprocess.run(
            ["pct", "exec", str(ctid), "--", "cat", config_path],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return None
        content = result.stdout
        start = content.find("<ApiKey>")
        if start == -1:
            return None
        start += len("<ApiKey>")
        end = content.find("</ApiKey>", start)
        if end == -1:
            return None
        return content[start:end].strip()
    except Exception:
        return None

def api_request(url, api_key, method="GET", body=None, timeout=30):
    """Make an API request and return (status, response_body)."""
    headers = {"X-Api-Key": api_key}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as e:
        return None, str(e)

def main():
    prowlarr_key = get_key(102, "/var/lib/prowlarr/config.xml")
    if not prowlarr_key:
        print("ERROR: Could not read Prowlarr API key from container 102")
        sys.exit(1)

    # Fetch app profiles to get a valid appProfileId
    status, body = api_request(f"{PROWLARR_URL}/api/v1/appprofile", prowlarr_key)
    if status != 200:
        print(f"ERROR: Could not fetch app profiles: HTTP {status}")
        print(body[:500])
        sys.exit(1)
    profiles = json.loads(body)
    if not profiles:
        print("ERROR: No app profiles found")
        sys.exit(1)
    app_profile_id = profiles[0]["id"]
    print(f"Using appProfileId: {app_profile_id}")

    # Fetch indexer schema
    status, body = api_request(f"{PROWLARR_URL}/api/v1/indexer/schema", prowlarr_key)
    if status != 200:
        print(f"ERROR: Could not fetch indexer schema: HTTP {status}")
        print(body[:500])
        sys.exit(1)
    schema = json.loads(body)
    public_indexers = [d for d in schema if d.get("privacy") == "public"]
    print(f"Found {len(public_indexers)} public indexer definitions")

    added = 0
    already_present = 0
    unreachable = []
    validation_failed = False

    for idx, entry in enumerate(public_indexers):
        name = entry.get("name", "unknown")
        post_body = dict(entry)
        post_body["appProfileId"] = app_profile_id
        post_body["priority"] = 25
        post_body["enable"] = True

        status, resp_body = api_request(
            f"{PROWLARR_URL}/api/v1/indexer",
            prowlarr_key,
            method="POST",
            body=post_body,
            timeout=30
        )

        if status == 200 or status == 201:
            added += 1
            print(f"added: {name}")
        elif status == 400:
            # Check if it's a validation error or availability error
            try:
                err = json.loads(resp_body)
                if isinstance(err, list) and len(err) > 0:
                    err = err[0]
                property_name = err.get("propertyName", "")
                error_message = err.get("errorMessage", "")
                # Availability errors have empty propertyName and mention connection
                if property_name == "" and any(
                    phrase in error_message.lower()
                    for phrase in ["unable to connect", "server is unavailable", "timed out", "bad gateway", "502", "503"]
                ):
                    unreachable.append(name)
                    print(f"unreachable: {name} - {error_message[:100]}")
                elif "should be unique" in error_message.lower():
                    already_present += 1
                    print(f"already present: {name}")
                else:
                    # Validation error - stop and show the body
                    print(f"VALIDATION ERROR on {name}:")
                    print(resp_body[:1000])
                    validation_failed = True
                    break
            except (json.JSONDecodeError, AttributeError):
                # Can't parse - treat as validation error
                print(f"VALIDATION ERROR on {name}:")
                print(resp_body[:1000])
                validation_failed = True
                break
        elif status is None:
            unreachable.append(name)
            print(f"unreachable: {name} - {resp_body[:100]}")
        else:
            unreachable.append(name)
            print(f"failed: {name} - HTTP {status}: {resp_body[:100]}")

    if validation_failed:
        print("Stopped due to validation error - fix the body and re-run")
        sys.exit(1)

    print(f"\nIndexer results: {added} added, {already_present} already present, {len(unreachable)} unreachable")
    if unreachable:
        print("Unreachable indexers:")
        for name in unreachable:
            print(f"  - {name}")

    # Now connect applications
    radarr_key = get_key(103, "/var/lib/radarr/config.xml")
    sonarr_key = get_key(104, "/var/lib/sonarr/config.xml")

    if not radarr_key:
        print("WARNING: Could not read Radarr API key from container 103")
    if not sonarr_key:
        print("WARNING: Could not read Sonarr API key from container 104")

    # Add Radarr application
    if radarr_key:
        radarr_body = {
            "name": "Radarr",
            "syncLevel": "fullSync",
            "implementation": "Radarr",
            "configContract": "RadarrSettings",
            "fields": [
                {"name": "baseUrl", "value": RADARR_URL},
                {"name": "apiKey", "value": radarr_key},
                {"name": "prowlarrUrl", "value": PROWLARR_URL},
                {"name": "syncCategories", "value": [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080, 2090]}
            ]
        }
        status, resp_body = api_request(
            f"{PROWLARR_URL}/api/v1/applications",
            prowlarr_key,
            method="POST",
            body=radarr_body,
            timeout=30
        )
        if status in (200, 201):
            print("added application: Radarr")
        elif status == 400 and "should be unique" in resp_body.lower():
            print("application already present: Radarr")
        else:
            print(f"failed to add Radarr application: HTTP {status}: {resp_body[:200]}")

    # Add Sonarr application
    if sonarr_key:
        sonarr_body = {
            "name": "Sonarr",
            "syncLevel": "fullSync",
            "implementation": "Sonarr",
            "configContract": "SonarrSettings",
            "fields": [
                {"name": "baseUrl", "value": SONARR_URL},
                {"name": "apiKey", "value": sonarr_key},
                {"name": "prowlarrUrl", "value": PROWLARR_URL},
                {"name": "syncCategories", "value": [5000, 5010, 5020, 5030, 5040, 5045, 5050, 5060, 5070, 5080, 5090]}
            ]
        }
        status, resp_body = api_request(
            f"{PROWLARR_URL}/api/v1/applications",
            prowlarr_key,
            method="POST",
            body=sonarr_body,
            timeout=30
        )
        if status in (200, 201):
            print("added application: Sonarr")
        elif status == 400 and "should be unique" in resp_body.lower():
            print("application already present: Sonarr")
        else:
            print(f"failed to add Sonarr application: HTTP {status}: {resp_body[:200]}")

    # Trigger sync
    status, resp_body = api_request(
        f"{PROWLARR_URL}/api/v1/command",
        prowlarr_key,
        method="POST",
        body={"name": "AppIndexerSync"},
        timeout=30
    )
    if status in (200, 201):
        print("triggered sync")
    else:
        print(f"failed to trigger sync: HTTP {status}: {resp_body[:200]}")

if __name__ == "__main__":
    main()
