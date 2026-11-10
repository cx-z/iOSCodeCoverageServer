"""Current-MR product cases using real Git objects and independent LLVM exports."""
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from ioscoverage.analysis.mr_presentation import MergeRequestPresentation
from ioscoverage.integrations.git_repository import GitRepository


class MRPresentationTests(unittest.TestCase):
    """Verifies evidence snapshots rather than only adjacent MR commits."""

    def setUp(self):
        """Creates a baseline and covered A with a single thirteen-line region."""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Coverage Test')
        self.git('config', 'user.email', 'test@example.test')
        self.base = self.commit({'File.swift': '// baseline\n'})
        self.original = ''.join('execute_%02d()\n' % i for i in range(1, 14))
        self.a = self.commit({'File.swift': self.original})
        self.cycle, self.builds = str(uuid.uuid4()), {}
        self.evidence = [self.make_evidence(self.a, self.original)]
        self.repository = GitRepository(self.root)
        self.projector = MergeRequestPresentation(self.repository, SimpleNamespace(compiler_repository_names=()),
            SimpleNamespace(get_build=lambda key: self.builds[key]))

    def tearDown(self):
        """Removes the isolated fixture."""
        self.temp.cleanup()

    def git(self, *args):
        """Runs Git in the temporary repository only."""
        return subprocess.run(['git', '-C', str(self.root), *args], check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True).stdout.strip()

    def commit(self, files):
        """Creates a fixed source snapshot."""
        for path, source in files.items():
            (self.root / path).write_text(source)
        self.git('add', '-A')
        self.git('commit', '-qm', 'fixture', '--allow-empty')
        return self.git('rev-parse', 'HEAD')

    def make_evidence(self, sha, source, misses=()):
        """Produces a same-Build export with full and optional unhit subregions."""
        build_id = str(uuid.uuid4())
        build = dict(id=build_id, cycle_id=self.cycle, artifact_state='ready', git_sha=sha,
                     target_sha_at_build=self.base, merge_base_sha=self.base,
                     manifest_sha256='a'*64, coverage_binary='app')
        self.builds[build_id] = build
        lines = source.splitlines()
        regions = [[1, 1, len(lines), len(lines[-1])+1, 1, 0, 0, 0]]
        regions += [[i, 2, i, 6, 0, 0, 0, 0] for i in misses]
        return dict(build_id=build_id, input_set_sha256='b'*64, export_sha256='c'*64,
            inputs=dict(cycle_id=self.cycle, build_id=build_id, build=build),
            payload=dict(type='llvm.coverage.json.export', version='2.0.1', data=[dict(
                files=[dict(filename='File.swift', segments=[])],
                functions=[dict(name='fixture', filenames=['File.swift'], regions=regions)])]))

    def project(self, sha, evidence=None, base=None):
        """Indexes current changed-side lines; summary stays global to the complete MR."""
        result = self.projector.project(self.cycle,
            dict(base_sha=base or self.base, start_sha=base or self.base, head_sha=sha),
            self.evidence if evidence is None else evidence)
        states = {detail['file']['repo_relative_path']:
                  {line['new_line_number']: line['state'] for line in detail['lines']}
                  for detail in result['details'].values()}
        return states, result

    def test_P01_P02_P03_P05_partial_edits_consecutive_commits_restore(self):
        """Only modified lines lose the original region's hit, even after further commits."""
        source = self.original.replace('execute_13()', 'changed_13()')
        sha = self.commit({'File.swift': source})
        self.assertTrue(all(self.project(sha)[0]['File.swift'][i] == 'covered' for i in range(1, 11)))
        self.assertEqual(self.project(sha)[0]['File.swift'][13], 'uncovered')
        source = source.replace('execute_08()', 'changed_08()')
        sha = self.commit({'File.swift': source})
        expected = ['covered', 'uncovered', 'covered', 'uncovered']
        self.assertEqual([self.project(sha)[0]['File.swift'][i] for i in (7, 8, 9, 13)], expected)
        sha = self.commit({'Other.swift': 'other()\n'})
        self.assertEqual([self.project(sha)[0]['File.swift'][i] for i in (7, 8, 9, 13)], expected)
        sha = self.commit({'File.swift': source.replace('changed_08()', 'execute_08()')})
        self.assertEqual(self.project(sha)[0]['File.swift'][8], 'covered')

    def test_P04_P12_P17_moving_lines_and_changed_scope(self):
        """Handles insertion, deletion, rebased scope and display counts without a percentage."""
        sha = self.commit({'File.swift': 'inserted()\n' + self.original})
        states, result = self.project(sha)
        self.assertEqual((states['File.swift'][1], states['File.swift'][9]), ('uncovered', 'covered'))
        self.assertEqual(result['summary']['covered'], 13)
        self.assertIsNone(result['summary']['changed_line_coverage_percent'])
        sha = self.commit({'File.swift': ''.join(self.original.splitlines(keepends=True)[2:])})
        self.assertEqual(self.project(sha)[1]['summary']['changed_new_side'], 11)
        self.assertTrue(all(state == 'covered' for state in self.project(sha)[0]['File.swift'].values()))
        # A newer target baseline changes the MR range, not evidence identity.
        self.assertEqual(self.project(sha, base=self.a)[1]['summary']['changed_new_side'], 0)

    def test_P06_copy_and_duplicate_ambiguity(self):
        """Copied files remain red, indistinguishable repetitions become unknown."""
        sha = self.commit({'Copy.swift': self.original, 'File.swift': self.original + 'execute_08()\n'})
        states, _ = self.project(sha)
        self.assertTrue(all(value == 'uncovered' for value in states['Copy.swift'].values()))
        self.assertNotEqual(states['File.swift'][14], 'covered')
        repeated = self.commit({'File.swift': 'same()\nsame()\n'})
        evidence = [self.make_evidence(repeated, 'same()\nsame()\n')]
        sha = self.commit({'File.swift': 'same()\nsame()\nsame()\n'})
        self.assertTrue(all(value == 'unknown' for value in self.project(sha, evidence)[0]['File.swift'].values()))

    def test_P07_rename_with_one_modified_line(self):
        """Uses a one-to-one rename without inheriting the edited line."""
        (self.root / 'File.swift').rename(self.root / 'Renamed.swift')
        sha = self.commit({'Renamed.swift': self.original.replace('execute_08()', 'changed_08()')})
        states, _ = self.project(sha)
        self.assertEqual((states['Renamed.swift'][8], states['Renamed.swift'][9]), ('uncovered', 'covered'))

    def test_P08_P09_P10_P11_build_evidence_union_and_partial_line(self):
        """Unbuilt/untested changes stay red; old hits persist; partial hits cannot green a line."""
        source = self.original.replace('execute_08()', 'changed_08()').replace('execute_13()', 'changed_13()')
        sha = self.commit({'File.swift': source})
        self.assertEqual(self.project(sha)[0]['File.swift'][8], 'uncovered')
        b = self.make_evidence(sha, source, misses=(7, 12))
        states, _ = self.project(sha, self.evidence + [b])
        self.assertEqual([states['File.swift'][i] for i in (7, 8, 13)], ['covered']*3)
        self.assertEqual(self.project(sha, self.evidence)[0]['File.swift'][13], 'uncovered')
        partial = self.make_evidence(self.a, self.original, misses=(8,))
        self.assertEqual(self.project(self.a, [partial])[0]['File.swift'][8], 'uncovered')

    def test_P13_local_failure_and_valid_other_evidence(self):
        """One unreadable file/export does not block valid complete hits."""
        sha = self.commit({'Other.swift': 'new()\n'})
        original = self.repository.source
        def source(commit, path):
            if path == 'Other.swift':
                raise ValueError('unavailable')
            return original(commit, path)
        with patch.object(self.repository, 'source', side_effect=source):
            states, _ = self.project(sha, self.evidence + [dict(build_id=self.evidence[0]['build_id'])])
        self.assertEqual((states['Other.swift'][1], states['File.swift'][8]), ('unknown', 'covered'))

    def test_existing_complete_published_line_can_retain_mixed_region_evidence(self):
        """A previously complete mixed-source line remains green when its own bytes survive."""
        partial = self.make_evidence(self.a, self.original, misses=(8,))
        sha = self.commit({'File.swift': self.original.replace('execute_13()', 'changed_13()')})
        published = {'build_id': partial['build_id'], 'details': {'file': {
            'file': {'repo_relative_path': 'File.swift'}, 'lines': [
                {'new_line_number': 8, 'state': 'covered', 'coverage_source': 'mixed',
                 'regions': [{'covered': True, 'source': {'kind': 'historical_inheritance'}}]}]}}}
        result = self.projector.project(self.cycle, dict(base_sha=self.base,start_sha=self.base,head_sha=sha),
                                        [partial], published)
        lines = next(iter(result['details'].values()))['lines']
        self.assertEqual(next(line for line in lines if line['new_line_number']==8)['state'], 'covered')
        self.assertEqual(next(line for line in lines if line['new_line_number']==13)['state'], 'uncovered')

    def test_review_large_repeated_file_retains_forced_ordered_lines(self):
        """A size limit cannot invalidate a unique full-subsequence insertion/deletion."""
        original = 'stepA()\nstepB()\n' * 750
        a = self.commit({'File.swift': original})
        evidence = [self.make_evidence(a, original)]
        current = self.commit({'File.swift': 'newStep()\n' + original})
        states, result = self.project(current, evidence)
        self.assertEqual(result['summary']['covered'],1500)
        self.assertEqual(result['summary']['uncovered'],1)
        self.assertEqual(result['summary']['unknown'],0)
        self.assertEqual(states['File.swift'][1],'uncovered')
        # Reversing the fixed snapshots proves the corresponding deletion too.
        b = self.make_evidence(current, 'newStep()\n' + original)
        self.assertEqual(self.project(a,[b])[1]['summary']['covered'],1500)
        changed = original.splitlines(keepends=True)
        changed[750] = 'changedStep()\n'
        target = self.commit({'File.swift': ''.join(changed)})
        states, _ = self.project(target,evidence)
        self.assertEqual(states['File.swift'][1],'covered')
        self.assertEqual(states['File.swift'][751],'uncovered')
        self.assertEqual(states['File.swift'][1500],'covered')

    def test_review_large_novel_insertion_plus_edit_keeps_other_repeated_rows(self):
        """Unmatchable new rows cannot inflate the edit budget into wholesale unknown."""
        original = 'stepA()\nstepB()\n' * 750
        a = self.commit({'File.swift': original})
        evidence = [self.make_evidence(a, original)]
        lines = original.splitlines(keepends=True)
        lines[750] = 'changedStep()\n'
        novel = ''.join('novel_%04d()\n' % index for index in range(1000))
        current = self.commit({'File.swift': novel + ''.join(lines)})
        states, result = self.project(current, evidence)
        self.assertEqual(states['File.swift'][1], 'uncovered')
        self.assertEqual(states['File.swift'][1001], 'covered')
        self.assertEqual(states['File.swift'][1751], 'uncovered')
        self.assertEqual(states['File.swift'][2500], 'covered')
        self.assertGreater(result['summary']['covered'], 1490)

    def test_review_large_reversal_cannot_inherit_git_arbitrary_match(self):
        """Budget exhaustion cannot turn a unique text match into forced identity."""
        original = ''.join('execute_%04d()\n' % index for index in range(1500))
        a = self.commit({'File.swift': original})
        evidence = [self.make_evidence(a, original)]
        current = self.commit({'File.swift': ''.join(reversed(original.splitlines(keepends=True)))})
        states, result = self.project(current, evidence)
        # The same fixed objects with the full proof are the regression oracle.
        with patch('ioscoverage.analysis.mapping.mr_lines.MAX_ALIGNMENT_CELLS', 3_000_000):
            exact_states, exact_result = self.project(current, evidence)
        self.assertEqual(exact_result['summary']['covered'], 0)
        self.assertEqual(exact_result['summary']['unknown'], 1500)
        self.assertEqual(states, exact_states)
        self.assertEqual(result['summary'], exact_result['summary'])

    def test_review_rename_is_independent_of_unrelated_additions(self):
        """Only competing rename identities may invalidate unchanged renamed lines."""
        (self.root/'File.swift').rename(self.root/'Renamed.swift')
        current = self.commit({'Renamed.swift':self.original.replace('execute_08()','changed_08()'),
                               'Other.swift':''.join('unrelated_%02d()\n' % index for index in range(1,14))})
        states, _ = self.project(current)
        self.assertEqual(states['Renamed.swift'][8],'uncovered')
        self.assertEqual(states['Renamed.swift'][9],'covered')

    def test_review_competing_rename_source_stays_unknown(self):
        """An indistinguishable removed source still cannot lend guessed green."""
        a = self.commit({'Twin.swift':self.original})
        evidence = [self.make_evidence(a,self.original)]
        (self.root/'File.swift').rename(self.root/'Renamed.swift')
        (self.root/'Twin.swift').unlink()
        current = self.commit({})
        states, _ = self.project(current,evidence)
        self.assertTrue(all(state=='unknown' for state in states['Renamed.swift'].values()))
