/* csvopt application wiring: toolbar, filters, side panels, dialogs, keyboard. */
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const MOD = API.MOD_LABEL;

  const state = {
    server: null,      // last /api/state payload
    filters: [],       // active conditions
    findHits: [],
    findAt: -1,
    activeJob: null,
  };

  let grid;

  /* ------------------------------------------------------------- helpers */

  function say(msg, isError) {
    const node = $('#st-msg');
    node.textContent = msg || '';
    node.className = isError ? 'err' : 'muted';
    if (msg) setTimeout(() => { if (node.textContent === msg) node.textContent = ''; }, 6000);
  }

  function fail(err) {
    console.error(err);
    say(err && err.message ? err.message : String(err), true);
  }

  async function runJob(name, payload, label) {
    $('#job').classList.remove('hidden');
    $('#job-label').textContent = label || name;
    try {
      const job = await API.job(name, payload, (info) => {
        state.activeJob = info.id;
        const pct = Math.round((info.ratio || 0) * 100);
        $('#job-bar').style.width = pct + '%';
      });
      return job;
    } finally {
      state.activeJob = null;
      $('#job').classList.add('hidden');
      $('#job-bar').style.width = '0%';
    }
  }

  function columnById(cid) {
    return (state.server.columns || []).find((c) => c.id === cid);
  }

  /* --------------------------------------------------------------- state */

  async function refreshState(data) {
    const st = data || (await API.call('state'));
    state.server = st;
    if (!st.open) {
      $('#empty').classList.remove('hidden');
      $('#side').classList.add('hidden');
      $('#grid').classList.add('hidden');
      return st;
    }
    $('#empty').classList.add('hidden');
    $('#side').classList.remove('hidden');
    $('#grid').classList.remove('hidden');
    $('#file-name').textContent = st.name + (st.dirty ? ' •' : '');
    $('#file-name').title = st.path;
    $('#file-sub').textContent =
      `${API.bytes(st.size)} · ${API.num(st.base_rows)}행 · ${st.dialect.encoding} · ` +
      `${st.dialect.delimiter === '\t' ? 'TAB' : st.dialect.delimiter} · ${st.dialect.newline.toUpperCase()}` +
      (st.edited_rows ? ` · 수정 ${API.num(st.edited_rows)}행` : '') +
      (st.deleted_rows ? ` · 삭제 ${API.num(st.deleted_rows)}행` : '');
    $('#btn-undo').disabled = !st.undo;
    $('#btn-redo').disabled = !st.redo;
    $('#btn-save').disabled = !st.dirty;
    $('#st-rows').textContent = `${API.num(st.rows)}행 / ${st.columns.length}열`;
    $('#st-view').textContent = st.view
      ? `필터 적용: ${API.num(st.view_rows)}행${st.view_label ? ' (' + st.view_label + ')' : ''}`
      : '';
    grid.setColumns(st.columns, true);
    grid.setTotal(st.rows);
    fillColumnSelects();
    renderColumnPanel();
    return st;
  }

  function fillColumnSelects() {
    const cols = state.server.columns || [];
    const fill = (sel, allLabel) => {
      const prev = sel.value;
      sel.innerHTML = '';
      if (allLabel) sel.appendChild(new Option(allLabel, ''));
      cols.forEach((c) => sel.appendChild(new Option(c.name, c.id)));
      if (prev) sel.value = prev;
    };
    fill($('#facet-col'), null);
    fill($('#find-col'), '모든 열');
  }

  /* -------------------------------------------------------------- filters */

  const OPS = [
    ['contains', '포함'], ['equals', '일치'], ['starts', '시작'], ['ends', '끝'],
    ['regex', '정규식'], ['gt', '>'], ['ge', '≥'], ['lt', '<'], ['le', '≤'],
    ['between', '범위(수치)'], ['time_between', '시간 범위'], ['in', '목록(,)'],
    ['empty', '비어 있음'], ['not_empty', '값 있음'],
  ];

  function renderChips() {
    const box = $('#chips');
    box.innerHTML = '';
    state.filters.forEach((f, i) => {
      const chip = el('span', 'chip');
      const col = f.col == null ? '전체' : (columnById(f.col) || {}).name || '?';
      const opName = (OPS.find((o) => o[0] === f.op) || [f.op, f.op])[1];
      chip.appendChild(el('b', null, col));
      chip.appendChild(el('span', null, `${f.negate ? '≠' : ''}${opName} ${f.value || ''}${f.value2 ? '~' + f.value2 : ''}`));
      const x = el('button', null, '×');
      x.onclick = () => { state.filters.splice(i, 1); applyFilters(); };
      chip.appendChild(x);
      chip.onclick = (e) => { if (e.target !== x) editFilter(i); };
      box.appendChild(chip);
    });
  }

  async function applyFilters(scopeView) {
    renderChips();
    const quick = $('#quick').value.trim();
    const payload = {
      conditions: state.filters,
      quick: quick,
      quick_regex: $('#quick-regex').checked,
      quick_case: $('#quick-case').checked,
      match_all: true,
      scope: (scopeView == null ? $('#scope-view').checked : scopeView) ? 'view' : 'all',
      label: [quick ? `"${quick}"` : '', ...state.filters.map((f) => {
        const col = f.col == null ? '전체' : (columnById(f.col) || {}).name;
        return `${col} ${f.op} ${f.value}`;
      })].filter(Boolean).join(' & '),
    };
    if (!payload.conditions.length && !quick && payload.scope === 'all') {
      await API.call('clear_view');
      grid.invalidate();
      return refreshState();
    }
    try {
      const job = await runJob('filter', payload, '필터 적용 중');
      if (job.status === 'cancelled') say('필터를 취소했습니다.');
      else say(`${API.num(job.result.rows)}행이 조건에 맞습니다. (${job.elapsed.toFixed(1)}초)`);
      grid.invalidate();
      grid.setCursor(0, grid.cursor.c, false);
      await refreshState();
    } catch (err) { fail(err); }
  }

  function editFilter(index) {
    const current = index >= 0 ? state.filters[index] : { col: null, op: 'contains', value: '', value2: '', negate: false, case_sensitive: false };
    const cols = state.server.columns || [];
    const body = el('div', 'content');
    const colSel = el('select');
    colSel.appendChild(new Option('모든 열', ''));
    cols.forEach((c) => colSel.appendChild(new Option(c.name, c.id)));
    colSel.value = current.col == null ? '' : current.col;
    const opSel = el('select');
    OPS.forEach(([v, label]) => opSel.appendChild(new Option(label, v)));
    opSel.value = current.op;
    const v1 = el('input'); v1.value = current.value || ''; v1.placeholder = '값';
    const v2 = el('input'); v2.value = current.value2 || ''; v2.placeholder = '두 번째 값 (범위)';
    const neg = el('input'); neg.type = 'checkbox'; neg.checked = !!current.negate;
    const cs = el('input'); cs.type = 'checkbox'; cs.checked = !!current.case_sensitive;

    const field = (label, node) => {
      const f = el('div', 'field');
      f.appendChild(el('label', null, label));
      f.appendChild(node);
      return f;
    };
    body.appendChild(field('열', colSel));
    body.appendChild(field('조건', opSel));
    body.appendChild(field('값', v1));
    body.appendChild(field('값 2 (범위/시간)', v2));
    const flags = el('div', 'row');
    const negWrap = el('label', 'chk'); negWrap.appendChild(neg); negWrap.appendChild(document.createTextNode(' 조건 반전(NOT)'));
    const csWrap = el('label', 'chk'); csWrap.appendChild(cs); csWrap.appendChild(document.createTextNode(' 대소문자 구분'));
    flags.appendChild(negWrap); flags.appendChild(csWrap);
    body.appendChild(flags);

    modal(index >= 0 ? '조건 편집' : '조건 추가', body, [
      { label: '취소' },
      { label: '적용', primary: true, action: () => {
        const cond = {
          col: colSel.value === '' ? null : +colSel.value,
          op: opSel.value, value: v1.value, value2: v2.value,
          negate: neg.checked, case_sensitive: cs.checked,
        };
        if (index >= 0) state.filters[index] = cond; else state.filters.push(cond);
        applyFilters(false);
      } },
    ]);
    setTimeout(() => v1.focus(), 30);
  }

  /* ---------------------------------------------------------------- modal */

  function modal(title, content, buttons) {
    const back = el('div', 'backdrop');
    const box = el('div', 'modal');
    box.appendChild(el('h2', null, title));
    content.classList.add('content');
    box.appendChild(content);
    const foot = el('div', 'foot');
    (buttons || []).forEach((b) => {
      const btn = el('button', 'btn' + (b.primary ? ' primary' : ''), b.label);
      btn.onclick = () => {
        if (!b.action) return close();
        const keep = b.action();
        if (keep !== 'keep') close();
      };
      foot.appendChild(btn);
    });
    box.appendChild(foot);
    back.appendChild(box);
    $('#modal-root').appendChild(back);
    const close = () => back.remove();
    back.addEventListener('mousedown', (e) => { if (e.target === back) close(); });
    document.addEventListener('keydown', function esc(e) {
      if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); }
    });
    return { close: close, box: box };
  }

  /* ------------------------------------------------------------ file open */

  async function openDialog() {
    const content = el('div');
    const pathInput = el('input');
    pathInput.placeholder = state.server && state.server.os === 'nt'
      ? 'C:\\logs\\app.csv' : '/Users/you/logs/app.csv';
    const roots = el('div', 'roots');
    const list = el('div', 'browser');
    const field = el('div', 'field');
    field.appendChild(el('label', null, '경로를 직접 입력하거나 아래에서 선택 (파일 탐색기에서 드래그해 붙여넣어도 됩니다)'));
    field.appendChild(pathInput);
    content.appendChild(field);
    content.appendChild(roots);
    content.appendChild(list);

    const dlg = modal('파일 열기', content, [
      { label: '취소' },
      { label: '열기', primary: true, action: () => { open(pathInput.value); } },
    ]);

    async function browse(path) {
      try {
        const data = await API.call('browse', { path: path });
        pathInput.value = data.path;
        roots.innerHTML = '';
        (data.roots || []).forEach((r) => {
          const b = el('button', 'btn tiny', r);
          b.onclick = () => browse(r);
          roots.appendChild(b);
        });
        list.innerHTML = '';
        if (data.parent) {
          const up = el('div', 'item');
          up.appendChild(el('span', null, '📁 ..'));
          up.onclick = () => browse(data.parent);
          list.appendChild(up);
        }
        data.entries.forEach((entry) => {
          const item = el('div', 'item');
          item.appendChild(el('span', null, (entry.dir ? '📁 ' : '📄 ') + entry.name));
          if (!entry.dir) item.appendChild(el('span', 'sz', API.bytes(entry.size)));
          item.onclick = () => { if (entry.dir) browse(entry.path); else { pathInput.value = entry.path; } };
          item.ondblclick = () => { if (!entry.dir) { dlg.close(); open(entry.path); } };
          list.appendChild(item);
        });
      } catch (err) { fail(err); }
    }

    async function open(path) {
      if (!path) return;
      try {
        const job = await runJob('open', { path: path }, '인덱스 생성 중');
        state.filters = [];
        $('#quick').value = '';
        renderChips();
        grid.autoFitted = false;
        grid.invalidate();
        grid.marks.clear();
        await refreshState();
        say(`${API.num(job.result.rows)}행을 열었습니다. (${job.elapsed.toFixed(1)}초)`);
      } catch (err) { fail(err); }
    }

    browse((state.server && state.server.path) || '');
  }

  /* ----------------------------------------------------------- side panel */

  function renderColumnPanel() {
    const box = $('#col-list');
    box.innerHTML = '';
    grid.columns.forEach((col, i) => {
      const row = el('div');
      const vis = el('input'); vis.type = 'checkbox'; vis.checked = !col.hidden;
      vis.title = '표시';
      vis.onchange = () => { col.hidden = !vis.checked; grid.renderHeader(); };
      const name = el('input'); name.type = 'text'; name.value = col.name;
      name.onchange = async () => {
        try {
          await API.call('column', { action: 'rename', cid: col.id, name: name.value });
          await refreshState();
        } catch (err) { fail(err); }
      };
      const pin = el('button', 'btn tiny' + (col.pinned ? ' primary' : ' ghost'), '고정');
      pin.onclick = () => { col.pinned = !col.pinned; grid.renderHeader(); renderColumnPanel(); };
      const up = el('button', 'btn tiny ghost', '↑');
      up.onclick = () => moveColumn(col.id, Math.max(0, i - 1));
      const down = el('button', 'btn tiny ghost', '↓');
      down.onclick = () => moveColumn(col.id, i + 1);
      const del = el('button', 'btn tiny ghost', '✕');
      del.title = '열 삭제';
      del.onclick = () => confirmDeleteColumn(col);
      [vis, name, pin, up, down, del].forEach((n) => row.appendChild(n));
      box.appendChild(row);
    });
  }

  async function moveColumn(cid, to) {
    try {
      await API.call('column', { action: 'move', cid: cid, to: to });
      grid.invalidate();
      await refreshState();
    } catch (err) { fail(err); }
  }

  function confirmDeleteColumn(col) {
    const body = el('div');
    body.appendChild(el('p', null, `'${col.name}' 열을 삭제할까요? 저장 전에는 실행 취소(${MOD}+Z)로 되돌릴 수 있습니다.`));
    modal('열 삭제', body, [
      { label: '취소' },
      { label: '삭제', primary: true, action: async () => {
        try {
          await API.call('column', { action: 'delete', cid: col.id });
          grid.invalidate();
          await refreshState();
        } catch (err) { fail(err); }
      } },
    ]);
  }

  async function runFacets() {
    const cid = +$('#facet-col').value;
    if (!cid) return;
    const out = $('#facet-out');
    out.textContent = '분석 중…';
    try {
      const job = await runJob('stats', { cid: cid, top: 30, scope: 'view' }, '열 분석 중');
      const s = job.result;
      out.innerHTML = '';
      const sum = el('div', 'summary');
      const add = (k, v) => { sum.appendChild(el('b', null, k)); sum.appendChild(el('span', null, v)); };
      add('행 수', API.num(s.total));
      add('고유값', API.num(s.distinct) + (s.truncated ? '+' : ''));
      add('빈 셀', API.num(s.empty));
      if (s.numeric_count) {
        add('숫자 셀', API.num(s.numeric_count));
        add('최소 / 최대', `${s.min} / ${s.max}`);
        add('평균', (s.mean != null ? s.mean.toFixed(3) : ''));
      }
      out.appendChild(sum);
      const table = el('table');
      const max = s.top.length ? s.top[0].count : 1;
      s.top.forEach((entry) => {
        const tr = el('tr');
        const td = el('td', 'v', entry.value === '' ? '(빈 값)' : entry.value);
        td.title = entry.value + ' — 클릭하면 이 값으로 필터';
        td.onclick = () => {
          state.filters.push({ col: cid, op: 'equals', value: entry.value, case_sensitive: true });
          applyFilters(false);
        };
        const tdn = el('td', 'n', API.num(entry.count));
        const bar = el('div', 'facet-bar');
        bar.style.width = Math.max(2, (entry.count / max) * 100) + '%';
        const tdb = el('td');
        tdb.appendChild(bar);
        tr.appendChild(td); tr.appendChild(tdn); tr.appendChild(tdb);
        table.appendChild(tr);
      });
      out.appendChild(table);
    } catch (err) { fail(err); out.textContent = ''; }
  }

  async function runFind() {
    const needle = $('#find-needle').value;
    if (!needle) return;
    try {
      const job = await runJob('find', {
        needle: needle,
        cid: $('#find-col').value ? +$('#find-col').value : null,
        regex: $('#find-regex').checked,
        case: $('#find-case').checked,
        whole: $('#find-whole').checked,
        scope: $('#find-scope').checked ? 'view' : 'all',
        limit: 20000,
      }, '검색 중');
      state.findHits = job.result.hits;
      state.findAt = -1;
      grid.hits = new Set(state.findHits.map((h) => h.rid + ':' + h.col));
      $('#find-out').textContent = state.findHits.length
        ? `${API.num(state.findHits.length)}개 셀에서 발견 (${MOD}+Enter로 다음)`
        : '결과가 없습니다.';
      grid.render();
      if (state.findHits.length) gotoHit(1);
    } catch (err) { fail(err); }
  }

  async function gotoHit(step) {
    if (!state.findHits.length) return;
    state.findAt = (state.findAt + step + state.findHits.length) % state.findHits.length;
    const hit = state.findHits[state.findAt];
    try {
      const pos = await API.call('position', { rid: hit.rid });
      if (pos.pos >= 0) {
        grid.setCursor(pos.pos, hit.col, false);
        grid.scrollToRow(pos.pos, 'center');
      }
      $('#find-out').textContent = `${state.findAt + 1} / ${state.findHits.length}`;
    } catch (err) { fail(err); }
  }

  async function replaceAll() {
    const needle = $('#find-needle').value;
    if (!needle) return;
    const body = el('div');
    body.appendChild(el('p', null,
      `'${needle}' → '${$('#find-repl').value}' 로 ${$('#find-scope').checked ? '현재 결과' : '파일 전체'}에서 모두 바꿉니다.`));
    body.appendChild(el('p', 'muted', `되돌리려면 ${MOD}+Z 를 누르세요. 저장 전까지는 원본이 바뀌지 않습니다.`));
    modal('모두 바꾸기', body, [
      { label: '취소' },
      { label: '바꾸기', primary: true, action: async () => {
        try {
          const job = await runJob('replace_all', {
            needle: needle,
            replacement: $('#find-repl').value,
            cid: $('#find-col').value ? +$('#find-col').value : null,
            regex: $('#find-regex').checked,
            case: $('#find-case').checked,
            whole: $('#find-whole').checked,
            scope: $('#find-scope').checked ? 'view' : 'all',
          }, '치환 중');
          grid.invalidate();
          await refreshState();
          say(`${API.num(job.result.changed)}개 셀을 바꿨습니다.`);
        } catch (err) { fail(err); }
      } },
    ]);
  }

  function renderMarks() {
    const box = $('#mark-list');
    box.innerHTML = '';
    if (!grid.marks.size) {
      box.appendChild(el('div', 'muted', '북마크가 없습니다.'));
      return;
    }
    [...grid.marks].forEach((rid) => {
      const row = el('div');
      const jump = el('span', 'jump', rid < 0 ? '새 행' : '행 ' + API.num(rid + 1));
      jump.onclick = async () => {
        const pos = await API.call('position', { rid: rid });
        if (pos.pos >= 0) { grid.setCursor(pos.pos, grid.cursor.c, false); grid.scrollToRow(pos.pos, 'center'); }
        else say('현재 필터 결과에 없는 행입니다.');
      };
      const del = el('button', 'btn tiny ghost', '✕');
      del.onclick = () => { grid.marks.delete(rid); renderMarks(); grid.render(); };
      row.appendChild(jump);
      row.appendChild(el('span', 'spacer'));
      row.appendChild(del);
      box.appendChild(row);
    });
  }

  /* --------------------------------------------------------- context menu */

  function menu(x, y, items) {
    const box = $('#ctx');
    box.innerHTML = '';
    items.forEach((item) => {
      if (item === '-') { box.appendChild(el('hr')); return; }
      const b = el('button');
      b.appendChild(el('span', null, item.label));
      if (item.key) b.appendChild(el('kbd', null, item.key));
      b.onclick = () => { hideMenu(); item.action(); };
      box.appendChild(b);
    });
    box.classList.remove('hidden');
    const rect = box.getBoundingClientRect();
    box.style.left = Math.min(x, window.innerWidth - rect.width - 8) + 'px';
    box.style.top = Math.min(y, window.innerHeight - rect.height - 8) + 'px';
  }
  const hideMenu = () => $('#ctx').classList.add('hidden');
  window.addEventListener('mousedown', (e) => { if (!e.target.closest('#ctx')) hideMenu(); });
  window.addEventListener('blur', hideMenu);

  function cellMenu(e, hit) {
    const ids = grid.selectedRowIds();
    const col = grid.columns[hit.c];
    menu(e.clientX, e.clientY, [
      { label: '복사', key: MOD + '+C', action: copySelection },
      { label: '붙여넣기', key: MOD + '+V', action: () => document.execCommand('paste') },
      { label: '아래로 채우기', key: MOD + '+D', action: fillDown },
      '-',
      { label: '이 값으로 필터', action: async () => {
        const row = grid.rowAt(hit.r);
        if (!row) return;
        state.filters.push({ col: col.id, op: 'equals', value: row.cells[hit.c], case_sensitive: true });
        applyFilters(false);
      } },
      { label: '이 값 제외', action: async () => {
        const row = grid.rowAt(hit.r);
        if (!row) return;
        state.filters.push({ col: col.id, op: 'equals', value: row.cells[hit.c], case_sensitive: true, negate: true });
        applyFilters(false);
      } },
      '-',
      { label: '위에 행 삽입', action: () => insertRow(hit.r) },
      { label: '아래에 행 삽입', action: () => insertRow(hit.r + 1) },
      { label: '선택한 행 복제', action: () => duplicateRows(ids) },
      { label: `선택한 ${ids.length}행 삭제`, key: MOD + '+-', action: () => deleteRows(ids) },
      '-',
      { label: '북마크 토글', key: 'M', action: () => { const row = grid.rowAt(hit.r); if (row) { grid.toggleMark(row.id); renderMarks(); } } },
    ]);
  }

  function headerMenu(e, col) {
    menu(e.clientX, e.clientY, [
      { label: '오름차순 정렬', action: () => sortBy(col, false) },
      { label: '내림차순 정렬', action: () => sortBy(col, true) },
      { label: '정렬 해제', action: () => { grid.columns.forEach((c) => { c.sort = 0; }); grid.renderHeader(); applyFilters(false); } },
      '-',
      { label: '이 열로 필터…', action: () => { state.filters.push({ col: col.id, op: 'contains', value: '' }); editFilter(state.filters.length - 1); } },
      { label: '값 분포 보기', action: () => { showTab('facets'); $('#facet-col').value = col.id; runFacets(); } },
      '-',
      { label: col.pinned ? '고정 해제' : '왼쪽에 고정', action: () => { col.pinned = !col.pinned; grid.renderHeader(); renderColumnPanel(); } },
      { label: '열 숨기기', action: () => { col.hidden = true; grid.renderHeader(); renderColumnPanel(); } },
      { label: '너비 자동 맞춤', action: () => grid.autoFit(col) },
      '-',
      { label: '이름 변경…', action: () => renameColumn(col) },
      { label: '왼쪽에 열 추가', action: () => addColumn(grid.columns.indexOf(col)) },
      { label: '열 삭제', action: () => confirmDeleteColumn(col) },
    ]);
  }

  function renameColumn(col) {
    const body = el('div');
    const input = el('input');
    input.value = col.name;
    body.appendChild(input);
    modal('열 이름 변경', body, [
      { label: '취소' },
      { label: '변경', primary: true, action: async () => {
        try {
          await API.call('column', { action: 'rename', cid: col.id, name: input.value });
          await refreshState();
        } catch (err) { fail(err); }
      } },
    ]);
    setTimeout(() => input.select(), 30);
  }

  async function addColumn(at) {
    const body = el('div');
    const input = el('input');
    input.value = 'new_column';
    body.appendChild(input);
    modal('열 추가', body, [
      { label: '취소' },
      { label: '추가', primary: true, action: async () => {
        try {
          await API.call('column', { action: 'add', name: input.value, at: at });
          grid.invalidate();
          await refreshState();
        } catch (err) { fail(err); }
      } },
    ]);
    setTimeout(() => input.select(), 30);
  }

  async function sortBy(col, desc) {
    try {
      const job = await runJob('sort', { cid: col.id, desc: desc }, '정렬 중');
      grid.columns.forEach((c) => { c.sort = 0; });
      col.sort = desc ? -1 : 1;
      grid.renderHeader();
      grid.invalidate();
      await refreshState();
      say(`${API.num(job.result.rows)}행 정렬 완료 (${job.elapsed.toFixed(1)}초)`);
    } catch (err) { fail(err); }
  }

  /* -------------------------------------------------------------- editing */

  async function setCells(cells) {
    try {
      const res = await API.call('set_cells', { cells: cells });
      grid.invalidate();
      await refreshState(res.state);
    } catch (err) { fail(err); }
  }

  async function insertRow(pos, count) {
    try {
      await API.call('insert_row', { pos: pos, count: count || 1 });
      grid.invalidate();
      await refreshState();
    } catch (err) { fail(err); }
  }

  async function duplicateRows(ids) {
    if (!ids.length) return;
    try {
      await API.call('duplicate_rows', { ids: ids });
      grid.invalidate();
      await refreshState();
    } catch (err) { fail(err); }
  }

  async function deleteRows(ids) {
    if (!ids.length) return;
    try {
      const res = await API.call('delete_rows', { ids: ids });
      grid.invalidate();
      await refreshState(res.state);
      say(`${API.num(res.removed)}행을 삭제했습니다. (${MOD}+Z로 취소)`);
    } catch (err) { fail(err); }
  }

  async function copySelection() {
    const sel = await grid.selectedCells();
    const cols = grid.columns;
    const lines = sel.rows.map((row) => {
      if (!row) return '';
      const out = [];
      for (let c = sel.box.c0; c <= sel.box.c1; c++) out.push(row.cells[c] != null ? row.cells[c] : '');
      return out.join('\t');
    });
    const text = lines.join('\r\n');
    try {
      await navigator.clipboard.writeText(text);
      say(`${lines.length}행 복사됨`);
    } catch (err) {
      const ta = el('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
      say(`${lines.length}행 복사됨`);
    }
    void cols;
  }

  async function pasteClipboard(text) {
    if (!text) return;
    const rows = text.replace(/\r\n?/g, '\n').replace(/\n$/, '').split('\n').map((l) => l.split('\t'));
    const startR = grid.cursor.r;
    const startC = grid.cursor.c;
    const cells = [];
    const data = await grid.rowsFor(startR, Math.min(startR + rows.length - 1, grid.total - 1));
    rows.forEach((cols, i) => {
      const row = data[i];
      if (!row) return;
      cols.forEach((value, j) => {
        const c = startC + j;
        if (c < grid.columns.length) cells.push({ rid: row.id, cid: grid.columns[c].id, value: value });
      });
    });
    if (cells.length) {
      await setCells(cells);
      say(`${cells.length}개 셀 붙여넣기`);
    }
  }

  async function fillDown() {
    const box = grid.selectionBox();
    if (box.r1 <= box.r0) return;
    const rows = await grid.rowsFor(box.r0, box.r1);
    const source = rows[0];
    if (!source) return;
    const cells = [];
    for (let i = 1; i < rows.length; i++) {
      const row = rows[i];
      if (!row) continue;
      for (let c = box.c0; c <= box.c1; c++) {
        cells.push({ rid: row.id, cid: grid.columns[c].id, value: source.cells[c] != null ? source.cells[c] : '' });
      }
    }
    if (cells.length) await setCells(cells);
  }

  async function clearSelection() {
    const box = grid.selectionBox();
    const rows = await grid.rowsFor(box.r0, box.r1);
    const cells = [];
    rows.forEach((row) => {
      if (!row) return;
      for (let c = box.c0; c <= box.c1; c++) cells.push({ rid: row.id, cid: grid.columns[c].id, value: '' });
    });
    if (cells.length) await setCells(cells);
  }

  /* ---------------------------------------------------------------- tools */

  function toolsMenu(e) {
    const rect = e.target.getBoundingClientRect();
    menu(rect.left, rect.bottom + 4, [
      { label: '행 삽입 (커서 위)', action: () => insertRow(grid.cursor.r) },
      { label: '선택 행 삭제', action: () => deleteRows(grid.selectedRowIds()) },
      '-',
      { label: '중복 행 제거…', action: dedupeDialog },
      { label: '공백 다듬기 (선택 열)', action: async () => {
        try {
          const job = await runJob('trim', { cid: grid.columns[grid.cursor.c].id, scope: 'view' }, '공백 정리');
          grid.invalidate();
          await refreshState();
          say(`${API.num(job.result.changed)}개 셀 정리됨`);
        } catch (err) { fail(err); }
      } },
      '-',
      { label: '행 높이: 좁게', action: () => grid.setRowHeight(20, false) },
      { label: '행 높이: 보통', action: () => grid.setRowHeight(24, false) },
      { label: '행 높이: 넓게(줄바꿈)', action: () => grid.setRowHeight(48, true) },
      '-',
      { label: '행으로 이동…', key: MOD + '+G', action: gotoDialog },
      { label: '단축키 도움말', key: '?', action: helpDialog },
    ]);
  }

  function dedupeDialog() {
    const body = el('div');
    const sel = el('select');
    sel.appendChild(new Option('모든 열 기준', ''));
    (state.server.columns || []).forEach((c) => sel.appendChild(new Option(c.name + ' 기준', c.id)));
    body.appendChild(sel);
    body.appendChild(el('p', 'muted', '두 번째 이후로 나타나는 중복 행을 삭제합니다. 저장 전까지는 되돌릴 수 있습니다.'));
    modal('중복 행 제거', body, [
      { label: '취소' },
      { label: '제거', primary: true, action: async () => {
        try {
          const job = await runJob('dedupe', {
            cids: sel.value ? [+sel.value] : null, scope: 'view', apply: true,
          }, '중복 검사');
          grid.invalidate();
          await refreshState();
          say(`중복 ${API.num(job.result.duplicates)}행 중 ${API.num(job.result.removed)}행 삭제`);
        } catch (err) { fail(err); }
      } },
    ]);
  }

  function gotoDialog() {
    const body = el('div');
    const input = el('input');
    input.type = 'number';
    input.placeholder = '행 번호';
    body.appendChild(input);
    modal('행으로 이동', body, [
      { label: '취소' },
      { label: '이동', primary: true, action: () => {
        const r = Math.max(1, Math.min(+input.value || 1, grid.total)) - 1;
        grid.setCursor(r, grid.cursor.c, false);
        grid.scrollToRow(r, 'center');
      } },
    ]);
    setTimeout(() => input.focus(), 30);
  }

  function helpDialog() {
    const body = el('div');
    const box = el('div', 'shortcuts');
    const add = (k, v) => { box.appendChild(el('b', null, k)); box.appendChild(el('span', null, v)); };
    add('↑ ↓ ← →', '셀 이동 (Shift: 범위 선택)');
    add('Enter / F2', '셀 편집 시작 / 편집 후 아래로');
    add('Tab', '오른쪽 셀로 이동');
    add(MOD + '+C / ' + MOD + '+V', '복사 / 붙여넣기 (여러 셀 지원)');
    add(MOD + '+D', '선택 범위 아래로 채우기');
    add('Delete', '선택 셀 비우기');
    add(MOD + '+Z / ' + MOD + '+Shift+Z', '실행 취소 / 다시 실행');
    add(MOD + '+F', '찾기 패널');
    add(MOD + '+Enter', '다음 검색 결과');
    add(MOD + '+G', '행으로 이동');
    add(MOD + '+S', '저장');
    add(MOD + '+O', '파일 열기');
    add('M', '북마크 토글');
    add('PageUp / PageDown', '한 화면 이동');
    add(MOD + '+Home / End', '처음 / 마지막 행');
    body.appendChild(box);
    body.appendChild(el('p', 'muted',
      API.IS_MAC ? 'macOS에서는 ⌘ 키를 사용합니다.' : 'Windows/Linux에서는 Ctrl 키를 사용합니다.'));
    modal('단축키', body, [{ label: '닫기', primary: true }]);
  }

  /* ----------------------------------------------------------------- save */

  async function save() {
    if (!state.server.dirty) return;
    const body = el('div');
    body.appendChild(el('p', null, `${state.server.name} 파일에 변경 내용을 덮어씁니다.`));
    const backup = el('input'); backup.type = 'checkbox'; backup.checked = true;
    const label = el('label', 'chk');
    label.appendChild(backup);
    label.appendChild(document.createTextNode(' 원본을 .bak 으로 백업'));
    body.appendChild(label);
    if (state.server.os === 'nt') {
      body.appendChild(el('p', 'muted', 'Excel 등에서 같은 파일을 열어두면 저장에 실패할 수 있습니다. 닫은 뒤 저장하세요.'));
    }
    modal('저장', body, [
      { label: '취소' },
      { label: '저장', primary: true, action: async () => {
        try {
          const job = await runJob('save', { backup: backup.checked }, '저장 중');
          state.filters = [];
          $('#quick').value = '';
          renderChips();
          grid.invalidate();
          await refreshState();
          say(`${API.num(job.result.written)}행 저장 완료`);
        } catch (err) { fail(err); }
      } },
    ]);
  }

  function saveAsDialog() {
    const st = state.server;
    const body = el('div');
    const path = el('input');
    const dot = st.path.lastIndexOf('.');
    path.value = (dot > 0 ? st.path.slice(0, dot) : st.path) + '_edited' + (dot > 0 ? st.path.slice(dot) : '.csv');
    const enc = el('select');
    [['', '원본과 동일 (' + st.dialect.encoding + ')'],
     ['utf-8-sig', 'UTF-8 (BOM 포함 · Excel 권장)'],
     ['utf-8', 'UTF-8'],
     ['cp949', 'CP949 (한국어 Windows 레거시)'],
     ['cp932', 'CP932 (일본어)'],
    ].forEach(([v, l]) => enc.appendChild(new Option(l, v)));
    const delim = el('select');
    [['', '원본과 동일'], [',', '쉼표 (,)'], ['\t', '탭'], [';', '세미콜론 (;)'], ['|', '파이프 (|)']]
      .forEach(([v, l]) => delim.appendChild(new Option(l, v)));
    const nl = el('select');
    [['', '원본과 동일 (' + st.dialect.newline.toUpperCase() + ')'],
     ['crlf', 'CRLF (Windows)'], ['lf', 'LF (macOS/Linux)']]
      .forEach(([v, l]) => nl.appendChild(new Option(l, v)));
    const viewOnly = el('input');
    viewOnly.type = 'checkbox';
    viewOnly.checked = !!st.view;
    viewOnly.disabled = !st.view;

    const field = (labelText, node, hint) => {
      const f = el('div', 'field');
      f.appendChild(el('label', null, labelText));
      f.appendChild(node);
      if (hint) f.appendChild(el('span', 'muted', hint));
      return f;
    };
    body.appendChild(field('저장 경로', path));
    body.appendChild(field('인코딩', enc, 'Excel에서 한글이 깨지면 UTF-8(BOM) 또는 CP949를 고르세요.'));
    body.appendChild(field('구분자', delim));
    body.appendChild(field('줄바꿈', nl));
    const vo = el('label', 'chk');
    vo.appendChild(viewOnly);
    vo.appendChild(document.createTextNode(
      st.view ? ` 현재 필터 결과 ${API.num(st.view_rows)}행만 저장` : ' 현재 필터 결과만 저장 (필터 없음)'));
    body.appendChild(vo);

    modal('다른 이름으로 저장 · 내보내기', body, [
      { label: '취소' },
      { label: '저장', primary: true, action: async () => {
        try {
          const job = await runJob('save_as', {
            path: path.value, view_only: viewOnly.checked, encoding: enc.value,
            delimiter: delim.value, newline: nl.value,
          }, '내보내는 중');
          say(`${API.num(job.result.written)}행을 ${job.result.path} 에 저장했습니다.`);
        } catch (err) { fail(err); }
      } },
    ]);
  }

  /* ------------------------------------------------------------- keyboard */

  function onKey(e) {
    const mod = API.IS_MAC ? e.metaKey : e.ctrlKey;
    if (grid.editing) return;
    const key = e.key;
    if (mod && key.toLowerCase() === 'c') { e.preventDefault(); copySelection(); return; }
    if (mod && key.toLowerCase() === 'z') {
      e.preventDefault();
      (e.shiftKey ? redo : undo)();
      return;
    }
    if (mod && key.toLowerCase() === 'y') { e.preventDefault(); redo(); return; }
    if (mod && key.toLowerCase() === 'd') { e.preventDefault(); fillDown(); return; }
    if (mod && key.toLowerCase() === 's') { e.preventDefault(); save(); return; }
    if (mod && key.toLowerCase() === 'o') { e.preventDefault(); openDialog(); return; }
    if (mod && key.toLowerCase() === 'g') { e.preventDefault(); gotoDialog(); return; }
    if (mod && key.toLowerCase() === 'f') { e.preventDefault(); showTab('find'); $('#find-needle').focus(); return; }
    if (mod && key === 'Enter') { e.preventDefault(); gotoHit(e.shiftKey ? -1 : 1); return; }
    if (mod && (key === '-' || key === 'Backspace')) { e.preventDefault(); deleteRows(grid.selectedRowIds()); return; }
    if (mod && key === '+') { e.preventDefault(); insertRow(grid.cursor.r + 1); return; }

    switch (key) {
      case 'ArrowDown': e.preventDefault(); grid.moveCursor(1, 0, e.shiftKey); break;
      case 'ArrowUp': e.preventDefault(); grid.moveCursor(-1, 0, e.shiftKey); break;
      case 'ArrowLeft': e.preventDefault(); grid.moveCursor(0, -1, e.shiftKey); break;
      case 'ArrowRight': e.preventDefault(); grid.moveCursor(0, 1, e.shiftKey); break;
      case 'PageDown': e.preventDefault(); grid.moveCursor(grid.visibleCount() - 3, 0, e.shiftKey); break;
      case 'PageUp': e.preventDefault(); grid.moveCursor(-(grid.visibleCount() - 3), 0, e.shiftKey); break;
      case 'Home': e.preventDefault(); mod ? grid.setCursor(0, 0, e.shiftKey) : grid.setCursor(grid.cursor.r, 0, e.shiftKey); break;
      case 'End': e.preventDefault(); mod ? grid.setCursor(grid.total - 1, grid.columns.length - 1, e.shiftKey) : grid.setCursor(grid.cursor.r, grid.columns.length - 1, e.shiftKey); break;
      case 'Enter': case 'F2': e.preventDefault(); grid.beginEdit(grid.cursor.r, grid.cursor.c); break;
      case 'Delete': case 'Backspace': e.preventDefault(); clearSelection(); break;
      case 'Escape': grid.cancelEdit(); break;
      case 'm': case 'M': {
        const row = grid.rowAt(grid.cursor.r);
        if (row) { grid.toggleMark(row.id); renderMarks(); }
        break;
      }
      case '?': helpDialog(); break;
      default:
        if (!mod && !e.altKey && key.length === 1) {
          e.preventDefault();
          grid.beginEdit(grid.cursor.r, grid.cursor.c, key);
        }
    }
  }

  async function undo() {
    try {
      const res = await API.call('undo');
      grid.invalidate();
      await refreshState(res.state);
      if (res.label) say('실행 취소: ' + res.label);
    } catch (err) { fail(err); }
  }

  async function redo() {
    try {
      const res = await API.call('redo');
      grid.invalidate();
      await refreshState(res.state);
      if (res.label) say('다시 실행: ' + res.label);
    } catch (err) { fail(err); }
  }

  function showTab(name) {
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
    document.querySelectorAll('.panel').forEach((p) => p.classList.toggle('hidden', p.dataset.panel !== name));
    $('#side').classList.remove('hidden');
  }

  /* ------------------------------------------------------------------ boot */

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('csvopt.theme', theme); } catch (err) { /* private mode */ }
  }

  async function boot() {
    let theme = 'light';
    try { theme = localStorage.getItem('csvopt.theme') || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); } catch (err) { /* ignore */ }
    applyTheme(theme);

    grid = new Grid({
      head: $('#header'),
      viewport: $('#viewport'),
      canvas: $('#canvas'),
      root: $('#grid'),
      fetchRows: (start, count) => API.call('rows', { start: start, count: count }),
      onSetCells: setCells,
      onContext: cellMenu,
      onHeaderContext: headerMenu,
      onSort: (col, desc) => sortBy(col, desc),
      onSelChange: (box) => {
        const rows = box.r1 - box.r0 + 1;
        const cols = box.c1 - box.c0 + 1;
        $('#st-sel').textContent = rows * cols > 1
          ? `선택 ${API.num(rows)}행 × ${cols}열`
          : `행 ${API.num(box.r0 + 1)}`;
      },
    });

    $('#grid').addEventListener('keydown', onKey);
    $('#grid').addEventListener('paste', (e) => {
      e.preventDefault();
      pasteClipboard((e.clipboardData || window.clipboardData).getData('text'));
    });
    $('#btn-open').onclick = openDialog;
    $('#btn-open2').onclick = openDialog;
    $('#btn-undo').onclick = undo;
    $('#btn-redo').onclick = redo;
    $('#btn-save').onclick = save;
    $('#btn-saveas').onclick = saveAsDialog;
    $('#btn-help').onclick = helpDialog;
    $('#btn-tools').onclick = toolsMenu;
    $('#btn-find').onclick = () => { showTab('find'); $('#find-needle').focus(); };
    $('#btn-theme').onclick = () => applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
    $('#btn-addfilter').onclick = () => editFilter(-1);
    $('#btn-clearfilter').onclick = () => {
      state.filters = [];
      $('#quick').value = '';
      $('#scope-view').checked = false;
      applyFilters(false);
    };
    $('#quick').addEventListener('keydown', (e) => { if (e.key === 'Enter') applyFilters(); });
    $('#facet-run').onclick = runFacets;
    $('#find-run').onclick = runFind;
    $('#find-next').onclick = () => gotoHit(1);
    $('#find-prev').onclick = () => gotoHit(-1);
    $('#find-replace').onclick = replaceAll;
    $('#find-needle').addEventListener('keydown', (e) => { if (e.key === 'Enter') runFind(); });
    $('#mark-clear').onclick = () => { grid.marks.clear(); renderMarks(); grid.render(); };
    $('#col-add').onclick = () => addColumn(null);
    $('#col-showall').onclick = () => { grid.columns.forEach((c) => { c.hidden = false; }); grid.renderHeader(); renderColumnPanel(); };
    $('#job-cancel').onclick = () => { if (state.activeJob) API.cancel(state.activeJob); };
    document.querySelectorAll('.tab').forEach((t) => { t.onclick = () => showTab(t.dataset.tab); });
    window.addEventListener('beforeunload', (e) => {
      if (state.server && state.server.dirty) { e.preventDefault(); e.returnValue = ''; }
    });

    const st = await refreshState();
    if (!st.open) openDialog();
    else $('#grid').focus();
    renderMarks();
  }

  boot().catch(fail);
})();
