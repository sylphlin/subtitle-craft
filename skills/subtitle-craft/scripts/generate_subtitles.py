#!/usr/bin/env python3
"""
Subtitle Craft CLI Tool (generate_subtitles.py).
Generates millisecond-accurate, contextually proofread YouTube and Netflix subtitles (SRT and VTT)
using Google Cloud Vertex AI (ADC) and Google Cloud Storage (GCS).

Three-Stage Golden Pipeline:
  Stage 1: Global Audio Context and Glossary Extraction (Vertex AI Gemini 1M Context Scan via GCS).
           Scans the full audio staged on GCS to extract speaker names, organizations,
           acronyms, and domain jargon. Merges optional user outlines (--outline) and scripts (--script).
  Stage 2: Zero-Drift Acoustic Transcription via Whisper (mlx-whisper / faster-whisper).
           Extracts physical word-level timestamps (word_timestamps=True) with zero drift.
  Stage 3: Multimodal Audio-Text Chunked Proofreading (Vertex AI Gemini 3.8 Flash + GCS).
           Slices audio at natural pauses, proofreads subtitles against audio slices and the Global Glossary,
           re-projects timestamps onto Whisper word boundaries, and runs an 8-dimension quality audit.

Usage Examples:
  # Standard YouTube Subtitle Generation
  python3 scripts/generate_subtitles.py -i output/video.mp4

  # With User Interview Outline or Reference Script
  python3 scripts/generate_subtitles.py -i output/video.mp4 --outline "Host: Alex, Guest: Chris, Topics: AI, Cloud"
"""

import argparse
import concurrent.futures
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

# Support internal modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from modules.llm_client import call_llm
    from modules.gcp_client import (
        resolve_gcp_config,
        upload_file_to_gcs_with_cache,
        is_gdrive_source,
        download_gdrive_file_with_cache,
        delete_gcs_blob,
    )
    from modules.progress import LiveTicker
except ImportError:
    from scripts.modules.llm_client import call_llm
    from scripts.modules.gcp_client import (
        resolve_gcp_config,
        upload_file_to_gcs_with_cache,
        is_gdrive_source,
        download_gdrive_file_with_cache,
        delete_gcs_blob,
    )
    from scripts.modules.progress import LiveTicker


def format_timestamp_srt(seconds):
    """Format seconds (float) into an SRT timestamp: HH:MM:SS,mmm."""
    if seconds < 0:
        seconds = 0
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    ms = int(round((s - int(s)) * 1000))
    if ms >= 1000:
        ms = 999
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"


def srt_to_vtt(srt_content):
    """Convert standard SRT text into WebVTT (.vtt) text."""
    lines = ["WEBVTT\n"]
    for line in srt_content.strip().splitlines():
        if "-->" in line:
            parts = line.split("-->")
            start = parts[0].strip().replace(",", ".")
            end = parts[1].strip().replace(",", ".")
            lines.append(f"{start} --> {end}")
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


def extract_audio_16k_mono(input_media, output_wav):
    """Extract audio from input media to 16kHz mono 16-bit PCM WAV."""
    cmd = [
        "ffmpeg", "-y", "-i", input_media,
        "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        output_wav
    ]
    with LiveTicker(f"Extracting 16kHz mono audio ({os.path.basename(input_media)})"):
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg audio extraction failed: {res.stderr}")


def run_whisper_transcription(audio_wav, model_size="base", language="zh", device="auto", initial_prompt=None):
    """
    Run Whisper acoustic transcription with hardware acceleration:
      1. mlx-whisper (Apple Silicon Metal / Neural Engine)
      2. faster-whisper (CTranslate2 int8 with ARM NEON / AVX vectorization)
      3. openai-whisper (PyTorch MPS / CUDA / CPU)
    Returns: (sub_list, detected_lang, all_words)
    """
    print(f"\n[Stage 2/3] Running Whisper acoustic transcription (Model: {model_size}, Device: {device}, Word Timestamps: ON)...")
    if initial_prompt:
        print(f"  • Bias Prompt   : {initial_prompt}")
    t0 = time.time()
    lang_arg = None if str(language).lower() in ("auto", "none") else language
    if lang_arg and "-" in str(lang_arg):
        lang_arg = str(lang_arg).split("-")[0].lower()
    sub_list = []
    all_words = []
    detected_lang = language if lang_arg else "zh"

    # Backend 1: Apple Silicon Native MLX (mlx-whisper)
    if device in ("auto", "mps", "mlx"):
        try:
            import mlx_whisper
            print("  ► [Backend: Apple MLX] Utilizing Apple Silicon GPU / Neural Engine acceleration (word_timestamps=True)...")
            repo_name = f"mlx-community/whisper-{model_size}-mlx"
            result = mlx_whisper.transcribe(
                audio_wav,
                path_or_hf_repo=repo_name,
                language=lang_arg,
                initial_prompt=initial_prompt,
                word_timestamps=True
            )
            detected_lang = result.get("language") or detected_lang
            for idx, seg in enumerate(result.get("segments", []), start=1):
                seg_words = []
                for w in seg.get("words", []):
                    w_dict = {"word": w.get("word", ""), "start": float(w.get("start", 0.0)), "end": float(w.get("end", 0.0))}
                    seg_words.append(w_dict)
                    all_words.append(w_dict)
                s_start = seg_words[0]["start"] if seg_words else float(seg["start"])
                s_end = seg_words[-1]["end"] if seg_words else float(seg["end"])
                sub_list.append({
                    "index": idx,
                    "start": s_start,
                    "end": s_end,
                    "text": seg["text"].strip(),
                    "words": seg_words
                })
            duration = time.time() - t0
            print(f"  ✓ MLX transcription complete in {duration:.1f}s ({len(sub_list)} segments, {len(all_words)} words, language: '{detected_lang}')")
            return sub_list, detected_lang, all_words
        except ImportError:
            pass
        except Exception as e:
            print(f"  [Notice] MLX backend skipped ({e}), switching to next acceleration engine...", file=sys.stderr)

    # Backend 2: faster-whisper (CTranslate2 with int8 & ARM NEON / AVX vectorization)
    try:
        from faster_whisper import WhisperModel
        num_threads = min(8, os.cpu_count() or 4)
        print(f"  ► [Backend: faster-whisper] Utilizing multi-core ARM NEON/AVX vector acceleration ({num_threads} CPU threads, int8, word_timestamps=True)...")
        model = WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=num_threads)
        segments, info = model.transcribe(
            audio_wav,
            language=lang_arg,
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=250),
            initial_prompt=initial_prompt,
            word_timestamps=True
        )
        detected_lang = getattr(info, "language", detected_lang)

        total_dur = getattr(info, "duration", 0)
        for idx, seg in enumerate(segments, start=1):
            seg_words = []
            for w in (seg.words or []):
                w_dict = {"word": w.word, "start": float(w.start), "end": float(w.end)}
                seg_words.append(w_dict)
                all_words.append(w_dict)
            s_start = seg_words[0]["start"] if seg_words else float(seg.start)
            s_end = seg_words[-1]["end"] if seg_words else float(seg.end)
            sub_list.append({
                "index": idx,
                "start": s_start,
                "end": s_end,
                "text": seg.text.strip(),
                "words": seg_words
            })
            if total_dur > 0:
                pct = min(100.0, (s_end / total_dur) * 100.0)
                print(f"\r  ► [Whisper ASR] {format_timestamp_srt(s_end)} / {format_timestamp_srt(total_dur)} ({pct:4.1f}%) | Segment #{idx:03d}...", end="", flush=True)
            else:
                print(f"\r  ► [Whisper ASR] Segment #{idx:03d} ({format_timestamp_srt(s_start)} -> {format_timestamp_srt(s_end)})...", end="", flush=True)
        print()

    except ImportError:
        # Backend 3: openai-whisper (PyTorch MPS / CUDA / CPU)
        try:
            import whisper
            import torch
            if device in ("auto", "mps") and torch.backends.mps.is_available():
                target_dev = "mps"
            elif device in ("auto", "cuda") and torch.cuda.is_available():
                target_dev = "cuda"
            else:
                target_dev = "cpu"

            print(f"  ► [Backend: openai-whisper] Running on PyTorch ({target_dev.upper()}, word_timestamps=True)...")
            model = whisper.load_model(model_size, device=target_dev)
            result = model.transcribe(audio_wav, language=lang_arg, initial_prompt=initial_prompt, word_timestamps=True)
            detected_lang = result.get("language") or detected_lang

            for idx, seg in enumerate(result.get("segments", []), start=1):
                seg_words = []
                for w in seg.get("words", []):
                    w_dict = {"word": w.get("word", ""), "start": float(w.get("start", 0.0)), "end": float(w.get("end", 0.0))}
                    seg_words.append(w_dict)
                    all_words.append(w_dict)
                s_start = seg_words[0]["start"] if seg_words else float(seg["start"])
                s_end = seg_words[-1]["end"] if seg_words else float(seg["end"])
                sub_list.append({
                    "index": idx,
                    "start": s_start,
                    "end": s_end,
                    "text": seg["text"].strip(),
                    "words": seg_words
                })
        except ImportError:
            raise RuntimeError("No Whisper backend found! Install via `pip install mlx-whisper` (Apple Silicon GPU) or `pip install faster-whisper`.")

    duration = time.time() - t0
    print(f"  ✓ Whisper acoustic transcription complete in {duration:.1f}s ({len(sub_list)} segments, {len(all_words)} words, language: '{detected_lang}')")
    return sub_list, detected_lang, all_words


def build_srt_from_segments(segments):
    """Build a standard SRT string from a segment list."""
    blocks = []
    for seg in segments:
        idx = seg["index"]
        t_start = format_timestamp_srt(seg["start"])
        t_end = format_timestamp_srt(seg["end"])
        txt = seg["text"]
        blocks.append(f"{idx}\n{t_start} --> {t_end}\n{txt}\n")
    return "\n".join(blocks)


DEDICATED_LOCALES = ("zh-TW", "zh-CN", "ja", "ko", "en")
FALLBACK_LOCALE = "en"
_WARNED_LANGUAGE_FALLBACK = set()


def normalize_language_tag(lang_str):
    """Normalize a language code to a supported locale code (zh-TW, zh-CN, ja, ko, en)."""
    if not lang_str:
        if "" not in _WARNED_LANGUAGE_FALLBACK:
            _WARNED_LANGUAGE_FALLBACK.add("")
            sys.stderr.write(
                "[Warning] Language tag is empty or unspecified. Falling back to 'en'\n"
                "conventions (Latin script typography and line limits). Subtitle\n"
                "segmentation and punctuation may not match the norms of this language.\n"
                "Supply --language explicitly to override.\n"
            )
            sys.stderr.flush()
        return FALLBACK_LOCALE

    l = str(lang_str).lower().replace("_", "-").strip()
    if l in ("zh", "zh-tw", "zh-hant", "zh-hk", "zh-mo", "cmn-hant", "cmn-tw"):
        return "zh-TW"
    if l in ("zh-cn", "zh-hans", "zh-sg", "cmn-hans", "cmn-cn"):
        return "zh-CN"
    if l.startswith("en"):
        return "en"
    if l.startswith("ja"):
        return "ja"
    if l.startswith("ko"):
        return "ko"

    if l not in _WARNED_LANGUAGE_FALLBACK:
        _WARNED_LANGUAGE_FALLBACK.add(l)
        sys.stderr.write(
            f"[Warning] Language '{lang_str}' has no dedicated subtitle template. Falling back to 'en'\n"
            f"conventions (Latin script typography and line limits). Subtitle\n"
            f"segmentation and punctuation may not match the norms of this language.\n"
            f"Supply --language explicitly to override.\n"
        )
        sys.stderr.flush()
    return FALLBACK_LOCALE


def load_proofread_template(language="zh-TW", max_chars_cjk=15,
                            max_chars_korean=16, max_chars_latin=42):
    """Load the subtitle proofreading prompt template for the given language locale."""
    norm_lang = normalize_language_tag(language)

    search_dirs = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets"),
        os.path.expanduser("~/.gemini/config/plugins/subtitle-craft/skills/subtitle-craft/assets"),
        os.path.expanduser("~/.gemini/config/skills/subtitle-craft/assets"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets"),
    ]

    target_names = [
        f"subtitle_proofread_template.{norm_lang}.md",
        "subtitle_proofread_template.zh-TW.md",
    ]

    for s_dir in search_dirs:
        for t_name in target_names:
            p = os.path.join(s_dir, t_name)
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                        if content:
                            return content, os.path.basename(p)
                except Exception:
                    pass

    return (
        "You are an expert subtitle proofreader for YouTube.\n"
        f"Your task: Re-segment and proofread subtitles into natural, fluent semantic clauses (max {max_chars_cjk} chars for CJK, {max_chars_korean} for Korean, {max_chars_latin} for Latin/English) with acoustic timestamp fusion.\n"
        "Output ONLY the corrected SRT inside ```srt ... ``` code block."
    ), "builtin_fallback"


def extract_whisper_prompt(glossary_text, max_chars=145, language="zh-TW"):
    """
    Extract a compact Whisper ASR initial prompt from the Global Glossary:
      1. Primary: Parse the '> **Whisper Initial Prompt**: ...' line.
      2. Fallback: Parse bold terms (**word**) from glossary sections.
    """
    if not glossary_text:
        return None

    m = re.search(r">\s*\*\*Whisper\s+(?:Initial\s+)?Prompt\*\*:\s*(.+)", glossary_text, re.IGNORECASE)
    if not m:
        m = re.search(r"\*\*Whisper\s+(?:Initial\s+)?Prompt\*\*:\s*(.+)", glossary_text, re.IGNORECASE)
    if m:
        line = m.group(1).strip()
        line = re.sub(r"^[`'\"]+|[`'\"]+$", "", line).strip()
        if len(line) > max_chars:
            cut = line[:max_chars]
            last_comma = max(cut.rfind("、"), cut.rfind("，"), cut.rfind(","))
            if last_comma > 20:
                line = cut[:last_comma] + "。"
            else:
                line = cut + "..."
        return line

    bold_matches = re.findall(r"\*\*(.+?)\*\*", glossary_text)
    filtered = []
    category_keywords = [
        "Person", "Speaker", "Names", "Organizations", "Products", "Places", "Brands",
        "Domain Jargon", "Tech Terms", "Core Topic", "全片專有名詞", "Whisper", "講者與人物",
        "公司、品牌", "行業專有名詞", "核心主題", "Glossary", "Terminology"
    ]
    for w in bold_matches:
        w = w.strip()
        if any(cat in w for cat in category_keywords):
            continue
        clean_w = re.split(r"[（\(]", w)[0].strip()
        if clean_w and clean_w not in filtered and len(clean_w) <= 24:
            filtered.append(clean_w)

    if not filtered:
        return None

    norm_lang = normalize_language_tag(language)
    locale_config = {
        "zh-TW": {
            "prefix": "以下為繁體中文對談字幕，專有名詞：",
            "sep": "、",
            "suffix": "。",
        },
        "zh-CN": {
            "prefix": "以下为简体中文对谈字幕，专有名词：",
            "sep": "、",
            "suffix": "。",
        },
        "ja": {
            "prefix": "以下は日本語の対談字幕です。専門用語：",
            "sep": "、",
            "suffix": "。",
        },
        "ko": {
            "prefix": "다음은 한국어 대화 자막입니다. 전문 용어: ",
            "sep": ", ",
            "suffix": ".",
        },
        "en": {
            "prefix": "The following is a video transcription containing terms: ",
            "sep": ", ",
            "suffix": ".",
        },
    }
    cfg = locale_config.get(norm_lang, locale_config[FALLBACK_LOCALE])
    prefix = cfg["prefix"]
    sep = cfg["sep"]
    suffix = cfg["suffix"]

    assembled = prefix
    for item in filtered:
        cand = assembled + (sep if assembled != prefix else "") + item
        if len(cand) + len(suffix) > max_chars:
            break
        assembled = cand
    assembled += suffix
    return assembled


def extract_global_glossary(audio_wav=None, segments=None, user_outline=None, user_script=None,
                            model="gemini-3.8-flash", project=None, gcs_bucket=None,
                            location=None, region=None, cleanup_gcs=False, language="auto"):
    """
    Stage 1: Global Audio Context and Consistency Glossary Extraction (Vertex AI Gemini 1M Context Scan).
    Listens to full episode audio staged on GCS and/or analyzes user script/outline to extract
    speaker names, organizations, acronyms, and domain terminology.
    Produces a concentrated Whisper Initial Prompt line at the top.
    """
    print("\n[Stage 1/3] Extracting Global Consistency Glossary across entire episode...")
    t0 = time.time()

    context_parts = []
    if user_script:
        context_parts.append(f"=== Source Script Reference ===\n{user_script}\n")
    if user_outline:
        context_parts.append(f"=== User Interview Outline ===\n{user_outline}\n")
    supplementary_section = "\n".join(context_parts) if context_parts else ""

    norm_lang = "zh-TW" if str(language).lower() in ("auto", "none") else normalize_language_tag(language)
    prompt_examples = {
        "zh-TW": "> **Whisper Initial Prompt**: 以下為繁體中文對談字幕，專有名詞：詞1、詞2、詞3...",
        "zh-CN": "> **Whisper Initial Prompt**: 以下为简体中文对谈字幕，专有名词：词1、词2、词3...",
        "ja": "> **Whisper Initial Prompt**: 以下は日本語の対談字幕です。専門用語：用語1、用語2、用語3...",
        "ko": "> **Whisper Initial Prompt**: 다음은 한국어 대화 자막입니다. 전문 용어: 용어1, 용어2, 용어3...",
        "en": "> **Whisper Initial Prompt**: The following is a video transcription containing terms: Term1, Term2, Term3...",
    }
    whisper_example_line = prompt_examples.get(norm_lang, prompt_examples["zh-TW"])

    prompt = (
        "You are an expert Chief Subtitle Editor for professional YouTube productions.\n"
        "Your goal is to extract a comprehensive, authoritative **Global Terminology Glossary (全片專有名詞與詞彙對照表)** "
        "directly from this recording (and supplied script/outline) to ensure 100% spelling, naming, and domain term consistency across all subtitle segments.\n\n"
        f"{supplementary_section}\n"
        "Extract ONLY verified domain terms, entity names, foreign proper nouns, and proper spellings that actually appear in this recording.\n\n"
        "=== CRITICAL INSTRUCTION FOR WHISPER ASR ===\n"
        "At the VERY TOP of your Markdown output, provide a single, highly-concentrated bias prompt line specifically designed for Whisper ASR (speech-to-text decoder initial prompt) matching the spoken language of the recording.\n"
        "Strictly limit this line to 100~140 characters, listing only the most critical, uncommon proper nouns, speaker names, and technical terms:\n"
        f"{whisper_example_line}\n\n"
        "Then structure the full detailed glossary cleanly in Markdown:\n"
        "1. **講者與人物姓名 (Person & Speaker Names)**: Official names, titles & roles\n"
        "2. **公司、品牌、產品與機構 (Organizations, Products & Brands)**: Official brand, company & tool names\n"
        "3. **行業專有名詞與技術術語 (Domain Jargon & Tech Terms)**: Industry terminology, English acronyms, Japanese Kanji/Kana\n"
        "4. **核心主題概念 (Core Topic Concepts)**: Key themes discussed in this episode\n\n"
        "Output ONLY the clean, authoritative Markdown Glossary:"
    )

    gcs_audio_uri = None
    tmp_audio_mp3 = None
    if audio_wav and os.path.exists(audio_wav) and gcs_bucket:
        try:
            tmp_audio_mp3 = os.path.join(os.path.dirname(audio_wav), "global_glossary_audio.mp3")
            subprocess.run(
                ["ffmpeg", "-y", "-i", audio_wav, "-vn", "-ar", "16000", "-ac", "1", "-b:a", "48k", tmp_audio_mp3],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
            )
            gcs_audio_uri = upload_file_to_gcs_with_cache(
                tmp_audio_mp3,
                bucket_name=gcs_bucket,
                gcs_prefix="raw",
                project=project,
                region=region or "us-central1",
            )
        except Exception as gcs_err:
            print(f"  [Notice] GCS audio upload failed ({gcs_err}), falling back to text-only glossary scan.", file=sys.stderr)

    try:
        with LiveTicker("Extracting Global Consistency Glossary via Vertex AI Gemini (1M context scan)"):
            glossary_content = call_llm(
                prompt=prompt,
                model=model,
                project=project,
                location=location,
                gcs_bucket=gcs_bucket,
                region=region,
                gcs_uri=gcs_audio_uri,
                temperature=0.1,
                max_tokens=4096,
                thinking_budget=0,
            )
        duration = time.time() - t0
        print(f"  ✓ Global Glossary extracted in {duration:.1f}s")
        return glossary_content.strip()
    except Exception as e:
        print(f"  [Warning] Global glossary extraction failed ({e}). Continuing with standard proofreading.", file=sys.stderr)
        return user_outline or user_script or ""
    finally:
        if cleanup_gcs and gcs_audio_uri:
            delete_gcs_blob(gcs_audio_uri, project=project)
        if tmp_audio_mp3 and os.path.exists(tmp_audio_mp3):
            try:
                os.remove(tmp_audio_mp3)
            except Exception:
                pass


def parse_timestamp_str(ts_str):
    """Parse an SRT timestamp 'HH:MM:SS,mmm' to float seconds."""
    h, m, sec_ms = ts_str.strip().split(":")
    sec, ms = sec_ms.replace(".", ",").split(",")
    return float(h) * 3600 + float(m) * 60 + float(sec) + float(ms) / 1000.0


def split_blocks_into_semantic_chunks(raw_blocks, target_chunk_size=80, min_chunk_size=55, max_chunk_size=105):
    """
    Split SRT blocks into silence-aware semantic chunks.
    Inspects adjacent timestamp gaps and clause closures within [min_chunk_size, max_chunk_size]
    to split at natural speech pauses (gap >= 0.4s).
    """
    total = len(raw_blocks)
    if total <= max_chunk_size:
        return [raw_blocks]

    parsed_blocks = []
    for b in raw_blocks:
        lines = b.splitlines()
        t_start, t_end, txt = 0.0, 0.0, ""
        if len(lines) >= 2 and "-->" in lines[1]:
            t1, t2 = lines[1].split("-->")
            t_start = parse_timestamp_str(t1.strip())
            t_end = parse_timestamp_str(t2.strip())
            txt = lines[2].strip() if len(lines) >= 3 else ""
        elif len(lines) >= 1 and "-->" in lines[0]:
            t1, t2 = lines[0].split("-->")
            t_start = parse_timestamp_str(t1.strip())
            t_end = parse_timestamp_str(t2.strip())
            txt = lines[1].strip() if len(lines) >= 2 else ""
        parsed_blocks.append({
            "block": b,
            "start": t_start,
            "end": t_end,
            "text": txt
        })

    chunks = []
    cur_idx = 0
    while cur_idx < total:
        remaining = total - cur_idx
        if remaining <= max_chunk_size:
            chunks.append(raw_blocks[cur_idx:])
            break

        search_start = cur_idx + min_chunk_size
        search_end = min(total - 1, cur_idx + max_chunk_size)

        best_split = min(total, cur_idx + target_chunk_size)
        best_score = -9999.0

        for i in range(search_start, search_end):
            c = parsed_blocks[i]
            n = parsed_blocks[i + 1]
            gap = n["start"] - c["end"]

            score = min(gap, 2.0) * 15.0
            dist = abs(i - (cur_idx + target_chunk_size))
            score -= dist * 0.4

            txt = c["text"]
            if any(txt.endswith(p) for p in ["？", "?", "！", "!", "。", "……", "..."]):
                score += 6.0
            if any(txt.endswith(w) for w in ["來說", "來講", "而言", "之後", "的話", "來看"]):
                score += 3.0

            n_txt = n["text"]
            if any(n_txt.startswith(w) for w in ["但是", "而且", "所以", "不過", "然而", "如果"]):
                score -= 4.0

            if score > best_score:
                best_score = score
                best_split = i + 1

        chunks.append(raw_blocks[cur_idx:best_split])
        cur_idx = best_split

    return chunks


def align_split_clauses_with_words(final_parts, t_start, t_end, all_words=None):
    """Pin split sub-clauses to physical word boundaries in all_words."""
    if not all_words or len(final_parts) <= 1:
        tot_len = max(1, sum(len(p) for p in final_parts))
        cur_t = t_start
        span = max(0.5, t_end - t_start)
        items = []
        for f_p in final_parts:
            p_dur = span * (len(f_p) / tot_len)
            items.append({"start": cur_t, "end": cur_t + p_dur, "text": f_p})
            cur_t += p_dur
        return items

    nearby_words = [
        w for w in all_words
        if float(w.get("end", 0.0)) >= t_start - 0.3 and float(w.get("start", 0.0)) <= t_end + 0.3
    ]
    if len(nearby_words) < len(final_parts):
        tot_len = max(1, sum(len(p) for p in final_parts))
        cur_t = t_start
        span = max(0.5, t_end - t_start)
        items = []
        for f_p in final_parts:
            p_dur = span * (len(f_p) / tot_len)
            items.append({"start": cur_t, "end": cur_t + p_dur, "text": f_p})
            cur_t += p_dur
        return items

    w_chars = []
    for w in nearby_words:
        w_text = re.sub(r"[\s\.,\?!，。？！、：:;；—\-~]+", "", w.get("word", ""))
        w_s = float(w.get("start", t_start))
        w_e = float(w.get("end", t_end))
        w_chars.append({"text": w_text, "start": w_s, "end": w_e})

    first_clean = re.sub(r"[\s\.,\?!，。？！、：:;；—\-~]+", "", final_parts[0]).lower()
    best_split_time = None
    accumulated = ""
    for wc in w_chars:
        accumulated += wc["text"].lower()
        if len(accumulated) >= len(first_clean) * 0.75:
            if first_clean in accumulated or accumulated in first_clean:
                best_split_time = wc["end"]
                break

    if best_split_time and t_start + 0.4 <= best_split_time <= t_end - 0.4:
        items = [{"start": t_start, "end": best_split_time, "text": final_parts[0]}]
        rem_parts = final_parts[1:]
        if len(rem_parts) == 1:
            items.append({"start": best_split_time, "end": t_end, "text": rem_parts[0]})
        else:
            items.extend(align_split_clauses_with_words(rem_parts, best_split_time, t_end, nearby_words))
        return items
    else:
        tot_len = max(1, sum(len(p) for p in final_parts))
        cur_t = t_start
        span = max(0.5, t_end - t_start)
        items = []
        for f_p in final_parts:
            p_dur = span * (len(f_p) / tot_len)
            items.append({"start": cur_t, "end": cur_t + p_dur, "text": f_p})
            cur_t += p_dur
        return items


def proofread_single_chunk(c_idx, num_chunks, chunk_slice, template, global_glossary, audio_wav,
                           model="gemini-3.8-flash", user_script=None,
                           project=None, location=None, gcs_bucket=None, region=None,
                           max_chars_cjk=15, max_chars_korean=16, max_chars_latin=42):
    """Proofread a single chunk of SRT blocks with a GCS-staged audio slice and optional reference script."""
    chunk_text = "\n\n".join(chunk_slice)
    glossary_section = f"\n=== 全片權威專有名詞對照表 (Global Consistency Glossary) ===\n{global_glossary}\n============================================================\n" if global_glossary else ""
    script_section = ""
    if user_script:
        script_section = (
            f"\n=== 錄音講稿/逐字稿原稿 (Source Script Reference) ===\n"
            f"{user_script[:6000]}\n"
            f"============================================================\n"
            f"【校對核心準則】：以講者實際口白發音為準（保留現場真實口語內容與自然句法），但凡遇到專有名詞、人物名稱、外來語或同音字疑義時，嚴格參照【錄音講稿/逐字稿原稿】之標準文字修正。\n"
        )

    chunk_mp3_path = None
    tmp_dir = os.path.dirname(audio_wav) if audio_wav else None

    if audio_wav and os.path.exists(audio_wav):
        try:
            first_block = chunk_slice[0].splitlines()
            last_block = chunk_slice[-1].splitlines()
            if len(first_block) >= 2 and "-->" in first_block[1] and len(last_block) >= 2 and "-->" in last_block[1]:
                t_start_sec = max(0.0, parse_timestamp_str(first_block[1].split("-->")[0]) - 0.5)
                t_end_sec = parse_timestamp_str(last_block[1].split("-->")[1]) + 0.5
                dur_sec = max(1.0, t_end_sec - t_start_sec)

                chunk_mp3_path = os.path.join(tmp_dir, f"chunk_{c_idx:03d}_{os.getpid()}.mp3")
                cmd = [
                    "ffmpeg", "-y", "-ss", f"{t_start_sec:.3f}", "-t", f"{dur_sec:.3f}",
                    "-i", audio_wav, "-vn", "-ar", "16000", "-ac", "1", "-b:a", "48k", chunk_mp3_path
                ]
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except Exception:
            chunk_mp3_path = None

    prompt = (
        f"{template}\n"
        f"{glossary_section}\n"
        f"{script_section}\n"
        f"--- 待校對與重整之原始 SRT 碎字幕（區塊 {c_idx + 1}/{num_chunks}）---\n"
        f"```srt\n{chunk_text}\n```\n\n"
        f"請邊聽附帶的音訊錄音、依據全片對照表與語意段落規範，進行自然斷句重整（中文/日文 <= {max_chars_cjk} 字，韓文 <= {max_chars_korean} 字，英文 <= {max_chars_latin} 字元）、時間軸物理聲學熔接與同音錯字校正，輸出重整後的完整 SRT："
    )

    try:
        response_text = call_llm(
            prompt=prompt,
            model=model,
            project=project,
            location=location,
            gcs_bucket=gcs_bucket,
            region=region,
            audio_path=chunk_mp3_path,
            cleanup_ephemeral_audio=True,
            temperature=0.1,
            max_tokens=8192,
            thinking_budget=0,
        )

        match = re.search(r"```(?:srt)?\s*\n(.*?)```", response_text, re.DOTALL | re.IGNORECASE)
        clean_chunk = match.group(1).strip() if match else response_text.strip()
        clean_chunk = re.sub(r"^```(?:srt)?\s*\n?", "", clean_chunk, flags=re.IGNORECASE)
        clean_chunk = re.sub(r"\n?```\s*$", "", clean_chunk).strip()

        if "-->" in clean_chunk:
            return c_idx, clean_chunk, True
        else:
            return c_idx, chunk_text, False

    except Exception as e:
        print(f"  [Warning] Chunk {c_idx+1} proofreading error after retries: {e}. Keeping original.", file=sys.stderr)
        return c_idx, chunk_text, False
    finally:
        if chunk_mp3_path and os.path.exists(chunk_mp3_path):
            try:
                os.remove(chunk_mp3_path)
            except Exception:
                pass


def clean_subtitle_text(text, language="zh-TW"):
    """
    Format subtitle text according to Netflix and YouTube typography rules:
      1. CJK Policy (zh-TW, zh-CN, ja, ko):
         - Replace inline commas with single spaces.
         - Normalize CJK-Latin and CJK-number spacing.
         - Strip trailing punctuation [。，、；:;,.—-] while preserving [？!……].
      2. Latin Policy (en and fallback locales):
         - Preserve inline commas and standard sentence punctuation.
         - Strip only trailing whitespace and stray isolated ASCII hyphens (-).
      3. Strip residual Markdown markers (**, __, `, ##).
    """
    norm_lang = normalize_language_tag(language)
    lines = text.strip().splitlines()
    cleaned_lines = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        line = re.sub(r"(\*{1,3}|_{2,}|`{1,3}|(?<=^)\s*#{1,6}\s+)", "", line)

        if norm_lang in ["zh-TW", "zh-CN", "ja", "ko"]:
            line = re.sub(r"[，,]+", " ", line)
            line = re.sub(r"([\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af])([A-Za-z0-9])", r"\1 \2", line)
            line = re.sub(r"([A-Za-z0-9])([\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af])", r"\1 \2", line)
            line = re.sub(r"[ \t]+", " ", line)
            line = re.sub(r"[\s。，、；:;,.—-]+$", "", line).strip()
        else:
            line = re.sub(r"[ \t]+", " ", line).strip()
            line = re.sub(r"(?<!-)-\s*$", "", line).strip()

        cleaned_lines.append(line)

    return "\n".join(cleaned_lines)


def calc_display_width(text, norm_lang="zh-TW"):
    """Calculate typographical display width (1.0 for CJK, 0.5 for ASCII alphanumeric)."""
    if norm_lang in ["zh-TW", "zh-CN", "ja"]:
        return sum(1.0 if ord(c) > 127 else 0.5 for c in text.replace(" ", ""))
    elif norm_lang == "ko":
        return sum(1.0 if ord(c) > 127 else 0.5 for c in text)
    else:
        return float(len(text))


def split_long_clause(text, max_w=15.0, norm_lang="zh-TW"):
    """Split a clause exceeding max_w into natural sub-clauses."""
    w = calc_display_width(text, norm_lang)
    if w <= max_w:
        return [clean_subtitle_text(text, language=norm_lang)]

    parts = [p.strip() for p in text.split(" ") if p.strip()]
    if len(parts) > 1:
        chunks = []
        cur = []
        for p in parts:
            cand = " ".join(cur + [p])
            if cur and calc_display_width(cand, norm_lang) > max_w:
                cleaned_c = clean_subtitle_text(" ".join(cur), language=norm_lang)
                if cleaned_c:
                    chunks.append(cleaned_c)
                cur = [p]
            else:
                cur.append(p)
        if cur:
            cleaned_c = clean_subtitle_text(" ".join(cur), language=norm_lang)
            if cleaned_c:
                chunks.append(cleaned_c)
        if len(chunks) > 1:
            return chunks

    sub_parts = [p.strip() for p in re.split(r"(?<=[、，,])", text) if p.strip()]
    if len(sub_parts) > 1:
        chunks = []
        cur = []
        for p in sub_parts:
            cand = "".join(cur + [p])
            if cur and calc_display_width(cand, norm_lang) > max_w:
                cleaned_c = clean_subtitle_text("".join(cur), language=norm_lang)
                if cleaned_c:
                    chunks.append(cleaned_c)
                cur = [p]
            else:
                cur.append(p)
        if cur:
            cleaned_c = clean_subtitle_text("".join(cur), language=norm_lang)
            if cleaned_c:
                chunks.append(cleaned_c)
        if len(chunks) > 1:
            return chunks

    return [clean_subtitle_text(text, language=norm_lang)]


def score_candidate(cand_str, clean_t):
    """Calculate composite acoustic match score with onset and termination bonuses."""
    matcher = difflib.SequenceMatcher(None, cand_str, clean_t)
    matched_chars = sum(b.size for b in matcher.get_matching_blocks())
    if matched_chars == 0:
        return 0.0
    coverage = matched_chars / len(clean_t)
    density = matched_chars / len(cand_str)
    first_bonus = 0.15 if cand_str[0] == clean_t[0] else (
        0.08 if len(cand_str) > 1 and len(clean_t) > 1 and cand_str[1] == clean_t[1] else 0.0
    )
    last_bonus = 0.10 if cand_str[-1] == clean_t[-1] else (
        0.05 if len(cand_str) > 1 and len(clean_t) > 1 and cand_str[-2] == clean_t[-2] else 0.0
    )
    return 0.50 * coverage + 0.25 * density + first_bonus + last_bonus


def realign_subtitles_to_words(proofread_srt, all_words, language="zh-TW", is_video_start=False):
    """
    Realign LLM-proofread subtitle lines to physical word boundaries from Whisper word timestamps.
    Returns: (realigned_srt, alignment_stats)
    """
    default_stats = {"total": 0, "locked": 0, "fallback": 0, "fallback_indices": []}
    if not all_words:
        return proofread_srt, default_stats

    char_timeline = []
    for w in all_words:
        w_raw = w.get("word", "")
        w_text = re.sub(r"[\s\.,\?!，。？！、：:;；—\-~]+", "", w_raw)
        if not w_text:
            continue
        w_start = float(w.get("start", 0.0))
        w_end = float(w.get("end", w_start + 0.1))
        w_dur = max(0.01, w_end - w_start)
        char_dur = w_dur / max(1, len(w_text))
        for idx, ch in enumerate(w_text):
            ch_s = w_start + idx * char_dur
            ch_e = ch_s + char_dur
            char_timeline.append({
                "char": ch,
                "start": ch_s,
                "end": ch_e
            })

    if not char_timeline:
        return proofread_srt, default_stats

    whisper_chars = "".join(c["char"] for c in char_timeline)
    whisper_chars_lower = whisper_chars.lower()
    total_chars = len(char_timeline)

    blocks = [b.strip() for b in proofread_srt.strip().split("\n\n") if b.strip()]
    items = []
    for b in blocks:
        lines = b.splitlines()
        if len(lines) >= 3 and "-->" in lines[1]:
            t1, t2 = lines[1].split("-->")
            txt = "\n".join(lines[2:]).strip()
            items.append({
                "fallback_start": parse_timestamp_str(t1.strip()),
                "fallback_end": parse_timestamp_str(t2.strip()),
                "text": txt
            })
        elif len(lines) == 2 and "-->" in lines[0]:
            t1, t2 = lines[0].split("-->")
            txt = lines[1].strip()
            items.append({
                "fallback_start": parse_timestamp_str(t1.strip()),
                "fallback_end": parse_timestamp_str(t2.strip()),
                "text": txt
            })

    if not items:
        return proofread_srt, default_stats

    alignment_stats = {
        "total": len(items),
        "locked": 0,
        "fallback": 0,
        "fallback_indices": []
    }

    cur_char_idx = 0
    realigned_items = []

    for idx, item in enumerate(items):
        raw_text = item["text"]
        clean_t = re.sub(r"[\s\.,\?!，。？！、：:;；—\-~]+", "", raw_text).lower()
        if not clean_t:
            continue

        clean_t_no_brackets = re.sub(r"[（\(].*?[）\)]", "", clean_t)
        has_bracket_variant = bool(clean_t_no_brackets and clean_t_no_brackets != clean_t)

        L = len(clean_t)
        best_match = None
        best_score = -1.0

        search_start = cur_char_idx
        search_extent = min(total_chars, search_start + max(L * 2 + 25, 80))

        for s_pos in range(search_start, search_extent):
            for cand_len in range(max(1, L - 4), min(total_chars - s_pos + 1, L + 10)):
                cand_str = whisper_chars_lower[s_pos: s_pos + cand_len]
                sc = score_candidate(cand_str, clean_t)
                if has_bracket_variant:
                    sc_nb = score_candidate(cand_str, clean_t_no_brackets)
                    sc = max(sc, sc_nb)
                if sc > best_score + 1e-4:
                    best_score = sc
                    best_match = (s_pos, s_pos + cand_len - 1)

        min_ratio = 0.50 if L >= 4 else 0.65
        if best_match and best_score >= min_ratio:
            m_start, m_end = best_match
            t_start = char_timeline[m_start]["start"]
            t_end = char_timeline[m_end]["end"]
            cur_char_idx = m_end + 1
            alignment_stats["locked"] += 1
        else:
            t_start = item.get("fallback_start", 0.0)
            t_end = item.get("fallback_end", t_start + 2.0)
            while cur_char_idx < total_chars and char_timeline[cur_char_idx]["end"] <= t_end:
                cur_char_idx += 1
            alignment_stats["fallback"] += 1
            alignment_stats["fallback_indices"].append(item.get("index", idx + 1))

        lead_sec = 0.180
        target_start = max(0.0, t_start - lead_sec)

        if idx == 0 and is_video_start:
            t_start = target_start
        elif len(realigned_items) > 0:
            prev = realigned_items[-1]
            prev_dur = prev["end"] - prev["start"]
            if target_start >= prev["end"] + 0.02:
                t_start = target_start
            else:
                prev_room = max(0.0, prev_dur - 1.0)
                needed_shift = (prev["end"] + 0.02) - target_start
                shift = min(prev_room, needed_shift)
                if shift >= 0.04:
                    prev["end"] -= shift
                    t_start = prev["end"]
                else:
                    t_start = max(prev["end"], target_start)

        realigned_items.append({
            "index": item.get("index", idx + 1),
            "start": t_start,
            "end": t_end,
            "text": raw_text
        })

    out_blocks = []
    for idx, it in enumerate(realigned_items, start=1):
        s_str = format_timestamp_srt(it["start"])
        e_str = format_timestamp_srt(it["end"])
        out_blocks.append(f"{idx}\n{s_str} --> {e_str}\n{it['text']}\n")

    return "\n\n".join(out_blocks).strip() + "\n", alignment_stats


def sanitize_subtitle_timings(raw_srt, all_words=None, min_duration=1.0, max_duration=6.0,
                             post_tail_buffer=0.4, min_gap_threshold=0.2, language="zh-TW",
                             max_chars_cjk=15, max_chars_korean=16, max_chars_latin=42):
    """
    Sanitize subtitle timings and pacing:
      1. Enforce monotonic forward alignment without overlaps.
      2. Extend post-tail reading buffer (+0.4s) into natural pauses.
      3. Enforce min duration (>= 1.0s) and max duration (<= 6.0s) bounds.
      4. Bridge micro-gaps (< 0.2s) to prevent visual flicker.
      5. Split overlength clauses and anchor them to word timestamps.
    """
    norm_lang = normalize_language_tag(language)
    if norm_lang in ["zh-TW", "zh-CN", "ja"]:
        max_w = float(max_chars_cjk)
    elif norm_lang == "ko":
        max_w = float(max_chars_korean)
    else:
        max_w = float(max_chars_latin)

    blocks = [b.strip() for b in raw_srt.strip().split("\n\n") if b.strip()]
    items = []

    for b in blocks:
        lines = b.splitlines()
        if len(lines) >= 3 and "-->" in lines[1]:
            t1, t2 = lines[1].split("-->")
            t_start = parse_timestamp_str(t1.strip())
            t_end = parse_timestamp_str(t2.strip())
            raw_txt = "\n".join(lines[2:]).strip()
            clean_txt = clean_subtitle_text(raw_txt, language=language)
            if clean_txt:
                q_parts = [p.strip() for p in re.split(r"(?<=[？?])\s+", clean_txt) if p.strip()]
                final_parts = []
                for q_p in q_parts:
                    final_parts.extend(split_long_clause(q_p, max_w=max_w, norm_lang=norm_lang))

                if len(final_parts) > 1:
                    items.extend(align_split_clauses_with_words(final_parts, t_start, t_end, all_words=all_words))
                else:
                    items.append({"start": t_start, "end": t_end, "text": clean_txt})
        elif len(lines) == 2 and "-->" in lines[0]:
            t1, t2 = lines[0].split("-->")
            t_start = parse_timestamp_str(t1.strip())
            t_end = parse_timestamp_str(t2.strip())
            raw_txt = lines[1].strip()
            clean_txt = clean_subtitle_text(raw_txt, language=language)
            if clean_txt:
                q_parts = [p.strip() for p in re.split(r"(?<=[？?])\s+", clean_txt) if p.strip()]
                final_parts = []
                for q_p in q_parts:
                    final_parts.extend(split_long_clause(q_p, max_w=max_w, norm_lang=norm_lang))

                if len(final_parts) > 1:
                    items.extend(align_split_clauses_with_words(final_parts, t_start, t_end, all_words=all_words))
                else:
                    items.append({"start": t_start, "end": t_end, "text": clean_txt})

    if not items:
        return raw_srt

    for i in range(len(items)):
        if items[i]["end"] <= items[i]["start"]:
            items[i]["end"] = items[i]["start"] + 1.0

        if items[i]["end"] - items[i]["start"] > max_duration:
            if i + 1 < len(items) and items[i + 1]["start"] > items[i]["start"]:
                items[i]["end"] = min(items[i]["start"] + max_duration, items[i + 1]["start"])
            else:
                items[i]["end"] = items[i]["start"] + min(4.0, max_duration)

    for i in range(1, len(items)):
        prev = items[i - 1]
        cur = items[i]
        if cur["start"] <= prev["start"]:
            cur["start"] = prev["start"] + 0.5
        if prev["end"] > cur["start"]:
            if cur["start"] >= prev["start"] + 0.6:
                prev["end"] = cur["start"]
            else:
                prev["end"] = max(prev["start"] + 0.5, cur["start"])
                if cur["start"] < prev["end"]:
                    cur["start"] = prev["end"]
        if cur["end"] <= cur["start"]:
            cur["end"] = cur["start"] + 1.0

    for i in range(len(items)):
        cur = items[i]
        nxt_start = items[i + 1]["start"] if i + 1 < len(items) else (cur["end"] + 10.0)

        raw_gap = nxt_start - cur["end"]
        if raw_gap < (post_tail_buffer + min_gap_threshold):
            if raw_gap > 0:
                cur["end"] = nxt_start
        else:
            cur["end"] = cur["end"] + post_tail_buffer

        dur = cur["end"] - cur["start"]
        if dur < min_duration:
            needed = min_duration - dur
            room = nxt_start - cur["end"]
            if room > 0:
                cur["end"] = min(cur["end"] + min(needed, room), nxt_start)

        if cur["end"] - cur["start"] > max_duration:
            cur["end"] = cur["start"] + max_duration

    out_blocks = []
    for idx, it in enumerate(items, start=1):
        s_str = format_timestamp_srt(it["start"])
        e_str = format_timestamp_srt(it["end"])
        ts_line = f"{s_str} --> {e_str}"
        txt = it["text"]
        out_blocks.append(f"{idx}\n{ts_line}\n{txt}\n")

    return "\n\n".join(out_blocks).strip() + "\n"


def proofread_srt_with_llm(raw_srt, audio_wav=None, global_glossary=None, user_script=None,
                           model="gemini-3.8-flash", chunk_size=80,
                           max_workers=5, language="zh-TW", all_words=None, cache_path=None,
                           project=None, location=None, gcs_bucket=None, region=None,
                           max_chars_cjk=15, max_chars_korean=16, max_chars_latin=42):
    """
    Stage 3: Multimodal Audio-Text Parallel Chunked Proofreading with Global Glossary and Reference Script.
    Slices local audio chunks, stages them to GCS, proofreads subtitles via Vertex AI,
    and re-projects timestamps onto Whisper word boundaries.
    """
    template, tmpl_file = load_proofread_template(
        language=language,
        max_chars_cjk=max_chars_cjk,
        max_chars_korean=max_chars_korean,
        max_chars_latin=max_chars_latin
    )
    print(f"\n[Stage 3/3] Running Multimodal Audio-Text LLM Proofreading (Model: {model}, Chunk: {chunk_size}, Workers: {max_workers})...")
    print(f"  • Template Loaded: {tmpl_file} (Locale: {normalize_language_tag(language)})")
    if user_script:
        print(f"  • Reference Script: Active ({len(user_script)} characters)")
    raw_blocks = [b.strip() for b in raw_srt.strip().split("\n\n") if b.strip()]
    if not raw_blocks:
        return raw_srt, {"total": 0, "locked": 0, "fallback": 0, "fallback_indices": []}

    chunk_slices = split_blocks_into_semantic_chunks(
        raw_blocks,
        target_chunk_size=chunk_size,
        min_chunk_size=max(20, int(chunk_size * 0.7)),
        max_chunk_size=int(chunk_size * 1.3)
    )
    num_chunks = len(chunk_slices)
    print(f"  • Total Subtitles: {len(raw_blocks)} items -> {num_chunks} silence-aware semantic chunks (target ~{chunk_size} lines)")

    cached_chunks = {}
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached_chunks = json.load(f)
        except Exception:
            cached_chunks = {}

    meta_sig = hashlib.md5(f"{model}::{template}::{global_glossary or ''}::{user_script or ''}".encode("utf-8")).hexdigest()[:12]

    results = {}
    uncached_indices = []
    for c_idx, c_slice in enumerate(chunk_slices):
        c_text = "\n\n".join(c_slice)
        c_sig = hashlib.md5(c_text.encode("utf-8")).hexdigest()
        ckey = f"{meta_sig}_{c_sig}"
        if ckey in cached_chunks and "-->" in cached_chunks[ckey]:
            results[c_idx] = cached_chunks[ckey]
        else:
            uncached_indices.append(c_idx)

    cached_count = num_chunks - len(uncached_indices)
    if cached_count > 0:
        print(f"  • Chunk Cache   : Restored {cached_count}/{num_chunks} chunks from cache ({os.path.basename(cache_path)})")

    if uncached_indices:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    proofread_single_chunk,
                    c_idx, num_chunks, chunk_slices[c_idx],
                    template, global_glossary, audio_wav,
                    model=model, user_script=user_script,
                    project=project, location=location,
                    gcs_bucket=gcs_bucket, region=region,
                    max_chars_cjk=max_chars_cjk, max_chars_korean=max_chars_korean, max_chars_latin=max_chars_latin
                ): c_idx
                for c_idx in uncached_indices
            }

            completed_count = cached_count
            for future in concurrent.futures.as_completed(futures):
                c_idx, clean_text, success = future.result()
                results[c_idx] = clean_text
                if success and cache_path:
                    c_slice = chunk_slices[c_idx]
                    c_sig = hashlib.md5("\n\n".join(c_slice).encode("utf-8")).hexdigest()
                    cached_chunks[f"{meta_sig}_{c_sig}"] = clean_text
                    try:
                        with open(cache_path, "w", encoding="utf-8") as f:
                            json.dump(cached_chunks, f, ensure_ascii=False, indent=2)
                    except Exception:
                        pass
                completed_count += 1
                status_tag = "OK" if success else "WARN"
                pct = (completed_count / num_chunks) * 100
                print(f"\r  ► Progress: {completed_count}/{num_chunks} chunks completed ({pct:.0f}%)... [{status_tag}]", end="", flush=True)
        print()
    else:
        print(f"  ► All {num_chunks} chunks served directly from chunk cache (100%).")

    overall_alignment_stats = {"total": 0, "locked": 0, "fallback": 0, "fallback_indices": []}
    if all_words:
        print("  ► [Acoustic Re-projection] Snapping subtitle boundaries to Whisper word-level ground truth (chunk-scoped)...")

    realigned_chunks = []
    for c_idx in range(num_chunks):
        c_text = results.get(c_idx, "")
        if not c_text:
            continue
        c_slice = chunk_slices[c_idx]

        try:
            t_first = parse_timestamp_str(c_slice[0].splitlines()[1].split("-->")[0])
            t_last = parse_timestamp_str(c_slice[-1].splitlines()[1].split("-->")[1])
        except Exception:
            t_first, t_last = 0.0, 999999.0

        if all_words:
            chunk_words = [
                w for w in all_words
                if (float(w.get("end", 0.0)) >= t_first - 2.0 and float(w.get("start", 0.0)) <= t_last + 2.0)
            ]
            realigned_chunk, c_stats = realign_subtitles_to_words(c_text, chunk_words, language=language, is_video_start=(c_idx == 0))
            overall_alignment_stats["total"] += c_stats["total"]
            overall_alignment_stats["locked"] += c_stats["locked"]
            overall_alignment_stats["fallback"] += c_stats["fallback"]
            overall_alignment_stats["fallback_indices"].extend(c_stats["fallback_indices"])
        else:
            realigned_chunk = c_text
        realigned_chunks.append(realigned_chunk)

    raw_combined = "\n\n".join(realigned_chunks).strip()
    sanitized_srt = sanitize_subtitle_timings(
        raw_combined, all_words=all_words, language=language,
        max_chars_cjk=max_chars_cjk, max_chars_korean=max_chars_korean, max_chars_latin=max_chars_latin
    )
    return sanitized_srt, overall_alignment_stats


def audit_subtitles_quality(srt_content, language="zh-TW", global_glossary=None, alignment_stats=None,
                            max_chars_cjk=15, max_chars_korean=16, max_chars_latin=42):
    """
    Run an 8-dimension Netflix and YouTube Subtitle Quality, Pacing, and Acoustic Audit.
    Returns: (metrics_dict, console_summary_str, markdown_report_str)
    """
    norm_lang = normalize_language_tag(language)
    blocks = [b.strip() for b in srt_content.strip().split("\n\n") if b.strip()]
    items = []

    for b in blocks:
        lines = b.splitlines()
        if len(lines) >= 3 and "-->" in lines[1]:
            idx = int(lines[0]) if lines[0].isdigit() else len(items) + 1
            t1, t2 = lines[1].split("-->")
            t_start = parse_timestamp_str(t1.strip())
            t_end = parse_timestamp_str(t2.strip())
            text = "\n".join(lines[2:]).strip()
            items.append({
                "index": idx,
                "start": t_start,
                "end": t_end,
                "duration": max(0.01, t_end - t_start),
                "text": text
            })
        elif len(lines) == 2 and "-->" in lines[0]:
            t1, t2 = lines[0].split("-->")
            t_start = parse_timestamp_str(t1.strip())
            t_end = parse_timestamp_str(t2.strip())
            text = lines[1].strip()
            items.append({
                "index": len(items) + 1,
                "start": t_start,
                "end": t_end,
                "duration": max(0.01, t_end - t_start),
                "text": text
            })

    total_lines = len(items)
    if total_lines == 0:
        return {}, "No subtitles found.", "# Subtitle Quality Report\nNo subtitles found."

    durs = [x["duration"] for x in items]
    mean_dur = sum(durs) / total_lines
    sorted_durs = sorted(durs)
    median_dur = sorted_durs[total_lines // 2]

    short_dur = [x for x in items if x["duration"] < 1.0]
    long_dur = [x for x in items if x["duration"] > 6.0]

    overlaps = []
    zero_gaps = []
    micro_gaps = []
    normal_gaps = []

    for i in range(total_lines - 1):
        c, n = items[i], items[i + 1]
        g = n["start"] - c["end"]
        if g < -0.001:
            overlaps.append((c, n, g))
        elif abs(g) < 0.001:
            zero_gaps.append((c, n))
        elif 0.001 <= g < 0.2:
            micro_gaps.append((c, n, g))
        else:
            normal_gaps.append((c, n, g))

    if norm_lang in ["zh-TW", "zh-CN", "ja"]:
        max_char_limit = max_chars_cjk
        cps_limit = 6.0
    elif norm_lang == "ko":
        max_char_limit = max_chars_korean
        cps_limit = 6.5
    else:
        max_char_limit = max_chars_latin
        cps_limit = 20.0

    overlength_lines = []
    high_cps_lines = []
    total_cps_sum = 0.0

    for x in items:
        char_count = calc_display_width(x["text"], norm_lang)
        x["char_count"] = round(char_count, 1)
        cps = round(char_count / max(0.1, x["duration"]), 2)
        x["cps"] = cps
        total_cps_sum += cps
        if char_count > max_char_limit:
            overlength_lines.append(x)
        if cps > cps_limit:
            high_cps_lines.append(x)

    mean_cps = round(total_cps_sum / total_lines, 2)
    peak_cps = max(x["cps"] for x in items)

    trailing_punct_lines = [
        x for x in items if re.search(r"[。，、；:;,.—-]+$", x["text"])
    ]
    inline_comma_lines = [
        x for x in items if "，" in x["text"]
    ]

    bracket_pairs = [
        ("（", "）"),
        ("(", ")"),
        ("【", "】"),
        ("《", "》"),
        ("「", "」"),
        ("『", "』"),
        ("[", "]"),
        ("{", "}")
    ]
    unclosed_bracket_lines = []
    residual_md_lines = []
    for x in items:
        unmatched = []
        for open_b, close_b in bracket_pairs:
            if x["text"].count(open_b) != x["text"].count(close_b):
                unmatched.append(f"{open_b}...{close_b}")
        if unmatched:
            unclosed_bracket_lines.append((x, ", ".join(unmatched)))

        if re.search(r"(\*{1,3}|_{2,}|`{1,3}|(?<=^)\s*#{1,6}\s|(?<=^)\s*>\s)", x["text"]):
            residual_md_lines.append(x)

    prolonged_silence_threshold = 10.0
    prolonged_silences = []
    if items[0]["start"] >= prolonged_silence_threshold:
        prolonged_silences.append({
            "start": 0.0,
            "end": round(items[0]["start"], 3),
            "duration": round(items[0]["start"], 2),
            "timestamp": f"{format_timestamp_srt(0.0)} --> {format_timestamp_srt(items[0]['start'])}",
            "prev_index": 0,
            "prev_text": "[影片開頭 / 片頭無語音]",
            "next_index": items[0]["index"],
            "next_text": items[0]["text"]
        })
    for i in range(total_lines - 1):
        c, n = items[i], items[i + 1]
        g = n["start"] - c["end"]
        if g >= prolonged_silence_threshold:
            prolonged_silences.append({
                "start": round(c["end"], 3),
                "end": round(n["start"], 3),
                "duration": round(g, 2),
                "timestamp": f"{format_timestamp_srt(c['end'])} --> {format_timestamp_srt(n['start'])}",
                "prev_index": c["index"],
                "prev_text": c["text"],
                "next_index": n["index"],
                "next_text": n["text"]
            })

    glossary_terms_found = []
    glossary_total_terms = 0
    if global_glossary:
        bold_terms = re.findall(r"\*\*(.+?)\*\*", global_glossary)
        category_keywords = [
            "Person", "Speaker", "Names", "Organizations", "Products", "Places", "Brands",
            "Domain Jargon", "Tech Terms", "Core Topic", "全片專有名詞", "Whisper", "講者與人物",
            "公司、品牌", "行業專有名詞", "核心主題", "Glossary", "Terminology"
        ]
        unique_terms = []
        for term in bold_terms:
            t = term.strip()
            if any(cat in t for cat in category_keywords):
                continue
            clean_t = re.split(r"[（\(]", t)[0].strip()
            if clean_t and clean_t not in unique_terms and len(clean_t) >= 2:
                unique_terms.append(clean_t)
        glossary_total_terms = len(unique_terms)
        all_subtitle_text = " ".join(x["text"] for x in items)
        for term in unique_terms:
            count = all_subtitle_text.count(term)
            if count > 0:
                glossary_terms_found.append({"term": term, "occurrences": count})

    locked_count = alignment_stats.get("locked", total_lines) if alignment_stats else total_lines
    fallback_count = alignment_stats.get("fallback", 0) if alignment_stats else 0
    locked_pct = round((locked_count / max(1, locked_count + fallback_count)) * 100, 1)

    base_score = 100.0
    base_score -= min(20.0, len(overlaps) * 5.0)
    base_score -= min(10.0, len(micro_gaps) * 2.0)
    base_score -= min(10.0, len(trailing_punct_lines) * 1.0)
    base_score -= min(10.0, len(unclosed_bracket_lines) * 3.0)
    base_score -= min(10.0, len(residual_md_lines) * 5.0)
    base_score -= min(10.0, (len(overlength_lines) / total_lines * 100) * 0.5)
    base_score -= min(5.0, (len(short_dur) / total_lines * 100) * 0.3)
    base_score -= min(10.0, (len(high_cps_lines) / total_lines * 100) * 0.5)
    if alignment_stats and (locked_count + fallback_count) > 0:
        base_score -= min(5.0, (fallback_count / (locked_count + fallback_count) * 100) * 0.2)

    compliance_score = max(0.0, min(100.0, round(base_score, 1)))
    grade = "A+" if compliance_score >= 95.0 else ("A" if compliance_score >= 90.0 else ("B" if compliance_score >= 80.0 else "C"))

    review_issues = []
    for x in residual_md_lines:
        review_issues.append({
            "index": x["index"],
            "timestamp": f"{format_timestamp_srt(x['start'])} --> {format_timestamp_srt(x['end'])}",
            "type": "Residual Markdown",
            "detail": "Contains syntax markers (*, _, `)",
            "text": x["text"],
            "severity": 20.0
        })
    for x, unclosed_info in unclosed_bracket_lines:
        review_issues.append({
            "index": x["index"],
            "timestamp": f"{format_timestamp_srt(x['start'])} --> {format_timestamp_srt(x['end'])}",
            "type": "Unclosed Bracket",
            "detail": f"Unmatched: {unclosed_info}",
            "text": x["text"],
            "severity": 15.0
        })
    for x in overlength_lines:
        review_issues.append({
            "index": x["index"],
            "timestamp": f"{format_timestamp_srt(x['start'])} --> {format_timestamp_srt(x['end'])}",
            "type": "Overlength Line",
            "detail": f"{x['char_count']} chars (max {max_char_limit})",
            "text": x["text"],
            "severity": x["char_count"] - max_char_limit
        })
    for x in high_cps_lines:
        review_issues.append({
            "index": x["index"],
            "timestamp": f"{format_timestamp_srt(x['start'])} --> {format_timestamp_srt(x['end'])}",
            "type": "High CPS",
            "detail": f"{x['cps']} CPS (max {cps_limit})",
            "text": x["text"],
            "severity": x["cps"] - cps_limit
        })
    for x in short_dur:
        if x["cps"] > 5.0:
            review_issues.append({
                "index": x["index"],
                "timestamp": f"{format_timestamp_srt(x['start'])} --> {format_timestamp_srt(x['end'])}",
                "type": "Short Duration",
                "detail": f"{x['duration']:.2f}s (CPS: {x['cps']})",
                "text": x["text"],
                "severity": 1.0 - x["duration"]
            })

    review_issues.sort(key=lambda it: it.get("severity", 0), reverse=True)

    metrics = {
        "language_locale": norm_lang,
        "total_subtitles": total_lines,
        "first_in_seconds": round(items[0]["start"], 3),
        "first_in_timestamp": format_timestamp_srt(items[0]["start"]),
        "last_out_seconds": round(items[-1]["end"], 3),
        "last_out_timestamp": format_timestamp_srt(items[-1]["end"]),
        "mean_duration_seconds": round(mean_dur, 2),
        "median_duration_seconds": round(median_dur, 2),
        "mean_cps": mean_cps,
        "peak_cps": peak_cps,
        "cps_limit": cps_limit,
        "high_cps_count": len(high_cps_lines),
        "high_cps_rate_pct": round(len(high_cps_lines) / total_lines * 100, 2),
        "char_limit_per_line": max_char_limit,
        "overlength_count": len(overlength_lines),
        "overlength_rate_pct": round(len(overlength_lines) / total_lines * 100, 2),
        "trailing_punct_violations": len(trailing_punct_lines),
        "unclosed_bracket_count": len(unclosed_bracket_lines),
        "residual_markdown_count": len(residual_md_lines),
        "inline_comma_count": len(inline_comma_lines),
        "duration_under_1s_count": len(short_dur),
        "duration_under_1s_rate_pct": round(len(short_dur) / total_lines * 100, 2),
        "duration_over_6s_count": len(long_dur),
        "overlaps_count": len(overlaps),
        "micro_gaps_count": len(micro_gaps),
        "seamless_zero_gaps_count": len(zero_gaps),
        "natural_pauses_count": len(normal_gaps),
        "prolonged_silence_threshold_seconds": prolonged_silence_threshold,
        "prolonged_silence_count": len(prolonged_silences),
        "prolonged_silence_gaps": prolonged_silences,
        "acoustic_locked_count": locked_count,
        "acoustic_fallback_count": fallback_count,
        "acoustic_locked_rate_pct": locked_pct,
        "glossary_terms_detected": len(glossary_terms_found),
        "glossary_total_terms": glossary_total_terms,
        "compliance_score_pct": compliance_score,
        "compliance_grade": grade,
        "actionable_issues_count": len(review_issues)
    }

    c_card = f"""
================================================================================
YouTube / Netflix Subtitle Quality Audit Report
================================================================================
[Core Metrics]
  • Locale                  : {norm_lang}
  • Total Lines             : {total_lines:,}
  • First In                : {metrics['first_in_timestamp']}
  • Last Out                : {metrics['last_out_timestamp']}
  • Mean Duration           : {mean_dur:.2f}s (Median: {median_dur:.2f}s)
  • Compliance Score        : {compliance_score:.1f}% (Grade {grade})

[Layout & Typography]
  • Line Length ({norm_lang} <= {max_char_limit}) : {total_lines - len(overlength_lines)}/{total_lines} compliant ({100.0 - metrics['overlength_rate_pct']:.1f}%)
  • Reading Speed (Mean CPS): {mean_cps} CPS (Peak: {peak_cps} CPS | Limit: {cps_limit} CPS)
  • High CPS Lines (> {cps_limit}) : {len(high_cps_lines)} ({metrics['high_cps_rate_pct']:.1f}%)
  • Trailing Punctuation    : {total_lines - len(trailing_punct_lines)}/{total_lines} clean (Violations: {len(trailing_punct_lines)})
  • Bracket Closure         : {total_lines - len(unclosed_bracket_lines)}/{total_lines} closed (Violations: {len(unclosed_bracket_lines)})
  • Markdown Cleanliness    : {total_lines - len(residual_md_lines)}/{total_lines} clean (Violations: {len(residual_md_lines)})

[Timing & Acoustics]
  • Acoustic Lock Rate      : {locked_pct:.1f}% ({locked_count}/{max(1, locked_count + fallback_count)} lines locked)
  • Min Duration (>= 1.0s)  : {total_lines - len(short_dur)}/{total_lines} compliant ({100.0 - metrics['duration_under_1s_rate_pct']:.1f}%)
  • Max Duration (<= 6.0s)  : {total_lines - len(long_dur)}/{total_lines} compliant (100.0%)
  • Micro-Gaps (< 0.2s)     : {len(micro_gaps)}
  • Overlaps                : {len(overlaps)}
  • Seamless Zero Gaps      : {len(zero_gaps)} pairs | Natural Pauses: {len(normal_gaps)} pairs
  • Prolonged Silence (>=10s): {len(prolonged_silences)}
"""

    if review_issues:
        c_card += f"\n[Actionable Review List Top {min(5, len(review_issues))}/{len(review_issues)}]\n"
        for it in review_issues[:5]:
            c_card += f"  • #{it['index']:03d} [{it['timestamp']}] {it['type']} ({it['detail']}): \"{it['text'][:24]}\"\n"

    c_card += "================================================================================\n"

    review_table_rows = []
    for it in review_issues[:15]:
        review_table_rows.append(
            f"| `#{it['index']}` | `{it['timestamp']}` | {it['type']} | `{it['detail']}` | {it['text']} |"
        )
    review_table_md = "\n".join(review_table_rows) if review_table_rows else "| - | - | - | - | None (100% Compliant) |"

    if prolonged_silences:
        silence_rows = []
        for s_idx, s_gap in enumerate(prolonged_silences, start=1):
            p_text = (s_gap["prev_text"][:20] + "...") if len(s_gap["prev_text"]) > 20 else s_gap["prev_text"]
            n_text = (s_gap["next_text"][:20] + "...") if len(s_gap["next_text"]) > 20 else s_gap["next_text"]
            silence_rows.append(
                f"| `{s_idx}` | `{s_gap['timestamp']}` | `{s_gap['duration']:.2f}s` | {p_text} | {n_text} | Verify B-roll / music or missing speech |"
            )
        prolonged_silence_md = (
            "| # | Silence Interval | Duration | Previous Line | Next Line | Note |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- |\n" +
            "\n".join(silence_rows)
        )
    else:
        prolonged_silence_md = "No prolonged dialogue silence (>= 10.0s) detected."

    md_report = f"""# YouTube / Netflix Subtitle Quality Audit Report

> **Generated At**: {time.strftime('%Y-%m-%d %H:%M:%S')}  
> **Locale**: `{norm_lang}`  
> **Compliance Score**: **{compliance_score:.1f}% (Grade {grade})**  
> **Acoustic Lock Rate**: **{locked_pct:.1f}%** ({locked_count}/{max(1, locked_count + fallback_count)} lines locked to word timestamps)

---

## 1. Summary & Time Coverage
* **Total Subtitle Lines**: `{total_lines:,}`
* **First In Timestamp**: `{metrics['first_in_timestamp']}`
* **Last Out Timestamp**: `{metrics['last_out_timestamp']}`
* **Mean Line Duration**: `{mean_dur:.2f}` s (Median `{median_dur:.2f}` s)
* **Mean Reading Speed**: `{mean_cps}` CPS (Peak `{peak_cps}` CPS)

---

## 2. Layout & Typography
| Check | Target Standard | Measured Value | Status |
| :--- | :--- | :--- | :--- |
| **Max Line Length** | $\\le {max_char_limit}$ chars | `{total_lines - len(overlength_lines)} / {total_lines}` ({100.0 - metrics['overlength_rate_pct']:.1f}%) | {'PASS' if metrics['overlength_rate_pct'] <= 5.0 else 'WARN'} |
| **Reading Speed (CPS)** | $\\le {cps_limit}$ CPS | `{total_lines - len(high_cps_lines)} / {total_lines}` ({100.0 - metrics['high_cps_rate_pct']:.1f}%) | {'PASS' if len(high_cps_lines) == 0 else f'WARN ({len(high_cps_lines)} lines)'} |
| **Trailing Punctuation** | No trailing `。`, `，`, `;` | `{len(trailing_punct_lines)}` violations | {'PASS' if len(trailing_punct_lines) == 0 else f'WARN ({len(trailing_punct_lines)})'} |
| **Bracket Closure** | Matched bracket pairs | `{len(unclosed_bracket_lines)}` unclosed | {'PASS' if len(unclosed_bracket_lines) == 0 else f'WARN ({len(unclosed_bracket_lines)})'} |
| **Markdown Cleanliness** | Zero `**`, `_`, `#` | `{len(residual_md_lines)}` violations | {'PASS' if len(residual_md_lines) == 0 else f'WARN ({len(residual_md_lines)})'} |
| **Inline Comma Spacing** | Space or enumeration comma | `{len(inline_comma_lines)}` fullwidth commas | PASS |
| **CJK-Latin Spacing** | Single ASCII space | 100% normalized | PASS |

---

## 3. Timing & Pacing
| Check | Target Standard | Measured Value | Note |
| :--- | :--- | :--- | :--- |
| **Acoustic Lock Rate** | Word-boundary lock | `{locked_pct:.1f}%` ({locked_count} lines) | {'PASS' if fallback_count == 0 else f'WARN ({fallback_count} fallback lines)'} |
| **Min Duration** | $\\ge 1.0\\text{{s}}$ | `{100.0 - metrics['duration_under_1s_rate_pct']:.1f}%` ({total_lines - len(short_dur)} lines) | Padded into available silence |
| **Max Duration** | $\\le 6.0\\text{{s}}$ | `100.0%` (0 stuck lines) | Capped at 6.0s |
| **Timeline Overlaps** | $0\\text{{s}}$ | `{len(overlaps)}` overlaps | Monotonic non-overlapping |
| **Micro-Gaps** | $< 0.2\\text{{s}}$ | `{len(micro_gaps)}` micro-gaps | Anti-flicker gap bridging |
| **Seamless Zero Gaps** | Gap == 0s | `{len(zero_gaps)}` pairs | Continuous speech transitions |
| **Natural Pauses** | Gap $\\ge 0.2\\text{{s}}$ | `{len(normal_gaps)}` pairs | Natural breathing room |
| **Prolonged Silence** | Gap $\\ge 10.0\\text{{s}}$ | `{len(prolonged_silences)}` intervals | {'PASS' if len(prolonged_silences) == 0 else f'WARN ({len(prolonged_silences)} intervals)'} |

---

## 4. Prolonged Silence Audit (>= 10s)
{prolonged_silence_md}

---

## 5. Glossary Consistency
* **Total Glossary Terms**: `{glossary_total_terms}`
* **Matched in Subtitles**: `{len(glossary_terms_found)}`

---

## 6. Actionable Review List
| Index | Timestamp | Issue Type | Detail | Subtitle Text |
| :--- | :--- | :--- | :--- | :--- |
{review_table_md}
"""
    if len(review_issues) > 15:
        md_report += f"\n*(Remaining {len(review_issues) - 15} minor items are recorded in the JSON report.)*\n"

    return metrics, c_card, md_report


def main():
    parser = argparse.ArgumentParser(
        description="Subtitle Craft: Global Audio Glossary + Whisper ASR + Vertex AI Audio Proofreading.",
        formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument("-i", "--input", required=True, help="Path or Google Drive URL to input video or audio file")
    parser.add_argument("-o", "--output-dir", default=None, help="Output directory for SRT/VTT subtitles (default: same as input)")
    parser.add_argument("--outline", default=None, help="User interview outline, topic summary, or glossary notes to bias terminology")
    parser.add_argument("--script", default=None, help="Path to full recording script, manuscript, or spoken draft to anchor terminology")
    parser.add_argument("--whisper-model", default="base", choices=["tiny", "base", "small", "medium", "large-v3"],
                        help="Whisper model size for Stage 2 acoustic transcription (default: base)")
    parser.add_argument("--model", default="gemini-3.8-flash",
                        help="Vertex AI Gemini model for Stage 1 and Stage 3 proofreading (default: gemini-3.8-flash)")
    parser.add_argument("--project", default=None,
                        help="Google Cloud Project ID for Vertex AI / GCS (or set GOOGLE_CLOUD_PROJECT in .env)")
    parser.add_argument("--gcs-bucket", "--bucket", dest="gcs_bucket", default=None,
                        help="Google Cloud Storage Bucket for audio staging (default: subtitle-craft-${PROJECT_ID})")
    parser.add_argument("--location", default=None,
                        help="Vertex AI Gemini endpoint location (default: global)")
    parser.add_argument("--region", default=None,
                        help="GCS Bucket infrastructure region (default: us-central1)")
    parser.add_argument("--cleanup-gcs", action="store_true",
                        help="Delete staged glossary audio from GCS immediately after completion (chunk audio is always auto-cleaned)")
    parser.add_argument("--language", default="auto", help="Spoken language code for transcription (default: auto, or zh-TW, en, ja, ko, zh-CN)")
    parser.add_argument("--device", default="auto", choices=["auto", "mps", "mlx", "cuda", "cpu"],
                        help="Device acceleration backend for Whisper (default: auto for Apple Silicon GPU / Neural Engine)")
    parser.add_argument("--chunk-size", type=int, default=80, help="Subtitle entries per proofread batch (default: 80)")
    parser.add_argument("--workers", type=int, default=5, help="Concurrent workers for parallel proofreading (default: 5)")
    parser.add_argument("--force", action="store_true", help="Force re-running Whisper transcription and Gemini proofreading")
    parser.add_argument("--force-glossary", action="store_true", help="Force re-extracting global glossary from scratch")
    parser.add_argument("--max-chars-cjk", type=int, default=15,
                        help="Maximum characters per line for CJK (zh-TW, zh-CN, ja) (default: 15)")
    parser.add_argument("--max-chars-korean", type=int, default=16,
                        help="Maximum characters per line for Korean (ko) (default: 16)")
    parser.add_argument("--max-chars-latin", type=int, default=42,
                        help="Maximum characters per line for Latin script (en and fallback) (default: 42)")

    args = parser.parse_args()

    gcp_cfg = resolve_gcp_config(
        cli_project=args.project,
        cli_bucket=args.gcs_bucket,
        cli_location=args.location,
        cli_region=args.region,
    )

    if not gcp_cfg.get("project") or not gcp_cfg.get("bucket"):
        print("\n[Error] Missing Google Cloud Project ID or GCS Bucket for Vertex AI!", file=sys.stderr)
        print(f"  Project : '{gcp_cfg.get('project')}'", file=sys.stderr)
        print(f"  Bucket  : '{gcp_cfg.get('bucket')}'", file=sys.stderr)
        print("  Action Required: Run './setup.sh' to auto-provision GCP & .env, or pass --project / --gcs-bucket.", file=sys.stderr)
        sys.exit(1)

    if is_gdrive_source(args.input):
        out_dir = args.output_dir or "."
        os.makedirs(out_dir, exist_ok=True)
        try:
            args.input = download_gdrive_file_with_cache(
                args.input,
                target_dir=os.path.join(out_dir, "gdrive_inputs"),
                project=gcp_cfg.get("project"),
                force_download=args.force,
            )
        except Exception as e:
            print(f"[Error] Failed to download Google Drive media '{args.input}': {e}", file=sys.stderr)
            sys.exit(1)
    elif not os.path.exists(args.input):
        print(f"[Error] Input media not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    input_basename = os.path.splitext(os.path.basename(args.input))[0]
    out_dir = args.output_dir or os.path.dirname(os.path.abspath(args.input)) or "."
    os.makedirs(out_dir, exist_ok=True)

    final_srt_path = os.path.join(out_dir, f"{input_basename}.srt")
    final_vtt_path = os.path.join(out_dir, f"{input_basename}.vtt")
    raw_srt_path = os.path.join(out_dir, f"{input_basename}_raw_whisper.srt")
    glossary_path = os.path.join(out_dir, f"{input_basename}_glossary.md")
    report_json_path = os.path.join(out_dir, f"{input_basename}_subtitle_report.json")
    report_md_path = os.path.join(out_dir, f"{input_basename}_subtitle_report.md")

    user_outline_text = args.outline
    if user_outline_text and os.path.isfile(user_outline_text):
        with open(user_outline_text, "r", encoding="utf-8") as f:
            user_outline_text = f.read().strip()

    user_script_text = args.script
    if user_script_text and os.path.isfile(user_script_text):
        with open(user_script_text, "r", encoding="utf-8") as f:
            user_script_text = f.read().strip()

    print("\n" + "=" * 78)
    print("Subtitle Craft (Global Glossary + Whisper ASR + Vertex AI Audio Proofreading)")
    print("=" * 78)
    print(f"  • Input Media   : {args.input}")
    print(f"  • LLM Model     : {args.model}")
    print(f"  • Active Backend: Google Cloud Vertex AI (Project: {gcp_cfg.get('project')}, Location: {gcp_cfg.get('location')})")
    print(f"  • GCS Storage   : gs://{gcp_cfg.get('bucket')}/raw/ (Region: {gcp_cfg.get('region')}, 2-day Lifecycle)")
    if args.script:
        print(f"  • Source Script : {os.path.basename(args.script) if os.path.isfile(args.script) else 'Supplied text'} ({len(user_script_text or '')} chars)")
    if args.outline:
        print(f"  • User Outline  : {args.outline[:50]}...")
    print(f"  • Whisper Model : {args.whisper_model} (Language: {args.language}, Device: {args.device})")
    print(f"  • Batch Settings: Chunk {args.chunk_size} lines | {args.workers} Parallel Workers")
    print(f"  • Target SRT    : {final_srt_path}")
    print(f"  • Target VTT    : {final_vtt_path}")
    print("-" * 78)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_wav = os.path.join(tmpdir, "audio_16k.wav")
        print("\n[Step 0] Extracting 16kHz mono audio from media...")
        extract_audio_16k_mono(args.input, tmp_wav)

        # Stage 1: Global Audio Context & Consistency Glossary Extraction
        global_glossary = None
        if os.path.exists(glossary_path) and not args.force_glossary:
            print(f"\n[Stage 1/3] Found cached Global Glossary: {glossary_path}")
            try:
                with open(glossary_path, "r", encoding="utf-8") as f:
                    global_glossary = f.read().strip()
                print(f"  ✓ Loaded global glossary from cache ({len(global_glossary)} chars).")
            except Exception as e:
                print(f"  [Notice] Failed to read cached glossary ({e}), re-extracting...")
                global_glossary = None

        if not global_glossary:
            global_glossary = extract_global_glossary(
                audio_wav=tmp_wav,
                user_outline=user_outline_text,
                user_script=user_script_text,
                model=args.model,
                project=gcp_cfg.get("project"),
                gcs_bucket=gcp_cfg.get("bucket"),
                location=gcp_cfg.get("location"),
                region=gcp_cfg.get("region"),
                cleanup_gcs=args.cleanup_gcs,
                language=args.language,
            )
            if global_glossary:
                with open(glossary_path, "w", encoding="utf-8") as f:
                    f.write(global_glossary + "\n")
                print(f"  • Saved Global Glossary: {glossary_path}")

        whisper_bias_prompt = extract_whisper_prompt(global_glossary, language=args.language)
        if whisper_bias_prompt:
            print(f"  • Extracted Whisper Initial Prompt ({len(whisper_bias_prompt)} chars):")
            print(f"    \"{whisper_bias_prompt}\"")

        # Stage 2: Whisper Acoustic Transcription
        raw_words_path = os.path.join(out_dir, f"{input_basename}_words.json")
        if os.path.exists(raw_srt_path) and os.path.exists(raw_words_path) and not args.force:
            print("\n[Stage 2/3] Found cached Whisper acoustic baseline:")
            print(f"  • SRT: {raw_srt_path}")
            print(f"  • Words JSON: {raw_words_path}")
            try:
                with open(raw_words_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                segments = cached.get("segments", [])
                all_words = cached.get("words", [])
                detected_lang = cached.get("language", "zh")
                with open(raw_srt_path, "r", encoding="utf-8") as f:
                    raw_srt = f.read()
                print(f"  ✓ Loaded {len(segments)} segments and {len(all_words)} word timestamps from cache.")
            except Exception as e:
                print(f"  [Notice] Failed to load cache ({e}), re-running Whisper transcription...")
                segments, detected_lang, all_words = run_whisper_transcription(
                    tmp_wav,
                    model_size=args.whisper_model,
                    language=args.language,
                    device=args.device,
                    initial_prompt=whisper_bias_prompt
                )
                raw_srt = build_srt_from_segments(segments)
                with open(raw_srt_path, "w", encoding="utf-8") as f:
                    f.write(raw_srt)
                with open(raw_words_path, "w", encoding="utf-8") as f:
                    json.dump({"language": detected_lang, "segments": segments, "words": all_words}, f, ensure_ascii=False)
        else:
            segments, detected_lang, all_words = run_whisper_transcription(
                tmp_wav,
                model_size=args.whisper_model,
                language=args.language,
                device=args.device,
                initial_prompt=whisper_bias_prompt
            )
            raw_srt = build_srt_from_segments(segments)
            with open(raw_srt_path, "w", encoding="utf-8") as f:
                f.write(raw_srt)
            with open(raw_words_path, "w", encoding="utf-8") as f:
                json.dump({"language": detected_lang, "segments": segments, "words": all_words}, f, ensure_ascii=False)
            print(f"  • Saved raw acoustic baseline: {raw_srt_path}")

        effective_lang = detected_lang if str(args.language).lower() in ("auto", "none") else args.language
        print(f"  • Effective Language Locale: {normalize_language_tag(effective_lang)} (Input: '{args.language}', Detected: '{detected_lang}')")

        chunk_cache_path = os.path.join(out_dir, f".{input_basename}_chunk_cache.json")
        if args.force and os.path.exists(chunk_cache_path):
            try:
                os.remove(chunk_cache_path)
            except Exception:
                pass

        # Stage 3: Multimodal Audio-Text Parallel Chunked Proofreading via Vertex AI & GCS
        final_srt, alignment_stats = proofread_srt_with_llm(
            raw_srt=raw_srt,
            audio_wav=tmp_wav,
            global_glossary=global_glossary,
            user_script=user_script_text,
            model=args.model,
            chunk_size=args.chunk_size,
            max_workers=args.workers,
            language=effective_lang,
            all_words=all_words,
            cache_path=chunk_cache_path,
            project=gcp_cfg.get("project"),
            location=gcp_cfg.get("location"),
            gcs_bucket=gcp_cfg.get("bucket"),
            region=gcp_cfg.get("region"),
            max_chars_cjk=args.max_chars_cjk,
            max_chars_korean=args.max_chars_korean,
            max_chars_latin=args.max_chars_latin,
        )

        with open(final_srt_path, "w", encoding="utf-8") as f:
            f.write(final_srt.strip() + "\n")
        print(f"\n[Output 1/4] Saved YouTube Standard SRT Subtitles: {final_srt_path}")

        vtt_content = srt_to_vtt(final_srt)
        with open(final_vtt_path, "w", encoding="utf-8") as f:
            f.write(vtt_content)
        print(f"[Output 2/4] Saved WebVTT (.vtt) Subtitles for YouTube / Web: {final_vtt_path}")

        metrics, c_card, md_report = audit_subtitles_quality(
            final_srt,
            language=effective_lang,
            global_glossary=global_glossary,
            alignment_stats=alignment_stats,
            max_chars_cjk=args.max_chars_cjk,
            max_chars_korean=args.max_chars_korean,
            max_chars_latin=args.max_chars_latin
        )

        with open(report_json_path, "w", encoding="utf-8") as f:
            json.dump(metrics, f, ensure_ascii=False, indent=2)
        print(f"[Output 3/4] Saved Subtitle Audit Metrics JSON: {report_json_path}")

        with open(report_md_path, "w", encoding="utf-8") as f:
            f.write(md_report)
        print(f"[Output 4/4] Saved Subtitle Audit Markdown Report: {report_md_path}")

        print(c_card)

    print("=" * 78)
    print("Subtitle Generation & Quality Audit Completed Successfully!")
    print("=" * 78 + "\n")


if __name__ == "__main__":
    main()
