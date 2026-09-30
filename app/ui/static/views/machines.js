// §17.1193 — connecting a machine is a SETTING, not a planning job.
//
// Until now the only door was "Walk me through it" on a capability card, which
// opened a research + plan + walkthrough job — nine open questions, a
// feasibility score and a DAG — to record a host, a port and a token. The
// operator said it plainly: "why am I making a whole 'nother plan just to
// connect it??? shouldn't the option already be programmed in?" Every
// comparable tool agrees: Ansible has an inventory, AWX and Jenkins have a
// Credentials form, Portainer has "Add environment", Proxmox has "Add node".
// None of them plan first. The walkthrough stays for anyone who wants it; it
// is no longer the only way in.
import * as api from "../api.js";
import { el, mount } from "../util.js";
import { errorPanel, loading, toast, askConfirm } from "../components.js";

const CLASS_TEXT = {
  ok: "answering",
  host_down: "nothing answered",
  stale_helper: "an older helper is running there",
  not_runner: "something else is on that port",
  unregistered: "no machine connected yet",
};

export default function machines(container, params, opts = {}) {
  let disposed = false;
  const body = el("div", {});
  mount(container,
    (opts && opts.embedded)
      ? el("p", { class: "dim hub-lede", text: "The machine the engine may reach, what it is allowed to run there, and the values it holds for it." })
      : el("div", { class: "view-header" },
          el("h1", { text: "Machines" }),
          el("div", { class: "sub", text: "The machine the engine may reach, what it is allowed to run there, and the values it holds for it." })),
    body);

  async function load() {
    mount(body, loading("Reading what is connected…"));
    let d;
    try {
      d = await api.get("/setup/machines");
    } catch (e) {
      if (!disposed) mount(body, errorPanel(e, load));
      return;
    }
    if (disposed) return;
    mount(body, connectCard(d), controlCard(d), activityCard(d), channelCard(d), secretsCard(d));
  }

  // ── 1. the machine ─────────────────────────────────────────────────
  function connectCard(d) {
    const r = d.runner || {};
    const host = el("input", { class: "input", type: "text", placeholder: "192.168.1.156 or a hostname", value: r.host || "" });
    const port = el("input", { class: "input input-sm", type: "number", value: String(r.port || 8790) });
    const status = el("p", { class: "sub cap-line",
      // §17.1205 — a paused machine is still connected. Reporting it as "no
      // machine connected yet" would send the operator to re-connect it.
      text: d.paused ? `paused — ${r.name} at ${r.endpoint} is still there, the engine is not using it`
        : d.connected ? `connected as ${r.name} at ${r.endpoint}` : "no machine connected yet" });
    const result = el("p", { class: "sub cap-line", text: "" });

    // §17.1205 — a PAUSED machine is connected, so the button must not offer to
    // "Connect" it: this is the re-point action either way, and inviting a
    // connect implies the address was never saved.
    const known = d.connected || d.paused;
    const save = el("button", { class: "btn btn-primary", text: known ? "Re-point and test" : "Connect and test" });
    save.addEventListener("click", async () => {
      const h = host.value.trim();
      if (!h) { toast("Enter the machine's address first.", "err"); return; }
      save.disabled = true; const was = save.textContent; save.textContent = "Testing…";
      try {
        const res = await api.post("/setup/machines/connect", { host: h, port: Number(port.value) || 8790 });
        say(result, res.probe);
        toast(res.probe && res.probe.ok ? "Connected." : "Saved — but it did not answer yet.", res.probe && res.probe.ok ? "ok" : "err");
        await load();
      } catch (e) {
        toast((e && e.message) || "Could not connect", "err");
        save.disabled = false; save.textContent = was;
      }
    });

    const test = el("button", { class: "btn btn-ghost", text: "Test again" });
    test.addEventListener("click", async () => {
      test.disabled = true; const was = test.textContent; test.textContent = "Asking…";
      try { say(result, await api.post("/setup/machines/probe", {})); } catch (e) { say(result, { detail: (e && e.message) || "probe failed" }); }
      test.disabled = false; test.textContent = was;
    });

    return el("div", { class: "card card-pad" },
      el("h3", { class: "cap-title", text: "The machine the engine can reach" }),
      el("p", { class: "cap-summary", text: "A small helper runs there and does what you approve. The engine never reaches anything you have not connected here." }),
      el("div", { class: "row row-wrap cap-actions" },
        el("label", { class: "sub", text: "Address" }), host,
        el("label", { class: "sub", text: "Port" }), port),
      status, result,
      el("div", { class: "row row-wrap cap-actions" }, save, known ? test : null));
  }

  // ── 1b. §17.1205 — stop, start, and ask it something ───────────────
  //
  // The page could connect a machine and test it, and nothing else: no way to
  // stop it, no way to ask it anything, no way to see what it had been doing.
  //
  // "Stop" is deliberately the ENGINE's use of it, not the service. The runner
  // refuses commands touching its own service by design (its denylist names
  // `local-runner-mcp`), and the engine has no other channel to the machine —
  // so the honest control here is the registry flag, and the real systemctl
  // lines are given to run where they can actually run.
  function controlCard(d) {
    if (!d.runner) return null;
    const r = d.runner;
    const out = el("pre", { class: "md-pre machines-out", text: "" });

    const pause = el("button", { class: d.paused ? "btn btn-primary" : "btn btn-ghost",
                                 text: d.paused ? "▶ Resume" : "⏸ Pause" });
    pause.addEventListener("click", async () => {
      if (!d.paused && !(await askConfirm(
        "The engine will stop sending it anything — state checks and approved blocks included. "
        + "The helper keeps running there and nothing is forgotten. You can resume from here.",
        { title: "Pause this machine?", confirmText: "Pause" }))) return;
      pause.disabled = true;
      try {
        await api.post(`/setup/machines/pause?paused=${d.paused ? "false" : "true"}`, {});
        toast(d.paused ? "Resumed." : "Paused — the engine will not use it.", "ok");
        await load();
      } catch (e) { toast((e && e.message) || "Could not change it", "err"); pause.disabled = false; }
    });

    const svc = `systemctl status local-runner-mcp\nsystemctl restart local-runner-mcp\nsystemctl stop local-runner-mcp`;
    const svcPre = el("pre", { class: "md-pre machines-install", text: svc });
    const copySvc = el("button", { class: "btn btn-sm btn-ghost", text: "⧉ copy" });
    copySvc.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(svc); toast("Copied — run it on the machine.", "ok"); }
      catch { toast("Select the lines and copy them.", "err"); }
    });

    const cmd = el("input", { class: "input", type: "text",
                              placeholder: "pvesm status", value: "" });
    const run = el("button", { class: "btn", text: "Ask it" });
    async function ask() {
      const c = cmd.value.trim();
      if (!c) { toast("Type a command that only reads.", "err"); return; }
      run.disabled = true; const was = run.textContent; run.textContent = "Asking…";
      out.textContent = "";
      try {
        const res = await api.post("/setup/machines/run", { command: c });
        out.textContent = res.ran ? (res.output || "(it printed nothing)")
          : `did not run — ${res.why}\n\n${res.output || ""}`.trim();
        if (!res.ran) toast(res.why, "err");
      } catch (e) {
        // a 422 from the read-only gate is the useful case: say what it said
        const m = (e && e.message) || "it did not run";
        out.textContent = m;
        toast(m, "err");
      }
      run.disabled = false; run.textContent = was;
      await refreshActivity();
    }
    run.addEventListener("click", ask);
    cmd.addEventListener("keydown", (ev) => { if (ev.key === "Enter") ask(); });

    return el("div", { class: "card card-pad" },
      el("h3", { class: "cap-title", text: "Start, stop, and ask it something" }),
      el("p", { class: "cap-summary", text: d.paused
        ? "Paused: the engine sends this machine nothing. The helper is still running there — resume and it picks straight back up."
        : "Pausing stops the engine using this machine, without forgetting it or touching the helper running there." }),
      el("div", { class: "row row-wrap cap-actions" }, pause),
      el("p", { class: "sub cap-line", text: "To control the service itself, run these on the machine — the engine will not stop its own runner:" }),
      svcPre,
      el("div", { class: "row row-wrap cap-actions" }, copySvc),
      el("p", { class: "sub cap-line", text: "Ask it something now. Read-only only: anything that could change the machine is refused here before it is sent." }),
      el("div", { class: "row row-wrap cap-actions" }, cmd, run),
      out);
  }

  // ── 1c. §17.1205 — what it has been doing ──────────────────────────
  const actBody = el("div", {});

  async function refreshActivity() {
    let a;
    try { a = await api.get("/setup/machines/activity"); }
    catch { mount(actBody, el("p", { class: "sub cap-line", text: "could not read the activity" })); return; }
    if (disposed) return;
    const rows = a.entries || [];
    if (!rows.length) {
      mount(actBody, el("p", { class: "sub cap-line",
        text: "Nothing yet. Commands appear here as the engine runs them — state checks, look-ups, and blocks you approve." }));
      return;
    }
    mount(actBody,
      el("p", { class: "sub cap-line", text: `${a.count} since the engine started`
        + (a.refused ? `, ${a.refused} refused` : "") + (a.running ? ` · ${a.running} running now` : "") }),
      el("ul", { class: "machines-activity" }, ...rows.map((e) => el("li", { class: "sub" },
        el("span", { class: "dim", text: (e.at || "").slice(11, 19) + " " }),
        el("span", { text: (e.ran ? "✓ " : "⨯ ") + e.command }),
        el("span", { class: "dim", text: e.ran
          ? ` — ${e.kind === "write" ? "approved block" : "read"}${e.lines ? `, ${e.lines} line${e.lines === 1 ? "" : "s"}` : ""}`
          : ` — ${e.why}` })))));
  }

  function activityCard(d) {
    if (!d.runner) return null;
    refreshActivity();
    const again = el("button", { class: "btn btn-sm btn-ghost", text: "Refresh" });
    again.addEventListener("click", refreshActivity);
    return el("div", { class: "card card-pad" },
      el("h3", { class: "cap-title", text: "What it has been doing" }),
      el("p", { class: "cap-summary", text: "Every command the engine has sent this machine, newest first. Kept in memory, so it starts over when the engine restarts." }),
      actBody,
      el("div", { class: "row row-wrap cap-actions" }, again));
  }

  function say(node, probe) {
    if (!probe) { node.textContent = ""; return; }
    const cls = CLASS_TEXT[probe.class] || probe.class || "";
    node.textContent = (cls ? cls + " — " : "") + (probe.detail || "");
  }

  // ── 2. what it may run ─────────────────────────────────────────────
  function channelCard(d) {
    const w = d.write_channel || {};
    const needed = (d.needed_prefixes || []).filter((p) => p.prefix);
    const reads = (d.needed_read_prefixes || []).filter((p) => p.prefix);   // §17.1198
    const rows = needed.map((p) =>
      el("li", { class: "sub", text: `${p.prefix} — ${p.why}${p.steps && p.steps.length ? " (" + p.steps.slice(0, 6).join(", ") + ")" : ""}` }));
    const install = el("pre", { class: "md-pre machines-install", text: d.install || "" });
    const copy = el("button", { class: "btn btn-sm btn-ghost", text: "⧉ copy" });
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(d.install || ""); toast("Copied — paste it on the machine.", "ok"); }
      catch { toast("Select the line and copy it.", "err"); }
    });
    // §17.1199 — the trust-level alternative to enumerating commands
    w.trusted = d.trust_mode === "approve";
    const trustedInstall = el("pre", { class: "md-pre machines-install", text: d.install_trusted || "" });
    const copyTrusted = el("button", { class: "btn btn-sm", text: "⧉ copy the trusted line" });
    copyTrusted.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(d.install_trusted || ""); toast("Copied — paste it on the machine.", "ok"); }
      catch { toast("Select the line and copy it.", "err"); }
    });
    return el("div", { class: "card card-pad" },
      el("h3", { class: "cap-title", text: "What the engine may run there" }),
      w.open
        ? el("p", { class: "cap-summary", text: `Open. It may run ${(w.allow || []).length} command prefix(es)${w.sudo ? " as root" : ""}, each block only after you approve it: ${(w.allow || []).join(", ")}` })
        : el("p", { class: "cap-summary", text: w.checked
            ? "Closed — the helper there was installed without a write list, so the engine can only read."
            : "Not checked yet. Press Test again above, or run the line below." }),
      w.privilege_note ? el("p", { class: "sub cap-line warn-line", text: w.privilege_note }) : null,
      needed.length
        ? el("div", {},
            el("p", { class: "sub cap-line", text: "Your open plan's remaining steps need these, read off the plan itself:" }),
            el("ul", {}, ...rows))
        : el("p", { class: "sub cap-line", text: "No step still to do needs a command run for you." }),
      reads.length
        ? el("div", {},
            el("p", { class: "sub cap-line", text: "And these it must be allowed to READ as root — on a Proxmox host even a check goes through /etc/pve, so an unprivileged runner cannot look at all:" }),
            el("ul", {}, ...reads.map((p) => el("li", { class: "sub", text: `${p.prefix} — ${p.why}` }))))
        : null,
      el("p", { class: "sub cap-line", text: "Run this once on the machine — it installs or replaces the helper with exactly these allowed:" }),
      install,
      el("div", { class: "row row-wrap cap-actions" }, copy),
      // §17.1199 — the list above can only be completed by FAILING: each step
      // turns up a command nobody predicted, and each one costs a console
      // round-trip. The operator already approves every block; this offers the
      // trust level that matches the decision they are actually making.
      el("hr", { class: "cap-rule" }),
      el("p", { class: "cap-summary", text: w.trusted
        ? "This machine is set to run anything you approve. Each block still stops for your approval, each command is still signed, and the never-allowed list (host power, disk destroyers) still refuses."
        : "Or: stop listing commands. The list above grows every time a step needs something nobody predicted, and each addition costs you a paste on the machine. You already read and approve every block — this makes that the whole decision." }),
      w.trusted ? null : el("p", { class: "sub cap-line warn-line", text:
        "What changes: the runner may run any command the engine puts in front of you, as root, once you press the approve button for that block. What does not: nothing runs unapproved, every command is signed and single-use, and the never-allowed list still refuses host power and disk destroyers." }),
      w.trusted ? null : trustedInstall,
      w.trusted ? null : el("div", { class: "row row-wrap cap-actions" }, copyTrusted));
  }

  // ── 3. the values it holds ─────────────────────────────────────────
  function secretsCard(d) {
    const list = el("div", {});
    const held = d.secrets || [];
    if (d.secrets_error) {
      list.append(el("p", { class: "sub cap-line", text: d.secrets_error }));
    } else if (!held.length) {
      list.append(el("p", { class: "sub cap-line", text: "Nothing stored. A step that needs a password will ask you for it once, here or at the run." }));
    } else {
      for (const s of held) {
        const del = el("button", { class: "btn btn-sm btn-ghost", text: "Forget" });
        del.addEventListener("click", async () => {
          if (!(await askConfirm(`Forget ${s.name}? A step that needs it will ask again.`, { title: "Forget this value", danger: true }))) return;
          try { await api.del(`/setup/secrets/${encodeURIComponent(s.name)}`); toast(`${s.name} forgotten.`, "ok"); await load(); }
          catch (e) { toast((e && e.message) || "Could not forget it", "err"); }
        });
        list.append(el("div", { class: "row row-wrap cap-actions" },
          el("code", { text: s.name }),
          el("span", { class: "sub", text: s.hint || "" }),
          el("span", { class: "spacer" }), del));
      }
    }
    const name = el("input", { class: "input input-sm", type: "text", placeholder: "DB_PASSWORD" });
    const value = el("input", { class: "input", type: "password", placeholder: "the value — stored encrypted, never shown again" });
    const add = el("button", { class: "btn btn-primary", text: "Store it" });
    add.addEventListener("click", async () => {
      const n = name.value.trim().toUpperCase();
      if (!n || !value.value) { toast("A name and a value.", "err"); return; }
      add.disabled = true;
      try {
        await api.put(`/setup/secrets/${encodeURIComponent(n)}`, { value: value.value, runner: (d.runner || {}).name || null });
        value.value = ""; name.value = "";
        toast(`${n} stored.`, "ok");
        await load();
      } catch (e) {
        toast((e && e.message) || "Could not store it", "err");
        add.disabled = false;
      }
    });
    return el("div", { class: "card card-pad" },
      el("h3", { class: "cap-title", text: "Values the engine holds for it" }),
      el("p", { class: "cap-summary", text: "A password or token a step needs. Typed once and kept encrypted; the engine writes only its NAME into a command and hands the value to the machine out of band, so it never appears in the command, the transcript, the helper's log or that machine's process list." }),
      list,
      el("div", { class: "row row-wrap cap-actions" }, name, value, add));
  }

  load();
  return () => { disposed = true; };
}
