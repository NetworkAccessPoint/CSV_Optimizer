/* Virtual-scrolling data grid: renders a window of rows over an arbitrarily
   large table, keeps a small page cache, and handles selection and editing.

   Row counts beyond what a browser can express as pixel height (Chrome caps an
   element around 33M px, Firefox lower) are handled by scaling scrollTop onto
   the row range instead of mapping 1:1. */
(function (global) {
  'use strict';

  const CHUNK = 400;          // rows fetched per request
  const MAX_CANVAS_PX = 12e6; // stay well inside every browser's element limit
  const NUM_W = 78;

  class Grid {
    constructor(opts) {
      this.head = opts.head;
      this.viewport = opts.viewport;
      this.canvas = opts.canvas;
      this.root = opts.root;
      this.fetchRows = opts.fetchRows;
      this.onSetCells = opts.onSetCells || (() => {});
      this.onContext = opts.onContext || (() => {});
      this.onSelChange = opts.onSelChange || (() => {});
      this.onHeaderContext = opts.onHeaderContext || (() => {});
      this.onSort = opts.onSort || (() => {});

      this.columns = [];
      this.total = 0;
      this.rowH = 24;
      this.wrap = false;
      this.cache = new Map();
      this.pending = new Map();
      this.pool = [];
      this.first = 0;
      this.cursor = { r: 0, c: 0 };
      this.anchor = { r: 0, c: 0 };
      this.marks = new Set();       // row ids
      this.hits = new Set();        // "rid:col" keys highlighted by find
      this.levelCol = -1;
      this.editing = null;
      this.dragging = false;

      this.viewport.addEventListener('scroll', () => this.onScroll());
      this.viewport.addEventListener('mousedown', (e) => this.onMouseDown(e));
      this.viewport.addEventListener('mousemove', (e) => this.onMouseMove(e));
      window.addEventListener('mouseup', () => { this.dragging = false; });
      this.viewport.addEventListener('dblclick', (e) => {
        const hit = this.cellFromEvent(e);
        if (hit) this.beginEdit(hit.r, hit.c);
      });
      this.viewport.addEventListener('contextmenu', (e) => {
        const hit = this.cellFromEvent(e);
        if (!hit) return;
        e.preventDefault();
        if (!this.inSelection(hit.r, hit.c)) this.setCursor(hit.r, hit.c, false);
        this.onContext(e, hit);
      });
      new ResizeObserver(() => this.render()).observe(this.viewport);
    }

    /* ------------------------------------------------------------ schema */

    setColumns(columns, keepWidths) {
      const prev = new Map(this.columns.map((c) => [c.id, c]));
      if (!keepWidths) this.autoFitted = false;
      this.columns = columns.map((c) => {
        const old = keepWidths ? prev.get(c.id) : null;
        return {
          id: c.id, name: c.name, src: c.src,
          width: old ? old.width : Math.max(80, Math.min(260, c.name.length * 9 + 40)),
          hidden: old ? old.hidden : false,
          pinned: old ? old.pinned : false,
          sort: old ? old.sort : 0,
        };
      });
      this.detectLevelColumn();
      this.renderHeader();
    }

    detectLevelColumn() {
      this.levelCol = this.columns.findIndex((c) =>
        /^(level|lvl|severity|loglevel|priority)$/i.test(c.name));
    }

    visibleColumns() { return this.columns.filter((c) => !c.hidden); }

    setTotal(n) {
      this.total = n;
      if (this.cursor.r >= n) this.cursor.r = Math.max(0, n - 1);
      this.layout();
    }

    invalidate() { this.cache.clear(); this.pending.clear(); this.render(); }

    setRowHeight(px, wrap) {
      this.rowH = px;
      this.wrap = !!wrap;
      document.body.classList.toggle('wrap', this.wrap);
      this.root.style.setProperty('--row-h', px + 'px');
      this.layout();
    }

    /* ------------------------------------------------------------ layout */

    layout() {
      const totalPx = this.total * this.rowH;
      this.scaled = totalPx > MAX_CANVAS_PX;
      this.canvasH = Math.max(this.rowH, Math.min(totalPx, MAX_CANVAS_PX));
      this.canvas.style.height = this.canvasH + 'px';
      this.canvas.style.width = this.rowWidth() + 'px';
      this.render();
    }

    rowWidth() {
      return this.visibleColumns().reduce((w, c) => w + c.width, NUM_W) + 2;
    }

    visibleCount() {
      return Math.max(1, Math.ceil(this.viewport.clientHeight / this.rowH) + 2);
    }

    maxFirst() { return Math.max(0, this.total - this.visibleCount() + 2); }

    firstFromScroll() {
      const st = this.viewport.scrollTop;
      if (!this.scaled) return Math.max(0, Math.floor(st / this.rowH));
      const range = Math.max(1, this.canvasH - this.viewport.clientHeight);
      return Math.round((st / range) * this.maxFirst());
    }

    scrollToRow(r, position) {
      r = Math.max(0, Math.min(r, this.total - 1));
      const vis = this.visibleCount() - 2;
      let target = r;
      if (position === 'center') target = r - Math.floor(vis / 2);
      else if (r >= this.first && r < this.first + vis) return;  // already shown
      else if (r >= this.first + vis) target = r - vis + 1;
      target = Math.max(0, Math.min(target, this.maxFirst()));
      if (!this.scaled) this.viewport.scrollTop = target * this.rowH;
      else {
        const range = Math.max(1, this.canvasH - this.viewport.clientHeight);
        this.viewport.scrollTop = (target / Math.max(1, this.maxFirst())) * range;
      }
      this.render();
    }

    onScroll() {
      this.head.scrollLeft = this.viewport.scrollLeft;
      this.render();
    }

    /* -------------------------------------------------------------- data */

    chunkOf(r) { return Math.floor(r / CHUNK); }

    rowAt(r) {
      const chunk = this.cache.get(this.chunkOf(r));
      if (!chunk) return null;
      const i = r - chunk.start;
      if (i < 0 || i >= chunk.rows.length) return null;
      return { cells: chunk.rows[i], id: chunk.ids[i], edited: chunk.edited[String(chunk.ids[i])] };
    }

    ensure(from, to) {
      for (let c = this.chunkOf(from); c <= this.chunkOf(to); c++) {
        if (this.cache.has(c) || this.pending.has(c)) continue;
        const start = c * CHUNK;
        const p = this.fetchRows(start, CHUNK).then((data) => {
          this.cache.set(c, {
            start: start, rows: data.rows, ids: data.ids, edited: data.edited || {},
          });
          if (!this.autoFitted && data.rows.length) {
            this.autoFitted = true;
            this.autoFitAll(data.rows);
          }
          if (this.cache.size > 60) {
            const keep = new Set();
            for (let k = this.chunkOf(this.first) - 2; k <= this.chunkOf(this.first + this.visibleCount()) + 2; k++) keep.add(k);
            for (const key of this.cache.keys()) if (!keep.has(key) && this.cache.size > 30) this.cache.delete(key);
          }
          this.pending.delete(c);
          this.render();
        }).catch(() => { this.pending.delete(c); });
        this.pending.set(c, p);
      }
    }

    async rowsFor(from, to) {
      const out = [];
      this.ensure(from, to);
      await Promise.all([...this.pending.values()]);
      for (let r = from; r <= to; r++) out.push(this.rowAt(r));
      return out;
    }

    /* ------------------------------------------------------------ render */

    renderHeader() {
      const cols = this.visibleColumns();
      this.head.innerHTML = '';
      const num = document.createElement('div');
      num.className = 'hcell rownum';
      num.style.width = NUM_W + 'px';
      num.textContent = '#';
      this.head.appendChild(num);
      cols.forEach((col) => {
        const el = document.createElement('div');
        el.className = 'hcell' + (col.pinned ? ' pinned' : '');
        el.style.width = col.width + 'px';
        el.dataset.cid = col.id;
        el.innerHTML = '<span class="nm"></span><span class="sort"></span>';
        el.querySelector('.nm').textContent = col.name;
        el.querySelector('.sort').textContent = col.sort === 1 ? '▲' : col.sort === -1 ? '▼' : '';
        el.title = col.name + ' — 클릭: 정렬, 우클릭: 메뉴';
        el.addEventListener('click', (e) => {
          if (e.target.classList.contains('grip')) return;
          const dir = col.sort === -1 ? 1 : -1;
          this.columns.forEach((c) => { c.sort = 0; });
          col.sort = dir;
          this.renderHeader();
          this.onSort(col, dir === -1);
        });
        el.addEventListener('contextmenu', (e) => {
          e.preventDefault();
          this.onHeaderContext(e, col);
        });
        const grip = document.createElement('div');
        grip.className = 'grip';
        grip.addEventListener('mousedown', (e) => this.startResize(e, col));
        grip.addEventListener('dblclick', (e) => { e.stopPropagation(); this.autoFit(col); });
        el.appendChild(grip);
        this.head.appendChild(el);
      });
      this.layout();
    }

    startResize(e, col) {
      e.preventDefault();
      e.stopPropagation();
      const x0 = e.clientX;
      const w0 = col.width;
      const move = (ev) => {
        col.width = Math.max(40, w0 + ev.clientX - x0);
        this.renderHeader();
        this.render();
      };
      const up = () => {
        window.removeEventListener('mousemove', move);
        window.removeEventListener('mouseup', up);
      };
      window.addEventListener('mousemove', move);
      window.addEventListener('mouseup', up);
    }

    /* Size every column from the first page of data, the way a person would
       drag the handles on opening a file. */
    autoFitAll(rows) {
      const sample = rows.slice(0, 200);
      this.columns.forEach((col, ci) => {
        let max = col.name.length + 2;
        for (const row of sample) {
          const value = row[ci];
          if (value != null && value.length > max) max = value.length;
        }
        col.width = Math.max(60, Math.min(460, Math.round(max * 7.25 + 22)));
      });
      this.renderHeader();
    }

    autoFit(col) {
      const idx = this.visibleColumns().indexOf(col);
      let max = col.name.length;
      for (let r = this.first; r < this.first + this.visibleCount(); r++) {
        const row = this.rowAt(r);
        if (row && row.cells[this.columns.indexOf(col)] != null) {
          max = Math.max(max, String(row.cells[this.columns.indexOf(col)]).length);
        }
      }
      col.width = Math.max(60, Math.min(600, max * 7.3 + 24));
      this.renderHeader();
    }

    render() {
      if (!this.columns.length) return;
      const first = this.firstFromScroll();
      this.first = Math.max(0, Math.min(first, this.maxFirst()));
      const count = Math.min(this.visibleCount(), Math.max(0, this.total - this.first));
      this.ensure(this.first, this.first + count);

      while (this.pool.length < count) {
        const el = document.createElement('div');
        el.className = 'row';
        this.canvas.appendChild(el);
        this.pool.push(el);
      }
      while (this.pool.length > count) this.canvas.removeChild(this.pool.pop());

      const cols = this.visibleColumns();
      const colIdx = cols.map((c) => this.columns.indexOf(c));
      const st = this.viewport.scrollTop;
      const sel = this.selectionBox();

      for (let i = 0; i < count; i++) {
        const r = this.first + i;
        const el = this.pool[i];
        const data = this.rowAt(r);
        const top = this.scaled ? st + i * this.rowH : r * this.rowH;
        el.style.top = top + 'px';
        el.style.width = this.rowWidth() + 'px';
        let cls = 'row' + (r % 2 ? ' alt' : '');
        if (data) {
          if (data.id < 0) cls += ' newrow';
          if (this.marks.has(data.id)) cls += ' marked';
          if (this.levelCol >= 0 && data.cells[this.levelCol]) {
            const lvl = String(data.cells[this.levelCol]).trim().toLowerCase();
            if (/^(error|err|critical|crit)$/.test(lvl)) cls += ' lvl-error';
            else if (/^(fatal|panic|emerg)$/.test(lvl)) cls += ' lvl-fatal';
            else if (/^(warn|warning)$/.test(lvl)) cls += ' lvl-warn';
            else if (/^(debug|dbg)$/.test(lvl)) cls += ' lvl-debug';
            else if (/^(trace|verbose)$/.test(lvl)) cls += ' lvl-trace';
          }
        }
        el.className = cls;

        if (el.childElementCount !== cols.length + 1) {
          el.innerHTML = '';
          const numCell = document.createElement('div');
          numCell.className = 'cell rownum';
          el.appendChild(numCell);
          cols.forEach(() => el.appendChild(document.createElement('div')));
        }
        const numCell = el.firstChild;
        numCell.style.width = NUM_W + 'px';
        numCell.textContent = data && data.id < 0 ? '+ ' + (r + 1) : (r + 1);
        for (let k = 0; k < cols.length; k++) {
          const cell = el.childNodes[k + 1];
          const ci = colIdx[k];
          const value = data ? (data.cells[ci] != null ? data.cells[ci] : '') : '';
          cell.textContent = data ? value : '⋯';
          cell.style.width = cols[k].width + 'px';
          let cc = 'cell' + (cols[k].pinned ? ' pinned' : '');
          if (data && data.edited && data.edited.indexOf(cols[k].id) >= 0) cc += ' edited';
          if (sel && r >= sel.r0 && r <= sel.r1 && ci >= sel.c0 && ci <= sel.c1) cc += ' sel';
          if (r === this.cursor.r && ci === this.cursor.c) cc += ' cursor';
          if (data && this.hits.has(data.id + ':' + ci)) cc += ' hit';
          cell.className = cc;
          cell.dataset.r = r;
          cell.dataset.c = ci;
        }
      }
      if (this.editing) this.positionEditor();
    }

    /* --------------------------------------------------------- selection */

    selectionBox() {
      return {
        r0: Math.min(this.cursor.r, this.anchor.r), r1: Math.max(this.cursor.r, this.anchor.r),
        c0: Math.min(this.cursor.c, this.anchor.c), c1: Math.max(this.cursor.c, this.anchor.c),
      };
    }

    inSelection(r, c) {
      const b = this.selectionBox();
      return r >= b.r0 && r <= b.r1 && c >= b.c0 && c <= b.c1;
    }

    cellFromEvent(e) {
      const cell = e.target.closest ? e.target.closest('.cell') : null;
      if (!cell || cell.dataset.r === undefined) return null;
      return { r: +cell.dataset.r, c: +cell.dataset.c };
    }

    onMouseDown(e) {
      if (e.button !== 0) return;
      const hit = this.cellFromEvent(e);
      if (!hit) return;
      this.commitEdit();
      this.setCursor(hit.r, hit.c, e.shiftKey);
      this.dragging = true;
      this.root.focus();
    }

    onMouseMove(e) {
      if (!this.dragging) return;
      const hit = this.cellFromEvent(e);
      if (hit) this.setCursor(hit.r, hit.c, true);
    }

    setCursor(r, c, extend) {
      r = Math.max(0, Math.min(r, this.total - 1));
      const maxC = this.columns.length - 1;
      c = Math.max(0, Math.min(c, maxC));
      this.cursor = { r: r, c: c };
      if (!extend) this.anchor = { r: r, c: c };
      this.scrollToRow(r);
      this.render();
      this.onSelChange(this.selectionBox());
    }

    moveCursor(dr, dc, extend) {
      this.setCursor(this.cursor.r + dr, this.cursor.c + dc, extend);
    }

    async selectedCells() {
      const b = this.selectionBox();
      const rows = await this.rowsFor(b.r0, b.r1);
      return { box: b, rows: rows };
    }

    selectedRowIds() {
      const b = this.selectionBox();
      const ids = [];
      for (let r = b.r0; r <= b.r1; r++) {
        const row = this.rowAt(r);
        if (row) ids.push(row.id);
      }
      return ids;
    }

    /* ----------------------------------------------------------- editing */

    beginEdit(r, c, initial) {
      const row = this.rowAt(r);
      if (!row) return;
      this.commitEdit();
      const input = document.createElement('input');
      input.className = 'editor';
      input.value = initial != null ? initial : (row.cells[c] != null ? row.cells[c] : '');
      this.canvas.appendChild(input);
      this.editing = { r: r, c: c, input: input, id: row.id, before: row.cells[c] };
      this.positionEditor();
      input.focus();
      if (initial == null) input.select();
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { e.stopPropagation(); this.cancelEdit(); this.root.focus(); }
        else if (e.key === 'Enter') { e.stopPropagation(); e.preventDefault(); this.commitEdit(); this.root.focus(); this.moveCursor(1, 0, false); }
        else if (e.key === 'Tab') { e.stopPropagation(); e.preventDefault(); this.commitEdit(); this.root.focus(); this.moveCursor(0, e.shiftKey ? -1 : 1, false); }
        else e.stopPropagation();
      });
      input.addEventListener('blur', () => this.commitEdit());
    }

    positionEditor() {
      if (!this.editing) return;
      const cols = this.visibleColumns();
      const idx = cols.indexOf(this.columns[this.editing.c]);
      if (idx < 0) return this.cancelEdit();
      let left = NUM_W;
      for (let i = 0; i < idx; i++) left += cols[i].width;
      const i = this.editing.r - this.first;
      const top = this.scaled ? this.viewport.scrollTop + i * this.rowH : this.editing.r * this.rowH;
      const el = this.editing.input;
      el.style.left = left + 'px';
      el.style.top = top + 'px';
      el.style.width = cols[idx].width + 'px';
      el.style.height = this.rowH + 'px';
    }

    commitEdit() {
      if (!this.editing) return;
      const ed = this.editing;
      this.editing = null;
      const value = ed.input.value;
      ed.input.remove();
      if (value !== ed.before) {
        this.onSetCells([{ rid: ed.id, cid: this.columns[ed.c].id, value: value }]);
      }
    }

    cancelEdit() {
      if (!this.editing) return;
      this.editing.input.remove();
      this.editing = null;
    }

    toggleMark(id) {
      if (this.marks.has(id)) this.marks.delete(id); else this.marks.add(id);
      this.render();
    }
  }

  global.Grid = Grid;
})(window);
