"""§17.1963 — a program that reads a held secret from its environment is not writing the secret's name as its value."""
from app.modules.supervised_runs import a_secret_name_written_as_its_value

_POLICY = {"secrets": ["PANEL_PASSWORD", "RADARR_API_KEY"]}


def test_environment_lookups_pass():
    for body in ('pw = os.environ["PANEL_PASSWORD"]', "pw = os.environ.get('PANEL_PASSWORD', '')",
                 'pw = os.getenv("PANEL_PASSWORD")', 'pw = ENV["PANEL_PASSWORD"]', 'my $pw = $ENV{"PANEL_PASSWORD"};'):
        assert a_secret_name_written_as_its_value([], [{"path": "/tmp/x", "content": body}], _POLICY) == [], body


def test_the_name_written_as_the_value_is_still_refused():
    live = "sed -i 's|<ApiKey>.*</ApiKey>|<ApiKey>RADARR_API_KEY</ApiKey>|' /var/lib/radarr/config.xml"
    assert a_secret_name_written_as_its_value([live], [], _POLICY)
    assert a_secret_name_written_as_its_value([], [{"path": "/tmp/x", "content": 'headers = {"X-Api-Key": "RADARR_API_KEY"}'}], _POLICY)


def test_a_step_that_spells_out_its_reads_is_not_rewritten_into_an_inventory():
    from app.modules.supervised_runs import rewrite_inventory_service_routes
    node = {"title": "CT 111: read the media-library handler, the engine address and the Palworld status code",
            "prompt_template": "Read only. Run on the Proxmox host:\n`pct exec 111 -- cat /opt/control-panel-backend/media_library_route.js`"}
    cmds = ["pct exec 111 -- cat /opt/control-panel-backend/media_library_route.js"]
    assert rewrite_inventory_service_routes(node, cmds, [])[0] == cmds
