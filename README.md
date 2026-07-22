# doci

設定駆動の複数チャンネルについて、台本・音声・映像・投稿までを日次実行するワークフローアプリ。
短尺は縦9:16、180秒超は横16:9へ自動ルーティングする。

- 台本生成 = **opus 4.8**（`claude` CLI / Anthropic API）。文章生成に Minimax は使わない。
- 映像生成 = **Minimax**（画像 `image-01` ＋ 一部 Hailuo 動画）。
- 音声 = **VOICEVOX**（コーナー別の声）。
- BGM = インターナショナル ピアノ（PD/CC0版を `channels/ideology/bgm/` に同梱）。
- 合成 = **ffmpeg**（Ken Burns・連結・BGMミックス・字幕焼込み）。

同梱の `ideology` チャンネルには次の2コーナーがある。

| コーナー | 立場 | 声 |
|---|---|---|
| communism（共産主義ネタ） | 共産主義者として振る舞う | 中華AI（VOICEVOX spk3 ずんだもん） |
| capitalism（資本主義ネタ） | 資本主義者として振る舞う | メリケンAI（VOICEVOX spk14 冥鳴ひまり） |

## アーキテクチャ

```text
channels/<id>/channel.toml
  ├─ prompts / voices / bgm / style / publish
  ▼
run_daily（単一 / 全チャンネル逐次）
  ├─ 共通生成パイプライン: ai_text → voicevox → assets → compose
  ├─ チャンネル別履歴: output/<id>/history.jsonl
  └─ チャンネル別投稿資格情報: secrets/<id>/
```

チャンネル固有値は `channels/<id>/`、全チャンネル共通の出力規則と実装は `doci/` に置く。

## セットアップ

```bash
cd doci
python3 -m venv .venv && . .venv/bin/activate
pip install -e .                 # YouTube アップロード用ライブラリ
cp .env.example .env             # 値を埋める（MINIMAX_API_KEY 等）
```

旧単一チャンネル配置から更新する場合、未追跡の履歴・OAuthファイルを先にdry-run確認して移す。

```bash
python tools/migrate_channels.py
python tools/migrate_channels.py --apply
```

### YouTube 認証（初回のみ）
1. Google Cloud で YouTube Data API v3 を有効化し、OAuth「デスクトップ」クライアントを作成 → `client_secret.json` を置く。
2. `python -m doci.youtube --auth` で同意 → `youtube_token.json`（refresh token）が保存される。以降は無人。

チャンネル別アカウントでは、資格情報を `secrets/<channel>/` に置き、次のように認証する。

```bash
python -m doci.youtube --auth --channel ideology
python -m doci.tiktok --auth --channel ideology
```

`secrets/` はGit管理対象外。`channel.toml` には秘密値を直接書かず、資格情報のパスか
アクセストークンを保持する環境変数名だけを書く。

```toml
[publish]
platforms = ["youtube"]

[publish.youtube]
privacy = "unlisted"
client_secret = "secrets/ideology/client_secret.json"
token = "secrets/ideology/youtube_token.json"

[publish.tiktok]
token = "secrets/ideology/tiktok_token.json"
privacy = "SELF_ONLY"

[publish.instagram]
user_id = "123456789"
access_token_env = "IG_TOKEN_IDEOLOGY"
```

パスはリポジトリルート相対。`[publish]` 未指定時は `.env` の従来パスへフォールバックする。
`PUBLISH_YOUTUBE` / `PUBLISH_TIKTOK` / `PUBLISH_INSTAGRAM` の `0` は、個別設定より優先する
全チャンネル共通の強制OFFスイッチ。実投稿前は `PUBLISH_DRY_RUN=1` で参照先を確認する。

## 使い方

現在のチャンネル:

| ID | 名前 | 内容 | YouTube公開設定 |
|---|---|---|---|
| `ideology` | doci（ソ連/アメリカ） | 共産主義・資本主義の小噺 | public |
| `youtube-growth` | YouTube攻略Ch | ショート・通常動画・分析改善 | unlisted |

```bash
# 1本だけ生成（アップロードしない・動作確認）
python -m doci.run_daily --channel ideology --no-upload

# コーナー指定 / 日付指定
python -m doci.run_daily --channel ideology --corner communism --no-upload

# YouTube攻略Chを生成（アップロードしない）
python -m doci.run_daily --channel youtube-growth --no-upload

# チャンネル指定 / 全チャンネル逐次 / 一覧
python -m doci.run_daily --channel ideology --no-upload
python -m doci.run_daily --all-channels --no-upload
python -m doci.run_daily --list-channels
```

`--all-channels` は1チャンネルが失敗しても残りを続行し、全滅時だけ非0で終了する。結果JSONに
各チャンネルの成否が入る。

## 新しいチャンネルを追加する

1. `channels/<id>/` に `channel.toml`、`prompts/`、`voices.json`、必要なら `bgm/` を作る。
   `<id>` は launchd のラベルにも使うため英数字・`_`・`-` のみとする。
2. 下記の設定例を埋め、`python -m doci.run_daily --channel <id> --no-upload` で試写する。
3. `python -m doci.youtube --auth --channel <id>` で投稿先を認証する。
4. `PUBLISH_DRY_RUN=1` でtoken参照先を確認後、限定公開で実投稿する。
5. `tools/install_launchd.sh 10800 <id>` で個別登録するか、引数なしの全チャンネルジョブを使う。

最小構成例:

```toml
voices = "voices.json"

[channel]
id = "sample"
name = "Sample channel"
rotation = ["main"]

[corners.main]
label = "Main"
persona = "prompts/persona.md"
corner = "prompts/corner.md"
voice = "narrator"

[pipeline]
research = false
factcheck = false

[style.bgm]
dir = "bgm"

[publish]
platforms = ["youtube"]

[publish.youtube]
client_secret = "secrets/sample/client_secret.json"
token = "secrets/sample/youtube_token.json"
```

主な設定:

| table | keys |
|---|---|
| `channel` | `id`, `name`, `rotation` |
| `corners.<key>` | `label`, `persona`, `corner`, `voice` |
| `pipeline` | `seconds_per_image`, `max_images`, `research`, `factcheck`, `plan`, `asset_media` |
| `style.subtitle` | `font`, `fill`, `stroke`, `box_color`, `box_alpha`, `position_ratio` |
| `style.thumbnail` | `font_family`, `title_color` |
| `style.chart` | `palette`, `font` |
| `style.video` | `pad_color`, `filter` |
| `style.bgm` | `dir`, `volume`, `rotation` |
| `style.credits` | `template` |
| `publish` | `platforms` |
| `publish.youtube` | `privacy`, `client_secret`, `token` |
| `publish.tiktok` | `token`, `privacy` |
| `publish.instagram` | `user_id`, `access_token_env` |

優先順位は「CLIの実行対象指定 → channel.toml → `.env` のグローバル既定値」。
`PUBLISH_*=0` と `PUBLISH_DRY_RUN=1` は安全弁として常に優先する。

個別レイヤのテスト:
```bash
python -m doci.ai_text  --corner capitalism          # 台本JSON
python -m doci.voicevox --speaker 14 --text "..."     # 音声
python -m doci.minimax  --image "a red flag, cinematic, vertical"  # 画像
```

## 日次スケジュール（ローカル）
```bash
tools/install_launchd.sh
# チャンネル別の時刻にしたい場合
tools/install_launchd.sh 10800 ideology
```
launchd エージェント（`com.azumag.doci.generate`）を現在のプロジェクト位置から生成・再ロードする。
既定ジョブは `--all-channels` を逐次実行する。第2引数にチャンネルを指定すると
`com.azumag.doci.generate.<id>` を登録する。プロジェクトを移動した場合は再実行すれば復旧する。

## クラウド移行
ローカル依存は **VOICEVOX のみ**。`.github/workflows/daily.yml` が雛形:
VOICEVOX を service container（2話者内蔵・常時稼働不要）で起動し、台本は Anthropic API、映像は Minimax REST、cron で日次実行。Secrets は GitHub Secrets に格納。

## 構成
- `doci/ai_text.py` 台本（opus 4.8、JSON出力）
- `doci/voicevox.py` 音声合成（文ごとの再生長＝字幕タイミング付き）
- `doci/minimax.py` 画像/動画（非同期ポーリング）
- `doci/compose.py` ffmpeg 合成（9:16・字幕焼込み）
- `doci/youtube.py` アップロード（unlisted）
- `channels/<id>/` チャンネル定義・ペルソナ・声・BGM
- `doci/prompts/output_rules.md` 全チャンネル共通の出力規則
- `doci/run_daily.py` オーケストレータ
