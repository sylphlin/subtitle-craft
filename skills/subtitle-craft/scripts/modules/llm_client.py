#!/usr/bin/env python3
"""
Vertex AI and GCS Multimodal LLM Client (llm_client.py).
Uses Google Cloud Vertex AI via Application Default Credentials (ADC)
and Google Cloud Storage (gs:// URIs). Do not use AI Studio API keys.
"""

import os
import random
import sys
import time

try:
    from .gcp_client import (
        resolve_gcp_config,
        upload_file_to_gcs_with_cache,
        delete_gcs_blob,
        guess_mime_type,
    )
except ImportError:
    try:
        from modules.gcp_client import (
            resolve_gcp_config,
            upload_file_to_gcs_with_cache,
            delete_gcs_blob,
            guess_mime_type,
        )
    except ImportError:
        from scripts.modules.gcp_client import (
            resolve_gcp_config,
            upload_file_to_gcs_with_cache,
            delete_gcs_blob,
            guess_mime_type,
        )


def get_vertex_client(project=None, location=None):
    """
    Initialize and return a Google GenAI Client on Vertex AI.
    Authenticates via Application Default Credentials (ADC).
    """
    import google.genai as genai

    gcp_cfg = resolve_gcp_config(cli_project=project, cli_location=location)
    resolved_project = gcp_cfg.get("project")
    resolved_location = gcp_cfg.get("location") or "global"

    if not resolved_project:
        raise ValueError(
            "Missing Google Cloud Project ID for Vertex AI.\n"
            "Run './setup.sh', set GOOGLE_CLOUD_PROJECT in .env, or run:\n"
            "  gcloud config set project <YOUR_PROJECT_ID>\n"
            "  gcloud auth application-default login"
        )

    return genai.Client(
        vertexai=True,
        project=resolved_project,
        location=resolved_location,
    )


def call_vertex_generate_content(
    prompt,
    project=None,
    location=None,
    gcs_bucket=None,
    region=None,
    model="gemini-3.8-flash",
    gcs_uri=None,
    audio_path=None,
    cleanup_ephemeral_audio=True,
    temperature=0.1,
    max_tokens=8192,
    thinking_budget=None,
    max_retries=5,
):
    """
    Call Google Cloud Vertex AI Gemini API with Application Default Credentials (ADC).
    Pass media files as GCS URIs (gs://...).
    If audio_path is supplied without gcs_uri, stage the file to gs://<bucket>/raw/audio_chunks/
    and delete it in the finally block when cleanup_ephemeral_audio is True.
    """
    from google.genai import types

    gcp_cfg = resolve_gcp_config(
        cli_project=project,
        cli_bucket=gcs_bucket,
        cli_location=location,
        cli_region=region,
    )
    resolved_project = gcp_cfg.get("project")
    resolved_location = gcp_cfg.get("location") or "global"
    resolved_bucket = gcp_cfg.get("bucket")
    resolved_region = gcp_cfg.get("region") or "us-central1"

    if not resolved_project:
        raise ValueError(
            "Missing Google Cloud Project ID for Vertex AI. "
            "Run './setup.sh', pass --project, or set GOOGLE_CLOUD_PROJECT in .env."
        )

    client = get_vertex_client(project=resolved_project, location=resolved_location)

    staged_ephemeral_uri = None
    effective_gcs_uri = gcs_uri

    # Stage local audio slice to GCS when audio_path is provided
    if not effective_gcs_uri and audio_path and os.path.isfile(audio_path):
        if not resolved_bucket:
            raise ValueError(
                "Missing GCS Bucket for staging audio to Vertex AI. "
                "Run './setup.sh', pass --gcs-bucket, or set SUBTITLE_CRAFT_BUCKET / GCS_BUCKET in .env."
            )
        effective_gcs_uri = upload_file_to_gcs_with_cache(
            local_path=audio_path,
            bucket_name=resolved_bucket,
            gcs_prefix="raw/audio_chunks",
            project=resolved_project,
            region=resolved_region,
        )
        if cleanup_ephemeral_audio:
            staged_ephemeral_uri = effective_gcs_uri

    contents = []
    if effective_gcs_uri:
        mime = guess_mime_type(effective_gcs_uri)
        contents.append(types.Part.from_uri(file_uri=effective_gcs_uri, mime_type=mime))
    contents.append(prompt)

    config_params = {"temperature": temperature, "max_output_tokens": max_tokens}
    if thinking_budget is not None and ("3.7" in model or "3.8" in model):
        config_params["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)
    config = types.GenerateContentConfig(**config_params)

    try:
        for attempt in range(max_retries):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )
                return resp.text.strip() if resp.text else ""
            except Exception as e:
                err_str = str(e)
                is_rate_limit = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str
                is_server_err = any(code in err_str for code in ("500", "502", "503", "504", "UNAVAILABLE"))
                if (is_rate_limit or is_server_err) and attempt < max_retries - 1:
                    sleep_sec = min(60.0, 2.0 * (2 ** attempt)) + random.uniform(0.5, 2.0)
                    status_label = "Rate Limit (429)" if is_rate_limit else "Server Error"
                    print(
                        f"\n  [Retry] Vertex AI {status_label} hit, retrying in {sleep_sec:.1f}s (Attempt {attempt + 1}/{max_retries})...",
                        file=sys.stderr,
                    )
                    time.sleep(sleep_sec)
                    continue
                raise RuntimeError(
                    f"Vertex AI API call failed (Project: '{resolved_project}', Location: '{resolved_location}'): {e}"
                )
    finally:
        if staged_ephemeral_uri:
            delete_gcs_blob(staged_ephemeral_uri, project=resolved_project)


def call_llm(
    prompt,
    model="gemini-3.8-flash",
    project=None,
    location=None,
    gcs_bucket=None,
    region=None,
    gcs_uri=None,
    audio_path=None,
    cleanup_ephemeral_audio=True,
    temperature=0.1,
    max_tokens=8192,
    thinking_budget=None,
):
    """Call Vertex AI Gemini API via ADC and GCS."""
    return call_vertex_generate_content(
        prompt=prompt,
        project=project,
        location=location,
        gcs_bucket=gcs_bucket,
        region=region,
        model=model,
        gcs_uri=gcs_uri,
        audio_path=audio_path,
        cleanup_ephemeral_audio=cleanup_ephemeral_audio,
        temperature=temperature,
        max_tokens=max_tokens,
        thinking_budget=thinking_budget,
    )
