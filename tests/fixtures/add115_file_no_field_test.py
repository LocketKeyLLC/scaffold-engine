import json
import subprocess
import sys
import urllib.request
import urllib.error

PROWLARR_URL = "http://192.168.1.21:9696"


def get_key(ctid, config_path):
    """Read the ApiKey from a container's config.xml."""
    try:
        out = subprocess.run(
            ["pct", "exec", str(ctid), "--", "cat", config_path],
            capture_output=True, text=True, check=True, timeout=30
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"failed to read key from container {ctid}: {e}")
        sys.exit(1)
    start = out.find("<ApiKey>")
    if start == -1:
        print(f"no ApiKey element found in container {ctid} config")
        sys.exit(1)
    start += len("<ApiKey>")
    end = out.find("<", start)
    return out[start:end].strip()


def api_request(method, path, key, body=None):
    """Make an API request to Prowlarr. Returns (status, parsed_body_or_none, raw_body)."""
    url = PROWLARR_URL + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Key", key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw), raw
            except json.JSONDecodeError:
                return resp.status, None, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw), raw
        except json.JSONDecodeError:
            return e.code, None, raw
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, None, str(e)


def main():
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    end = int(sys.argv[2]) if len(sys.argv) > 2 else 1000

    key = get_key(102, "/var/lib/prowlarr/config.xml")

    # Fetch app profiles to get a real appProfileId
    status, profiles, raw = api_request("GET", "/api/v1/appprofile", key)
    if status != 200 or not profiles:
        print(f"failed to fetch app profiles: HTTP {status} {raw}")
        sys.exit(1)
    if not profiles:
        print("no app profiles returned")
        sys.exit(1)
    app_profile_id = profiles[0].get("id")
    if app_profile_id is None:
        print(f"app profile has no id: {profiles[0]}")
        sys.exit(1)

    # Fetch the indexer schema
    status, schema, raw = api_request("GET", "/api/v1/indexer/schema", key)
    if status != 200 or not schema:
        print(f"failed to fetch schema: HTTP {status} {raw}")
        sys.exit(1)

    public = [d for d in schema if d.get("privacy") == "public"]
    public.sort(key=lambda x: x.get("name", ""))
    batch = public[start:end]

    added = 0
    already_present = 0
    unreachable = []
    validation_failures = []

    for entry in batch:
        name = entry.get("name", "unknown")
        body = dict(entry)
        body["priority"] = 25
        body["enable"] = True
        body["appProfileId"] = app_profile_id

        status, resp_body, raw = api_request("POST", "/api/v1/indexer", key, body)

        if status in (200, 201):
            added += 1
            print(f"added: {name}")
        elif status == 400:
            # Check if it's a uniqueness error (already exists) or a validation error
            err_text = raw.lower()
            if "unique" in err_text or "already exists" in err_text:
                already_present += 1
                print(f"already present: {name}")
            else:
                # Validation failure - print the body and stop
                print(f"VALIDATION FAILURE on {name}:")
                print(raw)
                print("Stopping - fix the body and re-run.")
                sys.exit(1)
        elif status in (502, 503, 504) or status is None:
            unreachable.append(name)
            print(f"unreachable: {name} - {raw[:200]}")
        else:
            unreachable.append(name)
            print(f"failed ({status}): {name} - {raw[:200]}")

    print(f"\nBatch {start}-{end}: added={added}, already_present={already_present}, unreachable={len(unreachable)}")
    if unreachable:
        print("Unreachable indexers:")
        for n in unreachable:
            print(f"  - {n}")


if __name__ == "__main__":
    main()
