"""Publish only static website assets and validated public connection metadata."""

import contextlib
import io
import json
import os
from pathlib import Path
import re
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
        (self.source / "dashboard.html").write_text(
            '<!doctype html><script type="module" src="dashboard.mjs"></script>', encoding="utf-8")
        (self.source / "index.html").write_text(
            '<!doctype html><script type="module" src="home.mjs"></script>', encoding="utf-8")
        (self.source / "home.mjs").write_text(
            'const {webClientSettings} = await import("./web-client-settings.mjs");\n'
            'if (webClientSettings.enabled) await import("./web-session-store.mjs");', encoding="utf-8")
        (self.source / "dashboard.mjs").write_text(
            'import {configureService} from "./dashboard-app.mjs";\n'
            'const {webClientSettings} = await import("./web-client-settings.mjs");', encoding="utf-8")
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

    def bootstrap_revision(self):
        html = (self.output / "dashboard.html").read_text(encoding="utf-8")
        bootstrap = (self.output / "dashboard.mjs").read_text(encoding="utf-8")
        html_revision = re.search(r'dashboard\.mjs\?v=([0-9a-f]{64})', html).group(1)
        settings_revision = re.search(r'web-client-settings\.mjs\?v=([0-9a-f]{64})', bootstrap).group(1)
        self.assertEqual(html_revision, settings_revision)
        self.assertIn(f'"./dashboard-app.mjs?v={html_revision}"', bootstrap)
        home_html = (self.output / "index.html").read_text(encoding="utf-8")
        self.assertIn(f'home.mjs?v={html_revision}', home_html)
        home = (self.output / "home.mjs").read_text(encoding="utf-8")
        self.assertIn(f'./web-client-settings.mjs?v={html_revision}', home)
        return settings_revision

    def test_default_demo_has_no_authentication_or_backend_assets_and_ignores_source_settings(self):
        output = build_site(self.source, self.output)
        self.assertEqual(output, self.output.resolve())
        self.assertEqual(self.files(), set(WEB_FILES) | {GENERATED_SETTINGS, ".nojekyll"})
        module = (output / GENERATED_SETTINGS).read_text(encoding="utf-8")
        self.assertIn('"enabled": false', module)
        self.assertNotIn(PRIVATE, module)
        self.assertTrue(all(PRIVATE not in path.read_text(encoding="utf-8") for path in output.rglob("*") if path.is_file()))

    @unittest.skipUnless(NODE, "Node is required for enabled public settings")
    def test_homepage_session_helper_is_published_and_versioned_only_when_enabled(self):
        original_html = (self.source / "index.html").read_bytes()
        original_module = (self.source / "home.mjs").read_bytes()
        build_site(self.source, self.output)
        demo_revision = self.bootstrap_revision()
        self.assertNotIn("web-session-store.mjs", self.files())
        demo_module = (self.output / "home.mjs").read_text(encoding="utf-8")
        self.assertIn('import("./web-session-store.mjs")', demo_module)
        build_site(self.source, self.output, self.settings())
        enabled_revision = self.bootstrap_revision()
        self.assertNotEqual(demo_revision, enabled_revision)
        self.assertIn("web-session-store.mjs", self.files())
        enabled_module = (self.output / "home.mjs").read_text(encoding="utf-8")
        self.assertIn(f'import("./web-session-store.mjs?v={enabled_revision}")', enabled_module)
        self.assertEqual((self.source / "index.html").read_bytes(), original_html)
        self.assertEqual((self.source / "home.mjs").read_bytes(), original_module)

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
        enabled_revision = self.bootstrap_revision()
        build_site(self.source, self.output)
        self.assertEqual(self.files(), set(WEB_FILES) | {GENERATED_SETTINGS, ".nojekyll"})
        self.assertNotEqual(enabled_revision, self.bootstrap_revision())

    @unittest.skipUnless(NODE, "Node is required for enabled public settings")
    def test_connection_changes_version_both_bootstrap_requests_without_changing_source(self):
        source_html = (self.source / "dashboard.html").read_bytes()
        source_bootstrap = (self.source / "dashboard.mjs").read_bytes()
        build_site(self.source, self.output)
        demo_revision = self.bootstrap_revision()
        build_site(self.source, self.output, self.settings())
        first_revision = self.bootstrap_revision()
        self.assertNotEqual(demo_revision, first_revision)
        build_site(self.source, self.output, self.settings(clientId="replacementclient2"))
        self.assertNotEqual(first_revision, self.bootstrap_revision())
        self.assertEqual((self.source / "dashboard.html").read_bytes(), source_html)
        self.assertEqual((self.source / "dashboard.mjs").read_bytes(), source_bootstrap)
        self.assertEqual(self.files(), set(WEB_FILES) | set(CONNECTION_FILES) | {GENERATED_SETTINGS, ".nojekyll"})

    @unittest.skipUnless(NODE, "Node is required for enabled public settings")
    def test_equivalent_settings_produce_stable_module_cache_keys(self):
        settings = self.settings()
        build_site(self.source, self.output, settings)
        revision = self.bootstrap_revision()
        staged = {name: (self.output / name).read_bytes() for name in ("dashboard.html", "dashboard.mjs", GENERATED_SETTINGS)}
        raw = json.loads(settings.read_text(encoding="utf-8"))
        settings.write_text(json.dumps(dict(reversed(list(raw.items())))), encoding="utf-8")
        build_site(self.source, self.output, settings)
        self.assertEqual(self.bootstrap_revision(), revision)
        for name, expected in staged.items():
            self.assertEqual((self.output / name).read_bytes(), expected)

    def test_application_changes_invalidate_main_and_imported_module_urls(self):
        bootstrap = (self.source / "dashboard.mjs").read_bytes()
        build_site(self.source, self.output)
        first_revision = self.bootstrap_revision()
        module = self.source / "dashboard-app.mjs"
        module.write_bytes(module.read_bytes() + b"\n// Clear the connected workspace count.\n")
        build_site(self.source, self.output)
        next_revision = self.bootstrap_revision()
        self.assertNotEqual(first_revision, next_revision)
        staged = (self.output / "dashboard.mjs").read_text(encoding="utf-8")
        self.assertIn(f'from "./dashboard-app.mjs?v={next_revision}"', staged)
        self.assertNotIn(first_revision, staged)
        self.assertEqual((self.source / "dashboard.mjs").read_bytes(), bootstrap)

    def test_css_changes_version_stylesheet_links_and_module_graph_without_editing_source(self):
        pages = {
            "dashboard.html": '<link rel="stylesheet" href="styles.css"><script type="module" src="dashboard.mjs"></script>',
            "index.html": "<link href='./styles.css' rel='stylesheet'><link rel='stylesheet' href='home.css'>"
                          '<script type="module" src="home.mjs"></script>',
            "privacy.html": '<link rel="stylesheet" href="privacy.css">',
            "terms.html": '<link rel="stylesheet" href="privacy.css">',
        }
        for name, html in pages.items():
            (self.source / name).write_text(html, encoding="utf-8")
        build_site(self.source, self.output)
        first_revision = self.bootstrap_revision()
        for name, targets in {"dashboard.html": ("styles.css",), "index.html": ("./styles.css", "home.css"),
                              "privacy.html": ("privacy.css",), "terms.html": ("privacy.css",)}.items():
            staged = (self.output / name).read_text(encoding="utf-8")
            for target in targets:
                self.assertIn(f"{target}?v={first_revision}", staged)
        css = self.source / "styles.css"
        css.write_bytes(css.read_bytes() + b"\nbody { background: #f6f5ef; }\n")
        original_css = css.read_bytes()
        build_site(self.source, self.output)
        next_revision = self.bootstrap_revision()
        self.assertNotEqual(first_revision, next_revision)
        for name, original in pages.items():
            staged = (self.output / name).read_text(encoding="utf-8")
            self.assertIn(f"?v={next_revision}", staged)
            self.assertNotIn(first_revision, staged)
            self.assertEqual((self.source / name).read_text(encoding="utf-8"), original)
        self.assertEqual(css.read_bytes(), original_css)
        self.assertEqual((self.output / "styles.css").read_bytes(), original_css)

    def test_stylesheet_versioning_preserves_external_inline_and_non_stylesheet_references(self):
        untouched = [
            '<link rel="stylesheet" href="https://example.com/styles.css">',
            '<link rel="stylesheet" href="//example.com/styles.css">',
            '<link rel="stylesheet" href="private.css">',
            '<link rel="icon" href="styles.css">',
            '<link rel="preload" as="style" href="home.css">',
            '<!-- <link rel="stylesheet" href="styles.css"> -->',
            '<style>.preview { background: url("styles.css"); }</style>',
            '<script>const example = \'<link rel="stylesheet" href="styles.css">\';</script>',
        ]
        source = ('<!doctype html><html><head><LINK HREF="styles.css" REL="stylesheet" media="screen">'
                  "<link rel='stylesheet' href='home.css'/>" + "".join(untouched)
                  + '<script type="module" src="home.mjs"></script></head><body>Comment Lens</body></html>')
        (self.source / "index.html").write_text(source, encoding="utf-8")
        build_site(self.source, self.output)
        revision = self.bootstrap_revision()
        staged = (self.output / "index.html").read_text(encoding="utf-8")
        self.assertIn(f'<LINK HREF="styles.css?v={revision}" REL="stylesheet" media="screen">', staged)
        self.assertIn(f"<link rel='stylesheet' href='home.css?v={revision}'/>", staged)
        self.assertEqual(staged.count(f"?v={revision}"), 3)
        for fragment in untouched:
            self.assertIn(fragment, staged)
        self.assertEqual((self.source / "index.html").read_text(encoding="utf-8"), source)

    @unittest.skipUnless(NODE, "Node is required for enabled public settings")
    def test_all_transitive_published_module_imports_share_the_revision(self):
        app = ('import {demo} from "./demo-data.mjs";\n'
               "import './lib.mjs';\n"
               'export {summarize} from "./lib.mjs";\n'
               'const dynamic = import("./analysis-client.mjs");\n'
               'const ordinaryString = "./lib.mjs";\n'
               'const extensionOnly = import("./auth-session.mjs");\n')
        (self.source / "dashboard-app.mjs").write_text(app, encoding="utf-8")
        (self.source / "analysis-client.mjs").write_text(
            'import {validateResult} from "./result-model.mjs";\n', encoding="utf-8")
        (self.source / "result-model.mjs").write_text(
            'export {JobController} from "./job-controller.mjs";\n', encoding="utf-8")
        build_site(self.source, self.output, self.settings())
        revision = self.bootstrap_revision()
        for name, targets in {
            "dashboard-app.mjs": ("demo-data.mjs", "lib.mjs", "analysis-client.mjs"),
            "analysis-client.mjs": ("result-model.mjs",),
            "result-model.mjs": ("job-controller.mjs",),
            "web-client-config.mjs": ("client-config.mjs",),
        }.items():
            staged = (self.output / name).read_text(encoding="utf-8")
            for target in targets:
                self.assertIn(f"./{target}?v={revision}", staged)
        staged_app = (self.output / "dashboard-app.mjs").read_text(encoding="utf-8")
        self.assertIn(f"import './lib.mjs?v={revision}'", staged_app)
        self.assertIn(f'export {{summarize}} from "./lib.mjs?v={revision}"', staged_app)
        self.assertIn('const ordinaryString = "./lib.mjs";', staged_app)
        self.assertIn('import("./auth-session.mjs")', staged_app)
        self.assertNotIn("auth-session.mjs?v=", staged_app)
        self.assertEqual((self.source / "dashboard-app.mjs").read_text(encoding="utf-8"), app)

        # Disabled mode never stages or versions connection-only assets.
        build_site(self.source, self.output)
        demo_revision = self.bootstrap_revision()
        self.assertNotEqual(revision, demo_revision)
        demo_app = (self.output / "dashboard-app.mjs").read_text(encoding="utf-8")
        self.assertIn('import("./analysis-client.mjs")', demo_app)
        self.assertNotIn("analysis-client.mjs?v=", demo_app)
        self.assertEqual(self.files(), set(WEB_FILES) | {GENERATED_SETTINGS, ".nojekyll"})

    @unittest.skipUnless(NODE, "Node is required for enabled public settings")
    def test_checked_in_public_module_graph_has_no_unversioned_published_imports(self):
        for name in (*WEB_FILES, *CONNECTION_FILES):
            if name.endswith(".mjs"):
                (self.source / name).write_bytes((ROOT / "extension" / name).read_bytes())
        build_site(self.source, self.output, self.settings())
        revision = self.bootstrap_revision()
        published_modules = {name for name in self.files() if name.endswith(".mjs")}
        checked_references = 0
        for name in published_modules:
            staged = (self.output / name).read_text(encoding="utf-8")
            # These known source files use plain ./module.mjs literals for all
            # imports. Read them independently of the builder's import pattern.
            for target, version in re.findall(r'''["'](\./[^'"\r\n?#]+\.mjs)(?:\?v=([0-9a-f]{64}))?["']''', staged):
                if target[2:] in published_modules:
                    self.assertEqual(version, revision, f"{name} -> {target}")
                    checked_references += 1
                else:
                    self.assertEqual(version, "", f"Unpublished module: {target}")
        self.assertGreaterEqual(checked_references, 15)

    def test_missing_or_duplicate_bootstrap_markers_fail_before_writing_output(self):
        for name, value in (("dashboard.html", '<script src="dashboard.mjs"></script>'),
                            ("index.html", '<script src="home.mjs"></script>'),
                            ("index.html", '<script type="module" src="home.mjs"></script>' * 2),
                            ("dashboard.mjs", 'import("./web-client-settings.mjs");import("./web-client-settings.mjs");')):
            with self.subTest(asset=name):
                path = self.source / name
                original = path.read_bytes()
                path.write_text(value, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "bootstrap reference"):
                    build_site(self.source, self.output)
                self.assertFalse(self.output.exists())
                path.write_bytes(original)

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
