# Codex 向け Unitap 利用ガイド

Unitap を Unity 操作の共通入口にする。Unity の診断・コンパイル復旧・独自ツールに、UniCLI の汎用コマンドと C# 評価を組み合わせる。UniCLI は任意の外部依存であり、Unitap の配布物にバイナリやサーバーソースを同梱しているわけではない。導入は [unicli-bridge.md](unicli-bridge.md) を参照。

## 最初に機能を調べる

以下の `unitap` はプロジェクトのラッパーを指す。ラッパーがなければ `python3 /path/to/unitap/cli/unitap.py --project /absolute/UnityProject` に置き換える。dogma ではリポジトリルートの `scripts/unitap` を使う。

```sh
# Unity 未起動・UniCLI 未導入でも CLI 自体の一覧が得られる
unitap --json commands

# 実 Editor の独自ツールと UniCLI のコマンドも一括探索
unitap --json commands --live
unitap --json commands --live --search prefab
unitap --json commands --live --backend unicli --search TestRunner

# 一覧の id で引数・型・既定値・実行経路を確認
unitap --json describe cli:compile_check
unitap --json describe tool:find_assets
unitap --json describe unicli:GameObject.Find
```

一覧は短い概要、`describe` は完全なスキーマを返す。CLI のスキーマは実際の argparse 定義（`unitap_ext` の追加コマンドを含む）、ツールは実 Editor の属性、UniCLI は実サーバーの `commands` 応答を正本とする。固定のコマンド数を前提にしない。

名前は `cli:` / `tool:` / `unicli:` で区別する。無修飾の `describe status` は `cli:status` と解釈する。`invocation` はグローバルオプションの後ろに渡す引数配列のひな形であり、必須引数は `describe` の情報で補う。`tool_exec` の `{}` も必要な JSON に置き換える。

探索は Editor を前面化せず、排他待機や再試行を行わない。既定の制限はバックエンドごと5秒（UniCLI の子プロセス終了には追加5秒の猶予）。`commands --live` は一部に接続できなくても取得済みの一覧を返し、`partial: true` と `sources.<backend>.error` に理由を示す。未照会は `available: null`。一部取得成功を全バックエンド利用可能と解釈しない。`describe` の対象が利用できない場合は終了コード1になる。カタログをキャッシュしないため、古い情報を現行の機能として返さない。

## 操作の選び方

| 目的 | 最初に使う入口 |
|---|---|
| Editor の状態・接続障害 | `status` / `diagnose` / `heartbeat` |
| C# 変更の反映・コンパイル確認 | `compile_check --timeout 60000` |
| シーン・Prefab・アセット・一般的な Unity API | `commands --live --search ...` → `describe unicli:...` → `exec ...` |
| ゲーム固有の検証や操作 | `describe tool:...` → `tool_exec --tool ... --params ...` |
| Game View / Editor Window の確認 | `capture` / `capture_editor`、または `capture_sceneview` ツール |
| 既存コマンドで表せない一時的な C# 調査 | `eval` |
| UniCLI 導入・接続・バージョン確認 | `unicli check` / `unicli status` |

```sh
unitap --json exec PlayMode.Status
unitap --json exec GameObject.Find '{"name":"Main Camera"}'
unitap --json tool_exec --tool find_assets --params '{"type":"Prefab","limit":5}'
unitap --json eval 'return UnityEngine.Application.unityVersion;'
unitap --json exec --timeout-ms 180000 TestRunner.RunEditMode --assemblies RabbitPunch.Tests
```

`exec` / `eval` は `unicli exec` / `unicli eval` と同じ実装を使う。グローバルの `--project` / `--json` / `--no-wait-lock` はサブコマンドの前、`--timeout-ms` は実行対象名や C# の前に置く。長い形は `unicli --timeout-ms 180000 exec ...`。子 CLI に別の `--project` / `--timeout` を渡せない。

UniCLI コマンドの引数調査には `describe unicli:NAME` を使う。`exec NAME --help` は UniCLI がテキストを返すため、JSON ブリッジでは不正応答扱いになる。

## 成功・失敗を判断する

- JSON の最上位 `ok` とプロセス終了コードを確認する。UniCLI の生データは `result.response` に残る。
- テストは `result.response.data` の集計も確認する。`total > 0`、`failed == 0` が必要。0件はテスト成功の証拠にならない。想定の assembly と件数を照合する。
- ビルドは返却結果とローカル成果物、画面変更は保存 capture の目視まで確認する。
- pending / jobId は開始の証拠であり完了ではない。該当ツールの polling スキーマに従う。プロジェクト固有の `run_automate_test` / `run_playmode_test` ラッパーは、カタログに存在するときだけ利用する。
- タイムアウト後も Unity 側処理が継続する場合がある。状態・ログを調べ、同じ変更を自動再送しない。別バックエンドによる同一操作の再送もしない。
- メニュー等で開始した Play Mode の QA スクリプトは、完了ファイルを固定時間 polling せず `wait_result` で待つ。例: `unitap --json wait_result --done <dir>/done.txt --fail <dir>/failure.txt --progress <dir>/progress.txt --stall 30 --require-playing --timeout 300`。新しいコンソールエラー、Play Mode の終了、進捗ファイルの停滞、失敗ファイルのいずれかで即座に終了し、`result.reason` に理由（`done` / `failure_file` / `console_error` / `play_mode_exited` / `not_playing` / `stalled` / `timeout`）を返す。スクリプト側は各段階で進捗ファイルに追記する。
- Unity は入れ子のコルーチン（`yield return OtherEnumerator()`）内の例外をログに出すだけで、親は再開も失敗もしない。QA スクリプトの外側で try/catch する場合は、入れ子の IEnumerator も自前で MoveNext して例外を失敗ファイルに書く。
- `read_console` のログはドメインリロード（Play の開始・停止、コンパイル）をまたいで保持される。`compile_check` は実行時にクリアする。`--since` は UTC の ISO8601（`Z` 付き）で指定する。
- UniCLI eval がコンパイルに失敗したらエラーを確認し、必要に応じて `compile_check` 後に状態を再確認する。eval のコード変更と Editor の復旧を区別する。

`exec` / `eval` は既存の `Library/Unitap/.editor-op.lock` を保持し、同じ正規化プロジェクトをロック・cwd・`UNICLI_PROJECT` に使う。汎用コマンドの副作用は自動推測せず、全 exec / eval を排他にする。`unicli check/status/commands` と機能探索はロックを取得しない。独自ツールの排他は既存のツール別ポリシーに従い、カタログの `lockPolicy` に表示する。

排他は Unitap を経由する処理にのみ有効。非同期処理が開始応答を返した後までロックが継続する保証はない。履歴は `Library/Unitap/execution-history.jsonl`（8MB で `.1.jsonl` に 1 世代だけ退避。`UNITAP_HISTORY_MAX_BYTES` で変更、0 で無制限）。各行にセッション ID・lock/lease 待ち時間を残す。exec / eval は backend・operation・コマンド名・timeout のみを要求情報に記録し、C# 本文や追加引数を記録しない。機能探索は履歴を増やさない。

## 複数 Editor・複数セッションで使う

1 台の Mac で複数プロジェクトの Editor を同時に動かしてよい。Unitap は対象を `--project` の Editor だけに限定する。

- `launch` は他プロジェクトの Editor を終了しない。終了するのは同じプロジェクトの Editor だけ。別プロジェクトも止めたい場合だけ `--kill-all` を明示する（他セッションの作業を壊すため通常は使わない）。
- `launch --restart` は heartbeat が止まった・凍結した Editor だけを再起動する。応答している Editor は `restartSkipped: true` を返して残す。健全でも再起動が必要な場合は `--restart --force-restart`。
- Editor の終了は `quit`（このプロジェクトの Editor だけ）。`execute_menu File/Quit` は拒否される。
- `focus` / `compile_check` の前面化は対象プロジェクトの PID だけを扱い、見つからなければ別の Unity を前面化せず失敗する。
- `editors` でマシン上の全 Editor（プロジェクト・PID・メモリ・heartbeat・排他ロック・lease 保持者）を確認できる。
- `--project` を明示した時は、そのプロジェクトの heartbeat だけを使う。Library ごと複製したプロジェクトに残った元 Editor の heartbeat（`projectPath` 不一致）は無視する。

同じプロジェクトを複数セッションが使う場合は、次のどちらかを選ぶ。

**順番に使う（lease）**: 一連の Editor 操作（play → 操作 → capture → stop 等）の前に占有を宣言する。lease 中は他セッションの排他コマンド（play / stop / compile_check / launch / tool_exec の入力操作など）が待機し、`--no-wait-lock` では `editor_leased` で失敗する。状態参照（status / read_console / heartbeat / ui_pointer status）は妨げない。所有者は `UNITAP_SESSION`、未設定なら Claude Code / Codex のセッション ID で識別する。

```sh
unitap --json lease acquire --ttl 1800 --note "PlayMode QA: shop"
# ... 排他コマンドを実行するたびに期限が延長される ...
unitap --json lease release
unitap --json lease status
```

終了し忘れた lease は期限で自動失効する。所有セッションが既に存在しない場合だけ `lease release --force` または各コマンドの `--ignore-lease` を使う。

**並行に使う（clone）**: セッションごとに作業用プロジェクトを作り、別 Editor で開く。git リポジトリでは worktree、Library は APFS clone（容量をほぼ使わず数秒）で複製するため、初回起動の再インポートをほぼ省ける。複製先は元（worktree ではリポジトリ）の兄弟 `<name>--<label>` に置き、`file:../../pkg` の相対参照を保つ。

```sh
unitap --json clone create --name qa-shop                 # HEAD の worktree（detached）
unitap --json clone create --name fix-a --branch fix/a    # ブランチを作る
unitap --json clone create --name wip --include-uncommitted  # 未コミット変更も複製
unitap --json --project <clone> launch
unitap --json clone list
unitap --json --project <clone> quit
unitap --json clone remove --dest <clone>                 # 未コミット変更・未参照コミットがあれば拒否
```

複製は ProjectSettings（companyName / productName）が元と同じため、Play Mode の PlayerPrefs と `Application.persistentDataPath` を元の Editor と共有する。セーブデータを書き換える Play 検証を並行させる場合は、プロジェクト側の隔離手段（検証用データ保存先など）を使う。clone の実機ビルド・アプリ ID の扱いは利用プロジェクトの規約に従う。compile_check は Play Mode を止めてから実行するが、別セッションが `play` した Play Mode は止めずに `play_mode_in_use` で失敗する（止めてよい場合だけ `--stop-foreign-play`）。

## 独自ツールにも説明を付ける

```csharp
[McpForUnityTool("my_tool", Description = "Explain the concrete operation")]
[Unitap.UnitapToolParameter("assetPath", "string", "Asset path under Assets", Required = true)]
[Unitap.UnitapToolParameter("limit", "int", "Maximum returned items", DefaultValue = "20")]
public static class MyTool
{
    public static object HandleCommand(JObject parameters) { /* ... */ }
}
```

クラスへの `UnitapToolParameter` は JObject 引数を文書化するための属性であり、入力の検証や既定値の設定はハンドラー側の責任。既存のフィールド・プロパティ用 `ToolParameterAttribute` も利用できる。`tool_list` / `list_custom_tools` / `tool_exec` は同じレジストリを参照する。重複名の実行は `ambiguous_tool` で失敗し、候補クラスを返す。プロジェクトの独自ツールが属性を持たない場合は引数一覧が空になるため、そのツールのソースやプロジェクト文書を確認する。

## 利用プロジェクトから発見できるようにする

UPM パッケージ内の AGENTS.md が、利用プロジェクトルートで作業する Codex に必ず読み込まれるとは限らない。利用プロジェクトの AGENTS.md に、既存の Unitap ラッパーと次の案内を置く。

> Unity 操作は `scripts/unitap` を使う。まず `scripts/unitap --json commands --live --search <用途>`、次に `scripts/unitap --json describe <id>` で利用可能な操作と引数を確認する。UniCLI も `scripts/unitap exec` / `eval` 経由で使う。

ラッパーのパスは実プロジェクトに合わせる。パッケージ導入時に利用者の AGENTS.md を自動上書きしない。

### uGUI の実座標による反応検証

`describe tool:ui_pointer` で仕様を確認してから、Play Mode の Game View 座標（左下原点）を渡す。
`inspect` は EventSystem の Raycast 上位候補を返す。`down` → `up` は別フレームの押下・解放を送り、両方の最上位クリック先が一致した場合だけクリックする。`click` は同一フレームの押下・解放。Button.onClick の直接呼び出しではないため、手前の暗幕、Raycast 無効、表示領域外などを検証できる。ドラッグ・スクロール・マルチタッチは対象外。

処理は Game View の画面サイズが有効なゲームフレームへ予約される。`_mcp_status: pending` の場合は同じツールの `{"action":"status"}` を取得する。Editor を前面にするか対象アプリの `runInBackground` を有効にし、ポーズを解除する。検証時の一時設定は終了時に戻す。
