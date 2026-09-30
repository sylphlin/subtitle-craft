"""Unit tests for Subtitle Craft GCP and Google Drive client module (tests/test_gcp_client.py)."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
from modules.gcp_client import (
    resolve_gcp_config,
    parse_gcs_uri,
    guess_mime_type,
    is_gdrive_source,
    parse_gdrive_url,
    fix_mojibake_filename,
    extract_filename_from_content_disposition,
)


class TestGcpClient(unittest.TestCase):
    def test_fix_mojibake_filename_and_content_disposition(self):
        original = "主機位訪談錄影_4K.mp4"
        latin1_mojibake = original.encode("utf-8").decode("latin-1")
        self.assertEqual(fix_mojibake_filename(latin1_mojibake), original)
        self.assertEqual(
            extract_filename_from_content_disposition(f'attachment; filename="{latin1_mojibake}"', "fallback.mp4"),
            original,
        )

    def test_guess_mime_type(self):
        self.assertEqual(guess_mime_type("interview_video.mp4"), "video/mp4")
        self.assertEqual(guess_mime_type("chunk_001.mp3"), "audio/mpeg")
        self.assertEqual(guess_mime_type("audio.wav"), "audio/wav")
        self.assertEqual(guess_mime_type("footage.mov"), "video/quicktime")

    def test_parse_gcs_uri(self):
        bucket, blob = parse_gcs_uri("gs://my-bucket/raw/global_glossary_audio.mp3")
        self.assertEqual(bucket, "my-bucket")
        self.assertEqual(blob, "raw/global_glossary_audio.mp3")

        with self.assertRaises(ValueError):
            parse_gcs_uri("https://storage.googleapis.com/my-bucket/file.mp4")

    def test_resolve_gcp_config_cli_overrides(self):
        cfg = resolve_gcp_config(
            cli_project="custom-proj",
            cli_bucket="gs://custom-bucket/",
            cli_location="global",
            cli_region="asia-east1",
        )
        self.assertEqual(cfg["project"], "custom-proj")
        self.assertEqual(cfg["bucket"], "custom-bucket")
        self.assertEqual(cfg["location"], "global")
        self.assertEqual(cfg["region"], "asia-east1")

    @patch("modules.gcp_client._parse_env_file", return_value={})
    @patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "auto-proj"}, clear=True)
    def test_resolve_gcp_config_deterministic_default_bucket(self, _mock_env):
        cfg = resolve_gcp_config()
        self.assertEqual(cfg["project"], "auto-proj")
        self.assertEqual(cfg["bucket"], "subtitle-craft-auto-proj")
        self.assertEqual(cfg["location"], "global")
        self.assertEqual(cfg["region"], "us-central1")

    def test_is_gdrive_source(self):
        self.assertTrue(is_gdrive_source("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz123456/view?usp=sharing"))
        self.assertTrue(is_gdrive_source("gdrive://1AbCdEfGhIjKlMnOpQrStUvWxYz123456"))
        self.assertFalse(is_gdrive_source("/Users/sylph/Videos/interview.mp4"))
        self.assertFalse(is_gdrive_source("gs://my-bucket/raw/interview.mp4"))
        self.assertFalse(is_gdrive_source(None))

    def test_parse_gdrive_url(self):
        r1 = parse_gdrive_url("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz123456?usp=drive_link")
        self.assertEqual(r1, {"id": "1AbCdEfGhIjKlMnOpQrStUvWxYz123456", "type": "folder"})

        r2 = parse_gdrive_url("https://drive.google.com/file/d/1XyZ9876543210AbCdEfGhIjKlMnOpQrS/view?usp=sharing")
        self.assertEqual(r2, {"id": "1XyZ9876543210AbCdEfGhIjKlMnOpQrS", "type": "file"})

        r3 = parse_gdrive_url("https://drive.google.com/open?id=1XyZ9876543210AbCdEfGhIjKlMnOpQrS")
        self.assertEqual(r3, {"id": "1XyZ9876543210AbCdEfGhIjKlMnOpQrS", "type": "unknown"})


if __name__ == "__main__":
    unittest.main()
