"""§17.958-960 — instructions the operator can actually act on.

All three cases are from live session 613dd1df (2026-09-06), not invented:
  T32 14:47 — an arrow-key menu answered with "simply type `eslint`"
  T31 14:03 — "explain the firewall in the web browser" answered with `pct start`
  T33 15:28 — a heredoc whose `${…}` the outer shell would have eaten
"""
import inspect

from app.modules.assist_directives import (
    apply_interactive_prompt,
    apply_interface_fidelity,
)
from app.modules.assist_guide import (
    find_unescaped_expansions,
    repair_unescaped_expansions,
)
from app.modules.assist_policy import (
    answer_dodges_the_interface,
    detect_interactive_prompt,
    looks_like_gui_question,
)

# The operator's actual paste — clack's rail flattened to `|`/`o` on copy.
_CLACK_MENU = """root@pve:~# pct exec 111 -- npm create vite@latest /opt/ui -- --template react

> npx
> create-vite /opt/ui --template react

|
o  Which linter to use?
|  ESLint
"""

_FIREWALL_Q = ("I need a better explanation on implementing the firewall in the "
               "web browser. There are far more options then what you are "
               "telling me to add with not enough context.")


# ── §17.958 — a command that stops and waits ──────────────────────────────


def test_an_arrow_key_menu_is_detected_as_a_select():
    got = detect_interactive_prompt(_CLACK_MENU)
    assert got and got["kind"] == "select"
    assert "arrow" in got["how"].lower()
    # The live mistake, pinned: never tell them to type the option.
    assert "do not type" in got["how"].lower()


def test_the_operator_saying_it_is_asking_counts():
    """Their words at 14:47 were the only signal that turn."""
    got = detect_interactive_prompt("its asking whick linter to use")
    assert got and got["kind"] == "unknown"
    assert "paste" in got["how"].lower()   # ask, don't guess the keys


def test_yes_no_and_free_text_prompts_are_distinguished():
    assert detect_interactive_prompt("Ok to proceed? (y)")["kind"] == "confirm"
    assert detect_interactive_prompt("Continue? [y/N]")["kind"] == "confirm"
    assert detect_interactive_prompt("? Project name: ")["kind"] == "text"


def test_ordinary_command_output_is_not_a_prompt():
    for benign in ("added 143 packages, and audited 144 packages in 16s",
                   "v20.20.2",
                   "Task finished with 1 warning(s)!",
                   ""):
        assert detect_interactive_prompt(benign) is None, benign


def test_the_directive_forbids_re_issuing_the_command():
    out = apply_interactive_prompt("SYS", prompt=detect_interactive_prompt(_CLACK_MENU))
    assert "Do NOT re-issue that command" in out
    assert "KEYSTROKE" in out
    assert "arrow" in out.lower()
    assert apply_interactive_prompt("SYS", prompt=None) == "SYS"
    assert apply_interactive_prompt("SYS", prompt={"how": "x"}, enabled=False) == "SYS"


# ── §17.959 — answer in the interface that was asked about ────────────────


def test_the_firewall_question_is_gui_scoped():
    assert looks_like_gui_question(_FIREWALL_Q)


def test_a_cli_question_is_not_gui_scoped():
    for q in ("how do i install nodejs",
              "what does this error mean",
              "pct exec is failing, why?"):
        assert not looks_like_gui_question(q), q


def test_the_live_deflection_is_caught():
    """Verbatim shape of the 14:03 reply: declines the interface, hands a CLI
    command instead."""
    deflection = ("Since the Web UI is confusing and the `pct` command doesn't "
                  "support security groups, skip the security groups for now "
                  "and just start the container.\n```bash\npct start 111\n```")
    assert answer_dodges_the_interface(deflection, _FIREWALL_Q)


def test_a_real_gui_answer_passes():
    good = ("Go to **Datacenter → Firewall → Security Group** and click **Add**. "
            "In the dialog, the Name field takes `management`; leave Comment "
            "empty.")
    assert not answer_dodges_the_interface(good, _FIREWALL_Q)


def test_a_non_gui_question_is_never_flagged():
    assert not answer_dodges_the_interface("run `apt install nodejs`",
                                           "how do i install nodejs")


def test_the_directive_forbids_the_cli_substitution():
    out = apply_interface_fidelity("SYS", gui=True)
    assert "ANSWER THE GUI QUESTION" in out
    assert "do not tell them to skip the interface" in out.lower()
    assert "every field" in out.lower()
    assert apply_interface_fidelity("SYS", gui=False) == "SYS"


# ── §17.960 — the outer shell eats the file's contents ────────────────────


_NESTED_WRITE = """```bash
pct exec 111 -- bash -c "cat > /opt/app/server.js <<'EOF'
const url = `https://${HOST}:8006/api2/json`;
const home = ${HOME};
EOF"
```"""


def test_unescaped_expansions_inside_a_quoted_heredoc_are_found():
    """Measured under a pty: these are substituted into the FILE, silently."""
    assert find_unescaped_expansions(_NESTED_WRITE)


def test_they_are_escaped_in_place():
    out, notes = repair_unescaped_expansions(_NESTED_WRITE)
    assert "\\${HOST}" in out and "\\${HOME}" in out and "\\`https" in out
    assert notes and "escaped" in notes[0]
    # the command line itself is untouched
    assert 'pct exec 111 -- bash -c "cat > /opt/app/server.js' in out


def test_a_true_outer_heredoc_needs_no_escaping():
    """No enclosing double quote means no expansion — escaping it would put
    literal backslashes in the operator's file."""
    plain = ("```bash\ncat > /opt/app/server.js <<'EOF'\n"
             "const home = ${HOME};\nEOF\n```")
    assert find_unescaped_expansions(plain) == []
    assert repair_unescaped_expansions(plain)[0] == plain


def test_escaping_is_idempotent():
    once, _ = repair_unescaped_expansions(_NESTED_WRITE)
    twice, notes = repair_unescaped_expansions(once)
    assert twice == once and notes == []


def test_a_bare_dollar_is_left_alone():
    block = '```bash\nbash -c "cat > /tmp/f <<\'EOF\'\ncost is 5 $ total\nEOF"\n```'
    assert repair_unescaped_expansions(block)[0] == block


# ── wiring ────────────────────────────────────────────────────────────────


def test_all_three_reach_the_paths_that_produced_them():
    from app.modules import assist_guide, assist_research_lib

    ask = inspect.getsource(assist_research_lib.research_one)
    assert "apply_interactive_prompt" in ask      # §17.958 — live path
    assert "apply_interface_fidelity" in ask      # §17.959 — live path
    assert "answer_dodges_the_interface" in ask   # §17.959 — enforcement

    fix = inspect.getsource(assist_guide.generate_fix)
    assert "apply_interactive_prompt" in fix
    assert "apply_interface_fidelity" in fix
    assert "repair_unescaped_expansions" in fix   # §17.960

    guide = inspect.getsource(assist_guide.generate_guidance)
    assert "repair_unescaped_expansions" in guide
