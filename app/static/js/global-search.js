/**
 * Dotori Global Search (Navbar)
 *
 * Ctrl+K shortcut, debounced search, dropdown results.
 */
(function () {
    'use strict';

    document.addEventListener('DOMContentLoaded', function () {
        var input = document.getElementById('globalSearchInput');
        var dropdown = document.getElementById('searchDropdown');
        if (!input || !dropdown) return;

        var debounceTimer;

        input.addEventListener('input', function () {
            clearTimeout(debounceTimer);
            var q = input.value.trim();
            if (q.length < 1) {
                dropdown.classList.remove('open');
                return;
            }
            debounceTimer = setTimeout(function () { doSearch(q); }, 300);
        });

        input.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                var q = input.value.trim();
                if (q) {
                    window.location.href = '/files/?q=' + encodeURIComponent(q);
                }
            }
        });

        // Ctrl+K shortcut
        document.addEventListener('keydown', function (e) {
            if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
                e.preventDefault();
                input.focus();
                input.select();
            }
        });

        // Close on outside click
        document.addEventListener('click', function (e) {
            if (!e.target.closest('#globalSearch')) dropdown.classList.remove('open');
        });

        function doSearch(q) {
            fetch('/files/api/v1/files/?q=' + encodeURIComponent(q) + '&limit=8')
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (!data.ok) return;
                    dropdown.innerHTML = '';
                    if (data.files.length === 0) {
                        var empty = window.DotoriI18n
                            ? window.DotoriI18n.t('globalSearchNoResults')
                            : '검색 결과가 없습니다.';
                        dropdown.innerHTML = '<div class="search-hint">' + empty + '</div>';
                    } else {
                        data.files.forEach(function (f) {
                            var a = document.createElement('a');
                            a.className = 'search-result-item';
                            a.href = '/files/' + f.uid + '/';
                            a.innerHTML =
                                '<span class="search-result-name">' + escapeHtml(f.name) + '</span>'
                                + '<span class="search-result-meta">'
                                + (f.size ? (f.size / 1024 / 1024).toFixed(1) + ' MB' : '')
                                + '</span>';
                            dropdown.appendChild(a);
                        });
                    }
                    dropdown.classList.add('open');
                });
        }

        function escapeHtml(value) {
            return String(value || '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }
    });
})();
