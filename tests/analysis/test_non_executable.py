"""Source syntax and resource exclusions must never hide runtime expressions."""

import unittest
from ioscoverage.analysis.mapping.file_classification import is_non_instrumented_file
from ioscoverage.analysis.mapping.source_non_executable import non_executable_lines


class NonExecutableTests(unittest.TestCase):
    """Asserts independent syntax examples rather than reproducing classifier patterns."""

    def test_configuration_and_resource_data_are_excluded(self):
        """New configuration, interface, localization and media data need no Build."""
        for path in ('Info.plist', 'config.JSON', 'Debug.xcconfig', 'App.entitlements', 'PrivacyInfo.xcprivacy',
                     'Assets.xcassets/Icon.imageset/Contents.json', 'Assets.xcassets/Icon.imageset/icon.png',
                     'Main.storyboard', 'Panel.xib', 'Localizable.xcstrings', 'Localizable.stringsdict',
                     'Model.xml', 'Data.csv', 'theme.svg', 'Document.pdf', 'Font.otf', 'Music.mp3', 'Movie.mov'):
            with self.subTest(path=path):
                self.assertTrue(is_non_instrumented_file(path))
        for path in ('Resources/Script.js', 'Resources/Feature.swift', 'Bundle.bundle/Kernel.metal',
                     'Assets.xcassets/Tool.cpp', 'Other.custom', 'config.json.swift'):
            with self.subTest(path=path):
                self.assertFalse(is_non_instrumented_file(path))

    def test_swift_declarations_and_structural_lines(self):
        """Imports, type/property declarations and multiline signatures are syntax-only."""
        source = '''import Foundation

@MainActor
struct Model {
    typealias ID = String
    var id: String
    var computed: Int {
        get {
            return load()
        }
    }
    enum Mode {
        case idle
        case named(String)
        case deferred(Int = nextValue())
    }
    func run(
        value: Int,
        label: String
    ) -> Void {
        let initialized = make()
        let literal = "hello"
        use(initialized)
    }
}
'''
        expected = set(range(1, 26)) - {9, 15, 21, 22, 23}
        self.assertEqual(set(non_executable_lines('Model.swift', source)), expected)

    def test_runtime_initializers_defaults_and_inline_bodies_remain_executable(self):
        """A declaration is not sufficient when its own source line can evaluate code."""
        source = '''func run(value: Int = load()) {
var computed: Int { read() }
func inline() { use() }
var stored = make()
let closure = { run() }
switch mode {
case .idle:
    use()
}
@State(initialValue: make())
'''
        self.assertEqual(set(non_executable_lines('Model.swift', source)), {9})

    def test_literal_contents_and_blank_payload_are_not_syntax(self):
        """Multiline payload resembling imports, declarations or braces stays executable data."""
        source = '''let payload = #"""
import Foundation
var value: Int
}

"""#
// actual comment

'''
        self.assertEqual(set(non_executable_lines('Model.swift', source)), {7, 8})

    def test_multiline_default_expression_is_not_excluded(self):
        """Default argument evaluation can span more than the '=' line."""
        source = 'func run(\n value: Int =\n  load()\n) {\n use()\n}\n'
        self.assertEqual(set(non_executable_lines('Model.swift', source)), {1, 4, 6})

    def test_protocol_requirements_cannot_hide_a_later_function_call(self):
        """A completed declaration never leaves a pending signature over runtime code."""
        source = 'protocol P {\n func required()\n}\nfunc run() {\n use()\n}\n'
        self.assertEqual(set(non_executable_lines('Model.swift', source)), {1, 2, 3, 4, 6})

    def test_objc_declarations_preprocessor_and_c_prototypes(self):
        """Objective-C metadata and compile-time directives are excluded with exact boundaries."""
        source = '''#import <Foundation/Foundation.h>
#define RUN(x) \\
  do { invoke(x); } while (0)
@interface Model : NSObject
@property(nonatomic) NSInteger count;
- (void)run;
@end
@implementation Model
- (void)run {
  invoke();
}
@end
int function(int value);
int initialized = compute();
'''
        self.assertEqual(set(non_executable_lines('Model.m', source)), set(range(1, 15)) - {10, 14})
        self.assertEqual(non_executable_lines('Model.cpp', 'Widget instance;\ninvoke();\n'), {})

    def test_enum_raw_values_and_switch_cases_have_different_ownership(self):
        """Enum definitions are metadata; switch cases retain branch coverage semantics."""
        source = 'enum Mode: String {\n case one = "one"\n}\nswitch mode {\n case .one:\n  run()\n}\n'
        self.assertEqual(set(non_executable_lines('Model.swift', source)), {1, 2, 3, 7})

    def test_unknown_language_does_not_receive_native_syntax_rules(self):
        """A script resource is not treated as configuration or native declaration syntax."""
        self.assertEqual(non_executable_lines('Script.js', 'import thing;\n{}\n'), {})


    def test_constructor_style_initialization_and_immediate_closure_calls_execute(self):
        """No equals sign is required for an initializer or an immediately invoked closure."""
        cpp = 'Widget object(factory);\nint value(compute());\nint count(0);\nWidget factory();\nint function(int value);\n'
        self.assertEqual(set(non_executable_lines('Model.cpp', cpp)), {4, 5})
        swift = 'let value = {\n return compute()\n}()\nlet another = (\n factory\n)()\n'
        self.assertEqual(non_executable_lines('Model.swift', swift), {})
        self.assertEqual(non_executable_lines('Model.c', 'typedef int Values[compute()];\n'), {})


    def test_multiline_type_definitions_and_inline_compile_time_attributes(self):
        """Type spelling remains metadata even across lines and known attribute prefixes."""
        source = 'typealias Callback = (\n Int,\n String\n) -> Void\n@MainActor public struct Model:\n Equatable,\n Identifiable\n{\n @objc var id: String\n}\nprotocol Required {\n var required: Int { get set }\n}\nrun()\n'
        self.assertEqual(set(non_executable_lines('Model.swift', source)), set(range(1, 14)))
        self.assertTrue(is_non_instrumented_file('Model.xcdatamodeld/Version.xcdatamodel/contents'))
        self.assertTrue(is_non_instrumented_file('Panel.html'))
        self.assertFalse(is_non_instrumented_file('Other/contents'))


    def test_accessor_names_as_calls_are_runtime_expressions(self):
        """get/set/willSet are not accessor declarations when used as ordinary function calls."""
        source = 'func run() {\n get()\n set(value)\n set(value) {\n  use()\n }\n}\n'
        self.assertEqual(set(non_executable_lines('Model.swift', source)), {1, 6, 7})
        self.assertEqual(non_executable_lines('Model.cpp', 'Widget object(Foo);\n'), {})
        alias = 'typealias ID =\n String\nrun()\n'
        self.assertEqual(set(non_executable_lines('Model.swift', alias)), {1, 2})


    def test_runtime_sized_arrays_and_contextual_getter_identifiers_stay_executable(self):
        """Array bounds can run code; 'get' can also be an ordinary expression identifier."""
        self.assertEqual(non_executable_lines('Model.c', 'int values[compute()];\n'), {})
        self.assertEqual(non_executable_lines('Model.swift', 'var value: Int { get }\n'), {})
        self.assertEqual(set(non_executable_lines('Model.swift', 'private(set) var count: Int\n')), {1})
        self.assertEqual(set(non_executable_lines('Model.swift', 'func run<T>() where T == Int {\n use()\n}\n')), {1, 3})
