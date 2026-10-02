import json, urllib.request, urllib.error, subprocess, sys, time

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
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        return None, (e.code, err_body)

prowlarr_key = get_key(102, "/var/lib/prowlarr/config.xml")
base = "http://192.168.1.21:9696"

# Fetch app profiles to get a real appProfileId
profiles = api_get(f"{base}/api/v1/appprofile", prowlarr_key)
if not profiles:
    print("ERROR: no app profiles found")
    sys.exit(1)
app_profile_id = profiles[0]["id"]

# Fetch schema
schema = api_get(f"{base}/api/v1/indexer/schema", prowlarr_key)
public = [d for d in schema if d.get("privacy") == "public"]
print(f"Total public indexers in schema: {len(public)}")

# Fetch existing indexers to detect already-present
existing = api_get(f"{base}/api/v1/indexer", prowlarr_key)
existing_names = {e["name"] for e in existing}

added = 0
already = 0
unreachable = []
validation_failed = False

for entry in public:
    name = entry["name"]
    if name in existing_names:
        already += 1
        print(f"already present: {name}")
        continue

    body = dict(entry)
    body["appProfileId"] = app_profile_id
    body["priority"] = 25
    body["enable"] = True

    result, err = api_post(f"{base}/api/v1/indexer", prowlarr_key, body)
    if err is None:
        added += 1
        print(f"added: {name}")
    else:
        code, err_body = err
        # Distinguish validation vs availability
        try:
            err_json = json.loads(err_body)
            prop = err_json[0].get("propertyName", "") if isinstance(err_json, list) and err_json else ""
            msg = err_json[0].get("errorMessage", "") if isinstance(err_json, list) and err_json else ""
        except Exception:
            prop = ""
            msg = err_body[:200]

        if prop:
            # Validation failure — stop and show
            print(f"VALIDATION FAILURE on {name}: HTTP {code}")
            print(f"  propertyName: {prop}")
            print(f"  errorMessage: {msg}")
            print(f"  Full body: {err_body}")
            validation_failed = True
            break
        else:
            # Availability failure — skip and continue
            unreachable.append(name)
            print(f"unreachable: {name} (HTTP {code}: {msg[:120]})")

    time.sleep(0.2)

print("\n=== SUMMARY ===")
print(f"Added: {added}")
print(f"Already present: {already}")
print(f"Unreachable: {len(unreachable)}")
if unreachable:
    print("Unreachable indexers:")
    for n in unreachable:
        print(f"  - {n}")
if validation_failed:
    print("STOPPED on validation failure — fix body and re-run")
