# Codex 向け Unitap 利用ガイド

Unitap を Unity 操作の共通入口にする。Unity の診断・コンパイル復旧・独自ツールに、UniCLI の汎用コマンドと C# 評価を組み合わせる。UniCLI は任意の外部依存であり、Unitap の配布物にバイナリやサーバーソースを同梱しているわけではない。導入は [unicli-bridge.md](unicli-bridge.md) を参照。

## 最初に機能を調べる

以下の `unitap` はプロジェクトのラッパー（リポジトリ直下の `scripts/unitap`）を指す。導入規約は「プロジェクトへの導入・更新・開発」節。

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

UniCLI コマンドの引数調査には `describe unicli:NAME` を使う。`exec NAME --help` は UniCLI がテキストを返すため、JSON ブリッジでは不正応答扱いになる。未導入の UniCLI と不正な引数は、Editor の lease・排他ロックを待つ前に失敗を返す。`unicli_not_found` が出たら同じ呼び出しを繰り返さず、利用プロジェクトの導入方針と `commands --live` の利用可能な経路を確認する。

## 成功・失敗を判断する

- JSON の最上位 `ok` とプロセス終了コードを確認する。UniCLI の生データは `result.response` に残る。
- テストは `result.response.data` の集計も確認する。`total > 0`、`failed == 0` が必要。0件はテスト成功の証拠にならない。想定の assembly と件数を照合する。
- ビルドは返却結果とローカル成果物、画面変更は保存 capture の目視まで確認する。
- pending / jobId は開始の証拠であり完了ではない。該当ツールの polling スキーマに従う。プロジェクト固有の `run_automate_test` / `run_playmode_test` ラッパーは、カタログに存在するときだけ利用する。
- タイムアウト後も Unity 側処理が継続する場合がある。状態・ログを調べ、同じ変更を自動再送しない。別バックエンドによる同一操作の再送もしない。
- メニュー等で開始した Play Mode の QA スクリプトは `wait_result` で待つ。開始前の Unix timestamp を `--after` に渡し、前回の完了・失敗・進捗ファイルを除外する。`--progress` と `--start-timeout` を併用すると、新しい進捗が出ない QA を `not_started` で打ち切れる。メニュー実行の `ok` を確認してから待機し、スクリプト側は開始時と各段階で進捗ファイルに追記する。マーカーのパスは絶対パスを推奨する。
- `wait_result` は `result.reason` に `done` / `failure_file` / `console_error` / `play_mode_exited` / `not_playing` / `not_started` / `stalled` / `timeout` を返す。成功は `done` のみ。失敗時は `ok: false`、終了コード1、`error.code: wait_result_<reason>` となり、詳細は `result` に残る。同じ実行の完了・失敗ファイルが両方あれば失敗を優先する。`--after` を省略した既存の呼び出しではファイルの新旧を区別しない。
- Unity は入れ子のコルーチン（`yield return OtherEnumerator()`）内の例外をログに出すだけで、親は再開も失敗もしない。QA スクリプトの外側で try/catch する場合は、入れ子の IEnumerator も自前で MoveNext して例外を失敗ファイルに書く。
- `read_console` のログはドメインリロード（Play の開始・停止、コンパイル）をまたいで保持される。`compile_check` は実行時にクリアする。`--since` は UTC の ISO8601（`Z` 付き）で指定する。
- UniCLI eval がコンパイルに失敗したらエラーを確認し、必要に応じて `compile_check` 後に状態を再確認する。eval のコード変更と Editor の復旧を区別する。

`exec` / `eval` は既存の `Library/Unitap/.editor-op.lock` を保持し、同じ正規化プロジェクトをロック・cwd・`UNICLI_PROJECT` に使う。汎用コマンドの副作用は自動推測せず、全 exec / eval を排他にする。`unicli check/status/commands` と機能探索はロックを取得しない。独自ツールの排他は既存のツール別ポリシーに従い、カタログの `lockPolicy` に表示する。

QA 開始前に時刻を記録し、その後にメニュー実行と成功確認を行う。例えば Python の `time.time()` で得た値を `<started>` に渡す。

```sh
unitap --json wait_result --done <dir>/done.txt --fail <dir>/failure.txt --progress <dir>/progress.txt --after <started> --start-timeout 15 --stall 30 --require-playing --timeout 300
```

`--after` 指定時のコンソール監視もその時刻から始まる。開始確認や停滞の時間は、Unitap が待機を始めた時点・進捗の変更を検知した時点から単調時計で計測する。エラー検出と Editor への状態照会は従来どおり行い、開始待ち中でも検出した失敗は先に返す。

排他は Unitap を経由する処理にのみ有効。非同期処理が開始応答を返した後までロックが継続する保証はない。履歴は `Library/Unitap/execution-history.jsonl`（8MB で `.1.jsonl` に 1 世代だけ退避。`UNITAP_HISTORY_MAX_BYTES` で変更、0 で無制限）。各行にセッション ID・lock/lease 待ち時間を残し、終了コードが非0なら成功として記録しない。`wait_result` はマーカーのパス・待機条件・終了理由・経過秒・開始検知の有無を記録する。exec / eval は backend・operation・コマンド名・timeout のみを要求情報に記録し、C# 本文や追加引数を記録しない。機能探索は履歴を増やさない。

## 複数 Editor・複数セッションで使う

1 台の Mac で複数プロジェクトの Editor を同時に動かしてよい。Unitap は対象を `--project` の Editor だけに限定する。

- `launch` は他プロジェクトの Editor を終了しない。終了するのは同じプロジェクトの Editor だけ。別プロジェクトも止めたい場合だけ `--kill-all` を明示する（他セッションの作業を壊すため通常は使わない）。
- `launch --restart` は heartbeat が止まった・凍結した Editor、または ProjectVersion.txt と違う Unity で開いている Editor だけを再起動する。コンパイルが進まない等、応答はあるが壊れている Editor の復旧には `--restart --force-restart` を使う。応答している Editor は `restartSkipped: true` を返して残す。
- Editor の終了は `quit`（このプロジェクトの Editor だけ）。`execute_menu File/Quit` は拒否される。プロセス一覧を取れなかった時は `process_list_failed`、一覧に無いのに新しい heartbeat の PID が生きている時は `unity_quit_unconfirmed` で失敗し、「起動していない」と誤って成功しない。
- `focus` / `compile_check` の前面化は対象プロジェクトの PID だけを扱い、見つからなければ別の Unity を前面化せず失敗する。
- `editors` でマシン上の全 Editor（プロジェクト・PID・メモリ・起動からの分数 `uptimeMinutes`・`batchMode`・heartbeat・排他ロック・lease 保持者・最後の操作 `lastCommand` と `idleMinutes`・置き忘れ候補 `staleCandidate`）を確認できる。
- **作業が終わったら Editor を閉じる。** 報告の前に、自分が起動・使用した Editor（clone は必ず）を `quit` し、`editors` で消えたのを確かめる。ユーザーの返事待ちで止まる時も閉じる（開いたままの Editor が何日も残り、メモリと CPU を使い続けるため）。
- 自分が使っていない Editor は、`staleCandidate: true` のものだけ終わったセッションの置き忘れとして閉じてよい。候補になるのは、起動後に unitap で操作され、最後の操作から6時間（`UNITAP_STALE_EDITOR_MINUTES`）過ぎ、lease・排他ロック・コンパイル中・batchmode のどれでもない Editor だけ。起動後に unitap の操作が無い Editor（手で開いたもの）は候補にしない。候補でない理由は `notStaleBecause` に出る。
- QA メニュー（パスに `/QA/` を含む `execute_menu`）は、前回その同じメニューを流した時から Assets・Packages・ProjectSettings が何も変わっていなければ `qa_repeat_unchanged` で拒否される。同じQAをコードを変えずに繰り返さず、前回の結果ファイルの失敗内容と撮影を見て直してから流す。前回の実行が Editor の不調などで結果を出さずに終わった時だけ `--allow-repeat` を付ける。記録は `Library/Unitap/qa-runs.json`。対象のパターンは `UNITAP_QA_MENU_PATTERN`、無効化は `UNITAP_QA_REPEAT_GUARD=0`。
- 起動・コンパイル中の既知のダイアログ（Enter Safe Mode? は Ignore、Corrupted Library は Rebuild、外部変更されたシーンは Reload、UI Toolkit Particles は Cancel）は、Editor コマンドの間に動く監視が、開いているすべての Editor について自動で押す（macOS のアクセシビリティ権限が必要）。AppleEvent は5秒上限。監視は巡回ごとに印を更新し、20秒以上更新がない監視（印のない旧版も含む）は次のコマンドで終了・再起動する。起動・heartbeat復旧・`compile_check` の待機中も5秒間隔で同期確認する。押したものはJSONの `dismissedDialogs`、Safe Modeを押した時は `hint` に記録する。Safe Mode が出たのはコンパイルエラーがある合図なので、続けて `compile_check` / `read_console` でエラーを直す。
- 手動の `unitap --json dismiss_dialogs` は全Editorを1回走査し、`dismissedDialogs`・`remainingDialogs`（文言・ボタン・PID）を返す。未知のボタンは押さない。既知のボタンが見つかったのに押せなかった場合は `editor_dialog_blocking` で待機を打ち切る。走査自体は15秒上限で、失敗時は `editor_dialog_scan_failed`・`scanError` を返す（自動待機では `scanError` を添えて接続確認を続ける）。
- Editor の heartbeat が `None`・stale のまま起動待ちが進まない時は、同じ待ちを繰り返さない。`editors` で対象 PID を確かめ、ダイアログが残っていないかを見る（例：System Events で `name of every button of every window of (first process whose unix id is <PID>)`。同名プロセスは保存した参照だと最初の Unity に解決されるので、PID で毎回指定する）。既知のダイアログなら上の監視と同じボタンを押して続ける。未知のダイアログ・押してはいけないもの（保存の確認など）は押さず、画面の内容を添えてユーザーに知らせる。
- `--project` を明示した時は、そのプロジェクトの heartbeat だけを使う。Library ごと複製したプロジェクトに残った元 Editor の heartbeat（`projectPath` 不一致）は無視する。

同じプロジェクトを複数セッションが使う場合は、次のどちらかを選ぶ。

**順番に使う（lease）**: 一連の Editor 操作（play → 操作 → capture → stop 等）の前に占有を宣言する。lease 中は他セッションの排他コマンド（play / stop / compile_check / launch / tool_exec の入力操作など）が待機し、`--no-wait-lock` では `editor_leased` で失敗する。状態参照（status / read_console / heartbeat / ui_pointer status）は妨げない。所有者は `UNITAP_SESSION`、未設定なら Claude Code / Codex のセッション ID で識別する。

```sh
unitap --json lease acquire --ttl 1800 --note "PlayMode QA: shop"
# ... 排他コマンドを実行するたびに期限が延長される ...
unitap --json lease release
unitap --json lease status
```

競合する lease と排他ロックの待ち時間は、それぞれ既定30秒。長く待つ予定がある場合だけ、サブコマンドの前に `--lock-timeout 1800` などを明示する。lease 自体の有効期限（`--ttl`、既定1800秒）とは別の値。取得できなかった場合も履歴に待機時間を残す。lease ファイルの更新用ロックを取得できなければ `lease_file_busy` で終了し、lease を変更しない。

終了し忘れた lease は期限で自動失効する。所有セッションが既に存在しない場合だけ `lease release --force` または各コマンドの `--ignore-lease` を使う。

**並行に使う（clone）**: セッションごとに作業用プロジェクトを作り、別 Editor で開く。git リポジトリでは worktree、Library は APFS clone（容量をほぼ使わず数秒）で複製するため、初回起動の再インポートをほぼ省ける。複製先は元（worktree ではリポジトリ）の兄弟 `<name>--<label>` に置き、`file:../../pkg` の相対参照を保つ。

clone の並行起動も Mac 全体の Editor 上限に従う。`launch` は物理メモリ GiB / 24 の切り捨て（最低 1、`UNITAP_MAX_EDITORS` の正整数で上書き）以上、または `memory_pressure` の free が 15% 未満なら、既定で最大 1800 秒待つ（理由は stderr に 1 分ごと）。`--wait-slot-timeout <秒>` で待機時間を指定し、`--no-wait-slot` なら直ちに `editor_slot_unavailable` を返す。`--no-wait` は起動確認だけを省略し、空き待ちは省略しない。AssetImportWorker は数えず、再起動する同一プロジェクトの Editor は枠の計算から除く。メモリ取得失敗時も起動を待つ。swap 使用量は診断用に表示し、使用量だけでは起動を止めない。`editors` と `doctor` の `editorSlots` で上限・現在数・メモリを確認できる。待たされても他の Editor を勝手に閉じない。

```sh
unitap --json clone create --name qa-shop                 # HEAD の worktree（detached）
unitap --json clone create --name fix-a --branch fix/a    # ブランチを作る
unitap --json clone create --name wip --include-uncommitted  # 未コミット変更も複製
unitap --json --project <clone> launch
unitap --json clone list
unitap --json --project <clone> quit
unitap --json clone remove --dest <clone>                 # 未コミット変更・未参照コミットがあれば拒否
```

gitignore 済みで Unity が必要とするファイル（SDK・symlink 等）も複製し、元リポジトリ内を絶対パスで指す symlink は複製内の同じ場所へ張り替える（元のファイルを複製の Editor が書き換えないため）。元の外を指す symlink は共有のまま残り、`warnings` に件数が出る。複製は ProjectSettings（companyName / productName）が元と同じため、Play Mode の PlayerPrefs と `Application.persistentDataPath` を元の Editor と共有する。セーブデータを書き換える Play 検証を並行させる場合は、プロジェクト側の隔離手段（検証用データ保存先など）を使う。clone の実機ビルド・アプリ ID の扱いは利用プロジェクトの規約に従う。compile_check は Play Mode を止めてから実行するが、別セッションが `play` した Play Mode は止めずに `play_mode_in_use` で失敗する（止めてよい場合だけ `--stop-foreign-play`）。

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

## プロジェクトへの導入・更新・開発（1 つの正本を submodule で使う）

unitap の正本は `git@github.com:kubota-yyy/unitap.git` だけ。各プロジェクトはこれを **git submodule として固定したコミット**で使い、ファイルのコピー（vendor）や別プロジェクトの unitap の参照をしない。

| 項目 | 規約 |
|---|---|
| 置き場所 | Unity プロジェクトを含む git リポジトリの直下 `unitap/`（submodule） |
| Unity からの参照 | `Packages/manifest.json` の `"com.nilone.unitap": "file:<Packages から unitap への相対パス>"`。例: リポジトリ直下が Unity プロジェクトなら `file:../unitap`、`<repo>/game/` なら `file:../../unitap` |
| 実行入口 | リポジトリ直下の `scripts/unitap`（`--project` を固定したラッパー）。プロジェクト拡張（`unitap_ext`）はラッパーが読み込む |
| 一致確認 | `scripts/unitap --json doctor`。CLI と Unity が読むコピーが違うと各コマンドは `unitap_copy_mismatch` で止まる |

導入（新しいプロジェクト）:

```sh
git -C <repo> submodule add git@github.com:kubota-yyy/unitap.git unitap
git -C <repo> config submodule.recurse true          # pull/checkout で submodule も追従（各 clone で 1 回）
# Packages/manifest.json に "com.nilone.unitap": "file:../unitap"（相対パスは配置に合わせる）
mkdir -p <repo>/scripts && cat > <repo>/scripts/unitap <<'SH'
#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec python3 "$ROOT/unitap/cli/unitap.py" --project "$ROOT/<unity-project-dir>" "$@"
SH
chmod +x <repo>/scripts/unitap && <repo>/scripts/unitap --json doctor
```

別マシン・新しい clone では `git submodule update --init unitap`。`doctor` が `matchesPinned: false` を返したら、親が固定したコミットと実際のチェックアウトがずれている。

更新（新しい unitap を取り込む）: `git -C <repo> submodule update --remote unitap` → `scripts/unitap --json compile_check` と主要な確認 → 親リポジトリで `unitap`（ポインタ）と必要なら `packages-lock.json` を commit。各プロジェクトは自分の都合の良い時に更新する。

unitap 自体を変更する: そのプロジェクトの `unitap/` の中でブランチを切って編集・テスト（`PYTHONPATH=cli python3 -m unittest discover -s cli/tests -q`）・commit し、unitap を push してから親のポインタを commit する。C# を変えると、その unitap を読む開いている Editor がすべて再コンパイルされるため、作業中の Editor に影響させたくない場合は `clone create` した複製で検証する。他プロジェクトへコピーして直さない。

UPM パッケージ内の AGENTS.md が、利用プロジェクトで作業するエージェントに読み込まれるとは限らない。利用プロジェクトの AGENTS.md / CLAUDE.md に次を置く（パッケージ導入時に自動上書きしない）。

> Unity 操作はこのリポジトリの `scripts/unitap` だけを使う（unitap は `unitap/` の submodule。他プロジェクトや別コピーの unitap を使わない・コピーしない）。まず `scripts/unitap --json commands --live --search <用途>`、`describe <id>` で操作と引数を確認する。複数 Editor・複数セッションの規約は `unitap/docs/agent-guide.md`。

### uGUI の実座標による反応検証

`describe tool:ui_pointer` で仕様を確認してから、Play Mode の Game View 座標（左下原点）を渡す。
`inspect` は EventSystem の Raycast 上位候補を返す。`down` → `up` は別フレームの押下・解放を送り、両方の最上位クリック先が一致した場合だけクリックする。`click` は同一フレームの押下・解放。Button.onClick の直接呼び出しではないため、手前の暗幕、Raycast 無効、表示領域外などを検証できる。ドラッグ・スクロール・マルチタッチは対象外。

処理は Game View の画面サイズが有効なゲームフレームへ予約される。`_mcp_status: pending` の場合は同じツールの `{"action":"status"}` を取得する。Editor を前面にするか対象アプリの `runInBackground` を有効にし、ポーズを解除する。検証時の一時設定は終了時に戻す。
