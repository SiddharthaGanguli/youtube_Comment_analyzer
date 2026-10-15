"""Publish only static website assets and validated public connection metadata."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.build_pages_site import (CONNECTION_FILES, GENERATED_SETTINGS, ROOT, WEB_FILES, build_site, main)

NODE = shutil.which("node")
PRIVATE = "synthetic-private-key-must-not-be-published"


class PagesSiteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pages-site-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source, self.output = self.root / "extension", self.root / "site"
        for name in (*WEB_FILES, *CONNECTION_FILES):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("static fixture: " + name, encoding="utf-8")
        for name in ("web-client-config.mjs", "client-config.mjs"):
            (self.source / name).write_bytes((ROOT / "extension" / name).read_bytes())
        # Poisoned source settings must never override the builder's explicit
        # disabled default or its validated public JSON input.
        (self.source / GENERATED_SETTINGS).write_text("export const webClientSettings = {apiKey:'" + PRIVATE + "'};", encoding="utf-8")
        for name in (".env", "manifest.json", "auth-session.mjs", "client-settings.mjs", "data.csv"):
            (self.source / name).write_text(PRIVATE, encoding="utf-8")
        (self.source / "tests").mkdir()
        (self.source / "tests" / "fixture-preview.mjs").write_text(PRIVATE, encoding="utf-8")

    def settings(self, **changes):
        raw = {"enabled": True, "apiBaseUrl": "https://fixture.execute-api.us-east-1.amazonaws.com",
               "cognitoDomain": "https://comment-lens-fixture.auth.us-east-1.amazoncognito.com", "clientId": "fixtureclient1",
               "redirectUri": "https://siddharthaganguli.github.io/youtube_Comment_analyzer/dashboard.html",
               "resultBaseUrl": "https://fixture-bucket.s3.us-east-1.amazonaws.com/serving/development"}
        raw.update(changes)
        path = self.root / "public-settings.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def files(self):
        return {path.relative_to(self.output).as_posix() for path in self.output.rglob("*") if path.is_file()}

    def test_default_demo_has_no_authentication_or_backend_assets_and_ignores_source_settings(self):
        output = build_site(self.source, self.output)
        self.assertEqual(output, self.output.resolve())
        self.assertEqual(self.files(), set(WEB_FILES) | {GENERATED_SETTINGS, ".nojekyll"})
        module = (output / GENERATED_SETTINGS).read_text(encoding="utf-8")
        self.assertIn('"enabled": false', module)
        self.assertNotIn(PRIVATE, module)
        self.assertTrue(all(PRIVATE not in path.read_text(encoding="utf-8") for path in output.rglob("*") if path.is_file()))

    @unittest.skipUnless(NODE, "Node is required for enabled public settings")
    def test_configured_site_adds_only_the_web_connection_allowlist(self):
        build_site(self.source, self.output, self.settings())
        self.assertEqual(self.files(), set(WEB_FILES) | set(CONNECTION_FILES) | {GENERATED_SETTINGS, ".nojekyll"})
        module = (self.output / GENERATED_SETTINGS).read_text(encoding="utf-8")
        self.assertIn('"enabled": true', module)
        self.assertIn('"redirectUri": "https://siddharthaganguli.github.io/youtube_Comment_analyzer/dashboard.html"', module)
        self.assertNotIn("extensionId", module)
        self.assertNotIn(PRIVATE, module)
        self.assertTrue({"web-auth-session.mjs", "auth-pkce.mjs", "analysis-client.mjs", "client-config.mjs"}.issubset(self.files()))

    @unittest.skipUnless(NODE, "Node is required for enabled public settings")
    def test_returning_to_demo_removes_only_allowlisted_connection_files(self):
        build_site(self.source, self.output, self.settings())
        build_site(self.source, self.output)
        self.assertEqual(self.files(), set(WEB_FILES) | {GENERATED_SETTINGS, ".nojekyll"})

    def test_unknown_existing_files_are_preserved_and_never_published(self):
        self.output.mkdir()
        sentinel = self.output / ".env"
        sentinel.write_text(PRIVATE, encoding="utf-8")
        with self.assertRaises(ValueError):
            build_site(self.source, self.output)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), PRIVATE)
        self.assertEqual(self.files(), {".env"})

    def test_unknown_empty_directories_are_refused(self):
        (self.output / "private").mkdir(parents=True)
        with self.assertRaises(ValueError):
            build_site(self.source, self.output)
        self.assertTrue((self.output / "private").is_dir())

    def test_output_cannot_overlap_source_or_replace_its_parent(self):
        for output in (self.source, self.source / "site", self.root):
            with self.subTest(path=output), self.assertRaises(ValueError):
                build_site(self.source, output)

    def test_missing_required_asset_fails_before_writing_any_output(self):
        (self.source / "dashboard.html").unlink()
        with self.assertRaises(ValueError):
            build_site(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_invalid_json_oversized_fields_and_secret_values_are_rejected_without_echo(self):
        inputs = ['{"enabled":false,"enabled":true}', '[]', 'null', 'NaN', '{',
                  '{"enabled":true,"apiKey":"' + PRIVATE + '"}',
                  '{"enabled":false,"token":"' + PRIVATE + '"}', " " * 16385,
                  "[" * 2000 + "0" + "]" * 2000]
        path = self.root / "bad-settings.json"
        for content in inputs:
            path.write_text(content, encoding="utf-8")
            with self.subTest(length=len(content)), self.assertRaises(ValueError) as caught:
                build_site(self.source, self.output, path)
            self.assertNotIn(PRIVATE, str(caught.exception))
            self.assertFalse(self.output.exists())

    @unittest.skipUnless(NODE, "Node is required for enabled public settings")
    def test_application_validator_rejects_unsafe_urls_and_unexpected_extension_fields(self):
        for changes in ({"apiBaseUrl": "http://api.example.com"}, {"redirectUri": "https://evil.example/path?token=" + PRIVATE},
                        {"resultBaseUrl": "https://evil.example/serving/development"}, {"extensionId": "a" * 32},
                        {"maxComments": 1001}, {"cognitoDomain": "https://evil.example"}):
            with self.subTest(fields=tuple(changes)), self.assertRaises(ValueError) as caught:
                build_site(self.source, self.output, self.settings(**changes))
            self.assertNotIn(PRIVATE, str(caught.exception))

    def test_validator_failure_and_timeout_do_not_echo_diagnostics(self):
        settings = self.settings()
        for outcome in (SimpleNamespace(returncode=1, stdout="", stderr=PRIVATE), subprocess.TimeoutExpired("node", 10)):
            with self.subTest(outcome=type(outcome).__name__), patch("scripts.build_pages_site.shutil.which", return_value="node"), \
                    patch("scripts.build_pages_site.subprocess.run", **({"side_effect": outcome} if isinstance(outcome, Exception)
                                                                      else {"return_value": outcome})):
                with self.assertRaises(ValueError) as caught:
                    build_site(self.source, self.output, settings)
                self.assertNotIn(PRIVATE, str(caught.exception))
                self.assertFalse(self.output.exists())

    def test_settings_inside_output_cannot_be_included_as_a_static_asset(self):
        self.output.mkdir()
        path = self.output / "settings.json"
        path.write_text('{"enabled":false}', encoding="utf-8")
        with self.assertRaises(ValueError):
            build_site(self.source, self.output, path)
        self.assertTrue(path.exists())

    def test_replacing_an_allowlisted_hard_link_does_not_overwrite_the_external_target(self):
        self.output.mkdir()
        external = self.root / "external.txt"
        external.write_text(PRIVATE, encoding="utf-8")
        try:
            os.link(external, self.output / "dashboard.html")
        except OSError:
            self.skipTest("Hard links are unavailable")
        build_site(self.source, self.output)
        self.assertEqual(external.read_text(encoding="utf-8"), PRIVATE)
        self.assertNotEqual((self.output / "dashboard.html").read_text(encoding="utf-8"), PRIVATE)

    def test_source_and_output_symlinks_are_rejected_without_following(self):
        external = self.root / "external.txt"
        external.write_text(PRIVATE, encoding="utf-8")
        target = self.source / "dashboard.html"
        target.unlink()
        try:
            target.symlink_to(external)
        except OSError:
            self.skipTest("Symlinks are unavailable")
        with self.assertRaises(ValueError):
            build_site(self.source, self.output)
        self.assertFalse(self.output.exists())
        target.unlink()
        target.write_text("ordinary", encoding="utf-8")
        self.output.mkdir()
        (self.output / "dashboard.html").symlink_to(external)
        with self.assertRaises(ValueError):
            build_site(self.source, self.output)
        self.assertEqual(external.read_text(encoding="utf-8"), PRIVATE)

    def test_cli_failure_is_safe_and_does_not_create_output(self):
        path = self.root / "private.json"
        path.write_text('{"apiKey":"' + PRIVATE + '"}', encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["--settings", str(path), "--output", str(self.output)]), 1)
        self.assertNotIn(PRIVATE, errors.getvalue())
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
