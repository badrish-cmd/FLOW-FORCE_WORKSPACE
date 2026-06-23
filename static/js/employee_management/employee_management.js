/**
 * Flow-Force Workspace - Employee Management JavaScript
 * Handles interactive features and bulk operations
 */

/**
 * Employee Management Module
 */
const EmployeeManagement = {
    /**
     * Initialize all event listeners and features
     */
    init() {
        this.setupBulkActions();
        this.setupFilterValidation();
        this.setupTableInteractions();
        this.setupFormValidation();
        this.setupDatePickerFallback();
    },

    /**
     * Setup bulk actions functionality
     */
    setupBulkActions() {
        const selectAllCheckbox = document.getElementById('select-all');
        const employeeCheckboxes = document.querySelectorAll('.employee-select');
        const bulkForm = document.getElementById('bulk-form');
        const bulkAction = document.getElementById('bulk-action');
        const bulkSubmit = document.getElementById('bulk-submit');

        const updateBulkButtonState = () => {
            const anyChecked = Array.from(employeeCheckboxes).some(cb => cb.checked);
            if (bulkSubmit) {
                bulkSubmit.disabled = !anyChecked || !bulkAction.value;
            }
        };

        // Select All functionality
        if (selectAllCheckbox) {
            selectAllCheckbox.addEventListener('change', () => {
                employeeCheckboxes.forEach(cb => {
                    cb.checked = selectAllCheckbox.checked;
                });
                updateBulkButtonState();
            });
        }

        // Individual checkbox listeners
        employeeCheckboxes.forEach(checkbox => {
            checkbox.addEventListener('change', () => {
                if (!checkbox.checked && selectAllCheckbox) {
                    selectAllCheckbox.checked = false;
                }
                updateBulkButtonState();
            });
        });

        // Bulk action dropdown listener
        if (bulkAction) {
            bulkAction.addEventListener('change', updateBulkButtonState);
        }

        // Form submission with confirmation and selected employee IDs
        if (bulkForm) {
            bulkForm.addEventListener('submit', (e) => {
                const selectedCheckboxes = Array.from(employeeCheckboxes).filter(cb => cb.checked);
                const selectedCount = selectedCheckboxes.length;

                if (!selectedCount) {
                    e.preventDefault();
                    return;
                }

                // Remove previously appended hidden inputs
                bulkForm.querySelectorAll('input[name="employee_ids"]').forEach((node) => node.remove());

                selectedCheckboxes.forEach((checkbox) => {
                    const hidden = document.createElement('input');
                    hidden.type = 'hidden';
                    hidden.name = 'employee_ids';
                    hidden.value = checkbox.value;
                    bulkForm.appendChild(hidden);
                });

                if (!confirm(`Are you sure you want to perform this action on ${selectedCount} employee(s)?`)) {
                    e.preventDefault();
                }
            });
        }
    },

    /**
     * Setup filter form validation
     */
    setupFilterValidation() {
        const filterForm = document.querySelector('.employee-filters form');
        if (filterForm) {
            const searchInput = filterForm.querySelector('input[name="search"]');
            if (searchInput) {
                searchInput.addEventListener('input', (e) => {
                    // Trim and lowercase for search consistency
                    e.target.value = e.target.value.trim();
                });
            }
        }
    },

    /**
     * Setup table interactions
     */
    setupTableInteractions() {
        const table = document.querySelector('.employee-table');
        if (!table) return;

        const rows = table.querySelectorAll('tbody tr');
        rows.forEach(row => {
            row.addEventListener('click', (e) => {
                // Don't navigate if clicking on buttons or checkboxes
                if (e.target.closest('.btn, .form-check-input')) {
                    return;
                }

                const detailLink = row.querySelector('a[href*="/"]');
                if (detailLink) {
                    window.location.href = detailLink.href;
                }
            });

            // Highlight on hover
            row.addEventListener('mouseover', () => {
                row.style.backgroundColor = '#f9fafb';
            });

            row.addEventListener('mouseout', () => {
                row.style.backgroundColor = '';
            });
        });
    },

    /**
     * Setup form validation
     */
    setupFormValidation() {
        const form = document.querySelector('.employee-form');
        if (!form) return;

        const submitBtn = form.querySelector('button[type="submit"]');
        if (submitBtn) {
            form.addEventListener('submit', (e) => {
                if (!form.checkValidity()) {
                    e.preventDefault();
                    e.stopPropagation();
                }
            });
        }
    },

    /**
     * Fallback for date picker if HTML5 not supported
     */
    setupDatePickerFallback() {
        const dateInputs = document.querySelectorAll('input[type="date"]');
        dateInputs.forEach(input => {
            // Check if browser supports HTML5 date input
            const test = document.createElement('input');
            test.setAttribute('type', 'date');
            
            if (test.type === 'text') {
                // Fallback for older browsers
                input.type = 'text';
                input.placeholder = 'YYYY-MM-DD';
                input.pattern = '\\d{4}-\\d{2}-\\d{2}';
            }
        });
    },

    /**
     * Show confirmation dialog
     */
    showConfirm(message) {
        return confirm(message);
    },

    /**
     * Format currency
     */
    formatCurrency(value) {
        return new Intl.NumberFormat('en-US', {
            style: 'currency',
            currency: 'USD'
        }).format(value);
    },

    /**
     * Format date
     */
    formatDate(date) {
        return new Intl.DateTimeFormat('en-US', {
            year: 'numeric',
            month: 'long',
            day: 'numeric'
        }).format(new Date(date));
    },

    /**
     * Export table to CSV
     */
    exportToCSV(filename = 'export.csv') {
        const table = document.querySelector('.employee-table');
        if (!table) return;

        let csv = [];
        const headers = Array.from(table.querySelectorAll('thead th'))
            .map(th => th.textContent.trim());
        csv.push(headers.join(','));

        Array.from(table.querySelectorAll('tbody tr')).forEach(row => {
            const cells = Array.from(row.querySelectorAll('td'))
                .map(td => `"${td.textContent.trim()}"`);
            csv.push(cells.join(','));
        });

        const csvContent = csv.join('\n');
        const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
        const link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = filename;
        link.click();
    },

    /**
     * Print table
     */
    printTable() {
        window.print();
    },

    /**
     * Initialize tooltips (Bootstrap)
     */
    initTooltips() {
        const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
        tooltipTriggerList.map(tooltipTriggerEl => new bootstrap.Tooltip(tooltipTriggerEl));
    },

    /**
     * Initialize popovers (Bootstrap)
     */
    initPopovers() {
        const popoverTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="popover"]'));
        popoverTriggerList.map(popoverTriggerEl => new bootstrap.Popover(popoverTriggerEl));
    },

    /**
     * Setup real-time search (debounced)
     */
    setupRealtimeSearch(searchInputSelector = 'input[name="search"]', tableSelector = '.employee-table') {
        const searchInput = document.querySelector(searchInputSelector);
        const table = document.querySelector(tableSelector);

        if (!searchInput || !table) return;

        let debounceTimer;
        searchInput.addEventListener('input', (e) => {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(() => {
                this.filterTableRows(e.target.value.toLowerCase(), table);
            }, 300);
        });
    },

    /**
     * Filter table rows based on search term
     */
    filterTableRows(searchTerm, table) {
        const rows = table.querySelectorAll('tbody tr');
        let visibleCount = 0;

        rows.forEach(row => {
            const text = row.textContent.toLowerCase();
            if (text.includes(searchTerm)) {
                row.style.display = '';
                visibleCount++;
            } else {
                row.style.display = 'none';
            }
        });

        if (visibleCount === 0) {
            const tbody = table.querySelector('tbody');
            if (!tbody.querySelector('.no-results')) {
                const noResults = document.createElement('tr');
                noResults.className = 'no-results';
                noResults.innerHTML = '<td colspan="100%" class="text-center text-muted">No results found</td>';
                tbody.appendChild(noResults);
            }
        }
    },

    /**
     * Sort table by column
     */
    sortTableByColumn(columnIndex, table) {
        const tbody = table.querySelector('tbody');
        const rows = Array.from(tbody.querySelectorAll('tr'));

        rows.sort((a, b) => {
            const aText = a.cells[columnIndex]?.textContent.trim() || '';
            const bText = b.cells[columnIndex]?.textContent.trim() || '';

            // Try numeric sort
            const aNum = parseFloat(aText);
            const bNum = parseFloat(bText);

            if (!isNaN(aNum) && !isNaN(bNum)) {
                return aNum - bNum;
            }

            return aText.localeCompare(bText);
        });

        rows.forEach(row => tbody.appendChild(row));
    }
};

/**
 * Activity Timeline Module
 */
const ActivityTimeline = {
    /**
     * Initialize activity timeline
     */
    init() {
        this.setupCollapseButtons();
        this.setupTimeAgo();
    },

    /**
     * Setup collapse buttons for activity details
     */
    setupCollapseButtons() {
        const collapseButtons = document.querySelectorAll('[data-bs-toggle="collapse"]');
        collapseButtons.forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                const target = document.querySelector(btn.dataset.bsTarget);
                if (target) {
                    target.classList.toggle('show');
                }
            });
        });
    },

    /**
     * Setup time ago display (relative time)
     */
    setupTimeAgo() {
        const elements = document.querySelectorAll('.time-ago');
        elements.forEach(el => {
            const datetime = el.getAttribute('datetime');
            if (datetime) {
                el.textContent = this.getTimeAgo(new Date(datetime));
            }
        });

        // Update every minute
        setInterval(() => {
            elements.forEach(el => {
                const datetime = el.getAttribute('datetime');
                if (datetime) {
                    el.textContent = this.getTimeAgo(new Date(datetime));
                }
            });
        }, 60000);
    },

    /**
     * Get time ago string
     */
    getTimeAgo(date) {
        const seconds = Math.floor((new Date() - date) / 1000);

        if (seconds < 60) return 'just now';
        if (seconds < 3600) return `${Math.floor(seconds / 60)} minutes ago`;
        if (seconds < 86400) return `${Math.floor(seconds / 3600)} hours ago`;
        if (seconds < 2592000) return `${Math.floor(seconds / 86400)} days ago`;

        return date.toLocaleDateString();
    }
};

/**
 * Initialize on document ready
 */
document.addEventListener('DOMContentLoaded', () => {
    EmployeeManagement.init();
    ActivityTimeline.init();
});

// Export for use in other modules
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { EmployeeManagement, ActivityTimeline };
}
