"""§17.1612 — a sourced file assigns: Docker's own install line is not "a variable nothing sets".

Live (job 0fb4ea7f T7 "Install Docker Engine", 2026-10-15): `echo "deb [...] https://download.docker.com/linux/ubuntu
$(. /etc/os-release && echo "$VERSION_CODENAME") stable"` was refused: "`$VERSION_CODENAME` is read here and nothing sets it".
"""
from __future__ import annotations

from app.modules.supervised_runs import variables_nothing_sets

DOCKER = ('echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] '
          'https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" '
          '| tee /etc/apt/sources.list.d/docker.list')


def test_the_live_docker_line_passes():
    assert variables_nothing_sets([DOCKER], [], {"secrets": []}) == []


def test_lsb_release_and_source_spelling():
    assert variables_nothing_sets(['source /etc/lsb-release && echo "$DISTRIB_CODENAME"'], [], {"secrets": []}) == []


def test_a_name_the_release_file_does_not_define_is_still_refused():
    out = variables_nothing_sets(['. /etc/os-release && echo "$MY_TOKEN"'], [], {"secrets": []})
    assert len(out) == 1 and "$MY_TOKEN" in out[0]["why"]


def test_an_unknown_sourced_file_judges_nothing_and_no_source_still_bites():
    assert variables_nothing_sets(['. /opt/app/env.sh && echo "$APP_PORT"'], [], {"secrets": []}) == []
    assert variables_nothing_sets(['echo "$APP_PORT"'], [], {"secrets": []}) != []
