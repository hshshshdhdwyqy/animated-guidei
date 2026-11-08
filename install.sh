#!/usr/bin/env bash
# ============================================================================
#  RoXeT AI (Gemini) Telegram bot — one-line installer / manager
#
#  First install (on a fresh Ubuntu/Debian server, as root):
#     bash <(curl -Ls https://raw.githubusercontent.com/YOUR_GITHUB_USER/roxet-gemini-bot/main/install.sh)
#
#  You only need TWO things: the bot token (from @BotFather) and your numeric
#  admin ID (the installer can detect it for you). The Gemini API key is added
#  later from the admin panel inside the bot.
#
#  After install, run   roxet   anytime to open the management menu.
# ============================================================================

# >>> EDIT THIS LINE after you upload the project to GitHub <<<
DEFAULT_REPO="https://github.com/YOUR_GITHUB_USER/roxet-gemini-bot.git"

REPO_URL="${ROXET_REPO:-$DEFAULT_REPO}"
BRANCH="${ROXET_BRANCH:-main}"
INSTALL_DIR="${ROXET_DIR:-/opt/roxet-gemini-bot}"
SERVICE="roxet-gemini-bot"
RUN_USER="roxetbot"
CONF="/etc/roxet-gemini-bot.conf"
CMD_LINK="/usr/local/bin/roxet"

C_RED=$'\033[1;31m'; C_GRN=$'\033[1;32m'; C_YLW=$'\033[1;33m'; C_CYN=$'\033[1;36m'; C_OFF=$'\033[0m'
info()  { echo "${C_CYN}[*]${C_OFF} $*"; }
ok()    { echo "${C_GRN}[✓]${C_OFF} $*"; }
warn()  { echo "${C_YLW}[!]${C_OFF} $*"; }
die()   { echo "${C_RED}[x]${C_OFF} $*" >&2; exit 1; }

# read from the terminal even when the script itself comes from a pipe
ask() { # ask "prompt" varname [default]
  local prompt="$1" var="$2" def="${3:-}" reply
  if [ -n "$def" ]; then prompt="$prompt [$def]"; fi
  read -r -p "$prompt: " reply </dev/tty || die "No interactive terminal available."
  printf -v "$var" '%s' "${reply:-$def}"
}
confirm() { # confirm "question" -> 0 for yes
  local reply; read -r -p "$1 [y/N]: " reply </dev/tty || return 1
  [[ "$reply" =~ ^[Yy]$ ]]
}

need_root() { [ "$(id -u)" -eq 0 ] || die "Please run as root (sudo -i), then run the command again."; }

load_conf() { [ -f "$CONF" ] && . "$CONF"; REPO_URL="${ROXET_REPO:-${SAVED_REPO:-$REPO_URL}}"; BRANCH="${ROXET_BRANCH:-${SAVED_BRANCH:-$BRANCH}}"; }
save_conf() { printf 'SAVED_REPO=%q\nSAVED_BRANCH=%q\n' "$REPO_URL" "$BRANCH" > "$CONF"; chmod 600 "$CONF"; }

is_installed() { [ -f "$INSTALL_DIR/bot.py" ] && [ -f "/etc/systemd/system/$SERVICE.service" ]; }

# --------------------------------------------------------------------------- deps
install_deps() {
  info "Installing system packages…"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y >/dev/null 2>&1 || warn "apt-get update reported problems (continuing)"
  apt-get install -y python3 python3-venv python3-pip git curl ca-certificates >/dev/null 2>&1 \
    || die "Could not install packages with apt-get. Is this Ubuntu/Debian with internet access?"
  python3 - <<'PY' || die "Python 3.9+ is required (use Ubuntu 22.04 or newer)."
import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY
  ok "System packages ready."
}

# ------------------------------------------------------------------------ source
copy_tree() { # copy_tree SRC DEST  (without git data, venv, database, secrets)
  (cd "$1" && tar --exclude=.git --exclude=.venv --exclude=data --exclude=.env --exclude=__pycache__ -cf - .) \
    | (cd "$2" && tar -xf -)
}

fetch_source() { # puts a copy of the project into $1
  local dest="$1" here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
  if [ -n "$here" ] && [ -f "$here/bot.py" ] && [ -f "$here/requirements.txt" ] && [ "$here" != "$INSTALL_DIR" ]; then
    info "Using the project files next to this script ($here)."
    copy_tree "$here" "$dest"
    return 0
  fi
  case "$REPO_URL" in
    *YOUR_GITHUB_USER*|"") die "Repository URL is not set. Edit DEFAULT_REPO at the top of install.sh, or run:  ROXET_REPO=https://github.com/you/repo.git bash install.sh" ;;
  esac
  info "Downloading $REPO_URL ($BRANCH)…"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$dest/.clone" >/dev/null 2>&1 \
    || die "git clone failed. Check REPO_URL / branch, and that the repository is public."
  rm -rf "$dest/.clone/.git"
  cp -a "$dest/.clone/." "$dest/" && rm -rf "$dest/.clone"
}

deploy_source() {
  local tmp; tmp="$(mktemp -d)"
  fetch_source "$tmp"
  [ -f "$tmp/bot.py" ] || die "bot.py not found in the downloaded project."
  mkdir -p "$INSTALL_DIR"
  # remove the old code but keep the user's settings, database and virtualenv
  find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name .env ! -name data ! -name .venv -exec rm -rf {} +
  cp -a "$tmp/." "$INSTALL_DIR/"
  rm -rf "$tmp"
  chmod +x "$INSTALL_DIR/install.sh" 2>/dev/null
  printf '#!/usr/bin/env bash\nexec bash %q "$@"\n' "$INSTALL_DIR/install.sh" > "$CMD_LINK"; chmod +x "$CMD_LINK"
  save_conf
}

setup_venv() {
  info "Setting up Python environment (this takes a minute)…"
  [ -d "$INSTALL_DIR/.venv" ] || python3 -m venv "$INSTALL_DIR/.venv" || die "python3 -m venv failed."
  "$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1
  "$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt" || die "pip install failed."
  ok "Python packages installed."
}

ensure_user() {
  id "$RUN_USER" >/dev/null 2>&1 || useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$RUN_USER"
  mkdir -p "$INSTALL_DIR/data"
  chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR"
  [ -f "$INSTALL_DIR/.env" ] && chmod 600 "$INSTALL_DIR/.env"
}

write_service() {
  cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=RoXeT AI Gemini Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/bot.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null 2>&1
}

# ----------------------------------------------------------------- telegram bits
tg_username() { # token -> prints bot username, or nothing if invalid
  curl -s --max-time 20 "https://api.telegram.org/bot$1/getMe" \
    | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); print(d["result"]["username"] if d.get("ok") else "")
except Exception:
    print("")' 2>/dev/null
}

detect_admin_id() { # token botname -> prints "id" on success
  local token="$1" botname="$2" end out offset=0 uid
  curl -s "https://api.telegram.org/bot$token/deleteWebhook" >/dev/null
  end=$((SECONDS + 120))
  while [ "$SECONDS" -lt "$end" ]; do
    out="$(curl -s --max-time 30 "https://api.telegram.org/bot$token/getUpdates?timeout=20&offset=$offset" \
      | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); mx=0; uid=""
    for u in d.get("result",[]):
        mx=max(mx,u["update_id"]); m=u.get("message")
        if m and m["chat"]["type"]=="private": uid=m["from"]["id"]
    print(mx, uid)
except Exception:
    print(0, "")' 2>/dev/null)"
    set -- $out
    if [ "${1:-0}" -gt 0 ]; then offset=$(( $1 + 1 )); fi
    uid="${2:-}"
    if [ -n "$uid" ]; then
      curl -s "https://api.telegram.org/bot$token/getUpdates?offset=$offset&timeout=0" >/dev/null
      echo "$uid"; return 0
    fi
  done
  return 1
}

collect_settings() { # sets TOKEN, ADMIN
  local botname
  while true; do
    ask "Bot token from @BotFather" TOKEN
    TOKEN="${TOKEN// /}"
    botname="$(tg_username "$TOKEN")"
    if [ -n "$botname" ]; then ok "Token OK — bot is @$botname"; break; fi
    warn "Telegram rejected this token. Check it and try again (the server must reach api.telegram.org)."
  done
  echo
  echo "Admin = the Telegram account that controls the bot (numeric ID)."
  echo "  • Type your numeric ID, or"
  echo "  • just press Enter and I will detect it: you then send any message to @$botname."
  ask "Admin numeric ID (Enter = auto-detect)" ADMIN
  ADMIN="${ADMIN// /}"
  if [ -z "$ADMIN" ]; then
    info "Now open Telegram, go to @$botname and press START (or send any message). Waiting up to 2 minutes…"
    ADMIN="$(detect_admin_id "$TOKEN" "$botname")" || die "No message received. Run the installer again, or enter your ID manually (get it from @userinfobot)."
    ok "Detected your Telegram ID: $ADMIN"
  fi
  [[ "$ADMIN" =~ ^[0-9]+(,[0-9]+)*$ ]] || die "Admin ID must be numbers (several admins: 123,456)."
}

write_env() {
  local envf="$INSTALL_DIR/.env" keep_model="" keep_img="" keep_name=""
  if [ -f "$envf" ]; then
    keep_model="$(grep -E '^GEMINI_MODEL=' "$envf" | head -1)"
    keep_img="$(grep -E '^GEMINI_IMAGE_MODEL=' "$envf" | head -1)"
    keep_name="$(grep -E '^BOT_NAME=' "$envf" | head -1)"
  fi
  {
    echo "BOT_TOKEN=$TOKEN"
    echo "ADMIN_IDS=$ADMIN"
    echo "${keep_name:-BOT_NAME=RoXeT AI}"
    echo "${keep_model:-GEMINI_MODEL=gemini-3.8-flash}"
    echo "${keep_img:-GEMINI_IMAGE_MODEL=gemini-2.5-flash-image}"
  } > "$envf"
  chmod 600 "$envf"
}

start_and_check() {
  systemctl restart "$SERVICE"
  sleep 6
  if systemctl is-active --quiet "$SERVICE"; then
    ok "Bot service is running."
  else
    warn "The service did not stay up. Last log lines:"
    journalctl -u "$SERVICE" -n 25 --no-pager
    die "Fix the error above, then run:  roxet  (menu → Restart)."
  fi
}

# --------------------------------------------------------------------- commands
do_install() {
  need_root
  echo "${C_CYN}=== RoXeT AI — Gemini Telegram bot installer ===${C_OFF}"
  if is_installed; then warn "Already installed in $INSTALL_DIR — use the menu instead (run: roxet)."; return 0; fi
  install_deps
  collect_settings
  mkdir -p "$INSTALL_DIR"
  deploy_source
  write_env
  setup_venv
  ensure_user
  write_service
  start_and_check
  echo
  ok "Installation finished 🎉"
  cat <<EOF

  Next steps (in Telegram):
   1. Open your bot and press START.
   2. Tap  🛠 پنل مدیریت  →  🔑 کلید API  and send your Gemini API key
      (create it free at https://aistudio.google.com/apikey).
   3. Done! Add the bot to groups if you want — it answers /ask, mentions and replies.

  Manage the server anytime with:   roxet
EOF
}

do_update() {
  need_root; load_conf
  is_installed || die "Not installed yet."
  deploy_source
  setup_venv
  ensure_user
  write_service
  start_and_check
  ok "Updated to the latest version (your data and settings were kept)."
}

do_reconfigure() {
  need_root
  is_installed || die "Not installed yet."
  systemctl stop "$SERVICE" 2>/dev/null
  collect_settings
  write_env
  ensure_user
  start_and_check
  ok "Token / admin updated."
}

do_backup() {
  need_root
  local out="/root/roxet-backup-$(date +%Y%m%d-%H%M%S).db"
  "$INSTALL_DIR/.venv/bin/python" - "$INSTALL_DIR/data/bot.db" "$out" <<'PY' || die "Backup failed."
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()
PY
  ok "Database copied to $out"
}

do_uninstall() {
  need_root
  confirm "Really remove the bot from this server?" || return 0
  systemctl disable --now "$SERVICE" 2>/dev/null
  rm -f "/etc/systemd/system/$SERVICE.service" "$CMD_LINK" "$CONF"
  systemctl daemon-reload
  if confirm "Also delete all bot data (users, history, API key)?"; then
    rm -rf "$INSTALL_DIR"
  else
    find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name data ! -name .env -exec rm -rf {} + 2>/dev/null
    ok "Kept $INSTALL_DIR/data and .env"
  fi
  id "$RUN_USER" >/dev/null 2>&1 && ! [ -d "$INSTALL_DIR" ] && userdel "$RUN_USER" 2>/dev/null
  ok "Uninstalled."
}

show_menu() {
  need_root
  while true; do
    echo
    echo "${C_CYN}=== RoXeT AI — management ===${C_OFF}"
    systemctl is-active --quiet "$SERVICE" && echo "Status: ${C_GRN}running${C_OFF}" || echo "Status: ${C_RED}stopped${C_OFF}"
    cat <<'EOF'
  1) Update to latest version
  2) Restart bot
  3) Live logs (Ctrl+C to leave)
  4) Change bot token / admin ID
  5) Backup database
  6) Stop bot
  7) Uninstall
  0) Exit
EOF
    local c; ask "Choose" c
    case "$c" in
      1) do_update ;;
      2) systemctl restart "$SERVICE" && ok "Restarted." ;;
      3) journalctl -u "$SERVICE" -f -n 50 ;;
      4) do_reconfigure ;;
      5) do_backup ;;
      6) systemctl stop "$SERVICE" && ok "Stopped." ;;
      7) do_uninstall; return ;;
      0|q|"") return ;;
      *) warn "Unknown option." ;;
    esac
  done
}

main() {
  load_conf
  case "${1:-}" in
    install)   do_install ;;
    update)    do_update ;;
    restart)   need_root; systemctl restart "$SERVICE" && ok "Restarted." ;;
    stop)      need_root; systemctl stop "$SERVICE" && ok "Stopped." ;;
    status)    systemctl status "$SERVICE" --no-pager ;;
    logs)      journalctl -u "$SERVICE" -f -n 50 ;;
    backup)    do_backup ;;
    uninstall) do_uninstall ;;
    "")        if is_installed; then show_menu; else do_install; fi ;;
    *)         echo "Usage: roxet [install|update|restart|stop|status|logs|backup|uninstall]"; exit 1 ;;
  esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
