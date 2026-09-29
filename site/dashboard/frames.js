(() => {
  const state = { range: '1h', charts: {}, timers: [], detailMetric: null, loadingTrends: false };
  const metricKeys = ['voltage-a', 'current-a', 'instantaneous-active-power', 'temperature'];
  const metricConfig = {
    'voltage-a': { title: 'A 相电压', chart: 'voltage-chart', panel: 'panel-voltage', kpi: 'kpi-voltage', time: 'kpi-voltage-time', color: '#0395ad', unit: 'V', digits: 1, min: 0, max: 200, interval: 50, aggregateMinutes: 10 },
    'current-a': { title: 'A 相电流', chart: 'current-chart', panel: 'panel-current', kpi: 'kpi-current', time: 'kpi-current-time', color: '#15946b', unit: 'A', digits: 3 },
    'instantaneous-active-power': { title: '瞬时有功功率', chart: 'power-chart', panel: null, kpi: 'kpi-power', time: 'kpi-power-time', color: '#2c6bed', unit: 'W', digits: 1, min: 0, max: 2000, interval: 100 },
    temperature: { title: '模块温度', chart: 'temperature-chart', panel: 'panel-temperature', kpi: null, time: null, color: '#bb6d13', unit: '°C', digits: 1 }
  };
  const THREE_HOURS_MS = 3 * 60 * 60 * 1000;
  const byId = (id) => document.getElementById(id);
  const chartFor = (id) => {
    if (!state.charts[id]) state.charts[id] = echarts.init(byId(id));
    return state.charts[id];
  };
  const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
  const formatTime = (value) => { const date = new Date(value); return Number.isNaN(date.getTime()) ? '--:--' : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }); };
  const formatClock = (value) => { const date = new Date(value); return Number.isNaN(date.getTime()) ? '--:--:--' : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }); };
  const formatCaptureTime = (value) => formatClock(value);
  const formatDateTime = (value) => { const date = new Date(value); return Number.isNaN(date.getTime()) ? '等待数据' : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }); };
  const setStatus = (online, text) => { byId('status-text').textContent = text; byId('status-dot').parentElement.classList.toggle('is-offline', !online); };
  const toLocalInputValue = (date) => { const pad = (value) => String(value).padStart(2, '0'); return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`; };
  const timeAxisLabel = (value) => formatTime(value);
  const getRange = () => {
    const end = new Date();
    if (state.range === 'daylight') return { start: new Date(end.getFullYear(), end.getMonth(), end.getDate(), 6), end: new Date(end.getFullYear(), end.getMonth(), end.getDate(), 18) };
    const hours = Number(state.range.replace('h', '')) || 1;
    return { start: new Date(end.getTime() - hours * 60 * 60 * 1000), end };
  };
  const aggregateVoltagePoints = (points) => {
    const bucketSize = 10 * 60 * 1000;
    const buckets = new Map();
    points.forEach((point) => {
      const timestamp = new Date(point.captured_at).getTime();
      const value = Number(point.value);
      if (!Number.isFinite(timestamp) || !Number.isFinite(value)) return;
      const bucket = Math.floor(timestamp / bucketSize) * bucketSize;
      const item = buckets.get(bucket) || { sum: 0, count: 0 };
      item.sum += value;
      item.count += 1;
      buckets.set(bucket, item);
    });
    const known = [...buckets.entries()]
      .map(([timestamp, item]) => ({ timestamp, value: item.sum / item.count }))
      .sort((left, right) => left.timestamp - right.timestamp);
    if (known.length < 2) return known.map((point) => ({ captured_at: new Date(point.timestamp).toISOString(), value: point.value }));

    const result = [];
    let nextIndex = 0;
    for (let timestamp = known[0].timestamp; timestamp <= known[known.length - 1].timestamp; timestamp += bucketSize) {
      while (nextIndex < known.length && known[nextIndex].timestamp < timestamp) nextIndex += 1;
      const exact = known[nextIndex] && known[nextIndex].timestamp === timestamp ? known[nextIndex] : null;
      if (exact) {
        result.push({ captured_at: new Date(timestamp).toISOString(), value: exact.value });
        continue;
      }
      const previous = known[nextIndex - 1];
      const next = known[nextIndex];
      if (!previous || !next) continue;
      const ratio = (timestamp - previous.timestamp) / (next.timestamp - previous.timestamp);
      result.push({ captured_at: new Date(timestamp).toISOString(), value: previous.value + (next.value - previous.value) * ratio });
    }
    return result;
  };
  const chartPointsFor = (key, points) => metricConfig[key].aggregateMinutes ? aggregateVoltagePoints(points) : points;
  const makeOption = (points, config, expanded = false) => {
    const values = points.map((point) => [new Date(point.captured_at).getTime(), Number(point.value)]);
    const dataMax = values.length ? Math.max(...values.map((point) => point[1])) : 0;
    const max = config.max ? (config.max === 2000 ? Math.min(2000, Math.max(200, Math.ceil(dataMax * 1.2 / 100) * 100)) : config.max) : undefined;
    return { animation: false, grid: { left: 56, right: 18, top: 22, bottom: expanded ? 70 : 35 }, tooltip: { trigger: 'axis', confine: true, formatter: (params) => { const items = Array.isArray(params) ? params : [params]; const time = timeAxisLabel(items[0] && (items[0].axisValue ?? (Array.isArray(items[0].value) ? items[0].value[0] : items[0].value))); return [time, ...items.map((item) => `${item.marker}${config.title} ${Number(Array.isArray(item.value) ? item.value[1] : item.value).toFixed(config.digits)} ${config.unit}`)].join('<br>'); } }, xAxis: { type: 'time', interval: THREE_HOURS_MS, axisLabel: { color: '#7b899b', fontSize: 10, formatter: timeAxisLabel, hideOverlap: true }, axisLine: { lineStyle: { color: '#cbd6e2' } }, splitLine: { show: false } }, yAxis: { type: 'value', min: config.min, max, interval: config.interval, axisLabel: { color: '#7b899b', fontSize: 10, formatter: (value) => Number(value).toFixed(config.digits) }, axisLine: { show: true, lineStyle: { color: '#cbd6e2' } }, splitLine: { lineStyle: { color: '#e8eef4' } } }, dataZoom: expanded ? [{ type: 'inside', filterMode: 'none' }, { type: 'slider', height: 22, bottom: 15, borderColor: '#dfe7ef', fillerColor: 'rgba(44,107,237,.16)', handleStyle: { color: '#2c6bed' } }] : [], series: [{ type: 'line', smooth: .22, connectNulls: true, showSymbol: false, symbolSize: 7, data: values, lineStyle: { width: 2.5, color: config.color }, itemStyle: { color: config.color } }] };
  };
  const renderMetric = (key, metric) => {
    const config = metricConfig[key]; const points = metric && metric.points ? metric.points.filter((point) => Number.isFinite(Number(point.value))) : []; const latest = points[points.length - 1]; const value = latest ? Number(latest.value).toFixed(config.digits) : '--';
    if (config.kpi) byId(config.kpi).textContent = value;
    if (config.panel) byId(config.panel).textContent = `${value} ${config.unit}`;
    if (config.time) byId(config.time).textContent = latest ? `${formatClock(latest.captured_at)} 更新` : '等待有效数据';
    chartFor(config.chart).setOption(makeOption(chartPointsFor(key, points), config), true);
  };
  const renderEnergy = (summary) => {
    const total = Number(summary.total_energy_kwh); const daily = Number(summary.daily_energy_kwh); const valid = (value) => Number.isFinite(value);
    byId('total-energy').textContent = valid(total) ? total.toFixed(3) : '--'; byId('daily-energy').textContent = valid(daily) ? daily.toFixed(3) : '--'; byId('total-income').textContent = valid(total) ? (total * .52).toFixed(2) : '--'; byId('daily-income').textContent = valid(daily) ? (daily * .52).toFixed(2) : '--';
    byId('kpi-energy').textContent = valid(daily) ? daily.toFixed(3) : '--'; byId('kpi-income').textContent = valid(daily) ? `收益 ${(daily * .52).toFixed(2)} 元` : '收益 -- 元';
    const points = (summary.daily_energy || []).filter((point) => Number.isFinite(Number(point.kwh))).map((point) => [point.date, Number(point.kwh)]);
    chartFor('energy-chart').setOption({ animation: false, grid: { left: 48, right: 12, top: 15, bottom: 30 }, tooltip: { trigger: 'axis', valueFormatter: (value) => `${Number(value).toFixed(3)} kWh` }, xAxis: { type: 'category', data: points.map((point) => point[0].slice(5)), axisLabel: { color: '#7b899b', fontSize: 10 } }, yAxis: { type: 'value', axisLabel: { color: '#7b899b', fontSize: 10 }, splitLine: { lineStyle: { color: '#e8eef4' } } }, series: [{ type: 'bar', barMaxWidth: 22, data: points.map((point) => point[1]), itemStyle: { color: '#7258d6', borderRadius: [4, 4, 0, 0] } }] }, true);
  };
  const loadTrends = async () => {
    if (state.loadingTrends) return;
    state.loadingTrends = true;
    const { start, end } = getRange();
    const params = new URLSearchParams({ limit: '1000', start_at: start.toISOString(), end_at: end.toISOString() }); metricKeys.forEach((key) => params.append('metric_key', key));
    try {
      const response = await fetch(`/api/v1/dlt645/trends?${params}`, { cache: 'no-store' });
      if (!response.ok) throw new Error('trend');
      const trends = await response.json();
      metricKeys.forEach((key) => renderMetric(key, (trends.metrics || []).find((metric) => metric.key === key)));
    } catch (error) {}
    finally { state.loadingTrends = false; }
  };
  const loadStatus = async () => {
    try { const response = await fetch('/api/v1/dlt645/status', { cache: 'no-store' }); if (!response.ok) throw new Error('status'); const status = await response.json(); byId('last-received').textContent = status.last_received_at ? `${formatDateTime(status.last_received_at)} 最近接收` : '等待数据'; setStatus(Boolean(status.online), status.online ? '设备在线' : '设备离线'); } catch (error) { setStatus(false, '状态暂不可用'); }
  };
  const loadEnergy = async () => { try { const response = await fetch('/api/v1/dlt645/summary?days=30', { cache: 'no-store' }); if (!response.ok) throw new Error('energy'); renderEnergy(await response.json()); } catch (error) {} };
  const loadFrames = async () => { try { const response = await fetch('/api/v1/dlt645/frame?limit=8&summary=0&total=0', { cache: 'no-store' }); if (!response.ok) throw new Error('frames'); renderFrames((await response.json()).frames || []); } catch (error) {} };
  const renderFrames = (frames) => { const target = byId('capture-list'); target.innerHTML = frames.length ? frames.map((frame) => `<div class="capture-row"><span class="capture-time">${escapeHtml(formatCaptureTime(frame.captured_at))}</span><span class="capture-name">${escapeHtml(frame.measurement_point_id || frame.device_id || '未命名测点')}</span><span class="capture-value">${frame.metric_value == null ? 'RAW' : escapeHtml(Number(frame.metric_value).toFixed(2))}</span></div>`).join('') : '<div class="capture-empty">暂无采集记录</div>'; };
  const setDialogRange = (start, end, startId, endId) => { byId(startId).value = toLocalInputValue(start); byId(endId).value = toLocalInputValue(end); };
  const queryDetailTrend = async () => {
    const start = new Date(byId('trend-start').value); const end = new Date(byId('trend-end').value); const status = byId('trend-query-status');
    if (!state.detailMetric || Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || start >= end) { status.textContent = '请选择有效的起止时间'; return; }
    status.textContent = '正在查询...'; const params = new URLSearchParams({ limit: '1000', start_at: start.toISOString(), end_at: end.toISOString(), metric_key: state.detailMetric });
    try { const response = await fetch(`/api/v1/dlt645/trends?${params}`, { cache: 'no-store' }); if (!response.ok) throw new Error('detail'); const result = await response.json(); const metric = (result.metrics || []).find((item) => item.key === state.detailMetric); const points = metric && metric.points ? metric.points : []; const config = metricConfig[state.detailMetric]; const chartPoints = chartPointsFor(state.detailMetric, points); chartFor('detail-chart').setOption(makeOption(chartPoints, config, true), true); status.textContent = chartPoints.length ? `${chartPoints.length} 个数据点` : '该时间段暂无数据'; } catch (error) { status.textContent = '趋势查询失败，请稍后重试'; }
  };
  const openTrendDialog = (metricKey) => { state.detailMetric = metricKey; const config = metricConfig[metricKey]; const range = getRange(); byId('trend-dialog-title').textContent = `${config.title}趋势`; setDialogRange(range.start, range.end, 'trend-start', 'trend-end'); byId('trend-query-status').textContent = ''; byId('trend-dialog').showModal(); window.setTimeout(() => { chartFor('detail-chart').resize(); queryDetailTrend(); }, 0); };
  const queryFrames = async () => {
    const start = new Date(byId('frame-start').value); const end = new Date(byId('frame-end').value); const status = byId('frame-query-status');
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || start >= end) { status.textContent = '请选择有效的起止时间'; return; }
    const params = new URLSearchParams({ limit: '100', summary: '0', total: '0', start_at: start.toISOString(), end_at: end.toISOString() }); const device = byId('frame-device').value.trim(); const direction = byId('frame-direction').value; if (device) params.set('device_id', device); if (direction) params.set('direction', direction); status.textContent = '正在查询...';
    try { const response = await fetch(`/api/v1/dlt645/frame?${params}`, { cache: 'no-store' }); if (!response.ok) throw new Error('frames'); const frames = (await response.json()).frames || []; byId('frame-query-results').innerHTML = frames.map((frame) => `<tr><td>${escapeHtml(formatDateTime(frame.captured_at))}</td><td><strong>${escapeHtml(frame.device_id || '--')}</strong><small>${escapeHtml(frame.measurement_point_id || '--')}</small></td><td>${escapeHtml(String(frame.direction || '').toUpperCase())}</td><td>${escapeHtml(frame.sequence)}</td><td><code>${escapeHtml(String(frame.frame_hex || '').replace(/\s|:/g, '').toUpperCase())}</code></td></tr>`).join(''); byId('frame-query-empty').hidden = frames.length > 0; status.textContent = `查询到 ${frames.length} 条原始帧`; } catch (error) { byId('frame-query-results').innerHTML = ''; byId('frame-query-empty').hidden = false; status.textContent = '原始帧查询失败，请稍后重试'; }
  };
  const openFrameQuery = () => { const end = new Date(); setDialogRange(new Date(end.getTime() - 24 * 60 * 60 * 1000), end, 'frame-start', 'frame-end'); byId('frame-query-status').textContent = ''; byId('frame-query-results').innerHTML = ''; byId('frame-query-empty').hidden = true; byId('frame-query-dialog').showModal(); window.setTimeout(queryFrames, 0); };
  const refreshAll = () => { loadTrends(); loadStatus(); loadEnergy(); loadFrames(); };
  document.querySelectorAll('.range-switch button').forEach((button) => button.addEventListener('click', () => { state.range = button.dataset.range; document.querySelectorAll('.range-switch button').forEach((item) => item.classList.toggle('is-active', item === button)); loadTrends(); }));
  document.querySelectorAll('.expand-button').forEach((button) => button.addEventListener('click', () => openTrendDialog(button.dataset.metric)));
  byId('trend-query-form').addEventListener('submit', (event) => { event.preventDefault(); queryDetailTrend(); }); byId('frame-query-form').addEventListener('submit', (event) => { event.preventDefault(); queryFrames(); });
  byId('open-frame-query').addEventListener('click', openFrameQuery); byId('close-trend-dialog').addEventListener('click', () => byId('trend-dialog').close()); byId('close-frame-query').addEventListener('click', () => byId('frame-query-dialog').close()); byId('refresh-button').addEventListener('click', refreshAll);
  window.addEventListener('resize', () => Object.values(state.charts).forEach((chart) => chart.resize())); refreshAll(); state.timers = [window.setInterval(loadTrends, 5000), window.setInterval(loadStatus, 5000), window.setInterval(loadEnergy, 60000), window.setInterval(loadFrames, 60000)];
})();
