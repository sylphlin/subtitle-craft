# Subtitle Craft - Operational Invariants for AI Clients

When you execute tasks or skills from this plugin, you MUST follow these operational rules:

## 1. Strict Read-Only Execution & Direct CLI Invocation (Do Not Modify Plugin Code)
- All Python scripts (`skills/subtitle-craft/scripts/*.py`), prompt templates (`skills/subtitle-craft/assets/*.md`), and configuration files are read-only tools.
- Do NOT edit, patch, or rewrite any files in this plugin with `replace_file_content`, `write_to_file`, or shell commands.
- Do NOT write ad-hoc temporary Python scripts, custom regex subtitle parsers, or one-off shell scripts.
- Resolve `<PLUGIN_ROOT>` as two directory levels above `skills/subtitle-craft/SKILL.md` (`../../`, e.g., `/Users/sylph/.gemini/config/plugins/subtitle-craft`).
- Set `Cwd` to `<PLUGIN_ROOT>` and run the official script (`python3 skills/subtitle-craft/scripts/generate_subtitles.py`) directly with `run_command` using the specified arguments. Do NOT search for global CLI aliases with `find_by_name` or `list_dir`.

## 2. Fail-Fast on Errors & Exit Gate Verification (Do Not Debug or Rewrite Code)
- If a script fails (exit code is not 0) or an external error occurs (such as 401 Unauthorized, 403 Forbidden, Quota Exceeded, missing Application Default Credentials, or missing FFmpeg):
  - Stop immediately.
  - Show the exact error message and exit status to the user.
  - Give a clear, actionable solution to the user (for example, run `./setup.sh --project YOUR_PROJECT_ID`, run `gcloud auth application-default login`, or install FFmpeg).
  - Do NOT try to modify the script, probe different code paths, or rewrite logic.
- Verify all required deliverable files (`.srt`, `.vtt`, `_glossary.md`, `_subtitle_report.md`, `_subtitle_report.json`) exist on disk and are non-empty (`> 0 bytes`) before declaring task completion.

## 3. Strict Zero-Emoji Policy in Technical Reports & Dynamic Language Mirroring
- Do NOT use decorative emojis or icons in generated technical Subtitle Audit Markdown reports or headings.
- Keep all generated documentation and audit reports in plain, professional technical text.
- Always respond to the user in their prompt language (Traditional Chinese `zh-TW` when prompted in Traditional Chinese, English when prompted in English, Japanese when prompted in Japanese, etc.) and pass the matching `--language` flag when specified.

## 4. Acoustic Ground Truth & Three-Stage Pipeline Integrity
- Execute the 3-Stage Golden Pipeline (`Stage 1: Global Glossary` -> `Stage 2: Whisper Word Timestamps` -> `Stage 3: Multimodal Audio Proofreading & Acoustic Re-Projection`) via `generate_subtitles.py` as defined in `skills/subtitle-craft/SKILL.md`.
- All subtitle timestamps must respect Whisper word-level acoustic ground truth (`word_timestamps=True`), monotonic forward alignment, and broadcast pacing rules ($1.0\text{s}\text{–}6.0\text{s}$ duration, $<0.2\text{s}$ micro-gap bridging).
- Never use legacy AI Studio API keys (`GEMINI_API_KEY`).
