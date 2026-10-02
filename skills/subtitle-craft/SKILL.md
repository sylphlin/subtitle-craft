---
name: subtitle-craft
description: >
  Broadcast-grade YouTube & Netflix subtitle generator (SRT & VTT) adhering to the Agent Plugins 1.0 Specification.
  Executes a 3-Stage Golden Pipeline: (1) Vertex AI Gemini 3.8 Flash 1M Context Global Glossary & Whisper Initial Prompt extraction,
  (2) Zero-drift Whisper word-level acoustic transcription (Apple Silicon MLX / faster-whisper with word_timestamps=True),
  and (3) Silence-aware chunked multimodal audio proofreading via Google Cloud Vertex AI (ADC) + GCS with physical word-boundary
  timestamp reprojection, rhythm sanitization, and an 8-dimension streaming quality audit.
  Keywords: subtitle-craft, subtitles, youtube-subtitles, netflix-subtitles, srt, vtt, whisper, mlx-whisper, faster-whisper, gemini, vertex-ai, gcs, adc, subtitle-proofreading, acoustic-alignment, subtitle-audit.
---

# Subtitle Craft — Broadcast-Grade AI Subtitle Suite (Antigravity Native Skill)

Standalone end-to-end toolkit for generating millisecond-accurate, terminology-verified **YouTube and Netflix subtitles (`.srt` and `.vtt`)** from local or Google Drive video and audio files. Powered by **Google Cloud Vertex AI Gemini 3.8 Flash (1M Token Context via ADC + GCS)** and **Whisper Word-Level Acoustic Ground Truth**.

---

## Prerequisites & Environment

- **Google Antigravity IDE / Agent Framework**
- **FFmpeg** (for 16 kHz mono PCM WAV and 48 kbps MP3 audio extraction)
- **Python 3.9+** with `google-genai`, `google-cloud-storage`, `google-auth`, `requests`, and a Whisper backend (`mlx-whisper` on Apple Silicon or `faster-whisper`)
- **Cloud Credentials (100% ADC + Vertex AI & GCS)**:
  - Authenticate via `gcloud auth application-default login`
  - Run `./setup.sh` once to provision the GCS bucket (`gs://subtitle-craft-${GOOGLE_CLOUD_PROJECT}`), two-tier Lifecycle auto-cleanup rules (`raw/` staging: 2 days; `output/`, `deliverables/`: 15 days), Vertex AI Service Agent IAM (`roles/storage.objectUser`), and `.env` configuration.

---

## Modular Toolset Architecture

| Component | Path (`skills/subtitle-craft/`) | Function |
| :--- | :--- | :--- |
| **CLI Entrypoint** | `scripts/generate_subtitles.py` | Executes the 3-Stage Golden Subtitle Pipeline and 8-Dimension Quality Audit |
| **Vertex AI Client** | `scripts/modules/llm_client.py` | Vertex AI Gemini 3.8 Flash multimodal inference via ADC and GCS `gs://` URIs with HTTP 429 retry |
| **GCP & Drive Client** | `scripts/modules/gcp_client.py` | GCS SHA-256 cached upload, ephemeral chunk cleanup, and 3-Tier Google Drive download |
| **Progress Utility** | `scripts/modules/progress.py` | Live terminal spinner and elapsed timer |
| **Locale Templates** | `assets/subtitle_proofread_template.{zh-TW,zh-CN,en,ja,ko}.md` | Language-specific Netflix & YouTube segmentation, typography, and proofreading rules |

---

## Core Technical Principles

1. **Stage 1 — Global Audio Context & Consistency Glossary (Vertex AI 1M Context Scan, Strict Fail-Fast)**:
   - Compresses the full episode audio to 48 kbps mono MP3, stages it to `gs://subtitle-craft-${PROJECT_ID}/raw/`, and scans the entire recording with **Gemini 3.8 Flash** to build `<BASENAME>_glossary.md` and a compact Whisper `initial_prompt` ($\le 145$ chars).
   - Optionally merges user-provided outlines (`--outline`) and reference manuscripts (`--script`). Terminates immediately with exit code `1` if GCS staging or Vertex AI inference fails.
2. **Stage 2 — Zero-Drift Acoustic Ground Truth (`word_timestamps=True`, Default Model: `small`)**:
   - Runs `mlx-whisper` (Apple Silicon Metal GPU / Neural Engine) or `faster-whisper` (`int8` multi-core vectorization) with `--whisper-model small` by default, biased with the Stage 1 `initial_prompt`.
   - Caches raw segments (`<BASENAME>_raw_whisper.srt`) and word-level timestamps (`<BASENAME>_words.json`) in `<OUTPUT_DIR>` (`<input_dir>/output/` by default) so subsequent proofreading re-runs skip ASR.
3. **Stage 3 — Silence-Aware Multimodal Proofreading & Resilient Non-Cascading Acoustic Re-Projection**:
   - Splits raw SRT blocks at natural speech pauses ($\text{gap} \ge 0.4\text{s}$), slices the corresponding audio chunks, stages them to `gs://<BUCKET>/raw/audio_chunks/`, and proofreads homophones, terminology, and clause boundaries in parallel (canceling pending futures and exiting with code `1` on any cloud failure).
   - Deletes remote audio chunks in `finally` blocks immediately after each chunk finishes.
   - Re-projects proofread subtitle lines onto Whisper's physical word-level character timeline (`realign_subtitles_to_words`) using an **elastic bidirectional search window** (`cur_char_idx - 15` lookback), **conservative fallback progression** (`+ L` chars without timestamp-skipping), **re-synchronization anchoring** (`cur_char_idx = m_end + 1`), and **`source_bounds` enclosure** so a single mismatched line never cascades to subsequent subtitles.
   - Applies broadcast rhythm sanitization (`sanitize_subtitle_timings`: 180 ms lead-in pre-roll, $+0.4\text{s}$ post-tail reading buffer capped at `media_duration + 0.4s`, $<0.2\text{s}$ micro-gap bridging, $1.0\text{s}\text{–}6.0\text{s}$ duration guards).
4. **8-Dimension Netflix & YouTube Streaming Quality Audit + `agent_verdict` Quality Gate**:
   - Evaluates line length limits (`zh-TW`/`zh-CN`/`ja` $\le 15$, `ko` $\le 16$, `en` $\le 42$), reading speed (CPS), trailing punctuation cleanliness, bracket closure, Markdown residue, acoustic lock rate ($\ge 80.0\%$), timing overlaps/micro-gaps, prolonged silences ($\ge 10\text{s}$), and glossary coverage.
   - Outputs `<BASENAME>_subtitle_report.md` and `<BASENAME>_subtitle_report.json` containing the top-level `agent_verdict` object (`pass_quality_gate`, `acoustic_lock_rate_pct`, `last_out_vs_duration_diff_sec`, `fatal_violations`, `suggested_action`).

---

## 3-Step Gated Execution Runbook

Resolve `<PLUGIN_ROOT>` as two directory levels above `skills/subtitle-craft/SKILL.md` (`../../`, e.g., `/Users/sylph/.gemini/config/plugins/subtitle-craft` or the repository root).

### Step 1: Environment & Cloud Auth Verification
Verify that FFmpeg, `gcloud` ADC credentials, and `.env` configuration (`GOOGLE_CLOUD_PROJECT`, `SUBTITLE_CRAFT_BUCKET` / `GCS_BUCKET`) are ready in `<PLUGIN_ROOT>`. If missing, instruct the user to run `./setup.sh --project YOUR_PROJECT_ID` or `gcloud auth application-default login`.

### Step 2: Execute the 3-Stage Subtitle Pipeline
Set `Cwd` to `<PLUGIN_ROOT>` and run `skills/subtitle-craft/scripts/generate_subtitles.py` directly via `run_command` (when `-o` is omitted, all outputs are automatically isolated in `<input_dir>/output/`):

```bash
# Standard Execution (Local Video/Audio or Google Drive Link):
python3 skills/subtitle-craft/scripts/generate_subtitles.py \
  -i <INPUT_VIDEO_OR_AUDIO_OR_GDRIVE_URL> \
  --language <auto|zh-TW|zh-CN|en|ja|ko> \
  --strict

# With Optional Interview Outline or Recording Script:
python3 skills/subtitle-craft/scripts/generate_subtitles.py \
  -i <INPUT_VIDEO_OR_AUDIO_OR_GDRIVE_URL> \
  --language <auto|zh-TW|zh-CN|en|ja|ko> \
  --outline "<OUTLINE_TEXT_OR_FILE>" \
  --script "<SCRIPT_TEXT_OR_FILE>" \
  --strict
```

### Step 3: Deliverable Exit Gate & One-Shot Self-Healing Verification
1. Verify that all deliverable files exist in `<OUTPUT_DIR>` (`<input_dir>/output/` by default) and are non-empty (`> 0 bytes`):
   - `<BASENAME>.srt` (Final broadcast-ready SubRip subtitles)
   - `<BASENAME>.vtt` (Final WebVTT subtitles)
   - `<BASENAME>_glossary.md` (Stage 1 Global Terminology Glossary)
   - `<BASENAME>_raw_whisper.srt` & `<BASENAME>_words.json` (Stage 2 Whisper acoustic ground truth)
   - `<BASENAME>_subtitle_report.md` & `<BASENAME>_subtitle_report.json` (8-Dimension Quality Audit Report)
2. Inspect `agent_verdict` in `<BASENAME>_subtitle_report.json`:
   - **`pass_quality_gate: true` (`suggested_action: "DELIVER"`)**: Present the audit metrics and deliverable file paths to the user.
   - **`pass_quality_gate: false` (`suggested_action: "ONE_SHOT_REMEDIATE"` / Exit Code `2`)**: Execute **at most ONE** automated self-healing retry with `--whisper-model small --force`. If the second run still fails the quality gate, stop immediately and report `[Degraded]` along with `fatal_violations` and the actionable review table to the user.

---

## CLI Options Reference (`generate_subtitles.py`)

| Option | Default | Description |
| :--- | :--- | :--- |
| `-i`, `--input` | *(Required)* | Path or Google Drive URL to input video or audio file |
| `-o`, `--output-dir` | `<input_dir>/output` | Output directory for SRT/VTT subtitles and audit reports |
| `--outline` | `None` | User interview outline, topic summary, or glossary notes file/text |
| `--script` | `None` | Path or text of full recording script or manuscript to anchor terminology |
| `--language` | `auto` | Spoken language code (`auto`, `zh-TW`, `zh-CN`, `en`, `ja`, `ko`) |
| `--whisper-model` | `small` | Whisper model size for Stage 2 acoustic transcription (`tiny`, `base`, `small`, `medium`, `large-v3`) |
| `--model` | `gemini-3.8-flash` | Vertex AI Gemini model for Stage 1 and Stage 3 proofreading |
| `--device` | `auto` | Device acceleration backend (`auto`, `mps`, `mlx`, `cuda`, `cpu`) |
| `--chunk-size` | `80` | Subtitle entries per proofread batch |
| `--workers` | `5` | Concurrent workers for parallel proofreading |
| `--strict` | `False` | Exit with code `2` if `agent_verdict.pass_quality_gate` is `False` |
| `--force` | `False` | Force re-running Whisper transcription and Gemini proofreading |
| `--force-glossary` | `False` | Force re-extracting Stage 1 global glossary from scratch |
| `--cleanup-gcs` | `False` | Delete staged glossary audio from GCS immediately after completion |
| `--project` | `.env` | Google Cloud Project ID (`GOOGLE_CLOUD_PROJECT`) |
| `--gcs-bucket`, `--bucket` | `.env` | GCS Bucket for audio staging (`SUBTITLE_CRAFT_BUCKET` / `GCS_BUCKET`) |
| `--location` | `global` | Vertex AI Gemini endpoint location |
| `--region` | `us-central1` | GCS Bucket infrastructure region |
| `--max-chars-cjk` | `15` | Maximum characters per line for CJK (`zh-TW`, `zh-CN`, `ja`) |
| `--max-chars-korean` | `16` | Maximum characters per line for Korean (`ko`) |
| `--max-chars-latin` | `42` | Maximum characters per line for Latin script (`en` and fallback) |

