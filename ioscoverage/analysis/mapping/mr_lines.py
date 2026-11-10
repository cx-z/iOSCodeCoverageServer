"""Snapshot-only MR line identity; independent of formal region inheritance."""

from array import array
from bisect import bisect_right

MAX_ALIGNMENT_CELLS = 2_000_000


def verified_line_mapping(old_source, new_source, git_mapping):
    """Keep Git unchanged pairs only when every optimal alignment agrees.

    Text is only a veto on Git provenance, never a source of positive mapping.
    Large files use complete subsequence embeddings or an edit-bounded band;
    unresolved alignments beyond both budgets authorize no inherited pairs.
    Identical snapshots retain positional identity, including repeated braces.
    """

    old, new = old_source.splitlines(keepends=True), new_source.splitlines(keepends=True)
    if old == new:
        return dict(git_mapping), set()
    n, m = len(old), len(new)
    if (n + 1) * (m + 1) > MAX_ALIGNMENT_CELLS:
        # Rows whose exact bytes occur on only one side cannot belong to any
        # matching path. Removing them preserves ALL optimal pairings and keeps
        # a large novel insertion plus a small edit inside the bounded path.
        common = set(old) & set(new)
        old_rows = [i for i, line in enumerate(old, 1) if line in common]
        new_rows = [j for j, line in enumerate(new, 1) if line in common]
        if len(old_rows) < n or len(new_rows) < m:
            old_index = {number: i for i, number in enumerate(old_rows, 1)}
            new_index = {number: j for j, number in enumerate(new_rows, 1)}
            reduced = {new_index[j]: old_index[i] for j, i in git_mapping.items()
                       if j in new_index and i in old_index}
            valid, ambiguous = verified_line_mapping(
                "".join(old[i-1] for i in old_rows), "".join(new[j-1] for j in new_rows), reduced)
            return ({new_rows[j-1]: old_rows[i-1] for j, i in valid.items()},
                    {new_rows[j-1] for j in ambiguous})
        # Pure insertion/deletion has a linear-time uniqueness proof, even for
        # repeated lines: compare the earliest and latest complete embeddings.
        embedded = _subsequence_pairs(old, new, git_mapping)
        if embedded is not None:
            return embedded
        distance_bound = n + m - 2 * len(git_mapping)
        if (n + 1) * min(m + 1, 2 * distance_bound + 1) <= MAX_ALIGNMENT_CELLS:
            return _banded_pairs(old, new, git_mapping, distance_bound)
        # A text occurring once on each side can still be optional in optimal
        # alignments (for example, reversed unique lines). Without a proof no
        # Git candidate may inherit evidence; common rows remain uncertain.
        return {}, _unresolved_lines(old, new, git_mapping, {})
    prefix = [array('I', [0]) * (m + 1) for _ in range(n + 1)]
    suffix = [array('I', [0]) * (m + 1) for _ in range(n + 1)]
    for i in range(n):
        for j in range(m):
            prefix[i+1][j+1] = prefix[i][j] + 1 if old[i] == new[j] else max(prefix[i][j+1], prefix[i+1][j])
    for i in range(n-1, -1, -1):
        for j in range(m-1, -1, -1):
            suffix[i][j] = suffix[i+1][j+1] + 1 if old[i] == new[j] else max(suffix[i+1][j], suffix[i][j+1])
    length = prefix[n][m]
    valid, ambiguous = {}, set()
    for j in range(m):
        candidates = [i+1 for i in range(n) if old[i] == new[j] and prefix[i][j] + 1 + suffix[i+1][j+1] == length]
        skippable = any(prefix[i][j] + suffix[i][j+1] == length for i in range(n+1))
        pair = git_mapping.get(j+1)
        if pair and candidates == [pair] and not skippable:
            valid[j+1] = pair
        elif candidates:
            ambiguous.add(j+1)
    # A clear insertion copying code whose original identity survived stays new/red.
    retained = set(valid.values())
    old_positions = {}
    for i, line in enumerate(old, 1):
        old_positions.setdefault(line, set()).add(i)
    for j in list(ambiguous):
        if j not in git_mapping and old_positions.get(new[j-1], set()).issubset(retained):
            ambiguous.remove(j)
    return valid, ambiguous


def _embedding(needle, haystack):
    """Finds the earliest complete monotone embedding, or no embedding."""

    positions, cursor = [], 0
    for index, line in enumerate(haystack):
        if cursor < len(needle) and needle[cursor] == line:
            positions.append(index + 1)
            cursor += 1
    return positions if cursor == len(needle) else None


def _unresolved_lines(old, new, git_mapping, valid):
    """Keeps uncertain originals orange but proves clear copied insertions red."""

    retained = set(valid.values())
    positions = {}
    for index, line in enumerate(old, 1):
        positions.setdefault(line, set()).add(index)
    return {j for j, line in enumerate(new, 1) if j not in valid and
            (j in git_mapping or (line in positions and not positions[line].issubset(retained)))}


def _subsequence_pairs(old, new, git_mapping):
    """Proves forced pairs using both extreme embeddings of the complete shorter side."""

    inserting = len(old) <= len(new)
    needle, haystack = (old, new) if inserting else (new, old)
    earliest = _embedding(needle, haystack)
    if earliest is None:
        return None
    reverse = _embedding(needle[::-1], haystack[::-1])
    latest = [len(haystack) + 1 - position for position in reversed(reverse)]
    forced = {}
    for index, (left, right) in enumerate(zip(earliest, latest), 1):
        if left == right:
            j, i = (left, index) if inserting else (index, left)
            if git_mapping.get(j) == i:
                forced[j] = i
    if not inserting:
        return forced, set(range(1, len(new) + 1)) - set(forced)
    # A candidate pair exists exactly between that old row's earliest/latest
    # complete embeddings. Merge intervals by text to avoid quadratic scans of
    # large repeated insertions; text still never supplies a positive mapping.
    intervals = {}
    for line, left, right in zip(old, earliest, latest):
        group = intervals.setdefault(line, [])
        if group and left <= group[-1][1] + 1:
            group[-1] = (group[-1][0], max(right, group[-1][1]))
        else:
            group.append((left, right))
    starts = {line: [left for left, _ in groups] for line, groups in intervals.items()}
    ambiguous = set()
    for j, line in enumerate(new, 1):
        if j in forced or line not in intervals:
            continue
        index = bisect_right(starts[line], j) - 1
        if index >= 0 and j <= intervals[line][index][1]:
            ambiguous.add(j)
    return forced, ambiguous


def _band_value(row, column, infinity):
    """Reads one compact band cell, treating out-of-band paths as unreachable."""

    start, values = row
    index = column - start
    return values[index] if 0 <= index < len(values) else infinity


def _edit_rows(old, new, bound):
    """Stores exact distances in compact arrays with the same bounded cell budget."""

    rows = []
    infinity = bound + 1
    for i in range(len(old) + 1):
        start = max(0, i - bound)
        values = array('I')
        previous = rows[-1] if rows else (0, array('I'))
        for j in range(start, min(len(new), i + bound) + 1):
            if not i:
                value = j
            elif not j:
                value = i
            elif old[i-1] == new[j-1]:
                value = _band_value(previous, j-1, infinity)
            else:
                value = 1 + min(_band_value(previous, j, infinity), values[-1] if values else infinity)
            values.append(min(value, infinity))
        rows.append((start, values))
    return rows


def _banded_pairs(old, new, git_mapping, bound):
    """Vetoes pairs against all optimal edit paths without a whole-file matrix."""

    n, m = len(old), len(new)
    prefix = _edit_rows(old, new, bound)
    reverse = _edit_rows(old[::-1], new[::-1], bound)
    infinity = bound + 1
    distance = _band_value(prefix[n], m, infinity)
    valid, ambiguous = {}, set()
    for j in range(m):
        candidates, skippable = [], False
        for i in range(max(0, j - bound), min(n, j + bound) + 1):
            before = _band_value(prefix[i], j, infinity)
            if before + 1 + _band_value(reverse[n-i], m-j-1, infinity) == distance:
                skippable = True
            if i < n and old[i] == new[j] and before + _band_value(reverse[n-i-1], m-j-1, infinity) == distance:
                candidates.append(i+1)
        pair = git_mapping.get(j+1)
        if pair and candidates == [pair] and not skippable:
            valid[j+1] = pair
        elif candidates:
            ambiguous.add(j+1)
    # An insertion whose possible originals are all retained cannot inherit them.
    ambiguous &= _unresolved_lines(old, new, git_mapping, valid)
    return valid, ambiguous
