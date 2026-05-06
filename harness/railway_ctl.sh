#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# harness/railway_ctl.sh — RaBbLE Railway management
#
# Usage:
#   ./harness/railway_ctl.sh <command> [service] [options]
#
# Commands:
#   auto         Full setup walkthrough — skips steps already done
#   setup        Install Railway CLI and log in
#   init         Create a new Railway project linked to a service
#   link         Link to an existing Railway project
#   deploy       Deploy a service [--force]
#   vars         Set env vars interactively (pre-fills from .env)
#   vars-push    Non-interactive: push .secrets + env.defaults to Railway
#   vars-show    Print current Railway environment variables
#   status       Health-check the deployed service
#   logs         Tail live service logs
#   open         Open the Railway dashboard
#   domain       Print the public Railway URL
#   install-hook Install a git post-commit hook for auto-deploy
#   github       Instructions for GitHub-based continuous deployment
#
# Deployment modes:
#   Direct upload   railway up from services/<svc>/ — no GitHub required.
#                   Change detection skips unchanged services.
#
#   GitHub-based    Connect repo in Railway dashboard.
#                   Set Root Directory = services/<svc>
#                   Railway auto-deploys on every push to the configured branch.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HARNESS_DIR/.." && pwd)"

COMMAND="${1:-help}"
SERVICE="${2:-intelligence}"
SERVICE_DIR="$ROOT/services/$SERVICE"
SERVICE_RELPATH="services/$SERVICE"

HEALTH_PATH="/health"
DEPLOY_STAMP="$ROOT/.railway_last_deploy_${SERVICE}"
GIT_ROOT="$(git -C "$ROOT" rev-parse --show-toplevel 2>/dev/null || true)"

# ── Helpers ───────────────────────────────────────────────────────────────────

info()   { echo "  $*"; }
ok()     { echo "✓ $*"; }
err()    { echo "✗ $*" >&2; }
header() { echo; echo "── $* ──"; }

_validate_service() {
  if [ ! -d "$SERVICE_DIR" ]; then
    err "Service '$SERVICE' not found at $SERVICE_DIR"
    echo "  Available services:" >&2
    ls "$ROOT/services/" 2>/dev/null | sed 's/^/    /' >&2
    exit 1
  fi
}

require_railway() {
  if ! command -v railway &>/dev/null; then
    err "Railway CLI not found. Run: ./harness/railway_ctl.sh setup"
    exit 1
  fi
}

_service_is_linked() {
  local output
  output="$(cd "$SERVICE_DIR" && railway variables 2>&1 || true)"
  ! echo "$output" | grep -qi "no service linked\|link a service\|no services found"
}

_ensure_service() {
  if _service_is_linked; then return 0; fi
  local out
  out="$(cd "$SERVICE_DIR" && railway service 2>&1)" || true
  if echo "$out" | grep -qi "no services found"; then
    err "No services in this Railway project yet — run a first deploy."
    info "  cd $SERVICE_DIR && railway up"
    return 1
  fi
  if ! _service_is_linked; then
    err "Still no service linked. Try: cd $SERVICE_DIR && railway service"
    return 1
  fi
}

require_link() {
  require_railway
  if ! (cd "$SERVICE_DIR" && railway status) &>/dev/null 2>&1; then
    err "No Railway project linked for '$SERVICE'."
    info "Run: ./harness/railway_ctl.sh init $SERVICE"
    info " or: ./harness/railway_ctl.sh link $SERVICE"
    exit 1
  fi
}

get_domain() {
  (cd "$SERVICE_DIR" && railway domain 2>/dev/null) \
    | grep -Eo 'https?://[^ ]+' | head -1 || true
}

_load_dotenv() {
  local env_file="$SERVICE_DIR/.env"
  [ -f "$env_file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    [[ "$line" != *=* ]] && continue
    local key="${line%%=*}" val="${line#*=}"
    val="${val#\"}" ; val="${val%\"}"
    val="${val#\'}" ; val="${val%\'}"
    export "$key"="$val"
  done < "$env_file"
}

# ── Commands ──────────────────────────────────────────────────────────────────

cmd_setup() {
  header "Railway CLI setup"
  if command -v railway &>/dev/null; then
    ok "CLI already installed: $(railway --version)"
  else
    info "Installing Railway CLI..."
    if command -v npm &>/dev/null; then
      npm install -g @railway/cli
    else
      curl -fsSL https://railway.app/install.sh | sh
    fi
    ok "Installed: $(railway --version)"
  fi
  header "Log in to Railway"
  railway login
  ok "Logged in as $(railway whoami 2>/dev/null || echo '(unknown)')"
}

cmd_init() {
  _validate_service
  require_railway
  header "Initialize Railway project for: $SERVICE"
  info "This creates a NEW project. For an existing project, run: link $SERVICE"
  echo
  cd "$SERVICE_DIR"
  railway init
  railway service
  ok "Project and service linked"
  info "Next: ./harness/railway_ctl.sh vars $SERVICE"
}

cmd_link() {
  _validate_service
  require_railway
  header "Link to existing Railway project for: $SERVICE"
  cd "$SERVICE_DIR"
  railway link
  railway service
  ok "Linked"
}

_service_changed() {
  [ ! -f "$DEPLOY_STAMP" ] && return 0
  local last_sha
  last_sha="$(cat "$DEPLOY_STAMP" 2>/dev/null || true)"
  if [ -z "$last_sha" ] || [ -z "$GIT_ROOT" ]; then
    find "$SERVICE_DIR" -newer "$DEPLOY_STAMP" \
      ! -path '*/__pycache__/*' ! -name '*.pyc' -print -quit | grep -q .
    return $?
  fi
  git -C "$GIT_ROOT" cat-file -e "${last_sha}^{commit}" 2>/dev/null || return 0
  git -C "$GIT_ROOT" diff --quiet "$last_sha" HEAD -- "$SERVICE_RELPATH"
  local rc=$?; [[ $rc -ne 0 ]]
}

_record_deploy() {
  if [ -n "$GIT_ROOT" ]; then
    git -C "$GIT_ROOT" rev-parse HEAD > "$DEPLOY_STAMP" 2>/dev/null || true
  else
    touch "$DEPLOY_STAMP"
  fi
}

_show_diff() {
  local last_sha
  last_sha="$(cat "$DEPLOY_STAMP" 2>/dev/null || true)"
  if [ -n "$GIT_ROOT" ] && [ -n "$last_sha" ]; then
    echo
    info "Changes since last deploy ($last_sha):"
    git -C "$GIT_ROOT" diff --stat "$last_sha" HEAD -- "$SERVICE_RELPATH" 2>/dev/null || true
    echo
  fi
}

cmd_deploy() {
  _validate_service
  require_link

  local force=0
  for arg in "$@"; do [[ "$arg" == "--force" ]] && force=1; done

  header "Deploy $SERVICE to Railway"

  if [ $force -eq 0 ]; then
    if ! _service_changed; then
      ok "No changes in $SERVICE_RELPATH since last deploy. Skipping."
      info "Use --force: ./harness/railway_ctl.sh deploy $SERVICE --force"
      return 0
    fi
    _show_diff
  else
    info "--force: deploying regardless of changes."
  fi

  info "Uploading $SERVICE_DIR ..."
  cd "$SERVICE_DIR"
  _ensure_service
  railway up --detach
  _record_deploy
  ok "Deploy triggered"
  echo
  info "Waiting 15s for service to start..."
  sleep 15
  cmd_status
}

cmd_vars() {
  _validate_service
  require_link
  header "Set Railway variables for: $SERVICE (interactive)"

  _load_dotenv
  info "Press Enter to keep an existing Railway value."
  echo
  cd "$SERVICE_DIR"
  _ensure_service

  _prompt_var() {
    local key="$1" hint="$2" default_cmd="${3:-}"
    local railway_val dotenv_val suggested val
    railway_val="$(railway variables get "$key" 2>/dev/null || true)"
    dotenv_val="${!key:-}"
    info "$key  — $hint"
    if [ -n "$railway_val" ]; then
      read -rp "  Value [already set in Railway, Enter to keep]: " val
    elif [ -n "$dotenv_val" ]; then
      read -rp "  Value [$dotenv_val]: " val
      val="${val:-$dotenv_val}"
    elif [ -n "$default_cmd" ]; then
      suggested="$(eval "$default_cmd")"
      read -rp "  Value [$suggested]: " val
      val="${val:-$suggested}"
    else
      read -rp "  Value: " val
    fi
    if [ -n "$val" ]; then
      railway variables set "${key}=${val}"
      ok "Set $key"
    else
      info "Skipped $key"
    fi
    echo
  }

  _prompt_var "JWT_SECRET"          "32-byte hex string" \
    "python3 -c \"import secrets; print(secrets.token_hex(32))\""
  _prompt_var "GROQ_API_KEY"        "console.groq.com/keys (fast LLM)"
  _prompt_var "OPENROUTER_API_KEY"  "openrouter.ai/keys (fallback + strong LLM)"
  _prompt_var "RABBLE_ADMIN_KEY"    "admin bypass key — optional, keep secret"
  _prompt_var "DEMO_MODE"           "true = unauthenticated guest access"
  _prompt_var "FRONTEND_URL"        "allowed CORS origins, comma-separated or *"
  _prompt_var "AUDIT_LOG_PATH"      "default: audit.jsonl"

  echo
  ok "Done. Review: ./harness/railway_ctl.sh vars-show $SERVICE"
}

cmd_vars_push() {
  _validate_service
  require_link
  header "Push secrets (non-interactive) for: $SERVICE"

  local secrets_file="$ROOT/.secrets"
  local defaults_file="$SERVICE_DIR/env.defaults"

  if [ ! -f "$secrets_file" ]; then
    err ".secrets not found. Copy secrets.example → .secrets and fill in values."
    exit 1
  fi

  declare -A VARS
  _load_kv() {
    local f="$1"; [ -f "$f" ] || return 0
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
  _load_kv "$secrets_file"

  cd "$SERVICE_DIR"
  _ensure_service
  for k in "${!VARS[@]}"; do
    railway variables set "${k}=${VARS[$k]}" && ok "Set $k" || err "Failed: $k"
  done
  ok "Done — Railway will redeploy automatically if vars changed."
}

cmd_vars_show() {
  _validate_service
  require_link
  header "Current Railway variables for: $SERVICE"
  cd "$SERVICE_DIR"
  railway variables
}

cmd_status() {
  _validate_service
  require_link
  header "Health check: $SERVICE"
  local domain
  domain="$(get_domain)"
  if [ -z "$domain" ]; then
    err "Could not get Railway domain."
    info "Try: ./harness/railway_ctl.sh domain $SERVICE"
    exit 1
  fi
  local url="${domain}${HEALTH_PATH}"
  info "GET $url"
  local response http_code body
  response="$(curl -sf --max-time 10 -w '\n%{http_code}' "$url" || true)"
  http_code="$(echo "$response" | tail -1)"
  body="$(echo "$response" | head -1)"
  if [ "$http_code" = "200" ]; then
    ok "Health check passed: $body"
  else
    err "Health check failed (HTTP $http_code): $body"
    exit 1
  fi
}

cmd_logs() {
  _validate_service
  require_link
  header "Live logs: $SERVICE (Ctrl+C to stop)"
  cd "$SERVICE_DIR"
  railway logs --tail
}

cmd_open() {
  _validate_service
  require_link
  cd "$SERVICE_DIR"
  railway open
}

cmd_domain() {
  _validate_service
  require_link
  local domain
  domain="$(get_domain)"
  if [ -n "$domain" ]; then
    ok "Public URL : $domain"
    info "Health     : ${domain}${HEALTH_PATH}"
    info "API docs   : ${domain}/docs"
  else
    err "No domain assigned yet. Deploy first or assign one in the Railway dashboard."
    exit 1
  fi
}

cmd_install_hook() {
  if [ -z "$GIT_ROOT" ]; then
    err "Not in a git repo — cannot install hook."
    exit 1
  fi
  local hook_file="$GIT_ROOT/.git/hooks/post-commit"
  local marker="# rabble-railway-deploy-$SERVICE"
  if grep -q "$marker" "$hook_file" 2>/dev/null; then
    ok "Hook already installed: $hook_file"
    return 0
  fi
  header "Install post-commit hook for: $SERVICE"
  cat >> "$hook_file" <<HOOK

$marker
if ! git diff --quiet HEAD~1 HEAD -- "$SERVICE_RELPATH" 2>/dev/null; then
  echo "[railway_ctl] $SERVICE changed — deploying to Railway..."
  "$HARNESS_DIR/railway_ctl.sh" deploy "$SERVICE"
fi
HOOK
  chmod +x "$hook_file"
  ok "Hook installed: $hook_file"
  info "Commits touching $SERVICE_RELPATH will trigger a Railway deploy."
}

cmd_github() {
  header "GitHub auto-deploy for: $SERVICE"
  cat <<EOF

  Connect your repo so Railway redeploys on every push.

  Steps:
  ──────
  1. Railway dashboard → your project → $SERVICE service
  2. Settings → Source → Connect Repo → select this repo

  3. In Service Settings → Source:
       Root Directory : $SERVICE_RELPATH
       Branch         : development   (staging)
                        main          (production)

  4. Railway now watches $SERVICE_RELPATH/** and redeploys on push.
     Other paths (harness/, other services) are ignored.

  5. Manage secrets in Railway dashboard → Variables
     or run:  ./harness/railway_ctl.sh vars-push $SERVICE

  Direct upload (./harness/railway_ctl.sh deploy $SERVICE) works
  alongside GitHub mode for urgent fixes.

EOF
}

cmd_auto() {
  _validate_service
  header "RaBbLE · Railway Full Setup: $SERVICE"
  info "Skips any step already completed."
  echo

  header "Step 1/6: Railway CLI"
  if command -v railway &>/dev/null; then
    ok "CLI already installed: $(railway --version)"
  else
    info "Installing Railway CLI..."
    command -v npm &>/dev/null \
      && npm install -g @railway/cli \
      || curl -fsSL https://railway.app/install.sh | sh
    ok "Installed: $(railway --version)"
  fi

  header "Step 2/6: Login"
  if railway whoami &>/dev/null 2>&1; then
    ok "Already logged in as $(railway whoami)"
  else
    info "Opening browser for Railway login..."
    railway login
    ok "Logged in"
  fi

  header "Step 3/6: Project link"
  if (cd "$SERVICE_DIR" && railway status) &>/dev/null 2>&1; then
    ok "Project already linked"
  else
    echo "  [1] Link to an existing Railway project"
    echo "  [2] Create a new Railway project"
    read -rp "  Choice [1]: " choice
    cd "$SERVICE_DIR"
    [[ "${choice:-1}" == "2" ]] && railway init || railway link
    ok "Project linked"
  fi

  header "Step 4/6: Initial deploy (creates Railway service)"
  cd "$SERVICE_DIR"
  if _service_is_linked; then
    ok "Service already exists — skipping"
  else
    info "Deploying to create the Railway service..."
    railway up --detach
    info "Waiting 20s for service to register..."
    sleep 20
    railway service 2>/dev/null || true
    ok "Service created"
  fi

  header "Step 5/6: Push secrets"
  cmd_vars_push

  header "Step 6/6: Final deploy"
  cmd_deploy --force

  echo
  ok "Setup complete."
  cmd_domain
}

# ── Entry point ────────────────────────────────────────────────────────────────

case "$COMMAND" in
  auto)         cmd_auto ;;
  setup)        cmd_setup ;;
  init)         cmd_init ;;
  link)         cmd_link ;;
  deploy)       cmd_deploy "$@" ;;
  vars)         cmd_vars ;;
  vars-push)    cmd_vars_push ;;
  vars-show)    cmd_vars_show ;;
  status)       cmd_status ;;
  logs)         cmd_logs ;;
  open)         cmd_open ;;
  domain)       cmd_domain ;;
  install-hook) cmd_install_hook ;;
  github)       cmd_github ;;
  help|--help|-h|"")
    sed -n '/^# Usage/,/^[^#]/p' "$0" | grep '^#' | sed 's/^# \?//'
    ;;
  *)
    err "Unknown command: $COMMAND"
    echo "  Run: $0 help"
    exit 1
    ;;
esac
