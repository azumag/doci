# 台本執筆モデル比較調査 (2026-08-08)

OpenCode Go ゲートウェイで利用可能なモデルの、doci 台本執筆 (narration) への適性を比較した調査結果。

## 背景

- 台本執筆は従来 `TEXT_BACKEND=codex` (`CODEX_MODEL=gpt-5.6-luna`) を使用
- 台本の質、特に**末尾の一文 (closing_sentence として逐語投稿される)** に「無理矢理感」が
  あるという指摘があり、モデル切替を検討
- 調査の過程で `_run_opencode_go` が Console Go プロバイダ経由モデル (kimi-k3 /
  deepseek-v4-pro / glm-5.2 / mimo-v2.5-pro) と非互換 (Anthropic SSE 変換失敗) であることを
  発見 → OpenAI 互換エンドポイント (`/chat/completions` + Bearer) へ統一 (issue #153)

## 比較方法

- `tools/compare_script_models.py` (作業用・コミット対象外): 既存の生成済み
  script.json (題材・research が記録済み) の入力を再利用し、同じプロンプトで
  各モデルに執筆させる
- 題材: 「会社は『責任のフタ』を持っている? 英国1855年の有限責任法」(ideology)
- 観点: 末尾の一文が本編内容に根ざした自然な問いかけになっているか、本文量

## 結果

| モデル | 末尾の一文 | 本文量 | 備考 |
|---|---|---|---|
| qwen3.7-plus | 「今日、あなたが支払う請求書の裏側には、どんなフタが閉ざされているのでしょうか。」 | 765字 | 問いかけは抽象寄り |
| minimax-m3 | 「もしもその『見えない最後の一人』を、設計図に書き込まなかったとしたら、それは誰にとっての安全装置なのでしょうか。」 | 1365字 | 本編の具体物に根ざす |
| kimi-k3 | 「その下に誰の請求書が眠っているのか、ちょっと想像してみませんか。」 | 823字 | 生成に約130秒 (遅い) |
| **deepseek-v4-pro (採用)** | 「あなたがなにかに投資するとき、その向こう側にいる顔を、ちらりと想像してみたことはありますか。それでは、また。」 | 882字 | 本編内容に根ざす・コスト低 |
| deepseek-v4-flash | 「みなさんは、責任の範囲をどこまで切りますか。」 | 479字 | やや短い |
| glm-5.2 | 「その時、あなたはフタを開けて中を見ますか。それとも、そのままにしておきますか。」 | 933字 | 二択形 |
| mimo-v2.5-pro | (エラー解消後の比較未実施) | — | — |

## 採用

`TEXT_BACKEND=opencode_go` / `OPENCODE_MODEL=opencode-go/deepseek-v4-pro` (.env)
- コストと末尾の一文の質のバランスを優先
- 2026-08-08 時点の実測: 本番プロンプトで882字のnarrationを正常生成

## 実装された修正 (issue #153)

`doci/ai_text.py` の `_run_opencode_go`:
1. エンドポイント: `/messages` (Anthropic互換+x-api-key) → `/chat/completions` (OpenAI互換+Bearer)
2. SSE パース: Anthropic 形式 (text_delta/message_delta) + OpenAI 形式 (choices[].delta.content/finish_reason) の両対応
3. `_strip_think_tags()`: reasoning の `<think>...</think>` を除去 (大文字・属性・未終端・孤立閉じタグ・自己終端形すべて対応)

## メモ

- モデル一覧は `curl -H "x-api-key: $KEY" https://opencode.ai/zen/go/v1/models` で取得可能
- deepseek-v4-flash は短いが安価・高速。用途によっては併用候補
- kimi-k3 は reasoning 消費が大きく生成が遅い (約130秒/回)
