"""Semantic counterexamples from the independent classification review."""

import unittest
from ioscoverage.analysis.mapping.source_comments import comment_only_lines
from ioscoverage.analysis.mapping.source_non_executable import non_executable_lines


class ClassificationReviewTests(unittest.TestCase):
    """Executable controls must remain eligible; declaration/comment controls must be excluded."""

    def test_declaration_prefix_does_not_hide_a_following_statement(self):
        """An exclusion must prove the entire physical line, not just its first declaration."""
        for path, source in (
            ('File.swift', 'func run() {\n var x: Int; sideEffect()\n typealias ID = Int; sideEffect()\n}\n'),
            ('File.c', 'void run(void) {\n typedef int ID; sideEffect();\n}\n'),
            ('File.cpp', 'void run() {\n using ID = int; sideEffect();\n}\n')):
            with self.subTest(path=path):
                excluded = non_executable_lines(path, source)
                self.assertNotIn(2, excluded)
                if path.endswith('swift'):
                    self.assertNotIn(3, excluded)

    def test_elaborated_cpp_object_definitions_can_construct(self):
        """Type declarations and object definitions using the same type prefix are distinct."""
        source = 'struct Widget { Widget(); };\nvoid run() {\n struct Widget object;\n struct Widget another{};\n struct Widget third(1);\n}\n'
        excluded = non_executable_lines('File.cpp', source)
        self.assertTrue(all(number not in excluded for number in (3, 4, 5)))
        self.assertIn(1, non_executable_lines('File.cpp', 'struct Forward;\n'))

    def test_directive_continuation_ends_on_comment_or_blank_row(self):
        """A continuation flag cannot survive a physical row without a splice."""
        for ending in ('// replacement comment', ''):
            source = 'void run(void) {\n#define FLAG ' + '\\' + '\n' + ending + '\nsideEffect();\n}\n'
            with self.subTest(ending=ending):
                self.assertNotIn(4, non_executable_lines('File.c', source))

    def test_unary_cast_calls_are_not_objc_method_declarations(self):
        """Only actual method signatures in Objective-C declaration context are metadata."""
        for path in ('File.c', 'File.cpp', 'File.m', 'File.mm'):
            source = 'void run(void) {\n -(int)sideEffect();\n +(int)sideEffect();\n}\n'
            with self.subTest(path=path):
                excluded = non_executable_lines(path, source)
                self.assertNotIn(2, excluded)
                self.assertNotIn(3, excluded)
        objc = '@implementation Model\n- (void)run {\n -(int)sideEffect();\n}\n@end\n'
        self.assertNotIn(3, non_executable_lines('File.m', objc))

    def test_function_parameter_vla_bound_can_execute(self):
        """Array-bound evaluation on function entry remains measurable."""
        source = 'void run(int values[size()]) {\n use(values);\n}\n'
        self.assertNotIn(1, non_executable_lines('File.c', source))
        self.assertIn(1, non_executable_lines('File.c', 'void run(int values[10]) {\n use(values);\n}\n'))

    def test_initializer_closure_is_not_a_property_accessor_block(self):
        """An ordinary trailing-closure get invocation cannot inherit accessor ownership."""
        source = 'let value: Int = {\n get() {\n  sideEffect()\n }\n return 1\n}()\n'
        self.assertNotIn(2, non_executable_lines('File.swift', source))

    def test_spliced_comment_delimiters_preserve_physical_runtime_lines(self):
        """C phase-two splicing joins delimiters before comment recognition."""
        source = 'int run(void) {\n/* comment *' + '\\' + '\n/ return sideEffect();\n}\n'
        self.assertEqual(comment_only_lines('File.c', source), {2})
        self.assertNotIn(3, non_executable_lines('File.c', source))
        opened = '/' + '\\' + '\n* comment */\nrun();\n'
        self.assertEqual(comment_only_lines('File.c', opened), {1, 2})
        line = '/' + '\\' + '\n/ comment\nrun();\n'
        self.assertEqual(comment_only_lines('File.c', line), {1, 2})

    def test_cpp_digit_separator_cannot_open_a_character_literal(self):
        """Decimal/hex/float numeric separators leave following comments in code context."""
        for number in ("1'000", "0xA'B", "1.0'0", "1e1'0"):
            source = 'auto n = ' + number + ';\n// documentation\nint m = 2;\n'
            with self.subTest(number=number):
                self.assertEqual(comment_only_lines('File.cpp', source), {2})
                self.assertIn(2, non_executable_lines('File.cpp', source))

    def test_objc_selector_continuation_is_metadata(self):
        """A complete method signature can span physical lines without open parentheses."""
        source = '@interface Model\n- (void)run:(int)value\n otherValue:(int)other;\n@end\n'
        self.assertEqual(set(non_executable_lines('File.m', source)), {1, 2, 3, 4})

    def test_swift_associated_values_and_return_type_continuations(self):
        """Enum associated-value types and method return types are declaration-owned."""
        enum = 'enum Item {\n case value(\n  id: Int,\n  name: String\n )\n}\n'
        self.assertEqual(set(non_executable_lines('File.swift', enum)), set(range(1, 7)))
        function = 'func value()\n -> Int\n{\n return compute()\n}\n'
        self.assertEqual(set(non_executable_lines('File.swift', function)), {1, 2, 3, 5})
        default = 'enum Item {\n case value(\n  id: Int = nextID()\n )\n}\n'
        self.assertNotIn(3, non_executable_lines('File.swift', default))

    def test_swift_enum_raw_constants_cover_literal_spellings(self):
        """Hexadecimal, separated and multiple raw values are compile-time enum metadata."""
        source = 'enum Bits: Int {\n case first = 0x01\n case second = 1_000\n case third = 3, fourth = 0b100\n}\n'
        self.assertEqual(set(non_executable_lines('File.swift', source)), set(range(1, 6)))
        call = 'enum Item {\n case value(id: Int = nextID())\n}\n'
        self.assertNotIn(2, non_executable_lines('File.swift', call))

    def test_long_division_line_keeps_following_comment_and_call(self):
        """Long slash-heavy expressions remain code and do not leak lexical state."""
        source = 'let x = ' + 'a / b + ' * 20000 + '1\n// comment\nrun()\n'
        self.assertEqual(comment_only_lines('File.swift', source), {2})
        self.assertNotIn(3, non_executable_lines('File.swift', source))

    def test_cpp_raw_payload_cannot_acquire_a_spliced_closing_delimiter(self):
        """Splicing must not make raw literal payload escape into comment syntax."""
        source = 'const char *s = R"tag(\n)' + '\\' + '\n' + 'tag"\n// still raw data\n)tag";\n// comment\n'
        self.assertEqual(comment_only_lines('File.cpp', source), {6})

    def test_objc_block_parameter_continuation_remains_declaration_owned(self):
        """Selector continuation proof consumes nested block/function-pointer type parentheses."""
        source = '@interface Model\n- (void)run:(int)value\n completion:(void (^)(int))completion;\n@end\n'
        self.assertEqual(set(non_executable_lines('File.m', source)), {1, 2, 3, 4})

    def test_review_cases_preserve_decisions_across_all_three_presentation_paths(self):
        """Real fixed Git plus strict snapshots exercise runtime controls and metadata through every path."""
        import hashlib
        from tests.analysis.test_mr_presentation import MRPresentationTests
        from ioscoverage.analysis.mr_presentation import counts, exclude_snapshot_non_executable
        fixture = MRPresentationTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        cases = (
            ('mixed Swift', 'File.swift', 'func run() {\n var x: Int; sideEffect()\n typealias ID = Int; sideEffect()\n}\n', (2, 3), (1, 4)),
            ('mixed C', 'File.c', 'void run(void) {\n typedef int ID; sideEffect();\n}\n', (2,), (1, 3)),
            ('mixed C++', 'File.cpp', 'void run() {\n using ID = int; sideEffect();\n}\n', (2,), (1, 3)),
            ('objects', 'File.cpp', 'struct Widget;\nvoid run() {\n struct Widget object;\n struct Widget another{};\n struct Widget third(1);\n}\n', (3, 4, 5), (1, 2, 6)),
            ('macro comment', 'File.c', '#define FLAG ' + '\\' + '\n// end\nsideEffect();\n', (3,), (1, 2)),
            ('macro blank', 'File.c', '#define FLAG ' + '\\' + '\n\nsideEffect();\n', (3,), (1, 2)),
            ('casts', 'File.m', '@implementation Model\n- (void)run {\n -(int)sideEffect();\n +(int)sideEffect();\n}\n@end\n', (3, 4), (1, 2, 5, 6)),
            ('VLA', 'File.c', 'void run(int values[size()]) {\n use(values);\n}\n', (1, 2), (3,)),
            ('initializer closure', 'File.swift', 'let value: Int = {\n get() {\n  sideEffect()\n }\n return 1\n}()\n', (1, 2, 3, 5, 6), (4,)),
            ('splice', 'File.c', 'int run(void) {\n/* comment *' + '\\' + '\n/ return sideEffect();\n}\n', (3,), (1, 2, 4)),
            ('digit separator', 'File.cpp', "int n = 1'000;\n// documentation\nint m = 2;\n", (1, 3), (2,)),
            ('selector', 'File.m', '@interface Model\n- (void)run:(int)value\n otherValue:(int)other;\n@end\n', (), (1, 2, 3, 4)),
            ('associated values', 'File.swift', 'enum Item {\n case value(\n  id: Int,\n  name: String\n )\n}\n', (), (1, 2, 3, 4, 5, 6)),
            ('return type', 'File.swift', 'func value()\n -> Int\n{\n return compute()\n}\n', (4,), (1, 2, 3, 5)),
            ('raw values', 'File.swift', 'enum Bits: Int {\n case first = 0x01\n case second = 1_000\n case third = 3, fourth = 0b100\n}\n', (), (1, 2, 3, 4, 5)),
            ('regex operator', 'File.swift', 'func run() {\n let value = 1 + /a\\/*/\n sideEffect()\n}\n', (2, 3), (1, 4)),
            ('regex try', 'File.swift', 'func run() {\n let value = try /a\\/*/\n sideEffect()\n}\n', (2, 3), (1, 4)),
            ('extended regex', 'File.swift', 'let pattern = #/\\[/#\n// actual documentation\nsideEffect()\n', (1, 3), (2,)),
            ('multiline VLA', 'File.c', 'void run(\n int values[\n  size\n  ()\n ]\n) {\n use(values);\n}\n', (3, 4, 7), (1, 6, 8)),
            ('generic return', 'File.swift', 'func run()\n -> Result<\n  Int,\n  Error\n >\n{\n return make()\n}\n', (7,), (1, 2, 3, 4, 5, 6, 8)),
            ('property brace', 'File.swift', 'var value: Int\n{\n get {\n  return make()\n }\n}\n', (4,), (1, 2, 3, 5, 6)),
            ('next-line initializer', 'File.swift', 'let value: Int\n = {\n get() {\n  sideEffect()\n }\n return 1\n}()\n', (2, 3, 4, 6, 7), (1, 5)),
            ('multiline default call', 'File.swift', 'func run(\n value: Int =\n  make\n  ()\n) {\n sideEffect()\n}\n', (2, 3, 4, 6), (1, 5, 7)),
            ('implicit getter call', 'File.swift', 'var computed: Int {\n let argument = 1\n set(argument) {\n  sideEffect()\n }\n return 1\n}\n', (2,3,4,6), (1,5,7)),
            ('Unicode operator regex', 'File.swift', 'let value = 1 ≈ /a\\/*/\nsideEffect()\n', (1,2), ()),
            ('dot operator regex', 'File.swift', 'let value = 1 .+. /a\\/*/\nsideEffect()\n', (1,2), ()),
            ('macro bracket', 'File.m', '#define OPEN [\n@interface Model\n- (void)run;\n@end\n', (), (1,2,3,4)),
            ('explicit accessors', 'File.swift', 'var computed: Int {\n get { return 1 }\n set(argument) {\n  sideEffect()\n }\n}\n', (2,4), (1,3,5,6)),
        )
        for name, path, source, runtime, metadata in cases:
            sha = fixture.commit({path: source})
            for evidence, mode in (([], 'source-only'), (fixture.evidence, 'mapped')):
                with self.subTest(case=name, mode=mode):
                    states, result = fixture.project(sha, evidence)
                    for number in runtime:
                        self.assertEqual(states[path][number], 'uncovered')
                    for number in metadata:
                        self.assertEqual(states[path][number], 'not_measurable')
                    for file in result['files']:
                        self.assertEqual(file, result['details'][file['file_id']]['file'])
                    summary = result['summary']
                    self.assertEqual(summary['changed_new_side'], sum(summary[k] for k in ('covered', 'uncovered', 'not_measurable', 'unknown')))
                    if not evidence:
                        self.assertEqual(summary['measurable'], 0)
                        self.assertIsNone(summary['changed_line_coverage_percent'])
            for state in ('covered', 'uncovered'):
                with self.subTest(case=name, mode='strict', original=state):
                    lines = [{'new_line_number': n, 'state': state, 'coverage_source': 'current_build',
                              'regions': [{'covered': state == 'covered'}]}
                             for n in range(1, len(source.splitlines()) + 1)]
                    file = {'file_id': hashlib.sha256(path.encode()).hexdigest(), 'repo_relative_path': path,
                            **counts(lines, coverage_available=True)}
                    snapshot = {'files': [file], 'summary': counts(lines, coverage_available=True),
                                'details': {file['file_id']: {'file': file, 'lines': lines}}}
                    result = exclude_snapshot_non_executable(snapshot, lambda requested: source)
                    actual = {line['new_line_number']: line['state'] for line in result['details'][file['file_id']]['lines']}
                    for number in runtime:
                        self.assertEqual(actual[number], state)
                    for number in metadata:
                        self.assertEqual(actual[number], 'not_measurable')
                    self.assertTrue(all(line['state'] == state for line in snapshot['details'][file['file_id']]['lines']))
                    self.assertEqual(result['files'][0], result['details'][file['file_id']]['file'])
                    self.assertEqual(result['summary']['measurable'], result['summary']['covered'] + result['summary']['uncovered'])

    def test_spliced_declaration_fragments_do_not_hide_joined_runtime_expressions(self):
        """A call or statement joined by C phase two is not proven metadata by physical fragments."""
        source = 'void run(int values[size' + '\\' + '\n' + '()]) {\n use(values);\n}\n'
        excluded = non_executable_lines('File.c', source)
        self.assertNotIn(1, excluded)
        self.assertNotIn(2, excluded)
        mixed = 'void run(void) {\ntypedef int ID;' + '\\' + '\nsideEffect();\n}\n'
        self.assertNotIn(3, non_executable_lines('File.c', mixed))

    def test_regex_after_operators_and_try_preserves_following_runtime(self):
        """Complete operators and expression prefixes cannot expose regex payload as comments."""
        for prefix in ('1 + ', '1 &+ ', '1 <+> ', '1 /* left */+/* right */ ', '^^', 'try ', 'try? ', 'try! ', 'await '):
            source = 'func run() {\n let value = ' + prefix + r'/a\/*/' + '\n sideEffect()\n}\n'
            with self.subTest(prefix=prefix):
                self.assertNotIn(3, non_executable_lines('File.swift', source))
                self.assertEqual(comment_only_lines('File.swift', source), set())
        division = 'let x = a / b + c / d\n// actual comment\nsideEffect()\n'
        self.assertEqual(comment_only_lines('File.swift', division), {2})
        operators = 'let x = a /^/ b\n// actual comment\nlet y = a! / b\nsideEffect()\n'
        self.assertEqual(comment_only_lines('File.swift', operators), {2})

    def test_extended_regex_uses_regex_escaping_and_restores_comment_context(self):
        """Extended regex backslashes escape brackets independently of delimiter hash count."""
        for pattern in (r'#/\[/#', r'##/\[/##', r'#/\]/#', r'#/\\[/]/#'):
            source = 'let pattern = ' + pattern + '\n// actual documentation\nsideEffect()\n'
            with self.subTest(pattern=pattern):
                self.assertEqual(comment_only_lines('File.swift', source), {2})
                excluded = non_executable_lines('File.swift', source)
                self.assertIn(2, excluded)
                self.assertNotIn(3, excluded)

    def test_multiline_vla_bound_preserves_all_runtime_fragments(self):
        """A function parameter's multiline array expression is not a sequence of type fragments."""
        source = 'void run(\n int values[\n  size\n  ()\n ]\n) {\n use(values);\n}\n'
        excluded = non_executable_lines('File.c', source)
        self.assertNotIn(3, excluded)
        self.assertNotIn(4, excluded)
        self.assertNotIn(7, excluded)
        with_next_parameter = source.replace(' ]\n', ' ],\n int other\n')
        self.assertIn(6, non_executable_lines('File.c', with_next_parameter))
        constant = 'void run(\n int values[\n  10\n ]\n) {\n use(values);\n}\n'
        # The runtime guard must not affect independently proven type rows.
        self.assertIn(2, non_executable_lines('File.c', constant))

    def test_generic_return_and_next_line_accessor_keep_declaration_ownership(self):
        """Return type delimiters and a property's next-line brace belong to their declaration."""
        function = 'func run()\n -> Result<\n  Int,\n  Error\n >\n{\n return make()\n}\n'
        self.assertEqual(set(non_executable_lines('File.swift', function)), {1, 2, 3, 4, 5, 6, 8})
        for header in ('func run() -> Result<', 'func run(\n value: Int\n) -> Result<'):
            generic = header + '\n Int,\n Error\n >\n{\n return make()\n}\n'
            rows = len(generic.splitlines())
            self.assertEqual(set(non_executable_lines('File.swift', generic)), set(range(1, rows + 1)) - {rows - 1})
        nested = 'func run() -> Result<(Int) -> Int,\n Error\n>\n{\n return make()\n}\n'
        self.assertEqual(set(non_executable_lines('File.swift', nested)), {1, 2, 3, 4, 6})
        prop = 'var value: Int\n{\n get {\n  return make()\n }\n}\n'
        self.assertEqual(set(non_executable_lines('File.swift', prop)), {1, 2, 3, 5, 6})
        initializer = 'let value: Int\n = {\n get() {\n  sideEffect()\n }\n return 1\n}()\n'
        excluded = non_executable_lines('File.swift', initializer)
        self.assertTrue(all(row not in excluded for row in (2, 3, 4, 6, 7)))


    def test_implicit_getter_body_cannot_reenter_accessor_declarations(self):
        """The first body statement permanently chooses implicit-getter execution ownership."""
        for header in ('var computed: Int {', 'var computed: Int\n{', 'subscript(index: Int) -> Int {'):
            for first in ('let argument = 1', 'var argument: Int', 'if flag {\n work()\n}'):
                source = header + '\n ' + first + '\n set(argument) {\n  sideEffect()\n }\n return 1\n}\n'
                lines=source.splitlines()
                with self.subTest(header=header, first=first):
                    excluded=non_executable_lines('File.swift', source)
                    for number,text in enumerate(lines,1):
                        if text.strip().startswith(('set(argument)', 'sideEffect()', 'return 1')):
                            self.assertNotIn(number,excluded)
        explicit = 'var value: Int {\n get {\n  set(argument) {\n   sideEffect()\n  }\n  return 1\n }\n set(newValue) {\n  set(newValue) {\n   sideEffect()\n  }\n }\n}\n'
        excluded=non_executable_lines('File.swift', explicit)
        self.assertIn(2,excluded)
        self.assertIn(8,excluded)
        self.assertTrue(all(row not in excluded for row in (3,4,6,9,10)))
        inline = 'var value: Int {\n get { return 1 }\n set(newValue) {\n  sideEffect()\n }\n}\n'
        excluded=non_executable_lines('File.swift', inline)
        self.assertNotIn(2,excluded)
        self.assertIn(3,excluded)
        split = 'var value: Int {\n get\n {\n  return 1\n }\n set(newValue)\n {\n  sideEffect()\n }\n}\n'
        self.assertEqual(set(non_executable_lines('File.swift',split)), {1,2,3,5,6,7,9,10})
        mutation = 'struct Model {\n var value: Int {\n  mutating get { return 1 }\n  nonmutating set(newValue) {\n   sideEffect()\n  }\n }\n}\n'
        excluded=non_executable_lines('File.swift',mutation)
        self.assertIn(4,excluded)
        self.assertNotIn(3,excluded)
        self.assertNotIn(5,excluded)

    def test_swift_operator_grammar_includes_unicode_heads_tails_and_dot_operators(self):
        """Documented operator ranges, not a list of known glyphs, protect regex payloads."""
        heads = ('\u00a1\u00a7\u00a9\u00ab\u00ac\u00ae\u00b0\u00b1\u00b6\u00bb\u00bf\u00d7\u00f7'
                 '\u2016\u2017\u2020\u2027\u2030\u203e\u2041\u2053\u2055\u205e'
                 '\u2190\u23ff\u2500\u2775\u2794\u2bff\u2e00\u2e7f\u3001\u3003\u3008\u3020\u3030')
        tails = ('\u0300\u036f\u1dc0\u1dff\u20d0\u20ff\ufe00\ufe0f\ufe20\ufe2f\U000e0100\U000e01ef')
        operators = list(heads) + ['+' + tail for tail in tails] + ['.≈.', '.+.', '...']
        for operator in operators:
            source = 'let value = 1 ' + operator + r' /a\/*/' + '\nsideEffect()\n// actual comment\n'
            with self.subTest(operator=repr(operator)):
                self.assertEqual(comment_only_lines('File.swift', source), {3})
                self.assertNotIn(2,non_executable_lines('File.swift',source))
        negative = 'let x = object.member / divisor\nlet y = 1 /^/ 2\n// actual comment\nsideEffect()\n'
        self.assertEqual(comment_only_lines('File.swift',negative),{3})

    def test_preprocessor_replacement_brackets_cannot_own_array_state(self):
        """Complete directives, including physical continuations, are outside C syntax ownership."""
        for directive in ('#define OPEN [', '#define CLOSE ]', '#define OPEN \\\n [',
                          '#define OPEN \\\n // replacement comment', '#define OPEN \\\n'):
            source=directive+'\n@interface Model\n- (void)run;\n@end\n'
            rows=len(source.splitlines())
            with self.subTest(directive=directive):
                self.assertTrue(set(range(rows-2,rows+1)) <= set(non_executable_lines('File.m',source)))
        vla='void run(\n int values[\n#define TOKEN [\n  size\n  ()\n ]\n) {\n use(values);\n}\n'
        excluded=non_executable_lines('File.c',vla)
        self.assertIn(3,excluded)
        self.assertTrue(all(row not in excluded for row in (4,5,8)))
