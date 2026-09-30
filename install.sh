#!/bin/bash
#
# install.sh - put `mail` on your PATH, register it for mailto: links, link the
# Jev auto-sort timer and start a config from examples/. Safe to run again: it
# never replaces a file it didn't create, and never touches an existing config.
#
#   install.sh              install
#   install.sh --remove     undo all of it (your config and mail cache stay)

set -euo pipefail
root=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
bin="$HOME/.local/bin"
conf="${XDG_CONFIG_HOME:-$HOME/.config}/petrzpav-mail"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
desktop="$apps/mail.desktop"
ours="# petrzpav.mail"

# A link of ours: points into this plugin.
mine_link() { [[ -L $1 && $(readlink -f "$1") == "$root"/* ]]; }

link() {
  local target="$bin/$(basename "$1")"
  if [[ -e $target || -L $target ]] && ! mine_link "$target"; then
    echo "skipped $target: it already exists and isn't from this plugin"
    return
  fi
  ln -sfn "$1" "$target"
}

if [[ ${1:-} == --remove ]]; then
  systemctl --user disable --now mail-sort.timer >/dev/null 2>&1 || true
  for unit in mail-sort.timer mail-sort.service; do
    f="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$unit"
    mine_link "$f" && rm -f "$f"
  done
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  for f in "$bin/mail" "$bin/mail-window"; do
    mine_link "$f" && rm -f "$f"
  done
  [[ -f $desktop ]] && grep -qx "$ours" "$desktop" && rm -f "$desktop"
  echo "Removed. Your config ($conf) and mail cache (~/.cache/petrzpav-mail) are left in place."
  exit 0
fi

mkdir -p "$bin" "$apps"
link "$root/bin/mail"
link "$root/bin/mail-window"

if [[ -e $desktop ]] && ! grep -qx "$ours" "$desktop"; then
  echo "skipped $desktop: it already exists and isn't from this plugin"
else
  { echo "$ours"; sed "s|^Exec=mail-window|Exec=$bin/mail-window|" "$root/mail.desktop"; } > "$desktop"
  command -v update-desktop-database >/dev/null && update-desktop-database "$apps" || true
fi

if [[ ! -e $conf ]]; then
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
  4. systemctl --user enable --now mail-sort.timer     # optional: Jev sorts new mail every 5 minutes
  5. xdg-mime default mail.desktop x-scheme-handler/mailto   # optional: open mailto: links here
MSG
