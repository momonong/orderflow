#!/bin/sh
# Upgrade the existing HP v0.2 service while Caddy Basic Auth still protects it.
set -eu
[ "$(id -u)" -eq 0 ] && [ "$(hostname)" = hp-ubuntu2604-server ] || {
  echo 'Run as root on hp-ubuntu2604-server.' >&2; exit 1;
}
commit=${1:-}
case "$commit" in *[!0-9a-f]*|'') echo 'Pass an exact commit SHA.' >&2; exit 1;; esac
[ "${#commit}" -eq 40 ] || { echo 'Pass an exact commit SHA.' >&2; exit 1; }
old_commit=09712b6d828259e416d4f857447544fab4b09304
stage=/home/morris/orderflow-staging/$commit
release=/opt/orderflow/releases/$commit
current=/opt/orderflow/current
unit=/etc/systemd/system/orderflow.service
credential=/etc/caddy/secrets/orderflow-auth.caddy
[ "$(readlink "$current")" = "releases/$old_commit" ] || {
  echo 'Live release differs from reviewed v0.2 baseline.' >&2; exit 1;
}
[ -f "$unit" ] && [ -f "$credential" ] && [ ! -L "$credential" ] || {
  echo 'Service unit or credential is missing.' >&2; exit 1;
}
[ "$(stat -c '%U:%G:%a' "$credential")" = root:caddy:640 ] || {
  echo 'Unexpected credential ownership or mode.' >&2; exit 1;
}
grep -Fq 'import /etc/caddy/secrets/orderflow-auth.caddy' /etc/caddy/Caddyfile || {
  echo 'Caddy Basic Auth must remain active during app upgrade.' >&2; exit 1;
}
[ -d "$stage/.venv" ] && [ -x "$stage/.venv/bin/python" ] &&
  [ -f "$stage/release-manifest.sha256" ] && [ -f "$stage/deploy/orderflow.service" ] || {
  echo 'Incomplete staged release.' >&2; exit 1;
}
[ ! -e "$release" ] && [ ! -L "$release" ] || {
  echo 'Target release already exists.' >&2; exit 1;
}
(cd "$stage" && sha256sum -c release-manifest.sha256 --status)
systemd-analyze verify "$stage/deploy/orderflow.service"
install -d -o root -g root -m 0755 "$release"
cp -a "$stage"/. "$release"/
chown -R root:root "$release"
chmod -R go-w "$release"
chmod 0755 "$release"
# Recheck the root-owned copy. Do not execute staged or release Python as root.
(cd "$release" && sha256sum -c release-manifest.sha256 --status)
backup=/var/backups/orderflow/pre-session-$commit
[ ! -e "$backup" ] || { echo 'Backup destination already exists.' >&2; exit 1; }
install -d -o root -g root -m 0700 /var/backups/orderflow "$backup" "$backup/data"
cp -p "$unit" "$backup/orderflow.service"
printf '%s\n' "releases/$old_commit" > "$backup/previous-link"
stopped=0
switched=0
finished=0
work=''
rollback() {
  result=$?
  trap - EXIT
  if [ -n "$work" ]; then rm -rf -- "$work"; fi
  if [ "$finished" -ne 1 ] && [ "$stopped" -eq 1 ]; then
    # Data is retained. The old app ignores session_auth and reads the same PDFs/jobs.
    if [ "$switched" -eq 1 ]; then
      ln -s "releases/$old_commit" "$current.rollback"
      mv -Tf "$current.rollback" "$current"
      cp -p "$backup/orderflow.service" "$unit"
      systemctl daemon-reload
    fi
    if systemctl start orderflow.service && systemctl is-active --quiet orderflow.service; then
      echo 'Upgrade failed; previous app restarted. Backup retained.' >&2
    else
      echo 'ROLLBACK FAILED: inspect the saved unit and release before changing Caddy.' >&2
    fi
  fi
  exit "$result"
}
trap rollback EXIT
stopped=1
systemctl stop orderflow.service
# A stopped service gives a consistent SQLite WAL and file snapshot. Keep all files.
cp -a /var/lib/orderflow/. "$backup/data/"
ln -s "releases/$commit" "$current.next"
mv -Tf "$current.next" "$current"
switched=1
install -o root -g root -m 0644 "$release/deploy/orderflow.service" "$unit"
systemctl daemon-reload
systemctl start orderflow.service
systemctl is-active --quiet orderflow.service
curl --fail --silent --show-error --retry 5 --retry-delay 1 --retry-connrefused --max-time 5 \
  -H 'Host: momonong.me' http://127.0.0.1:18081/orderflow/ |
  grep -Fq '網站登入'
[ "$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' -H 'Host: momonong.me' \
  http://127.0.0.1:18081/orderflow/api/bootstrap)" = 401 ]
work=$(mktemp -d /tmp/orderflow-upgrade.XXXXXX)
chmod 0700 "$work"
# Password moves only through a pipe into local curl; never argv, logs, or Git.
/usr/bin/python3 -c 'import json; from pathlib import Path; print(json.dumps({"password": Path("/etc/caddy/secrets/orderflow-password").read_text().strip()}))' |
  curl --fail --silent --show-error --max-time 10 \
    -H 'Host: momonong.me' -H 'Origin: https://momonong.me' -H 'X-Orderflow-Request: 1' \
    -H 'Content-Type: application/json' --data-binary @- \
    -D "$work/login.headers" -o "$work/login.json" http://127.0.0.1:18081/orderflow/api/login
/usr/bin/python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["status"] == "ok"' "$work/login.json"
# Secure cookies are intentionally not sent by curl over this loopback HTTP probe.
# Simulate the HTTPS browser request to the local backend without printing the token.
/usr/bin/python3 - "$work/login.headers" 18081 <<'PY_PROBE'
import http.client
import json
import sys
from pathlib import Path
from http.cookies import SimpleCookie
lines = [line for line in Path(sys.argv[1]).read_text().splitlines() if line.lower().startswith('set-cookie:')]
assert len(lines) == 1
cookie = SimpleCookie()
cookie.load(lines[0].split(':', 1)[1].strip())
assert 'of_session' in cookie
assert cookie['of_session']['secure'] and cookie['of_session']['httponly']
conn = http.client.HTTPConnection('127.0.0.1', int(sys.argv[2]), timeout=10)
conn.request('GET', '/orderflow/api/bootstrap', headers={
    'Host': 'momonong.me', 'Cookie': 'of_session=' + cookie['of_session'].value,
})
response = conn.getresponse()
assert response.status == 200
body = json.loads(response.read())
assert body['version'] == '0.3.0' and isinstance(body['documents'], list)
conn.close()
PY_PROBE
rm -rf "$work"
work=''
finished=1
echo "OrderFlow $commit active behind existing Caddy Basic Auth."
echo "Backup: $backup"
echo 'Validate app and rollback compatibility before changing the Caddy route.'
