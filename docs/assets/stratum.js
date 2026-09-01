/* Stratum documentation — shared navigation.
 *
 * The site has no build step. Every page sets `data-page` and `data-root` on
 * <body>; this file owns the single copy of the nav model and renders the
 * sidebar, the active-page highlight and the prev/next pager from it.
 *
 * Adding a page = one entry in PAGES below + the page file itself.
 */
(function () {
  'use strict';

  var PAGES = [
    { section: null, items: [
      { id: 'index', href: 'index.html', label: 'Overview' }
    ]},
    { section: 'Design', items: [
      { id: 'design/concepts',     href: 'design/concepts.html',     label: 'Concepts & vocabulary' },
      { id: 'design/architecture', href: 'design/architecture.html', label: 'Why this architecture' },
      { id: 'design/caching',      href: 'design/caching.html',      label: 'Caching & iteration' },
      { id: 'design/execution',    href: 'design/execution.html',    label: 'Execution & orchestration' }
    ]},
    { section: 'Reference', items: [
      { id: 'reference/plugins',  href: 'reference/plugins.html',  label: 'Plugin contracts' },
      { id: 'reference/types',    href: 'reference/types.html',    label: 'Core types' },
      { id: 'reference/manifest', href: 'reference/manifest.html', label: 'Run manifest' }
    ]},
    { section: 'Project', items: [
      { id: 'decisions/index', href: 'decisions/index.html', label: 'Decision records' },
      { id: 'status',          href: 'status.html',          label: 'Status & open questions' }
    ]}
  ];

  var SOURCES = [
    { href: 'specs/00-overview.md', label: 'Component specs (Markdown)' },
    { href: 'design/Cloud-Mosaic-Architecture.html', label: 'Original proposal' },
    { href: 'notes/2026-08-28-mines-tagup.md', label: 'Mines tag-up notes' }
  ];

  var body = document.body;
  var here = body.getAttribute('data-page') || '';
  var root = body.getAttribute('data-root') || '';

  function el(tag, attrs, text) {
    var n = document.createElement(tag);
    if (attrs) { for (var k in attrs) { n.setAttribute(k, attrs[k]); } }
    if (text != null) { n.textContent = text; }
    return n;
  }

  /* ---- sidebar ---- */

  var side = document.getElementById('sidebar');
  if (side) {
    var flat = [];
    PAGES.forEach(function (group) {
      if (group.section) { side.appendChild(el('h2', null, group.section)); }
      var ul = el('ul');
      group.items.forEach(function (item) {
        flat.push(item);
        var a = el('a', { href: root + item.href }, item.label);
        if (item.id === here) { a.className = 'current'; a.setAttribute('aria-current', 'page'); }
        var li = el('li');
        li.appendChild(a);
        ul.appendChild(li);
      });
      side.appendChild(ul);
    });

    side.appendChild(el('h2', null, 'Sources'));
    var su = el('ul');
    SOURCES.forEach(function (s) {
      var li = el('li');
      li.appendChild(el('a', { href: root + s.href }, s.label));
      su.appendChild(li);
    });
    side.appendChild(su);

    /* ---- prev / next ---- */

    var idx = flat.map(function (p) { return p.id; }).indexOf(here);
    var pager = document.querySelector('.pager');
    if (pager && idx !== -1) {
      if (idx > 0) {
        var p = flat[idx - 1];
        var pa = el('a', { href: root + p.href, class: 'prev' });
        pa.appendChild(el('span', { class: 'dir' }, 'Previous'));
        pa.appendChild(document.createTextNode(p.label));
        pager.appendChild(pa);
      }
      if (idx < flat.length - 1) {
        var n = flat[idx + 1];
        var na = el('a', { href: root + n.href, class: 'next' });
        na.appendChild(el('span', { class: 'dir' }, 'Next'));
        na.appendChild(document.createTextNode(n.label));
        pager.appendChild(na);
      }
    }
  }

  /* ---- heading anchors ---- */

  var mainEl = document.querySelector('main');
  if (mainEl) {
    mainEl.querySelectorAll('h2[id], h3[id]').forEach(function (h) {
      var a = el('a', { href: '#' + h.id, class: 'anchor', 'aria-label': 'Link to this section' }, '#');
      h.appendChild(a);
    });
  }
})();
