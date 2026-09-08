#!/bin/zsh
# Git自動同期スクリプト
# サーバーの post-receive フックから呼ばれる。リモートにpushがあれば自動でpull --rebaseし、
# コンフリクト時はClaude Codeが解決する。

SCRIPT_DIR="${0:A:h}"
REPO_DIR=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)
PROJECT_SLUG=$(basename "$REPO_DIR")
LOCK_FILE="/tmp/${PROJECT_SLUG}-git-autosync.lock"
LOG_FILE="/tmp/${PROJECT_SLUG}-git-autosync.log"
MAX_LOG_LINES=500
# claude コマンドを PATH から探す
CLAUDE_BIN=$(command -v claude 2>/dev/null || true)
# post-receive から渡されるブランチ（省略時は空＝全ブランチ対象）
FILTER_BRANCH="${1:-}"

# ─── ユーティリティ ────────────────────────────────────────────

log() {
    local ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$ts] $1" >> "$LOG_FILE"
    # ログが膨らみすぎないよう末尾N行に切り詰める
    if [[ $(wc -l < "$LOG_FILE") -gt $MAX_LOG_LINES ]]; then
        tail -n $MAX_LOG_LINES "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"
    fi
}

notify() {
    local title="$1"
    local body="$2"
    osascript -e "display notification \"$body\" with title \"$title\"" 2>/dev/null || true
}

cleanup() {
    rm -f "$LOCK_FILE"
}

ensure_claude_bin() {
    if [[ -n "$CLAUDE_BIN" ]]; then
        return 0
    fi
    log "ERROR: claude コマンドが見つからない"
    notify "Git AutoSync [$PROJECT_SLUG]" "claude コマンドが見つかりません"
    exit 1
}

# ─── 多重起動防止 ─────────────────────────────────────────────

if [[ -e "$LOCK_FILE" ]]; then
    local_pid=$(cat "$LOCK_FILE" 2>/dev/null)
    if [[ -n "$local_pid" ]] && kill -0 "$local_pid" 2>/dev/null; then
        log "SKIP: 別インスタンスが実行中 (PID $local_pid)"
        exit 0
    fi
    rm -f "$LOCK_FILE"
fi

echo $$ > "$LOCK_FILE"
trap cleanup EXIT

# ─── メイン処理 ───────────────────────────────────────────────

cd "$REPO_DIR" || { log "ERROR: cd $REPO_DIR 失敗"; exit 1; }

BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
[[ -z "$BRANCH" ]] && { log "ERROR: ブランチ取得失敗"; exit 1; }

# post-receive から特定ブランチが指定されていれば、一致しない場合はスキップ
if [[ -n "$FILTER_BRANCH" && "$BRANCH" != "$FILTER_BRANCH" ]]; then
    exit 0
fi

# 未コミット変更があればスキップ（作業中を邪魔しない）
if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
    log "SKIP: 未コミット変更あり (branch: $BRANCH)"
    exit 0
fi

# フェッチ
if ! git fetch origin 2>> "$LOG_FILE"; then
    log "ERROR: git fetch 失敗"
    exit 1
fi

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "")

if [[ -z "$REMOTE" ]]; then
    log "SKIP: origin/$BRANCH が存在しない"
    exit 0
fi

if [[ "$LOCAL" = "$REMOTE" ]]; then
    exit 0  # 最新、何もしない
fi

MERGE_BASE=$(git merge-base HEAD "origin/$BRANCH" 2>/dev/null || echo "")

if [[ "$LOCAL" = "$MERGE_BASE" ]]; then
    # ─── ローカルがリモートより遅れている ──────────────────
    log "リモートより遅れています。pull --rebase を実行 (branch: $BRANCH)..."

    if git pull --rebase 2>> "$LOG_FILE"; then
        MSG=$(git log --oneline -1)
        log "SUCCESS: $MSG"
        notify "Git AutoSync [$PROJECT_SLUG]" "同期完了: $MSG"
    else
        # コンフリクト発生 -> Claude Codeに解決させる
        CONFLICTED=$(git diff --name-only --diff-filter=U 2>/dev/null | tr '\n' ' ')
        log "CONFLICT: コンフリクト発生: $CONFLICTED"
        notify "Git AutoSync [$PROJECT_SLUG]" "コンフリクト検出、Claude Code で解決中..."
        ensure_claude_bin

        "$CLAUDE_BIN" -p \
            "git rebase でコンフリクトが発生しました。以下のファイルのコンフリクトマーカーを解決し、git add してから git rebase --continue を実行してください。リポジトリ: $REPO_DIR, コンフリクトファイル: $CONFLICTED" \
            --dangerously-skip-permissions \
            --allowedTools "Bash,Edit,Read" \
            2>> "$LOG_FILE"
        CLAUDE_EXIT=$?

        if [[ $CLAUDE_EXIT -eq 0 ]] && git diff --quiet && git diff --cached --quiet; then
            log "SUCCESS: Claude Code がコンフリクトを解決"
            notify "Git AutoSync [$PROJECT_SLUG]" "Claude Code がコンフリクトを解決しました"
        else
            log "ERROR: コンフリクト解決失敗 (claude exit: $CLAUDE_EXIT)、rebase を中断"
            notify "Git AutoSync [$PROJECT_SLUG]" "コンフリクト解決失敗 - 手動対応が必要です"
            git rebase --abort 2>/dev/null || true
        fi
    fi

elif [[ "$MERGE_BASE" = "$REMOTE" ]]; then
    # ─── ローカルがリモートより進んでいる ──────────────────
    # push 待ち。このスクリプトは pull のみ担当するので何もしない
    log "SKIP: ローカルがリモートより進んでいます (branch: $BRANCH)"
    exit 0

else
    # ─── 両方に新コミットあり（diverged） ──────────────────
    log "DIVERGED: ブランチが分岐しています (branch: $BRANCH)。Claude Code を起動..."
    notify "Git AutoSync [$PROJECT_SLUG]" "ブランチ分岐検出、Claude Code で解決中..."
    ensure_claude_bin

    "$CLAUDE_BIN" -p \
        "git ブランチが diverge しています（ローカルとリモートの両方に新しいコミットがあります）。git pull --rebase でリベースして整理してください。リポジトリ: $REPO_DIR, ブランチ: $BRANCH" \
        --dangerously-skip-permissions \
        --allowedTools "Bash,Edit,Read" \
        2>> "$LOG_FILE"
    CLAUDE_EXIT=$?

    if [[ $CLAUDE_EXIT -eq 0 ]] && git diff --quiet && git diff --cached --quiet; then
        log "SUCCESS: Claude Code が diverge を解決"
        notify "Git AutoSync [$PROJECT_SLUG]" "Claude Code が diverge を解決しました"
    else
        log "ERROR: diverge 解決失敗 (claude exit: $CLAUDE_EXIT)"
        notify "Git AutoSync [$PROJECT_SLUG]" "diverge 解決失敗 - 手動対応が必要です"
    fi
fi
