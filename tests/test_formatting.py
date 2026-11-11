from __future__ import annotations

import unittest

from .helper import module

fmt = module("formatting")


class RenderHtmlTests(unittest.TestCase):
    def test_bold_italic_and_code(self):
        out = fmt.render_html("**bold** *italic* `x < y`")
        self.assertIn("<b>bold</b>", out)
        self.assertIn("<i>italic</i>", out)
        self.assertIn("<pre>x &lt; y</pre>", out)

    def test_plain_html_is_escaped(self):
        out = fmt.render_html("<script>alert(1)</script>")
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script>", out)

    def test_link(self):
        out = fmt.render_html("[Hermes](https://example.com/a?x=1&y=2)")
        self.assertIn('<a href="https://example.com/a?x=1&amp;y=2">Hermes</a>', out)

    def test_non_http_link_is_not_converted(self):
        out = fmt.render_html("[x](javascript:alert(1))")
        self.assertNotIn("<a href", out)
        self.assertIn("javascript:alert(1)", out)

    def test_fenced_code_is_preserved(self):
        out = fmt.render_html("before\n```python\nprint('<x>')\n```\nafter")
        self.assertIn("<pre>", out)
        self.assertIn("print('&lt;x&gt;')", out)
        self.assertNotIn("```", out)

    def test_unterminated_fence_renders(self):
        out = fmt.render_html("```\nabc")
        self.assertIn("<pre>", out)
        self.assertIn("abc", out)

    def test_headings(self):
        self.assertIn("<b>Title</b>", fmt.render_html("# Title"))


class SplitTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(fmt.split_text("hello"), ["hello"])
        self.assertEqual(fmt.split_text(""), [])

    def test_utf16_length(self):
        self.assertEqual(fmt.utf16_len("abc"), 3)
        self.assertEqual(fmt.utf16_len("😀"), 2)

    def test_truncate_utf16_does_not_split_emoji(self):
        self.assertEqual(fmt.truncate_utf16("a😀b", 2), "a")
        self.assertEqual(fmt.truncate_utf16("a😀b", 3), "a😀")

    def test_long_text_splits_under_limit(self):
        text = "\n\n".join(f"paragraph {i} " + "x" * 300 for i in range(40))
        chunks = fmt.split_text(text, 4096)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(fmt.utf16_len(chunk), 4096)
        joined = "".join(chunks)
        self.assertIn("paragraph 0", joined)
        self.assertIn("paragraph 39", joined)

    def test_fence_split_is_reopened(self):
        text = "```python\n" + "\n".join(f"line {i} " + "y" * 50 for i in range(200)) + "\n```"
        chunks = fmt.split_text(text, 1000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(fmt.utf16_len(chunk), 1000)

    def test_plain_fallback_keeps_content(self):
        self.assertIn("hello", fmt.plain_fallback("**hello**"))


if __name__ == "__main__":
    unittest.main()
