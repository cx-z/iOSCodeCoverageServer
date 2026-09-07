    const cycleId = __CYCLE_ID_JSON__;
    const tokenKey = 'ios-coverage-operator-token';
    let token = sessionStorage.getItem(tokenKey) || '';
    let report = null;
    let selectedFileId = null;
    let selectedFile = null;
    let refreshTimer = null;
    document.getElementById('cycle-id').textContent = cycleId;
    document.getElementById('token').value = token;

    function sourceLabel(value, state) {
      const label = ({current_build:'当前 Build',historical_inheritance:'历史继承',mixed:'当前 + 历史',none:'无命中来源'})[value] || value;
      return state === 'covered' || value === 'none' ? label : `${label}（部分 region）`;
    }
    function stateLabel(value) {
      return ({covered:'Covered',uncovered:'Uncovered',not_measurable:'不可测量',unknown:'Unknown'})[value] || value;
    }
    function setStatus(state) {
      const dot = document.getElementById('status-dot');
      dot.className = `dot ${state.status}`;
      document.getElementById('status-text').textContent = ({idle:'空闲',queued:'排队中',running:'分析中',succeeded:'已完成',failed:'失败'})[state.status] || state.status;
      const time = state.completed_at || state.started_at || state.requested_at;
      document.getElementById('updated-at').textContent = time ? `更新时间 ${new Date(time).toLocaleString()}` : '尚无分析时间';
    }
    async function api(path) {
      const response = await fetch(path, {headers:{Authorization:`Bearer ${token}`},cache:'no-store'});
      let payload = {};
      try { payload = await response.json(); } catch (_) { payload = {error:'invalid_response'}; }
      if (response.status === 401 || response.status === 403) throw new Error('TOKEN_INVALID');
      if (!response.ok) throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
      return payload;
    }
    async function connect() {
      token = document.getElementById('token').value.trim();
      if (!token) return;
      sessionStorage.setItem(tokenKey, token);
      document.getElementById('auth-error').textContent = '';
      await refresh(true);
    }
    async function refresh(forceReport=false) {
      if (!token) return;
      try {
        const state = await api(`/api/v1/cycles/${cycleId}/analysis`);
        setStatus(state);
        document.getElementById('auth-panel').hidden = true;
        document.getElementById('content').hidden = false;
        const identity = state.latest_success && state.latest_success.orchestration_input_sha256;
        if (identity && (forceReport || !report || report.orchestration_input_sha256 !== identity)) {
          report = await api(`/api/v1/cycles/${cycleId}/report`);
          selectedFileId = null;
          selectedFile = null;
          renderReport();
        }
        if (state.error) document.getElementById('report-meta').textContent = `分析失败：${state.error.code}；仍展示上一次成功结果`;
      } catch (error) {
        if (error.message === 'TOKEN_INVALID') {
          sessionStorage.removeItem(tokenKey); token = ''; report = null;
          document.getElementById('auth-panel').hidden = false;
          document.getElementById('content').hidden = true;
          document.getElementById('auth-error').textContent = 'Token 不正确，请重新输入。';
        } else {
          document.getElementById('status-text').textContent = '刷新失败';
          document.getElementById('updated-at').textContent = error.message;
        }
      } finally {
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(() => refresh(false), 2000);
      }
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
      const list = document.getElementById('file-list');
      list.replaceChildren();
      for (const file of files) {
        const button = document.createElement('button');
        button.className = `file${file.file_id === selectedFileId ? ' active' : ''}`;
        const path = document.createElement('div'); path.className = 'path'; path.textContent = file.repo_relative_path;
        const counts = document.createElement('div'); counts.className = 'counts';
        counts.textContent = `✓ ${file.covered}  ✕ ${file.uncovered}  不可测 ${file.not_measurable}  ? ${file.unknown}`;
        button.append(path, counts); button.addEventListener('click', () => loadFile(file.file_id)); list.append(button);
      }
      if (!files.length) list.innerHTML = '<div class="empty">没有符合筛选条件的文件</div>';
      if (selectedFile && onlyUncovered && selectedFile.file.uncovered === 0) { selectedFileId = null; selectedFile = null; renderDetail(); }
    }
    async function loadFile(fileId) {
      try { selectedFile = await api(`/api/v1/cycles/${cycleId}/report/files/${fileId}`); selectedFileId = fileId; renderFiles(); renderDetail(); }
      catch (error) { document.getElementById('detail').innerHTML = `<div class="empty error"></div>`; document.querySelector('#detail .error').textContent = error.message; }
    }
    function renderDetail() {
      const detail = document.getElementById('detail'); detail.replaceChildren();
      if (!selectedFile) { detail.innerHTML = '<div class="empty">选择一个文件查看变更行</div>'; return; }
      const onlyUncovered = document.getElementById('only-uncovered').checked;
      const lines = selectedFile.lines.filter(line => !onlyUncovered || line.state === 'uncovered');
      if (!lines.length) { detail.innerHTML = '<div class="empty">该文件没有符合筛选条件的变更行</div>'; return; }
      for (const line of lines) {
        const row = document.createElement('div'); row.className = 'line-row';
        const number = document.createElement('div'); number.className = 'line-number'; number.textContent = line.new_line_number;
        const state = document.createElement('span'); state.className = `badge badge-${line.state}`; state.textContent = stateLabel(line.state);
        const source = document.createElement('span'); source.className = 'badge source-badge'; source.textContent = sourceLabel(line.coverage_source, line.state);
        const code = document.createElement('div'); code.className = 'source'; code.textContent = line.source_text || '';
        row.append(number, state, source, code); detail.append(row);
      }
    }
    document.getElementById('connect').addEventListener('click', () => connect().catch(error => { document.getElementById('auth-error').textContent = error.message; }));
    document.getElementById('token').addEventListener('keydown', event => { if (event.key === 'Enter') document.getElementById('connect').click(); });
    document.getElementById('only-uncovered').addEventListener('change', () => { renderFiles(); renderDetail(); });
    if (token) refresh(true);
