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
