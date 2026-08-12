// 迷你 Markdown 渲染器（仅支持更新日志用到的语法：标题 / 列表 / 加粗 / 行内代码）
// 安全：先转义 HTML 再应用标记 → 无 XSS；零依赖，可离线运行（与 tailwind.js 同为本地化 vendor）
(function () {
  if (window.renderMarkdown) return;

  var esc = function (s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  };

  var inline = function (s) {
    return esc(s)
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`]+)`/g, '<code>$1</code>');
  };

  window.renderMarkdown = function (src) {
    if (!src) return '';
    var html = '';
    var inList = false;
    var lines = src.replace(/\r\n?/g, '\n').split('\n');
    var closeList = function () { if (inList) { html += '</ul>'; inList = false; } };
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].replace(/\s+$/, '');
      var h = /^(#{1,3})\s+(.*)$/.exec(line);
      if (h) { closeList(); var lv = h[1].length; html += '<h' + lv + '>' + inline(h[2]) + '</h' + lv + '>'; continue; }
      var li = /^[-*]\s+(.*)$/.exec(line);
      if (li) { if (!inList) { html += '<ul>'; inList = true; } html += '<li>' + inline(li[1]) + '</li>'; continue; }
      if (!line.trim()) { closeList(); continue; }
      closeList();
      html += '<p>' + inline(line) + '</p>';
    }
    closeList();
    return html;
  };

  // 注入日志弹窗样式（标题/列表/加粗在 text-xs 容器内保持协调）
  var style = document.createElement('style');
  style.textContent =
    '.md-body h1,.md-body h2,.md-body h3{font-weight:600;line-height:1.4;margin:.6em 0 .3em}' +
    '.md-body h1{font-size:1.15em}.md-body h2{font-size:1.05em}.md-body h3{font-size:1em}' +
    '.md-body p{margin:.25em 0}' +
    '.md-body ul{list-style:disc;padding-left:1.4em;margin:.25em 0}' +
    '.md-body li{margin:.15em 0}' +
    '.md-body strong{font-weight:600}' +
    '.md-body code{background:rgba(127,127,127,.12);border-radius:4px;padding:0 .3em;font-size:.95em}';
  document.head.appendChild(style);
})();
