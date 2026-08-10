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
`python -m doci.youtube --auth --analytics --channel <id>` で再認証する。APIやscopeが
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
1つのissueにまとめ、corner毎に次の節を書く:

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

状態は`output/<channel>/performance_experiments.jsonl`に`proposed → applied →
evaluated → reported`（または`expired`）として追記される。全cornerが「新仮説なし
かつ未報告の検証結果なし、かつ`gap_query`付きのコンテンツギャップ動画も無い」なら
issue作成自体をスキップする（無内容issueの防止）。

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

### YouTube終了画面1枠の検証（issue #165）

通常動画の終了画面を「登録・動画・再生リストを同時に並べるだけ」にせず、次の1本に
内容が直結するvideo要素を1枠だけ設定する運用を、ローカルのマニフェストで固定して
記録する（YouTube書込みなし）。Studioのエンゲージメント → 終了画面要素のクリック率で
検証する。記録先は`output/<channel>/end_screen_tests/<experiment-id>/`。

```bash
# 次の1本へ直結する終了画面video要素を1枠だけ計画
python -m doci.end_screen plan \
  --channel youtube-growth --video-id <video-id> --link-video-id <next-video-id> \
  --confirm-content-direct

# Studioで1枠だけ設定した後、runningへ進める
python -m doci.end_screen start \
  --channel youtube-growth --experiment-id <experiment-id> \
  --confirm-studio-setup

# 終了画面要素のクリック率を記録
python -m doci.end_screen complete \
  --channel youtube-growth --experiment-id <experiment-id> \
  --outcome clicked --click-rate 3.5 --confirm-setup-unchanged \
  --notes "次の一本の冒頭が視聴された"
```

`not_clicked`・`insufficient_views`ではクリック率の勝者判定をせず、テスト中に終了
画面の構成を変更した場合は`--outcome stopped_changed_setup`で`invalidated`にする。
クリック率は0〜100の範囲のみ受け付ける。結果は`next_idea_memo.md`へ書き出す。

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
- `doci/end_screen.py` YouTube終了画面1枠の計画・クリック率検証記録（YouTube書込みなし）
- `doci/feedback_issues.py` issueの重複防止・週次レート制御・GitHub I/O基盤
- `doci/tactic_issues.py` 動画が紹介するYouTube運用施策(viewer_action)の検知・issue化（issue #90）
- `channels/<id>/` チャンネル定義・ペルソナ・声・BGM
- `doci/prompts/output_rules.md` 全チャンネル共通の出力規則
- `doci/run_daily.py` オーケストレータ
