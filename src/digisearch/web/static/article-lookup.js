/* Article-number typeahead for the part-number field on the Add-component and New-assembly forms.
 *
 * Queries GET /article-register/api/unassigned for live internal numbers that have no catalog
 * part/assembly yet, and lets the user pick one to fill the field. Progressive enhancement: the
 * input stays a plain free-text field, so anything can still be typed by hand.
 *
 * Usage:  attachArticleLookup({ input: 'part_no', prefix: '98', descTarget: 'value' })
 *   input      – id of the text input to attach to (required)
 *   prefix     – optional 2-digit category to scope suggestions (e.g. '98' assemblies)
 *   descTarget – optional id of a field to fill with the article's product/description on pick,
 *                but only when that field is still empty
 */
function attachArticleLookup(opts) {
  const input = document.getElementById(opts.input);
  if (!input) return;
  const prefix = opts.prefix || '';
  const descTarget = opts.descTarget ? document.getElementById(opts.descTarget) : null;

  // Wrap the input so the menu can position against it.
  const wrap = document.createElement('div');
  wrap.className = 'ac-wrap';
  input.parentNode.insertBefore(wrap, input);
  wrap.appendChild(input);
  input.setAttribute('autocomplete', 'off');

  const menu = document.createElement('div');
  menu.className = 'ac-menu';
  menu.hidden = true;
  wrap.appendChild(menu);

  let items = [];       // current result rows
  let active = -1;      // highlighted index
  let seq = 0;          // guards against out-of-order responses
  let timer = null;

  function close() { menu.hidden = true; active = -1; }

  function pick(row) {
    input.value = row.code;
    if (descTarget && !descTarget.value.trim() && row.product) descTarget.value = row.product;
    close();
    input.focus();
  }

  function render() {
    if (!items.length) {
      menu.innerHTML = '<div class="ac-empty">No unassigned article numbers match.</div>';
      menu.hidden = false;
      return;
    }
    menu.innerHTML = '';
    items.forEach((row, i) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ac-item' + (i === active ? ' active' : '');
      const tag = row.prefix_label ? '<span class="ac-tag">' + esc(row.prefix_label) + '</span>' : '';
      const desc = row.product ? '<span class="ac-desc">' + esc(row.product) + '</span>' : '';
      b.innerHTML = '<span class="ac-code">' + esc(row.code) + '</span>' + tag + desc;
      b.addEventListener('mousedown', (e) => { e.preventDefault(); pick(row); });
      menu.appendChild(b);
    });
    menu.hidden = false;
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function search() {
    const q = input.value.trim();
    const mine = ++seq;
    const url = '/article-register/api/unassigned?q=' + encodeURIComponent(q) +
                (prefix ? '&prefix=' + encodeURIComponent(prefix) : '');
    fetch(url, { headers: { 'Accept': 'application/json' } })
      .then((r) => (r.ok ? r.json() : { results: [] }))
      .then((data) => {
        if (mine !== seq) return;   // a newer keystroke already fired
        items = data.results || [];
        active = -1;
        render();
      })
      .catch(() => {});
  }

  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(search, 180);
  });
  input.addEventListener('focus', () => { if (input.value.trim() || prefix) search(); });
  input.addEventListener('keydown', (e) => {
    if (menu.hidden) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); active = Math.min(active + 1, items.length - 1); render(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); active = Math.max(active - 1, 0); render(); }
    else if (e.key === 'Enter' && active >= 0) { e.preventDefault(); pick(items[active]); }
    else if (e.key === 'Escape') { close(); }
  });
  document.addEventListener('click', (e) => { if (!wrap.contains(e.target)) close(); });
}
