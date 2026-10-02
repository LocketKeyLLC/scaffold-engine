import json
import sys
import urllib.request
import urllib.error

PROWLARR_URL = "http://192.168.1.21:9696"
CTID = 102

def get_key(ctid, config_path):
    import subprocess
    result = subprocess.run(
        ["pct", "exec", str(ctid), "--", "cat", config_path],
        capture_output=True, text=True, check=True
    )
    content = result.stdout
    start = content.find("<ApiKey>") + len("<ApiKey>")
    end = content.find("</ApiKey>", start)
    return content[start:end].strip()

def api_request(url, method="GET", body=None, api_key=None):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode()), resp.status
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        try:
            error_json = json.loads(error_body)
        except json.JSONDecodeError:
            error_json = [{"propertyName": "", "errorMessage": error_body}]
        return error_json, e.code
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return [{"propertyName": "", "errorMessage": f"Connection error: {e}"}], 0

def whose_fault(errors):
    msgs = " ".join(str(e.get("errorMessage", "")) for e in errors if isinstance(e, dict)).lower()
    if "unique" in msgs or "already exists" in msgs:
        return "duplicate"
    if any(str(e.get("propertyName") or "").strip() for e in errors if isinstance(e, dict)):
        return "bad_request"
    return "unreachable"

def main():
    start, end = int(sys.argv[1]), int(sys.argv[2])
    
    api_key = get_key(CTID, "/var/lib/prowlarr/config.xml")
    
    # Fetch schema
    schema, status = api_request(f"{PROWLARR_URL}/api/v1/indexer/schema", api_key=api_key)
    if status != 200:
        print(f"Failed to fetch schema: {schema}")
        sys.exit(1)
    
    public = [d for d in schema if d.get("privacy") == "public"]
    
    # Fetch app profiles
    profiles, status = api_request(f"{PROWLARR_URL}/api/v1/appprofile", api_key=api_key)
    if status != 200 or not profiles:
        print(f"Failed to fetch app profiles: {profiles}")
        sys.exit(1)
    app_profile_id = profiles[0]["id"]
    
    added = 0
    already_present = 0
    unreachable = []
    
    for entry in public[start:end]:
        body = dict(entry)
        body["priority"] = 25
        body["enable"] = True
        body["appProfileId"] = app_profile_id
        
        result, code = api_request(
            f"{PROWLARR_URL}/api/v1/indexer",
            method="POST",
            body=body,
            api_key=api_key
        )
        
        if code in (200, 201):
            added += 1
            print(f"added: {entry['name']}")
        elif code == 400:
            fault = whose_fault(result)
            if fault == "duplicate":
                already_present += 1
                print(f"already present: {entry['name']}")
            elif fault == "bad_request":
                print(f"BAD REQUEST on {entry['name']}: {json.dumps(result, indent=2)}")
                print(f"Stopping. Added so far: {added}, already present: {already_present}, unreachable: {len(unreachable)}")
                sys.exit(1)
            else:
                unreachable.append(entry['name'])
                print(f"unreachable: {entry['name']}")
        elif code == 0:
            unreachable.append(entry['name'])
            print(f"unreachable: {entry['name']}")
        else:
            unreachable.append(entry['name'])
            print(f"failed ({code}): {entry['name']} - {result}")
    
    print(f"\nBatch {start}-{end} complete: added={added}, already_present={already_present}, unreachable={len(unreachable)}")
    if unreachable:
        print(f"Unreachable: {', '.join(unreachable)}")

if __name__ == "__main__":
    main()
