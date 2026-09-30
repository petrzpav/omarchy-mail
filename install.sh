#!/bin/bash
#
# install.sh - put `mail` on your PATH, register it for mailto: links, link the
# Jev auto-sort timer and start a config from examples/. Safe to run again.

set -euo pipefail
root=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
bin="$HOME/.local/bin"
conf="${XDG_CONFIG_HOME:-$HOME/.config}/petrzpav-mail"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

mkdir -p "$bin" "$apps"
ln -sf "$root/bin/mail" "$bin/mail"
ln -sf "$root/bin/mail-window" "$bin/mail-window"
sed "s|^Exec=mail-window|Exec=$bin/mail-window|" "$root/mail.desktop" > "$apps/mail.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$apps" || true

if [[ ! -d $conf ]]; then
  mkdir -p "$conf"
  cp "$root/examples/config.toml" "$root/examples/categories.toml" "$root/examples/secrets" "$conf/"
  chmod 600 "$conf/secrets"
  echo "Created $conf: fill in config.toml and secrets."
fi

systemctl --user link "$root/systemd/mail-sort.service" "$root/systemd/mail-sort.timer" >/dev/null

cat <<MSG
Installed. Next:
  1. edit $conf/config.toml and $conf/secrets
  2. mail cat ls                                       # check your categories
  3. mail-window                                       # open the client
  4. systemctl --user enable --now mail-sort.timer     # let Jev sort new mail every 5 minutes
  5. xdg-mime default mail.desktop x-scheme-handler/mailto   # optional: open mailto: links here
MSG
