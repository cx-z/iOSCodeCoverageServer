    const cycleId = __CYCLE_ID_JSON__;
    let report = null;
    let selectedFileId = null;
    let selectedFile = null;
    let refreshTimer = null;
    let administrator = false;
    let refreshing = null;
    let detailRequest = 0;
    let fileSelection = 0;
    document.getElementById('cycle-id').textContent = cycleId;

    function sourceLabel(value, state) {
      const label = ({current_build:'当前 Build',historical_inheritance:'历史继承',mixed:'当前 + 历史',none:'无命中来源'})[value] || value;
      return state === 'covered' || value === 'none' ? label : `${label}（部分 region）`;
    }
    function stateLabel(value) {
      return ({covered:'Covered',uncovered:'Uncovered',not_measurable:'不可测量',unknown:'Unknown'})[value] || value;
    }
    function setStatus(state) {
      const dot = document.getElementById('status-dot'); dot.className = `dot ${state.status}`;
      const diffOnly = report && report.status === 'diff_only';
      document.getElementById('status-text').textContent = state.status === 'idle' && diffOnly
        ? '仅展示 diff' : ({idle:'空闲',queued:'排队中',running:'分析中',succeeded:'已完成',failed:'失败'})[state.status] || state.status;
      document.getElementById('analysis-note').textContent = state.error
        ? `后台分析失败：${state.error.code}；${diffOnly ? '当前仅展示 diff' : '当前展示仍使用已验证的构建证据'}`
        : ({queued:'后台分析排队中',running:'后台分析中',failed:diffOnly ? '后台分析失败；当前仅展示 diff' : '后台分析失败；当前展示使用已验证的构建证据'})[state.status] || '';
      document.getElementById('analysis-times').textContent = [
        ['请求', state.requested_at], ['开始', state.started_at], ['完成', state.completed_at]
      ].filter(([, time]) => time).map(([label, time]) => `${label} ${new Date(time).toLocaleString()}`).join(' · ');
    }
    async function api(path, options={}) {
      const response = await fetch(path, Object.assign({cache:'no-store',credentials:'same-origin'}, options));
      let payload = {};
      try { payload = await response.json(); } catch (_) { payload = {error:'invalid_response'}; }
      if (!response.ok) { const error = new Error(payload.message || payload.error || `HTTP ${response.status}`); error.status = response.status; error.code = payload.error; throw error; }
      return payload;
    }
    async function readCycleView() {
      // Formal report availability is independent of live GitLab/MR verification.
      let reason;
      try {
        const next = await api(`/api/v1/cycles/${cycleId}/mr-presentation`);
        if (next.can_annotate && next.projection) return next;
        reason = ({page_version_unverified:'MR 正在更新，尚未确认最新 diff',
          mr_not_open:'MR 已关闭或已合并',
          report_not_available:'MR 覆盖投影尚不可用',
          analysis_pending:'后台分析尚未完成',
          analysis_failed:'后台分析失败'})[next.status] || '最新 MR 投影暂不可用';
      } catch (error) {
        if (error.status === 401 || error.status === 403) throw error;
        reason = '最新 MR 暂不可用';
      }
      const build = await api(`/api/v1/cycles/${cycleId}/report`);
      return {status:'build_report', can_annotate:true, mr:null, analysis:null,
        presentation_id:null, page_version:null, report:build,
        notice:`${reason}；当前展示最近一次成功的正式构建报告，非最新 MR diff。`,
        projection:{strict_match:true, summary:build.summary, files:build.files}};
    }
    function refresh() {
      // Recovery callers await the same refresh instead of dropping their file request.
      if (refreshing) return refreshing;
      refreshing = refreshReport().finally(() => { refreshing = null; });
      return refreshing;
    }
    async function refreshReport() {
      try {
        const next = await readCycleView();
        const changed = !report || next.presentation_id !== report.presentation_id ||
          next.report?.orchestration_input_sha256 !== report.report?.orchestration_input_sha256;
        report = next;
        document.getElementById('content').hidden = false;
        document.getElementById('page-error').hidden = true;
        document.getElementById('report-notice').textContent = next.notice || '';
        renderBuildMetadata();
        if (changed) {
          detailRequest++; selectedFileId = null; selectedFile = null;
          renderReport(); renderDetail();
        }
        setStatus(report.analysis || {status:'succeeded'});
        if (administrator) await refreshAdminState();
        return report;
      } catch (error) {
        detailRequest++;
        document.getElementById('content').hidden = true;
        const panel = document.getElementById('page-error'); panel.textContent = error.message; panel.hidden = false;
        return null;
      } finally {
        clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 2000);
      }
    }
    async function refreshAdminState() {
      try {
        const state = await api(`/api/v1/cycles/${cycleId}/analysis`); setStatus(state);
      } catch (_) { administrator = false; document.getElementById('retry-analysis').hidden = true; }
    }
    function renderReport() {
      const summary = report.projection.summary;
      document.getElementById('covered').textContent = summary.covered;
      document.getElementById('uncovered').textContent = summary.uncovered;
      document.getElementById('not-measurable').textContent = summary.not_measurable;
      document.getElementById('unknown').textContent = summary.unknown;
      document.getElementById('percent').textContent = summary.changed_line_coverage_percent == null ? '—' : `${summary.changed_line_coverage_percent.toFixed(2)}%`;
      document.getElementById('diff-title').textContent = report.mr ? '最新 MR 变更文件与新侧行' : '构建变更文件与新侧行';
      document.getElementById('report-meta').textContent = report.mr
        ? `MR !${report.mr.iid} · 提交 ${report.mr.diff.head_sha.slice(0, 8)} · ${report.projection.files.length} 个变更文件${report.report ? ' · 覆盖率 = Covered / (Covered + Uncovered)' : ''}`
        : `${report.projection.files.length} 个变更文件`;
      renderFiles();
    }
    function renderBuildMetadata() {
      // Package availability can change while the immutable diff snapshot stays the same.
      const build = report.report;
      if (!build) {
        document.getElementById('build-report-meta').textContent = report.build_count === 0
          ? '尚无构建包，暂无正式构建报告' : '尚无成功的正式构建报告';
        document.getElementById('updated-at').textContent = '尚无构建报告';
        return;
      }
      const percent = build.summary.changed_line_coverage_percent;
      document.getElementById('build-report-meta').textContent = `正式构建报告：Generation ${build.generation} · Build ${build.current_build_id}${build.build_diff ? ` · 提交 ${build.build_diff.head_sha.slice(0, 8)}` : ''} · 可测量覆盖率 ${percent == null ? '—' : `${percent.toFixed(2)}%`}`;
      document.getElementById('updated-at').textContent = build.updated_at ? `构建报告时间 ${new Date(build.updated_at).toLocaleString()}` : '—';
    }
    function renderFiles() {
      const onlyUncovered = document.getElementById('only-uncovered').checked;
      const files = report.projection.files.filter(file => !onlyUncovered || file.uncovered > 0);
      const list = document.getElementById('file-list'); list.replaceChildren();
      for (const file of files) {
        const button = document.createElement('button'); button.className = `file${file.file_id === selectedFileId ? ' active' : ''}`;
        const path = document.createElement('div'); path.className = 'path'; path.textContent = file.repo_relative_path;
        const counts = document.createElement('div'); counts.className = 'counts'; counts.textContent = `✓ ${file.covered}  ✕ ${file.uncovered}  不可测 ${file.not_measurable}  ? ${file.unknown}`;
        button.append(path, counts); button.addEventListener('click', () => loadFile(file.file_id)); list.append(button);
      }
      if (!files.length) { const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = '没有符合筛选条件的文件'; list.append(empty); }
      if (selectedFile && onlyUncovered && selectedFile.file.uncovered === 0) { selectedFileId = null; selectedFile = null; renderDetail(); }
    }
    async function loadFile(fileId, canRetry = true, selection = ++fileSelection) {
      const snapshot = report;
      const request = ++detailRequest;
      try {
        const path = snapshot.presentation_id
          ? `/api/v1/cycles/${cycleId}/mr-presentation?${new URLSearchParams({page_version:snapshot.page_version, presentation_id:snapshot.presentation_id, file_id:fileId})}`
          : `/api/v1/cycles/${cycleId}/report/files/${fileId}`;
        const next = await api(path);
        if (request !== detailRequest || next.file.file_id !== fileId) return;
        if (snapshot.presentation_id && (next.presentation_id !== snapshot.presentation_id ||
            next.page_version !== snapshot.page_version || next.cycle_id !== cycleId)) return;
        selectedFile = next; selectedFileId = fileId; renderFiles(); renderDetail();
      } catch (error) {
        if (request !== detailRequest) return;
        if (error.code === 'presentation_expired' && canRetry) {
          const refreshed = await refresh();
          if (selection !== fileSelection || !refreshed ||
              !refreshed.projection.files.some(file => file.file_id === fileId)) return;
          await loadFile(fileId, false, selection);
          return;
        }
        const detail = document.getElementById('detail'); detail.replaceChildren(); const empty = document.createElement('div'); empty.className = 'empty error'; empty.textContent = error.message; detail.append(empty);
      }
    }
    function renderDetail() {
      const detail = document.getElementById('detail'); detail.replaceChildren();
      if (!selectedFile) { const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = '选择一个文件查看变更行'; detail.append(empty); return; }
      const onlyUncovered = document.getElementById('only-uncovered').checked;
      const lines = selectedFile.lines.filter(line => !onlyUncovered || line.state === 'uncovered');
      if (!lines.length) { const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = '该文件没有符合筛选条件的变更行'; detail.append(empty); return; }
      for (const line of lines) {
        const row = document.createElement('div'); row.className = 'line-row';
        const number = document.createElement('div'); number.className = 'line-number'; number.textContent = line.new_line_number;
        const state = document.createElement('span'); state.className = `badge badge-${line.state}`; state.textContent = stateLabel(line.state);
        const source = document.createElement('span'); source.className = 'badge source-badge'; source.textContent = sourceLabel(line.coverage_source, line.state);
        const code = document.createElement('div'); code.className = 'source'; code.textContent = line.source_text || '';
        row.append(number, state, source, code); detail.append(row);
      }
    }
    document.getElementById('only-uncovered').addEventListener('change', () => { renderFiles(); renderDetail(); });
    document.getElementById('retry-analysis').addEventListener('click', async () => { try { await api(`/api/v1/cycles/${cycleId}/analysis/retry`, {method:'POST'}); await refreshAdminState(); } catch (error) { const panel = document.getElementById('page-error'); panel.textContent = error.message; panel.hidden = false; } });
    api('/api/v1/auth/session').then(payload => { administrator = payload.user.role === 'admin'; document.getElementById('retry-analysis').hidden = !administrator; }).catch(() => {}).finally(refresh);
