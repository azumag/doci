# doci プロジェクト固有の指示

Claude Code の一般的な作業規律は `AGENTS.md` を参照。ここにはそれに加えて
Claude Code に対して明示的に記憶させたい事項を書く。

## Admin UI (doci/admin/) を内部変更と同時に更新する

`doci/admin/` は `.env`・`channels/<id>/channel.toml`・Markdownプロンプト・
コード内蔵プロンプト文字列定数(11個)を編集するローカルWeb UI(PR #190で追加)。
これらが依存する内部実装を変更するときは、追って直すのではなく**同じ変更の中で**
Admin UI側も追随させること。具体的には:

- `doci/ai_text.py`・`doci/factcheck.py`・`doci/research.py`・`doci/plan.py`・
  `doci/tactic_backfill.py` 内のプロンプト文字列定数を追加/削除/リネーム/移動したり、
  `.format()`呼び出し箇所のkwargsを変更した場合 →
  `doci/admin/code_prompt_registry.py` の該当エントリを追加/削除/更新する。
  このレジストリは安全のため意図的に自動探索ではなく手書きの静的リストなので、
  実装側だけ変更しても自動的には追随しない。
- `doci/channel.py` の channel.toml スキーマ(許可キー・検証ルール)を変更した場合 →
  `doci/admin/channel_store.py` の解決後プレビュー(`_summarize()`)が新しいフィールドを
  表示できているか確認する。検証自体は `channel.load()` をそのまま再利用しているため
  通常は追随するが、プレビュー表示だけは手動反映が必要な場合がある。
- `doci/config.py` に新しい環境変数や `_SUPPORTED_*` 選択肢集合を追加した場合 →
  `doci/admin/env_schema.py` は `config.py` をASTで走査して型・選択肢を自動収集する
  ため通常は追随するが、新しい選択肢集合は `env_schema.py` の
  `_CHOICES_ATTR_BY_KEY` に登録しないとUI上で選択式にならない(登録しなくても
  文字列入力にフォールバックするだけで壊れはしない)。
- `doci/corners.py` のプロンプト組み立てロジック(persona/corner/output_rules の
  結合方法や `{date}`/`{past_topics}` 以外の新しい差し込みトークン)を変更した場合 →
  `doci/admin/markdown_store.py` の `required_tokens` 判定を更新する。

変更後は `tests/test_admin_*.py` を実行し、レジストリ・スキーマの前提が壊れて
いないか確認すること。
