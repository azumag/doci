"""doci設定・プロンプト管理用のローカルWeb UI（localhost限定・単独運用者向け）。

`python -m doci.admin` または `doci-admin` で起動する。cron/launchd が直接参照する
作業ディレクトリ上のファイル（.env・channels/<id>/channel.toml・プロンプトmd・
一部のコード内蔵プロンプト定数）を、保存前バリデーション＋アトミック書き込み＋
バックアップ付きで編集するためのサーバ。詳細は各サブモジュールのdocstring参照。
"""
from __future__ import annotations
