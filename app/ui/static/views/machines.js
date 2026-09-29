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
    mount(body, connectCard(d), channelCard(d), secretsCard(d));
  }

  // ── 1. the machine ─────────────────────────────────────────────────
  function connectCard(d) {
    const r = d.runner || {};
    const host = el("input", { class: "input", type: "text", placeholder: "192.168.1.156 or a hostname", value: r.host || "" });
    const port = el("input", { class: "input input-sm", type: "number", value: String(r.port || 8790) });
    const status = el("p", { class: "sub cap-line", text: d.connected ? `connected as ${r.name} at ${r.endpoint}` : "no machine connected yet" });
    const result = el("p", { class: "sub cap-line", text: "" });

    const save = el("button", { class: "btn btn-primary", text: d.connected ? "Re-point and test" : "Connect and test" });
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
      el("div", { class: "row row-wrap cap-actions" }, save, d.connected ? test : null));
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
      el("div", { class: "row row-wrap cap-actions" }, copy));
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
