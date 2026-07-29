# doci

設定駆動の複数チャンネルについて、台本・音声・映像・投稿までを日次実行するワークフローアプリ。
短尺は縦9:16、180秒超は横16:9へ自動ルーティングする。

- 台本生成・ファクトチェック後の文章修正・図表背景 = **OpenCode Go / qwen3.7-plus**。
- リサーチ・構成プラン・ファクトチェック監査 = **OpenCode Go / minimax-m3**。
- 映像生成 = **Minimax**（画像 `image-01` ＋ 一部 Hailuo 動画）。
- 音声 = **VOICEVOX**（コーナー別の声）。
- BGM = インターナショナル ピアノ（PD/CC0版を `channels/ideology/bgm/` に同梱）。
- 合成 = **ffmpeg**（Ken Burns・連結・BGMミックス・字幕焼込み）。

実行時の生成・リサーチ・検証は Claude CLI/API に依存しません。Claude は、必要な場合に
Pull Request の品質確認を行うリポジトリ側 GitHub Actions（`claude-review.yml`）でのみ使います。
既存の `TEXT_BACKEND=claude_cli` や `*_BACKEND=claude` は旧設定との互換性のために残していますが、
自動フォールバックでは呼び出しません。
OpenCode GoのMiniMaxリサーチは実取得済みの候補・一次資料URLだけを根拠として受け入れ、資料がない場合は出典を作らず
リサーチなしで生成を続けます。
ファクトチェックも取得済みの `facts` がある場合だけ実行し、MiniMaxが構造化監査、
Qwenが監査結果だけに基づく文章修正を担当します。資料がない場合は原文を維持します。
資料欠落を実行失敗にしたい運用だけ `SCRIPT_FACTCHECK_REQUIRE_SOURCES=1` を明示します。
監査段の恒常的失敗（モデル誤設定・API障害等）も実行失敗にしたい運用だけ
`SCRIPT_FACTCHECK_REQUIRE_AUDIT=1` を明示します（既定は原文維持で後続処理）。
2段処理全体は既定で `SCRIPT_FACTCHECK_TOTAL_TIMEOUT=900` 秒に制限され、`0` で無制限にできます。

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

[publish.youtube.review]
enabled = false
repository = "owner/repository"
publish_label = "公開承認"
hold_label = "保留"
keep_unlisted_label = "限定公開で保持"

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
| `youtube-growth` | YouTube攻略Ch | ショート・通常動画・分析改善 | 主題ガード通過時 public / それ以外 unlisted |

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
| `pipeline` | `seconds_per_image`, `max_images`, `research`, `factcheck`, `plan`, `asset_media`, `topic_cooldown_days`, `performance_feedback` |
| `style.subtitle` | `font`, `fill`, `stroke`, `box_color`, `box_alpha`, `position_ratio` |
| `style.thumbnail` | `font_family`, `title_color` |
| `style.chart` | `palette`, `font` |
| `style.video` | `pad_color`, `filter` |
| `style.bgm` | `dir`, `volume`, `rotation` |
| `style.credits` | `template` |
| `publish` | `platforms` |
| `publish.youtube` | `privacy`, `client_secret`, `token`, `review` |
| `publish.youtube.review` | `enabled`, `repository`, `publish_label`, `hold_label`, `keep_unlisted_label` |
| `publish.tiktok` | `token`, `privacy` |
| `publish.instagram` | `user_id`, `access_token_env` |

優先順位は「CLIの実行対象指定 → channel.toml → `.env` のグローバル既定値」。
`PUBLISH_*=0` と `PUBLISH_DRY_RUN=1` は安全弁として常に優先する。
台本の出力規則は共通の `doci/prompts/output_rules.md`（またはチャンネル側の
`prompts/output_rules.md` による全面上書き）の後へ、任意の
`channels/<id>/prompts/output_rules_addendum.md` を追加できる。追加ファイルがない
チャンネルのプロンプトは従来どおりとなる。
`pipeline.topic_cooldown_days` は公開済み・キュー済みの近似題材を再利用しない期間で、
既定は30日、`0`で無効化する。重複runは動画生成・投稿前に正常スキップされ、理由が
チャンネル別 `history.jsonl` に記録される。
`pipeline.performance_feedback = true` は投稿履歴の動画をYouTube Data APIで
read-only同期し、十分な比較標本がある場合だけ相対的な形式仮説を次回promptへ渡す。
retention等のAnalytics指標には、OAuthクライアントのGoogle Cloud projectで
YouTube Analytics APIを有効化したうえで追加scopeが必要。明示的に許可する場合のみ
`python -m doci.youtube --auth --analytics --channel <id>` で再認証する。APIやscopeが
未設定でもData API snapshotを残し、指標を推測せず通常生成を継続する。
`python -m doci.performance --sync --channel <id> --corner <key>` でreadbackと判断根拠を
確認できる。仮説は同一corner・同一尺・同一tierの最低8本を比較し、1回に1形式だけを
YouTube投稿成功1本へ適用する。その動画が評価閾値に届くまで同じcornerの次実験は待機する。

### YouTube攻略Ch の主題確認

`youtube-growth` は、企画に次の3点が明記され、主題適合も `clear` の場合だけ自動で
`public` 投稿する。
この運用を有効にするチャンネルの `publish.youtube.privacy` は、確認待ちの安全な
基準値として `unlisted` を必須とし、未指定時もグローバル設定に関係なく `unlisted` になる。
最終状態はこの基準値を暗黙上書きするのではなく、
上記判定による `public` または `unlisted` のどちらかとして明示的に決まる。

- 対象者がYouTube制作者
- 解決する具体的なYouTube上の課題または指標
- 視聴後にYouTube Studioまたは次の動画制作で取れる操作

1点でも欠ける、主題が曖昧、リサーチが失敗した、またはタイトルからYouTube攻略と
確認できない場合も生成は止めず、`unlisted` で投稿して動画ごとのGitHub Issueを作る。
Issueでは `公開承認` / `保留` / `限定公開で保持` のうち1ラベルだけを決定として使う。
`公開承認` の場合だけYouTubeを公開へ変更し、動画URLをIssueへ記録して自動クローズする。
ほかの2ラベル、ラベル無し、複数ラベル、限定公開からの経過時間では公開設定を変更しない。

公開設定の変更には `youtube.force-ssl` scope が必要なため、運用開始前に対象チャンネルを
次のコマンドで再認証する。`--analytics` は既存の実績分析scopeも同時に維持するために指定する。

```bash
python -m doci.youtube --auth --analytics --manage --channel youtube-growth
```

決定ラベルはリポジトリ設定で事前に作成する。dociはラベルを自動作成せず、GitHub操作には
既存 `gh` 認証を使う。実行時に `gh api user` から確認した認証ユーザー以外が作成した
同形式のIssueは追跡対象にしない。Issue作成intentにはその認証ユーザー名をoutboxへ
耐久記録し、作成結果が不明な間に認証ユーザーが変わった場合は重複作成せずfail-closedにする。
トークンや秘密値は設定・ログ・Issue本文へ保存しない。
3時間ごとの既存 `--all-channels` launchd 実行は、VOICEVOX起動や動画生成より前に
`--reconcile-youtube-reviews` を実行して確認Issueを取得する。動画単位の処理失敗も
CLIの非zero終了へ伝搬し、その場合は後続のチャンネルrunでも生成前に再試行する。限定公開アップロードは
Issue作成より先に `output/<channel>/youtube_review_outbox.jsonl` へ耐久記録されるため、
Issue作成や後続の履歴保存に失敗しても次の3時間実行で再試行される。
Issue作成結果が不明な動画だけはSearch index反映前の重複作成を避け、同一cron内では
再試行せず次の3時間cycleまで待つ。
`保留` は3時間周期に1回だけIssueを再取得し、動画状態もoutbox状態も変更・追記しない。
記録済みの確認Issue番号は作成者単位のGraphQL batchで直接取得し、無関係なIssue総数に
依存しない。公開・限定公開保持の変更直前だけ対象Issueを個別再取得する。
`限定公開で保持` の決定は動画を変更せず、確定コメントを残してIssueをクローズする。
`保留` とラベル無しはopenのまま次の3時間確認へ残す。
未決Issueを経過時間で打ち切ることはなく、outboxは各動画の最新状態を残したままatomicに
圧縮する。同一cron内の選択的再試行planも並行cycleを分離しつつ最大64ファイルに制限する。

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
VOICEVOX を service container（2話者内蔵・常時稼働不要）で起動し、台本・文章修正は
OpenCode Go / Qwen、リサーチ・監査は OpenCode Go / MiniMax、映像は Minimax REST、
cron で日次実行。Secrets は GitHub Secrets に格納する。

## 構成
- `doci/ai_text.py` 台本・文章修正（OpenCode Go / qwen3.7-plus、JSON出力）
- `doci/research.py` リサーチ（OpenCode Go / minimax-m3、取得済み資料内で整理）
- `doci/factcheck.py` MiniMax構造化監査 → Qwen文章修正
- `doci/voicevox.py` 音声合成（文ごとの再生長＝字幕タイミング付き）
- `doci/minimax.py` 画像/動画（非同期ポーリング）
- `doci/compose.py` ffmpeg 合成（9:16・字幕焼込み）
- `doci/youtube.py` アップロード・公開設定更新
- `doci/youtube_review.py` 主題ガード・限定公開Issue確認
- `channels/<id>/` チャンネル定義・ペルソナ・声・BGM
- `doci/prompts/output_rules.md` 全チャンネル共通の出力規則
- `doci/run_daily.py` オーケストレータ
