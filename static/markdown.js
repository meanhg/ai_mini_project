/* 채팅용 Markdown 표시. 원문 HTML은 실행하지 않고 모두 이스케이프한다. */
(function (root) {
  'use strict';
  const escape = value => String(value).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);

  function inline(text) {
    const pattern = /`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^\s)]+)\)/g;
    let output = '', start = 0;
    for (const match of text.matchAll(pattern)) {
      output += escape(text.slice(start, match.index));
      if (match[1] !== undefined) output += '<code>' + escape(match[1]) + '</code>';
      else if (match[2] !== undefined) output += '<strong>' + escape(match[2]) + '</strong>';
      else if (/^(https?:\/\/|mailto:|#)/i.test(match[4])) {
        output += '<a href="' + escape(match[4]) + '" target="_blank" rel="noopener noreferrer">'
          + escape(match[3]) + '</a>';
      } else output += escape(match[0]);
      start = match.index + match[0].length;
    }
    return output + escape(text.slice(start));
  }

  const cells = line => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(s => s.trim());
  const tableRule = line => line.includes('|') && cells(line).every(c => /^:?-{3,}:?$/.test(c));
  function render(value) {
    const lines = String(value || '').replace(/\r\n/g, '\n').split('\n');
    let output = '', list = null;
    const close = () => { if (list) { output += '</' + list + '>'; list = null; } };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (/^\s*```/.test(line)) {
        close(); const block = [];
        while (++i < lines.length && !/^\s*```/.test(lines[i])) block.push(lines[i]);
        output += '<pre><code>' + escape(block.join('\n')) + '</code></pre>'; continue;
      }
      if (i + 1 < lines.length && line.includes('|') && tableRule(lines[i + 1])) {
        close(); output += '<div class="table-scroll"><table><thead><tr>'
          + cells(line).map(c => '<th>' + inline(c) + '</th>').join('') + '</tr></thead><tbody>';
        i++;
        while (i + 1 < lines.length && lines[i + 1].includes('|') && lines[i + 1].trim()) {
          output += '<tr>' + cells(lines[++i]).map(c => '<td>' + inline(c) + '</td>').join('') + '</tr>';
        }
        output += '</tbody></table></div>'; continue;
      }
      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      const section = line.match(/^\s*\d+\)\s+(.+)$/);
      const item = line.match(/^\s*([-*+]|\d+\.)\s+(.+)$/);
      if (heading || section) {
        close(); const level = heading ? heading[1].length : 3;
        output += '<h' + level + '>' + inline(heading ? heading[2] : line.trim()) + '</h' + level + '>';
      } else if (item) {
        const kind = /^\d/.test(item[1]) ? 'ol' : 'ul';
        if (list !== kind) { close(); list = kind; output += '<' + kind + '>'; }
        output += '<li>' + inline(item[2]) + '</li>';
      } else {
        close();
        if (line.trim()) output += /^\s*---+\s*$/.test(line) ? '<hr>' : '<p>' + inline(line) + '</p>';
      }
    }
    close(); return output;
  }
  const api = {render};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.OpsMarkdown = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
