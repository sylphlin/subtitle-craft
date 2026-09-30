# Subtitle Craft — Workspace & Development Rules (AGENTS.md)

This file defines the authoritative rules for AI Coding Agents (Google Antigravity / Jetski, Claude Code, Codex, etc.) working in this repository. It covers both **Client Execution Invariants** (when running the subtitle generation pipeline for end users) and **Repository Engineering Standards** (when developing, maintaining, or extending this project).

---

## Part I: Operational Invariants (When Executing Subtitle Craft Tasks)

1. **Strict Toolset Execution Only (No Ad-Hoc Scripts)**:
   - Execute all glossary extraction, Whisper acoustic transcription, multimodal subtitle proofreading, timestamp reprojection, and quality auditing exclusively via `skills/subtitle-craft/scripts/generate_subtitles.py` (symlinked at `scripts/generate_subtitles.py` and `subtitle_craft.py` at `<PLUGIN_ROOT>`).
   - Writing temporary Python scripts or custom subtitle manipulation logic is **STRICTLY FORBIDDEN**.
2. **Mandatory 3-Step Gated Workflow (Direct CLI Invocation)**:
   - Resolve `<PLUGIN_ROOT>` as two directory levels above `skills/subtitle-craft/SKILL.md` (`../../`, e.g., `/Users/sylph/.gemini/config/plugins/subtitle-craft`).
   - Follow the 3-Step Runbook defined in [SKILL.md](file:///Users/sylph/Documents/Antigravity/subtitle-craft/skills/subtitle-craft/SKILL.md):
     - **Step 1 (Environment & Cloud Auth Verification)**: Verify FFmpeg, `gcloud` ADC credentials, and `.env` configuration (`GOOGLE_CLOUD_PROJECT`, `SUBTITLE_CRAFT_BUCKET` / `GCS_BUCKET`) from `<PLUGIN_ROOT>`.
     - **Step 2 (Pipeline Execution)**: Run `python3 skills/subtitle-craft/scripts/generate_subtitles.py` (or `python3 subtitle_craft.py` with `Cwd` set to `<PLUGIN_ROOT>`) directly via `run_command`. Pass `--outline` or `--script` when the user provides an outline or reference script.
     - **Step 3 (Deliverable Verification)**: Verify that `.srt`, `.vtt`, `_glossary.md`, `_subtitle_report.md`, and `_subtitle_report.json` exist on disk and are non-empty (`> 0 bytes`).
3. **Fail-Fast & Exit Gate Verification**:
   - If any script exits with a non-zero status (such as missing ADC credentials, 403/401 GCS/Vertex AI permission error, or missing FFmpeg), stop immediately, report the exact error and exit status, and instruct the user to run `./setup.sh --project YOUR_PROJECT_ID` or `gcloud auth application-default login`.
   - Never declare completion until all required deliverable files exist on disk and are non-empty (`> 0 bytes`).
4. **Dynamic Language Mirroring & Strict Zero-Emoji Policy**:
   - Respond to the user in their prompt language (Traditional Chinese `zh-TW` when prompted in Traditional Chinese, English when prompted in English, Japanese when prompted in Japanese, etc.).
   - Do NOT use decorative emojis or icons in section headings, tables, or generated audit reports.

---

## Part II: Repository Development & Engineering Standards (When Developing This Project)

When modifying code, prompts, infrastructure scripts, or documentation in this repository, you MUST adhere to the following engineering standards:

### 1. Single Source of Truth (SSOT) & Symlink Integrity (Agent Plugins 1.0 Specification)
- **Canonical Code Location**: All core Python scripts (`scripts/*.py`), internal modules (`scripts/modules/*.py`), and prompt templates (`assets/*.md`) physically reside inside `skills/subtitle-craft/scripts/` and `skills/subtitle-craft/assets/` in compliance with the [Agent Plugins 1.0 Specification](https://agent-plugins.org/specification) (§4.2 & §7.1).
- **Root Symlinks**: Top-level `SKILL.md`, `scripts`, and `assets` at the repository root are POSIX symlinks pointing to `skills/subtitle-craft/SKILL.md`, `skills/subtitle-craft/scripts`, and `skills/subtitle-craft/assets` (§4.1.3).
- **Rule**: Always edit files under `skills/subtitle-craft/scripts/` and `skills/subtitle-craft/assets/`. Never replace root symlinks with duplicate physical directories.

### 2. Three-Stage Golden Subtitle Architecture & Acoustic Integrity
- **Stage 1 (Vertex AI 1M Context Global Glossary)**: Extracts verified speaker names, brands, and domain terminology from the full recording on GCS and produces a language-matched Whisper `initial_prompt`.
- **Stage 2 (Whisper Word-Level Acoustic Ground Truth)**: Extracts physical word timestamps (`word_timestamps=True`) via `mlx-whisper` or `faster-whisper` and caches `_words.json` and `_raw_whisper.srt`.
- **Stage 3 (Silence-Aware Multimodal Proofreading & Re-Projection)**: Splits chunks at natural speech pauses ($\ge 0.4\text{s}$), proofreads against GCS-staged audio slices, re-projects subtitle boundaries onto physical word timestamps (`realign_subtitles_to_words`), sanitizes reading rhythm (`sanitize_subtitle_timings`), and executes the 8-dimension Netflix/YouTube quality audit (`audit_subtitles_quality`).

### 3. 100% Google Cloud Vertex AI (ADC) + GCS Architecture
- **Zero API Key Policy**: All Gemini invocations in [llm_client.py](file:///Users/sylph/Documents/Antigravity/subtitle-craft/skills/subtitle-craft/scripts/modules/llm_client.py) and [gcp_client.py](file:///Users/sylph/Documents/Antigravity/subtitle-craft/skills/subtitle-craft/scripts/modules/gcp_client.py) MUST use `genai.Client(vertexai=True, project=..., location=...)` authenticated via Application Default Credentials (ADC), defaulting to `GOOGLE_CLOUD_LOCATION=global`. Never introduce `GEMINI_API_KEY` or AI Studio File API uploads.
- **GCS Infrastructure & Two-Tier Lifecycle (`setup.sh`)**:
  - Consolidate all cloud environment setup in [setup.sh](file:///Users/sylph/Documents/Antigravity/subtitle-craft/setup.sh) using native `gcloud` CLI commands.
  - Maintain the two-tier retention policy on `gs://subtitle-craft-${PROJECT_ID}`:
    - **`raw/` prefix**: **2 days (`age: 2`)** for ephemeral staging audio (with local SHA-256 hash caching).
    - **`output/`, `deliverables/` prefixes**: **15 days (`age: 15`)** for deliverables retention.
    - **`raw/audio_chunks/`**: Immediate deletion in `finally` blocks after each subtitle proofreading chunk completes.

### 4. ASD-STE100 English, Strict Generality, & Unit Testing Gate
- **ASD-STE100 & Zero Entity Hardcoding**: Write all Python code, docstrings, comments, and README documentation following **ASD-STE100 (Simplified Technical English)** principles. Use only generic placeholders (`video.mp4`, `interview_audio.mp3`, `outline.md`, `script.md`) and never hardcode test-specific names or titles.
- **Mandatory Unit Test Gate**: Run the full unit test suite and verify 100% pass rate before committing any change:
  ```bash
  python3 -m unittest discover -s tests -v
  ```

### 5. Antigravity Plugin Architecture, 5-Language Parity, & Commit Policy
- **Plugin & README Synchronization**: Keep [plugin.json](file:///Users/sylph/Documents/Antigravity/subtitle-craft/plugin.json), [rules/AGENTS.md](file:///Users/sylph/Documents/Antigravity/subtitle-craft/rules/AGENTS.md), [skills/subtitle-craft/SKILL.md](file:///Users/sylph/Documents/Antigravity/subtitle-craft/skills/subtitle-craft/SKILL.md), and all 5 language READMEs ([README.md](file:///Users/sylph/Documents/Antigravity/subtitle-craft/README.md), [README.zh-TW.md](file:///Users/sylph/Documents/Antigravity/subtitle-craft/README.zh-TW.md), [README.zh-CN.md](file:///Users/sylph/Documents/Antigravity/subtitle-craft/README.zh-CN.md), [README.ja.md](file:///Users/sylph/Documents/Antigravity/subtitle-craft/README.ja.md), [README.ko.md](file:///Users/sylph/Documents/Antigravity/subtitle-craft/README.ko.md)) synchronized at all times.
- **Human Authorship Only**: Author all commits as `sylphlin <sylph.lin@gmail.com>` with zero AI assistant branding or `Co-Authored-By` trailers.
