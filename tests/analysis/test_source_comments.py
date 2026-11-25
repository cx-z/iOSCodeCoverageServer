"""Comment-only proof must not classify literal contents or executable tokens as comments."""
import unittest

from ioscoverage.analysis.mapping.source_comments import comment_only_lines


class SourceCommentTests(unittest.TestCase):
    """Checks full-file lexical context without any compiler or Build input."""

    def test_line_doc_and_inline_comments(self):
        """Only lines with comments and no code are excluded, including an EOF comment."""
        source = ' // line\n /// doc\nlet x = 1 // executable\n/* comment */ run()\n/* pure */\n\n// eof'
        self.assertEqual(comment_only_lines('File.swift', source), {1, 2, 5, 7})

    def test_nested_blocks_and_unchanged_context(self):
        """Comment bodies, blank bodies and closing markers require the full source context."""
        source = 'let x = 1 /* begin\n body\n /* nested */\n\n end */\nrun()\n'
        self.assertEqual(comment_only_lines('File.swift', source), {2, 3, 4, 5})

    def test_swift_strings_raw_literals_and_interpolation(self):
        """Comment-looking text in ordinary/raw/multiline strings remains source data."""
        source = '''let url = "https://example.test/*path*/"
let text = "escaped \\" // still string"
let raw = ##"literal " /* still string */"##
let multi = #"""
// string content
/* string content */
"""#
let interpolated = "value \\(flag ? "/*" : "//")"
/// actual documentation
'''
        self.assertEqual(comment_only_lines('File.swift', source), {9})

    def test_extended_regex_is_literal_data(self):
        """Extended regex bodies cannot start comment state."""
        source = 'let regex = #/\n// data\n/* data */\n/#\n// comment\n'
        self.assertEqual(comment_only_lines('File.swift', source), {5})

    def test_c_family_strings_characters_and_raw_strings(self):
        """C-family literals and non-nesting block comments retain their own lexical rules."""
        source = '''const char *url = "https://example.test";
char quote = '\\"';
const char *raw = R"tag(
// data
/* data */
)tag";
/* block
 body */
// eof
'''
        for path in ('File.m', 'File.mm', 'File.c', 'File.cpp', 'File.h'):
            with self.subTest(path=path):
                self.assertEqual(comment_only_lines(path, source), {7, 8, 9})

    def test_unrecognized_language_is_not_guessed(self):
        """Unsupported source syntax does not acquire a comment-only classification."""
        self.assertEqual(comment_only_lines('File.custom', '// data\n'), set())


    def test_swift_bare_regex_character_class_and_division(self):
        """Regex character classes containing comment delimiters cannot consume later code."""
        source = 'let regex = /[/*]/\nrun()\n// real comment\nlet divided = a / b /* comment\n body */\n'
        self.assertEqual(comment_only_lines('File.swift', source), {3, 5})
