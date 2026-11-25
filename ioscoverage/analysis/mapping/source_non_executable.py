"""Build-independent, syntax-proven non-executable lines in native iOS source."""

import re
from pathlib import PurePosixPath

from ioscoverage.analysis.mapping.source_comments import scan_source_lines, supports_comment_classification


MODIFIERS = (r'(?:(?:(?:public|private|fileprivate|internal|package)(?:\(set\))?|'
             r'open|final|static|class|override|required|convenience|mutating|nonmutating|'
             r'nonisolated(?:\(unsafe\))?|isolated|distributed|indirect|weak|'
             r'unowned(?:\((?:safe|unsafe)\))?|lazy|optional|dynamic|borrowing|consuming)\s+)*')
SWIFT_TYPE = re.compile(r'^' + MODIFIERS + r'(class|struct|enum|protocol|extension|actor)\s+(?!func\b|var\b|let\b)([A-Za-z_]\w*)\b')
SWIFT_FUNCTION = re.compile(r'^' + MODIFIERS + r'(?:func\s+\S+|init[!?]?\s*\(|deinit\b|subscript\s*\()')
SWIFT_PROPERTY = re.compile(r'^' + MODIFIERS + r'(?:let|var)\s+[A-Za-z_]\w*\s*(?::|$)')
SWIFT_TYPE_ALIAS = re.compile(r'^' + MODIFIERS + r'(?:typealias|associatedtype)\s+[A-Za-z_]\w*\b')
SWIFT_IMPORT = re.compile(r'^(?:(?:@_exported|@_implementationOnly|@preconcurrency|public|internal|private|package)\s+)*import\s+(?:(?:typealias|struct|class|enum|protocol|let|var|func)\s+)?[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s*;?$')
SWIFT_ATTRIBUTE = re.compile(r'^@(?:available|objc|objcMembers|nonobjc|MainActor|Sendable|unchecked|preconcurrency|discardableResult|inlinable|inline|_transparent|_alwaysEmitIntoClient|_cdecl|globalActor|propertyWrapper|resultBuilder|dynamicCallable|dynamicMemberLookup|backDeployed|IBOutlet|IBAction|IBInspectable|IBDesignable|NSManaged|NSCopying|GKInspectable)\b')
SWIFT_DIRECTIVE = re.compile(r'^#(?:if|elseif|else|endif|warning|error|sourceLocation)\b')
OBJC_DECLARATION = re.compile(r'^@(?:interface|implementation|protocol|class|end|optional|required|property|synthesize|dynamic|compatibility_alias)\b')
C_TYPE = re.compile(r'^(?:typedef\s+)?(?:enum(?:\s+class)?|struct|union|class)\s+\w+\b')
C_PROTOTYPE = re.compile(r'^(?:(?:extern|static|inline|const|volatile|unsigned|signed|long|short)\s+)*(?:void|char|int|float|double|bool|_Bool|id|BOOL|NSInteger|NSUInteger|CGFloat|size_t|[A-Z]\w*(?:\s*\*)?)\s+\**\w+\s*\(')
C_SCALAR_DECLARATION = re.compile(r'^(?:(?:extern|static|const|volatile|unsigned|signed|long|short)\s+)*(?:char|int|float|double|bool|_Bool|id|BOOL|NSInteger|NSUInteger|CGFloat|size_t)\s+\**\w+(?:\s*\[[^]]*\])?\s*;$')
DELIMITERS = re.compile(r'^(?:[\]})]+\{?[,;]?|[({\[]|[;,])$')

def _single_statement(text):
    """Accepts at most one trailing semicolon, never a declaration followed by another statement."""

    return ';' not in text.rstrip(';').rstrip() and not text.rstrip().endswith(';;')


def _has_initializer(text):
    """Separates assignment/default evaluation from equality in type constraints."""

    return re.search(r'(?<![=!<>])=(?!=)', text) is not None


def _header_only(text):
    """Proves the complete line is a header with no initializer or inline executable body."""

    header, opening, body = text.partition('{')
    return _single_statement(text) and not _has_initializer(header) and (not opening or not body.strip('} \t;'))


def _without_swift_attributes(text):
    """Removes known compile-time prefixes before examining the remaining source tokens."""

    while True:
        match = SWIFT_ATTRIBUTE.match(text)
        if not match:
            return text
        rest = text[match.end():].lstrip()
        if rest.startswith('('):
            depth, end = 0, None
            for index, character in enumerate(rest):
                depth += (character == '(') - (character == ')')
                if depth == 0:
                    end = index + 1
                    break
            if end is None:
                return text
            rest = rest[end:].lstrip()
        if not rest:
            return text
        text = rest


def _split_cases(text):
    """Splits enum cases only at commas outside associated-value/type parentheses."""

    parts, start, depth = [], 0, 0
    for index, character in enumerate(text):
        depth += (character == '(') - (character == ')')
        if character == ',' and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def _swift_case(text):
    """Recognizes raw literal values without treating associated-value defaults as metadata."""

    if any(character in text for character in '{}') or not _single_statement(text):
        return False
    if not _has_initializer(text):
        return True
    number = r'[+-]?(?:0[xX][\da-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|\d[\d_]*(?:\.[\d_]+)?(?:[eE][+-]?[\d_]+)?)'
    raw_case = re.compile(r'\w+\s*=\s*(?:\x00|' + number + r')')
    return all(raw_case.fullmatch(part) or re.fullmatch(r'\w+(?:\([^=]*\))?', part)
               for part in _split_cases(text[5:].rstrip(';')))


def _swift_accessor_start(text, code_lines, following_index):
    """Proves an explicit accessor head, separately from whether its row also executes code."""

    text = _without_swift_attributes(text)
    head, opening, _ = text.partition('{')
    accessor = r'(?:(?:mutating|nonmutating)\s+)?(?:get(?:\s+(?:async|throws))*|(?:set|willSet|didSet)(?:\(\s*\w+\s*\))?)'
    if not re.fullmatch(accessor, head.strip()):
        return False
    if opening:
        return True
    # A bare identifier such as `get` may be an implicit getter's returned value.
    # Require a following body brace before treating a split head as metadata.
    for index in range(following_index, len(code_lines)):
        if code_lines[index].strip():
            return code_lines[index].strip().startswith('{')
    return False


def _swift_declaration(text, enum_context, accessor_context, protocol_context):
    """Classifies complete Swift declaration rows and identifies their owned continuation."""

    text = _without_swift_attributes(text)
    if SWIFT_IMPORT.fullmatch(text):
        return 'import', None
    if SWIFT_DIRECTIVE.match(text):
        return 'compiler_directive', None
    if not _single_statement(text):
        return None, None
    if SWIFT_ATTRIBUTE.match(text) and (')' not in text or not text.split(')', 1)[-1].strip()):
        return 'declaration', 'attribute' if text.count('(') > text.count(')') else None
    if SWIFT_TYPE_ALIAS.match(text):
        continued = any(text.count(a) > text.count(b) for a, b in (('(', ')'), ('[', ']'), ('<', '>')))
        return 'declaration', 'typealias' if continued or text.endswith('=') else None
    if SWIFT_TYPE.match(text) and _header_only(text):
        return 'declaration', 'type' if '{' not in text else None
    if SWIFT_FUNCTION.match(text) and _header_only(text):
        result_type = text.partition('->')[2].replace('->', '')
        continuation = 'function' if text.count('(') > text.count(')') else (
            'function_result' if '->' in text and any(result_type.count(a) > result_type.count(b) for a, b in (('[', ']'), ('<', '>')))
            else 'function_tail')
        return 'declaration', continuation if '{' not in text and not text.endswith(';') else None
    requirement = protocol_context and re.fullmatch(r'.*\{\s*get(?:\s+(?:async|throws))*\s*(?:set\s*)?\}', text)
    if SWIFT_PROPERTY.match(text) and not _has_initializer(text.partition('{')[0]) and (_header_only(text) or requirement):
        return 'declaration', None
    if accessor_context and _header_only(text):
        return 'declaration', None
    if enum_context and text.startswith('case ') and _swift_case(text):
        return 'declaration', 'enum_case' if text.count('(') > text.count(')') else None
    return None, None


def _constant_array_bounds(text):
    """Requires literal bounds; VLA expressions can evaluate at declaration or function entry."""

    integer = r'(?:0[xX][\da-fA-F\']+|0[bB][01\']+|\d[\d\']*)(?:[uUlL]*)'
    return all(not bound.strip() or re.fullmatch(r'(?:(?:static|const|restrict|volatile)\s+)*' + integer, bound.strip())
               for bound in re.findall(r'\[([^]]*)\]', text))


def _c_directive_rows(physical_lines, code_lines):
    """Owns complete preprocessing directives before any C bracket/declaration scanning."""

    rows, continuation = set(), False
    for number, code in enumerate(code_lines, 1):
        in_directive = continuation or code.lstrip().startswith('#')
        if in_directive:
            rows.add(number)
        # A blank/comment replacement row also terminates a directive without a splice.
        continuation = in_directive and physical_lines[number - 1].endswith('\\')
    return rows


def _runtime_array_rows(code_lines, directive_rows):
    """Keeps complete multiline C array-bound expressions out of declaration proof."""

    rows, depth, start, parts = set(), 0, 0, []
    for number, code in enumerate(code_lines, 1):
        if number in directive_rows:
            continue
        for character in code:
            if character == '[':
                if depth == 0:
                    start, parts = number, []
                depth += 1
            if depth:
                parts.append(character)
            if character == ']' and depth:
                depth -= 1
                if depth == 0 and not _constant_array_bounds(''.join(parts)):
                    rows.update(range(start, number + 1))
        if depth:
            parts.append('\n')
    if depth:
        # An incomplete bound is not proof of compile-time-only syntax.
        rows.update(range(start, len(code_lines) + 1))
    return rows


def _c_type_header(text):
    """Proves a forward/type definition rather than an elaborated-type object definition."""

    match = C_TYPE.match(text)
    if not match or not _header_only(text):
        return False
    tail = text[match.end():].strip()
    return tail in ('', ';') or tail.startswith('{') or tail.startswith(':')


def _c_prototype(text, cpp):
    """Keeps VLA evaluation and ambiguous C++ direct initialization eligible for coverage."""

    match = C_PROTOTYPE.match(text)
    if not match or not _header_only(text) or not _constant_array_bounds(text.partition('{')[0]):
        return False
    if not cpp or '{' in text:
        return True
    parameters, separator, suffix = text[match.end():].rpartition(')')
    if not separator or suffix.strip() not in ('', ';'):
        return False
    if not parameters.strip():
        return True
    parameter_type = r'(?:(?:const|volatile|unsigned|signed|long|short|struct|enum|class)\s+)*(?:void|char|int|float|double|bool|_Bool|id|BOOL|NSInteger|NSUInteger|CGFloat|size_t)(?:\s*[*&]+\s*|\s+)?(?:[A-Za-z_]\w*)?(?:\[\])?'
    return all(re.fullmatch(parameter_type, item.strip()) for item in parameters.split(','))


def _parenthesized_end(text, start):
    """Consumes nested parentheses in an Objective-C return/parameter type."""

    if start >= len(text) or text[start] != '(':
        return None
    depth = 0
    for index in range(start, len(text)):
        depth += (text[index] == '(') - (text[index] == ')')
        if depth == 0:
            return index + 1
    return None


def _objc_signature(text, first):
    """Consumes a complete selector in declaration context, including block/function-pointer types."""

    if not _header_only(text) or not _constant_array_bounds(text):
        return False
    header = text.partition('{')[0].strip().rstrip(';').strip()
    if first:
        start = re.match(r'^[-+]\s*', header)
        if not start:
            return False
        end = _parenthesized_end(header, start.end())
        if end is None:
            return False
        header = header[end:].lstrip()
    components = 0
    while header:
        selector = re.match(r'\w+', header)
        if not selector:
            return False
        header = header[selector.end():].lstrip()
        if not header:
            return first and components == 0
        if not header.startswith(':'):
            return False
        header = header[1:].lstrip()
        end = _parenthesized_end(header, 0)
        if end is None:
            return False
        parameter = re.match(r'\s*\w+', header[end:])
        if not parameter:
            return False
        header = header[end + parameter.end():].lstrip()
        components += 1
    return components > 0


def _type_piece(text):
    """Admits declaration continuation spelling while rejecting calls, expressions and extra statements."""

    if not _header_only(text) or '\x00' in text:
        return False
    if re.search(r'\b(?:return|throw|let|var|break|continue|case)\b', text):
        return False
    if re.search(r'\b\w+\s*\(', text):
        return False
    return bool(re.fullmatch(r'[\w\s<>\[\]():.,?!&@\-{};]+', text))


def non_executable_lines(path, source):
    """Excludes only complete proven non-executable rows with request-local declaration ownership."""

    if not supports_comment_classification(path):
        return {}
    physical_lines = source.splitlines()
    comments, code_lines = scan_source_lines(path, source)
    suffix = PurePosixPath(path).suffix
    swift = suffix == '.swift'
    cpp = suffix in ('.mm', '.cc', '.cpp', '.cxx', '.h', '.hh', '.hpp', '.hxx')
    objc_language = suffix in ('.m', '.mm', '.h', '.hh', '.hpp')
    result = {line: 'comment_only_source_line' for line in comments}
    # Lexical proof survives splicing, but a physical declaration fragment alone
    # cannot prove a joined C expression non-executable. Keep those code rows eligible.
    spliced_rows = set()
    if not swift:
        for index, physical in enumerate(physical_lines[:-1], 1):
            if physical.endswith('\\'):
                spliced_rows.update((index, index + 1))
    directive_rows = set() if swift else _c_directive_rows(physical_lines, code_lines)
    runtime_array_rows = set() if swift else _runtime_array_rows(code_lines, directive_rows)
    scopes = []
    pending, balance, pending_scope, objc_container = None, 0, None, False
    for number, code in enumerate(code_lines, 1):
        text = code.strip()
        reason, next_pending = None, None
        if number in comments:
            continue
        if not text:
            result[number] = 'blank_source_line'
            continue
        if number in directive_rows:
            result[number] = 'compiler_directive'
            continue
        accessor_start = False
        if swift and scopes and scopes[-1] in ('property', 'accessors'):
            accessor_start = _swift_accessor_start(text, code_lines, number)
            if scopes[-1] == 'property':
                if accessor_start:
                    scopes[-1] = 'accessors'
                elif not SWIFT_DIRECTIVE.match(text) and not SWIFT_ATTRIBUTE.match(text):
                    # Even a pure local declaration starts the implicit getter body.
                    # Its outer property can never regain accessor-declaration ownership.
                    scopes[-1] = 'implicit_getter'
        mixed = not _single_statement(text) or number in spliced_rows
        runtime_bound = number in runtime_array_rows
        enum_context = bool(scopes and scopes[-1] == 'enum')
        if mixed:
            pending = None
        elif runtime_bound:
            # Keep the signature owner but never prove an array expression from fragments.
            pass
        elif DELIMITERS.fullmatch(re.sub(r'\s+', '', text)):
            reason = 'syntax_only_source_line'
        elif swift:
            reason, next_pending = _swift_declaration(
                text, enum_context, accessor_start,
                bool(scopes and scopes[-1] == 'protocol'))
        else:
            if (OBJC_DECLARATION.match(text) and _header_only(text)) or (text.startswith('using ') and text.endswith(';')):
                reason = 'declaration'
            elif text.startswith('typedef ') and text.endswith(';') and _constant_array_bounds(text):
                reason = 'declaration'
            elif _c_type_header(text):
                reason = 'declaration'
            elif objc_language and objc_container and not scopes and _objc_signature(text, True):
                reason = 'declaration'
                next_pending = 'objc_method' if '{' not in text and not text.endswith(';') else None
            elif _c_prototype(text, cpp):
                reason = 'declaration'
                next_pending = 'function' if text.count('(') > text.count(')') and '{' not in text else None
            elif C_SCALAR_DECLARATION.fullmatch(text) and _constant_array_bounds(text):
                reason = 'declaration'
            elif enum_context and re.fullmatch(r'\w+(?:\s*=\s*[^{};]+)?\s*,?', text):
                reason = 'declaration'
        if pending and not reason and not mixed and not runtime_bound:
            if pending == 'objc_method':
                if objc_language and objc_container and not scopes and _objc_signature(text, False):
                    reason = 'declaration'
                else:
                    pending = None
            elif pending == 'function_tail':
                if re.match(r'^(?:->|async\b|throws\b|rethrows\b|where\b)', text) and _type_piece(text):
                    reason = 'declaration'
                    if text.startswith('->'):
                        next_pending = 'function_result'
                else:
                    pending = None
            elif pending == 'attribute' or _type_piece(text):
                reason = 'declaration'
            if '{' in text or '}' in text or _has_initializer(text) or text.endswith(';'):
                pending = None
        if next_pending:
            pending, balance = next_pending, 0
        elif reason and pending and balance <= 0 and (SWIFT_TYPE_ALIAS.match(text) or SWIFT_PROPERTY.match(text)):
            pending = None
        if pending:
            bracket_text = text.replace('->', '')
            pairs = (('(', ')'), ('[', ']'), ('<', '>')) if pending in ('typealias', 'type', 'function_result') else (('(', ')'),)
            balance += sum(bracket_text.count(a) - bracket_text.count(b) for a, b in pairs)
            complete = balance <= 0 and pending not in ('type', 'function_tail', 'objc_method') and not (pending == 'typealias' and text.endswith('='))
            if pending == 'function' and complete and swift and '->' in text:
                result_type = text.partition('->')[2].replace('->', '')
                balance = sum(result_type.count(a) - result_type.count(b) for a, b in (('(', ')'), ('[', ']'), ('<', '>')))
                pending, complete = 'function_result' if balance > 0 else 'function_tail', False
            elif pending in ('function', 'function_result') and complete:
                pending, complete = 'function_tail' if swift else None, False
            if complete or '{' in text or '}' in text or text.endswith(';'):
                pending = None
        if reason:
            result[number] = reason if reason in ('compiler_directive', 'syntax_only_source_line') else 'non_executable_' + reason
        if reason == 'compiler_directive':
            continue
        normalized = _without_swift_attributes(text) if swift else text
        nominal = SWIFT_TYPE.match(normalized) if swift else C_TYPE.match(text) if _c_type_header(text) else None
        kind = 'enum' if nominal and re.search(r'\benum\b', text) else (
            'protocol' if nominal and re.search(r'\bprotocol\b', text) else 'other')
        property_header = swift and _header_only(normalized) and (SWIFT_PROPERTY.match(normalized) or re.match(r'^subscript\b', normalized))
        if property_header:
            kind = 'property'
        if objc_language and not scopes:
            if re.match(r'^@(?:interface|implementation|protocol)\s+\w+', text):
                objc_container = True
            elif re.match(r'^@end\b', text):
                objc_container, pending = False, None
        if (nominal or property_header) and '{' not in text:
            pending_scope = kind
        elif '{' in text and pending_scope:
            if pending_scope != 'property' or re.fullmatch(r'\{\s*\}?', text):
                kind = pending_scope
            pending_scope = None
        elif not nominal and pending != 'type':
            pending_scope = None
        for character in text:
            if character == '{':
                scopes.append(kind)
                kind = 'other'
            elif character == '}' and scopes:
                scopes.pop()
    return result
