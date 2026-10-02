import json
import sys
import urllib.request
import urllib.error
import subprocess

PROWLARR_URL = "http://192.168.1.21:9696"
RADARR_URL = "http://192.168.1.22:7878"
SONARR_URL = "http://192.168.1.23:8989"


def get_key(ctid, config_path):
    out = subprocess.run(
        ["pct", "exec", str(ctid), "--", "cat", config_path],
        capture_output=True, text=True, check=True,
    ).stdout
    return out.split("<ApiKey>")[1].split("<")[0]


def api_request(url, method="GET", key=None, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("X-Api-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, str(e)


def main():
    start, end = int(sys.argv[1]), int(sys.argv[2])
    key = get_key(102, "/var/lib/prowlarr/config.xml")

    status, profiles = api_request(f"{PROWLARR_URL}/api/v1/appprofile", key=key)
    if status != 200:
        print(f"failed to fetch app profiles: {status} {profiles}")
        sys.exit(1)
    app_profile_id = profiles[0]["id"]

    status, schema = api_request(f"{PROWLARR_URL}/api/v1/indexer/schema", key=key)
    if status != 200:
        print(f"failed to fetch schema: {status} {schema}")
        sys.exit(1)

    public = [d for d in schema if d.get("privacy") == "public"]
    batch = public[start:end]

    added = 0
    already = 0
    unreachable = []

    for entry in batch:
        body = dict(entry)
        body["appProfileId"] = app_profile_id
        body["priority"] = 25
        body["enable"] = True

        status, resp = api_request(
            f"{PROWLARR_URL}/api/v1/indexer", method="POST", key=key, body=body
        )

        if status == 201 or status == 200:
            added += 1
            print(f"added: {entry['name']}")
        elif status == 400 and "should be unique" in str(resp).lower():
            already += 1
            print(f"already present: {entry['name']}")
        elif status == 400 and "propertyName" in str(resp):
            print(f"VALIDATION FAILURE on {entry['name']}: {resp}")
            sys.exit(1)
        elif status in (502, 503) or status is None:
            unreachable.append(entry["name"])
            print(f"unreachable: {entry['name']}")
        else:
            unreachable.append(entry["name"])
            print(f"failed ({status}): {entry['name']} - {str(resp)[:200]}")

    print(f"batch {start}-{end}: added={added} already={already} unreachable={len(unreachable)}")
    if unreachable:
        print("unreachable names: " + ", ".join(unreachable))


if __name__ == "__main__":
    main()
