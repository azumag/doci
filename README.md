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
監査段・書き換え段いずれかの恒常的失敗（モデル誤設定・API障害等）も
実行失敗にしたい運用だけ `SCRIPT_FACTCHECK_REQUIRE_AUDIT=1` を明示します
（既定は原文維持で後続処理）。
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
  ├─ 全チャネル共通の日次投稿枠・投稿状態台帳: output/topic_ledger.jsonl
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
| `youtube-growth` | YouTube攻略Ch | ショート・通常動画・分析改善 | 実績施策を適用した動画のみ public / それ以外 unlisted |

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

### outputの保存ポリシー

`output/<channel>/<日時>_<コーナー>_<時刻>` はアップロード完了までの一時領域として扱う。
少なくとも1投稿が成功し、ほかの投稿結果に `error` / `unknown` がない場合、完成動画、
シーン動画、ナレーション音声、写真、サムネイルは履歴の耐久保存後に自動削除する。
`script.json`、図表仕様JSON、復元時の音声・レンダー設定を記録した `recovery.json` は残る。
`--no-upload`、dry-run、投稿失敗、結果不明の場合は再送用に媒体を保持する。

既存成果物は、履歴でアップロード成功を確認できたworkdirだけを次のコマンドで整理できる。
既定は読み取り専用previewで、`--apply`を付けた場合だけ削除する。

```bash
python -m doci.output_cleanup
python -m doci.output_cleanup --apply
python -m doci.output_cleanup --channel ideology --apply
```

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
| `pipeline` | `seconds_per_image`, `max_images`, `research`, `factcheck`, `plan`, `asset_media`, `topic_cooldown_days`, `performance_feedback`, `title_pattern_check`, `plan_topic_retries`, `max_uploads_per_day`, `performance_eval_window_hours`, `performance_gated_publish` |
| `style.subtitle` | `font`, `fill`, `stroke`, `box_color`, `box_alpha`, `position_ratio` |
| `style.thumbnail` | `font_family`, `title_color` |
| `style.chart` | `palette`, `font` |
| `style.video` | `pad_color`, `filter` |
| `style.bgm` | `dir`, `volume`, `rotation` |
| `style.credits` | `template` |
| `publish` | `platforms` |
| `publish.youtube` | `privacy`, `client_secret`, `token`, `review` |
| `publish.youtube.review` | `enabled` |
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
チャンネル別 `history.jsonl` に記録される。この題材照合はチャンネル単位で完結し、
別チャンネルとの跨ぎ照合は行わない（チャンネル間で扱うテーマは十分に異なるため）。
続編・反対視点・別視聴者向けは、元題材・新しい切り口・違いをresearchが構造化して
明示した場合だけ同一チャンネル内で再利用できる。単なるタイトル・形式変更は許可しない。
制作前の `queued` 予約は既定24時間のリースで、異常終了したrunを無期限にブロックしない。
外部投稿開始後の `publishing` は結果不明でも安全側に保持し、手動確認が完了するまで
再利用しない。
research無し（`ideology`等）のコーナーは、執筆前の構成プラン段（起承転結の実質テーマ＝
`plan.topic`）でこのcooldownを照合する。タイトルは煽り文句で言い換えられやすく重複検出を
すり抜けやすいため、内容そのものに近いこの段階で判定し、重複と判定されても即スキップにせず
直近題材を避けて`pipeline.plan_topic_retries`（既定3。初回1回＋重複時の再設計最大2回、
という初回込みの総試行回数）まで構成を設計し直す。すべて重複のまま試行を使い切った場合だけ、
その回を通常どおりスキップする。この再設計ループ全体は
`PLAN_TOPIC_TOTAL_TIMEOUT`（既定1800秒、`0`で無制限）で打ち切られ、backend不調日に
試行回数分の待ち時間が積み重なるのを防ぐ。打ち切り時はプラン無しにフォールバックし、
従来どおりタイトルベースでcooldownを照合する。
`pipeline.title_pattern_check = true`（既定OFF、`youtube-growth`で有効）は、題材が違っても
タイトルの修辞パターン（固有名詞・問題語・疑問形/煽り構文）が使い回されていないかをLLMで
検出し、`script._title_pattern_check`へ記録する（検出のみで公開判断は変えない）。
`pipeline.max_uploads_per_day` を設定したチャンネルは、JST暦日ごとの実投稿枠を、
全チャンネル共通の `output/topic_ledger.jsonl` をファイルロック下で使って原子的に
予約する（この台帳は日次枠と投稿状態の安全な管理だけに使い、題材内容の跨ぎ照合は
行わない）。`--no-upload` と `PUBLISH_DRY_RUN=1` は枠を消費せず、制作失敗時は解放、
投稿成功または結果不明時は安全側に保持する。日付をまたいだ `queued`／`publishing`も
結果確定まで次の枠を使わない。
プロセス停止などで結果不明の `publishing` が残った場合は、自動解除せず、外部側の結果を
運用者が確認してから次で明示的に終端化する。未投稿を確認した場合は `cancelled`、投稿済み
動画を確認した場合は `published` と動画IDを指定する。共通台帳と該当チャネル履歴を同時に
復旧し、操作理由を記録する。

```bash
# 外部投稿が発生していないことを確認した後
python -m doci.run_daily --recover-publishing <reservation-id> \
  --recovery-status cancelled --recovery-reason "YouTube Studioで投稿なしを確認"

# 投稿済み動画を確認した場合
python -m doci.run_daily --recover-publishing <reservation-id> \
  --recovery-status published --recovery-video-id <video-id> \
  --recovery-reason "YouTube Studioで投稿済みを確認"
```
`pipeline.performance_feedback = true` は投稿履歴の動画をYouTube Data APIで
read-only同期し、十分な比較標本がある場合だけ相対的な形式仮説を次回promptへ渡す。
retention等のAnalytics指標には、OAuthクライアントのGoogle Cloud projectで
YouTube Analytics APIを有効化したうえで追加scopeが必要。明示的に許可する場合のみ
`python -m doci.youtube --auth --analytics --channel <id>` で再認証する。APIやscopeが
未設定でもData API snapshotを残し、指標を推測せず通常生成を継続する。
`python -m doci.performance --sync --channel <id> --corner <key>` でreadbackと判断根拠を
確認できる。仮説は同一corner・同一尺・同一tierの最低8本を比較し、1回に1形式だけを
YouTube投稿成功1本へ適用する。その動画が評価閾値に届くまで同じcornerの次実験は待機する。
`pipeline.performance_eval_window_hours`（既定0＝無効、`performance_feedback = true`
必須。設定時に無ければ`ChannelConfigError`）を設定したチャンネルは、閾値
到達だけで評価完了とせず、適用時刻からこの時間が経過するまで同じcornerの次実験生成を
`PerformanceEvalWindowSkip`として通常スキップする（初動データが育つ前に次の実験が
投稿されるのを防ぐ、issue #38）。`youtube-growth`は72時間を設定している。
このゲートは動画IDが確定した実験（`performance_applied`/`published`）だけに適用され、
video_id未確定のまま保留された`performance_queued`行（下記の投稿結果`unknown`保留分）
には適用しない。保護すべき公開済み動画が存在しないうえ、適用してしまうと復旧まで
生成自体が最長window_hours分だけ止まってしまうため。

実験はcorner単位で独立しているため、`--corner`未指定の自動選択では、rotationの
次候補が評価期間内でブロックされていても、そこで諦めずrotationの他のcornerを
順に試す（`corners.rotation_order`）。全cornerが評価期間内のときだけ、その回を
通常どおりスキップする。1つのcornerの評価待ちだけで、無関係な他cornerの投稿枠
まで奪わないための挙動。

YouTube投稿結果が`unknown`（タイムアウト等でAPI受理の可否が不明）の場合、実際には
公開済みの可能性があるため実績適用（`performance_application_id`）は自動で取り消さず、
`topic_ledger`の`publishing`予約と同様に運用者確認まで保留する。保留したままだと
`active_performance_experiment`がこのapplicationを返し続け、そのcornerの次実験が
永久に適用されなくなる（`performance_gated_publish`のチャンネルは新規動画が
永久にunlistedのままになる）ため、外部状態を確認したら明示的に終端化する。

```bash
# 外部投稿が発生していないことを確認した後
python -m doci.run_daily --channel <id> \
  --recover-performance-application <application-id> \
  --recovery-status cancelled --recovery-reason "YouTube Studioで投稿なしを確認"

# 投稿済み動画を確認した場合
python -m doci.run_daily --channel <id> \
  --recover-performance-application <application-id> \
  --recovery-status published --recovery-video-id <video-id> \
  --recovery-reason "YouTube Studioで投稿済みを確認"
```

### YouTube攻略Ch の公開判定

`youtube-growth` は `max_uploads_per_day = 1` とし、JSTで1日1本だけ実投稿する。
GitHub Issueでの人手承認・ラベル待ち・reconcileの仕組みは廃止した。人手ラベルや
限定公開からの経過時間は公開可否に一切関与しない。

`pipeline.performance_gated_publish = true`（`performance_feedback = true` が前提。
`publish.youtube.review.enabled` との併用は設定エラー）を設定したチャンネルは、
実績フィードバックの単一変数施策（`performance_feedback`のdecision）を実際に
予約・適用できたrunの動画だけを `public` で投稿し、それ以外は全て `unlisted` の
まま投稿する。適用有無は生成時点で確定しており、後からの承認・切り替えは無い。
判定結果は `script["_performance_gated_publish"]`（`applied`/`privacy`/`decision_id`）
に記録される。

稼働初期など、`performance_feedback` の比較標本が十分に育つまでは（相対比較には
最低8本の動画が必要）、実験が `active` にならず全動画が `unlisted` のままになる。
これは意図した挙動であり、既存の公開済み動画が十分に蓄積すると解消する。

`pipeline.performance_eval_window_hours` の72時間ゲート（前節）は、次の実験生成
自体を抑制する仕組みとしてそのまま併存する。

`performance_gated_publish` を使わないチャンネル（`ideology` 等）は、従来どおり
`publish.youtube.review.enabled` の有無で `doci.youtube_review.choose_privacy()` が
動く。`enabled=false` なら `publish.youtube.privacy` の静的な値をそのまま使い、
`enabled=true` なら企画の主題適合（対象者・課題・視聴後操作の3点＋主題適合が
すべて明確か）を都度判定して `public`/`unlisted` を決める。

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
- `doci/youtube_review.py` 主題適合の自動判定（テーマガード）
- `doci/gh_cli.py` gh CLIの薄い共有ラッパー（secret除去）
- `doci/topic_ledger.py` 全チャネル共通の日次投稿枠・投稿状態管理（題材の跨ぎ照合はしない）
- `channels/<id>/` チャンネル定義・ペルソナ・声・BGM
- `doci/prompts/output_rules.md` 全チャンネル共通の出力規則
- `doci/run_daily.py` オーケストレータ
