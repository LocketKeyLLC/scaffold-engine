"""§17.1462 — on a runner that elevates only writes, `cat > /etc/…` cannot write.

Live (ADD151, 2026-10-11 02:03, fixture: the approved block): the `python3` edit of cluster.fw ran as root (a write
head) and the next line, `cat > /etc/pve/firewall/101.fw <<"EOF"`, stopped the block — "the runner is unprivileged for
READ commands (the write grant covers writes only)".
"""
from app.modules import supervised_runs as sr

POLICY = {"sudo": True, "allow": ["ANY"], "helper": "21"}
LIVE = 'cat > /etc/pve/firewall/101.fw <<"EOF"\n[OPTIONS]\nenable: 1\npolicy_in: DROP\n\n[RULES]\nGROUP media\nEOF'


def test_the_live_line_is_refused_with_the_tee_remedy():
    out = sr.read_head_redirects_into_system([LIVE], POLICY)
    assert len(out) == 1 and "is run as a READ, unprivileged" in out[0]["why"] and "| tee /etc/pve/firewall/101.fw" in out[0]["why"]


def test_tee_tmp_and_an_unprivileged_policy_pass():
    ok = ["printf '%s\\n' '[OPTIONS]' 'enable: 1' | tee /etc/pve/firewall/101.fw",
          "cat > /tmp/x.sh <<'EOF'\necho hi\nEOF",
          "python3 - <<'EOF'\nopen('/etc/x','w').write('y')\nEOF"]
    assert sr.read_head_redirects_into_system(ok, POLICY) == []
    assert sr.read_head_redirects_into_system([LIVE], {"sudo": False}) == []


def test_registered_for_redraft():
    m = "is run as a READ, unprivileged"
    assert m in sr._SHAPE_REFUSALS and sr._WHOSE_GAP[m] == "drafter"
