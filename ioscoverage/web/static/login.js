    const loginForm = document.getElementById('login-form');
    const registerForm = document.getElementById('register-form');
    const authError = document.getElementById('auth-error');
    let submitting = false;

    function showMode(mode) {
      if (submitting) return;
      loginForm.hidden = mode !== 'login';
      registerForm.hidden = mode !== 'register';
      document.getElementById('login-password').value = '';
      document.getElementById('register-password').value = '';
      authError.textContent = '';
      document.getElementById(mode === 'login' ? 'login-username' : 'register-username').focus();
    }

    async function authenticate(event, mode) {
      event.preventDefault();
      if (submitting) return;
      submitting = true;
      const form = mode === 'login' ? loginForm : registerForm;
      const password = document.getElementById(`${mode}-password`);
      const endpoint = mode === 'login' ? '/api/v1/auth/login' : '/api/v1/auth/register';
      const submit = form.querySelector('button[type="submit"]');
      submit.disabled = true;
      authError.textContent = '';
      try {
        const response = await fetch(endpoint, {
          method:'POST', cache:'no-store', credentials:'same-origin',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({schema_version:1,username:document.getElementById(`${mode}-username`).value.trim(),password:password.value})
        });
        let payload = {};
        try { payload = await response.json(); } catch (_) { payload = {}; }
        if (!response.ok) throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
        location.assign('/cycles/');
      } catch (error) {
        authError.textContent = error.message;
      } finally {
        password.value = '';
        submit.disabled = false;
        submitting = false;
      }
    }

    document.getElementById('show-register').addEventListener('click', () => showMode('register'));
    document.getElementById('show-login').addEventListener('click', () => showMode('login'));
    loginForm.addEventListener('submit', event => authenticate(event, 'login'));
    registerForm.addEventListener('submit', event => authenticate(event, 'register'));
