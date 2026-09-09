# Unitap を扱う Codex / エージェントへ

- Unity 操作には対象プロジェクトの Unitap ラッパーを優先する。この checkout の dogma では `../scripts/unitap`。別の Editor を操作しない。
- 最初に `unitap --json commands`、接続後に `unitap --json commands --live --search <用途>` で実際に利用できる機能を調べる。`unitap` はプロジェクトのラッパーまたは `python3 /path/to/unitap/cli/unitap.py --project /path/to/UnityProject` を意味する。
- `unitap --json describe <一覧のid>` で引数・既定値・返却型を確認してから使う。詳細な操作手順・失敗時の扱いは [docs/agent-guide.md](docs/agent-guide.md)。
- UniCLI の操作も `unitap exec` / `unitap eval` / `unitap unicli` を通す。既存のロック、プロジェクト指定、履歴を迂回しない。
- 新機能は実際の CLI パーサーまたは C# ツール属性に登録し、探索結果から見える状態を維持する。一覧用の別のコマンド名リストを作らない。
- Python の検証は `PYTHONPATH=cli python3 -m unittest discover -s cli/tests -q`。C# 変更は対象 Editor で `compile_check` と変更したツールの実行結果を確認する。
- 未コミット変更を戻さない。ログや検証結果は `.tmp/` に保存し、外部公開しない。
- このリポジトリを変更した作業は、必要な検証を行い、自分の変更を commit・push するまで完了としない。毎回の確認は不要。ユーザーが明示的に commit / push を止めた場合はその指示を優先する。
- submodule として利用している場合は、先に Unitap の commit・push を完了し、親リポジトリの submodule 参照も更新して commit・push する。無関係な未コミット変更は含めない。push 失敗時は未反映であることと原因を報告し、force push はしない。
