"""PWA frontend quality checks — HTML structure + JS syntax + DOM consistency + i18n."""

import json
import os
import re
import subprocess
import tempfile

import pytest

HTML_PATH = os.path.join(os.path.dirname(__file__), "..", "src", "intercom.html")
I18N_PATH = os.path.join(os.path.dirname(__file__), "..", "src", "static", "i18n.js")

# All translation keys required in both zh-CN and en — single source of truth
I18N_REQUIRED_KEYS = [
    "appTitle",
    "appHint",
    "broadcastAll",
    "statusReady",
    "statusRecording",
    "statusSending",
    "statusSent",
    "statusFailed",
    "statusNetworkError",
    "statusLoadFailed",
    "micError",
    "langLabel",
]

CHINESE_STATUS_STRINGS = [
    "\u6309\u4f4f\u5f55\u97f3",  # 按住录音
    "\u51c6\u5907\u597d",  # 准备好
    "\u5f55\u97f3\u4e2d",  # 录音中
    "\u53d1\u9001\u4e2d",  # 发送中
    "\u5df2\u53d1\u9001",  # 已发送
    "\u7f51\u7edc\u9519\u8bef",  # 网络错误
    "\u52a0\u8f7d\u5931\u8d25",  # 加载失败
    "\u5168\u90e8\u5e7f\u64ad",  # 全部广播
]


@pytest.fixture
def html_content():
    with open(HTML_PATH) as f:
        return f.read()


def _extract_inline_js(html):
    """Extract the inline <script> block (not the i18n.js external load)."""
    matches = list(re.finditer(r"<script>(.*?)</script>", html, re.DOTALL))
    for m in matches:
        inner = m.group(1).strip()
        if len(inner) > 100:  # inline JS, not the empty external script tag
            return inner
    return ""


class TestHtmlStructure:
    def test_has_doctype(self, html_content):
        assert html_content.strip().startswith("<!DOCTYPE html>")

    def test_has_lang_attribute(self, html_content):
        assert 'lang="zh-CN"' in html_content

    def test_has_meta_viewport(self, html_content):
        assert 'name="viewport"' in html_content

    def test_no_console_log_left(self, html_content):
        """Production PWA should have no console.log debugging."""
        assert "console.log" not in html_content

    def test_no_debugger_statements(self, html_content):
        assert "debugger" not in html_content

    def test_script_tags(self, html_content):
        """One external i18n.js + one inline script."""
        assert html_content.count("<script") == 2
        assert html_content.count("</script>") == 2

    def test_style_tag_closed(self, html_content):
        assert html_content.count("<style>") == 1
        assert html_content.count("</style>") == 1

    def test_body_tag_closed(self, html_content):
        assert html_content.count("<body>") == 1
        assert html_content.count("</body>") == 1

    def test_loads_i18n_js(self, html_content):
        assert '<script src="/static/i18n.js">' in html_content

    def test_has_lang_toggle(self, html_content):
        assert 'id="lang-toggle"' in html_content
        assert "I18N.toggleLang()" in html_content

    def test_has_data_i18n_attributes(self, html_content):
        """Broadcast name should use data-i18n, not fragile nth-child selectors."""
        assert 'data-i18n="broadcastAll"' in html_content


class TestJsSyntax:
    def test_node_check_passes(self):
        """Extract inline JS and run node --check for syntax errors.

        Skips if Node.js is not installed (dev machine) — CI has it.
        """
        try:
            subprocess.run(["node", "--version"], capture_output=True, timeout=5, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            pytest.skip("Node.js not available — skipping JS syntax check")

        with open(HTML_PATH) as f:
            html = f.read()

        js_code = _extract_inline_js(html)
        assert js_code, "No inline JS block found in HTML"
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

    def test_i18n_js_syntax(self):
        """i18n.js should also pass node --check."""
        try:
            subprocess.run(["node", "--version"], capture_output=True, timeout=5, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            pytest.skip("Node.js not available — skipping JS syntax check")

        result = subprocess.run(
            ["node", "--check", I18N_PATH],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"i18n.js syntax error:\n{result.stderr}"


class TestDomConsistency:
    """Verify JS-referenced DOM IDs actually exist in HTML."""

    def test_all_js_element_ids_exist(self):
        with open(HTML_PATH) as f:
            html = f.read()

        js = _extract_inline_js(html)
        assert js

        id_refs = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", js))
        id_refs |= set(re.findall(r"querySelector\(['\"]#([a-zA-Z0-9_-]+)['\"]\)", js))

        for ref_id in id_refs:
            assert f'id="{ref_id}"' in html or f"id='{ref_id}'" in html, (
                f"JS references id='{ref_id}' but it does not exist in HTML"
            )

    def test_room_card_prefix_consistent(self, html_content):
        assert "card-' + target" in html_content or 'card-" + target' in html_content
        assert "card-" in html_content


class TestI18N:
    """Translation module quality checks."""

    def test_i18n_file_exists(self):
        assert os.path.exists(I18N_PATH), "i18n.js is missing"
        assert os.path.getsize(I18N_PATH) > 100

    def test_all_keys_present(self):
        """Every required key must appear as a property in i18n.js."""
        with open(I18N_PATH) as f:
            i18n = f.read()

        for key in I18N_REQUIRED_KEYS:
            # Robust: just grep for 'key:' — works regardless of whitespace/indentation
            assert f"{key}:" in i18n, (
                f"Key '{key}' not found in i18n.js (expected '{key}:' pattern)"
            )

    def test_both_languages_have_all_keys(self):
        """Each required key must appear at least twice (once in zh-CN, once in en)."""
        with open(I18N_PATH) as f:
            i18n = f.read()

        for key in I18N_REQUIRED_KEYS:
            # Each key should appear in both language blocks → at least 2 occurrences
            count = i18n.count(f"{key}:")
            assert count >= 2, (
                f"Key '{key}' appears only {count} time(s) in i18n.js — "
                f"expected at least 2 (zh-CN + en)"
            )

    def test_html_uses_i18n_t(self, html_content):
        """All user-facing strings in JS should use I18N.t()."""
        js = _extract_inline_js(html_content)
        for literal in [
            "\u51c6\u5907\u597d",
            "\u5f55\u97f3\u4e2d",
            "\u53d1\u9001\u4e2d",
            "\u5df2\u53d1\u9001",
            "\u7f51\u7edc\u9519\u8bef",
            "\u52a0\u8f7d\u5931\u8d25",
            "\u5168\u90e8\u5e7f\u64ad",
        ]:
            assert literal not in js, (
                f"Hardcoded Chinese string '{literal}' found in JS — use I18N.t() instead"
            )

    def test_html_body_no_hardcoded_chinese(self, html_content):
        """Inline JS must not contain hardcoded Chinese status strings."""
        js = _extract_inline_js(html_content)
        for literal in CHINESE_STATUS_STRINGS:
            assert literal not in js, f"Hardcoded Chinese '{literal}' in JS — use I18N.t() instead"

    def test_data_room_name_attributes(self, html_content):
        """Room cards must have data-room-name for i18n room name switching."""
        assert "data-room-name=" in html_content
        assert "window._ROOM_DATA" in html_content


class TestManifestAndIcons:
    def test_manifest_json_valid(self):
        manifest_path = os.path.join(os.path.dirname(__file__), "..", "src", "manifest.json")

        with open(manifest_path) as f:
            manifest = json.load(f)

        assert manifest["name"] == "\u5bb6\u5ead\u5e7f\u64ad"
        assert "icons" in manifest
        assert len(manifest["icons"]) > 0

    def test_icon_files_exist(self):
        static_dir = os.path.join(os.path.dirname(__file__), "..", "src", "static")
        expected = [
            "icon-192.png",
            "icon-512.png",
            "favicon-32.png",
            "apple-touch-icon.png",
            "i18n.js",
        ]
        for fname in expected:
            path = os.path.join(static_dir, fname)
            assert os.path.exists(path), f"Missing file: {fname}"
            assert os.path.getsize(path) > 0, f"Empty file: {fname}"
