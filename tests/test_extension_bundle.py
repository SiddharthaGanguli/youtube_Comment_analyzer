import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.build_extension_bundle import (
    BUNDLE_FILES, MARKER_FILE, ROOT, RUNTIME_FILES, build_extension_bundle,
)


NODE = shutil.which("node")
VALIDATOR = ROOT / "extension" / "client-config.mjs"


@unittest.skipUnless(NODE and VALIDATOR.is_file(), "Node and the extension settings validator are required")
class ExtensionBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="extension-bundle-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.output = self.root / "bundle"
        for name in RUNTIME_FILES:
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"fixture: {name}\n", encoding="utf-8")
        # Run the actual application validator, not a second Python implementation.
        (self.source / "client-config.mjs").write_bytes(VALIDATOR.read_bytes())
        (self.source / "client-settings.mjs").write_text(
            "export const clientSettings = {enabled: true, apiKey: 'must-not-be-copied'};",
            encoding="utf-8",
        )

    def settings(self, **changes):
        settings = {
            "enabled": True,
            "apiBaseUrl": "https://abc123.execute-api.us-east-1.amazonaws.com/v1/",
            "cognitoDomain": "https://comment-lens.auth.us-east-1.amazoncognito.com/",
            "clientId": "publicclient123",
            "extensionId": "a" * 32,
            "resultBaseUrl": "https://comment-lens-results.s3.us-east-1.amazonaws.com/serving/production/",
        }
        settings.update(changes)
        return settings

    def write_settings(self, raw):
        path = self.root / "public-settings.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def build(self, settings=None, **kwargs):
        return build_extension_bundle(self.source, self.output, settings=settings, **kwargs)

    def test_demo_bundle_contains_only_runtime_assets_and_no_network_permissions(self):
        for name in ("README.md", ".env", "tests/hidden.test.mjs", "config/secret.json", "backend.py",
                     "web-session-store.mjs", "web-auth-session.mjs", "web-client-settings.mjs"):
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("never include this", encoding="utf-8")
        built = self.build()
        self.assertEqual(built, self.output.resolve())
        actual = {path.relative_to(built).as_posix() for path in built.rglob("*") if path.is_file()}
        self.assertEqual(actual, BUNDLE_FILES)
        manifest = json.loads((built / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "0.2.0")
        self.assertEqual(manifest["permissions"], ["activeTab"])
        self.assertNotIn("host_permissions", manifest)
        self.assertNotIn("oauth2", manifest)
        module = (built / "client-settings.mjs").read_text(encoding="utf-8")
        self.assertIn('Object.freeze({"enabled": false})', module)
        self.assertNotIn("must-not-be-copied", module)
        self.assertEqual((built / "assets/developed-with-youtube.png").read_bytes(),
                         (self.source / "assets/developed-with-youtube.png").read_bytes())
        self.assertEqual((built / "home.mjs").read_bytes(), (self.source / "home.mjs").read_bytes())
        self.assertNotIn("web-session-store.mjs", actual)
        self.assertNotIn("web-auth-session.mjs", actual)
        self.assertNotIn("web-client-settings.mjs", actual)

    def test_enabled_bundle_has_exact_origins_and_normalized_public_settings(self):
        self.build(self.write_settings(self.settings()))
        manifest = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["permissions"], ["activeTab", "identity", "storage"])
        self.assertEqual(manifest["host_permissions"], [
            "https://abc123.execute-api.us-east-1.amazonaws.com/*",
            "https://comment-lens.auth.us-east-1.amazoncognito.com/*",
            "https://comment-lens-results.s3.us-east-1.amazonaws.com/*",
        ])
        self.assertNotIn("<all_urls>", json.dumps(manifest))
        self.assertEqual(manifest["action"]["default_popup"], "popup.html")
        settings_path = self.output / "client-settings.mjs"
        imported = subprocess.run(
            [NODE, "--input-type=module", "--eval",
             "import {pathToFileURL} from 'node:url'; const {clientSettings} = await import(pathToFileURL(process.argv[1]).href); process.stdout.write(JSON.stringify(clientSettings));",
             "--", str(settings_path)], capture_output=True, text=True, timeout=10, check=True,
        )
        checked = json.loads(imported.stdout)
        self.assertEqual(checked["pollScheduleSeconds"], [2, 5, 10])
        self.assertEqual(checked["maxComments"], 1000)
        self.assertFalse(checked["apiBaseUrl"].endswith("/"))
        self.assertFalse(checked["resultBaseUrl"].endswith("/"))

    def test_unknown_secret_fields_are_rejected_without_echoing_values(self):
        for key in ("apiKey", "clientSecret", "accessToken", "awsSecretAccessKey", "password"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as error:
                    self.build(self.write_settings(self.settings(**{key: "sensitive-test-value"})))
                self.assertNotIn("sensitive-test-value", str(error.exception))
                self.assertFalse(self.output.exists())

    def test_invalid_connection_settings_fail_before_writing(self):
        cases = [
            {"apiBaseUrl": "http://api.example.com"},
            {"apiBaseUrl": "https://user:private@api.example.com"},
            {"apiBaseUrl": "https://api.example.com?key=private"},
            {"apiBaseUrl": "https://*.example.com"},
            {"apiBaseUrl": "https://localhost"},
            {"cognitoDomain": "https://login.example.com"},
            {"resultBaseUrl": "https://comment-lens-results.s3.us-east-1.amazonaws.com/public"},
            {"extensionId": "invalid"}, {"maxComments": 1001},
            {"pollScheduleSeconds": [1, 2, 3]},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.build(self.write_settings(self.settings(**changes)))
                self.assertFalse(self.output.exists())

    def test_invalid_json_duplicate_keys_and_oversized_settings_are_rejected(self):
        path = self.root / "public-settings.json"
        for text in ('{"enabled":false,"enabled":true}', '{"enabled":false,"maxComments":NaN}',
                     '[]', '{"enabled":0}', '{broken', ' ' * (16 * 1024 + 1)):
            with self.subTest(text=text[:60]):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.build(path)
                self.assertFalse(self.output.exists())

    def test_disabled_settings_cannot_carry_unused_connection_fields(self):
        with self.assertRaises(ValueError):
            self.build(self.write_settings({"enabled": False, "clientId": "unused"}))
        self.assertFalse(self.output.exists())

    def test_enabled_services_require_three_distinct_origins(self):
        settings = self.settings(apiBaseUrl="https://comment-lens.auth.us-east-1.amazoncognito.com/api")
        with self.assertRaisesRegex(ValueError, "three distinct origins"):
            self.build(self.write_settings(settings))
        self.assertFalse(self.output.exists())

    def test_source_and_destination_must_not_overlap_in_either_direction(self):
        original = (self.source / "dashboard.html").read_bytes()
        for destination in (self.source, self.source / "nested", self.root):
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(ValueError, "overlap"):
                    build_extension_bundle(self.source, destination)
                self.assertEqual((self.source / "dashboard.html").read_bytes(), original)
        self.assertFalse((self.source / "nested").exists())

    def test_arbitrary_existing_directory_is_not_overwritten_even_with_known_filenames(self):
        self.output.mkdir()
        target = self.output / "dashboard.html"
        target.write_text("user's existing dashboard", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not an owned"):
            self.build()
        self.assertEqual(target.read_text(encoding="utf-8"), "user's existing dashboard")
        self.assertFalse((self.output / MARKER_FILE).exists())

    def test_unexpected_files_and_directories_in_owned_output_are_preserved(self):
        self.build()
        target = self.output / "dashboard.html"
        original = target.read_bytes()
        user_file = self.output / "keep-my-notes.txt"
        user_file.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unexpected files"):
            self.build()
        self.assertEqual(user_file.read_text(encoding="utf-8"), "keep")
        self.assertEqual(target.read_bytes(), original)
        user_file.unlink()
        user_directory = self.output / "other-project"
        user_directory.mkdir()
        with self.assertRaisesRegex(ValueError, "unexpected directories"):
            self.build()
        self.assertTrue(user_directory.is_dir())

    def test_owned_bundle_can_be_rebuilt_and_switched_back_to_demo(self):
        self.build(self.write_settings(self.settings()))
        (self.source / "dashboard.html").write_text("updated dashboard", encoding="utf-8")
        self.build()
        self.assertEqual((self.output / "dashboard.html").read_text(encoding="utf-8"), "updated dashboard")
        manifest = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["permissions"], ["activeTab"])
        self.assertNotIn("host_permissions", manifest)

    def test_missing_runtime_input_does_not_create_output(self):
        (self.source / "job-controller.mjs").unlink()
        with self.assertRaisesRegex(ValueError, "runtime file"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_settings_cannot_be_an_output_file(self):
        self.output.mkdir()
        settings = self.output / "client-settings.mjs"
        settings.write_text('{"enabled":false}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside the output"):
            self.build(settings)
        self.assertEqual(settings.read_text(encoding="utf-8"), '{"enabled":false}')

    def symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"Symlinks are unavailable: {error}")

    def test_linked_runtime_file_is_rejected_without_touching_target(self):
        target = self.root / "external.txt"
        target.write_text("external data", encoding="utf-8")
        link = self.source / "dashboard.html"
        link.unlink()
        self.symlink(link, target)
        with self.assertRaisesRegex(ValueError, "symlinks or junctions"):
            self.build()
        self.assertEqual(target.read_text(encoding="utf-8"), "external data")
        self.assertFalse(self.output.exists())

    def test_linked_output_ancestor_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        parent = self.root / "linked-parent"
        self.symlink(parent, outside, directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks or junctions"):
            build_extension_bundle(self.source, parent / "bundle")
        self.assertEqual(list(outside.iterdir()), [])

    def test_link_added_to_owned_output_is_rejected(self):
        self.build()
        outside = self.root / "outside.txt"
        outside.write_text("preserve", encoding="utf-8")
        target = self.output / "dashboard.html"
        target.unlink()
        self.symlink(target, outside)
        with self.assertRaisesRegex(ValueError, "symlink or junction"):
            self.build()
        self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")

    @unittest.skipUnless(os.name == "nt" and hasattr(Path(), "is_junction"), "Windows junctions require Python 3.12+")
    def test_windows_output_junction_is_rejected_without_writing_target(self):
        import _winapi

        outside = self.root / "outside"
        outside.mkdir()
        parent = self.root / "junction-parent"
        _winapi.CreateJunction(str(outside), str(parent))
        self.assertTrue(parent.is_junction())
        with self.assertRaisesRegex(ValueError, "symlinks or junctions"):
            build_extension_bundle(self.source, parent / "bundle")
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(os.name == "nt" and hasattr(Path(), "is_junction"), "Windows junctions require Python 3.12+")
    def test_windows_source_junction_is_rejected_without_copying_target(self):
        import _winapi

        outside = self.root / "external-assets"
        outside.mkdir()
        target = outside / "developed-with-youtube.png"
        target.write_bytes(b"preserve external asset")
        assets = self.source / "assets"
        (assets / "developed-with-youtube.png").unlink()
        assets.rmdir()
        _winapi.CreateJunction(str(outside), str(assets))
        with self.assertRaisesRegex(ValueError, "symlinks or junctions"):
            self.build()
        self.assertEqual(target.read_bytes(), b"preserve external asset")
        self.assertFalse(self.output.exists())

    def test_replacing_output_hard_link_preserves_external_file(self):
        self.build()
        outside = self.root / "outside.txt"
        outside.write_text("preserve", encoding="utf-8")
        target = self.output / "dashboard.html"
        target.unlink()
        try:
            os.link(outside, target)
        except OSError as error:
            self.skipTest(f"Hard links are unavailable: {error}")
        self.build()
        self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")
        self.assertEqual(target.read_bytes(), (self.source / "dashboard.html").read_bytes())

    def test_validator_failure_does_not_expose_its_exception_text(self):
        (self.source / "client-config.mjs").write_text(
            "export function validateClientConfig() {throw new Error('private-validation-value');}",
            encoding="utf-8",
        )
        with self.assertRaises(ValueError) as error:
            self.build()
        self.assertNotIn("private-validation-value", str(error.exception))
        self.assertFalse(self.output.exists())

    def test_node_is_required_and_failure_does_not_create_output(self):
        with patch("scripts.build_extension_bundle.shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "Node.js is required"):
                self.build()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
