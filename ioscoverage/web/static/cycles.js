    let loadedCycles = [];
    let createdCycle = null;

    async function api(path, options={}) {
      const response = await fetch(path, Object.assign({cache:'no-store',credentials:'same-origin'}, options));
      let payload = {};
      try { payload = await response.json(); } catch (_) { payload = {error:'invalid_response'}; }
      if (!response.ok) {
        const error = new Error(payload.message || payload.error || `HTTP ${response.status}`);
        error.status = response.status; error.code = payload.error; throw error;
      }
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
        addCell(row, Array.isArray(cycle.submitters) ? cycle.submitters.map(item => item.username).join(', ') : '—');
        addCell(row, cycle.updated_at ? new Date(cycle.updated_at).toLocaleString() : '—');
        const idCell = addCell(row, ''); const idCode = document.createElement('code'); idCode.textContent = cycle.id; idCell.append(idCode);
        const actionCell = document.createElement('td'); actionCell.className = 'actions';
        const open = document.createElement('button'); open.type = 'button'; open.textContent = '查看报告'; open.addEventListener('click', () => { location.assign(`/cycles/${encodeURIComponent(cycle.id)}`); });
        const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'secondary'; copy.textContent = '复制 ID'; copy.addEventListener('click', () => navigator.clipboard.writeText(cycle.id));
        actionCell.append(open, copy); row.append(actionCell); body.append(row);
      }
    }

    function showAuthenticated(user) {
      document.getElementById('current-user').textContent = user.username;
      document.getElementById('current-role').textContent = user.role;
      document.getElementById('admin-users').hidden = user.role !== 'admin';
      document.getElementById('identity').hidden = false;
      document.getElementById('content').hidden = false;
    }

    function redirectToLogin() {
      loadedCycles = []; createdCycle = null;
      document.getElementById('identity').hidden = true;
      document.getElementById('content').hidden = true;
      document.getElementById('cycle-rows').replaceChildren();
      location.assign('/login/');
    }

    function showRequestError(error) {
      if (error.status === 401) { redirectToLogin(); return; }
      const message = document.getElementById('page-error');
      message.textContent = error.message;
      message.hidden = false;
    }

    async function loadCycles() {
      const payload = await api('/api/v1/cycles'); loadedCycles = payload.cycles || [];
      const filter = document.getElementById('project-filter'); const selected = filter.value;
      const projects = [...new Map(loadedCycles.map(c => [c.project, c.project_display_name || c.project])).entries()];
      filter.replaceChildren(new Option('全部项目', ''));
      for (const item of projects) filter.append(new Option(item[1], item[0]));
      filter.value = selected; renderCycles(selected ? loadedCycles.filter(c => c.project === selected) : loadedCycles);
    }

    async function createCycle(event) {
      event.preventDefault(); const status = document.getElementById('create-status'); status.textContent = '正在读取 MR…';
      try {
        const payload = await api('/api/v1/cycles', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({schema_version:1,mr_url:document.getElementById('mr-url').value.trim()})});
        createdCycle = payload.cycle; document.getElementById('created-cycle-id').textContent = payload.cycle.id;
        document.getElementById('created-cycle').hidden = false;
        status.textContent = payload.submission_status === 'existing' ? '该账号已提交过此 MR，已复用现有 Cycle。' : (payload.status === 'reused' ? '已复用现有 Cycle，并记录本次提交关系。' : 'Cycle 已创建。');
        await loadCycles();
      } catch (error) { if (error.status === 401) redirectToLogin(); else status.textContent = error.message; }
    }

    document.getElementById('logout').addEventListener('click', async () => { try { await api('/api/v1/auth/logout', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); redirectToLogin(); } catch (error) { showRequestError(error); } });
    document.getElementById('refresh').addEventListener('click', () => loadCycles().catch(showRequestError));
    document.getElementById('project-filter').addEventListener('change', event => renderCycles(event.target.value ? loadedCycles.filter(c => c.project === event.target.value) : loadedCycles));
    document.getElementById('create-form').addEventListener('submit', createCycle);
    document.getElementById('copy-created-cycle').addEventListener('click', () => { if (createdCycle) navigator.clipboard.writeText(createdCycle.id); });
    document.getElementById('open-created-report').addEventListener('click', () => { if (createdCycle) location.assign(`/cycles/${encodeURIComponent(createdCycle.id)}`); });
    api('/api/v1/auth/session').then(payload => { showAuthenticated(payload.user); return loadCycles(); }).catch(showRequestError);
