#!/bin/sh
# Fresh HP installation of one reviewed, staged OrderFlow commit.
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run as root with sudo.' >&2
  exit 1
fi
commit=${1:-}
case "$commit" in
  *[!0-9a-f]*|'') echo 'Pass the exact 40-character Git commit SHA.' >&2; exit 1 ;;
esac
if [ "${#commit}" -ne 40 ]; then
  echo 'Pass the exact 40-character Git commit SHA.' >&2
  exit 1
fi
stage=/home/morris/orderflow-staging/$commit
release=/opt/orderflow/releases/$commit
unit=/etc/systemd/system/orderflow.service
if [ ! -d "$stage/.venv" ] || [ ! -x "$stage/.venv/bin/python" ] || [ ! -f "$stage/release-manifest.sha256" ]; then
  echo 'Staged source, manifest, or virtual environment is missing.' >&2
  exit 1
fi
if [ -e "$release" ] || [ -e /opt/orderflow/current ] || [ -L /opt/orderflow/current ] || [ -e "$unit" ]; then
  echo 'An OrderFlow release or service already exists; inspect before changing it.' >&2
  exit 1
fi
if ss -H -ltn '( sport = :18081 )' | grep -q .; then
  echo 'Port 18081 is in use; stop only the known staging process before installing.' >&2
  exit 1
fi
(cd "$stage" && sha256sum -c release-manifest.sha256 --status)
installed=0
rollback() {
  status=$?
  trap - EXIT
  if [ "$installed" -ne 1 ]; then
    systemctl disable --now orderflow.service >/dev/null 2>&1 || :
    rm -f "$unit" /opt/orderflow/current
    systemctl daemon-reload >/dev/null 2>&1 || :
    echo "Install failed (status $status). Service and current link removed; release retained at $release for diagnosis." >&2
    echo 'Inspect: journalctl -u orderflow.service -n 50 --no-pager' >&2
  fi
  exit "$status"
}
trap rollback EXIT
install -d -o root -g root -m 0755 /opt/orderflow /opt/orderflow/releases
install -d -o root -g root -m 0755 "$release"
cp -a "$stage"/. "$release"/
chown -R root:root "$release"
chmod -R go-w "$release"
chmod 0755 "$release"
"$release/.venv/bin/python" -c 'import orderflow.app, pypdf'
ln -s "releases/$commit" /opt/orderflow/current
install -o root -g root -m 0644 "$release/deploy/orderflow.service" "$unit"
systemd-analyze verify "$unit"
systemctl daemon-reload
systemctl enable --now orderflow.service
systemctl is-active --quiet orderflow.service
curl --fail --silent --show-error --retry 5 --retry-delay 1 --retry-connrefused --max-time 5 \
  -H 'Host: momonong.me' http://127.0.0.1:18081/orderflow/api/health |
  "$release/.venv/bin/python" -c 'import json,sys; data=json.load(sys.stdin); assert data["status"] == "ok" and data["mode"] == "mock-and-real"'
installed=1
echo "OrderFlow release $commit active on 127.0.0.1:18081."
