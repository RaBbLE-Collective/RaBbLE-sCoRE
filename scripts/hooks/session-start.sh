#!/usr/bin/env bash
# session-start.sh — orients sCoRE on startup
# Reads most recent context snapshot and pending/active task state

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

SUMMARIZE_MODE=false
[[ "${1:-}" == "--summarize" ]] && SUMMARIZE_MODE=true

echo ""
echo "=== RaBbLE-sCoRE Session Start ==="
echo "$(date -Iseconds)"
echo ""

# Most recent context snapshot
SNAPSHOT_DIR="$REPO_ROOT/memory/context-snapshots"
if [[ -d "$SNAPSHOT_DIR" ]] && compgen -G "$SNAPSHOT_DIR/snapshot-*.md" > /dev/null 2>&1; then
  latest=$(ls -t "$SNAPSHOT_DIR"/snapshot-*.md | head -1)
  echo "--- Last Snapshot: $(basename "$latest") ---"
  head -20 "$latest"
  echo ""
else
  echo "--- No prior context snapshots found ---"
  echo ""
fi

# Active tasks
ACTIVE_DIR="$REPO_ROOT/tasks/active"
active_count=$(find "$ACTIVE_DIR" -name "TASK-*.md" 2>/dev/null | wc -l)
echo "Active tasks: $active_count"
if [[ $active_count -gt 0 ]]; then
  find "$ACTIVE_DIR" -name "TASK-*.md" -exec basename {} \; | sort
fi
echo ""

# Pending tasks
PENDING_DIR="$REPO_ROOT/tasks/pending"
pending_count=$(find "$PENDING_DIR" -name "TASK-*.md" 2>/dev/null | wc -l)
echo "Pending tasks: $pending_count"
if [[ $pending_count -gt 0 ]]; then
  find "$PENDING_DIR" -name "TASK-*.md" -exec basename {} \; | sort
fi
echo ""

echo "=== End of State Report ==="
echo ""
