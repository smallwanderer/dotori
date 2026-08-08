/**
 * Dotori Theme Toggle (Dark Mode)
 *
 * Reads/writes 'dotori-theme' in localStorage.
 * Toggles the 'dark' class on <html>.
 */
(function () {
    'use strict';

    var html = document.documentElement;
    var saved = localStorage.getItem('dotori-theme');
    if (saved === 'dark') html.classList.add('dark');

    document.addEventListener('DOMContentLoaded', function () {
        var btn = document.getElementById('themeToggle');
        var icon = document.getElementById('themeIcon');
        if (!btn || !icon) return;

        var SUN_PATH = '<circle cx="12" cy="12" r="5"/>'
            + '<line x1="12" y1="1" x2="12" y2="3"/>'
            + '<line x1="12" y1="21" x2="12" y2="23"/>'
            + '<line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/>'
            + '<line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>'
            + '<line x1="1" y1="12" x2="3" y2="12"/>'
            + '<line x1="21" y1="12" x2="23" y2="12"/>'
            + '<line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>'
            + '<line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>';
        var MOON_PATH = '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>';

        function updateIcon() {
            icon.innerHTML = html.classList.contains('dark') ? SUN_PATH : MOON_PATH;
        }
        updateIcon();

        btn.addEventListener('click', function () {
            html.classList.toggle('dark');
            localStorage.setItem('dotori-theme', html.classList.contains('dark') ? 'dark' : 'light');
            updateIcon();
        });
    });
})();
