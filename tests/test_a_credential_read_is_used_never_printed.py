"""§17.1652 — a command that reads a service's credential must use it, never print it (job 5db1b088 T6/T9v2)."""
from __future__ import annotations

from app.modules.prints_a_secret import prints_a_credential

T6_CHECK = "pct exec 102 -- bash -c \"grep -oP '(?<=<ApiKey>)[^<]+' /var/lib/prowlarr/config.xml\""
SQL = ("pct exec 101 -- sh -c 'sqlite3 /var/lib/jellyfin/data/jellyfin.db \"SELECT AccessToken FROM ApiKeys LIMIT 1\"'")


def test_a_printed_key_is_refused():
    assert prints_a_credential([], [T6_CHECK]) and prints_a_credential([SQL], [])
    assert prints_a_credential(["pct exec 103 -- sed -n 's:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p' /var/lib/radarr/config.xml"], [])


def test_a_key_used_where_it_is_read_or_only_counted_passes():
    used = ("pct exec 103 -- sh -c 'curl -s -H \"X-Api-Key: $(cat /var/lib/radarr/config.xml /config/config.xml "
            "2>/dev/null | sed -n \"s:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p\" | head -n 1)\" http://127.0.0.1:7878/api/v3/system/status'")
    counted = "pct exec 102 -- grep -c '<ApiKey>' /var/lib/prowlarr/config.xml"
    quiet = "pct exec 102 -- grep -q '<ApiKey>' /var/lib/prowlarr/config.xml && echo has-key"
    py = "pct exec 103 -- python3 -c 'import re;k=re.search(r\"<ApiKey>([^<]*)</ApiKey>\",open(\"/var/lib/radarr/config.xml\").read())'"
    assert prints_a_credential([used, counted, quiet, py], [used]) == []
