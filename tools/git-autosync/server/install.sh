#!/bin/zsh
# サーバーに post-receive フックをインストールする

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
REPO_DIR=$(git -C "$SCRIPT_DIR/../.." rev-parse --show-toplevel)
PROJECT_SLUG=$(basename "$REPO_DIR")
SERVER="harry@harry.local"
REMOTE_HOOKS="/Users/harry/git-server/${PROJECT_SLUG}.git/hooks"

echo "サーバー ($SERVER) に post-receive フックをインストール中..."
echo "  プロジェクト: $PROJECT_SLUG"

scp "$SCRIPT_DIR/post-receive" "${SERVER}:${REMOTE_HOOKS}/post-receive"
ssh "$SERVER" "chmod +x '${REMOTE_HOOKS}/post-receive'"

# autosync-clients ファイルをまだ作成していなければ作る
ssh "$SERVER" "touch '${REMOTE_HOOKS}/autosync-clients'"

echo "完了"
echo "  フック: ${SERVER}:${REMOTE_HOOKS}/post-receive"
echo "  クライアントリスト: ${SERVER}:${REMOTE_HOOKS}/autosync-clients"
echo ""
echo "次のステップ: 各マシンで以下を実行して自分を登録する"
echo "  bash tools/git-autosync/register.sh"
