    const cycleId = __CYCLE_ID_JSON__;
    let report = null;
    let selectedFileId = null;
    let selectedFile = null;
    let refreshTimer = null;
    let administrator = false;
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
      document.getElementById('status-text').textContent = ({idle:'空闲',queued:'排队中',running:'分析中',succeeded:'已完成',failed:'失败'})[state.status] || state.status;
      const time = state.completed_at || state.started_at || state.requested_at;
      document.getElementById('updated-at').textContent = time ? `更新时间 ${new Date(time).toLocaleString()}` : '尚无分析时间';
    }
    async function api(path, options={}) {
      const response = await fetch(path, Object.assign({cache:'no-store',credentials:'same-origin'}, options));
      let payload = {};
      try { payload = await response.json(); } catch (_) { payload = {error:'invalid_response'}; }
      if (!response.ok) { const error = new Error(payload.message || payload.error || `HTTP ${response.status}`); error.status = response.status; throw error; }
      return payload;
    }
    async function refresh() {
      try {
        const next = await api(`/api/v1/cycles/${cycleId}/report`);
        const changed = !report || next.orchestration_input_sha256 !== report.orchestration_input_sha256;
        report = next; document.getElementById('content').hidden = false; document.getElementById('page-error').hidden = true;
        if (changed) { selectedFileId = null; selectedFile = null; renderReport(); }
        if (!administrator) { document.getElementById('status-dot').className = 'dot succeeded'; document.getElementById('status-text').textContent = '已完成'; document.getElementById('updated-at').textContent = report.updated_at ? `更新时间 ${new Date(report.updated_at).toLocaleString()}` : '—'; }
        if (administrator) await refreshAdminState();
      } catch (error) {
        const panel = document.getElementById('page-error'); panel.textContent = error.message; panel.hidden = false;
      } finally {
        clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 2000);
      }
    }
    async function refreshAdminState() {
      try {
        const state = await api(`/api/v1/cycles/${cycleId}/analysis`); setStatus(state);
        if (state.error) document.getElementById('report-meta').textContent = `分析失败：${state.error.code}；仍展示上一次成功结果`;
      } catch (_) { administrator = false; document.getElementById('retry-analysis').hidden = true; }
    }
    function renderReport() {
      const summary = report.summary;
      document.getElementById('covered').textContent = summary.covered;
      document.getElementById('uncovered').textContent = summary.uncovered;
      document.getElementById('not-measurable').textContent = summary.not_measurable;
      document.getElementById('unknown').textContent = summary.unknown;
      document.getElementById('percent').textContent = summary.changed_line_coverage_percent == null ? '—' : `${summary.changed_line_coverage_percent.toFixed(2)}%`;
      document.getElementById('report-meta').textContent = `Generation ${report.generation} · Current Build ${report.current_build_id} · ${report.files.length} 个变更文件`;
      renderFiles();
    }
    function renderFiles() {
      const onlyUncovered = document.getElementById('only-uncovered').checked;
      const files = report.files.filter(file => !onlyUncovered || file.uncovered > 0);
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
    async function loadFile(fileId) {
      try { selectedFile = await api(`/api/v1/cycles/${cycleId}/report/files/${fileId}`); selectedFileId = fileId; renderFiles(); renderDetail(); }
      catch (error) { const detail = document.getElementById('detail'); detail.replaceChildren(); const empty = document.createElement('div'); empty.className = 'empty error'; empty.textContent = error.message; detail.append(empty); }
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
