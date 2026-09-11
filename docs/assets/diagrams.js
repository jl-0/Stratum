/* Stratum documentation — diagram rendering.
 *
 * Loaded only by pages that carry a <pre class="mermaid"> figure, after the
 * pinned Mermaid script. Owns two things the diagrams must not hard-code:
 *
 *   theming   The palette is read off :root at run time and handed to Mermaid
 *             as themeVariables, so a diagram follows light/dark with the rest
 *             of the page. No diagram contains a literal colour.
 *   failure   If Mermaid did not load — offline, CDN blocked — every figure
 *             keeps its source visible as a code block rather than collapsing
 *             to nothing. An unrendered diagram is still readable.
 *
 * No build step, no ES modules, no fetch: see CLAUDE.md.
 */
(function () {
  'use strict';

  var nodes = [].slice.call(document.querySelectorAll('pre.mermaid'));
  if (!nodes.length) { return; }

  /* Keep the source: Mermaid replaces the element's content with SVG, and a
     re-render on theme change needs the original text back. */
  nodes.forEach(function (n) { n.setAttribute('data-src', n.textContent); });

  if (typeof window.mermaid === 'undefined') {
    document.documentElement.classList.add('no-mermaid');
    return;                       /* the <pre> stays a code block; see the CSS */
  }

  function token(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }

  function theme() {
    var text = token('--text', '#1f1c18');
    var line = token('--border-strong', '#cec7ba');
    var surface = token('--surface-2', '#f4f1ec');
    var accent = token('--accent', '#a8481f');
    var accentSoft = token('--accent-soft', '#f7ebe4');
    var muted = token('--muted', '#6b6459');
    return {
      background: token('--surface', '#ffffff'),
      primaryColor: surface,
      primaryBorderColor: line,
      primaryTextColor: text,
      secondaryColor: accentSoft,
      secondaryBorderColor: accent,
      secondaryTextColor: text,
      tertiaryColor: token('--code-bg', '#f5f2ec'),
      tertiaryBorderColor: token('--border', '#e3ded5'),
      tertiaryTextColor: text,
      lineColor: line,
      textColor: text,
      mainBkg: surface,
      nodeBorder: line,
      nodeTextColor: text,
      clusterBkg: token('--bg', '#fbfaf8'),
      clusterBorder: token('--border', '#e3ded5'),
      titleColor: muted,
      edgeLabelBackground: token('--surface', '#ffffff'),
      /* sequence diagrams */
      actorBkg: surface,
      actorBorder: line,
      actorTextColor: text,
      actorLineColor: line,
      signalColor: text,
      signalTextColor: text,
      labelBoxBkgColor: accentSoft,
      labelBoxBorderColor: accent,
      labelTextColor: text,
      loopTextColor: text,
      noteBkgColor: accentSoft,
      noteBorderColor: accent,
      noteTextColor: text,
      sequenceNumberColor: token('--surface', '#ffffff')
    };
  }

  function config() {
    return {
      startOnLoad: false,
      securityLevel: 'strict',
      theme: 'base',
      themeVariables: theme(),
      fontFamily: token('--sans', 'system-ui, sans-serif'),
      fontSize: 15,
      /* The prose column is ~76ch. Mermaid scales a diagram down to fit, so keep the
         natural width near the column or the labels render at half size. */
      flowchart: { useMaxWidth: true, curve: 'basis', htmlLabels: true,
                   padding: 8, nodeSpacing: 34, rankSpacing: 40 },
      sequence: { useMaxWidth: true, mirrorActors: false, boxMargin: 8, wrap: true,
                  actorFontSize: 14, messageFontSize: 13, noteFontSize: 13 }
    };
  }

  function render() {
    nodes.forEach(function (n) {
      n.removeAttribute('data-processed');
      n.innerHTML = '';
      n.appendChild(document.createTextNode(n.getAttribute('data-src')));
    });
    try {
      window.mermaid.initialize(config());
      var out = window.mermaid.run({ nodes: nodes });
      /* mermaid.run is a promise in v10+; mark rendered either way */
      if (out && typeof out.then === 'function') {
        out.then(mark).catch(fail);
      } else {
        mark();
      }
    } catch (e) {
      fail(e);
    }
  }

  function mark() {
    nodes.forEach(function (n) {
      if (n.querySelector('svg')) { n.classList.add('rendered'); }
    });
  }

  function fail(e) {
    document.documentElement.classList.add('no-mermaid');
    if (window.console && console.warn) { console.warn('[stratum] diagram render failed:', e); }
  }

  render();

  /* The site has no theme toggle: light/dark comes from the OS. Mermaid bakes
     colours into the SVG at render time, so follow the change. */
  var mq = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)');
  if (mq) {
    var onChange = function () { nodes.forEach(function (n) { n.classList.remove('rendered'); }); render(); };
    if (mq.addEventListener) { mq.addEventListener('change', onChange); }
    else if (mq.addListener) { mq.addListener(onChange); }
  }
})();
