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
- UniCLI eval がコンパイルに失敗したらエラーを確認し、必要に応じて `compile_check` 後に状態を再確認する。eval のコード変更と Editor の復旧を区別する。

`exec` / `eval` は既存の `Library/Unitap/.editor-op.lock` を保持し、同じ正規化プロジェクトをロック・cwd・`UNICLI_PROJECT` に使う。汎用コマンドの副作用は自動推測せず、全 exec / eval を排他にする。`unicli check/status/commands` と機能探索はロックを取得しない。独自ツールの排他は既存のツール別ポリシーに従い、カタログの `lockPolicy` に表示する。

排他は Unitap を経由する処理にのみ有効。非同期処理が開始応答を返した後までロックが継続する保証はない。履歴は `Library/Unitap/execution-history.jsonl`。exec / eval は backend・operation・コマンド名・timeout のみを要求情報に記録し、C# 本文や追加引数を記録しない。機能探索は履歴を増やさない。

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
