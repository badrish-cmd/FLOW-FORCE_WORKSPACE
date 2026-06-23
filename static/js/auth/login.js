(function() {
    'use strict';

    function init() {
        const form = document.getElementById('loginForm');
        const email = document.getElementById('email');
        const password = document.getElementById('password');
        const passwordToggle = document.getElementById('passwordToggle');

        if (!form || !email || !password || !passwordToggle) {
            return;
        }

        const rememberedEmail = localStorage.getItem('flowforce_remembered_email');

        if (rememberedEmail) {
            email.value = rememberedEmail;
        }

        passwordToggle.addEventListener('click', function() {
            const isPassword = password.type === 'password';
            password.type = isPassword ? 'text' : 'password';

            const icon = passwordToggle.querySelector('i');

            if (icon) {
                icon.classList.toggle('fa-eye');
                icon.classList.toggle('fa-eye-slash');
            }
        });

        form.addEventListener('submit', function() {
            if (email.value) {
                localStorage.setItem('flowforce_remembered_email', email.value);
            }

            const btnText = document.querySelector('#loginBtn .btn-text');
            const btnLoading = document.querySelector('#loginBtn .btn-loading');
            const loginBtn = document.getElementById('loginBtn');
            if (btnText && btnLoading && loginBtn) {
                btnText.classList.add('d-none');
                btnLoading.classList.remove('d-none');
                // Allow form submit, but disable button to prevent double-click
                setTimeout(() => { loginBtn.disabled = true; }, 10);
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
