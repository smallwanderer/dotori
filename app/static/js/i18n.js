/**
 * Dotori i18n Engine
 *
 * Provides language switching (ko/en) with:
 *  - Async JSON namespace loading  (new pattern)
 *  - Inline register()             (backward compatible)
 *  - data-i18n attribute scanning
 *  - Placeholder interpolation     e.g. "{0}개 파일"
 */
(function () {
    'use strict';

    var STORAGE_KEY = 'dotori:lang';
    var dictionaries = { ko: {}, en: {} };
    var loadedNamespaces = {};
    var currentLang = loadSavedLanguage();

    /* ── persistence ── */

    function loadSavedLanguage() {
        try {
            var saved = localStorage.getItem(STORAGE_KEY);
            return saved === 'en' ? 'en' : 'ko';
        } catch (_) {
            return 'ko';
        }
    }

    function saveLanguage(lang) {
        try {
            localStorage.setItem(STORAGE_KEY, lang);
        } catch (_) { /* storage disabled */ }
    }

    /* ── namespace loader ── */

    /**
     * Load a translation namespace from static JSON files.
     * Safe to call multiple times — duplicates are ignored.
     *
     * @param {string} ns  Namespace name, e.g. "common", "file_list"
     * @returns {Promise<void>}
     */
    function loadNamespace(ns) {
        if (loadedNamespaces[ns]) return loadedNamespaces[ns];

        var basePath = '/static/i18n';
        var meta = document.querySelector('meta[name="i18n-base"]');
        if (meta && meta.content) basePath = meta.content;

        var promise = Promise.all([
            fetch(basePath + '/ko/' + ns + '.json').then(function (r) { return r.json(); }),
            fetch(basePath + '/en/' + ns + '.json').then(function (r) { return r.json(); }),
        ]).then(function (results) {
            mergeDict('ko', results[0]);
            mergeDict('en', results[1]);
            // Re-apply if DOM is already interactive
            if (document.readyState !== 'loading') {
                apply();
            }
        });

        loadedNamespaces[ns] = promise;
        return promise;
    }

    function mergeDict(lang, data) {
        var target = dictionaries[lang];
        var keys = Object.keys(data);
        for (var i = 0; i < keys.length; i++) {
            target[keys[i]] = data[keys[i]];
        }
    }

    /* ── backward-compatible register ── */

    /**
     * Merge inline dictionaries (used by child templates not yet migrated).
     *
     * @param {{ ko?: object, en?: object }} extra
     */
    function register(extra) {
        if (extra && extra.ko) mergeDict('ko', extra.ko);
        if (extra && extra.en) mergeDict('en', extra.en);
        apply();
    }

    /* ── translation ── */

    /**
     * Translate a key with optional interpolation.
     *
     * Function values  (from register):  t('selected', 3)  → fn(3)
     * Placeholder values (from JSON):    t('count', 3)     → "3개 파일"  ({0} replaced)
     *
     * @param {string} key
     * @param {...*} args
     * @returns {string|undefined}
     */
    function translate(key) {
        var value = (dictionaries[currentLang] && dictionaries[currentLang][key]) ||
                    dictionaries.ko[key];
        if (value === undefined) return undefined;

        var args = Array.prototype.slice.call(arguments, 1);
        if (typeof value === 'function') return value.apply(null, args);
        if (args.length > 0) {
            return String(value).replace(/\{(\d+)\}/g, function (match, index) {
                var i = parseInt(index, 10);
                return i < args.length ? args[i] : match;
            });
        }
        return value;
    }

    /* ── DOM application ── */

    /**
     * Scan the DOM and apply translations to all data-i18n-* attributes.
     * Idempotent — safe to call multiple times.
     */
    function apply() {
        document.documentElement.lang = currentLang;

        document.querySelectorAll('[data-i18n]').forEach(function (el) {
            var v = translate(el.dataset.i18n);
            if (v !== undefined) el.textContent = v;
        });
        document.querySelectorAll('[data-i18n-html]').forEach(function (el) {
            var v = translate(el.dataset.i18nHtml);
            if (v !== undefined) el.innerHTML = v;
        });
        document.querySelectorAll('[data-i18n-placeholder]').forEach(function (el) {
            var v = translate(el.dataset.i18nPlaceholder);
            if (v !== undefined) el.placeholder = v;
        });
        document.querySelectorAll('[data-i18n-title]').forEach(function (el) {
            var v = translate(el.dataset.i18nTitle);
            if (v !== undefined) el.title = v;
        });

        var toggle = document.getElementById('languageToggle');
        if (toggle) toggle.textContent = currentLang === 'ko' ? 'ENG' : 'KOR';

        document.dispatchEvent(
            new CustomEvent('dotori:languagechange', { detail: { language: currentLang } })
        );
    }

    /* ── language controls ── */

    function setLanguage(lang) {
        currentLang = lang === 'en' ? 'en' : 'ko';
        saveLanguage(currentLang);
        apply();
    }

    function toggleLanguage() {
        setLanguage(currentLang === 'ko' ? 'en' : 'ko');
    }

    /* ── public API ── */

    window.DotoriI18n = {
        loadNamespace: loadNamespace,
        register: register,
        apply: apply,
        getLanguage: function () { return currentLang; },
        setLanguage: setLanguage,
        t: translate,
        toggle: toggleLanguage,
    };

    /* ── auto-init ── */

    document.addEventListener('DOMContentLoaded', function () {
        var btn = document.getElementById('languageToggle');
        if (btn) btn.addEventListener('click', toggleLanguage);
        apply();
    });
})();
