#!/usr/bin/env bash
# post-write.sh — validates task files before dispatch picks them up
# Called by inotify after a file is written to tasks/pending/

set -euo pipefail

TASK_FILE="${1:-}"

if [[ -z "$TASK_FILE" || ! -f "$TASK_FILE" ]]; then
  echo "[post-write] No file provided or file not found: $TASK_FILE" >&2
  exit 1
fi

REQUIRED_FIELDS=(agent priority created)
REQUIRED_SECTIONS=("## Objective" "## Input" "## Output format" "## Success criteria")

errors=0

for field in "${REQUIRED_FIELDS[@]}"; do
  if ! grep -q "^${field}:" "$TASK_FILE"; then
    echo "[post-write] MISSING field: $field" >&2
    ((errors++))
  fi
done

for section in "${REQUIRED_SECTIONS[@]}"; do
  if ! grep -qF "$section" "$TASK_FILE"; then
    echo "[post-write] MISSING section: $section" >&2
    ((errors++))
  fi
done

VALID_AGENTS=(memory search execution)
agent_value=$(grep -m1 '^agent:' "$TASK_FILE" | awk '{print $2}')
if [[ ! " ${VALID_AGENTS[*]} " =~ " ${agent_value} " ]]; then
  echo "[post-write] INVALID agent value: '$agent_value' (must be: memory|search|execution)" >&2
  ((errors++))
fi

if [[ $errors -gt 0 ]]; then
  echo "[post-write] Task file invalid: $(basename "$TASK_FILE") — $errors error(s)" >&2
  exit 1
fi

echo "[post-write] Task file valid: $(basename "$TASK_FILE")"
