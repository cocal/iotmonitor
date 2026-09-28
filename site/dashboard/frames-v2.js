(() => {
  const state = { hours: 1, charts: {}, timer: null };
  const metricKeys = ['voltage-a', 'current-a', 'instantaneous-active-power', 'temperature'];
  const metricConfig = {
    'voltage-a': { chart: 'voltage-chart', panel: 'panel-voltage', kpi: 'kpi-voltage', time: 'kpi-voltage-time', color: '#0395ad', unit: 'V', digits: 1, min: 0, max: 200, interval: 20 },
    'current-a': { chart: 'current-chart', panel: 'panel-current', kpi: 'kpi-current', time: 'kpi-current-time', color: '#15946b', unit: 'A', digits: 3 },
    'instantaneous-active-power': { chart: 'power-chart', panel: null, kpi: 'kpi-power', time: 'kpi-power-time', color: '#2c6bed', unit: 'W', digits: 1, min: 0, max: 2000, interval: 100 },
    temperature: { chart: 'temperature-chart', panel: 'panel-temperature', kpi: null, time: null, color: '#bb6d13', unit: '°C', digits: 1 }
  };
  const byId = (id) => document.getElementById(id);
  const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
  const formatTime = (value) => { const date = new Date(value); return Number.isNaN(date.getTime()) ? '--:--' : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }); };
  const formatDateTime = (value) => { const date = new Date(value); return Number.isNaN(date.getTime()) ? '等待数据' : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }); };
  const setStatus = (online, text) => { byId('status-text').textContent = text; byId('status-dot').parentElement.classList.toggle('is-offline', !online); };
  const makeOption = (points, config) => {
    const values = points.map((point) => [new Date(point.captured_at).getTime(), Number(point.value)]);
    const dataMax = values.length ? Math.max(...values.map((point) => point[1])) : 0;
    const max = config.max ? (config.max === 2000 ? Math.min(2000, Math.max(200, Math.ceil(dataMax * 1.2 / 100) * 100)) : config.max) : undefined;
    return { animation: false, grid: { left: 56, right: 18, top: 22, bottom: 35 }, tooltip: { trigger: 'axis', confine: true, valueFormatter: (value) => `${Number(value).toFixed(config.digits)} ${config.unit}` }, xAxis: { type: 'time', axisLabel: { color: '#7b899b', fontSize: 10 }, axisLine: { lineStyle: { color: '#cbd6e2' } }, splitLine: { show: false } }, yAxis: { type: 'value', min: config.min, max, interval: config.interval, axisLabel: { color: '#7b899b', fontSize: 10, formatter: (value) => Number(value).toFixed(config.digits) }, axisLine: { show: true, lineStyle: { color: '#cbd6e2' } }, splitLine: { lineStyle: { color: '#e8eef4' } } }, series: [{ type: 'line', smooth: .22, showSymbol: false, symbolSize: 7, data: values, lineStyle: { width: 2.5, color: config.color }, itemStyle: { color: config.color }, areaStyle: { color: config.color, opacity: .08 } }] };
  };
  const renderMetric = (key, metric) => {
    const config = metricConfig[key]; const points = metric && metric.points ? metric.points.filter((point) => Number.isFinite(Number(point.value))) : []; const latest = points[points.length - 1]; const value = latest ? Number(latest.value).toFixed(config.digits) : '--';
    if (config.kpi) byId(config.kpi).textContent = value;
    if (config.panel) byId(config.panel).textContent = `${value} ${config.unit}`;
    if (config.time) byId(config.time).textContent = latest ? `${formatDateTime(latest.captured_at)} 更新` : '等待有效数据';
    if (!state.charts[config.chart]) state.charts[config.chart] = echarts.init(byId(config.chart));
    state.charts[config.chart].setOption(makeOption(points, config), true);
  };
  const renderEnergy = (summary) => {
    const total = Number(summary.total_energy_kwh); const daily = Number(summary.daily_energy_kwh); const valid = (value) => Number.isFinite(value);
    byId('total-energy').textContent = valid(total) ? total.toFixed(3) : '--'; byId('daily-energy').textContent = valid(daily) ? daily.toFixed(3) : '--'; byId('total-income').textContent = valid(total) ? (total * .52).toFixed(2) : '--'; byId('daily-income').textContent = valid(daily) ? (daily * .52).toFixed(2) : '--';
    const points = (summary.daily_energy || []).filter((point) => Number.isFinite(Number(point.kwh))).map((point) => [point.date, Number(point.kwh)]);
    if (!state.charts.energy) state.charts.energy = echarts.init(byId('energy-chart'));
    state.charts.energy.setOption({ animation: false, grid: { left: 48, right: 12, top: 15, bottom: 30 }, tooltip: { trigger: 'axis', valueFormatter: (value) => `${Number(value).toFixed(3)} kWh` }, xAxis: { type: 'category', data: points.map((point) => point[0].slice(5)), axisLabel: { color: '#7b899b', fontSize: 10 } }, yAxis: { type: 'value', axisLabel: { color: '#7b899b', fontSize: 10 }, splitLine: { lineStyle: { color: '#e8eef4' } } }, series: [{ type: 'bar', barMaxWidth: 22, data: points.map((point) => point[1]), itemStyle: { color: '#7258d6', borderRadius: [4, 4, 0, 0] } }] }, true);
  };
  const load = async () => {
    const end = new Date(); const start = new Date(end.getTime() - state.hours * 60 * 60 * 1000); const params = new URLSearchParams({ limit: '1000', start_at: start.toISOString(), end_at: end.toISOString() }); metricKeys.forEach((key) => params.append('metric_key', key));
    try {
      const [trendResponse, energyResponse, statusResponse, frameResponse] = await Promise.all([fetch(`/api/v1/dlt645/trends?${params}`, { cache: 'no-store' }), fetch('/api/v1/dlt645/summary?days=30', { cache: 'no-store' }), fetch('/api/v1/dlt645/status', { cache: 'no-store' }), fetch('/api/v1/dlt645/frame?limit=8&summary=0&total=0', { cache: 'no-store' })]);
      if (!trendResponse.ok || !energyResponse.ok || !statusResponse.ok || !frameResponse.ok) throw new Error('api');
      const trends = await trendResponse.json(); const energy = await energyResponse.json(); const status = await statusResponse.json(); const frames = await frameResponse.json();
      metricKeys.forEach((key) => renderMetric(key, (trends.metrics || []).find((metric) => metric.key === key))); renderEnergy(energy); byId('last-received').textContent = status.last_received_at ? `${formatDateTime(status.last_received_at)} 最近接收` : '等待数据'; setStatus(Boolean(status.online), status.online ? '设备在线' : '设备离线'); renderFrames(frames.frames || []);
    } catch (error) { setStatus(false, '接口暂不可用'); }
  };
  const renderFrames = (frames) => { const target = byId('capture-list'); target.innerHTML = frames.length ? frames.map((frame) => `<div class="capture-row"><span class="capture-time">${escapeHtml(formatTime(frame.captured_at))}</span><span class="capture-name">${escapeHtml(frame.measurement_point_id || frame.device_id || '未命名测点')}</span><span class="capture-value">${frame.metric_value == null ? 'RAW' : escapeHtml(Number(frame.metric_value).toFixed(2))}</span></div>`).join('') : '<div class="capture-empty">暂无采集记录</div>'; };
  document.querySelectorAll('.range-switch button').forEach((button) => button.addEventListener('click', () => { state.hours = Number(button.dataset.hours); document.querySelectorAll('.range-switch button').forEach((item) => item.classList.toggle('is-active', item === button)); load(); }));
  byId('refresh-button').addEventListener('click', load); window.addEventListener('resize', () => Object.values(state.charts).forEach((chart) => chart.resize())); load(); state.timer = window.setInterval(load, 5000);
})();
