"""§17.1346 — the engine measures the SERVICES on a guest, not just the guest.

The day's failures were all one shape: the engine had no model of the
applications on its machines, so every draft re-derived which guest an app lives
in, which port it answers on, where its config is, which user it runs as, and
whether it rewrites that config itself — from the step's prose. Each rule that
read prose became a new way to be wrong:

* a step about an ssh key drafted as a 4.6 GB game install, because the guest is
  NAMED after the game (§17.1339);
* the shared-storage step drafted inside Jellyfin, the one container that already
  had the mount (§17.1340);
* `in-container 999:996` — a service's uid and gid — read as guest 999 (§17.1344);
* an edit to qBittorrent's config thrown away, because the service rewrites that
  file on shutdown and nothing knew (§17.1343);
* the root-folder step putting Sonarr's call inside Radarr's container.

Every one of those is a fact a machine states plainly when asked. The fixtures
below are those answers, verbatim from containers 103, 104, 105 and 101.
"""
from __future__ import annotations

from app.modules import service_truth as st

#: `systemctl show -p Id -p User -p Group -p ExecStart -p FragmentPath radarr` in 103
SHOW_RADARR = """ExecStart={ path=/opt/Radarr/Radarr ; argv[]=/opt/Radarr/Radarr -nobrowser -data=/var/lib/radarr ; ignore_errors=no ; start_time=[Sun 2026-10-04 01:32:14 UTC] ; stop_time=[n/a] ; pid=127 ; code=(null) ; status=0/0 }
User=radarr
Group=radarr
Id=radarr.service
FragmentPath=/etc/systemd/system/radarr.service"""
#: `ss -tlnp` in 103
SS_103 = """State  Recv-Q Send-Q Local Address:Port Peer Address:PortProcess
LISTEN 0      100        127.0.0.1:25        0.0.0.0:*    users:(("master",pid=289,fd=13))
LISTEN 0      512                *:7878            *:*    users:(("Radarr",pid=127,fd=319))
LISTEN 0      4096               *:22              *:*    users:(("sshd",pid=136,fd=3),("systemd",pid=1,fd=38))
LISTEN 0      100            [::1]:25           [::]:*    users:(("master",pid=289,fd=14))"""

RADARR = st.ServiceTruth(
    guest="103", unit="radarr.service", name="radarr", state="active", user="radarr", group="radarr",
    uid="999", gid="996", argv="/opt/Radarr/Radarr -nobrowser -data=/var/lib/radarr",
    data_dir="/var/lib/radarr", ports=("7878",),
    configs=("/var/lib/radarr/config.xml",),
    config_stat={"/var/lib/radarr/config.xml": ("radarr:radarr", "644")})
SONARR = st.ServiceTruth(
    guest="104", unit="sonarr.service", name="sonarr", state="active", user="sonarr", group="sonarr",
    uid="999", gid="996", data_dir="/var/lib/sonarr", ports=("8989",),
    configs=("/var/lib/sonarr/config.xml",),
    config_stat={"/var/lib/sonarr/config.xml": ("sonarr:sonarr", "644")})
QB = st.ServiceTruth(
    guest="105", unit="qbittorrent-nox.service", name="qbittorrent-nox", state="active",
    user="qbittorrent-nox", group="qbittorrent-nox", uid="999", gid="996", ports=("61661", "8080"),
    configs=("/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent-data.conf",
             "/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf"),
    config_stat={"/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent-data.conf": ("qbittorrent-nox:qbittorrent-nox", "664"),
                 "/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf": ("qbittorrent-nox:qbittorrent-nox", "664")})
#: a config the service CANNOT write: root-owned, service runs as `caddy`
CADDY = st.ServiceTruth(guest="120", unit="caddy.service", name="caddy", user="caddy", group="caddy",
                        configs=("/etc/caddy/Caddyfile",),
                        config_stat={"/etc/caddy/Caddyfile": ("root:root", "644")})
SERVICES = [RADARR, SONARR, QB, CADDY]


def test_the_unit_says_its_user_and_where_its_data_lives():
    sh = st.parse_show(SHOW_RADARR)
    assert sh["Id"] == "radarr.service" and sh["User"] == "radarr" and sh["Group"] == "radarr"
    assert sh["argv"] == "/opt/Radarr/Radarr -nobrowser -data=/var/lib/radarr"
    assert st.data_dir_of(sh["argv"]) == "/var/lib/radarr", "the app was TOLD where its data goes"


def test_the_port_comes_from_the_process_holding_it():
    """The process name is the binary's, not the unit's: `Radarr` for
    `radarr.service`."""
    assert st.ports_of(SS_103, "radarr") == ("7878",)
    assert st.ports_of(SS_103, "sshd") == ("22",)
    assert st.ports_of(SS_103, "sonarr") == (), "nothing of Sonarr's listens in 103"
    assert st.ports_of(SS_103, "") == ()


def test_the_ids_and_the_ownership_are_read_not_assumed():
    assert st.parse_id("uid=999(radarr) gid=996(radarr) groups=996(radarr)") == ("radarr", "999", "radarr", "996")
    assert st.parse_stat("radarr:radarr 644 /var/lib/radarr/config.xml") == ("radarr:radarr", "644", "/var/lib/radarr/config.xml")
    many = ("qbittorrent-nox:qbittorrent-nox 664 /a/qBittorrent-data.conf\n"
            "qbittorrent-nox:qbittorrent-nox 664 /a/qBittorrent.conf")
    assert len(st.parse_stats(many)) == 2


def test_who_can_rewrite_a_config_is_measured():
    """§17.1343's cause, as a fact: qBittorrent owns its conf and rewrites the whole
    file on shutdown; Caddy's Caddyfile is root-owned and safe to edit live."""
    assert QB.rewrites("/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf") is True
    assert RADARR.rewrites("/var/lib/radarr/config.xml") is True
    assert CADDY.rewrites("/etc/caddy/Caddyfile") is False
    assert RADARR.rewrites("/etc/hosts") is None, "unmeasured is unknown, not False"


def test_a_value_from_another_guest_is_refused():
    """The live draft: both calls inside 103, the second reading Sonarr's config and
    calling 8989 — where nothing of Sonarr's exists."""
    bad = ["pct exec 103 -- sh -c 'curl -s -H \"X-Api-Key: $(cat /var/lib/sonarr/config.xml | sed -n x)\" "
           "http://127.0.0.1:8989/api/v3/rootfolder'"]
    out = st.values_from_another_guest(bad, None, SERVICES)
    assert len(out) == 1, out
    why = out[0]["why"]
    assert "guest 103" in why and "sonarr on guest 104" in why
    assert "pct exec 104" in why, "it names where the call belongs"


def test_the_same_call_in_its_own_guest_is_fine():
    good = ["pct exec 104 -- sh -c 'curl -s http://127.0.0.1:8989/api/v3/rootfolder'",
            "pct exec 103 -- sh -c 'curl -s http://127.0.0.1:7878/api/v3/rootfolder'"]
    assert st.values_from_another_guest(good, None, SERVICES) == []


def test_a_port_that_exists_in_both_guests_is_not_refused():
    both = [st.ServiceTruth(guest="103", name="a", ports=("9000",), data_dir="/srv/a"),
            st.ServiceTruth(guest="104", name="b", ports=("9000",), data_dir="/srv/b")]
    assert st.values_from_another_guest(["pct exec 103 -- sh -c 'curl :9000'"], None, both) == []


def test_editing_a_config_the_service_rewrites_is_refused():
    """§17.1343's live failure, caught before the run this time."""
    edit = ["pct exec 105 -- sh -c 'sed -i \"s|x|y|\" "
            "/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf; systemctl restart qbittorrent-nox'"]
    out = st.edits_a_config_the_service_rewrites(edit, None, SERVICES)
    assert len(out) == 1, out
    why = out[0]["why"]
    assert "owner qbittorrent-nox:qbittorrent-nox, mode 664" in why
    assert "systemctl stop qbittorrent-nox" in why and "AFTER the restart" in why


def test_stopping_the_service_first_is_accepted():
    ok = ["pct exec 105 -- sh -c 'systemctl stop qbittorrent-nox; sed -i \"s|x|y|\" "
          "/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf; systemctl start qbittorrent-nox'"]
    assert st.edits_a_config_the_service_rewrites(ok, None, SERVICES) == []


def test_a_config_the_service_cannot_write_is_left_alone():
    caddy = ["pct exec 120 -- sh -c 'sed -i \"s|x|y|\" /etc/caddy/Caddyfile; systemctl reload caddy'"]
    assert st.edits_a_config_the_service_rewrites(caddy, None, SERVICES) == []


def test_the_facts_table_states_them_for_the_drafter():
    t = st.table([RADARR, SONARR])
    assert "do not infer others" in t
    assert "radarr on guest 103 · port 7878" in t and "runs as radarr (uid 999, gid 996)" in t
    assert "the service rewrites it: stop it before editing" in t
    assert st.table([]) == ""


def test_the_names_a_step_mentions_are_measured_too():
    node = {"title": "Set Radarr's root folder and Sonarr's", "description": "qbittorrent-nox saves there."}
    assert st.names_in(node) == ["radarr", "sonarr", "qbittorrent-nox"]
    assert st.names_in({"title": "Grow the disk", "description": "nothing here"}) == []


def test_nothing_is_judged_without_a_measurement():
    bad = ["pct exec 103 -- sh -c 'curl http://127.0.0.1:8989/x'"]
    assert st.values_from_another_guest(bad, None, []) == []
    assert st.edits_a_config_the_service_rewrites(bad, None, []) == []


def test_the_frame_runs_both_gates_and_redrafts_on_them():
    """Verify the lane: a gate the framer never calls is no gate, and a refusal whose
    text is not registered parks the frame instead of asking the drafter again."""
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr.frame_run)
    assert "values_from_another_guest(cmds, files, services)" in src
    assert "edits_a_config_the_service_rewrites(cmds, files, services)" in src
    assert "_svc_refused" in src.split("refused = list(refused)")[1][:200]
    for mark in ("the call can only fail", "rewrites the whole file when it stops"):
        assert mark in sr._SHAPE_REFUSALS, mark


def test_the_pause_measures_the_services_and_hands_them_over():
    import pathlib
    from app.modules import execution_agent as ea
    src = pathlib.Path(ea.__file__).read_text(encoding="utf-8")
    i = src.index("async def _pause_for_decision(")
    body = src[i:src.index("\nasync def ", i + 10)]
    assert "service_truth as _st" in body and "read_services(spec, _g" in body
    assert body.count("services=_services") >= 8, "every draft of the chain gets them"
    assert "_st2.table(_services)" in body, "and the drafter sees them as facts"
