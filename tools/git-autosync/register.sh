#!/bin/zsh
# このマシンをサーバーの autosync-clients に登録する。
# 登録後、誰かが push するたびにこのマシンが自動同期される。

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
REPO_DIR=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)
PROJECT_SLUG=$(basename "$REPO_DIR")
SERVER="harry@harry.local"
CLIENTS_FILE="/Users/harry/git-server/${PROJECT_SLUG}.git/hooks/autosync-clients"

LOCAL_USER=$(whoami)

# ホスト名取得（mDNS で DHCP に依存しない）
LOCAL_HOST=$(scutil --get LocalHostName 2>/dev/null || hostname -s 2>/dev/null || true)

if [[ -z "$LOCAL_HOST" ]]; then
    echo "エラー: ホスト名を取得できませんでした"
    echo "scutil --set LocalHostName <名前> で設定してください"
    exit 1
fi

LOCAL_HOST="${LOCAL_HOST}.local"
ENTRY="${LOCAL_USER}@${LOCAL_HOST} ${REPO_DIR}"

echo "登録情報: $ENTRY"
echo ""

# 重複登録チェック
if ssh "$SERVER" "grep -qF '${LOCAL_USER}@' '${CLIENTS_FILE}' 2>/dev/null"; then
    # 同じユーザーで別IPの古いエントリがあれば更新
    OLD_ENTRY=$(ssh "$SERVER" "grep -F '${LOCAL_USER}@' '${CLIENTS_FILE}' 2>/dev/null || true")
    if [[ "$OLD_ENTRY" = "$ENTRY" ]]; then
        echo "既に登録されています（変更なし）"
    else
        echo "既存エントリを更新: $OLD_ENTRY -> $ENTRY"
        ssh "$SERVER" "
            tmp=\$(mktemp)
            grep -vF '${LOCAL_USER}@' '${CLIENTS_FILE}' > \"\$tmp\" || true
            echo '${ENTRY}' >> \"\$tmp\"
            mv \"\$tmp\" '${CLIENTS_FILE}'
        "
        echo "更新しました"
    fi
else
    ssh "$SERVER" "echo '${ENTRY}' >> '${CLIENTS_FILE}'"
    echo "登録しました"
fi

# ─── サーバー → このマシンへの SSH アクセス設定 ───────────────────────────

echo ""
echo "サーバーの公開鍵をこのマシンの authorized_keys に追加中..."

AUTHORIZED_KEYS="$HOME/.ssh/authorized_keys"
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
touch "$AUTHORIZED_KEYS"
chmod 600 "$AUTHORIZED_KEYS"

SERVER_PUBKEY=$(ssh "$SERVER" "cat ~/.ssh/id_ed25519.pub 2>/dev/null || cat ~/.ssh/id_rsa.pub 2>/dev/null || echo ''" 2>/dev/null || echo "")

if [[ -z "$SERVER_PUBKEY" ]]; then
    echo "警告: サーバーの公開鍵を取得できませんでした"
    echo "手動で以下を実行してください:"
    echo "  ssh ${SERVER} cat ~/.ssh/id_ed25519.pub >> ~/.ssh/authorized_keys"
elif grep -qF "$SERVER_PUBKEY" "$AUTHORIZED_KEYS" 2>/dev/null; then
    echo "サーバーの公開鍵は既に登録されています"
else
    echo "$SERVER_PUBKEY" >> "$AUTHORIZED_KEYS"
    echo "追加しました: ~/.ssh/authorized_keys"
fi

# ─── macOS Remote Login の確認 ───────────────────────────────────────────

echo ""
REMOTE_LOGIN=$(sudo systemsetup -getremotelogin 2>/dev/null || echo "")
if echo "$REMOTE_LOGIN" | grep -q "On"; then
    echo "Remote Login (SSH): 有効"
else
    echo "注意: Remote Login (SSH) が無効になっています"
    echo "有効化するには:"
    echo "  sudo systemsetup -setremotelogin on"
    echo "  または: システム設定 > 一般 > 共有 > リモートログイン"
fi

# ─── 接続テスト ──────────────────────────────────────────────────────────

echo ""
echo "サーバーからこのマシンへの接続テスト..."
if ssh "$SERVER" "ssh -o ConnectTimeout=5 -o BatchMode=yes -o StrictHostKeyChecking=no '${LOCAL_USER}@${LOCAL_HOST}' 'echo OK'" 2>/dev/null | grep -q "OK"; then
    echo "接続テスト成功"
    echo ""
    echo "設定完了！次の push から自動同期が有効になります"
else
    echo "接続テスト失敗"
    echo "以下を確認してください:"
    echo "  1. Remote Login が有効になっているか"
    echo "  2. ファイアウォールが SSH (port 22) をブロックしていないか"
    echo "  3. サーバーの公開鍵が authorized_keys に入っているか"
fi
