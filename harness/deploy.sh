#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# harness/deploy.sh — RaBbLE deployment TUI
#
# Interactive walkthrough for Railway cloud deployment.
# Wraps railway_ctl.sh with a navigable menu.
#
# Usage: ./harness/deploy.sh [service]
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HARNESS_DIR/.." && pwd)"
CTL="$HARNESS_DIR/railway_ctl.sh"

SERVICE="${1:-intelligence}"

# ── Colours + drawing ──────────────────────────────────────────────────────

GREEN='\033[0;32m' ; CYAN='\033[0;36m' ; YELLOW='\033[0;33m'
RED='\033[0;31m'   ; DIM='\033[2m'     ; BOLD='\033[1m'
RESET='\033[0m'

ok()     { echo -e "  ${GREEN}✓${RESET}  $*"; }
info()   { echo -e "  ${CYAN}·${RESET}  $*"; }
warn()   { echo -e "  ${YELLOW}!${RESET}  $*"; }
err()    { echo -e "  ${RED}✗${RESET}  $*" >&2; }
pause()  { echo; read -rp "  Press Enter to continue..." _; }
banner() { clear; _draw_banner; }

_draw_banner() {
  echo
  echo -e "${CYAN}  ╔══════════════════════════════════════════════════════╗${RESET}"
  echo -e "${CYAN}  ║${RESET}  ${BOLD}R a B b L E${RESET}  ·  Intelligence API                  ${CYAN}║${RESET}"
  echo -e "${CYAN}  ║${RESET}  ${DIM}Born from entropy. Deployed to the cloud.${RESET}         ${CYAN}║${RESET}"
  echo -e "${CYAN}  ╚══════════════════════════════════════════════════════╝${RESET}"
  echo
  _preflight_summary
  echo
}

_preflight_summary() {
  local rail_ok=0 secrets_ok=0 linked_ok=0

  command -v railway &>/dev/null && rail_ok=1
  [ -f "$ROOT/.secrets" ] && secrets_ok=1
  (cd "$ROOT/services/$SERVICE" && railway status &>/dev/null 2>&1) && linked_ok=1

  local rail_icon secrets_icon linked_icon
  rail_icon="$(    [ $rail_ok     -eq 1 ] && echo "${GREEN}✓${RESET}" || echo "${RED}✗${RESET}")"
  secrets_icon="$( [ $secrets_ok  -eq 1 ] && echo "${GREEN}✓${RESET}" || echo "${YELLOW}!${RESET}")"
  linked_icon="$(  [ $linked_ok   -eq 1 ] && echo "${GREEN}✓${RESET}" || echo "${YELLOW}·${RESET}")"

  echo -e "  ${rail_icon}  Railway CLI installed   ${secrets_icon}  .secrets file   ${linked_icon}  project linked"
}

# ── Preflight checks ────────────────────────────────────────────────────────

_check_secrets() {
  if [ ! -f "$ROOT/.secrets" ]; then
    warn ".secrets not found — copy the template first:"
    echo
    echo "    cp $ROOT/secrets.example $ROOT/.secrets"
    echo "    # Fill in GROQ_API_KEY, OPENROUTER_API_KEY, JWT_SECRET"
    echo
    pause
    return 1
  fi
  return 0
}

_check_railway() {
  if ! command -v railway &>/dev/null; then
    info "Railway CLI not installed. Installing..."
    echo
    if command -v npm &>/dev/null; then
      npm install -g @railway/cli
    else
      curl -fsSL https://railway.app/install.sh | sh
    fi
    ok "Railway CLI installed: $(railway --version)"
    pause
  fi
}

# ── Menu actions ────────────────────────────────────────────────────────────

action_full_setup() {
  banner
  echo -e "  ${BOLD}Full Setup Walkthrough${RESET}"
  echo    "  Installs CLI → login → link project → push secrets → deploy"
  echo
  _check_railway
  "$CTL" auto "$SERVICE"
  pause
}

action_push_secrets() {
  banner
  echo -e "  ${BOLD}Push Secrets to Railway${RESET}"
  echo
  _check_secrets || return

  info "Reading .secrets and env.defaults ..."
  local svc_dir="$ROOT/services/$SERVICE"
  local defaults_file="$svc_dir/env.defaults"

  declare -A VARS
  _load_kv() {
    local f="$1"
    [ -f "$f" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
      [[ "$line" =~ ^[[:space:]]*# ]] && continue
      [[ "$line" =~ ^[[:space:]]*$ ]] && continue
      [[ "$line" != *=* ]] && continue
      local k="${line%%=*}" v="${line#*=}"
      v="${v#\"}"; v="${v%\"}"; v="${v#\'}"; v="${v%\'}"; k="${k// /}"
      [ -n "$k" ] && VARS["$k"]="$v"
    done < "$f"
  }
  _load_kv "$defaults_file"
  _load_kv "$ROOT/.secrets"

  echo    "  Variables to push:"
  for k in "${!VARS[@]}"; do
    local display
    if [[ "$k" =~ (SECRET|KEY|TOKEN|PASSWORD) ]]; then
      display="${VARS[$k]:0:4}****"
    else
      display="${VARS[$k]}"
    fi
    printf "    %-28s %s\n" "$k" "$display"
  done
  echo
  read -rp "  Push these to Railway service '$SERVICE'? [y/N] " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || { info "Cancelled."; pause; return; }

  cd "$svc_dir"
  for k in "${!VARS[@]}"; do
    railway variables set "${k}=${VARS[$k]}" && ok "Set $k" || warn "Failed to set $k"
  done
  echo
  ok "Secrets pushed. Railway will redeploy automatically."
  pause
}

action_deploy() {
  banner
  echo -e "  ${BOLD}Deploy Service to Railway${RESET}"
  echo    "  Service: $SERVICE"
  echo
  read -rp "  Force deploy (even if no changes)? [y/N] " force
  if [[ "$force" =~ ^[Yy]$ ]]; then
    "$CTL" deploy --force "$SERVICE"
  else
    "$CTL" deploy "$SERVICE"
  fi
  pause
}

action_health() {
  banner
  echo -e "  ${BOLD}Deployment Health Check${RESET}"
  echo
  "$CTL" status "$SERVICE"
  echo
  local domain
  domain="$("$CTL" domain "$SERVICE" 2>/dev/null | grep -Eo 'https?://[^ ]+' | head -1 || true)"
  if [ -n "$domain" ]; then
    echo
    info "Remote URL : $domain"
    info "API docs   : $domain/docs"
  fi
  pause
}

action_logs() {
  banner
  echo -e "  ${BOLD}Live Deployment Logs${RESET}"
  echo    "  Ctrl+C to stop"
  echo
  "$CTL" logs "$SERVICE"
}

action_open() {
  "$CTL" open "$SERVICE"
}

action_domain() {
  banner
  echo -e "  ${BOLD}Deployment URL${RESET}"
  echo
  "$CTL" domain "$SERVICE"
  pause
}

action_github() {
  banner
  echo -e "  ${BOLD}GitHub Auto-Deploy Setup${RESET}"
  echo
  echo    "  Connect your repo so Railway redeploys on every push."
  echo
  echo    "  Steps:"
  echo    "  1. Railway dashboard → your project → your service"
  echo    "  2. Settings → Source → Connect Repo → choose this repo"
  echo    "  3. Root Directory: services/$SERVICE"
  echo    "  4. Branch: development  (or main for production)"
  echo
  echo    "  That's it — Railway watches services/$SERVICE/** and"
  echo    "  triggers a redeploy on every matching push."
  echo
  echo    "  You can still use ./harness/deploy.sh for manual deploys"
  echo    "  alongside GitHub-based auto-deploy."
  pause
}

action_gen_env() {
  banner
  echo -e "  ${BOLD}Generate Local .env File${RESET}"
  echo    "  Service: $SERVICE"
  echo
  _check_secrets || return
  "$HARNESS_DIR/env_gen.sh" "$SERVICE"
  echo
  ok "Local .env is ready. Run the server with:"
  echo "    ./harness/local.sh $SERVICE"
  pause
}

action_local() {
  echo
  info "Starting local server — switching to local.sh ..."
  echo
  exec "$HARNESS_DIR/local.sh" "$SERVICE"
}

# ── Main menu loop ──────────────────────────────────────────────────────────

main_menu() {
  while true; do
    banner
    echo    "  Service: $SERVICE"
    echo
    echo    "  Cloud deployment"
    echo -e "  ${CYAN}[1]${RESET}  Full setup walkthrough  (CLI → login → link → secrets → deploy)"
    echo -e "  ${CYAN}[2]${RESET}  Push .secrets to Railway"
    echo -e "  ${CYAN}[3]${RESET}  Deploy now"
    echo -e "  ${CYAN}[4]${RESET}  Health check"
    echo -e "  ${CYAN}[5]${RESET}  Live logs"
    echo -e "  ${CYAN}[6]${RESET}  Show deployment URL"
    echo -e "  ${CYAN}[7]${RESET}  Open Railway dashboard"
    echo -e "  ${CYAN}[8]${RESET}  Set up GitHub auto-deploy"
    echo
    echo    "  Local development"
    echo -e "  ${CYAN}[9]${RESET}  Generate local .env from .secrets"
    echo -e "  ${CYAN}[l]${RESET}  Start local server"
    echo
    echo -e "  ${DIM}[0]  Exit${RESET}"
    echo
    read -rp "  → " choice

    case "$choice" in
      1) action_full_setup ;;
      2) action_push_secrets ;;
      3) action_deploy ;;
      4) action_health ;;
      5) action_logs ;;
      6) action_domain ;;
      7) action_open ;;
      8) action_github ;;
      9) action_gen_env ;;
      l|L) action_local ;;
      0|q|Q) echo; echo -e "  ${DIM}Goodbye.${RESET}"; echo; exit 0 ;;
      *) warn "Unknown option '$choice'" ; sleep 0.8 ;;
    esac
  done
}

main_menu
