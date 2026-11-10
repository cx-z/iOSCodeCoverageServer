"""Presentation identity, protocol compatibility, and completion-protected evidence."""
import json
import unittest
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch
from ioscoverage.storage.reports import CoverageReportError
from tests.server import test_gitlab_coverage_resolver as resolver_fixtures
MR_URL = resolver_fixtures.MR_URL
from tests.web.test_report_ui import ReportFixture


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

    def _cache_snapshot(self, degraded=False):
        """Builds contrasting complete results for the same target/evidence identities."""
        lines = [{"new_line_number": number, "state": "unknown" if degraded else (
            "covered" if number < 13 else "uncovered"), "coverage_source": "none", "regions": []}
            for number in range(1,14)]
        summary = dict(changed_new_side=13,measurable=0,covered=0 if degraded else 12,
                       uncovered=0 if degraded else 1,not_measurable=0,unknown=13 if degraded else 0,
                       changed_line_coverage_percent=None)
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
