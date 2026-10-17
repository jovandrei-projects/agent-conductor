/* Aimd - escape-first markdown subset for assistant replies.
 *
 * Twin of conductor's _summary_body (#-#### headings, - and 1. bullets,
 * ``` fences, **bold**, *italics*, `code`, ---) PLUS pipe tables and
 * http(s) links - the provider contract tells the model markdown is
 * fine, and answers citing runs commonly use tables.
 *
 * Safety contract: every source line is HTML-escaped BEFORE any markup
 * is applied, so model-authored text can never inject markup. Links are
 * restricted to http(s) targets. No arbitrary HTML is ever emitted.
 */
(function () {
'use strict';

function esc(s) {
  return (s == null ? '' : String(s)).replace(/[&<>"]/g,
    function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
}

function inline(s) {
  s = esc(s);
  // [label](http...) and bare http(s) URLs in ONE pass so a linked url
  // is never linkified twice; only http/https render as anchors.
  // `nav:<view>/<key>` links (the model's citation grammar) render as
  // in-app navigation affordances - the chat's delegated handler opens
  // the record inside the dashboard instead of leaving the page.
  s = s.replace(
    /\[([^\]\n]+)\]\((nav:[a-z-]+\/[^\s)]+|https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<"()]+)/g,
    function (m, t, u, u2) {
      var url = u || u2, label = t || u2;
      if (url.indexOf('nav:') === 0) {
        var rest = url.slice(4), sl = rest.indexOf('/');
        if (sl < 0) return label;
        return '<a href="#" data-ai-nav="' + rest.slice(0, sl) +
          '" data-ai-key="' +
          esc(decodeURIComponent(rest.slice(sl + 1))) +
          '">' + label + '</a>';
      }
      return '<a href="' + url + '" target="_blank" ' +
        'rel="noopener noreferrer">' + label + '</a>';
    });
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  return s;
}

function isTableSep(ln) {
  // | --- | --- |  (optional colons for alignment) - needs >=1 dash
  return /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(ln) &&
    ln.indexOf('-') >= 0;
}

function splitRow(ln) {
  // |a|b| or a|b -> trimmed cell list (no escaped-pipe support: subset)
  var t = ln.trim();
  if (t.charAt(0) === '|') t = t.slice(1);
  if (t.charAt(t.length - 1) === '|') t = t.slice(0, -1);
  return t.split('|').map(function (c) { return c.trim(); });
}

function render(src) {
  var lines = String(src == null ? '' : src).split('\n');
  var out = [], inPre = false, inUl = false, inOl = false, i;

  function closeLists() {
    if (inUl) { out.push('</ul>'); inUl = false; }
    if (inOl) { out.push('</ol>'); inOl = false; }
  }

  for (i = 0; i < lines.length; i++) {
    var ln = lines[i];
    if (inPre) {
      if (ln.trim() === '```') { out.push('</code></pre>'); inPre = false; }
      else out.push(esc(ln) + '\n');
      continue;
    }
    if (/^```/.test(ln.trim())) {
      closeLists();
      var lang = ln.trim().slice(3).trim();
      out.push('<pre class="mdpre"><code' +
        (lang ? ' class="lang-' + esc(lang) + '"' : '') + '>');
      inPre = true;
      continue;
    }
    // pipe table: header row | sep row | body rows
    if (ln.indexOf('|') >= 0 && i + 1 < lines.length &&
        isTableSep(lines[i + 1]) && ln.trim().charAt(0) === '|') {
      closeLists();
      var head = splitRow(ln);
      i += 2;
      var rows = [];
      while (i < lines.length && lines[i].trim().charAt(0) === '|' &&
             lines[i].indexOf('|', 1) >= 0) {
        rows.push(splitRow(lines[i]));
        i++;
      }
      i--;
      out.push('<table class="mdtable"><thead><tr>' +
        head.map(function (c) { return '<th>' + inline(c) + '</th>'; })
          .join('') +
        '</tr></thead><tbody>' +
        rows.map(function (r) {
          return '<tr>' + r.map(function (c) {
            return '<td>' + inline(c) + '</td>'; }).join('') + '</tr>';
        }).join('') + '</tbody></table>');
      continue;
    }
    var m = ln.match(/^(#{1,4}) /);
    if (m) {
      closeLists();
      var lvl = m[1].length;
      out.push('<h' + lvl + '>' + inline(ln.slice(lvl + 1)) + '</h' +
        lvl + '>');
      continue;
    }
    if (/^-{3,}\s*$/.test(ln.trim())) {
      closeLists();
      out.push('<hr>');
      continue;
    }
    if (ln.indexOf('- ') === 0 || ln.indexOf('* ') === 0) {
      if (inOl) { out.push('</ol>'); inOl = false; }
      if (!inUl) { out.push('<ul>'); inUl = true; }
      out.push('<li>' + inline(ln.slice(2)) + '</li>');
      continue;
    }
    var om = ln.match(/^\d+\. /);
    if (om) {
      if (inUl) { out.push('</ul>'); inUl = false; }
      if (!inOl) { out.push('<ol>'); inOl = true; }
      out.push('<li>' + inline(ln.slice(om[0].length)) + '</li>');
      continue;
    }
    if (ln.indexOf('> ') === 0 || ln.trim() === '>') {
      closeLists();
      out.push('<blockquote>' + inline(ln.replace(/^>\s?/, '')) +
        '</blockquote>');
      continue;
    }
    if (!ln.trim()) { closeLists(); continue; }
    closeLists();
    out.push('<p>' + inline(ln) + '</p>');
  }
  closeLists();
  if (inPre) out.push('</code></pre>');
  return out.join('');
}

if (typeof window !== 'undefined') window.Aimd = { render: render, inline: inline };
if (typeof module !== 'undefined' && module.exports)
  module.exports = { render: render, inline: inline };
})();
