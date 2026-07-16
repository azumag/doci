"""旧単一チャンネルの未追跡データを channels/ideology 用配置へ移す。

既定は dry-run。実行する場合:
    python tools/migrate_channels.py --apply

追跡済み prompts / voices / BGM はGit上で移行済み。このツールはGit管理しない履歴と
OAuth資格情報だけを扱い、既存の移行先ファイルは上書きしない。
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def migration_pairs(root: Path) -> list[tuple[Path, Path]]:
    return [
        (root / "output/history.jsonl", root / "output/ideology/history.jsonl"),
        (root / "client_secret.json", root / "secrets/ideology/client_secret.json"),
        (root / "youtube_token.json", root / "secrets/ideology/youtube_token.json"),
        (root / "tiktok_token.json", root / "secrets/ideology/tiktok_token.json"),
    ]


def migrate(root: Path, *, apply: bool) -> list[dict[str, str]]:
    root = Path(root).resolve()
    results: list[dict[str, str]] = []
    for source, destination in migration_pairs(root):
        relative_source = str(source.relative_to(root))
        relative_destination = str(destination.relative_to(root))
        if not source.exists():
            status = "source_missing"
        elif destination.exists():
            status = "destination_exists"
        elif not apply:
            status = "would_move"
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            status = "moved"
        results.append(
            {
                "source": relative_source,
                "destination": relative_destination,
                "status": status,
            }
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="実際にファイルを移動")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    results = migrate(args.root, apply=args.apply)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    blocked = any(item["status"] == "destination_exists" for item in results)
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
