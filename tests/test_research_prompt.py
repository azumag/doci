from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from doci import config, research
from doci.channel import CornerSpec


class ResearchPromptTest(unittest.TestCase):
    def test_prompt_includes_channel_guidance_and_primary_source_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            persona = root / "persona.md"
            corner_prompt = root / "corner.md"
            persona.write_text("裏技を断定しない人格", encoding="utf-8")
            corner_prompt.write_text("公式YouTube資料を優先", encoding="utf-8")
            corner = CornerSpec(
                key="shorts",
                label="ショート攻略",
                persona_path=persona,
                corner_path=corner_prompt,
                voice_key="narrator",
            )
            raw = json.dumps(
                {
                    "topic": "題材",
                    "angle": "切り口",
                    "facts": [
                        {
                            "claim": "検証済みの事実",
                            "source_url": "https://support.google.com/youtube/example",
                            "source_title": "YouTube Help",
                        },
                        {
                            "claim": "公式ブログの事実",
                            "source_url": "https://blog.youtube/news/example",
                            "source_title": "YouTube Blog",
                        },
                        {
                            "claim": "別の公式ヘルプの事実",
                            "source_url": "https://support.google.com/youtube/answer/123",
                            "source_title": "YouTube Help",
                        },
                        {
                            "claim": "SEO記事の主張",
                            "source_url": "https://example.com/seo",
                            "source_title": "非公式記事",
                        },
                    ],
                    "examples": [
                        {
                            "title": "伸びたショートの例",
                            "channel": "参考チャンネル",
                            "url": "https://www.youtube.com/watch?v=example",
                            "published_at": "2026-07-01",
                            "observed": "冒頭3秒で改善前後の映像を並べ、結果を先に見せている",
                        },
                        {
                            "title": "分析方法の例",
                            "channel": "動画運営ラボ",
                            "url": "https://youtu.be/second-example",
                            "observed": "視聴者維持率の画面を示した後、改善手順を三段階で説明している",
                        },
                        {
                            "title": "別サイトの例",
                            "url": "https://example.com/video",
                            "observed": "これは除外される",
                        },
                    ],
                },
                ensure_ascii=False,
            )
            with (
                mock.patch.object(config, "RESEARCH_BACKEND", "claude"),
                mock.patch.object(config, "SCRIPT_RESEARCH_RETRIES", 1),
                mock.patch.object(
                    research.llm, "run_claude", return_value=raw
                ) as run_mock,
            ):
                result = research.web_research(
                    corner,
                    [],
                    performance_guidance="decision abc: retention形式を1変数だけ試す",
                )

        prompt = run_mock.call_args.args[0]
        self.assertIn("裏技を断定しない人格", prompt)
        self.assertIn("公式YouTube資料を優先", prompt)
        self.assertIn("一次資料を最優先", prompt)
        self.assertIn("数値閾値", prompt)
        self.assertIn("YouTubeの伸ばし方", prompt)
        self.assertIn("動画を2〜3本", prompt)
        self.assertIn("主張の共通点", prompt)
        self.assertIn("因果を断定しない", prompt)
        self.assertIn("decision abc", prompt)
        self.assertEqual(result["topic"], "題材")
        self.assertEqual(len(result["facts"]), 3)
        self.assertEqual(len(result["examples"]), 2)

        brief = research.brief_for_prompt(result)
        self.assertIn("公開YouTube動画の比較事例", brief)
        self.assertIn("伸びたショートの例", brief)
        self.assertIn("成功原因の証明ではない", brief)

    def test_non_youtube_channel_does_not_request_video_examples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            persona = root / "persona.md"
            corner_prompt = root / "corner.md"
            persona.write_text("歴史解説者", encoding="utf-8")
            corner_prompt.write_text("一次史料を優先", encoding="utf-8")
            corner = CornerSpec(
                key="history",
                label="歴史",
                persona_path=persona,
                corner_path=corner_prompt,
                voice_key="narrator",
            )
            raw = json.dumps(
                {
                    "topic": "題材",
                    "facts": [
                        {
                            "claim": "検証済みの事実",
                            "source_url": "https://example.org/primary",
                        }
                    ],
                },
                ensure_ascii=False,
            )
            with (
                mock.patch.object(config, "RESEARCH_BACKEND", "claude"),
                mock.patch.object(config, "SCRIPT_RESEARCH_RETRIES", 1),
                mock.patch.object(research.llm, "run_claude", return_value=raw) as run_mock,
            ):
                result = research.web_research(corner, [])

        self.assertNotIn("YouTubeの伸ばし方", run_mock.call_args.args[0])
        self.assertEqual(result["examples"], [])


if __name__ == "__main__":
    unittest.main()
