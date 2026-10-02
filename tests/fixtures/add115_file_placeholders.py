import json
import sys
import subprocess
import urllib.request
import urllib.error

PROWLARR_CT = 102
PROWLARR_URL = "http://<PROWLARR_IP>:9696"


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


def main():
    start, end = int(sys.argv[1]), int(sys.argv[2])
    key = get_key(PROWLARR_CT, "/var/lib/prowlarr/config.xml")

    status, schema = api_request(f"{PROWLARR_URL}/api/v1/indexer/schema", key)
    if status != 200:
        print(f"schema fetch failed: {status}")
        sys.exit(1)

    public = [d for d in schema if d.get("privacy") == "public"]
    batch = public[start:end]

    status, profiles = api_request(f"{PROWLARR_URL}/api/v1/appprofile", key)
    if status != 200 or not profiles:
        print("could not read app profiles")
        sys.exit(1)
    app_profile_id = profiles[0]["id"]

    added = 0
    already = 0
    unreachable = []
    needs_input = []

    for entry in batch:
        body = dict(entry)
        body["appProfileId"] = app_profile_id
        body["priority"] = 25
        body["enable"] = True
        name = entry.get("name", "?")

        try:
            status, resp = api_request(
                f"{PROWLARR_URL}/api/v1/indexer", key,
                method="POST", body=body
            )
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            unreachable.append(f"{name} ({type(e).__name__})")
            continue

        if status in (200, 201):
            added += 1
            print(f"added: {name}")
        elif status == 400:
            verdict = whose_fault(resp if isinstance(resp, list) else [resp])
            if verdict == "duplicate":
                already += 1
                print(f"already present: {name}")
            elif verdict == "unreachable":
                unreachable.append(name)
                print(f"unreachable: {name}")
            elif verdict == "needs_input":
                fields = ", ".join(
                    str(e.get("propertyName")) for e in resp
                    if isinstance(e, dict) and str(e.get("propertyName") or "").strip()
                )
                needs_input.append(f"{name} ({fields})")
                print(f"needs configuration: {name} ({fields})")
            else:
                print(f"bad request for {name}:")
                print(json.dumps(resp, indent=2))
                sys.exit(1)
        else:
            print(f"failed: {name} - HTTP {status}")
            print(json.dumps(resp, indent=2) if isinstance(resp, (dict, list)) else resp)
            sys.exit(1)

    print(f"batch {start}-{end}: added={added} already_present={already} "
          f"unreachable={len(unreachable)} needs_input={len(needs_input)}")
    for u in unreachable:
        print(f"  unreachable: {u}")
    for n in needs_input:
        print(f"  needs configuration: {n}")


if __name__ == "__main__":
    main()
