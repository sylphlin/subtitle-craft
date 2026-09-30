"""Unit tests for Subtitle Craft core pipeline functions (tests/test_generate_subtitles.py)."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
from generate_subtitles import (
    format_timestamp_srt,
    parse_timestamp_str,
    srt_to_vtt,
    build_srt_from_segments,
    normalize_language_tag,
    load_proofread_template,
    extract_whisper_prompt,
    split_blocks_into_semantic_chunks,
    clean_subtitle_text,
    calc_display_width,
    split_long_clause,
    realign_subtitles_to_words,
    sanitize_subtitle_timings,
    proofread_srt_with_llm,
    audit_subtitles_quality,
)


class TestGenerateSubtitles(unittest.TestCase):

    def test_timestamp_formatting_and_parsing(self):
        self.assertEqual(format_timestamp_srt(0.0), "00:00:00,000")
        self.assertEqual(format_timestamp_srt(65.432), "00:01:05,432")
        self.assertAlmostEqual(parse_timestamp_str("00:01:05,432"), 65.432, places=3)

    def test_srt_to_vtt_conversion(self):
        srt = "1\n00:00:01,000 --> 00:00:03,500\nHello world\n"
        vtt = srt_to_vtt(srt)
        self.assertTrue(vtt.startswith("WEBVTT\n"))
        self.assertIn("00:00:01.000 --> 00:00:03.500", vtt)
        self.assertIn("Hello world", vtt)

    def test_normalize_language_tag(self):
        self.assertEqual(normalize_language_tag("zh-TW"), "zh-TW")
        self.assertEqual(normalize_language_tag("zh_Hant"), "zh-TW")
        self.assertEqual(normalize_language_tag("zh-CN"), "zh-CN")
        self.assertEqual(normalize_language_tag("ja-JP"), "ja")
        self.assertEqual(normalize_language_tag("ko-KR"), "ko")
        self.assertEqual(normalize_language_tag("en-US"), "en")

    def test_load_proofread_template_locales(self):
        for loc in ("zh-TW", "zh-CN", "en", "ja", "ko"):
            content, filename = load_proofread_template(language=loc)
            self.assertEqual(filename, f"subtitle_proofread_template.{loc}.md")
            self.assertGreater(len(content), 100)

    def test_extract_whisper_prompt_primary_and_fallback(self):
        glossary_direct = "> **Whisper Initial Prompt**: 以下為繁體中文對談字幕，專有名詞：Vertex AI、Gemini、FFmpeg。\n"
        self.assertEqual(
            extract_whisper_prompt(glossary_direct, language="zh-TW"),
            "以下為繁體中文對談字幕，專有名詞：Vertex AI、Gemini、FFmpeg。",
        )

        glossary_bold = "1. **Domain Jargon**: **Vertex AI**, **Cloud Storage**, **MLX**\n"
        fallback_en = extract_whisper_prompt(glossary_bold, language="en")
        self.assertIn("Vertex AI", fallback_en)
        self.assertIn("Cloud Storage", fallback_en)

    def test_clean_subtitle_text_cjk_and_latin(self):
        raw_cjk = "**哈囉**，大家好，歡迎收看 AI 101 節目。"
        cleaned_cjk = clean_subtitle_text(raw_cjk, language="zh-TW")
        self.assertEqual(cleaned_cjk, "哈囉 大家好 歡迎收看 AI 101 節目")

        raw_en = "**Hello**, everyone, welcome to the show."
        cleaned_en = clean_subtitle_text(raw_en, language="en")
        self.assertEqual(cleaned_en, "Hello, everyone, welcome to the show.")

    def test_split_long_clause_cjk(self):
        long_line = "今天我們要一起來深入探討雲端人工智慧模型的最新發展趨勢"
        parts = split_long_clause(long_line, max_w=15.0, norm_lang="zh-TW")
        self.assertGreaterEqual(len(parts), 1)
        spaced_line = "今天我們要一起探討 雲端人工智慧模型的最新發展"
        parts_spaced = split_long_clause(spaced_line, max_w=14.0, norm_lang="zh-TW")
        self.assertEqual(len(parts_spaced), 2)
        for p in parts_spaced:
            self.assertLessEqual(calc_display_width(p, "zh-TW"), 14.0)

    def test_split_blocks_into_semantic_chunks(self):
        blocks = []
        t = 0.0
        for i in range(1, 15):
            # Add a larger silence pause after block 7
            gap = 1.2 if i == 8 else 0.05
            t_start = t + gap
            t_end = t_start + 1.5
            t = t_end
            blocks.append(f"{i}\n{format_timestamp_srt(t_start)} --> {format_timestamp_srt(t_end)}\n測試句子{i}。")

        chunks = split_blocks_into_semantic_chunks(blocks, target_chunk_size=7, min_chunk_size=5, max_chunk_size=10)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0]), 7)
        self.assertEqual(len(chunks[1]), 7)

    def test_realign_subtitles_to_words_and_sanitize(self):
        words = [
            {"word": "歡迎", "start": 1.00, "end": 1.40},
            {"word": "收看", "start": 1.40, "end": 1.80},
            {"word": "今天", "start": 1.80, "end": 2.20},
            {"word": "節目", "start": 2.20, "end": 2.60},
            {"word": "謝謝", "start": 3.50, "end": 4.00},
            {"word": "大家", "start": 4.00, "end": 4.60},
        ]
        raw_srt = (
            "1\n00:00:00,500 --> 00:00:03,000\n歡迎收看今天節目\n\n"
            "2\n00:00:03,100 --> 00:00:05,000\n謝謝大家\n"
        )
        realigned, stats = realign_subtitles_to_words(raw_srt, words, language="zh-TW", is_video_start=True)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["locked"], 2)

        sanitized = sanitize_subtitle_timings(realigned, all_words=words, language="zh-TW")
        metrics, _, md_report = audit_subtitles_quality(sanitized, language="zh-TW", alignment_stats=stats)
        self.assertEqual(metrics["total_subtitles"], 2)
        self.assertEqual(metrics["overlaps_count"], 0)
        self.assertEqual(metrics["trailing_punct_violations"], 0)
        self.assertIn("YouTube / Netflix Subtitle Quality Audit Report", md_report)

    @patch("generate_subtitles.call_llm")
    def test_proofread_srt_with_llm_and_chunk_cache(self, mock_call_llm):
        raw_srt = (
            "1\n00:00:01,000 --> 00:00:02,500\n歡迎收看今天的節目。\n\n"
            "2\n00:00:02,600 --> 00:00:04,200\n我們今天來聊雲端運算。\n"
        )
        mock_call_llm.return_value = (
            "```srt\n"
            "1\n00:00:01,000 --> 00:00:02,500\n歡迎收看今天的節目\n\n"
            "2\n00:00:02,600 --> 00:00:04,200\n我們今天來聊雲端運算\n"
            "```"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = os.path.join(tmpdir, ".test_chunk_cache.json")
            out_srt, _ = proofread_srt_with_llm(
                raw_srt=raw_srt,
                cache_path=cache_file,
                language="zh-TW",
            )
            self.assertIn("歡迎收看今天的節目", out_srt)
            self.assertTrue(os.path.exists(cache_file))
            self.assertEqual(mock_call_llm.call_count, 1)

            # Second run should hit cache and not invoke call_llm again
            out_srt_cached, _ = proofread_srt_with_llm(
                raw_srt=raw_srt,
                cache_path=cache_file,
                language="zh-TW",
            )
            self.assertEqual(out_srt_cached, out_srt)
            self.assertEqual(mock_call_llm.call_count, 1)


if __name__ == "__main__":
    unittest.main()
