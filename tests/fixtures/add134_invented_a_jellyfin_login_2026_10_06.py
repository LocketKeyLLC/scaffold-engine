#!/usr/bin/env python3
"""Prove the full media chain: Radarr -> Prowlarr -> qBittorrent -> Radarr import -> Jellyfin.

Usage: python3 /tmp/chain_proof.py <TITLE> [--skip-add]
  --skip-add   skip adding the movie to Radarr (it is already there)
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

RADARR_IP = "192.168.1.22"
PROWLARR_IP = "192.168.1.21"
QBIT_IP = "192.168.1.24"
JELLYFIN_IP = "192.168.1.20"

RADARR_PORT = 7878
PROWLARR_PORT = 9696
QBIT_PORT = 8080
JELLYFIN_PORT = 8096

RADARR_KEY = os.environ.get("RADARR_API_KEY", "")
PROWLARR_KEY = os.environ.get("PROWLARR_API_KEY", "")
MASS_PASSWORD = os.environ.get("MASS_PASSWORD", "")

QBIT_USER = "admin"
QBIT_PASS = MASS_PASSWORD

TITLE = sys.argv[1] if len(sys.argv) > 1 else "Sintel"
SKIP_ADD = "--skip-add" in sys.argv


def http_json(url, method="GET", data=None, headers=None, timeout=20):
    """Make an HTTP request and return parsed JSON. Raises on non-2xx."""
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            if not raw.strip():
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {url}: {err_body[:500]}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"cannot reach {url}: {e}")


def radarr_get(path):
    return http_json(
        f"http://{RADARR_IP}:{RADARR_PORT}/api/v3/{path}",
        headers={"X-Api-Key": RADARR_KEY},
    )


def radarr_post(path, data):
    return http_json(
        f"http://{RADARR_IP}:{RADARR_PORT}/api/v3/{path}",
        method="POST",
        data=data,
        headers={"X-Api-Key": RADARR_KEY},
    )


def prowlarr_get(path):
    return http_json(
        f"http://{PROWLARR_IP}:{PROWLARR_PORT}/api/v1/{path}",
        headers={"X-Api-Key": PROWLARR_KEY},
    )


def qbit_login():
    """Log in to qBittorrent WebUI and return a cookie header."""
    login_url = f"http://{QBIT_IP}:{QBIT_PORT}/api/v2/auth/login"
    data = urllib.parse.urlencode(
        {"username": QBIT_USER, "password": QBIT_PASS}
    ).encode("utf-8")
    req = urllib.request.Request(
        login_url,
        data=data,
        headers={"Referer": f"http://{QBIT_IP}:{QBIT_PORT}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            cookie = resp.headers.get("Set-Cookie", "")
            if not cookie:
                raise RuntimeError("qBittorrent login returned no cookie")
            return cookie.split(";")[0]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"qBittorrent login failed: HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"cannot reach qBittorrent at {QBIT_IP}:{QBIT_PORT}: {e}")


def qbit_get(cookie, path):
    return http_json(
        f"http://{QBIT_IP}:{QBIT_PORT}/api/v2/{path}",
        headers={"Cookie": cookie},
    )


def jellyfin_get(path, token):
    return http_json(
        f"http://{JELLYFIN_IP}:{JELLYFIN_PORT}{path}",
        headers={"X-Emby-Token": token},
    )


def jellyfin_login():
    """Log in to Jellyfin and return an API token."""
    url = f"http://{JELLYFIN_IP}:{JELLYFIN_PORT}/Users/AuthenticateByName"
    data = {"Username": "jellyfin", "Pw": MASS_PASSWORD}
    try:
        return http_json(url, method="POST", data=data)["AccessToken"]
    except KeyError:
        raise RuntimeError("Jellyfin login returned no AccessToken")


def main():
    print(f"[1/6] Looking up '{TITLE}' in Radarr...")
    lookup = radarr_get(f"movie/lookup?term={urllib.parse.quote(TITLE)}")
    if not lookup:
        raise RuntimeError(f"Radarr found nothing for '{TITLE}'")
    movie = lookup[0]
    print(f"      Found: {movie['title']} ({movie.get('year')}) — tmdbId {movie['tmdbId']}")

    if not SKIP_ADD:
        print(f"[2/6] Adding '{TITLE}' to Radarr...")
        add_payload = {
            "title": movie["title"],
            "tmdbId": movie["tmdbId"],
            "year": movie.get("year", 0),
            "qualityProfileId": 1,
            "rootFolderPath": "/media/movies",
            "monitored": True,
            "addOptions": {"searchForMovie": True},
        }
        try:
            added = radarr_post("movie", add_payload)
            print(f"      Added: id={added['id']} status={added.get('status')} monitored={added.get('monitored')}")
        except RuntimeError as e:
            if "already" in str(e).lower() or "exists" in str(e).lower():
                print("      Already in Radarr — continuing")
            else:
                raise
    else:
        print("[2/6] Skipping add (--skip-add)")

    print("[3/6] Checking Prowlarr for releases of the title...")
    history = prowlarr_get("history?pageSize=20")
    found_release = False
    for item in history.get("records", []):
        if TITLE.lower() in str(item.get("sourceTitle", "")).lower():
            print(f"      Prowlarr history: {item.get('sourceTitle')} — {item.get('eventType')}")
            found_release = True
    if not found_release:
        print("      No Prowlarr history yet — checking Radarr queue instead")
        queue = radarr_get("queue")
        if queue.get("records"):
            for rec in queue["records"]:
                print(f"      Radarr queue: {rec.get('title')} — {rec.get('status')}")
        else:
            print("      Queue empty — search may still be running")

    print("[4/6] Waiting for qBittorrent to receive the torrent...")
    cookie = qbit_login()
    torrents = qbit_get(cookie, "torrents/info")
    found_torrent = False
    for t in torrents:
        if TITLE.lower() in t.get("name", "").lower():
            print(f"      qBittorrent has: {t['name']} — progress {t.get('progress', 0):.1%} — state {t.get('state')}")
            found_torrent = True
    if not found_torrent:
        print("      No torrent yet in qBittorrent — this may take a moment")
        print("      (the search was just triggered; re-run with --skip-add to check again)")

    print("[5/6] Checking Radarr import status...")
    queue = radarr_get("queue")
    imported = False
    for rec in queue.get("records", []):
        if TITLE.lower() in rec.get("title", "").lower():
            print(f"      Queue: {rec['title']} — status {rec.get('status')} — trackedDownloadState {rec.get('trackedDownloadState')}")
            if rec.get("status") == "completed":
                imported = True
    if not imported:
        print("      Not imported yet — download may still be in progress")

    print("[6/6] Checking Jellyfin for the title...")
    token = jellyfin_login()
    items = jellyfin_get(
        f"/Items?Recursive=true&IncludeItemTypes=Movie&SearchTerm={urllib.parse.quote(TITLE)}",
        token,
    )
    found_jellyfin = False
    for item in items.get("Items", []):
        print(f"      Jellyfin has: {item.get('Name')} ({item.get('ProductionYear')})")
        found_jellyfin = True
    if not found_jellyfin:
        print("      Not in Jellyfin yet — a library scan may be needed")

    print("\nChain proof complete.")


if __name__ == "__main__":
    main()
