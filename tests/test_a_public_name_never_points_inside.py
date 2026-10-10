"""§17.1453 — a dynamic-DNS update never points a public name at a private address.

Live (ADD128, 2026-10-10, the Auto-mode frame): the drafted block carried, next to the correct
`…&ip=` update, `curl "https://www.duckdns.org/update?domains=defrusciohomelab&token=<DUCKDNS_TOKEN>&ip=192.168.1.127"`
— Caddy's LAN address. Approved, it would have taken the domain off the internet and made its certificate
impossible to issue.
"""
from app.modules import supervised_runs as sr

LIVE = [
    "pct exec 120 -- getent hosts acme-v02.api.letsencrypt.org",
    'curl -s "https://www.duckdns.org/update?domains=defrusciohomelab&token=<DUCKDNS_TOKEN>&ip="',
    'curl -s "https://www.duckdns.org/update?domains=defrusciohomelab&token=<DUCKDNS_TOKEN>&ip=192.168.1.127"',
    "pct exec 120 -- systemctl reload caddy",
]


def test_the_live_private_update_is_refused_and_the_empty_one_is_not():
    out = sr.public_dns_to_private_address(LIVE)
    assert [o["command"] for o in out] == [LIVE[2]]
    assert "points a PUBLIC DNS name at a private address (192.168.1.127)" in out[0]["why"]


def test_other_services_and_ranges():
    bad = ['curl "https://dynupdate.no-ip.com/nic/update?hostname=x.ddns.net&myip=10.0.0.5"',
           'curl -X PUT "https://api.cloudflare.com/client/v4/zones/z/dns_records/r" -d \'{"type":"A","content":"172.20.1.4"}\'']
    assert len(sr.public_dns_to_private_address(bad)) == 2
    ok = ['curl "https://www.duckdns.org/update?domains=x&token=t&ip=67.240.32.243"',
          'curl -s http://192.168.1.127:80/']
    assert sr.public_dns_to_private_address(ok) == []


def test_it_is_a_registered_drafter_refusal():
    m = "points a PUBLIC DNS name at a private address"
    assert m in sr._SHAPE_REFUSALS and sr._WHOSE_GAP[m] == "drafter"
    import pathlib
    assert "refused = refused + public_dns_to_private_address(cmds)" in pathlib.Path("app/modules/supervised_runs.py").read_text()
