await (async () => {
  // Batch Wordstat measurement for the vk-ads-keywords skill. kw.py fills REQ;
  // run through javascript_tool in a logged-in https://wordstat.yandex.ru tab.
  // It calls the same endpoint the Wordstat page uses, so one call measures a
  // whole OR group of any length. The full result goes into <article id=kw-result>
  // because javascript_tool truncates return values; read it with get_page_text.
  const REQ = __REQ__;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const pad = (n) => String(n).padStart(2, '0');
  const ru = (d) => `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()}`;
  // FNV-1a over UTF-8, mirrored in kw.py: binds results to the requested expressions and
  // catches any change to the numbers while the JSON is copied into kw.py record.
  const fnv = (s) => {
    let h = 0x811c9dc5;
    for (const byte of new TextEncoder().encode(s)) h = Math.imul(h ^ byte, 0x01000193) >>> 0;
    return h.toString(16).padStart(8, '0');
  };
  const rowsKey = (rows) => (rows || []).map(([t, n]) => `${t}:${n}`).join(';');
  const canon = (r) => [r.id, r.h, r.error || '', r.total ?? '', r.invalid ? 1 : 0,
    (r.period_ms && r.period_ms.endDate) || '', rowsKey(r.popular), rowsKey(r.similar)].join('|');
  if (location.hostname !== 'wordstat.yandex.ru') return 'ОШИБКА: это не вкладка wordstat.yandex.ru';

  // Same date window the Wordstat UI sends; the popular table itself is the last 30 days.
  const now = new Date();
  const end = new Date(now.getFullYear(), now.getMonth(), 0);
  const start = new Date(end.getFullYear() - 2, end.getMonth() + 1, 1);
  // The region label renders a moment after navigation; kw.py record needs it to verify the id.
  let text = document.body.innerText || '';
  for (let wait = 0; wait < 20 && !/Похожие\n/.test(text); wait += 1) {
    await sleep(250);
    text = document.body.innerText || '';
  }
  const out = {
    request_id: REQ.request_id,
    retrieved_at: now.toISOString(),
    page: {
      region_param: new URLSearchParams(location.search).get('region'),
      region_label: ((text.match(/Похожие\n([^\n]+)\n/) || [])[1] || '').trim() || null,
    },
    results: [],
  };

  for (const item of REQ.items) {
    let data = null;
    let error = null;
    for (let attempt = 0; attempt < 3 && !data; attempt += 1) {
      try {
        const response = await fetch('/wordstat/api/getTable', {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            currentDevice: REQ.devices,
            dbname: 'rus',
            filters: { region: String(REQ.region), tableType: 'popular' },
            searchValue: item.expr,
            startDate: ru(start),
            endDate: ru(end),
          }),
        });
        const body = await response.text();
        if (!response.ok) {
          error = `HTTP ${response.status}: ${body.slice(0, 160)}`;
        } else {
          try {
            data = JSON.parse(body);
            // Throttled sessions answer {"table":{"noData":...}} with no totalValue —
            // treat that as an error and retry with a longer pause, not as a zero.
            if (data && typeof data.totalValue !== 'number' && !(data.table || {}).isQueryInvalid) {
              error = 'noData без totalValue (лимит Вордстата?)';
              data = null;
              await sleep(4000 * (attempt + 1));
            }
          } catch (parseError) {
            // An HTML answer here means Yandex asked for a robot check.
            error = `не JSON (проверка на робота?): ${body.slice(0, 160)}`;
          }
        }
      } catch (networkError) {
        error = String(networkError);
      }
      if (!data) await sleep(1500 * (attempt + 1));
    }
    if (!data) {
      out.results.push({ id: item.id, h: fnv(item.expr), error });
      continue;
    }
    const table = data.table || {};
    const rows = table.tableData || {};
    const pick = (list, limit) => (list || []).slice(0, limit)
      .map((row) => [String(row.text), parseInt(row.value, 10) || 0]);
    out.results.push({
      id: item.id,
      h: fnv(item.expr),
      total: data.totalValue,
      invalid: Boolean(table.isQueryInvalid),
      period_ms: (table.info || {}).period || null,
      popular: pick(rows.popular, item.rows),
      similar: pick(rows.associations, item.similar),
    });
    await sleep(300);
  }
  out.check = fnv(`${out.request_id}\n${out.results.map(canon).join('\n')}`);

  let box = document.getElementById('kw-result');
  if (!box) {
    box = document.createElement('article');
    box.id = 'kw-result';
    document.body.prepend(box);
  }
  box.textContent = JSON.stringify(out);
  // "p1=105; p2=618" looks like a cookie string and gets redacted by javascript_tool.
  const summary = out.results.map((r) => `${r.id} ${r.error ? 'ОШИБКА ' + r.error : r.total}`).join(', ');
  return `${REQ.request_id} готов: ${summary}. Дальше get_page_text этой вкладки и kw.py record`;
})()
