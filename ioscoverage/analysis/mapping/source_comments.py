"""Physical-line-preserving lexical context for native-source coverage classification."""

import re
from pathlib import PurePosixPath


SOURCE_SUFFIXES = frozenset({'.swift', '.m', '.mm', '.h', '.c', '.cc', '.cpp', '.cxx', '.hh', '.hpp', '.hxx'})
CPP_RAW_STRING = re.compile(r'(?:u8|u|U|L)?R"([^ ()\\\t\r\n]{0,16})\(')
SWIFT_LITERAL = re.compile(r'(#+)?("""|"|/)')
IDENTIFIER = re.compile(r'[^\W\d]\w*')
NUMBER = re.compile(r"(?:\d|\.\d)(?:[eEpP][+-]|[A-Za-z_0-9.'])*")
# Swift Reference Manual, Lexical Structure: operator-head / operator-character.
# Dot operators require a second operator character; a lone member-access dot is punctuation.
SWIFT_OPERATOR_HEAD = (r'/=\-+!*%<>&|^~?\u00a1-\u00a7\u00a9\u00ab\u00ac\u00ae'
                       r'\u00b0-\u00b1\u00b6\u00bb\u00bf\u00d7\u00f7\u2016-\u2017'
                       r'\u2020-\u2027\u2030-\u203e\u2041-\u2053\u2055-\u205e'
                       r'\u2190-\u23ff\u2500-\u2775\u2794-\u2bff\u2e00-\u2e7f'
                       r'\u3001-\u3003\u3008-\u3020\u3030')
SWIFT_OPERATOR_TAIL = (SWIFT_OPERATOR_HEAD + r'\u0300-\u036f\u1dc0-\u1dff\u20d0-\u20ff'
                       r'\ufe00-\ufe0f\ufe20-\ufe2f\U000e0100-\U000e01ef')
SWIFT_OPERATOR = re.compile(r'(?:\.[' + SWIFT_OPERATOR_TAIL + r'.]+|[' +
                            SWIFT_OPERATOR_HEAD + r'][' + SWIFT_OPERATOR_TAIL + r']*)')
C_SPLICE = re.compile(r'\\\r?\n')


def supports_comment_classification(path):
    """Limits native syntax rules to the source languages supported by this platform."""

    return PurePosixPath(path).suffix in SOURCE_SUFFIXES


def _splice_c_source(source):
    """Applies C phase-two splicing and records removed physical newline positions."""

    pieces, boundaries, previous, length = [], [], 0, 0
    for match in C_SPLICE.finditer(source):
        piece = source[previous:match.start()]
        pieces.append(piece)
        length += len(piece)
        boundaries.append(length)
        previous = match.end()
    if not boundaries:
        return source, ()
    pieces.append(source[previous:])
    return ''.join(pieces), tuple(boundaries)


def _literal_at(source, offset, swift, regex_allowed):
    """Recognizes literal openers using incremental expression context, never line-prefix scans."""

    if swift:
        match = SWIFT_LITERAL.match(source, offset)
        if match:
            hashes = match[1] or ''
            if match[2] != '/' or hashes:
                return match[2] + hashes, '\\' if match[2] == '/' else '\\' + hashes, match.end(), match[2] != '/', match[2] == '/'
            if regex_allowed and offset + 1 < len(source) and not source[offset + 1].isspace():
                return '/', '\\', offset + 1, False, True
    else:
        match = CPP_RAW_STRING.match(source, offset)
        if match:
            return ')' + match[1] + '"', '', match.end(), False, False
        if source[offset] in ('"', "'"):
            return source[offset], '\\', offset + 1, False, False
    return None


def scan_source_lines(path, source):
    """Returns comment-only physical lines and spelling with opaque literal contents.

    C splices are removed before lexing, then token spans are projected back to
    their original physical lines. All frames, buffers and context belong to
    this scan; callers never receive shared mutable lexical state.
    """

    if not supports_comment_classification(path):
        return frozenset(), ()
    swift = PurePosixPath(path).suffix == '.swift'
    physical_count = len(source.splitlines())
    source, boundaries = (source, ()) if swift else _splice_c_source(source)
    # Code frames carry interpolation depth, expression position and last token end.
    stack = [['code', 0, True, 0]]
    comments, code_lines, spelling = set(), [], []
    has_code = has_comment = False
    boundary_index = 0

    def flush():
        """Finishes one physical row without changing the logical lexical context."""
        nonlocal spelling, has_code, has_comment
        if has_comment and not has_code:
            comments.add(len(code_lines) + 1)
        code_lines.append(''.join(spelling))
        spelling = []
        has_code = has_comment = False

    def record(piece, kind):
        """Projects one token fragment while keeping literal contents opaque."""
        nonlocal has_code, has_comment
        if kind == 'comment':
            has_comment = True
            if not spelling or spelling[-1] != ' ':
                spelling.append(' ')
        elif kind == 'literal':
            has_code = True
            if not spelling or spelling[-1] != '\x00':
                spelling.append('\x00')
        elif kind == 'code' and piece:
            has_code = True
            spelling.append(piece)
        elif piece and piece != '\n':
            spelling.append(piece)
        if piece == '\n':
            flush()

    def emit(start, end, kind):
        """Splits a logical token at splice boundaries, preserving every physical row."""
        nonlocal boundary_index
        while boundary_index < len(boundaries) and boundaries[boundary_index] < end:
            boundary = boundaries[boundary_index]
            record(source[start:boundary], kind)
            flush()
            start = boundary
            boundary_index += 1
        record(source[start:end], kind)

    offset = 0
    while offset < len(source):
        frame = stack[-1]
        kind, character = frame[0], source[offset]
        if character == '\n':
            emit(offset, offset + 1, 'comment' if kind in ('line', 'block') else 'literal' if kind == 'literal' else 'space')
            if kind == 'line':
                stack.pop()
            elif kind == 'literal':
                frame[4] = False
            elif kind == 'code':
                frame[2] = True
            offset += 1
        elif kind == 'line':
            emit(offset, offset + 1, 'comment')
            offset += 1
        elif kind == 'block':
            end = offset + 1
            if swift and source.startswith('/*', offset):
                frame[1] += 1
                end += 1
            elif source.startswith('*/', offset):
                frame[1] -= 1
                end += 1
                if frame[1] == 0:
                    stack.pop()
            emit(offset, end, 'comment')
            offset = end
        elif kind == 'literal':
            end = offset + 1
            if frame[4]:
                frame[4] = False
            elif not (frame[5] and frame[6]) and source.startswith(frame[1], offset):
                candidate_end = offset + len(frame[1])
                crossed = boundary_index < len(boundaries) and offset < boundaries[boundary_index] < candidate_end
                # C++ raw payload must not acquire a closing delimiter from phase-two splicing.
                if frame[2] or not crossed:
                    end = candidate_end
                    stack.pop()
                    if stack[-1][0] == 'code':
                        stack[-1][2] = False
                        stack[-1][3] = end
            elif frame[3] and source.startswith(frame[2] + '(', offset):
                end = offset + len(frame[2]) + 1
                stack.append(['code', 1, True, end])
            elif frame[2] and source.startswith(frame[2], offset):
                end = offset + len(frame[2])
                frame[4] = True
            elif frame[5] and character in '[]':
                frame[6] = character == '['
            emit(offset, end, 'literal')
            offset = end
        elif source.startswith('//', offset) or source.startswith('/*', offset):
            line = source.startswith('//', offset)
            stack.append(['line'] if line else ['block', 1])
            emit(offset, offset + 2, 'comment')
            offset += 2
        elif character.isspace():
            emit(offset, offset + 1, 'space')
            offset += 1
        else:
            # Numeric tokens precede character literals, including C++ digit separators.
            number = NUMBER.match(source, offset)
            operator = SWIFT_OPERATOR.match(source, offset) if swift and not number else None
            identifier = None if number or operator else IDENTIFIER.match(source, offset)
            literal = None if number or identifier else _literal_at(source, offset, swift, frame[2])
            # C++ raw literal prefixes are identifiers until their full opener is proven.
            if not swift and identifier and CPP_RAW_STRING.match(source, offset):
                literal = _literal_at(source, offset, swift, frame[2])
                identifier = None
            if literal:
                end_marker, escape, end, interpolate, regex = literal
                stack.append(['literal', end_marker, escape, interpolate, False, regex, False])
                emit(offset, end, 'literal')
            elif number or identifier:
                token = number or identifier
                end = token.end()
                emit(offset, end, 'code')
                frame[2] = bool(identifier and token[0] in ('return', 'throw', 'case', 'try', 'await'))
            elif operator:
                # Consume a complete operator. Only expression-position prefixes may
                # split at a slash; infix operators such as '+/' remain one token.
                end = operator.end()
                for index in range(offset, end):
                    if source.startswith('//', index) or source.startswith('/*', index) or (frame[2] and source[index] == '/'):
                        end = index
                        break
                if end == offset:
                    end += 1
                emit(offset, end, 'code')
                left_space = offset == 0 or offset > frame[3] or source[offset - 1] in '([{,:;'
                right_space = (end == len(source) or source[end].isspace() or source[end] in ')]},:;'
                               or source.startswith('//', end) or source.startswith('/*', end))
                # Prefix operators preserve expression position; a postfix operator
                # after an operand does not turn a following division into a regex.
                frame[2] = frame[2] or left_space == right_space
            else:
                end = offset + 1
                emit(offset, end, 'code')
                frame[2] = character in '=(:,[!&|?{};'
                if frame[1]:
                    frame[1] += (character == '(') - (character == ')')
                    if frame[1] == 0:
                        stack.pop()
            frame[3] = end
            offset = end
    # EOF may itself follow one or more removed physical newlines.
    while boundary_index < len(boundaries):
        record('', 'comment' if stack[-1][0] in ('block', 'line') else 'literal' if stack[-1][0] == 'literal' else 'space')
        flush()
        boundary_index += 1
    if len(code_lines) < physical_count:
        flush()
    return frozenset(comments), tuple(code_lines)


def comment_only_lines(path, source):
    """Returns comment-only physical line numbers using the shared source lexer."""

    return scan_source_lines(path, source)[0]
