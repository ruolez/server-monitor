#!/usr/bin/env bash
#
# Server Monitor — installer / updater / remover for Ubuntu 24.04 LTS.
#
# One-line install (interactive menu):
#   curl -fsSL https://raw.githubusercontent.com/ruolez/server-monitor/main/install.sh | sudo bash
#
# Non-interactive:
#   curl -fsSL https://raw.githubusercontent.com/ruolez/server-monitor/main/install.sh | sudo bash -s -- install
#   curl -fsSL https://raw.githubusercontent.com/ruolez/server-monitor/main/install.sh | sudo bash -s -- update
#   curl -fsSL https://raw.githubusercontent.com/ruolez/server-monitor/main/install.sh | sudo bash -s -- remove
#
# Already cloned:
#   sudo bash /opt/server-monitor/install.sh           # menu
#   sudo bash /opt/server-monitor/install.sh status    # direct
#
set -euo pipefail

# ---------------------------- globals -----------------------------------------

REPO_URL="https://github.com/ruolez/server-monitor.git"
INSTALL_DIR="/opt/server-monitor"
DEFAULT_PORT="8765"
BACKUP_KEEP=14
BACKUP_CRON_FILE="/etc/cron.d/server-monitor-backup"
BACKUP_CRON_LOG="/var/log/server-monitor-backup.log"

# ---------------------------- ui ---------------------------------------------

if [ -t 1 ]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'
    C_DIM=$'\033[2m'
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'; C_CYAN=$'\033[36m'
else
    C_RESET=""; C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_CYAN=""
fi

info()  { printf '%s[*]%s %s\n' "$C_BLUE"   "$C_RESET" "$*"; }
ok()    { printf '%s[+]%s %s\n' "$C_GREEN"  "$C_RESET" "$*"; }
warn()  { printf '%s[!]%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()   { printf '%s[x]%s %s\n' "$C_RED"    "$C_RESET" "$*" >&2; }
die()   { err "$*"; exit 1; }

print_banner() {
    cat <<BANNER
${C_BOLD}${C_CYAN}
==========================================
  Server Monitor — Installer
==========================================${C_RESET}
BANNER
}

prompt_with_default() {
    local prompt="$1" default="${2-}"
    local reply
    if [ -t 0 ]; then
        if [ -n "$default" ]; then
            read -rp "$prompt [$default]: " reply
        else
            read -rp "$prompt: " reply
        fi
    fi
    printf '%s' "${reply:-$default}"
}

prompt_yes_no() {
    # $1 = prompt, $2 = default y|n
    local prompt="$1" default="${2:-n}" reply
    local hint="[y/N]"; [ "$default" = "y" ] && hint="[Y/n]"
    if [ ! -t 0 ]; then
        [ "$default" = "y" ]
        return $?
    fi
    read -rp "$prompt $hint " reply
    reply="${reply:-$default}"
    [[ "$reply" =~ ^[Yy]([Ee][Ss])?$ ]]
}

# ---------------------------- checks -----------------------------------------

has_cmd() { command -v "$1" >/dev/null 2>&1; }

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        die "This action requires root. Re-run with: sudo bash $0 ${ACTION:-}"
    fi
}

require_ubuntu() {
    if [ ! -r /etc/os-release ]; then
        warn "Cannot detect distro (no /etc/os-release). Proceeding anyway."
        return
    fi
    # shellcheck source=/dev/null
    . /etc/os-release
    if [ "${ID:-}" != "ubuntu" ]; then
        warn "Detected '${PRETTY_NAME:-unknown}' — this script targets Ubuntu 24.04. It may still work on Debian-likes."
    fi
}

detect_state() {
    # Echoes one of: not_installed, stopped, running
    if [ ! -d "$INSTALL_DIR/.git" ] && [ ! -f "$INSTALL_DIR/docker-compose.yml" ]; then
        echo "not_installed"; return
    fi
    if has_cmd docker && docker compose version >/dev/null 2>&1; then
        if docker compose -f "$INSTALL_DIR/docker-compose.yml" ps --status running 2>/dev/null \
            | grep -q server-monitor; then
            echo "running"; return
        fi
    fi
    echo "stopped"
}

# ---------------------------- docker install ---------------------------------

install_docker_if_missing() {
    if has_cmd docker && docker compose version >/dev/null 2>&1; then
        ok "Docker already installed: $(docker --version | head -1)"
        return
    fi
    info "Installing Docker Engine + Compose plugin (Docker official apt repo)..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg lsb-release

    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    local codename
    codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-noble}")"
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $codename stable" \
        > /etc/apt/sources.list.d/docker.list

    apt-get update -qq
    apt-get install -y -qq \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin

    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
        usermod -aG docker "$SUDO_USER" || true
        info "Added '$SUDO_USER' to the 'docker' group (effective on next login)."
    fi
    systemctl enable --now docker >/dev/null
    ok "Docker installed: $(docker --version)"
}

# ---------------------------- legacy cleanup ---------------------------------

# Older versions persisted a ping_group_range sysctl for the (since removed)
# host-network scheduler. The scheduler now runs on the bridge network where
# Docker enables unprivileged ICMP itself, so the host sysctl is unnecessary.
SYSCTL_FILE="/etc/sysctl.d/99-server-monitor.conf"

remove_legacy_sysctl() {
    if [ -f "$SYSCTL_FILE" ]; then
        rm -f "$SYSCTL_FILE"
        info "Removed legacy $SYSCTL_FILE (no longer needed; scheduler uses bridge networking)."
    fi
}

# ---------------------------- db backup --------------------------------------

do_db_backup() {
    mkdir -p "$INSTALL_DIR/backups"
    chmod 700 "$INSTALL_DIR/backups"
    local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
    local backup_file="$INSTALL_DIR/backups/db-$stamp.sql.gz"
    if ( cd "$INSTALL_DIR" && docker compose ps --status running 2>/dev/null | grep -q server-monitor-db ); then
        ( cd "$INSTALL_DIR" \
          && docker compose exec -T postgres pg_dump -U monitor monitor \
          | gzip > "$backup_file" )
        ok "DB backup → $backup_file ($(du -h "$backup_file" | cut -f1))"
    else
        warn "Postgres container not running — skipping DB backup."
        rm -f "$backup_file"
    fi

    # Trim to last $BACKUP_KEEP files
    local to_delete
    to_delete="$(ls -1t "$INSTALL_DIR"/backups/db-*.sql.gz 2>/dev/null | tail -n +$((BACKUP_KEEP+1)) || true)"
    if [ -n "$to_delete" ]; then
        echo "$to_delete" | xargs -r rm -f --
        info "Pruned backups older than the most recent $BACKUP_KEEP."
    fi
}

install_backup_cron() {
    # Nightly pg_dump at 02:17 — finishes well before the app's own 03:00 UTC
    # retention prune. Truncating '>' keeps the log to the latest run only.
    cat > "$BACKUP_CRON_FILE" <<EOF
SHELL=/bin/bash
PATH=/usr/sbin:/usr/bin:/sbin:/bin
17 2 * * * root $INSTALL_DIR/install.sh backup > $BACKUP_CRON_LOG 2>&1
EOF
    chmod 644 "$BACKUP_CRON_FILE"
    info "Nightly DB backup cron installed at $BACKUP_CRON_FILE (02:17, keeps last $BACKUP_KEEP)."
}

# ---------------------------- repo handling ----------------------------------

ensure_repo_present() {
    if [ -d "$INSTALL_DIR/.git" ]; then
        return
    fi
    if [ -d "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
        die "$INSTALL_DIR exists and is not a git checkout. Move it aside or run remove first."
    fi
    info "Cloning $REPO_URL → $INSTALL_DIR ..."
    apt-get install -y -qq git >/dev/null 2>&1 || true
    has_cmd git || die "git is required but not installed"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
    ok "Repository cloned."
}

# ---------------------------- env generation ---------------------------------

gen_env_value() {
    # Generate APP_SECRET_KEY (Fernet-format: 32 random bytes urlsafe-base64)
    # using Python if available (matches the format Fernet expects), else openssl.
    if has_cmd python3; then
        python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
    else
        # Fallback: openssl. Convert to urlsafe alphabet.
        openssl rand 32 | base64 | tr '+/' '-_' | tr -d '\n'
        echo
    fi
}

gen_pg_password() {
    openssl rand -base64 24 | tr -d '/+=\n'
}

generate_env() {
    local port="$1"
    local key pw
    key="$(gen_env_value)"
    pw="$(gen_pg_password)"
    install -m 0600 /dev/null "$INSTALL_DIR/.env"
    cat > "$INSTALL_DIR/.env" <<EOF
APP_SECRET_KEY=$key
POSTGRES_PASSWORD=$pw
HOST_PORT=$port
EOF
    chmod 600 "$INSTALL_DIR/.env"
}

port_in_use() {
    local port="$1"
    if has_cmd ss; then
        ss -ltn "( sport = :$port )" 2>/dev/null | grep -q LISTEN
    elif has_cmd lsof; then
        lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
    else
        return 1
    fi
}

prompt_for_port() {
    local current_port="${1:-$DEFAULT_PORT}"
    local port
    while :; do
        port="$(prompt_with_default "Host port to expose" "$current_port")"
        if [[ ! "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
            warn "Port must be a number between 1 and 65535."
            continue
        fi
        if [ "$port" != "$current_port" ] && port_in_use "$port"; then
            if ! prompt_yes_no "Port $port appears to be in use. Use it anyway?" "n"; then
                continue
            fi
        fi
        printf '%s' "$port"
        return
    done
}

# ---------------------------- health & info ----------------------------------

wait_for_health() {
    local port="$1" tries=30
    info "Waiting for /health on http://localhost:$port ..."
    while [ $tries -gt 0 ]; do
        if curl -fs -o /dev/null "http://localhost:$port/health"; then
            ok "Service is healthy."
            return 0
        fi
        sleep 2
        tries=$((tries - 1))
    done
    warn "Service did not return 200 within 60s. Run '$0 status' to investigate."
    return 1
}

extract_bootstrap_password() {
    # Captures the password line printed by app/auth.py:ensure_bootstrap_admin
    # via app/main.py:create_app's logger.warning(...).
    docker logs server-monitor-web 2>&1 \
        | awk '/BOOTSTRAP ADMIN CREATED/{found=1; next}
               found && /password:/{ sub(/^[^:]*: */,""); print; exit }'
}

primary_ip() {
    if has_cmd ip; then
        ip route get 1.1.1.1 2>/dev/null | awk '/src/ {for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1
    elif has_cmd hostname; then
        hostname -I 2>/dev/null | awk '{print $1}'
    else
        echo "<server-ip>"
    fi
}

print_access_box() {
    local port="$1" pw="$2"
    local ip; ip="$(primary_ip)"
    cat <<BOX
${C_GREEN}${C_BOLD}
==========================================
  Server Monitor is running.
==========================================${C_RESET}
  URL:       http://${ip:-<server-ip>}:$port
  Username:  admin
  Password:  ${pw:-(check: docker logs server-monitor-web | grep -A1 BOOTSTRAP)}

  Logs:      docker compose -f $INSTALL_DIR/docker-compose.yml logs -f
  Update:    sudo $INSTALL_DIR/install.sh update
  Remove:    sudo $INSTALL_DIR/install.sh remove
${C_DIM}
Save the password — it will not be shown again. Change it from
the UI after first login.${C_RESET}
BOX
}

# ---------------------------- actions ----------------------------------------

action_install() {
    require_root
    require_ubuntu
    install_docker_if_missing
    remove_legacy_sysctl
    ensure_repo_present
    install_backup_cron

    local port="$DEFAULT_PORT"
    local need_env=1
    if [ -f "$INSTALL_DIR/.env" ]; then
        # Read current port if present
        local existing_port
        existing_port="$(grep -E '^HOST_PORT=' "$INSTALL_DIR/.env" | tail -1 | cut -d= -f2-)"
        port="${existing_port:-$DEFAULT_PORT}"
        if prompt_yes_no "Existing .env detected. Keep it (preserves data and admin password)?" "y"; then
            need_env=0
        else
            cp "$INSTALL_DIR/.env" "$INSTALL_DIR/.env.bak.$(date +%s)"
            info "Backed up existing .env to .env.bak.<timestamp>"
        fi
    fi

    if [ "$need_env" -eq 1 ]; then
        port="$(prompt_for_port "$port")"
        info "Generating fresh secrets and .env ..."
        generate_env "$port"
        ok ".env created at $INSTALL_DIR/.env (chmod 600)"
    fi

    info "Building and starting containers (this may take a few minutes the first time)..."
    ( cd "$INSTALL_DIR" && docker compose up -d --build )

    if wait_for_health "$port"; then
        local pw=""
        if [ "$need_env" -eq 1 ]; then
            pw="$(extract_bootstrap_password)"
        fi
        print_access_box "$port" "$pw"
    else
        ( cd "$INSTALL_DIR" && docker compose ps )
        warn "Inspect logs with: docker compose -f $INSTALL_DIR/docker-compose.yml logs"
    fi
}

action_update() {
    require_root
    [ -f "$INSTALL_DIR/docker-compose.yml" ] || die "No install found at $INSTALL_DIR. Run install first."
    has_cmd docker && docker compose version >/dev/null 2>&1 || die "Docker is required."

    if [ "${SM_UPDATE_STAGE:-}" != "post" ]; then
        # Self-update hazard: 'git reset --hard' below replaces this very file
        # while bash is still reading it. Re-exec from a temp copy first (when
        # we're running from the repo file, not curl|bash), so execution is
        # immune to the replacement.
        if [ -z "${SM_UPDATE_STAGE:-}" ] && [ -f "${BASH_SOURCE[0]:-}" ]; then
            local self_copy
            self_copy="$(mktemp /tmp/server-monitor-install.XXXXXX)"
            cp "${BASH_SOURCE[0]}" "$self_copy"
            SM_UPDATE_STAGE=pre exec bash "$self_copy" update
        fi

        info "Backing up the database before update ..."
        do_db_backup

        info "Pulling latest code from origin/main ..."
        git -C "$INSTALL_DIR" fetch --prune origin
        local before; before="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
        git -C "$INSTALL_DIR" reset --hard origin/main
        local after; after="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
        if [ "$before" = "$after" ]; then
            info "Already on the latest commit ($(git -C "$INSTALL_DIR" rev-parse --short HEAD))."
        else
            ok "Updated $(git -C "$INSTALL_DIR" rev-parse --short "$before") → $(git -C "$INSTALL_DIR" rev-parse --short "$after")"
        fi

        # Hand the rest of the update to the freshly-pulled script so new
        # installer steps (sysctls, future migrations) apply in this same run.
        [ "${SM_UPDATE_STAGE:-}" = "pre" ] && rm -f -- "${BASH_SOURCE[0]}"
        SM_UPDATE_STAGE=post exec bash "$INSTALL_DIR/install.sh" update
    fi

    # ---- post stage: runs from the freshly-pulled script -------------------
    remove_legacy_sysctl
    install_backup_cron

    local port
    port="$(grep -E '^HOST_PORT=' "$INSTALL_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
    port="${port:-$DEFAULT_PORT}"

    info "Pulling base images (postgres, nginx) ..."
    ( cd "$INSTALL_DIR" && docker compose pull --quiet postgres nginx ) || true

    info "Rebuilding and restarting containers ..."
    ( cd "$INSTALL_DIR" && docker compose up -d --build )

    info "Pruning unused Docker images ..."
    docker image prune -f >/dev/null
    docker builder prune -f >/dev/null 2>&1 || true

    wait_for_health "$port" || true

    ok "Update complete."
    ( cd "$INSTALL_DIR" && docker compose ps )
}

action_backup() {
    require_root
    [ -f "$INSTALL_DIR/docker-compose.yml" ] || die "No install found at $INSTALL_DIR. Run install first."
    has_cmd docker && docker compose version >/dev/null 2>&1 || die "Docker is required."
    do_db_backup
}

action_status() {
    [ -f "$INSTALL_DIR/docker-compose.yml" ] || { warn "Not installed at $INSTALL_DIR"; return; }
    if ! has_cmd docker; then
        warn "Docker is not installed."
        return
    fi
    echo
    info "Containers:"
    ( cd "$INSTALL_DIR" && docker compose ps )
    echo
    local port; port="$(grep -E '^HOST_PORT=' "$INSTALL_DIR/.env" 2>/dev/null | cut -d= -f2- || true)"
    port="${port:-$DEFAULT_PORT}"
    if curl -fsS -o /dev/null "http://localhost:$port/health"; then
        ok "Health: 200 OK on http://localhost:$port/health"
    else
        warn "Health: not responding on http://localhost:$port/health"
    fi
    echo
    info "Recent logs (web):"
    docker logs --tail 15 server-monitor-web 2>&1 | sed 's/^/  /' || true
    echo
    info "Recent logs (scheduler):"
    docker logs --tail 15 server-monitor-scheduler 2>&1 | sed 's/^/  /' || true
}

action_remove() {
    require_root
    [ -d "$INSTALL_DIR" ] || { warn "Nothing to remove at $INSTALL_DIR"; return; }

    cat <<EOF

${C_RED}${C_BOLD}This will permanently delete:${C_RESET}
  • all running containers (web, scheduler, postgres, nginx)
  • the postgres data volume (entire monitoring database)
  • the on-disk backups under $INSTALL_DIR/backups/
  • optionally, the install directory $INSTALL_DIR

EOF

    local confirm
    if [ -t 0 ]; then
        read -rp "Type ${C_BOLD}DELETE${C_RESET} to confirm (anything else cancels): " confirm
    else
        confirm="${REMOVE_CONFIRM:-}"
    fi
    [ "$confirm" = "DELETE" ] || { info "Cancelled."; return; }

    if has_cmd docker && [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
        info "Stopping containers and removing volumes ..."
        ( cd "$INSTALL_DIR" && docker compose down -v --remove-orphans ) || true
    fi

    if prompt_yes_no "Remove install directory $INSTALL_DIR (including .env)?" "n"; then
        rm -rf "$INSTALL_DIR"
        ok "Removed $INSTALL_DIR"
    else
        info "Kept $INSTALL_DIR (re-run with 'install' to start over)."
    fi

    remove_legacy_sysctl
    rm -f "$BACKUP_CRON_FILE" "$BACKUP_CRON_LOG"

    if has_cmd docker && prompt_yes_no "Also uninstall Docker Engine itself?" "n"; then
        info "Removing Docker packages ..."
        apt-get purge -y -qq docker-ce docker-ce-cli containerd.io \
            docker-buildx-plugin docker-compose-plugin || true
        rm -rf /var/lib/docker /var/lib/containerd /etc/docker
        rm -f /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.gpg
        ok "Docker removed."
    fi

    ok "Done."
}

# ---------------------------- menu / dispatch --------------------------------

show_menu() {
    print_banner
    local state; state="$(detect_state)"
    local label
    case "$state" in
        not_installed) label="${C_DIM}not installed${C_RESET}" ;;
        stopped)       label="${C_YELLOW}installed, stopped${C_RESET}" ;;
        running)       label="${C_GREEN}running${C_RESET}" ;;
    esac
    printf '\n  Detected install: %s — %s\n\n' "$INSTALL_DIR" "$label"
    cat <<MENU
  1) Install   — first-time setup
  2) Update    — pull latest, rebuild, preserve data
  3) Status    — show containers and recent logs
  4) Remove    — delete containers/volumes (optional: directory)
  5) Quit

MENU
    local choice
    read -rp "Choose [1-5]: " choice
    case "$choice" in
        1) action_install ;;
        2) action_update ;;
        3) action_status ;;
        4) action_remove ;;
        5|q|Q) exit 0 ;;
        *) die "Invalid choice." ;;
    esac
}

usage() {
    cat <<USAGE
Usage: $0 [install|update|status|backup|remove]

With no argument, an interactive menu is shown.
Run as root (or via sudo) for install / update / remove.

Examples:
  curl -fsSL https://raw.githubusercontent.com/ruolez/server-monitor/main/install.sh | sudo bash
  curl -fsSL https://raw.githubusercontent.com/ruolez/server-monitor/main/install.sh | sudo bash -s -- install
  sudo $0 update
USAGE
}

main() {
    ACTION="${1:-}"
    case "$ACTION" in
        install) action_install ;;
        update)  action_update ;;
        status)  action_status ;;
        backup)  action_backup ;;
        remove)  action_remove ;;
        ""|menu) show_menu ;;
        -h|--help|help) usage ;;
        *) usage; exit 1 ;;
    esac
}

main "$@"
