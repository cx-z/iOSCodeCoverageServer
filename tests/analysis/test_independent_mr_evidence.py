"""Old-package producer/scheduler/storage/Git mapping when current Build cannot report."""

import copy
import hashlib
import json
import sqlite3
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from ioscoverage.analysis.orchestrator import CycleAnalysisOrchestrator
from ioscoverage.analysis.scheduler import AnalysisScheduler
from ioscoverage.analysis.single_build.analyzer import AnalysisConfiguration
from ioscoverage.analysis.mr_presentation import MergeRequestPresentation
from ioscoverage.storage.analysis_state import AnalysisStateStore
from ioscoverage.storage.reports import CoverageReportStore
from tests.analysis import test_mr_presentation as mapping_fixtures
from tests.analysis.test_orchestrator import OrchestrationWorkspace, FakeDiffAnalyzer, FakeCycleAnalyzer, sha, write_json


class IndependentMREvidenceTests(unittest.TestCase):
    """Uses actual orchestration, publication, SQLite state and fixed Git, with fake LLVM output."""

    def setUp(self):
        """Creates a valid A result before advancing the Cycle head."""
        self.mapping = mapping_fixtures.MRPresentationTests()
        self.mapping.setUp()
        self.workspace = OrchestrationWorkspace()
        self.ws = self.workspace
        self.calls = []
        with sqlite3.connect(str(self.ws.database)) as db:
            db.execute("UPDATE builds SET artifact_state='invalid' WHERE id!=?",(self.ws.build_a,))
        self._set_build(self.ws.build_a,self.mapping.a)
        self._head(self.mapping.a)
        self.configuration = AnalysisConfiguration(database_path=self.ws.database,data_dir=self.ws.data,
            analysis_root=self.ws.analysis/'single-build',repository_path=self.mapping.root)
        self.states = AnalysisStateStore(self.ws.database)
        self.scheduler = AnalysisScheduler(self.states,self._orchestrator)
        self.addCleanup(lambda: self.scheduler.shutdown(timeout=5))
        self.scheduler.start()
        self.scheduler.request_analysis(self.ws.cycle_id,'run_uploaded')
        self._completed(1)
        self.initial = self.states.get_state(self.ws.cycle_id)
        self.assertEqual(self.initial['status'],'succeeded',self.initial)
        self.report = {'orchestration_input_sha256':self.initial['latest_success_orchestration_sha256']}
        self.formal_directory = Path(self.initial['latest_success_output_directory'])
        self.formal_bytes = {path.name:path.read_bytes() for path in self.formal_directory.iterdir() if path.is_file()}
        self.reports = CoverageReportStore(self.ws.analysis,self.states)

    def tearDown(self):
        """Stops the worker before removing its temporary state and Git objects."""
        self.scheduler.shutdown(timeout=5)
        self.ws.close()
        self.mapping.tearDown()

    def _set_build(self, build_id, git_sha):
        """Binds fixture Build metadata and on-disk manifest to the real fixed Git source."""
        with sqlite3.connect(str(self.ws.database)) as db:
            row=db.execute('SELECT manifest_json FROM builds WHERE id=?',(build_id,)).fetchone()
            manifest=json.loads(row[0]); manifest['git_sha']=git_sha
            db.execute("UPDATE builds SET git_sha=?,merge_base_sha=?,target_sha_at_build=?,manifest_json=?,manifest_sha256=?,artifact_state='ready' WHERE id=?",
                (git_sha,self.mapping.base,self.mapping.base,json.dumps(manifest),sha(manifest),build_id))
        write_json(self.ws.data/'cycles'/self.ws.cycle_id/'builds'/build_id/'manifest.json',manifest)

    def _head(self, value):
        """Advances only the test Cycle's requirement head."""
        with sqlite3.connect(str(self.ws.database)) as db:
            db.execute('UPDATE coverage_cycles SET head_sha=? WHERE id=?',(value,self.ws.cycle_id))

    def _build(self, build_id):
        """Reads the immutable fixture Build through a request-local SQLite connection."""
        with sqlite3.connect(str(self.ws.database)) as db:
            db.row_factory=sqlite3.Row
            return dict(db.execute('SELECT * FROM builds WHERE id=?',(build_id,)).fetchone())

    def _completed(self, generation):
        """Waits for the real scheduler to persist this bounded fixture generation."""
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            state=self.states.get_state(self.ws.cycle_id)
            if state['completed_generation']>=generation and state['status'] in ('succeeded','failed'):
                return state
            time.sleep(.01)
        self.fail('scheduler did not finish the fixture generation')

    def _orchestrator(self):
        """Retains the production selection and failure paths around a controlled LLVM boundary."""
        cycle = FakeCycleAnalyzer(self.ws.analysis)
        cycle.lines.update(changed_new_side=10,not_measurable=0,unknown=0,deleted_excluded=0,
                           covered_with_inherited_evidence=0)
        def complete_cycle(*args):
            """Adds the formal counters consumed by the real scheduler projection."""
            result = cycle.analyze(*args)
            result.summary['changed_line_coverage_percent'] = 90
            write_json(result.output_directory/'summary.json',result.summary)
            return result
        return CycleAnalysisOrchestrator(self.configuration,SimpleNamespace(analyze=self._single),
            FakeDiffAnalyzer(self.ws.analysis),SimpleNamespace(analyze=complete_cycle))

    def _single(self, cycle_id, build_id, run_ids):
        """Emits a full completion-bound export whose new A Run adds execution hits."""
        self.calls.append((build_id,tuple(run_ids)))
        identity=sha({'cycle':cycle_id,'build':build_id,'runs':run_ids})
        directory=self.ws.analysis/'single-build'/build_id/identity
        directory.mkdir(parents=True,exist_ok=True)
        build=self._build(build_id)
        input_build={key:build[key] for key in ('git_sha','target_sha_at_build','merge_base_sha','coverage_binary','manifest_sha256')}
        input_build['manifest']=json.loads(build['manifest_json'])
        with sqlite3.connect(str(self.ws.database)) as db:
            db.row_factory=sqlite3.Row
            records=[dict(db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()) for run_id in run_ids]
        inputs={'schema_version':1,'cycle_id':cycle_id,'build_id':build_id,'input_set_sha256':identity,
            'build':input_build,'runs':[{'run_id':run['id'],**{key:run[key] for key in
                ('payload_size','payload_sha256','metadata_sha256')}} for run in records]}
        payload=copy.deepcopy(self.mapping.evidence[0]['payload'])
        payload['data'][0]['functions'][0]['regions'][0][4]=1 if len(run_ids)>1 else 0
        write_json(directory/'inputs.json',inputs)
        write_json(directory/'llvm-cov-export.json',payload)
        summary={'schema_version':1,'status':'complete','cycle_id':cycle_id,'build_id':build_id,'input_set_sha256':identity}
        write_json(directory/'summary.json',summary)
        (directory/'merged.profdata').write_bytes(b'controlled independent profile')
        outputs={path.name:{'size':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
                 for path in directory.iterdir() if path.name!='completion.json'}
        write_json(directory/'completion.json',{'schema_version':1,'status':'complete','input_set_sha256':identity,'outputs':outputs})
        return SimpleNamespace(input_set_sha256=identity,output_directory=directory,summary=summary)

    def _continued_A_upload(self, untested_b):
        """Exercises P10 through production scheduler and verified storage, not supplied evidence."""
        target=self.mapping.commit({'File.swift':self.mapping.original.replace('execute_08()','changed_08()').replace('execute_13()','changed_13()')})
        self._head(target)
        if untested_b:
            self._set_build(self.ws.build_b,target)
            with sqlite3.connect(str(self.ws.database)) as db:
                db.execute('DELETE FROM runs WHERE build_id=?',(self.ws.build_b,))
        new_run='10000000-0000-4000-8000-000000000002'
        self.ws.add_run(self.ws.build_a,new_run,b'A2')
        self.scheduler.request_analysis(self.ws.cycle_id,'run_uploaded',new_run)
        state=self._completed(2)
        self.assertEqual(state['error_code'],'current_build_has_no_analyzable_runs' if untested_b else 'current_build_not_found')
        self.assertEqual(state['latest_success_orchestration_sha256'],self.initial['latest_success_orchestration_sha256'])
        self.assertEqual({path.name:path.read_bytes() for path in self.formal_directory.iterdir() if path.is_file()},self.formal_bytes)
        references=self.reports.get_mr_evidence_references(self.ws.cycle_id,self.report)
        self.assertEqual(len(references),1)
        evidence=self.reports.get_mr_evidence(self.ws.cycle_id,self.report,references)
        projection=MergeRequestPresentation(self.mapping.repository,SimpleNamespace(compiler_repository_names=()),
            SimpleNamespace(get_build=self._build)).project(self.ws.cycle_id,
                dict(base_sha=self.mapping.base,start_sha=self.mapping.base,head_sha=target),evidence)
        lines={line['new_line_number']:line['state'] for detail in projection['details'].values() for line in detail['lines']}
        self.assertEqual([lines[index] for index in (1,7,8,9,13)],['covered','covered','uncovered','covered','uncovered'])
        self.assertIn(new_run,self.calls[-1][1])
        self.assertTrue(all(build==self.ws.build_a for build,_ in self.calls))
        # A later formal success incorporates the same independent input and
        # returns to the original strict report path without mutating old output.
        self._head(self.mapping.a)
        self.scheduler.request_analysis(self.ws.cycle_id,'manual_retry')
        state=self._completed(3)
        self.assertEqual(state['status'],'succeeded')
        report={'orchestration_input_sha256':state['latest_success_orchestration_sha256']}
        self.assertEqual(self.reports.get_mr_evidence_references(self.ws.cycle_id,report),[])

    def test_review_A_upload_without_matching_current_build(self):
        """An absent current package does not block new applicable A evidence."""
        self._continued_A_upload(False)

    def test_review_A_upload_with_untested_current_build(self):
        """A built-but-untested B does not block A or acquire execution evidence."""
        self._continued_A_upload(True)
