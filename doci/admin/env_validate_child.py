"""`.env` 候補を検証する専用の子プロセスエントリポイント。

`doci/config.py` はインポート時に副作用で `os.environ` を書き換えて多数の派生定数を
計算し `validate_pipeline_backends()` を呼ぶ非純粋モジュールなので、プロセス内で
`importlib.reload()` するのは安全ではない（既にインポート済みの他モジュールの
派生定数が古いまま残る）。代わりに、`DOCI_DOTENV` 環境変数だけを渡した最小環境
（`tools/cron_generate.sh` が実際にexportする環境と同じ `PATH`/`HOME` のみ）で
このモジュールをサブプロセス起動し、`doci.config`/`doci.channel` を新規プロセスで
一から読み込ませて検証する。stdout へ JSON 1行だけを出す。

直接実行しない。`doci.admin.env_store.validate_candidate()` から
`python -m doci.admin.env_validate_child` として呼ばれる。
"""
from __future__ import annotations

import json
import warnings


def main() -> int:
    try:
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            from doci import channel, config  # noqa: F401  (importそのものが検証)

            channels = channel.discover()
            for channel_id in channels:
                channel.load(channel_id)
            caught = [str(w.message) for w in recorded]
    except Exception as exc:  # noqa: BLE001 - 検証結果として文言をそのまま呼び出し元へ返す
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 0
    print(json.dumps({"ok": True, "channels": channels, "warnings": caught}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
