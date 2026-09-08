# unitap を共通入口にする（2026-09-09）

## 結論

unitap は削除せず、普段の入口を unitap に一本化する。既存の障害診断・コンパイル回復・ゲーム専用検証は unitap 本体、UniCLI の汎用コマンドと C# eval は明示的な `unitap unicli` サブコマンドを使う。既存コマンドを自動で別バックエンドへ置き換えない。

| 用途 | 入口 | 理由 |
|---|---|---|
| 接続・コンパイル障害の診断と回復 | `unitap status` / `diagnose` / `compile_check` | heartbeat、ファイル経路、Editor.log を使う既存機能 |
| 専用の通し検証、capture、独自ツール | `unitap tool_exec` など | 既存のゲーム固有処理をそのまま利用 |
| 汎用の状態・アセット・テスト操作 | `unitap unicli exec ...` | UniCLI の構造化コマンドを再利用 |
| 一時的な Unity API 調査、ビルド呼び出し | `unitap unicli eval '...'` | 毎回専用ツールを書かずに C# を実行 |
| 接続・バージョン確認、コマンド探索 | `unitap unicli check` / `commands` | UniCLI 側の状態を確認 |

## 導入

検証環境は Unity 6000.6.0f1、UniCLI CLI / Server v1.7.0、macOS arm64。既存の unitap ローカルパッケージに加えて、Unity の `Packages/manifest.json` に以下を追加する。

```json
"com.yucchiy.unicli-server": "https://github.com/yucchiy/UniCli.git?path=src/UniCli.Unity/Packages/com.yucchiy.unicli-server#v1.7.0"
```

CLI は [UniCLI 公式リリース](https://github.com/yucchiy/UniCli/releases/tag/v1.7.0) の OS 対応バイナリを PATH に置く。この環境は `/Users/eipoc/.local/bin/unicli`。別の場所なら `UNITAP_UNICLI_BIN` に実行ファイルを指定する。unitap は自動ダウンロード・インストールを行わない。unitap 本体だけ使う場合には UniCLI は不要。

```sh
# 以下の unitap は python3 /path/to/unitap/cli/unitap.py でもよい。
unitap --project /path/to/UnityProject --json unicli check
unitap --project /path/to/UnityProject --json unicli exec PlayMode.Status
unitap --project /path/to/UnityProject --json unicli exec TestRunner.RunEditMode --assemblies RabbitPunch.Tests
unitap --project /path/to/UnityProject --json unicli eval 'return UnityEngine.Application.unityVersion;'
unitap --project /path/to/UnityProject --json unicli --timeout-ms 600000 eval 'RabbitPunch.Editor.PunchWebBuild.Build(); return "built";'
```

グローバルオプションは `unicli` より前、`--timeout-ms` は `exec` / `eval` より前に置く。対象プロジェクトは unitap が解決・正規化し、同じパスをロック、cwd、`UNICLI_PROJECT` に使う。子 CLI に別の `--project` / `--timeout` を渡すことは拒否する。`exec` と `eval` は既定で `--no-focus`。C# と引数はシェルを経由せず配列のまま渡す。

## 成功、失敗、履歴と排他

成功は `ok: true`、`result.backend: "unicli"`、`result.response` に UniCLI の応答全体。CLI が非ゼロ終了、`success: false`、不正 JSON、起動不能、タイムアウトなら `ok: false` と終了コード 1。自動再試行・unitap 本体への自動フォールバックは行わない。

全 `unicli` サブコマンドが既存の `Library/Unitap/.editor-op.lock` を使う。既定で待機し、`--no-wait-lock` なら即時 `editor_busy`、`--lock-timeout` で待機上限を指定できる。unitap を通さない UniCLI の直接実行や手動 Editor 操作には、この排他は効かない。非同期コマンドが「開始済み」で返る場合は処理完了までロックが続くわけではない。

タイムアウトは CLI 待ちを打ち切るが、Unity 側処理の取り消しを保証しない。ロック解除後も Unity がビルド等を続ける可能性があるため、Editor・ログ・状態を調べてから次の変更を実行する。

実行履歴は `Library/Unitap/execution-history.jsonl` に記録する。operation、backend、exec のコマンド名、timeout を記録し、eval の C# 本文や追加引数は新たに履歴へ保存しない。

## 実測からの判断

Rabbit Punch で両方の接続・テストを確認済み。以前の3回測定では状態応答の中央値は unitap 0.280 秒 / UniCLI 0.125 秒、コンパイルは 3.696 秒 / 2.405 秒。ただし前面化条件が異なり、長期の安定性や全操作の優劣を示す数字ではない。

UniCLI 1.7.0 の eval は Play 後に CS1703（BCL facade の重複参照）を再現した。停止のみでは復旧せず、unitap の compile_check 後に復旧した。このため全面置換せず、明示的な補助バックエンドとする。テストは誤った assembly 名でも成功・0件になるので、`total > 0` と `failed == 0` を確認する。

今回のブリッジは Python 回帰21件（別プロセスの排他拒否を含む）、実 Editor の状態取得・EditModeテスト6件成功・WebGLビルド成功で確認した。

## ミニゲームの実装順

Unity が最終成果物なら直接 Unity から開始してよい。縦画面の基準、UI Toolkit の文字・カード・ゲージ、3D、入力→判定→結果→再挑戦の一周を先に作る。Editor で短い反復を回し、最初の一周の時点で Web ビルドを作ってロード・フォント・入力・音声を確認する。Babylon の前段を必須にすると二重実装になる。Web を最終成果物にする場合や Web の配布速度を優先する試作では Babylon が有効。
