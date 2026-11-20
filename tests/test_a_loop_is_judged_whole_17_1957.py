"""§17.1957 — a read-only body's multi-line blocks are judged as whole statements, not line by line."""
from app.modules.runbook_templates import shell_statements, strict_line

# the live T2v2 shape (job 7cc2a7ac, "Verify the Caddy vhost on CT 120"), addresses masked
_LIVE = """set -u
echo "=== caddy.service state ==="
systemctl is-active caddy.service
for p in /etc/caddy/Caddyfile /etc/caddy/caddy.json /root/Caddyfile; do
  if [ -e "$p" ]; then echo "EXISTS: $p"; ls -l "$p"; else echo "MISSING: $p"; fi
done
pid=$(systemctl show caddy.service -p ExecMainPID --value)
if [ -n "${pid:-}" ] && [ "$pid" != "0" ] && [ -r "/proc/$pid/cmdline" ]; then
  tr '\\0' ' ' < "/proc/$pid/cmdline"; echo
  echo "--- cwd ---"; ls -l "/proc/$pid/cwd" 2>&1 || true
fi
if command -v caddy >/dev/null 2>&1; then
  caddy list-modules 2>&1 | head -n 5 || true
fi
curl -s -m 10 -o /dev/null -w 'upstream status=%{http_code}\\n' http://10.0.0.25:3001/ || true
journalctl -u caddy.service -n 40 --no-pager 2>&1 || true"""


def test_the_live_read_only_inventory_is_marked_read_only():
    assert strict_line(_LIVE).startswith("set +e")


def test_a_write_inside_a_loop_still_makes_the_block_strict():
    body = "for f in /tmp/a /tmp/b; do\n  rm -f \"$f\"\ndone\nsystemctl is-active caddy"
    assert strict_line(body) == "set -e"


def test_a_write_in_a_condition_still_makes_the_block_strict():
    body = "if systemctl restart caddy; then\n  echo restarted\nfi"
    assert strict_line(body) == "set -e"


def test_a_write_inside_a_branch_still_makes_the_block_strict():
    body = "if command -v caddy >/dev/null 2>&1; then\n  systemctl restart caddy\nfi"
    assert strict_line(body) == "set -e"


def test_statements_join_blocks_and_keep_plain_lines():
    got = shell_statements(["echo a", "for p in x y; do", "if [ -e \"$p\" ]; then", "echo \"$p\"", "fi", "done", "echo b"])
    assert got == ["echo a", "for p in x y; do if [ -e \"$p\" ]; then echo \"$p\"; fi; done", "echo b"]


def test_a_keyword_inside_quotes_opens_no_block():
    assert shell_statements(['echo "for the record, if any"', "echo b"]) == ['echo "for the record, if any"', "echo b"]
