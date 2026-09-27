"""§17.1183 — a step is hands-on because of what it DOES, not its tool tag.

Fixtures are the live home-lab plan's inserted steps (2026-09-27), verbatim:
LLM-tagged, no fenced commands, the verification command inline — invisible
to the §17.624 tag count, which saw 37% Shell on a plan that is nearly all
host work."""
import inspect

import pytest

from app.modules.step_classify import command_writes, step_commands, step_is_hands_on

ADD82 = {"tool": "LLM", "node_type": "task", "title": "Install and enable QEMU Guest Agent in VM 106",
         "prompt_template": "Install qemu-guest-agent inside the palworld-server guest and enable the agent on the VM "
                            "(agent=1 on the VM config), then confirm the agent responds. Done when `qm agent 106 ping` "
                            "returns a successful reply from the guest."}
ADD50 = {"tool": "LLM", "title": "Start container 111 (control-panel)",
         "prompt_template": "Bring LXC container 111 (control-panel) back up so its backend can be launched. Done when "
                            "`pct status 111` reports 'status: running'."}
ADD17 = {"tool": "LLM", "title": "Install the NVIDIA driver 580.173.02 on the Proxmox host",
         "prompt_template": "Install the NVIDIA driver (version 580.173.02) on the Proxmox host (root@pve) so that the "
                            "nvidia-smi utility becomes available there. Done when running `nvidia-smi` on root@pve returns "
                            "the driver version 580.173.02 and reports the Tesla P40."}
ADD88 = {"tool": "LLM", "title": "Install Caddy and write the Caddyfile inside LXC 120",
         "prompt_template": "Install Caddy inside container 120 (caddy-proxy) and create /etc/caddy/Caddyfile with the "
                            "17-line content, writing it block-by-block via `tee -a`. Done when "
                            "`caddy validate --config /etc/caddy/Caddyfile` prints 'Valid configuration'."}
ADD92 = {"tool": "LLM", "title": "Attach veth105i0 to vmbr0",
         "prompt_template": "Set the master of the veth105i0 interface to vmbr0 so it is bridged onto the flat network. "
                            "Done when 'ip -brief link show veth105i0' reports master vmbr0 (no longer fwbr105i0)."}
ADD68 = {"tool": "LLM", "title": "Set static IP on control-panel container 111",
         "prompt_template": "Add ip=192.168.1.25/24,gw=192.168.1.1 to net0 of container 111 (control-panel) via pct set, "
                            "then reboot the container and confirm it holds 192.168.1.25/24 with gateway 192.168.1.1."}
ADD90 = {"tool": "LLM", "title": "Re-verify the host firewall rule counters for port 8790",
         "prompt_template": "Re-run the host firewall rule check (iptables -L PVEFW-HOST-IN -n -v | grep 8790) on root@pve "
                            "to capture the current packet/byte counters and record them in the transcript."}
T38 = {"tool": "LLM", "title": "Document architecture and setup",
       "prompt_template": "Starts from T37. Write comprehensive documentation covering network topology, VLANs, firewall "
                          "rules, service deployment, GPU passthrough, VPN access, and maintenance procedures."}
RESEARCH = {"tool": "LLM", "title": "Summarise the options for GPU passthrough",
            "prompt_template": "Compare VFIO passthrough with vGPU for a Tesla P40 and recommend one, with trade-offs."}
CODE = {"tool": "CodeGen", "title": "Build control panel backend",
        "prompt_template": "Write the Express backend. Done when `npm test` passes and `curl localhost:3001/health` returns 200."}


def test_a_step_that_writes_to_a_machine_is_hands_on():
    on, why = step_is_hands_on(ADD88, shell_backend=False, mcp_enabled=True)
    assert on and why.startswith("writes:tee -a")


def test_a_step_whose_completion_is_observed_by_a_command_is_hands_on():
    for node, cmd in ((ADD82, "qm agent 106 ping"), (ADD50, "pct status 111"), (ADD92, "ip -brief link show veth105i0")):
        on, why = step_is_hands_on(node, shell_backend=False, mcp_enabled=True)
        assert on and why == f"observed:{cmd}", (node["title"], why)


def test_a_user_at_host_target_with_a_command_is_hands_on():
    on, why = step_is_hands_on(ADD17, shell_backend=False, mcp_enabled=True)
    assert on and (why.startswith("observed:nvidia-smi") or why == "target:root@pve")


def test_an_unquoted_command_still_counts_when_its_head_is_known():
    on, why = step_is_hands_on(ADD68, shell_backend=False, mcp_enabled=True)
    assert on and why == "writes:pct set"
    on, why = step_is_hands_on(ADD90, shell_backend=False, mcp_enabled=True)
    assert on and why.startswith("writes:iptables -L"), why   # `| grep` — a pipe the AST sees; the head table calls bare iptables a write


def test_prose_that_happens_to_start_with_a_command_word_is_prose():
    for text in ("Make sure the ip address is static.", "kill the process that holds the port.",
                 "The service (control-panel) should git a new repo.", "docker images are large."):
        node = {"tool": "LLM", "title": "t", "prompt_template": text}
        assert step_is_hands_on(node, shell_backend=False, mcp_enabled=True) == (False, ""), text


def test_writing_and_thinking_steps_stay_executable():
    for node in (T38, RESEARCH):
        assert step_is_hands_on(node, shell_backend=False, mcp_enabled=True) == (False, "")


def test_tools_with_their_own_executor_keep_their_classification():
    assert step_is_hands_on(CODE, shell_backend=False, mcp_enabled=True) == (False, "")   # the sandbox runs it
    assert step_is_hands_on({"tool": "Shell", "title": "x"}, shell_backend=False, mcp_enabled=True) == (True, "tool:shell")
    assert step_is_hands_on({"tool": "Shell", "title": "x"}, shell_backend=True, mcp_enabled=True) == (False, "")
    assert step_is_hands_on({"tool": "human"}, shell_backend=True, mcp_enabled=True) == (True, "tool:human")
    assert step_is_hands_on({}, shell_backend=False, mcp_enabled=True) == (False, "")


def test_unparsable_is_not_evidence():
    """§17.1165 — a detector that decides 'is this hands-on?' fails OPEN."""
    assert command_writes("if [ then ((( ") is None
    assert command_writes("") is None
    assert command_writes("tee -a /etc/x") is True
    assert command_writes("sudo apt install -y qemu-guest-agent") is True
    assert command_writes("qm status 106") is False
    assert command_writes("cat /etc/hosts > /tmp/copy") is True
    node = {"tool": "LLM", "title": "Explain", "prompt_template": "Done when `((( broken` shows."}
    assert step_is_hands_on(node, shell_backend=False, mcp_enabled=True) == (False, "")


def test_step_commands_finds_inline_and_fenced_but_not_paths_or_values():
    cmds = [c for c, _ in step_commands("Create `/etc/caddy/Caddyfile` (`agent=1`) via `tee -a`.\n```bash\nqm start 106\n```")]
    assert cmds == ["qm start 106", "tee -a"]


def test_the_four_consumers_are_wired():
    from app.modules import execution_agent, execution_compile
    gate = inspect.getsource(execution_agent._classify_dag_executability)
    assert "step_is_hands_on(dict(r))" in gate and '== "checkpoint"' in gate and "hands_on_by_text" in gate
    run = inspect.getsource(execution_agent.execute_next_node)
    assert "hands_on_step_as_runbook" in run and 'tool, tool_lower = "Shell", "shell"' in run
    comp = inspect.getsource(execution_compile)
    assert comp.count("step_is_hands_on(dict(") >= 2, "the banner count and the deliverable kind both read what a step does"
