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
analytics_token = "secrets/ideology/youtube_analytics_token.json"

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
analytics_token = "secrets/sample/youtube_analytics_token.json"
```

主な設定:

| table | keys |
|---|---|
| `channel` | `id`, `name`, `rotation` |
| `corners.<key>` | `label`, `persona`, `corner`, `voice` |
| `pipeline` | `seconds_per_image`, `max_images`, `research`, `factcheck`, `plan`, `asset_media`, `topic_cooldown_days`, `performance_feedback`, `research_requires_youtube_case_studies`, `title_pattern_check`, `narration_opening_guard`, `narration_pattern_check`, `ambiguous_date_title_check`, `plan_topic_retries`, `max_uploads_per_day`, `feedback_repository`, `youtube_auto_playlist`, `youtube_auto_engagement_comment`, `youtube_engagement_comment_mode`, `tactic_issues` |
| `style` | `theme` |
| `style.subtitle` | `font`, `fill`, `stroke`, `box_color`, `box_alpha`, `position_ratio`, `box_radius` |
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

### チャンネル別デザインテーマ(`style.theme`)

字幕・サムネイル・図表の色/フォントだけでは「同じ投稿者」に見えてしまう問題
（issue #76）に対応するため、`style.theme`でレイアウト構造・装飾モチーフごと
切り替えられる名前付きテーマを選べる。既存の`style.subtitle.*`等の個別キーは
テーマの上への上書きとして機能する（優先順位: テーマ既定値 < channel.tomlの
個別キー明示値）。

| テーマ | トーン | 用途の目安 |
|---|---|---|
| `classic`（既定） | 墨地+ゴールド+明朝。額縁・グレイン・星型モチーフの印刷物/アーカイブ調 | 思想史・教養系など、ナレーション主体のコンテンツ |
| `tech` | ネイビー+赤青アクセント+極太ゴシック。フラット矩形のダッシュボード調 | 実務・データ系など、数値・図表主体のコンテンツ |

現行チャンネルの対応: `ideology`=`classic`（思想史コンテンツのアーカイブ調に合わせて明示指定）、
`youtube-growth`=`tech`（実務・データ系のダッシュボード調に合わせて指定）。

新しいテーマの追加は `doci/style_themes.py` の `THEMES` 辞書に `DesignTheme` を
追加する（同モジュールのdocstring参照）。

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
read-only同期し、十分な比較標本がある場合だけ相対的な形式仮説を作る。
retention等のAnalytics指標には、OAuthクライアントのGoogle Cloud projectで
YouTube Analytics APIを有効化したうえで追加scopeが必要。明示的に許可する場合のみ
`python -m doci.youtube --auth --analytics-readonly --channel <id>` で分析専用tokenを
認証する。upload権限を持つ投稿tokenとは別ファイルに保存する。APIやscopeが
未設定でもData API snapshotを残し、指標を推測せず通常生成を継続する。
`python -m doci.performance --sync --channel <id> --corner <key>` でreadbackと判断根拠を
確認できる。仮説は同一corner・同一尺・同一tierの最低8本を比較して1回に1形式だけを提案する。

**この仮説は自動適用されない**（issue #92）。実際の生成プロンプトへ反映する作業は
運用者が手動で行う。かわりに`doci.performance_report`（後述）が3日に1回程度、
チャンネルごとに実績を分析し、調査内容・実験内容・前回提案の効果検証を1つの
GitHub issueにまとめて報告する。`run_daily`の投稿フローは実績フィードバックを
一切参照しない（完全に分離）。

### 実績レポートissue（3日毎、issue #92）

```bash
python -m doci.performance_report --channel youtube-growth   # dry-run（readbackと候補表示のみ）
python -m doci.performance_report --apply                    # 全チャンネルへ実際にissue作成
tools/install_performance_launchd.sh                         # launchdへ登録
```

dry-run（既定）は実績readback（`performance.jsonl`。`--sync`単体実行と同じ副作用）と
issue候補の表示までは行うが、実験状態(`performance_experiments.jsonl`)の記録・
intervalタイマーの更新・GitHub issueの作成は一切行わない。

`pipeline.performance_feedback = true`かつ`pipeline.feedback_repository`
（`owner/repo`）を設定したチャンネルが対象。チャンネル内の全corner分を
1つのissueにまとめ、corner毎に次の節を書く。現在のYouTubeチャンネル
（`youtube-growth`と`ideology`）はいずれも対象:

- **調査内容**: 今回のreadback分析（指標・cohort・対象本数・上位/下位video_id）
- **実験内容**: 新しい単一trait仮説（cooldown中や比較標本不足の場合は「新仮説なし」）
- **前回提案の効果検証**: 前回提案したtraitが、その後投稿された動画の
  `format_traits`に実際に出現したかを自動検知し（`_detect_applied_video`）、
  出現した最初の動画を「適用済み」とみなして`_experiment_result`で事後評価する。
  自動適用をしないため、明示的な予約ではなくこの出現検知で代替している。
  提案から`PERFORMANCE_EXPERIMENT_MAX_AGE_DAYS`（既定30日）以内に出現しなければ
  `expired`として打ち切る。
- **検索発見（Discovery） / 視聴後評価（Satisfaction）**（issue #164）:
  コンテンツギャップ企画（`topic_metadata.gap_query`が記録された動画）を中心に、
  YouTube検索からの視聴回数・構成比と、`gap_query`と実検索語句の一致状態を表示する。
  取得できないトラフィックソース・検索語句・維持率は0や「なし」と断定せず、
  取得不可と明記する。通常動画（`gap_query`未記録）はこの節の評価対象にしない。
  形式仮説が無くても、`gap_query`が記録されたgap動画がsnapshotにあれば
  レポート候補として扱う（検索流入・検索語句だけを持つ通常動画では候補にしない）。
- **チャンネルページ流入割合と次の1本**（issue #122）:
  `performance_feedback`対象の全チャンネル・全cornerで、cornerの最新動画を
  投稿履歴から先に固定する。Data APIの部分応答・削除・一時欠落で履歴上の最新動画を
  確認できない場合は、返った旧動画を最新へ繰り上げない。peerも履歴上の直近IDを
  先に固定し、欠落IDを古い動画へ置換しない。公開日そのものは部分日になるため除外し、
  YouTube Analyticsの
  太平洋時間境界で公開翌日から7日間の`YT_CHANNEL` viewsを、同じ7日間の全viewsで
  割る。traffic-sourceの日次reportで利用可能最終日を先に確認し、最新動画の7日窓が
  完了していなければ期間が揃うまで評価しない。APIが要求終了日を利用可能最終日へ
  黙って短縮した結果を、完了済み7日間として扱わない。
  最新動画の公開時刻・video_id・尺/tier・`YT_CHANNEL`・全viewsのいずれかが
  欠ける場合、または7日間の全viewsが100未満の場合は、欠落を0とせず旧動画へ
  fallbackしない。同一corner・同一尺/tierの直近peerを先に最大5本へ固定し、
  全動画をそれぞれ同じ「公開翌日から7日間」で取得する。peerが3本以上、全peerの
  指標が揃い、各動画の全viewsが100以上の場合だけ中央値と比較する。欠落peerを
  除外して古い有効動画へ置換するselection biasを許さない。
  差が5ポイント以上なら単独でもレポート候補とし、下回る場合はpeer上位動画と
  最新動画の`YT_CHANNEL`詳細と実表示をStudioで手動確認する。流入元channel pageを
  識別できず比較条件を揃えられない場合は変更しない。比較可能な場合だけ、次の
  同一cohortの1本でタイトルまたはサムネイルの一方を変更する。上回る場合は、
  この指標だけを理由にタイトルとサムネイルの組み合わせを変更しない。
  5ポイントは報告対象を絞る検知閾値で、合否や因果を示さない。`YT_CHANNEL`は
  自分または他チャンネルのページで生じたviewsを集計し、既存視聴者・信頼・満足を
  識別しないため、CTR・維持率とは別指標として扱う。専用read-only API呼び出しは
  cornerごとに最新1本と同一cohortの直近peer最大5本へ限定する。動画固有の取得不能・
  不正応答はその固定IDだけを保留し、他cornerの成功結果を保持する。認証・権限・
  quota・リクエスト契約不備は全体失敗として扱う。actionableな最新動画・cohort・
  上下方向・固定peer ID集合はレポートfingerprintへ含め、同じ比較対象は集計値の
  後日補正だけで重複させず、次の最新動画やpeer集合のシグナルは別サイクルとして
  扱う。自動変更は行わない。
- **Shorts冒頭3秒の維持率と削る情報**（issue #127）:
  `performance_feedback`対象の全チャンネル・全cornerで、履歴上のtierが
  `short`または`long_short`の動画だけを対象にする（corner名が`shorts`かどうかでは
  判定しない）。既存の`audienceWatchRatio`維持率カーブから、最初の観測点と
  冒頭3秒以内の最終観測点の累計低下、および最大の隣接点低下を算出する。
  3秒以内の有効点が2点未満、動画長不明、カーブ取得不可は推測せず判定材料不足とする。
  低下が8ポイント以上なら実映像と台本scene（動画長の均等割近似）を照合し、次の
  同じcorner・同じtier・近い尺の1本で、冒頭の理解に不要な情報を1つだけ手動で削る
  施策を示す。他の中心変数は固定し、同じ3秒ウィンドウで比較する。これは観測点間の維持率低下であり、
  スワイプアウト率や離脱人数そのものではない。追加API呼び出し、自動台本変更、
  因果判定は行わない。
- **冒頭30秒の維持率と次の1本**（issue #142）:
  最初の観測点から冒頭30秒内の最終観測点までの累計低下と、隣接観測点間で
  最大の低下区間を算出し、`script.json`のscene（均等割近似）と照合する。
  30秒未満の動画は全長を対象とし、動画長不明・有効点2点未満は推測せず
  判定材料不足とする。累計または最大区間の低下が8ポイント以上なら、同じcorner・
  近い尺/tierの次の1本で冒頭フックだけを変え、同じ指標で比較する手動施策を示す。
  詳細は最新動画を優先し、同時刻なら低下幅が大きい順に最大10本まで表示する。
  8ポイントはレポート対象を絞る検知閾値であり、万能な合格ラインではない。
  dociは編集作業時間を計測せず、台本へ自動適用もしない。運用者が映像・台本と
  照合し、一度に変える中心変数を1つに絞って反映する。
- **サムネの約束・合成入力文と30秒→中盤の傾き**（issue #125）:
  `performance_feedback`対象の全チャンネル・全cornerで、動画長が60秒を超え、
  30秒地点からData API動画長の50%地点までの`audienceWatchRatio`を分析する。
  有効点2点以上に加え、実測端点が対象区間の80%以上を覆い、各端点が対象境界から
  `min(10秒, max(2秒, 動画長の2%))`以内、最大観測間隔が
  `min(30秒, max(5秒, 対象区間の25%))`以内の場合だけ、最初と最後の実測点から
  維持率変化と10秒当たりの傾きを計算する。変換不能・NaN・負値、同一時点の矛盾値、
  疎な点、動画長不明は曲線を判定材料不足とし、8ポイント以上の低下だけを報告する。
  生成時にVOICEVOXへ渡した文と合成WAV上の文区間を`script.json`へ保存する。
  同時に、タイトル短縮後の実描画文字、サムネ描画成功、YouTube API設定成功を
  provenanceとして保存する。これらと冒頭30秒の合成入力文がすべて揃った動画だけを
  #125の変更候補にする。30秒をまたぐ文は時刻を捏造して分割せずその旨を明記し、
  長文は400字の抜粋と表示する。過去動画など証拠が不足する回は参考表示に留め、
  #125単独のissue候補や変更提案には使わない。
  詳細は最新優先・同時刻なら低下幅順の5本まで、合成入力文は先頭の検証対象だけに
  表示する。レポート本文全体も60,000文字以下に制限し、生成文はmention・HTML・
  Markdownリンクとして作用しない形へ無害化する。次の同じcorner・近い尺/tierの1本で、
  「サムネ描画文字（タイトル）」または「冒頭の発話内容」の一方だけを運用者が手動変更し、
  他の中心変数を固定して同じ指標を比較する。サムネ背景画像の意味的一致、低下原因、
  因果は自動判定せず、公開後にYouTube Studioで手動変更したタイトル・サムネイルや
  各画面での実表示も自動追跡しない。追加API呼び出しや自動変更は行わない。
- **制作意図・視聴者コメントと序盤/中盤離脱**（issue #123）:
  `performance_feedback`対象の全チャンネル・全cornerで、cornerの最新動画を先に固定し、
  保存済み`script.json`の`_research.topic`/`angle`と、実測の冒頭3秒・冒頭30秒・
  30秒→50%地点・50%地点までのdipを照合する。最新動画の維持率または制作意図が
  欠ける場合は、コメントが多い旧動画へfallbackせず判定材料不足とする。
  実測低下がある場合、運用者は同じ動画のYouTube Studioでtop-levelコメントを
  新しい順に最大12件確認し、運営自身のコメントを除く。除外後の分類対象が3件未満なら
  判定材料不足として変更しない。3件以上なら「意図と整合」「別の解釈」「判定不能」へ
  手動分類する。コメント本文・分類件数・投稿者情報をdociへ入力・保存せず、Data APIで
  コメントを取得せず、外部LLMにも送らない。割合の分母は本人除外後の有効標本数とし、
  「別の解釈」が2件以上かつ有効標本の50%以上なら、次の1本で制作意図の中心語を
  低下位置までに1度だけ明示する。「意図と整合」が2件以上かつ過半数なら表現を維持し、
  低下位置の説明順またはテンポだけを変更する。どちらにも該当しなければ変更しない。
  選んだ1変数以外を固定して運用者が手動で反映する。コメント投稿者は自己選択された
  一部で、新しい順の限定標本でもあるため、視聴者全体の理解率や離脱原因を代表せず、
  相関を因果と断定しない。
- **維持率カーブの山/谷とシーン照合**（issue #149）:
  Analytics APIの`audienceWatchRatio`（比率。0.9=90%）を
  `elapsedVideoTimeRatio`ディメンションで動画ごとに取得し、前後点より8%ポイント
  （0.08）以上高い点を山（spike）、低い点を谷（dip）として検出する。動画長は
  Data APIの`duration`（ISO 8601）から秒へ変換し、秒位置を算出する。検出した
  時点を`script.json`のscenes（均等割近似）と照合して「どのシーンで維持率が
  上がった/下がったか」をレポートに載せる。山=成功・谷=失敗と断定せず、
  再視聴・巻き戻し・スキップ・離脱の確認は運用者が動画内容と照合して行う。
  API全体の失敗は取得失敗と明記し、Shorts等でカーブが返らない場合と区別する
  （推測で補わない）。
- **維持率カーブの平坦区間の長さ**（issue #117）:
  `performance_feedback`対象の全チャンネル・全cornerで、隣接観測点間の変化量が
  3%ポイント（0.03）以下で連続する区間を平坦とみなし、経過比率の幅が25%以上の
  区間を検出する。最長の平坦区間を動画秒数と`script.json`のscene（均等割近似）に
  照合して表示し、「核心ではない要素」を1つだけ削った前後を同じcorner・近い尺/
  tierの動画群で比較する手動施策の手がかりにする。平坦区間の長さだけで良し悪しを
  判定せず、山=成功・谷=失敗と断定しないのと同じ扱いで、該当箇所の内容照合と
  1本の結果だけで効果を断定しないことを明記する。追加API呼び出し・自動台本変更は
  行わない。生成側はvideo / shorts / analyticsの各cornerプロンプトへ
  「核心ではない要素を一つだけ削り、公開後に平均視聴維持率と平坦区間の長さを
  確認する」ルールを追加する（#127の冒頭3秒の情報削除とは別の編集ルール）。
- **尺×フォーマット別の維持率クロス集計**（issue #115）:
  `performance_feedback`対象の全チャンネル・全cornerで、snapshotの
  `format_traits`（尺`duration:`とtier）の組み合わせごとに、
  `analytics.average_view_percentage`の平均維持率をクロス集計する。同じ
  尺×tierの組み合わせで動画が`MIN_FORMAT_RETENTION_GROUP_SIZE`（3本）以上
  揃い、各動画の維持率が正の有効値（0.0は無データ扱い）の場合だけ表示する
  （判定材料不足・無効な動画は推測で補わない）。
  平均維持率が最も高い組み合わせを「次の1本の仮説候補」として提示するが、
  尺/フォーマット以外の要因（題材・公開条件・企画内容）も混ざるため因果と断定せず、
  同じ視聴者・近い題材で次回1本を試して同じ指標で比較する（反映は運用者が手動で
  行う）。比較対象の組み合わせが2つ以上あるcornerだけ仮説候補行を表示し、
  1つしかないcornerは集計表のみで仮説は提示しない。組み合わせが2つ以上あるcornerが
  ある場合にレポート候補とし、fingerprintにはcornerと最良cohortのみを含めて、
  同じ最良組み合わせは集計値の後日補正だけで重複させない。追加API呼び出し・
  自動台本変更は行わない。
- **中盤の視覚的な進捗指標と離脱確認**（issue #112）:
  `corner_video.md` の生成ルールとして、通常動画の中盤に視覚的な進捗指標
  （残り時間の表示・区切りの見出し・次に説明することの提示）を1つだけ設置し、
  dociの台本スキーマには進捗指標の専用フィールドを設けないため、種別と設置位置
  （おおよその秒位置）の記録は運用者が映像・台本と照合して行う運用を追加する。
  複数の進捗表現を重ねず、視聴者が「今どこにいるか・次に何が来るか」を一つだけ
  掴める構成にする。公開後は視聴者維持率グラフでその設置地点の離脱状況を確認するが、
  山や谷はそれだけで成功・失敗を判定せず（#149のルールと同じ）、該当箇所の内容と
  照合し、1本の結果だけで効果を断定しない。比較は同じcorner・近い尺/tierの
  動画群で進捗指標を置く前後に行い、反映は運用者が手動で行い、一度に変える変数は
  1つ。追加API呼び出し・自動台本変更は行わない。
- **購読状態別の維持率と流入元**（issue #128）:
  各cornerの最新5本までを対象に、Analytics APIの`subscribedStatus` filterで
  `SUBSCRIBED`（購読者）と`UNSUBSCRIBED`（非購読者）の維持率カーブを分離取得する。
  両segmentの`totalSegmentImpressions`が20以上の共通点が5点以上ある場合だけ
  形状を比較し、最大差が8ポイント以上の位置を動画秒数・`script.json`のsceneと
  照合して次の1本の手動仮説候補にする。8ポイントは報告対象を絞る検知閾値で、
  原因や合否を示さない。購読状態は新規視聴者/リピーターとは異なるため読み替えず、
  `traffic_sources`のviewsも別行に表示して維持率カーブへ直接結合しない。取得不可・
  標本不足は推測で補わず、設定へ自動反映しない。
- **共有率と共有される動画の構造**（issue #144）:
  shortsのみを対象に、太平洋時間基準の完了日から遡る30暦日（開始-終了が29日差）
  のAnalytics API `shares`（共有数）÷`views`（再生数）から共有率を算出し、1%を
  超える動画の既存の形式属性（`format_traits`＝tier・尺・scene数・chart有無）を
  次の企画の材料としてレポートに載せる（90日集計の`analytics`とは別に、shorts ID
  だけを`views,shares`のみで取得して`share_30d`へ分離保存する。従来の90日
  `analytics`クエリには`shares`を混ぜない）。再生数だけの評価を避けるための
  補助指標であり、共有率1%超かつ構造が記録された動画がsnapshotにあれば形式仮説が
  無くてもレポート候補として扱う。表示は構造付きを最優先しつつ先頭20件まで、
  構造未記録の1%超・取得不可は件数要約に留め、共有率1%超動画が構造の有無を
  問わず1本もない場合に限り、1%以下の動画を最大5本まで参考表示する。
  `share_30d`の`start_date/end_date`はAPIへの要求期間であり、実データが
  揃っている期間（利用可能最終日）とは必ずしも一致しない。

状態は`output/<channel>/performance_experiments.jsonl`に`proposed → applied →
evaluated → reported`（または`expired`）として追記される。全cornerが「新仮説なし
かつ未報告の検証結果なし」で、gap動画、Shorts冒頭3秒低下、冒頭30秒低下、
30秒→中盤低下、維持率の山/谷、チャンネルページ流入割合の差、
対象となる共有率シグナルも無いなら
issue作成自体をスキップする
（無内容issueの防止）。

起動間隔は`StartInterval=86400`（毎日）でlaunchdジョブを登録し、Python側の
`PERFORMANCE_REPORT_MIN_INTERVAL_HOURS`（既定72時間）ゲートで実質「3日に1回程度」
に保つ。`StartInterval`を3日(259200秒)に直接設定しない理由は、スリープ/再起動で
launchdのタイマーがリセットされ、間隔が長いほど実行そのものが遅延・脱落しやすい
ため。毎日起動＋ソフトゲートの方が確実に間隔を守れる。issueのレート制御・
重複防止・週次上限は`doci.feedback_issues`（チャンネル単位で週
`FEEDBACK_ISSUES_MAX_PER_WEEK`件、既定3件）がそのまま担う。

### YouTube Studioのタイトル・サムネイルA/Bテスト（issue #151）

通常動画のネイティブA/BテストはYouTube Studio上で人が開始する。dociはStudioを
自動操作せず、登録する2〜3案を不変のマニフェストへ固定し、テスト開始と結果だけを
`output/<channel>/youtube_ab_tests/<experiment-id>/`へ記録する。対象はdoci履歴で
`published`、`tier=longform`、公開設定が`public`または`unlisted`と確認できる動画に
限定する。Shorts・private動画・履歴外動画はfail-closedで拒否する。

```bash
# タイトルだけを2〜3案で比較する計画を作成
python -m doci.youtube_ab_test plan \
  --channel youtube-growth --video-id <video-id> --mode title \
  --title "案A" --title "案B" --title "案C" \
  --confirm-studio-eligible

# サムネイルだけなら --mode thumbnail と --thumbnail を2〜3回指定。
# 両方なら --mode both とし、同じ順序・同じ数の --title / --thumbnail を指定。

# plan.mdと同じ案をパソコン版Studioへ登録した後だけrunningへ進める
python -m doci.youtube_ab_test start \
  --channel youtube-growth --experiment-id <experiment-id> \
  --confirm-studio-started

# Studioの総再生時間シェアによる結果を記録
python -m doci.youtube_ab_test complete \
  --channel youtube-growth --experiment-id <experiment-id> \
  --outcome winner --winner B --confirm-no-manual-change \
  --notes "次回企画で検証する仮説"
```

結果は`manifest.json`と`next_idea_memo.md`へ保存する。`performed_same`と
`inconclusive`では勝者を記録せず、テスト中にタイトルまたはサムネイルを手動変更した
場合は`--outcome stopped_manual_change`で`invalidated`にする。1動画に対する
`planned`/`running`テストは同時に1件だけ許可し、結果を別動画へ自動適用しない。
YouTube公式仕様どおり、判定指標はクリック率ではなく総再生時間シェアとして記録する。
通常結果の記録には`--confirm-no-manual-change`が必須。計画全体のチェックサムと
コピー済みサムネイルのSHA-256を`start`時に再検証し、計画後に案が変わっていれば
Studioでの開始記録を拒否する。

### YouTube終了画面の比較実験（issues #165/#171）

通常動画の終了画面について、内容が直結するvideo要素1枠を普遍的な正解にせず、
`single_related_video`と`multi_element_baseline`を比較variantとしてローカルへ記録する
（YouTube書込みなし）。全チャネル・全コーナーで利用できるが、対象と遷移先は同じ
チャネル履歴にある`published`、`public`/`unlisted`の通常動画に限る。記録先は
`output/<channel>/end_screen_tests/<experiment-id>/`。

`comparison_key`には近い題材・尺を表すcohort名を付け、観測期間は太平洋時間の完了日
7日または28日に固定する。各variantが同じcorner・comparison_key・観測期間で異なる
元動画2本以上`observed`になるまで、`summary`は比較材料不足とする。同じ元動画の再実験は
別の1本として数えない。対象要素クリック率の中央値は記述統計として表示するだけで、
勝者や因果を決めない。

比較時はtargetと追加要素のtype・selection・timing・positionから`setup_signature`を作る。
実際のvideo ID、playlist ID、channel ID、URLは各manifestへ保存する一方、signatureでは
content-specific参照として正規化する。同じvariant内に複数signatureが混在したgroupは
`incompatible_setup_profiles`として保留し、異なる構成のクリック率を一つの中央値へ混ぜない。
さらにsingle/multi間でtarget要素のtype・selection・timing・positionが一致しなければ
`incompatible_cross_variant_target_profile`として保留し、追加要素以外の差を混同しない。

```bash
# 内容直結のvideo要素1枠を、比較実験の1variantとして計画
python -m doci.end_screen plan \
  --channel youtube-growth --video-id <video-id> --link-video-id <next-video-id> \
  --variant single_related_video --comparison-key "同ジャンル-8分級" \
  --target-timing last_20_seconds_to_end --target-position center \
  --observation-days 7 --confirm-content-direct

# 複数要素baselineは選択方式・参照先・タイミング・位置まで記録
python -m doci.end_screen plan \
  --channel youtube-growth --video-id <video-id> --link-video-id <next-video-id> \
  --variant multi_element_baseline \
  --target-timing last_20_seconds_to_end --target-position center \
  --extra-element '{"type":"subscribe","selection":"current_channel","reference":null,"timing":"last_20_seconds_to_end","position":"bottom_right"}' \
  --extra-element '{"type":"playlist","selection":"specific","reference":"<playlist-id>","timing":"last_20_seconds_to_end","position":"top_left"}' \
  --comparison-key "同ジャンル-8分級" \
  --observation-days 7 --confirm-content-direct

# Studioで計画variantを設定した後、runningへ進める
python -m doci.end_screen start \
  --channel youtube-growth --experiment-id <experiment-id> \
  --confirm-studio-setup

# 観測期間後、対象video要素のクリック率と遷移先の終了画面流入視聴を記録
python -m doci.end_screen complete \
  --channel youtube-growth --experiment-id <experiment-id> \
  --sample-sufficient --click-rate 3.5 --end-screen-traffic-views 12 \
  --confirm-period-data-complete --confirm-setup-unchanged

# 同条件の記述統計（勝者判定なし）
python -m doci.end_screen summary --channel youtube-growth
```

クリック率は指定したtarget video要素の操作指標、`end-screen-traffic-views`は遷移先動画に
入った全終了画面トラフィックの視聴数であり、元動画だけへのsource別帰属とはみなさない。
同じ遷移先・同じ期間の総流入は一つの文脈観測として扱い、値の食い違いを拒否し、variant別
中央値には使わない。
公式API文書では`END_SCREEN`のsource detail可否に不整合があるため、dociはsource別の値を
推測しない。取得不能時は`--insufficient-views --insufficient-reason analytics_unavailable`
と、取得できないKPIだけを`--missing-metric click_rate`または
`--missing-metric end_screen_traffic_views`で指定する。取得済みKPIは残し、両方取得不能なら
両方を指定して`null`にする。低母数時は両KPIを記録したうえで`--insufficient-reason
low_views`として結論を保留する。テスト中に構成を変えた場合は`--setup-changed`で
`invalidated`にする。schema v1の既存記録は引き続き読み取りと完了ができるが、新規計画は
schema v2だけを作成する。結果は`next_idea_memo.md`へ書き出す。

### Shortsから関連動画への橋渡し検証（issue #138）

特定のチャネルや「アナリティクス」コーナー専用ではなく、dociの全チャネル・全コーナー
に共通する手動検証として扱う。doci履歴で`published`かつ`public`/`unlisted`と確認できる
元Shortと、同じチャネル履歴にある内容直結の通常動画（`tier=longform`）を固定する。
橋渡し文は元Shortの
`script.json`を空白正規化した本文の最終3分の1に開始位置があり、実在しなければならない。
位置と本文SHA-256も計画へ固定する。YouTube Studioの関連動画設定は人が行い、dociから
YouTubeへの書込みはしない。Analytics照会には`youtube.readonly`と
`yt-analytics.readonly`だけを要求し、`youtube.upload`は要求しない。記録先は
`output/<channel>/shorts_bridge_tests/<experiment-id>/`。

```bash
# Analyticsだけを使うtokenを新規作成する場合（upload権限なし）
python -m doci.youtube --auth --analytics-readonly --channel youtube-growth

# 公開済みShortと、内容が直結する次の動画を固定
python -m doci.shorts_bridge plan \
  --channel youtube-growth --source-video-id <short-id> \
  --target-video-id <next-video-id> \
  --bridge-text "続きは関連動画で確認してください" \
  --observation-days 7 --confirm-content-direct

# Studioで元Shortの関連動画を手動設定した後、次の太平洋時間の完了日から観測
python -m doci.shorts_bridge start \
  --channel youtube-growth --experiment-id <experiment-id> \
  --confirm-studio-setup

# 観測期間終了後、設定が変わっていない場合だけAnalyticsをread-only取得
python -m doci.shorts_bridge complete \
  --channel youtube-growth --experiment-id <experiment-id> \
  --confirm-setup-unchanged --notes "次の比較で変える要素"

# 同じsource corner・source/target tier・観測日数の観測だけを比較
python -m doci.shorts_bridge summary --channel youtube-growth
```

同一観測期間の元Shortの`views`と、遷移先動画の`RELATED_VIDEO`上位25参照元のうち
元ShortのIDと一致した`views`を記録する。ただし公式資料はShortsの「関連動画」リンクが
Analyticsの`RELATED_VIDEO`へ必ず分類されるとは明記していない。参照元行が無い場合は
0件とせず`insufficient_data`にする。この比率はクリック率ではなく、5%などの万能な
合格ラインは適用しない。同じ条件の有効な観測が3件以上揃った場合だけmedianを参考表示し、
因果・勝者・次施策を自動決定しない。観測中に橋渡し文または関連動画設定を変えた場合は
`complete --setup-changed`で`invalidated`にする。`complete`時は先に`day`次元で`views`の
利用可能最終日を、最新の完了PT日まで広げて確認する。観測終了日以降の行があれば期間確定と
みなし、届いていなければ実験を`running`のまま残して同じコマンドを再実行する。観測終了後
7完了日を待っても行が無い場合は、無再生を0と推測せず`insufficient_data`（取得不可）で
終了し、比較対象に含めない。要求期間を実取得期間として推測保存しない。

### コメントステッカー返信Shortの検証（issue #105）

視聴者の質問・要望コメントを次のShortへ変える施策は、特定チャネルやshortsコーナーの
出典に限定せず、dociに定義された全チャネル・全コーナーで使える手動実験として扱う。
コメント選択、ステッカー付与、Short作成、公開はYouTubeアプリで人が行い、dociから
YouTubeへ書き込まない。アプリ投稿はdoci履歴に無いため、開始時にData APIをread-onlyで
照会し、返信Short、コメント元動画、比較baselineが認証中の同一チャンネルにあることを
確認する。APIだけではShorts分類や内容の同系統性を確定できないため、180秒以内の動画で
あることに加え、運用者の明示確認を必須にする。記録先は
`output/<channel>/comment_reply_short_tests/<experiment-id>/`。

```bash
# 投稿tokenを置換せず、2つのread-only scopeだけを持つ分析tokenを作る
python -m doci.youtube --auth --analytics-readonly --channel youtube-growth

# 質問・要望の要約だけを計画へ固定する。投稿者名とコメント原文は保存しない
python -m doci.comment_reply_short plan \
  --channel youtube-growth \
  --source-video-id <commented-video-id> \
  --source-comment-id <comment-id> \
  --request-summary "視聴者が知りたい内容の安全な要約" \
  --reply-corner shorts --comparison-key "同じ題材・回答形式" \
  --observation-days 7 --confirm-question-or-request

# YouTubeアプリでコメントステッカー付きShortを公開した後に開始する
# baselineは返信Shortより前に公開した直近同系統Shortを1〜5本明示する
python -m doci.comment_reply_short start \
  --channel youtube-growth --experiment-id <experiment-id> \
  --reply-video-id <reply-short-id> \
  --baseline-video-id <baseline-1> \
  --baseline-video-id <baseline-2> \
  --baseline-video-id <baseline-3> \
  --confirm-comment-sticker --confirm-youtube-app-published \
  --confirm-recent-same-type

# 返信Shortの観測期間終了後にread-only取得する
python -m doci.comment_reply_short complete \
  --channel youtube-growth --experiment-id <experiment-id> \
  --confirm-setup-unchanged --notes "次の1本で変える中心変数"

# 同じcorner・comparison key・観測日数の実験を記述集計する
python -m doci.comment_reply_short summary --channel youtube-growth
```

各動画について、公開日の次の太平洋時間完了日から同じ日数の`views`、`comments`、
`subscribersGained`、`subscribersLost`を取得する。登録者増減は動画dimension/filterを
使うため、指定動画のwatch pageへ帰属した値であり、チャンネル全体の登録者増減ではない。
生のコメント数・登録者純増減に加え、再生数差を確認するため1,000再生当たりの参考値も
保存する。比較baselineの有効指標が3本未満ならmedianと差分を表示せず、3本以上でも
勝者・因果・万能な合格ラインは決めない。

`complete`は同じメトリクス群の日次channel reportで利用可能最終日を先に確認する。
返信Shortの観測終了日まで届いていなければ`running`のまま再試行し、終了後7完了日を
待っても確認できなければ0にせず`insufficient_data`で閉じる。指標列や動画行が欠落した
場合も`null`と理由を残す。投稿や比較条件を途中で変えた場合は
`complete --setup-changed`で`invalidated`にする。Analytics専用tokenには
`youtube.readonly`と`yt-analytics.readonly`だけを使い、`youtube.upload`を要求しない。
このtokenは`publish.youtube.analytics_token`へ保存し、投稿用の
`publish.youtube.token`を読み書きしない。保存scopeが2つと完全一致しないtokenは拒否する。

### YouTube攻略Ch の公開判定

`youtube-growth` は `max_uploads_per_day = 1` とし、JSTで1日1本だけ実投稿する。
GitHub Issueでの人手承認・ラベル待ち・reconcileの仕組みは廃止した。人手ラベルや
限定公開からの経過時間は公開可否に一切関与しない。実績フィードバックとも無関係で、
`publish.youtube.privacy = "unlisted"` の静的な値をそのまま使う。

`publish.youtube.review.enabled = true` を設定したチャンネルは、
`doci.youtube_review.choose_privacy()` が企画の主題適合（対象者・課題・視聴後操作の
3点＋主題適合がすべて明確か）を都度判定して `public`/`unlisted` を決める。
`enabled`未設定/`false`のチャンネル（`ideology`等）は、この判定を経由せず
`publish.youtube.privacy`の静的な値（`ideology`は`public`固定）をそのまま使う。

shortsコーナーは全台本で、narrationに情報を留める間（休止表現）が3箇所あることを
生成ルールと自動公開判定の両方で要求する（issue #150）。3箇所に満たない台本は
`unlisted`のままにして、維持率グラフの離脱急落点を後ろへずらす仮説を未検証の状態で
公開しない。

### エンゲージメントコメント（issue #86、チャンネル別方式は issue #98）

`pipeline.youtube_auto_engagement_comment = true` のチャンネルは、公開直後の
動画へ運営者本人としてコメントを1つ自動投稿する。`pipeline.youtube_engagement_comment_mode`
（既定`"debate"`）でチャンネルごとに方式を選べる:

| mode | 動作 | 想定チャンネル |
|---|---|---|
| `debate`（既定） | 議論を誘発する一言をLLM生成する | 汎用 |
| `closing_sentence` | narration末尾の一文をLLMを呼ばずそのまま投稿する（疑問形かは問わない）。末尾の一文を抽出できない回は投稿しない（`debate`へはフォールバックしない） | narrationの締めを本編の言葉のまま届けたいチャンネル（`ideology`等） |
| `call_to_action` | 討論誘発ではなく、視聴者が今日すぐ試せる1手を促す実用的なコメントをLLM生成する。`viewer_action`が空ならnarration末尾一文を代わりに使い、どちらも取れなければ投稿しない（`debate`へはフォールバックしない） | 不特定多数の議論が成立しないチャンネル（`youtube-growth`等、全動画がunlistedのため） |

固定（ピン留め）はYouTube Data APIに無いため、投稿後に固定したい場合は
YouTube Studioで手動操作する。

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

実績レポートissue（前節）は別ジョブ `com.azumag.doci.performance` として
`tools/install_performance_launchd.sh` で登録する（動画生成の投稿フローとは
完全に独立しており、`tools/install_launchd.sh`とは別に実行が必要）。

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
- `doci/performance.py` 実績readbackと形式仮説の生成（自動適用はしない）
- `doci/performance_report.py` 3日毎の実績レポートissueサイクル（issue #92、`run_daily`とは独立）
- `doci/youtube_ab_test.py` YouTube Studioの通常動画A/Bテスト計画・結果記録（YouTube書込みなし）
- `doci/end_screen.py` YouTube終了画面variantの比較・二段階KPI記録（YouTube書込みなし）
- `doci/shorts_bridge.py` Shorts関連動画への橋渡し計画・read-only検証記録（YouTube書込みなし）
- `doci/feedback_issues.py` issueの重複防止・週次レート制御・GitHub I/O基盤
- `doci/tactic_issues.py` 動画が紹介するYouTube運用施策(viewer_action)の検知・issue化（issue #90）
- `channels/<id>/` チャンネル定義・ペルソナ・声・BGM
- `doci/prompts/output_rules.md` 全チャンネル共通の出力規則
- `doci/run_daily.py` オーケストレータ
