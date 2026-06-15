"""PWA 前端质量检查 — HTML 结构 + JS 语法 + DOM 一致性"""

import json
import os
import re
import subprocess
import tempfile

import pytest

HTML_PATH = os.path.join(os.path.dirname(__file__), "..", "src", "intercom.html")


@pytest.fixture
def html_content():
    with open(HTML_PATH) as f:
        return f.read()


class TestHtmlStructure:
    def test_has_doctype(self, html_content):
        assert html_content.strip().startswith("<!DOCTYPE html>")

    def test_has_lang_attribute(self, html_content):
        assert 'lang="zh-CN"' in html_content

    def test_has_meta_viewport(self, html_content):
        assert 'name="viewport"' in html_content

    def test_no_console_log_left(self, html_content):
        """Production PWA should have no console.log debugging"""
        assert "console.log" not in html_content

    def test_no_debugger_statements(self, html_content):
        assert "debugger" not in html_content

    def test_script_tag_closed(self, html_content):
        assert html_content.count("<script>") == 1
        assert html_content.count("</script>") == 1

    def test_style_tag_closed(self, html_content):
        assert html_content.count("<style>") == 1
        assert html_content.count("</style>") == 1

    def test_body_tag_closed(self, html_content):
        assert html_content.count("<body>") == 1
        assert html_content.count("</body>") == 1


class TestJsSyntax:
    def test_node_check_passes(self):
        """Extract inline JS and run node --check for syntax errors.

        Skips if Node.js is not installed (dev machine) — CI has it.
        """
        # Check if node is available
        try:
            subprocess.run(["node", "--version"], capture_output=True, timeout=5, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            pytest.skip("Node.js not available — skipping JS syntax check")

        with open(HTML_PATH) as f:
            html = f.read()

        # Extract content between <script> and </script>
        match = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
        assert match, "No <script> block found in HTML"

        js_code = match.group(1).strip()
        assert len(js_code) > 100, "JS code seems too short — extraction may be broken"

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".js", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(js_code)
            tmp_path = tmp.name

        try:
            result = subprocess.run(
                ["node", "--check", tmp_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 0, f"JS syntax error:\n{result.stderr}"
        finally:
            os.unlink(tmp_path)


class TestDomConsistency:
    """Verify JS-referenced DOM IDs actually exist in HTML"""

    def test_all_js_element_ids_exist(self):
        with open(HTML_PATH) as f:
            html = f.read()

        # Extract JS
        match = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
        assert match
        js = match.group(1)

        # Find all getElementById('xxx') calls
        id_refs = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", js))
        # Also querySelector with #id
        id_refs |= set(re.findall(r"querySelector\(['\"]#([a-zA-Z0-9_-]+)['\"]\)", js))

        for ref_id in id_refs:
            assert f'id="{ref_id}"' in html or f"id='{ref_id}'" in html, (
                f"JS references id='{ref_id}' but it does not exist in HTML"
            )

    def test_room_card_prefix_consistent(self, html_content):
        """JS generates card-<target> IDs — verify the format is consistent"""
        assert "card-' + target" in html_content or 'card-" + target' in html_content
        assert "card-" in html_content


class TestManifestAndIcons:
    def test_manifest_json_valid(self):
        manifest_path = os.path.join(os.path.dirname(__file__), "..", "src", "manifest.json")

        with open(manifest_path) as f:
            manifest = json.load(f)

        assert manifest["name"] == "家庭广播"
        assert "icons" in manifest
        assert len(manifest["icons"]) > 0

    def test_icon_files_exist(self):
        static_dir = os.path.join(os.path.dirname(__file__), "..", "src", "static")
        expected = [
            "icon-192.png",
            "icon-512.png",
            "favicon-32.png",
            "apple-touch-icon.png",
        ]
        for fname in expected:
            path = os.path.join(static_dir, fname)
            assert os.path.exists(path), f"Missing icon: {fname}"
            assert os.path.getsize(path) > 0, f"Empty icon: {fname}"
