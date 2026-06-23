/**
 * Flow-Force Workspace - Modern App JavaScript
 * Handles sidebar toggles, UI interactions, and responsive behavior
 */

(function() {
    'use strict';

    /**
     * Initialize the application
     */
    function init() {
        setupSidebarToggle();
        setupFormEnhancements();
        setupTableInteractions();
        setupNotifications();
        setupKeyboardShortcuts();
        setupPageLoader();
    }

    /**
     * Setup sidebar toggle functionality
     */
    function setupSidebarToggle() {
        const sidebarToggleBtn = document.querySelector('.btn-sidebar-toggle');
        const sidebarTogglerBtn = document.querySelector('.sidebar-toggler');
        const sidebar = document.querySelector('.sidebar');

        if (sidebarToggleBtn) {
            sidebarToggleBtn.addEventListener('click', () => {
                sidebar.classList.toggle('active');
                document.body.style.overflow = sidebar.classList.contains('active') ? 'hidden' : 'auto';
            });
        }

        if (sidebarTogglerBtn) {
            sidebarTogglerBtn.addEventListener('click', () => {
                sidebar.classList.remove('active');
                document.body.style.overflow = 'auto';
            });
        }

        // Close sidebar when clicking on a link
        const navLinks = sidebar.querySelectorAll('.nav-link');
        navLinks.forEach(link => {
            link.addEventListener('click', () => {
                if (window.innerWidth < 992) {
                    sidebar.classList.remove('active');
                    document.body.style.overflow = 'auto';
                }
            });
        });

        // Close sidebar when clicking outside on mobile
        document.addEventListener('click', (e) => {
            if (window.innerWidth < 992) {
                const isClickInsideSidebar = sidebar.contains(e.target);
                const isClickOnToggle = sidebarToggleBtn && sidebarToggleBtn.contains(e.target);
                
                if (!isClickInsideSidebar && !isClickOnToggle && sidebar.classList.contains('active')) {
                    sidebar.classList.remove('active');
                    document.body.style.overflow = 'auto';
                }
            }
        });
    }

    /**
     * Enhance form interactions
     */
    function setupFormEnhancements() {
        // Add focus class to form controls
        const formControls = document.querySelectorAll('.form-control, .form-select');
        formControls.forEach(control => {
            control.addEventListener('focus', () => {
                control.classList.add('focused');
            });
            control.addEventListener('blur', () => {
                control.classList.remove('focused');
            });
        });

        // Add password visibility toggle
        setupPasswordVisibility();

        // Validate forms on submit
        const forms = document.querySelectorAll('form');
        forms.forEach(form => {
            form.addEventListener('submit', (e) => {
                if (!form.checkValidity()) {
                    e.preventDefault();
                    e.stopPropagation();
                }
                form.classList.add('was-validated');
            });
        });
    }

    /**
     * Setup password visibility toggle
     */
    function setupPasswordVisibility() {
        const passwordFields = document.querySelectorAll('input[type="password"]');
        passwordFields.forEach(field => {
            const wrapper = document.createElement('div');
            wrapper.className = 'password-field-wrapper';
            field.parentNode.insertBefore(wrapper, field);
            wrapper.appendChild(field);

            const toggleBtn = document.createElement('button');
            toggleBtn.type = 'button';
            toggleBtn.className = 'btn-password-toggle';
            toggleBtn.innerHTML = '<i class="fas fa-eye"></i>';
            toggleBtn.setAttribute('aria-label', 'Toggle password visibility');
            
            wrapper.appendChild(toggleBtn);

            toggleBtn.addEventListener('click', (e) => {
                e.preventDefault();
                const isPassword = field.type === 'password';
                field.type = isPassword ? 'text' : 'password';
                toggleBtn.innerHTML = isPassword 
                    ? '<i class="fas fa-eye-slash"></i>' 
                    : '<i class="fas fa-eye"></i>';
                field.focus();
            });
        });
    }

    /**
     * Setup table interactions
     */
    function setupTableInteractions() {
        const tables = document.querySelectorAll('.table');
        tables.forEach(table => {
            // Make rows clickable if they have a data-href attribute
            const rows = table.querySelectorAll('tbody tr[data-href]');
            rows.forEach(row => {
                row.style.cursor = 'pointer';
                row.addEventListener('click', (e) => {
                    if (e.target.tagName !== 'INPUT' && e.target.tagName !== 'BUTTON') {
                        window.location.href = row.dataset.href;
                    }
                });
                row.addEventListener('keypress', (e) => {
                    if (e.key === 'Enter') {
                        window.location.href = row.dataset.href;
                    }
                });
            });

            // Checkbox selection
            const selectAllCheckbox = table.querySelector('thead input[type="checkbox"]');
            if (selectAllCheckbox) {
                const checkboxes = table.querySelectorAll('tbody input[type="checkbox"]');
                selectAllCheckbox.addEventListener('change', () => {
                    checkboxes.forEach(checkbox => {
                        checkbox.checked = selectAllCheckbox.checked;
                    });
                });

                checkboxes.forEach(checkbox => {
                    checkbox.addEventListener('change', () => {
                        const allChecked = Array.from(checkboxes).every(cb => cb.checked);
                        const someChecked = Array.from(checkboxes).some(cb => cb.checked);
                        selectAllCheckbox.checked = allChecked;
                        selectAllCheckbox.indeterminate = someChecked && !allChecked;
                    });
                });
            }
        });
    }

    /**
     * Setup notifications
     */
    function setupNotifications() {
        // Auto-dismiss alerts after 5 seconds
        const alerts = document.querySelectorAll('.alert');
        alerts.forEach(alert => {
            setTimeout(() => {
                const bsAlert = new window.bootstrap.Alert(alert);
                bsAlert.close();
            }, 5000);
        });

        // Close button functionality
        const closeButtons = document.querySelectorAll('.btn-close');
        closeButtons.forEach(btn => {
            btn.addEventListener('click', (e) => {
                const alert = e.target.closest('.alert');
                if (alert) {
                    const bsAlert = new window.bootstrap.Alert(alert);
                    bsAlert.close();
                }
            });
        });
    }

    /**
     * Setup keyboard shortcuts
     */
    function setupKeyboardShortcuts() {
        document.addEventListener('keydown', (e) => {
            // Ctrl/Cmd + K: Focus search
            if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
                e.preventDefault();
                const searchInput = document.querySelector('input[type="search"]');
                if (searchInput) {
                    searchInput.focus();
                }
            }

            // Esc: Close modals
            if (e.key === 'Escape') {
                const modals = document.querySelectorAll('.modal.show');
                modals.forEach(modal => {
                    const bsModal = window.bootstrap.Modal.getInstance(modal);
                    if (bsModal) {
                        bsModal.hide();
                    }
                });
            }
        });
    }

    /**
     * Utility: Show loading state
     */
    window.showLoading = function(button) {
        if (!button) return;
        button.disabled = true;
        const originalHTML = button.innerHTML;
        button.innerHTML = '<span class="spinner"></span> Loading...';
        button.dataset.originalHTML = originalHTML;
    };

    /**
     * Utility: Hide loading state
     */
    window.hideLoading = function(button) {
        if (!button || !button.dataset.originalHTML) return;
        button.disabled = false;
        button.innerHTML = button.dataset.originalHTML;
    };

    /**
     * Utility: Show toast notification
     */
    window.showToast = function(message, type = 'info') {
        const toastContainer = document.querySelector('.toast-container') || createToastContainer();
        const toast = document.createElement('div');
        toast.className = `alert alert-${type} alert-dismissible fade show slide-in-up`;
        toast.innerHTML = `
            ${message}
            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        `;
        toastContainer.appendChild(toast);

        setTimeout(() => {
            toast.remove();
        }, 5000);
    };

    /**
     * Create toast container if it doesn't exist
     */
    function createToastContainer() {
        const container = document.createElement('div');
        container.className = 'toast-container position-fixed bottom-0 end-0 p-3';
        document.body.appendChild(container);
        return container;
    }

    /**
     * Setup Page Loading Progress Bar
     */
    function setupPageLoader() {
        let loader = document.getElementById('top-loading-bar');
        if (!loader) {
            loader = document.createElement('div');
            loader.id = 'top-loading-bar';
            document.body.appendChild(loader);
        }

        let animationFrame;
        let progress = 0;

        function startLoading() {
            cancelAnimationFrame(animationFrame);
            loader.style.width = '0%';
            loader.style.opacity = '1';
            progress = 0;
            
            function simulateProgress() {
                if (progress < 90) {
                    progress += (90 - progress) * 0.05;
                    loader.style.width = progress + '%';
                    animationFrame = requestAnimationFrame(simulateProgress);
                }
            }
            simulateProgress();
        }

        function stopLoading() {
            cancelAnimationFrame(animationFrame);
            loader.style.width = '100%';
            setTimeout(() => {
                loader.style.opacity = '0';
                setTimeout(() => {
                    loader.style.width = '0%';
                }, 300);
            }, 200);
        }

        document.addEventListener('click', (e) => {
            const anchor = e.target.closest('a');
            if (!anchor) return;

            const href = anchor.getAttribute('href');
            const target = anchor.getAttribute('target');

            if (!href || href.startsWith('#') || href.startsWith('javascript:') || href.startsWith('mailto:') || href.startsWith('tel:') || target === '_blank' || e.ctrlKey || e.metaKey || e.shiftKey) {
                return;
            }

            startLoading();
        });

        document.addEventListener('submit', (e) => {
            const form = e.target;
            if (form && !e.defaultPrevented) {
                startLoading();
            }
        });

        stopLoading();
        window.addEventListener('load', stopLoading);
        
        document.addEventListener('htmx:configRequest', startLoading);
        document.addEventListener('htmx:afterOnLoad', stopLoading);
    }

    /**
     * Initialize when DOM is ready
     */
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
