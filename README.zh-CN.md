# Subtitle Craft — 影视级 AI 智能字幕生成与校对套件

[English (en)](README.md) | [繁體中文 (zh-TW)](README.zh-TW.md) | [简体中文 (zh-CN)](README.zh-CN.md) | [日本語 (ja)](README.ja.md) | [한국어 (ko)](README.ko.md)

---

> [!IMPORTANT]
> **Google Antigravity 原生技能与工作流套件**  
> 本工具集为 **Google Antigravity**（由 **Vertex AI Gemini 3.8 Flash** 与 **Whisper 毫秒级逐词时间戳** 驱动）打造独立的三阶段 YouTube / Netflix 字幕生成与质量审核工作流。

---

**Subtitle Craft** 支持将本地或 Google Drive 视频与音频文件转换为毫秒级精准对齐、专有名词统一的 `.srt` 与 `.vtt` 字幕。可在 Antigravity 对话窗口使用自然语言下达指令，或通过终端 CLI 独立运行。

---

## 安装与 Google Cloud 环境配置 (`setup.sh`)

本项目遵循 [Agent Plugins 1.0](https://agent-plugins.org/) 规范，完全基于 **Google Cloud Vertex AI (ADC)** 与 **Cloud Storage (GCS)** 运行，无需 API Key。

```bash
# 1. 安装为全局 Antigravity Plugin
git clone https://github.com/sylphlin/subtitle-craft.git ~/.gemini/config/plugins/subtitle-craft

# 2. 安装依赖与授权 ADC
brew install ffmpeg
pip install -r requirements.txt
gcloud auth application-default login

# 3. 运行 setup.sh 配置 GCS 存储桶、双层生命周期规则（raw: 2 天，交付物: 15 天）、IAM 与 .env
chmod +x setup.sh
./setup.sh --project YOUR_GCP_PROJECT_ID
```

### 项目目录结构（Agent Plugins 1.0 标准规范）
- **SSOT 实体目录**：`skills/subtitle-craft/`（内含 `SKILL.md`、`scripts/` 与 `assets/`），根目录 `SKILL.md`、`scripts` 与 `assets` 为指向该目录的 POSIX symlinks。
- **双层 `AGENTS.md` 规范**：根目录 `AGENTS.md` 定义工作区与工程开发规范（Part I & Part II），`rules/AGENTS.md` 随 Plugin 打包注入 AI 客户端执行期守则（定位 `<PLUGIN_ROOT>` 直接调用 CLI、只读与 Fail-Fast）。

---

## 三阶段黄金字幕管线与 CLI 命令

1. **Stage 1（Vertex AI 1M 全局术语表与 Whisper 初始提示词）**：使用 **Gemini 3.8 Flash** 扫描全片音频，生成 `<basename>_glossary.md` 与 `<145` 字符的 Whisper 引导词。
2. **Stage 2（Whisper 毫秒级逐词时间戳）**：通过 `mlx-whisper` 或 `faster-whisper`（`word_timestamps=True`）提取物理词级时间戳并缓存至 `<basename>_words.json`。
3. **Stage 3（静音感知分块、多模态音频校对与 8 维度流媒体质量审核）**：在自然停顿处（$\ge 0.4\text{s}$）切分块，结合音频切片与全局术语表进行校对，将时间轴重投影回物理词边界，并生成 `<basename>_subtitle_report.md` 与 `.json`。

```bash
# 标准字幕生成
python3 subtitle_craft.py -i output/final_cut.mp4 --language zh-CN

# 结合访谈提纲或文稿校对
python3 subtitle_craft.py -i output/final_cut.mp4 --outline outline.md --script script.md --language zh-CN

# 直接输入 Google Drive 分享链接
python3 subtitle_craft.py -i "https://drive.google.com/file/d/FILE_ID/view" --language zh-CN
```

---

## GCS 双层生命周期规则 (`gs://subtitle-craft-${PROJECT_ID}`)

| GCS 路径前缀 (`matchesPrefix`) | 存储对象 | 保留天数 (`age`) | 清理机制 |
| :--- | :--- | :--- | :--- |
| **`raw/audio_chunks/`** | Stage 3 音频切片 | **推理后立即删除** | 每个分块完成后在 Python `finally` 块中立即删除。 |
| **`raw/`** | 暂存完整音轨 | **2 天 (`age: 2`)** | 保留 2 天供 SHA-256 缓存复用，期满自动删除。 |
| **`output/`**、**`deliverables/`** | SRT/VTT 字幕与报告 | **15 天 (`age: 15`)** | 保留 15 天供团队审阅，期满自动清理。 |

---

## 许可证 (License)

本项目采用 [MIT License](LICENSE) 授权。
