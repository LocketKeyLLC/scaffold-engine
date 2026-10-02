import json
import sys
import urllib.request
import urllib.error
import subprocess

PROWLARR_URL = "http://192.168.1.21:9696"
PROWLARR_CT = "102"

def get_key(ctid, config_path):
    """Read the ApiKey from a container's config.xml."""
    try:
        out = subprocess.run(
            ["pct", "exec", ctid, "--", "cat", config_path],
            capture_output=True, text=True, check=True
        ).stdout
    except subprocess.CalledProcessError as e:
        print(f"failed to read key from container {ctid}: {e}")
        sys.exit(1)
    try:
        key = out.split("<ApiKey>")[1].split("<")[0]
    except IndexError:
        print(f"no ApiKey element found in container {ctid} config")
        sys.exit(1)
    return key

def api_request(path, key, method="GET", body=None):
    """Make an API request to Prowlarr."""
    url = PROWLARR_URL + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Key", key)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        return {"_error": e.code, "_body": err_body}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"_error": "unreachable", "_body": str(e)}

def main():
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    end = int(sys.argv[2]) if len(sys.argv) > 2 else 999

    key = get_key(PROWLARR_CT, "/var/lib/prowlarr/config.xml")

    # Get app profiles to find a valid appProfileId
    profiles = api_request("/api/v1/appprofile", key)
    if "_error" in profiles:
        print(f"failed to fetch app profiles: {profiles}")
        sys.exit(1)
    if not profiles:
        print("no app profiles found")
        sys.exit(1)
    app_profile_id = profiles[0]["id"]
    print(f"using appProfileId {app_profile_id}")

    # Fetch schema
    schema = api_request("/api/v1/indexer/schema", key)
    if "_error" in schema:
        print(f"failed to fetch schema: {schema}")
        sys.exit(1)

    public = [d for d in schema if d.get("privacy") == "public"]
    print(f"total public indexers in schema: {len(public)}")

    added = 0
    already = 0
    unreachable = []
    validation_failed = False

    batch = public[start:end]
    for entry in batch:
        name = entry.get("name", "unknown")
        body = dict(entry)
        body["priority"] = 25
        body["enable"] = True
        body["appProfileId"] = app_profile_id

        result = api_request("/api/v1/indexer", key, method="POST", body=body)
        if "_error" in result:
            err = result["_error"]
            body_text = result.get("_body", "")
            if err == 400:
                # Check if it's a validation error (propertyName present) or availability
                try:
                    err_json = json.loads(body_text)
                    if isinstance(err_json, list) and err_json:
                        first = err_json[0]
                        prop = first.get("propertyName", "")
                        msg = first.get("errorMessage", "")
                        if prop:
                            # Validation error - stop and show
                            print(f"VALIDATION FAILURE on {name}:")
                            print(f"  propertyName: {prop}")
                            print(f"  errorMessage: {msg}")
                            print(f"  full body: {body_text}")
                            validation_failed = True
                            break
                        elif "unable to connect" in msg.lower() or "unavailable" in msg.lower() or "502" in msg or "503" in msg or "timeout" in msg.lower():
                            unreachable.append(name)
                            print(f"unreachable: {name}")
                            continue
                        else:
                            # Unknown 400 - treat as validation, stop
                            print(f"VALIDATION FAILURE on {name}:")
                            print(f"  full body: {body_text}")
                            validation_failed = True
                            break
                    else:
                        print(f"VALIDATION FAILURE on {name}:")
                        print(f"  full body: {body_text}")
                        validation_failed = True
                        break
                except json.JSONDecodeError:
                    print(f"VALIDATION FAILURE on {name}:")
                    print(f"  full body: {body_text}")
                    validation_failed = True
                    break
            elif err == "unreachable":
                unreachable.append(name)
                print(f"unreachable: {name}")
                continue
            else:
                # Other HTTP error - check for "should be unique"
                if "should be unique" in body_text.lower() or "already exists" in body_text.lower():
                    already += 1
                    print(f"already present: {name}")
                    continue
                print(f"failed: {name} - HTTP {err}: {body_text[:200]}")
                unreachable.append(name)
                continue
        else:
            added += 1
            print(f"added: {name}")

    if validation_failed:
        print(f"STOPPED due to validation failure. Added so far: {added}, already present: {already}, unreachable: {len(unreachable)}")
        sys.exit(1)

    print(f"BATCH COMPLETE [{start}:{end}] - added: {added}, already present: {already}, unreachable: {len(unreachable)}")
    if unreachable:
        print(f"unreachable names: {', '.join(unreachable)}")

if __name__ == "__main__":
    main()
