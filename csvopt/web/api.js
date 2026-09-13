/* Thin client for the local csvopt API, plus job polling and platform helpers. */
(function (global) {
  'use strict';

  const TOKEN = new URLSearchParams(location.search).get('t') || '';
  const IS_MAC = /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent);
  const MOD_LABEL = IS_MAC ? '⌘' : 'Ctrl';

  async function call(name, payload) {
    const res = await fetch('/api/' + name, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Csvopt-Token': TOKEN },
      body: JSON.stringify(payload || {}),
    });
    let data;
    try {
      data = await res.json();
    } catch (err) {
      throw new Error('서버 응답을 읽지 못했습니다 (' + res.status + ')');
    }
    if (!res.ok || data.error) throw new Error(data.error || ('HTTP ' + res.status));
    return data;
  }

  /* Runs a server job to completion, reporting progress along the way.
     onProgress(job) is called on every poll; returns the finished job. */
  async function job(name, payload, onProgress) {
    const started = await call(name, payload);
    let info = started.job;
    if (onProgress) onProgress(info);
    let delay = 60;
    while (info.status === 'running') {
      await new Promise((r) => setTimeout(r, delay));
      delay = Math.min(delay * 1.35, 400);
      info = (await call('job', { id: info.id })).job;
      if (onProgress) onProgress(info);
    }
    if (info.status === 'error') throw new Error(info.error);
    return info;
  }

  function cancel(id) {
    return call('cancel_job', { id: id }).catch(() => {});
  }

  function bytes(n) {
    if (n == null) return '';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)) + ' ' + units[i];
  }

  const num = (n) => (n == null ? '' : n.toLocaleString());

  global.API = { call, job, cancel, bytes, num, IS_MAC, MOD_LABEL, TOKEN };
})(window);
