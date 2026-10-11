/* An editable, Excel-shaped grid for the multi searcher's conditions.

   The conditions are a table in the user's head -- it arrives as a table from
   Excel and gets checked row by row against the log -- so it is edited as a
   table here too, rather than as pasted text.  Each column carries its own
   mapping right in the header: which log column it is compared against and
   how.  That is the part a user has to be able to see and correct, so it sits
   on the column itself instead of in a separate panel. */
(function () {
  'use strict';

  const KINDS = [
    ['ip', 'IP / CIDR'], ['ports', '포트 목록'], ['equals', '같음'],
    ['contains', '포함'], ['regex', '정규식'], ['number', '숫자'],
    ['ignore', '조건 아님'],
  ];
  const MIN_ROWS = 8;
  const MIN_COLS = 4;

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };

  function splitPaste(text) {
    const lines = text.replace(/\r\n/g, '\n').replace(/\s+$/, '').split('\n');
    const tab = lines.some((l) => l.indexOf('\t') >= 0);
    return lines.map((line) => (tab ? line.split('\t') : splitCsv(line)));
  }

  /* Excel quotes a cell that holds the separator, so a naive split loses
     "22, 23" -- the exact shape a port list arrives in. */
  function splitCsv(line) {
    const out = [];
    let cell = '';
    let quoted = false;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i];
      if (quoted) {
        if (ch === '"' && line[i + 1] === '"') { cell += '"'; i++; }
        else if (ch === '"') quoted = false;
        else cell += ch;
      } else if (ch === '"') quoted = true;
      else if (ch === ',') { out.push(cell); cell = ''; }
      else cell += ch;
    }
    out.push(cell);
    return out;
  }

  class RuleSheet {
    constructor(options) {
      this.root = options.root;
      this.onChange = options.onChange || (() => {});
      this.onPasteText = options.onPasteText || null;
      this.onPickRow = options.onPickRow || null;
      this.columns = [];                 // log columns, for the mapping select
      this.header = [];
      this.rows = [];
      this.mapping = [];
      this.counts = null;                // hits per rule row, after a run
      this.picked = null;
      this.focus = { r: 0, c: 0 };
      this.root.classList.add('sheet');
      this.root.addEventListener('paste', (e) => this.onPaste(e));
      this.reset();
    }

    /* ------------------------------------------------------------ state */

    reset(cols = MIN_COLS, rows = MIN_ROWS) {
      this.header = new Array(cols).fill('');
      this.mapping = new Array(cols).fill(null).map(() => ({ column: null, kind: 'equals' }));
      this.rows = [];
      for (let r = 0; r < rows; r++) this.rows.push(new Array(cols).fill(''));
      this.counts = null;
      this.picked = null;
      this.render();
    }

    setColumns(columns) {
      this.columns = columns || [];
      this.render();
    }

    load(header, rows, mapping) {
      const width = Math.max(
        header.length, mapping.length, MIN_COLS,
        ...rows.map((r) => r.length));
      this.header = pad(header, width, '');
      this.mapping = pad(mapping, width, null).map(
        (m) => (m ? { column: m.column, kind: m.kind } : { column: null, kind: 'equals' }));
      this.rows = rows.map((r) => pad(r, width, ''));
      while (this.rows.length < MIN_ROWS) this.rows.push(new Array(width).fill(''));
      this.counts = null;
      this.picked = null;
      this.render();
      this.onChange();
    }

    /* Rows with something in them, plus where each sits in the sheet, so a
       result can be written back onto the right line. */
    filled() {
      const out = [];
      this.rows.forEach((row, index) => {
        const live = row.some((cell, i) =>
          String(cell).trim() && this.mapping[i] && this.mapping[i].kind !== 'ignore');
        if (live) out.push({ index: index, cells: row.map((c) => String(c)) });
      });
      return out;
    }

    isEmpty() {
      return !this.rows.some((row) => row.some((cell) => String(cell).trim()))
        && !this.header.some((name) => String(name).trim());
    }

    asText() {
      const body = this.rows
        .filter((row) => row.some((cell) => String(cell).trim()))
        .map((row) => row.join('\t'));
      const head = this.header.some((h) => String(h).trim())
        ? [this.header.join('\t')] : [];
      return head.concat(body).join('\n');
    }

    setCounts(counts) {
      this.counts = counts;      // {sheet row index: hits}
      this.render();
    }

    setPicked(index) {
      this.picked = index;
      this.render();
    }

    /* ------------------------------------------------------------ edits */

    addRow(at) {
      const width = this.header.length;
      this.rows.splice(at == null ? this.rows.length : at, 0, new Array(width).fill(''));
      this.counts = null;
      this.render();
      this.onChange();
    }

    deleteRow(index) {
      if (this.rows.length <= 1) return;
      this.rows.splice(index, 1);
      this.counts = null;
      this.render();
      this.onChange();
    }

    addColumn() {
      this.header.push('');
      this.mapping.push({ column: null, kind: 'equals' });
      this.rows.forEach((row) => row.push(''));
      this.render();
      this.onChange();
    }

    deleteColumn(index) {
      if (this.header.length <= 1) return;
      this.header.splice(index, 1);
      this.mapping.splice(index, 1);
      this.rows.forEach((row) => row.splice(index, 1));
      this.counts = null;
      this.render();
      this.onChange();
    }

    grow(rows, cols) {
      while (this.header.length < cols) {
        this.header.push('');
        this.mapping.push({ column: null, kind: 'equals' });
        this.rows.forEach((row) => row.push(''));
      }
      while (this.rows.length < rows) this.rows.push(new Array(this.header.length).fill(''));
    }

    onPaste(event) {
      const text = (event.clipboardData || window.clipboardData).getData('text');
      if (!text || text.indexOf('\n') < 0 && text.indexOf('\t') < 0) return;  // one cell
      event.preventDefault();
      // An empty sheet takes the paste whole, so the header row and the column
      // mapping can be worked out from it; otherwise it lands where the cursor is.
      if (this.isEmpty() && this.onPasteText) {
        this.onPasteText(text);
        return;
      }
      const block = splitPaste(text);
      const { r, c } = this.focus;
      this.grow(r + block.length, c + Math.max(...block.map((line) => line.length)));
      block.forEach((line, i) => {
        line.forEach((cell, j) => { this.rows[r + i][c + j] = cell; });
      });
      this.counts = null;
      this.render();
      this.focusCell(r + block.length - 1, c);
      this.onChange();
    }

    focusCell(r, c) {
      const cell = this.root.querySelector(`.sc[data-r="${r}"][data-c="${c}"] input`);
      if (cell) { cell.focus(); cell.select(); }
    }

    move(r, c) {
      if (r >= this.rows.length) this.addRow();
      const row = Math.max(0, Math.min(r, this.rows.length - 1));
      const col = Math.max(0, Math.min(c, this.header.length - 1));
      this.focusCell(row, col);
    }

    /* ----------------------------------------------------------- render */

    render() {
      const width = this.header.length;
      const template = `34px repeat(${width}, minmax(132px, 1fr)) 84px`;
      this.root.innerHTML = '';

      const head = el('div', 'sheet-head');
      head.style.gridTemplateColumns = template;
      head.appendChild(el('div', 'sh corner'));
      this.header.forEach((name, c) => head.appendChild(this.headerCell(name, c)));
      const hits = el('div', 'sh hits', '건수');
      hits.title = '검색 후 이 조건에 맞은 로그 행 수';
      head.appendChild(hits);
      this.root.appendChild(head);

      const body = el('div', 'sheet-body');
      this.rows.forEach((row, r) => {
        const line = el('div', 'sr');
        line.style.gridTemplateColumns = template;
        if (this.picked === r) line.classList.add('picked');
        const num = el('div', 'rn', String(r + 1));
        num.title = '이 줄 지우기';
        num.onclick = () => this.deleteRow(r);
        line.appendChild(num);
        row.forEach((value, c) => line.appendChild(this.cell(value, r, c)));
        line.appendChild(this.hitCell(r));
        body.appendChild(line);
      });
      this.root.appendChild(body);

      const foot = el('div', 'sheet-foot');
      const addRow = el('button', 'btn tiny ghost', '+ 줄');
      addRow.onclick = () => this.addRow();
      const addCol = el('button', 'btn tiny ghost', '+ 열');
      addCol.onclick = () => this.addColumn();
      foot.appendChild(addRow);
      foot.appendChild(addCol);
      this.root.appendChild(foot);
    }

    headerCell(name, c) {
      const spec = this.mapping[c] || (this.mapping[c] = { column: null, kind: 'equals' });
      const cell = el('div', 'sh col');
      if (spec.kind !== 'ignore' && spec.column == null) cell.classList.add('unmapped');

      const label = el('input', 'name');
      label.value = name;
      label.placeholder = '열 이름';
      label.oninput = () => { this.header[c] = label.value; };
      label.onchange = () => this.onChange();

      const arrow = el('span', 'to', '→');
      const map = el('select', 'map');
      map.appendChild(new Option('연결 안 됨', ''));
      this.columns.forEach((col) => map.appendChild(new Option(col.name, col.id)));
      map.value = spec.column == null ? '' : spec.column;
      map.title = '이 조건을 어느 로그 열과 비교할지';
      map.onchange = () => {
        spec.column = map.value === '' ? null : +map.value;
        if (spec.column != null && spec.kind === 'ignore') spec.kind = 'equals';
        this.counts = null;
        this.render();
        this.onChange();
      };

      const kind = el('select', 'kind');
      KINDS.forEach(([v, text]) => kind.appendChild(new Option(text, v)));
      kind.value = spec.kind || 'equals';
      kind.title = '비교 방식';
      kind.onchange = () => {
        spec.kind = kind.value;
        this.counts = null;
        this.render();
        this.onChange();
      };

      const drop = el('button', 'x', '×');
      drop.title = '이 열 지우기';
      drop.onclick = () => this.deleteColumn(c);

      const top = el('div', 'top');
      top.appendChild(label);
      top.appendChild(drop);
      const bottom = el('div', 'bottom');
      bottom.appendChild(arrow);
      bottom.appendChild(map);
      bottom.appendChild(kind);
      cell.appendChild(top);
      cell.appendChild(bottom);
      return cell;
    }

    cell(value, r, c) {
      const box = el('div', 'sc');
      box.dataset.r = r;
      box.dataset.c = c;
      const input = el('input');
      input.value = value;
      input.spellcheck = false;
      input.oninput = () => {
        this.rows[r][c] = input.value;
        if (this.counts) { this.counts = null; this.repaintHits(); }
      };
      input.onchange = () => this.onChange();
      input.onfocus = () => { this.focus = { r: r, c: c }; };
      input.onkeydown = (e) => this.onKey(e, r, c, input);
      box.appendChild(input);
      return box;
    }

    onKey(event, r, c, input) {
      const at = input.selectionStart;
      const end = input.value.length;
      if (event.key === 'Enter') { event.preventDefault(); this.move(e_shift(event) ? r - 1 : r + 1, c); }
      else if (event.key === 'ArrowDown') { event.preventDefault(); this.move(r + 1, c); }
      else if (event.key === 'ArrowUp') { event.preventDefault(); this.move(r - 1, c); }
      else if (event.key === 'ArrowLeft' && at === 0) { event.preventDefault(); this.move(r, c - 1); }
      else if (event.key === 'ArrowRight' && at === end) { event.preventDefault(); this.move(r, c + 1); }
      else if (event.key === 'Delete' && e_shift(event)) { event.preventDefault(); this.deleteRow(r); }
    }

    hitCell(r) {
      const hits = this.counts ? this.counts[r] : undefined;
      const box = el('div', 'hit');
      if (hits == null) {
        box.textContent = '';
        return box;
      }
      box.textContent = hits.toLocaleString();
      box.classList.add(hits ? 'some' : 'none');
      box.title = hits
        ? '클릭하면 이 조건에 맞는 행만 봅니다'
        : '이 조건에 맞는 로그가 없습니다';
      if (hits && this.onPickRow) box.onclick = () => this.onPickRow(r);
      return box;
    }

    repaintHits() {
      this.root.querySelectorAll('.hit').forEach((node) => {
        node.textContent = '';
        node.className = 'hit';
        node.onclick = null;
      });
    }
  }

  function e_shift(event) { return event.shiftKey; }

  function pad(list, width, filler) {
    const out = (list || []).slice(0, width);
    while (out.length < width) out.push(filler);
    return out;
  }

  window.RuleSheet = RuleSheet;
  window.RuleSheet.KINDS = KINDS;
})();
