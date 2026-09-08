#!/bin/zsh
# このマシンをサーバーの autosync-clients から削除する

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
REPO_DIR=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)
PROJECT_SLUG=$(basename "$REPO_DIR")
SERVER="harry@harry.local"
CLIENTS_FILE="/Users/harry/git-server/${PROJECT_SLUG}.git/hooks/autosync-clients"
LOCAL_USER=$(whoami)

echo "登録解除: ${LOCAL_USER}@* をサーバーから削除中..."

ssh "$SERVER" "
    tmp=\$(mktemp)
    grep -vF '${LOCAL_USER}@' '${CLIENTS_FILE}' > \"\$tmp\" || true
    mv \"\$tmp\" '${CLIENTS_FILE}'
"

echo "完了"
echo ""
echo "現在の登録クライアント一覧:"
ssh "$SERVER" "cat '${CLIENTS_FILE}'"
