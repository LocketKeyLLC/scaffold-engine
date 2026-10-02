import json
import sys
import urllib.request
import urllib.error

PROWLARR_URL = "http://192.168.1.21:9696"
CTID = 102

def get_key(ctid, config_path):
    import subprocess
    out = subprocess.run(
        ["pct", "exec", str(ctid), "--", "cat", config_path],
        capture_output=True, text=True, check=True
    ).stdout
    for line in out.splitlines():
        if "<ApiKey>" in line:
            return line.split("<ApiKey>")[1].split("<")[0].strip()
    raise RuntimeError("ApiKey not found in config")

def api_request(method, path, key, body=None, timeout=15):
    url = PROWLARR_URL + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Key", key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        return {"_error": e.code, "_body": detail}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"_error": "unreachable", "_body": str(e)}

def main():
    start = int(sys.argv[1])
    end = int(sys.argv[2])

    key = get_key(CTID, "/var/lib/prowlarr/config.xml")

    schema = api_request("GET", "/api/v1/indexer/schema", key)
    if "_error" in schema:
        print(f"schema fetch failed: {schema}")
        sys.exit(1)

    public = [d for d in schema if d.get("privacy") == "public"]
    public.sort(key=lambda x: x["name"])

    profiles = api_request("GET", "/api/v1/appprofile", key)
    if "_error" in profiles:
        print(f"appprofile fetch failed: {profiles}")
        sys.exit(1)
    app_profile_id = profiles[0]["id"] if profiles else 1

    added = 0
    already = 0
    unreachable = []

    batch = public[start:end]
    for entry in batch:
        body = dict(entry)
        body["appProfileId"] = app_profile_id
        body["priority"] = 25
        body["enable"] = True
        body.pop("id", None)

        result = api_request("POST", "/api/v1/indexer", key, body=body, timeout=15)

        if "_error" in result:
            err = result["_error"]
            body_text = result.get("_body", "")
            if err == "unreachable":
                unreachable.append(entry["name"])
                print(f"unreachable: {entry['name']}")
            elif "unique" in body_text.lower():
                already += 1
                print(f"already present: {entry['name']}")
            elif "propertyName" in body_text or "must be" in body_text.lower():
                print(f"VALIDATION FAILURE on {entry['name']}: {body_text}")
                sys.exit(1)
            else:
                unreachable.append(entry["name"])
                print(f"unreachable: {entry['name']} - {body_text[:200]}")
        else:
            added += 1
            print(f"added: {entry['name']}")

    print(f"batch {start}-{end}: added={added} already_present={already} unreachable={len(unreachable)}")
    for name in unreachable:
        print(f"  unreachable: {name}")

if __name__ == "__main__":
    main()
