import json
import sys
import urllib.request
import urllib.error

PROWLARR_URL = "http://192.168.1.21:9696"


def get_key(ctid, config_path):
    import subprocess
    out = subprocess.run(
        ["pct", "exec", str(ctid), "--", "cat", config_path],
        capture_output=True, text=True, check=True
    ).stdout
    return out.split("<ApiKey>")[1].split("<")[0]


def api_request(url, key, method="GET", body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Key", key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {detail}")


def main():
    start = int(sys.argv[1])
    end = int(sys.argv[2])
    key = get_key(102, "/var/lib/prowlarr/config.xml")

    schema = api_request(f"{PROWLARR_URL}/api/v1/indexer/schema", key)
    public = [d for d in schema if d.get("privacy") == "public"]
    public.sort(key=lambda x: x["name"])

    profiles = api_request(f"{PROWLARR_URL}/api/v1/appprofile", key)
    app_profile_id = profiles[0]["id"]

    added = 0
    already = 0
    unreachable = []

    for entry in public[start:end]:
        body = dict(entry)
        body["appProfileId"] = app_profile_id
        body["priority"] = 25
        body["enable"] = True
        name = entry["name"]
        try:
            api_request(f"{PROWLARR_URL}/api/v1/indexer", key, method="POST", body=body)
            print(f"added: {name}")
            added += 1
        except RuntimeError as e:
            msg = str(e)
            if "Should be unique" in msg or "already exists" in msg.lower():
                print(f"already present: {name}")
                already += 1
            elif "Unable to connect" in msg or "unavailable" in msg or "502" in msg or "503" in msg or "timed out" in msg.lower():
                print(f"unreachable: {name}")
                unreachable.append(name)
            else:
                print(f"FAILED validation: {name} -- {msg}")
                raise

    print(f"Batch {start}-{end}: added={added}, already_present={already}, unreachable={len(unreachable)}")
    for n in unreachable:
        print(f"  unreachable: {n}")


if __name__ == "__main__":
    main()