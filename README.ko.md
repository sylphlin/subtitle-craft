# Subtitle Craft — 방송급 AI 자막 생성 및 교정 스위트

[English (en)](README.md) | [繁體中文 (zh-TW)](README.zh-TW.md) | [简体中文 (zh-CN)](README.zh-CN.md) | [日本語 (ja)](README.ja.md) | [한국어 (ko)](README.ko.md)

---

> [!IMPORTANT]
> **Google Antigravity 네이티브 플러그인 및 워크플로 스위트**  
> 본 툴킷은 **Google Antigravity**(**Vertex AI Gemini 3.8 Flash** 및 **Whisper 단어 타임스탬프** 기반)를 위한 독립형 3단계 YouTube / Netflix 자막 생성 및 품질 감사 스위트입니다.

---

**Subtitle Craft**는 로컬 또는 Google Drive의 비디오 및 오디오 파일에서 밀리초 단위로 정확하게 정렬되고 전문 용어가 통일된 자막(`.srt` 및 `.vtt`)을 생성합니다.

---

## 설치 및 Google Cloud 설정 (`setup.sh`)

본 프로젝트는 [Agent Plugins 1.0](https://agent-plugins.org/) 표준을 준수하며 **Google Cloud Vertex AI (ADC)** 및 **Cloud Storage (GCS)** 기반으로 동작합니다.

```bash
# 1. 글로벌 Antigravity Plugin으로 클론
git clone https://github.com/sylphlin/subtitle-craft.git ~/.gemini/config/plugins/subtitle-craft

# 2. 의존성 설치 및 ADC 인증
brew install ffmpeg
pip install -r requirements.txt
gcloud auth application-default login

# 3. setup.sh 실행하여 GCS 버킷, 2단계 수명 주기(raw: 2일, 결과물: 15일), IAM 및 .env 설정
chmod +x setup.sh
./setup.sh --project YOUR_GCP_PROJECT_ID
```

### 디렉터리 구조 (Agent Plugins 1.0 표준)
- **SSOT 실제 디렉터리**: `skills/subtitle-craft/`(`SKILL.md`, `scripts/`, `assets/` 포함)를 단일 진실 공급원(SSOT)으로 사용하며 루트 `SKILL.md`, `scripts` 및 `assets`는 POSIX 심볼릭 링크로 연결됩니다.
- **2계층 `AGENTS.md` 구성**: 루트 `AGENTS.md`는 워크스페이스 및 개발 표준(Part I & Part II)을 정의하고, 플러그인 내부의 `rules/AGENTS.md`는 AI 클라이언트 실행 규칙(`<PLUGIN_ROOT>` 직접 CLI 호출, 읽기 전용, Fail-Fast)을 정의합니다.

---

## 3단계 골든 자막 파이프라인 및 CLI 명령

1. **Stage 1 (Vertex AI 1M 글로벌 용어집 및 Whisper 초기 프롬프트)**: **Gemini 3.8 Flash**로 전체 오디오를 스캔하여 `<basename>_glossary.md`와 Whisper 초기 프롬프트를 추출합니다.
2. **Stage 2 (Whisper 단어 수준 음향 타임스탬프)**: `mlx-whisper` 또는 `faster-whisper`(`word_timestamps=True`)를 실행하여 밀리초 단위 단어 경계를 `<basename>_words.json`에 캐시합니다.
3. **Stage 3 (무음 인식 청킹 및 멀티모달 오디오 교정 + 8차원 스트리밍 품질 감사)**: 자연스러운 호흡 구간($\ge 0.4\text{s}$)에서 분할하고 오디오 슬라이스 및 용어집을 기반으로 동음이의어를 교정한 뒤, 물리적 단어 경계로 타임스탬프를 재투영하고 `<basename>_subtitle_report.md` 및 `.json`을 생성합니다.

```bash
# 표준 자막 생성
python3 subtitle_craft.py -i output/final_cut.mp4 --language ko

# 인터뷰 개요 또는 대본을 함께 전달하여 용어 고정
python3 subtitle_craft.py -i output/final_cut.mp4 --outline outline.md --script script.md --language ko

# Google Drive 공유 링크에서 직접 자막 생성
python3 subtitle_craft.py -i "https://drive.google.com/file/d/FILE_ID/view" --language ko
```

---

## GCS 2단계 수명 주기 정책 (`gs://subtitle-craft-${PROJECT_ID}`)

| GCS 경로 접두사 (`matchesPrefix`) | 저장 객체 | 보관 기간 (`age`) | 정리 방식 |
| :--- | :--- | :--- | :--- |
| **`raw/audio_chunks/`** | Stage 3 오디오 청크 | **추론 직후 즉시 삭제** | 각 청크 완료 후 Python `finally` 블록에서 즉시 삭제합니다. |
| **`raw/`** | 스테이징된 에피소드 오디오 | **2일 (`age: 2`)** | SHA-256 캐시 재사용을 위해 2일간 보관 후 자동 삭제합니다. |
| **`output/`**, **`deliverables/`** | SRT/VTT 자막 및 감사 리포트 | **15일 (`age: 15`)** | 팀 검토를 위해 15일간 보관한 후 자동 삭제합니다. |

---

## 라이선스 (License)

이 프로젝트는 [MIT License](LICENSE)에 따라 라이선스가 부여됩니다.
