"""Unit tests for Subtitle Craft core pipeline functions (tests/test_generate_subtitles.py)."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "subtitle-craft", "scripts"),
)
from generate_subtitles import (
    resolve_output_dir,
    format_timestamp_srt,
    parse_timestamp_str,
    srt_to_vtt,
    build_srt_from_segments,
    normalize_language_tag,
    load_proofread_template,
    extract_whisper_prompt,
    extract_global_glossary,
    split_blocks_into_semantic_chunks,
    normalize_text_for_alignment,
    align_split_clauses_with_words,
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

    def test_resolve_output_dir_isolation_and_no_nesting(self):
        self.assertEqual(
            resolve_output_dir("/tmp/project/video.mp4"),
            "/tmp/project/output",
        )
        self.assertEqual(
            resolve_output_dir("/tmp/project/output/video.mp4"),
            "/tmp/project/output",
        )
        self.assertEqual(
            resolve_output_dir("/tmp/project/video.mp4", "/tmp/custom_out"),
            "/tmp/custom_out",
        )

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

        # Preserve thousand separators, decimals, %, and $ with clean CJK spacing
        raw_num = "今年營收成長25.8%，投入$1,000購買設備。"
        cleaned_num = clean_subtitle_text(raw_num, language="zh-TW")
        self.assertEqual(cleaned_num, "今年營收成長 25.8% 投入 $1,000 購買設備")

        raw_en = "**Hello**, everyone, welcome to the show."
        cleaned_en = clean_subtitle_text(raw_en, language="en")
        self.assertEqual(cleaned_en, "Hello, everyone, welcome to the show.")

    def test_normalize_text_for_alignment_multilingual_numbers_and_symbols(self):
        # Chinese percentages, decimals, years, currencies, and thousand separators
        self.assertEqual(
            normalize_text_for_alignment("成長了百分之二十五點八"),
            normalize_text_for_alignment("成長了 25.8%"),
        )
        self.assertEqual(
            normalize_text_for_alignment("Gemini 三點八"),
            normalize_text_for_alignment("Gemini 3.8"),
        )
        self.assertEqual(
            normalize_text_for_alignment("兩千零二十六年"),
            normalize_text_for_alignment("2026 年"),
        )
        self.assertEqual(
            normalize_text_for_alignment("花了 100 美金"),
            normalize_text_for_alignment("花了 $100"),
        )
        self.assertEqual(
            normalize_text_for_alignment("預算 1,000 萬"),
            normalize_text_for_alignment("預算一千萬"),
        )
        # Japanese
        self.assertEqual(
            normalize_text_for_alignment("三点五パーセント"),
            normalize_text_for_alignment("3.5%"),
        )
        # Korean
        self.assertEqual(
            normalize_text_for_alignment("삼점팔 퍼센트"),
            normalize_text_for_alignment("3.8%"),
        )
        self.assertEqual(
            normalize_text_for_alignment("이십오 프로"),
            normalize_text_for_alignment("25%"),
        )
        # English
        self.assertEqual(
            normalize_text_for_alignment("twenty-five percent"),
            normalize_text_for_alignment("25%"),
        )
        self.assertEqual(
            normalize_text_for_alignment("three point eight"),
            normalize_text_for_alignment("3.8"),
        )

    def test_split_long_clause_cjk(self):
        # Tier 3: Unspaced 27-char CJK line is partitioned into 2 balanced clauses <= 15.0 chars
        long_line = "今天我們要一起來深入探討雲端人工智慧模型的最新發展趨勢"
        parts = split_long_clause(long_line, max_w=15.0, norm_lang="zh-TW")
        self.assertEqual(len(parts), 2)
        for p in parts:
            self.assertLessEqual(calc_display_width(p, "zh-TW"), 15.0)
            self.assertGreaterEqual(calc_display_width(p, "zh-TW"), 10.0)

        # Tier 1: Avoid greedy 14 + 4 orphan tail in favor of balanced 8 + 10 split
        orphan_prone = "今天我們一起探討 雲端人工智慧 發展趨勢"
        parts_balanced = split_long_clause(orphan_prone, max_w=15.0, norm_lang="zh-TW")
        self.assertEqual(parts_balanced, ["今天我們一起探討", "雲端人工智慧 發展趨勢"])

        spaced_line = "今天我們要一起探討 雲端人工智慧模型的最新發展"
        parts_spaced = split_long_clause(spaced_line, max_w=14.0, norm_lang="zh-TW")
        self.assertEqual(len(parts_spaced), 2)
        for p in parts_spaced:
            self.assertLessEqual(calc_display_width(p, "zh-TW"), 14.0)

    def test_clamp_inflated_post_silence_word_onset(self):
        # Whisper inflated the first word "歡迎" across 2.3s of opening silence (0.10s..2.40s)
        words = [
            {"word": "歡迎", "start": 0.10, "end": 2.40},
            {"word": "來到", "start": 2.40, "end": 2.80},
            {"word": "現場", "start": 2.80, "end": 3.20},
        ]
        raw_srt = "1\n00:00:00,100 --> 00:00:03,200\n歡迎來到現場\n"
        realigned, stats = realign_subtitles_to_words(raw_srt, words, language="zh-TW", is_video_start=True)
        self.assertEqual(stats["locked"], 1)
        ts_line = realigned.strip().splitlines()[1]
        s_sec, e_sec = [parse_timestamp_str(x) for x in ts_line.split("-->")]
        # Clamped onset (2.40 - 0.75 = 1.65s) minus 0.18s lead-in = 1.47s (never 0.00s!)
        self.assertGreaterEqual(s_sec, 1.40)
        self.assertAlmostEqual(e_sec, 3.20, places=2)

    def test_sanitize_subtitle_timings_cps_aware_adaptive_tail(self):
        # 12 CJK chars spoken rapidly in 1.10s (1.00s..2.10s), followed by a 2.50s silence gap before line 2 (4.60s)
        fast_srt = (
            "1\n00:00:01,000 --> 00:00:02,100\n今天我們一起深入探討雲端\n\n"
            "2\n00:00:04,600 --> 00:00:06,200\n謝謝大家收看\n"
        )
        sanitized = sanitize_subtitle_timings(fast_srt, language="zh-TW", media_duration=6.50)
        blocks = [b.strip() for b in sanitized.strip().split("\n\n") if b.strip()]
        l1_s, l1_e = [parse_timestamp_str(x) for x in blocks[0].splitlines()[1].split("-->")]
        l1_dur = l1_e - l1_s
        # Instead of fixed +0.4s (1.50s -> 8.0 CPS), adaptive tail extends up to ~2.18s (<= 5.6 CPS)
        self.assertGreaterEqual(l1_dur, 2.05)
        self.assertLessEqual(12.0 / l1_dur, 6.0)

    def test_align_split_clauses_with_words_fuzzy_typo_and_pause(self):
        # Whisper had a homophone typo ("探逃" instead of "探討") and a 0.5s pause at 2.20s..2.70s
        words = [
            {"word": "今天", "start": 1.00, "end": 1.40},
            {"word": "我們", "start": 1.40, "end": 1.80},
            {"word": "探逃", "start": 1.80, "end": 2.20},
            {"word": "雲端", "start": 2.70, "end": 3.10},
            {"word": "架構", "start": 3.10, "end": 3.60},
        ]
        parts = ["今天我們探討", "雲端架構"]
        aligned = align_split_clauses_with_words(parts, 1.00, 3.60, all_words=words)
        self.assertEqual(len(aligned), 2)
        self.assertAlmostEqual(aligned[0]["end"], 2.20, places=2)
        self.assertAlmostEqual(aligned[1]["start"], 2.70, places=2)

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

        sanitized = sanitize_subtitle_timings(realigned, all_words=words, language="zh-TW", media_duration=4.70)
        metrics, _, md_report = audit_subtitles_quality(
            sanitized, language="zh-TW", alignment_stats=stats, media_duration=4.70
        )
        self.assertEqual(metrics["total_subtitles"], 2)
        self.assertEqual(metrics["overlaps_count"], 0)
        self.assertEqual(metrics["trailing_punct_violations"], 0)
        self.assertTrue(metrics["agent_verdict"]["pass_quality_gate"])
        self.assertEqual(metrics["agent_verdict"]["suggested_action"], "DELIVER")
        self.assertIn("YouTube / Netflix Subtitle Quality Audit Report", md_report)

    def test_realign_spoken_numbers_and_global_milestone_recovery(self):
        # Whisper has spoken numbers ("百分之", "二十五點八") AND a 50-char unkept filler block in the middle
        words = [
            {"word": "業績", "start": 1.00, "end": 1.40},
            {"word": "成長", "start": 1.40, "end": 1.80},
            {"word": "百分之", "start": 1.80, "end": 2.20},
            {"word": "二十五點八", "start": 2.20, "end": 2.90},
            {"word": "那那那其實這裡有一段超過五十個字元的無意義冗長口吃與重複廢話測試片段完全被校對刪除掉的內容再加一些廢話湊滿長度", "start": 3.00, "end": 6.50},
            {"word": "最後", "start": 7.00, "end": 7.40},
            {"word": "圓滿", "start": 7.40, "end": 7.80},
            {"word": "成功", "start": 7.80, "end": 8.30},
        ]
        raw_srt = (
            "1\n00:00:01,000 --> 00:00:03,000\n業績成長 25.8%\n\n"
            "2\n00:00:06,800 --> 00:00:08,500\n最後圓滿成功\n"
        )
        realigned, stats = realign_subtitles_to_words(raw_srt, words, language="zh-TW", is_video_start=True)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["locked"], 2)
        blocks = [b.strip() for b in realigned.strip().split("\n\n") if b.strip()]
        l1_s, l1_e = [parse_timestamp_str(x) for x in blocks[0].splitlines()[1].split("-->")]
        l2_s, l2_e = [parse_timestamp_str(x) for x in blocks[1].splitlines()[1].split("-->")]
        self.assertAlmostEqual(l1_e, 2.90, places=2)
        self.assertAlmostEqual(l2_s, 6.82, places=2)
        self.assertAlmostEqual(l2_e, 8.30, places=2)

    def test_realign_non_cascading_fallback_recovery_and_source_bounds(self):
        words = [
            {"word": "歡迎", "start": 1.00, "end": 1.40},
            {"word": "收看", "start": 1.40, "end": 1.80},
            {"word": "今天", "start": 1.80, "end": 2.20},
            {"word": "節目", "start": 2.20, "end": 2.60},
            {"word": "我們", "start": 2.70, "end": 3.00},
            {"word": "馬上", "start": 3.00, "end": 3.40},
            {"word": "開始", "start": 3.40, "end": 3.80},
            {"word": "謝謝", "start": 4.00, "end": 4.50},
            {"word": "大家", "start": 4.50, "end": 5.00},
        ]
        # Line 2 is completely mismatched with an inflated fallback timestamp (up to 9.0s),
        # but BBC Pass 2 interpolates it onto the unconsumed Whisper words (2.70s..3.80s),
        # and Line 3 ("謝謝大家") locks to 4.00s..5.00s without cascading!
        raw_srt = (
            "1\n00:00:01,000 --> 00:00:02,600\n歡迎收看今天節目\n\n"
            "2\n00:00:02,700 --> 00:00:09,000\n完全不相關的測試語句\n\n"
            "3\n00:00:04,000 --> 00:00:05,000\n謝謝大家\n"
        )
        realigned, stats = realign_subtitles_to_words(
            raw_srt,
            words,
            language="zh-TW",
            is_video_start=True,
            source_bounds=(1.00, 5.00),
        )
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["locked"], 2)
        self.assertEqual(stats["fallback"], 1)
        self.assertEqual(stats["fallback_indices"], [2])
        self.assertEqual(stats["interpolated"], 1)

        blocks = [b.strip() for b in realigned.strip().split("\n\n") if b.strip()]
        line2_ts = blocks[1].splitlines()[1]
        l2_start, l2_end = [parse_timestamp_str(x) for x in line2_ts.split("-->")]
        self.assertAlmostEqual(l2_end, 3.80, places=2)

        line3_ts = blocks[2].splitlines()[1]
        l3_start, l3_end = [parse_timestamp_str(x) for x in line3_ts.split("-->")]
        # Line 3 must lock around 3.82s..5.00s and never drift to 9.00s
        self.assertLessEqual(l3_start, 4.05)
        self.assertAlmostEqual(l3_end, 5.00, places=2)

    def test_audit_subtitles_quality_agent_verdict_failure_modes(self):
        srt = (
            "1\n00:00:01,000 --> 00:00:03,000\n第一句測試字幕\n\n"
            "2\n00:00:03,200 --> 00:00:07,500\n第二句超出片長字幕\n"
        )
        low_lock_stats = {"total": 10, "locked": 5, "fallback": 5, "fallback_indices": [2, 3, 4, 5, 6]}
        metrics, _, _ = audit_subtitles_quality(
            srt,
            language="zh-TW",
            alignment_stats=low_lock_stats,
            media_duration=5.00,
        )
        verdict = metrics["agent_verdict"]
        self.assertFalse(verdict["pass_quality_gate"])
        self.assertEqual(verdict["suggested_action"], "ONE_SHOT_REMEDIATE")
        self.assertEqual(len(verdict["fatal_violations"]), 2)

    @patch("generate_subtitles.call_llm")
    def test_fail_fast_on_cloud_llm_failure(self, mock_call_llm):
        mock_call_llm.side_effect = RuntimeError("401 Unauthorized: Missing ADC credentials")
        with self.assertRaises(SystemExit) as cm1:
            extract_global_glossary(user_outline="Test outline")
        self.assertEqual(cm1.exception.code, 1)

        raw_srt = "1\n00:00:01,000 --> 00:00:02,500\n測試句子。\n"
        with self.assertRaises(SystemExit) as cm2:
            proofread_srt_with_llm(raw_srt=raw_srt, language="zh-TW")
        self.assertEqual(cm2.exception.code, 1)

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

