"""Presentation identity, protocol compatibility, and completion-protected evidence."""
import json
import os
import shutil
import subprocess
from pathlib import Path
import unittest
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch
from ioscoverage.analysis.mr_presentation import counts
from ioscoverage.storage.reports import CoverageReportError
from tests.server import test_gitlab_coverage_resolver as resolver_fixtures
MR_URL = resolver_fixtures.MR_URL
from tests.web.test_report_ui import ReportFixture
from tests.analysis import test_mr_presentation as mapping_fixtures


class PresentationProtocolTests(unittest.TestCase):
    """Checks schema 2 without weakening schema 1 or formal reports."""

    def setUp(self):
        """Reuses the read-only resolver collaborators."""
        self.fixture = resolver_fixtures.MergeRequestCoverageResolverTests()
        self.fixture.setUp()
        self.f = self.fixture
        self.page_diff = dict(base_sha=self.f.gitlab.merge_request.base_sha,
                              start_sha=self.f.gitlab.merge_request.start_sha,
                              head_sha=self.f.gitlab.merge_request.head_sha)
        self.f.gitlab.fetch_version_refs = lambda url, version: dict(self.page_diff)
        self.file_id = 'f'*64
        self.snapshot = dict(files=[], summary=self.f.report['summary'],
            details={self.file_id: dict(file=dict(file_id=self.file_id, repo_relative_path='File.swift'), lines=[])})
        self.f.report_store.get_mr_report_snapshot = lambda cycle, report: dict(self.snapshot)
        self.f.report_store.get_mr_evidence_references = lambda cycle, report: []
        self.f.report_store.get_mr_evidence = lambda cycle, report, independent=(): []
        self.f.report_store.report['files'] = []
        self.f.coverage.get_cycle = lambda cycle: {**self.f.coverage.cycle, 'mr_url': MR_URL}
        self.f.gitlab.fetch_latest_version = lambda url: {'id': '123', 'diff': dict(self.page_diff)}

    def tearDown(self):
        """Removes temporary fixture state."""
        self.fixture.tearDown()

    def test_P12_P14_generation_unchanged_head_base_start_each_invalidate(self):
        """The complete MR triple participates in snapshot/cache identity."""
        initial = self.f.resolver.resolve_presentation(MR_URL, '123')
        for field in ('head', 'base', 'start'):
            self.f.gitlab.merge_request = replace(self.f.gitlab.merge_request, **{field+'_sha': 'e'*40})
            self.page_diff[field+'_sha'] = 'e'*40
            with patch('ioscoverage.server.mr_coverage_resolver.MergeRequestPresentation.project', return_value=self.snapshot):
                result = self.f.resolver.resolve_presentation(MR_URL, '123')
            self.assertNotEqual(result['presentation_id'], initial['presentation_id'])
            self.assertTrue(result['can_annotate'])
            self.assertEqual(result['report']['generation'], initial['report']['generation'])
            initial = result

    def test_P14_report_identity_and_page_version_change_cache_keys(self):
        """A different evidence set invalidates even the same MR and generation."""
        a = self.f.resolver.resolve_presentation(MR_URL, '123')
        self.f.state_store.state['latest_success_orchestration_sha256'] = 'f'*64
        self.f.report_store.report['orchestration_input_sha256'] = 'f'*64
        b = self.f.resolver.resolve_presentation(MR_URL, '123')
        self.assertNotEqual(a['presentation_id'], b['presentation_id'])
        c = self.f.resolver.resolve_presentation(MR_URL, '124')
        self.assertNotEqual(b['presentation_id'], c['presentation_id'])
        with self.assertRaises(CoverageReportError):
            self.f.resolver.resolve_presentation(MR_URL, '124', a['presentation_id'], self.file_id)

    def test_P15_page_identity_and_no_report_fail_closed(self):
        """A stale browser latest label does not authorize new server results."""
        self.page_diff['head_sha'] = 'f'*40
        result = self.f.resolver.resolve_presentation(MR_URL, '123')
        self.assertFalse(result['can_annotate'])
        self.assertEqual(result['status'], 'page_version_unverified')
        self.f.state_store.state['latest_success_generation'] = None
        result = self.f.resolver.resolve_presentation(MR_URL, '123')
        self.assertIsNone(result['projection'])
        self.assertFalse(result['can_annotate'])

    def test_P20_strict_path_uses_formal_projection_and_schema1_unchanged(self):
        """Strict matches preserve existing decisions; old clients still reject mismatch."""
        result = self.f.resolver.resolve_presentation(MR_URL, '123')
        self.assertTrue(result['projection']['strict_match'])
        self.assertEqual(result['projection']['summary'], self.f.report['summary'])
        detail = self.f.resolver.resolve_presentation(MR_URL, '123', result['presentation_id'], self.file_id)
        self.assertEqual(detail['presentation_id'], result['presentation_id'])
        self.f.coverage.build['git_sha'] = 'd'*40
        self.assertFalse(self.f.resolver.resolve(MR_URL)['can_annotate'])

    def test_cycle_and_extension_share_the_same_immutable_presentation(self):
        """Consumers share immutable line evidence but retain their own summary contracts."""
        self.f.coverage.build['git_sha'] = 'd' * 40
        before = json.dumps(self.f.report_store.report, sort_keys=True)
        with patch('ioscoverage.server.mr_coverage_resolver.MergeRequestPresentation.project',
                   return_value=self._cache_snapshot()):
            cycle = self.f.resolver.resolve_cycle_presentation(self.f.cycle_id)
            extension = self.f.resolver.resolve_presentation(MR_URL, '123')
        self.assertEqual(cycle['presentation_id'], extension['presentation_id'])
        self.assertEqual(cycle['report'], extension['report'])
        self.assertEqual(extension['projection']['summary']['measurable'], 0)
        self.assertIsNone(extension['projection']['summary']['changed_line_coverage_percent'])
        self.assertFalse(cycle['projection']['strict_match'])
        self.assertAlmostEqual(cycle['projection']['summary']['changed_line_coverage_percent'], 12 / 13 * 100)
        detail = self.f.resolver.resolve_cycle_presentation(
            self.f.cycle_id, '123', cycle['presentation_id'], self.file_id)
        self.assertEqual(detail['lines'][0]['state'], 'covered')
        self.assertEqual(detail['file'], cycle['projection']['files'][0])
        extension_detail = self.f.resolver.resolve_presentation(
            MR_URL, '123', extension['presentation_id'], self.file_id)
        self.assertEqual(extension_detail['file'], extension['projection']['files'][0])
        self.assertEqual(extension_detail['lines'], detail['lines'])
        self.assertEqual(cycle, self.f.resolver.resolve_cycle_presentation(self.f.cycle_id))
        self.assertEqual(before, json.dumps(self.f.report_store.report, sort_keys=True))

    def test_cycle_latest_markdown_commit_reuses_old_build_hits_and_source(self):
        """Real Git and LLVM evidence expose unbuilt docs without changing the formal report."""
        mapping = mapping_fixtures.MRPresentationTests()
        mapping.setUp()
        self.addCleanup(mapping.tearDown)
        mapping.cycle = self.f.cycle_id
        evidence = mapping.make_evidence(mapping.a, mapping.original)
        latest = mapping.commit({'Guide.md': '# Latest documentation\n'})
        self.f.gitlab.project_registry = resolver_fixtures.project_registry(
            mapping.root, host='git.example.com', project_path='mobile/example-app')
        self.f.gitlab.merge_request = replace(self.f.gitlab.merge_request,
            base_sha=mapping.base, start_sha=mapping.base, head_sha=latest)
        self.page_diff.update(base_sha=mapping.base, start_sha=mapping.base, head_sha=latest)
        self.f.coverage.build.update(git_sha=mapping.a, merge_base_sha=mapping.base,
                                     target_sha_at_build=mapping.base)
        self.f.coverage.get_build = lambda key: (self.f.coverage.build if key == self.f.build_id
                                                else mapping.builds[key])
        self.f.report_store.get_mr_evidence = lambda *args: [evidence]
        self.f.report_store.get_mr_report_snapshot = lambda *args: {'files': [], 'details': {}}
        before = json.dumps(self.f.report_store.report, sort_keys=True)
        result = self.f.resolver.resolve_cycle_presentation(self.f.cycle_id)
        self.assertEqual(result['projection']['summary']['covered'], 13)
        self.assertEqual(result['projection']['summary']['not_measurable'], 1)
        self.assertEqual(result['projection']['summary']['measurable'], 13)
        self.assertEqual(result['projection']['summary']['changed_line_coverage_percent'], 100)
        file = next(item for item in result['projection']['files'] if item['repo_relative_path'] == 'Guide.md')
        detail = self.f.resolver.resolve_cycle_presentation(
            self.f.cycle_id, result['page_version'], result['presentation_id'], file['file_id'])
        self.assertEqual(detail['lines'][0]['source_text'], '# Latest documentation')
        self.assertEqual(detail['lines'][0]['state'], 'not_measurable')
        self.assertEqual(before, json.dumps(self.f.report_store.report, sort_keys=True))

    def test_produced_presentations_are_accepted_by_existing_extension(self):
        """Runs real Git/evidence output through the unmodified extension validator and client."""
        extension_root = Path(os.environ.get('IOSCOVERAGE_EXTENSION_ROOT',
            str(Path(__file__).resolve().parents[3] / 'chrome_plugins' / 'IOSCoverageGitLabExtension')))
        if not shutil.which('node') or not (extension_root / 'src/shared/contracts.js').is_file():
            self.skipTest('Requires Node and the extension checkout (IOSCOVERAGE_EXTENSION_ROOT)')
        mapping = mapping_fixtures.MRPresentationTests()
        mapping.setUp()
        self.addCleanup(mapping.tearDown)
        mapping.cycle = self.f.cycle_id
        evidence = mapping.make_evidence(mapping.a, mapping.original)
        self.f.gitlab.project_registry = resolver_fixtures.project_registry(
            mapping.root, host='git.example.com', project_path='mobile/example-app')
        self.f.coverage.get_build = lambda key: (self.f.coverage.build if key == self.f.build_id
                                                else mapping.builds[key])
        self.f.report_store.get_mr_evidence = lambda *args: [evidence]
        self.f.report_store.get_mr_report_snapshot = lambda *args: {'files': [], 'details': {}}
        cases = (
            ('mismatch', mapping.original.replace('execute_13()', 'changed_13()'), False, 12 / 13 * 100),
            ('matching_refs_with_independent_evidence', mapping.original, True, 100),
            ('zero_percent', ''.join('changed_%02d()\n' % i for i in range(1, 14)), False, 0),
            ('zero_denominator', '// baseline\n', False, None),
        )
        for name, source, matching, expected_percent in cases:
            with self.subTest(case=name):
                latest = mapping.commit({'File.swift': source, 'Guide.md': '# Documentation\n'})
                self.f.gitlab.merge_request = replace(self.f.gitlab.merge_request,
                    base_sha=mapping.base, start_sha=mapping.base, head_sha=latest)
                self.page_diff.update(base_sha=mapping.base, start_sha=mapping.base, head_sha=latest)
                self.f.coverage.build.update(git_sha=latest if matching else mapping.a,
                    merge_base_sha=mapping.base, target_sha_at_build=mapping.base)
                self.f.report_store.get_mr_evidence_references = lambda *args: [
                    {'build_id': evidence['build_id'], 'input_set_sha256': evidence['input_set_sha256']}]
                cycle = self.f.resolver.resolve_cycle_presentation(self.f.cycle_id)
                if expected_percent is None:
                    self.assertIsNone(cycle['projection']['summary']['changed_line_coverage_percent'])
                else:
                    self.assertAlmostEqual(cycle['projection']['summary']['changed_line_coverage_percent'], expected_percent)
                extension = self.f.resolver.resolve_presentation(MR_URL, '123')
                self.assertFalse(extension['projection']['strict_match'])
                self.assertEqual(extension['projection']['summary']['measurable'], 0)
                self.assertIsNone(extension['projection']['summary']['changed_line_coverage_percent'])
                details = {file['file_id']: self.f.resolver.resolve_presentation(
                    MR_URL, '123', extension['presentation_id'], file['file_id'])
                    for file in extension['projection']['files']}
                for file in extension['projection']['files']:
                    self.assertEqual(file, details[file['file_id']]['file'])
                completed = subprocess.run(['node', str(Path(__file__).with_name('extension_consumer.cjs')),
                    str(extension_root)], input=json.dumps({'summary': extension, 'details': details}),
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn('accepted', completed.stdout)
                self.assertEqual(cycle, self.f.resolver.resolve_cycle_presentation(self.f.cycle_id))

    def test_strict_mr_comments_are_excluded_in_both_consumers_and_bound_details(self):
        """Exact Build refs still exclude comments without rewriting formal report evidence."""
        mapping = mapping_fixtures.MRPresentationTests()
        mapping.setUp()
        self.addCleanup(mapping.tearDown)
        latest = mapping.commit({'File.swift': 'execute_01()\n/// documentation\nexecute_03()\n'})
        self.f.gitlab.project_registry = resolver_fixtures.project_registry(
            mapping.root, host='git.example.com', project_path='mobile/example-app')
        self.f.gitlab.merge_request = replace(self.f.gitlab.merge_request,
            base_sha=mapping.base, start_sha=mapping.base, head_sha=latest)
        self.page_diff.update(base_sha=mapping.base, start_sha=mapping.base, head_sha=latest)
        self.f.coverage.build.update(git_sha=latest, merge_base_sha=mapping.base,
                                    target_sha_at_build=mapping.base)
        lines = [{'new_line_number': n, 'state': 'covered' if n != 2 else 'uncovered',
                  'coverage_source': 'current_build', 'regions': [{'covered': n != 2}]}
                 for n in range(1, 4)]
        summary = counts(lines, coverage_available=True)
        file = {'file_id': self.file_id, 'repo_relative_path': 'File.swift', **summary}
        self.snapshot = {'files': [file], 'summary': summary,
                         'details': {self.file_id: {'file': file, 'lines': lines}}}
        self.f.report_store.report['summary'] = dict(summary)
        before = json.dumps(self.f.report_store.report, sort_keys=True)
        cycle = self.f.resolver.resolve_cycle_presentation(self.f.cycle_id)
        extension = self.f.resolver.resolve_presentation(MR_URL, '123')
        self.assertTrue(cycle['projection']['strict_match'])
        self.assertEqual(cycle, extension)
        self.assertEqual(cycle['projection']['summary']['measurable'], 2)
        self.assertEqual(cycle['projection']['summary']['uncovered'], 0)
        self.assertEqual(cycle['projection']['summary']['not_measurable'], 1)
        self.assertEqual(cycle['projection']['summary']['changed_line_coverage_percent'], 100)
        for read in (lambda: self.f.resolver.resolve_cycle_presentation(
                         self.f.cycle_id, '123', cycle['presentation_id'], self.file_id),
                     lambda: self.f.resolver.resolve_presentation(
                         MR_URL, '123', extension['presentation_id'], self.file_id)):
            detail = read()
            self.assertEqual(detail['file'], cycle['projection']['files'][0])
            self.assertEqual(detail['lines'][1]['state'], 'not_measurable')
        self.assertEqual(before, json.dumps(self.f.report_store.report, sort_keys=True))
        self.assertEqual(self.snapshot['summary']['measurable'], 3)

    def test_cycle_without_builds_exposes_verified_diff_without_creating_report(self):
        """Real fixed Git serves unbuilt source while formal/extension report gates stay intact."""
        mapping = mapping_fixtures.MRPresentationTests()
        mapping.setUp()
        self.addCleanup(mapping.tearDown)
        latest = mapping.commit({'Guide.md': '# Unbuilt documentation\n'})
        self.f.gitlab.project_registry = resolver_fixtures.project_registry(
            mapping.root, host='git.example.com', project_path='mobile/example-app')
        self.f.gitlab.merge_request = replace(self.f.gitlab.merge_request,
            base_sha=mapping.base, start_sha=mapping.base, head_sha=latest)
        self.page_diff.update(base_sha=mapping.base, start_sha=mapping.base, head_sha=latest)
        self.f.state_store.state.update(status='idle', latest_success_generation=None,
                                       requested_generation=0, completed_generation=0)
        self.f.coverage.get_cycle = lambda cycle: {**self.f.coverage.cycle, 'mr_url': MR_URL, 'build_count': 0}
        before = json.dumps(self.f.state_store.state, sort_keys=True)
        with patch.object(self.f.report_store, 'get_report', side_effect=AssertionError('no report read')):
            result = self.f.resolver.resolve_cycle_presentation(self.f.cycle_id)
            self.assertEqual(result['status'], 'diff_only')
            self.assertIsNone(result['report'])
            self.assertEqual(result['build_count'], 0)
            self.assertIn('尚无构建包', result['notice'])
            self.assertEqual(result['projection']['summary']['covered'], 0)
            self.assertEqual(result['projection']['summary']['uncovered'], 13)
            self.assertEqual(result['projection']['summary']['not_measurable'], 1)
            self.assertIsNone(result['projection']['summary']['changed_line_coverage_percent'])
            for file in result['projection']['files']:
                detail = self.f.resolver.resolve_cycle_presentation(
                    self.f.cycle_id, result['page_version'], result['presentation_id'], file['file_id'])
                self.assertTrue(all(line.get('source_text') for line in detail['lines']))
                self.assertTrue(all(line['state'] != 'covered' for line in detail['lines']))
            self.assertFalse(self.f.resolver.resolve(MR_URL)['can_annotate'])
            self.assertIsNone(self.f.resolver.resolve_presentation(MR_URL, '123')['projection'])
        self.assertEqual(before, json.dumps(self.f.state_store.state, sort_keys=True))

    def test_diff_only_still_verifies_version_and_distinguishes_pending_builds(self):
        """No report cannot bypass full refs or claim an existing Build is absent."""
        self.f.state_store.state.update(status='running', latest_success_generation=None)
        self.f.coverage.get_cycle = lambda cycle: {**self.f.coverage.cycle, 'mr_url': MR_URL, 'build_count': 1}
        with patch('ioscoverage.server.mr_coverage_resolver.MergeRequestPresentation.project',
                   return_value=self._cache_snapshot(coverage_available=False)):
            result = self.f.resolver.resolve_cycle_presentation(self.f.cycle_id)
        self.assertIn('尚无成功构建报告', result['notice'])
        self.assertEqual(result['analysis']['status'], 'running')
        self.page_diff['head_sha'] = 'e'*40
        result = self.f.resolver.resolve_cycle_presentation(self.f.cycle_id)
        self.assertFalse(result['can_annotate'])
        self.assertIsNone(result['projection'])
        self.assertEqual(result['status'], 'page_version_unverified')

    def test_cycle_version_race_no_report_and_expired_details_fail_closed(self):
        """A moving MR or missing report cannot expose old data as the latest projection."""
        self.page_diff['head_sha'] = 'e' * 40
        result = self.f.resolver.resolve_cycle_presentation(self.f.cycle_id)
        self.assertEqual(result['status'], 'page_version_unverified')
        self.assertIsNone(result['projection'])
        with self.assertRaises(CoverageReportError) as raised:
            self.f.resolver.resolve_cycle_presentation(self.f.cycle_id, '123', '0'*64, self.file_id)
        self.assertEqual(raised.exception.code, 'presentation_expired')
        self.f.state_store.state['latest_success_generation'] = None
        self.assertIsNone(self.f.resolver.resolve_cycle_presentation(self.f.cycle_id)['projection'])

    def test_cycle_without_mr_keeps_formal_report_view(self):
        """Legacy non-MR Cycles retain the existing report and file endpoint semantics."""
        self.f.coverage.get_cycle = lambda cycle: dict(self.f.coverage.cycle)
        result = self.f.resolver.resolve_cycle_presentation(self.f.cycle_id)
        self.assertEqual(result['status'], 'build_report')
        self.assertEqual(result['report'], self.f.report_store.report)
        self.assertIsNone(result['presentation_id'])

    def test_cycle_details_cannot_cross_cycle_identity(self):
        """Even a valid MR snapshot is rejected under another Cycle URL."""
        result = self.f.resolver.resolve_cycle_presentation(self.f.cycle_id)
        with self.assertRaises(CoverageReportError) as raised:
            self.f.resolver.resolve_cycle_presentation('other-cycle', '123', result['presentation_id'], self.file_id)
        self.assertEqual(raised.exception.code, 'presentation_expired')

    def _cache_snapshot(self, degraded=False, coverage_available=True):
        """Builds contrasting complete results for the same target/evidence identities."""
        lines = [{"new_line_number": number, "state": "unknown" if degraded else (
            "covered" if number < 13 else "uncovered"), "coverage_source": "none", "regions": []}
            for number in range(1,14)]
        summary = counts(lines, coverage_available=coverage_available)
        descriptor = dict(file_id=self.file_id,repo_relative_path="File.swift",**summary)
        return dict(files=[descriptor],summary=summary,details={self.file_id:dict(file=descriptor,lines=lines)},
                    retryable=degraded)

    def test_review_concurrent_resolutions_publish_one_immutable_snapshot(self):
        """A second request cannot replace an issued summary with different file contents."""
        started, release = threading.Event(), threading.Event()
        calls = []
        healthy = self._cache_snapshot()
        def project(cycle, report):
            """The old implementation let a second failed fill replace the first healthy fill."""
            calls.append(1)
            if len(calls) == 1:
                started.set()
                if not release.wait(3):
                    raise RuntimeError("test publication gate timed out")
                return healthy
            return self._cache_snapshot(True)
        self.f.report_store.get_mr_report_snapshot = project
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.f.resolver.resolve_presentation,MR_URL,'123')
            self.assertTrue(started.wait(3))
            second = executor.submit(self.f.resolver.resolve_presentation,MR_URL,'123')
            time.sleep(.03)
            release.set()
            first, second = first.result(3), second.result(3)
        self.assertEqual(len(calls),1)
        self.assertEqual(first['presentation_id'],second['presentation_id'])
        self.assertEqual(first['projection']['summary']['covered'],12)
        # External aliases cannot mutate the published cache snapshot either.
        healthy['details'][self.file_id]['lines'][0]['state']='unknown'
        detail=self.f.resolver.resolve_presentation(MR_URL,'123',first['presentation_id'],self.file_id)
        self.assertEqual(detail['lines'][0]['state'],'covered')

    def test_review_transient_degradation_recovers_without_rebinding_old_id(self):
        """Retries temporary unknowns while old issued details remain byte-consistent."""
        clock=[0]
        with patch('ioscoverage.server.mr_coverage_resolver.time.monotonic',side_effect=lambda:clock[0]):
            self.f.report_store.get_mr_report_snapshot=lambda cycle,report:self._cache_snapshot(True)
            before=self.f.resolver.resolve_presentation(MR_URL,'123')
            self.f.report_store.get_mr_report_snapshot=lambda cycle,report:self._cache_snapshot(False)
            self.assertEqual(self.f.resolver.resolve_presentation(MR_URL,'123')['presentation_id'],before['presentation_id'])
            clock[0]=16
            after=self.f.resolver.resolve_presentation(MR_URL,'123')
        self.assertNotEqual(before['presentation_id'],after['presentation_id'])
        self.assertEqual(after['projection']['summary']['covered'],12)
        old=self.f.resolver.resolve_presentation(MR_URL,'123',before['presentation_id'],self.file_id)
        new=self.f.resolver.resolve_presentation(MR_URL,'123',after['presentation_id'],self.file_id)
        self.assertTrue(all(line['state']=='unknown' for line in old['lines']))
        self.assertEqual(new['lines'][0]['state'],'covered')

    def test_review_projection_failure_does_not_poison_single_flight(self):
        """A failed owner releases its input reservation so a later request can recover."""
        self.f.report_store.get_mr_report_snapshot=lambda cycle,report:(_ for _ in ()).throw(ValueError('temporary'))
        with self.assertRaises(ValueError):
            self.f.resolver.resolve_presentation(MR_URL,'123')
        self.f.report_store.get_mr_report_snapshot=lambda cycle,report:self._cache_snapshot()
        self.assertTrue(self.f.resolver.resolve_presentation(MR_URL,'123')['can_annotate'])

    def test_review_independent_evidence_changes_identity_without_formal_generation(self):
        """An old-package analysis update participates in the input and issued IDs."""
        first=self.f.resolver.resolve_presentation(MR_URL,'123')
        self.f.report_store.get_mr_evidence_references=lambda cycle,report:[{'build_id':self.f.build_id,'input_set_sha256':'d'*64}]
        with patch('ioscoverage.server.mr_coverage_resolver.MergeRequestPresentation.project',return_value=self._cache_snapshot()):
            second=self.f.resolver.resolve_presentation(MR_URL,'123')
        self.assertNotEqual(first['presentation_id'],second['presentation_id'])
        self.assertFalse(second['projection']['strict_match'])
        self.assertEqual(first['report']['generation'],second['report']['generation'])

    def test_review_pending_failed_state_survives_annotatable_mismatch(self):
        """Valid historic evidence does not erase the actual background analysis status."""
        self.f.coverage.build['git_sha']='d'*40
        for status, expected in (('running','analysis_pending'),('failed','analysis_failed')):
            self.f.state_store.state['status']=status
            with patch('ioscoverage.server.mr_coverage_resolver.MergeRequestPresentation.project',return_value=self._cache_snapshot()):
                result=self.f.resolver.resolve_presentation(MR_URL,'123')
            self.assertEqual(result['status'],expected)
            self.assertTrue(result['can_annotate'])


class MREvidenceStoreTests(unittest.TestCase):
    """Exercises completion names, content hashes, and partial failures on disk."""

    def test_P13_evidence_requires_single_build_completion_and_export_hash(self):
        """A valid independent export loads; altered export becomes locally unavailable."""
        f = ReportFixture()
        try:
            sha = 'c'*64
            single = f.analysis/'single-build'/f.build_id/sha
            single.mkdir(parents=True)
            f._write_json(single/'inputs.json', dict(cycle_id=f.cycle_id, build_id=f.build_id, input_set_sha256=sha, build={}))
            f._write_json(single/'summary.json', dict(cycle_id=f.cycle_id, build_id=f.build_id))
            f._write_json(single/'llvm-cov-export.json', dict(type='fixture'))
            f._write_json(single/'completion.json', dict(status='complete',input_set_sha256=sha,
                outputs=f._directory_identity(single,'completion.json')))
            results_path = f.orchestration_directory/'results.json'
            results = json.loads(results_path.read_text())
            results['single_build_results'] = [dict(build_id=f.build_id,input_set_sha256=sha,
                output_directory=str(single),completion=f._identity(single/'completion.json'),summary=f._identity(single/'summary.json'))]
            f._write_json(results_path,results)
            f._refresh_orchestration_completion()
            report = f.store.get_report(f.cycle_id)
            evidence = f.store.get_mr_evidence(f.cycle_id,report)
            self.assertEqual(evidence[0]['payload'],dict(type='fixture'))
            (single/'llvm-cov-export.json').write_text('{}')
            evidence = f.store.get_mr_evidence(f.cycle_id,report)
            self.assertIsNone(evidence[0]['payload'])
            self.assertEqual(f.store.get_report(f.cycle_id),report)
        finally:
            f.close()
