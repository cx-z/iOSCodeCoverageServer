    async function api(path, options={}) {
      const response = await fetch(path, Object.assign({cache:'no-store',credentials:'same-origin'}, options));
      let payload = {};
      try { payload = await response.json(); } catch (_) { payload = {error:'invalid_response'}; }
      if (!response.ok) throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
      return payload;
    }

    async function write(path, method, payload) {
      return api(path, {method,headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    }

    function addCell(row, value) {
      const cell = document.createElement('td'); cell.textContent = value == null ? '—' : String(value); row.append(cell); return cell;
    }

    function renderUsers(users) {
      const body = document.getElementById('user-rows'); body.replaceChildren();
      for (const user of users) {
        const row = document.createElement('tr'); addCell(row, user.username); addCell(row, user.role); addCell(row, user.status); addCell(row, user.created_at ? new Date(user.created_at).toLocaleString() : '—');
        const actions = document.createElement('td'); actions.className = 'actions';
        const statusButton = document.createElement('button'); statusButton.type = 'button'; statusButton.className = 'secondary'; statusButton.textContent = user.status === 'active' ? '停用' : '启用';
        statusButton.addEventListener('click', async () => { const status = document.getElementById('create-status'); try { await write(`/api/v1/admin/users/${encodeURIComponent(user.id)}/status`, 'PATCH', {schema_version:1,status:user.status === 'active' ? 'disabled' : 'active'}); status.textContent = `已更新 ${user.username}`; await loadUsers(); } catch (error) { status.textContent = error.message; } });
        const password = document.createElement('input'); password.type = 'password'; password.autocomplete = 'new-password'; password.placeholder = '新密码';
        const reset = document.createElement('button'); reset.type = 'button'; reset.textContent = '重置密码'; reset.addEventListener('click', async () => { const status = document.getElementById('create-status'); try { await write(`/api/v1/admin/users/${encodeURIComponent(user.id)}/reset-password`, 'POST', {schema_version:1,password:password.value}); status.textContent = `已重置 ${user.username} 的密码`; } catch (error) { status.textContent = error.message; } finally { password.value = ''; } });
        actions.append(statusButton, password, reset); row.append(actions); body.append(row);
      }
    }

    async function loadUsers() {
      const payload = await api('/api/v1/admin/users'); renderUsers(payload.users || []);
    }

    document.getElementById('create-user').addEventListener('submit', async event => { event.preventDefault(); const password = document.getElementById('password'); const status = document.getElementById('create-status'); try { const payload = await write('/api/v1/admin/users', 'POST', {schema_version:1,username:document.getElementById('username').value.trim(),password:password.value,role:document.getElementById('role').value}); status.textContent = `已创建 ${payload.user.username}`; document.getElementById('username').value = ''; await loadUsers(); } catch (error) { status.textContent = error.message; } finally { password.value = ''; } });
    document.getElementById('refresh').addEventListener('click', () => loadUsers().catch(error => { document.getElementById('create-status').textContent = error.message; }));
    api('/api/v1/auth/session').then(payload => { if (payload.user.role !== 'admin') throw new Error('仅管理员可访问。'); document.getElementById('content').hidden = false; return loadUsers(); }).catch(error => { const panel = document.getElementById('access-error'); panel.textContent = error.message; panel.hidden = false; });
