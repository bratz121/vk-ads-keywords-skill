await (async () => {
  // Double-meaning audit for the vk-ads-keywords skill. kw.py audit fills REQ; run through
  // javascript_tool in a logged-in https://wordstat.yandex.ru tab. For every phrase it pulls
  // the real Wordstat rows and prints them as a compact report with marker flags (film/series,
  // games, school, person ...). Nothing is recorded: the agent reads the report via
  // get_page_text and decides per phrase (kw.py checked / rm).
  const REQ = __REQ__;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const pad = (n) => String(n).padStart(2, '0');
  const ru = (d) => `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()}`;
  if (location.hostname !== 'wordstat.yandex.ru') return 'ОШИБКА: это не вкладка wordstat.yandex.ru';
  const markers = REQ.markers.map(([name, pattern]) => [name, new RegExp(pattern, 'i')]);
  const flags = (text) => markers.filter(([, rx]) => rx.test(text)).map(([name]) => name);
  const now = new Date();
  const end = new Date(now.getFullYear(), now.getMonth(), 0);
  const start = new Date(end.getFullYear() - 2, end.getMonth() + 1, 1);
  const lines = [`АУДИТ ДВОЙНОГО СМЫСЛА · регион ${REQ.region} · фраз: ${REQ.items.length}`];

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
        if (!response.ok) error = `HTTP ${response.status}`;
        else try { data = JSON.parse(body); } catch (e) { error = 'не JSON (проверка на робота?)'; }
      } catch (networkError) {
        error = String(networkError);
      }
      if (!data) await sleep(1500 * (attempt + 1));
    }
    if (!data) {
      lines.push(`\n=== ${item.expr}: ОШИБКА ${error}`);
      continue;
    }
    const rows = (data.table || {}).tableData || {};
    const total = data.totalValue;
    const popular = (rows.popular || []).slice(0, REQ.rows);
    const similar = (rows.associations || []).slice(0, REQ.similar);
    // The share of top rows the markers flagged, weighted by row frequency. A hint only:
    // the verdict is made on the rows themselves.
    let flagged = 0;
    let weight = 0;
    const format = (row) => {
      const hits = flags(row.text);
      const value = parseInt(row.value, 10) || 0;
      weight += value;
      if (hits.length) flagged += value;
      return `  ${value}\t${row.text}${hits.length ? '   [' + hits.join(', ') + ']' : ''}`;
    };
    const popularLines = popular.map(format);
    const share = weight ? Math.round((100 * flagged) / weight) : 0;
    lines.push(`\n=== ${item.expr}: ${total} · маркеры в топе ~${share}%`);
    lines.push(...popularLines);
    if (similar.length) {
      lines.push('  похожие:');
      lines.push(...similar.map((row) => {
        const hits = flags(row.text);
        return `  ~ ${row.text}${hits.length ? '   [' + hits.join(', ') + ']' : ''}`;
      }));
    }
    await sleep(300);
  }
  lines.push('\nКонец аудита. Решение по каждой фразе: чистая -> kw.py checked; чужой смысл -> kw.py rm; '
    + 'узкая примесь -> kw.py minus add.');

  let box = document.getElementById('kw-result');
  if (!box) {
    box = document.createElement('article');
    box.id = 'kw-result';
    document.body.prepend(box);
  }
  box.textContent = lines.join('\n');
  return `аудит готов: ${REQ.items.length} фраз. Дальше get_page_text этой вкладки`;
})()
