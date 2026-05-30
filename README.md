# doci

ラジオ番組のコンテンツ生成ロジックを使って、**毎日1本のショート動画（縦9:16）を自動生成し YouTube にアップロード**するワークフローアプリ。

- 台本生成 = **opus 4.8**（`claude` CLI / Anthropic API）。文章生成に Minimax は使わない。
- 映像生成 = **Minimax**（画像 `image-01` ＋ 一部 Hailuo 動画）。
- 音声 = **VOICEVOX**（コーナー別の声）。
- BGM = インターナショナル ピアノ（PD/CC0版を `assets/bgm/` に同梱）。
- 合成 = **ffmpeg**（Ken Burns・連結・BGMミックス・字幕焼込み）。

v1 はこの2コーナーのみ。他コーナーは後日アップグレードで追加。

| コーナー | 立場 | 声 |
|---|---|---|
| communism（共産主義ネタ） | 共産主義者として振る舞う | 中華AI（VOICEVOX spk3 ずんだもん） |
| capitalism（資本主義ネタ） | 資本主義者として振る舞う | メリケンAI（VOICEVOX spk14 冥鳴ひまり） |

その他のコーナー（将来）は共産主義テイストを残しつつ傾倒しすぎず、現代社会への多角的な風刺を入れる方針。

## セットアップ

```bash
cd doci
python3 -m venv .venv && . .venv/bin/activate
pip install -e .                 # YouTube アップロード用ライブラリ
cp .env.example .env             # 値を埋める（MINIMAX_API_KEY 等）
```

### YouTube 認証（初回のみ）
1. Google Cloud で YouTube Data API v3 を有効化し、OAuth「デスクトップ」クライアントを作成 → `client_secret.json` を置く。
2. `python -m doci.youtube --auth` で同意 → `youtube_token.json`（refresh token）が保存される。以降は無人。

## 使い方

```bash
# 1本だけ生成（アップロードしない・動作確認）
python -m doci.run_daily --no-upload

# コーナー指定 / 日付指定
python -m doci.run_daily --corner communism --no-upload

# 生成＋unlistedでアップロード（前回と交互にコーナー選択）
python -m doci.run_daily
```

個別レイヤのテスト:
```bash
python -m doci.ai_text  --corner capitalism          # 台本JSON
python -m doci.voicevox --speaker 14 --text "..."     # 音声
python -m doci.minimax  --image "a red flag, cinematic, vertical"  # 画像
```

## 日次スケジュール（ローカル）
```bash
cp scripts/com.azumag.doci.daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.azumag.doci.daily.plist
```

## クラウド移行
ローカル依存は **VOICEVOX のみ**。`.github/workflows/daily.yml` が雛形:
VOICEVOX を service container（2話者内蔵・常時稼働不要）で起動し、台本は Anthropic API、映像は Minimax REST、cron で日次実行。Secrets は GitHub Secrets に格納。

## 構成
- `doci/ai_text.py` 台本（opus 4.8、JSON出力）
- `doci/voicevox.py` 音声合成（文ごとの再生長＝字幕タイミング付き）
- `doci/minimax.py` 画像/動画（非同期ポーリング）
- `doci/compose.py` ffmpeg 合成（9:16・字幕焼込み）
- `doci/youtube.py` アップロード（unlisted）
- `doci/corners.py` / `doci/prompts/` コーナー・ペルソナ（ソ連ゲーム要素は除去済）
- `doci/run_daily.py` オーケストレータ
