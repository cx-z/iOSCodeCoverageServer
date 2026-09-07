    const tokenKey = 'ios-coverage-operator-token';
    let token = sessionStorage.getItem(tokenKey) || '';
    let loadedCycles = [];
    const tokenInput = document.getElementById('token');
    tokenInput.value = token;

    async function api(path, options={}) {
      const headers = Object.assign({}, options.headers || {}, {Authorization:`Bearer ${token}`});
      const response = await fetch(path, Object.assign({}, options, {headers,cache:'no-store'}));
      let payload = {};
      try { payload = await response.json(); } catch (_) { payload = {error:'invalid_response'}; }
      if (response.status === 401 || response.status === 403) throw new Error('TOKEN_INVALID');
      if (!response.ok) throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
      return payload;
    }

    function allowedMRURL(value, cycle) {
      try {
        const parsed = new URL(value);
        const parts = parsed.pathname.split('/').filter(Boolean);
        const projectParts = String(cycle.gitlab_project_path || '').split('/').filter(Boolean);
        const expected = [...projectParts, 'merge_requests', String(cycle.mr_iid)];
        const exactPath = parts.length === expected.length && parts.every((part, index) => part === expected[index]);
        return parsed.protocol === 'https:' && parsed.hostname === cycle.gitlab_host && parsed.port === '' && parsed.username === '' && parsed.password === '' && exactPath ? parsed.href : null;
      } catch (_) { return null; }
    }

    function addCell(row, value) {
      const cell = document.createElement('td'); cell.textContent = value == null ? '—' : String(value); row.append(cell); return cell;
    }

    function renderCycles(cycles) {
      const body = document.getElementById('cycle-rows'); body.replaceChildren();
      document.getElementById('empty').hidden = cycles.length !== 0;
      for (const cycle of cycles) {
        const row = document.createElement('tr');
        const mrCell = document.createElement('td');
        const label = `${cycle.project_display_name || cycle.project || '—'} !${cycle.mr_iid || '—'}`;
        const safeURL = allowedMRURL(cycle.mr_url, cycle);
        if (safeURL) { const link = document.createElement('a'); link.href = safeURL; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = label; mrCell.append(link); }
        else { mrCell.textContent = label; }
        row.append(mrCell);
        addCell(row, cycle.target_branch); addCell(row, cycle.status); addCell(row, `${cycle.build_count} / ${cycle.run_count}`);
        const updated = cycle.updated_at ? new Date(cycle.updated_at).toLocaleString() : '—'; addCell(row, updated);
        const idCell = addCell(row, ''); const idCode = document.createElement('code'); idCode.textContent = cycle.id; idCell.append(idCode);
        const actionCell = document.createElement('td'); actionCell.className = 'actions';
        const open = document.createElement('button'); open.type = 'button'; open.textContent = '查看报告'; open.addEventListener('click', () => { location.assign(`/cycles/${encodeURIComponent(cycle.id)}`); });
        const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'secondary'; copy.textContent = '复制 ID'; copy.addEventListener('click', () => navigator.clipboard.writeText(cycle.id));
        actionCell.append(open, copy); row.append(actionCell); body.append(row);
      }
    }

    async function loadCycles() {
      const payload = await api('/api/v1/cycles'); loadedCycles = payload.cycles || [];
      const filter = document.getElementById('project-filter'); const selected = filter.value;
      const projects = [...new Map(loadedCycles.map(c => [c.project, c.project_display_name || c.project])).entries()];
      filter.replaceChildren(new Option('全部项目', ''));
      for (const item of projects) filter.append(new Option(item[1], item[0]));
      filter.value = selected; renderCycles(selected ? loadedCycles.filter(c => c.project === selected) : loadedCycles);
    }

    async function connect() {
      token = tokenInput.value.trim(); if (!token) return;
      sessionStorage.setItem(tokenKey, token); document.getElementById('auth-error').textContent = '';
      try { await loadCycles(); document.getElementById('auth-panel').hidden = true; document.getElementById('content').hidden = false; }
      catch (error) { if (error.message === 'TOKEN_INVALID') { sessionStorage.removeItem(tokenKey); token = ''; document.getElementById('auth-error').textContent = 'Token 不正确或权限不足。'; } else { document.getElementById('auth-error').textContent = error.message; } }
    }

    async function createCycle(event) {
      event.preventDefault(); const status = document.getElementById('create-status'); status.textContent = '正在读取 MR…';
      try {
        const payload = await api('/api/v1/cycles', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({schema_version:1,mr_url:document.getElementById('mr-url').value.trim()})});
        status.textContent = payload.status === 'reused' ? '已找到现有 Cycle，正在打开…' : 'Cycle 已创建，正在打开…';
        location.assign(`/cycles/${encodeURIComponent(payload.cycle.id)}`);
      } catch (error) { status.textContent = error.message === 'TOKEN_INVALID' ? 'Token 不正确或权限不足。' : error.message; }
    }

    document.getElementById('connect').addEventListener('click', connect);
    tokenInput.addEventListener('keydown', event => { if (event.key === 'Enter') connect(); });
    document.getElementById('refresh').addEventListener('click', () => loadCycles().catch(error => { document.getElementById('create-status').textContent = error.message; }));
    document.getElementById('project-filter').addEventListener('change', event => renderCycles(event.target.value ? loadedCycles.filter(c => c.project === event.target.value) : loadedCycles));
    document.getElementById('create-form').addEventListener('submit', createCycle);
    if (token) connect();
