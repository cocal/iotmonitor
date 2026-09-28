(() => {
  const state = {
    device: '',
    direction: '',
    timers: [],
    trendKey: null,
    filterTimer: null,
    trendCache: {},
    trendCacheDevice: '',
    trendCacheDay: null
  };
  const byId = (id) => document.getElementById(id);
  const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
  const formatTime = (value) => {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '--:--:--';
    return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  };
  const formatChartTime = (value) => {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '--/-- --:--';
    return date.toLocaleString('zh-CN', {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false
    }).replace(/\//g, '-');
  };
  const formatDate = (value) => {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '等待数据';
    return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
  };
  const toLocalInputValue = (date) => {
    const pad = (value) => String(value).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  };
  const isMobileViewport = () => typeof window !== 'undefined' && window.matchMedia('(max-width: 600px)').matches;
  const localDayKey = (date = new Date()) => {
    const pad = (value) => String(value).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  };
  const resetTrendCacheIfNeeded = (device) => {
    const today = localDayKey();
    if (state.trendCacheDevice !== device || state.trendCacheDay !== today) {
      state.trendCache = {};
      state.trendCacheDevice = device;
      state.trendCacheDay = today;
    }
  };
  const pointsFromToday = (points) => points
    .filter((point) => {
      const capturedAt = new Date(point.captured_at);
      return !Number.isNaN(capturedAt.getTime()) && localDayKey(capturedAt) === state.trendCacheDay;
    })
    .filter((point) => Number.isFinite(Number(point.value)))
    .sort((left, right) => new Date(left.captured_at) - new Date(right.captured_at))
    .slice(-60);

  const metricMap = {
    'voltage-a': { card: 'metric-voltage', time: 'metric-voltage-time', chart: 'chart-voltage', current: 'chart-voltage-value', color: '#0088a8', digits: 1 },
    'current-a': { card: 'metric-current', time: 'metric-current-time', chart: 'chart-current', current: 'chart-current-value', color: '#13845c', digits: 3 },
    'instantaneous-active-power': { card: 'metric-power', time: 'metric-power-time', chart: 'chart-power', current: 'chart-power-value', color: '#2563eb', digits: 1 },
    temperature: { chart: 'chart-temperature', current: 'chart-temperature-value', color: '#d97706', digits: 1 }
  };

  const smoothPath = (values, x, y) => {
    if (values.length < 2) return `M ${x(0).toFixed(2)} ${y(values[0]).toFixed(2)}`;
    const points = values.map((value, index) => ({ x: x(index), y: y(value) }));
    let path = `M ${points[0].x.toFixed(2)} ${points[0].y.toFixed(2)}`;
    for (let index = 0; index < points.length - 1; index += 1) {
      const previous = points[index - 1] || points[index];
      const current = points[index];
      const next = points[index + 1];
      const following = points[index + 2] || next;
      const control1 = { x: current.x + (next.x - previous.x) / 6, y: current.y + (next.y - previous.y) / 6 };
      const control2 = { x: next.x - (following.x - current.x) / 6, y: next.y - (following.y - current.y) / 6 };
      path += ` C ${control1.x.toFixed(2)} ${control1.y.toFixed(2)}, ${control2.x.toFixed(2)} ${control2.y.toFixed(2)}, ${next.x.toFixed(2)} ${next.y.toFixed(2)}`;
    }
    return path;
  };

  const renderChart = (target, points, color, unit, digits) => {
    if (!points.length) {
      target.classList.add('is-empty');
      target.innerHTML = '<span>等待有效测量数据</span>';
      return;
    }
    target.classList.remove('is-empty');
    const width = target.classList.contains('trend-chart-dialog')
      ? (isMobileViewport() ? 360 : 1000)
      : target.classList.contains('trend-chart-half') ? (isMobileViewport() ? 420 : 560)
      : (target.classList.contains('trend-chart-small') ? (isMobileViewport() ? 400 : 760) : (isMobileViewport() ? 420 : 1200));
    const height = target.classList.contains('trend-chart-small') ? 210 : target.classList.contains('trend-chart-half') ? 230 : 270;
    const margin = { top: 16, right: 14, bottom: 28, left: 50 };
    const values = points.map((point) => Number(point.value));
    const rawMin = Math.min(...values);
    const rawMax = Math.max(...values);
    const span = Math.max(rawMax - rawMin, Math.abs(rawMax) * 0.05, 1);
    const min = rawMin - span * 0.15;
    const max = rawMax + span * 0.15;
    const x = (index) => margin.left + index / Math.max(points.length - 1, 1) * (width - margin.left - margin.right);
    const y = (value) => margin.top + (1 - (value - min) / (max - min)) * (height - margin.top - margin.bottom);
    const path = smoothPath(values, x, y);
    const grid = Array.from({ length: 5 }, (_, index) => {
      const value = min + (max - min) * index / 4;
      const position = y(value);
      return `<line x1="${margin.left}" y1="${position}" x2="${width - margin.right}" y2="${position}"></line><text x="${margin.left - 8}" y="${position + 3}" text-anchor="end">${value.toFixed(digits)}</text>`;
    }).join('');
    const labels = points.map((point, index) => {
      if (index !== 0 && index !== points.length - 1 && index % Math.max(1, Math.ceil(points.length / 5)) !== 0) return '';
      return `<text x="${x(index)}" y="${height - 7}" text-anchor="middle">${escapeHtml(formatChartTime(point.captured_at))}</text>`;
    }).join('');
    const dotStep = Math.max(1, Math.ceil(points.length / 24));
    const dots = points.map((point, index) => {
      if (index !== points.length - 1 && index % dotStep !== 0) return '';
      return `<circle cx="${x(index)}" cy="${y(values[index])}" r="${index === points.length - 1 ? 4 : 2.5}" fill="${color}"><title>${escapeHtml(formatChartTime(point.captured_at))}: ${values[index].toFixed(digits)} ${unit}</title></circle>`;
    }).join('');
    target.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="xMidYMid meet" aria-hidden="true"><g class="chart-grid-lines">${grid}</g>${labels}<path class="chart-line" stroke="${color}" d="${path}"></path>${dots}</svg>`;
  };

  const renderMetric = (key, metric) => {
    const config = metricMap[key];
    const normalized = metric || { unit: '', points: [] };
    const latest = normalized.points[normalized.points.length - 1];
    const value = latest ? Number(latest.value).toFixed(config.digits) : '--';
    if (config.card) byId(config.card).textContent = value;
    if (config.current) byId(config.current).textContent = value;
    if (config.time) byId(config.time).textContent = latest ? `${formatTime(latest.captured_at)} 更新` : '等待有效帧';
    renderChart(byId(config.chart), normalized.points, config.color, normalized.unit, config.digits);
  };

  const renderMetrics = (metrics) => {
    Object.keys(metricMap).forEach((key) => {
      const metric = metrics.find((item) => item.key === key);
      renderMetric(key, metric);
    });
  };

  const renderCollectionChart = (target, collection) => {
    const points = collection && Array.isArray(collection.points) ? collection.points : [];
    if (!points.length) {
      target.classList.add('is-empty');
      target.innerHTML = '<span>等待采集结果</span>';
      return;
    }
    target.classList.remove('is-empty');
    const values = points;
    const width = target.classList.contains('trend-chart-dialog') ? (isMobileViewport() ? 360 : 1000) : target.classList.contains('trend-chart-half') ? (isMobileViewport() ? 420 : 560) : (isMobileViewport() ? 420 : 1200);
    const height = target.classList.contains('trend-chart-half') ? 230 : 270;
    const margin = { top: 16, right: 14, bottom: 28, left: 50 };
    const max = Math.max(1, ...values.flatMap((point) => [point.success, point.failure]));
    const x = (index) => margin.left + index / Math.max(values.length - 1, 1) * (width - margin.left - margin.right);
    const y = (value) => margin.top + (1 - value / max) * (height - margin.top - margin.bottom);
    const pathFor = (key) => smoothPath(values.map((point) => point[key]), x, y);
    const grid = Array.from({ length: 5 }, (_, index) => {
      const value = max * (4 - index) / 4;
      const position = y(value);
      return `<line x1="${margin.left}" y1="${position}" x2="${width - margin.right}" y2="${position}"></line><text x="${margin.left - 8}" y="${position + 3}" text-anchor="end">${Math.round(value)}</text>`;
    }).join('');
    const labels = values.map((point, index) => {
      if (index !== 0 && index !== values.length - 1 && index % Math.max(1, Math.ceil(values.length / 5)) !== 0) return '';
      return `<text x="${x(index)}" y="${height - 7}" text-anchor="middle">${escapeHtml(formatChartTime(point.captured_at))}</text>`;
    }).join('');
    target.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="xMidYMid meet" aria-hidden="true"><g class="chart-grid-lines">${grid}</g>${labels}<path class="chart-line" stroke="#13845c" d="${pathFor('success')}"></path><path class="chart-line" stroke="#c2413b" d="${pathFor('failure')}"></path></svg>`;
  };

  const renderDailyEnergyChart = (target, dailyEnergy) => {
    const points = Array.isArray(dailyEnergy) ? dailyEnergy : [];
    const validValues = points
      .map((point) => point.kwh)
      .filter((value) => value !== null && value !== '' && Number.isFinite(Number(value)))
      .map(Number);
    if (!points.length || !validValues.length) {
      target.classList.add('is-empty');
      target.innerHTML = '<span>等待累计电量日基线</span>';
      byId('chart-energy-total').textContent = '--';
      return;
    }
    target.classList.remove('is-empty');
    const width = target.classList.contains('trend-chart-dialog') ? (isMobileViewport() ? 360 : 1000) : target.classList.contains('trend-chart-half') ? (isMobileViewport() ? 420 : 560) : (isMobileViewport() ? 420 : 1200);
    const height = target.classList.contains('trend-chart-half') ? 230 : 270;
    const margin = { top: 16, right: 14, bottom: 34, left: 50 };
    const values = points.map((point) => point.kwh !== null && point.kwh !== '' && Number.isFinite(Number(point.kwh)) ? Math.max(0, Number(point.kwh)) : 0);
    const max = Math.max(0.1, ...values) * 1.15;
    const chartWidth = width - margin.left - margin.right;
    const chartHeight = height - margin.top - margin.bottom;
    const barGap = Math.max(3, chartWidth / points.length * 0.22);
    const barWidth = Math.max(4, chartWidth / points.length - barGap);
    const x = (index) => margin.left + index * (chartWidth / points.length) + barGap / 2;
    const y = (value) => margin.top + (1 - value / max) * chartHeight;
    const grid = Array.from({ length: 5 }, (_, index) => {
      const value = max * (4 - index) / 4;
      const position = y(value);
      return `<line x1="${margin.left}" y1="${position}" x2="${width - margin.right}" y2="${position}"></line><text x="${margin.left - 8}" y="${position + 3}" text-anchor="end">${value.toFixed(1)}</text>`;
    }).join('');
    const labelStep = Math.max(1, Math.ceil(points.length / 6));
    const bars = points.map((point, index) => {
      const value = values[index];
      const top = y(value);
      const label = String(point.date || '').slice(5);
      const axisLabel = index === 0 || index === points.length - 1 || index % labelStep === 0
        ? `<text x="${(x(index) + barWidth / 2).toFixed(2)}" y="${height - 9}" text-anchor="middle">${escapeHtml(label)}</text>` : '';
      const methodLabel = point.calculation_method === 'power-integration' ? '（功率积分估算）' : '（电表累计差值）';
      const detail = point.kwh === null || point.kwh === '' || !Number.isFinite(Number(point.kwh))
        ? `${point.date}: 无法计算（${point.quality || '缺少数据'}）`
        : `${point.date}: ${value.toFixed(3)} kWh ${methodLabel}`;
      const qualityClass = point.quality === 'ok'
        ? ''
        : point.quality === 'estimated' ? ' energy-bar-estimated' : ' energy-bar-missing';
      return `<rect class="energy-bar${qualityClass}" x="${x(index).toFixed(2)}" y="${top.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${Math.max(0, margin.top + chartHeight - top).toFixed(2)}" rx="2"><title>${escapeHtml(detail)}</title></rect>${axisLabel}`;
    }).join('');
    const total = values.reduce((sum, value) => sum + value, 0);
    byId('chart-energy-total').textContent = total.toFixed(3);
    target.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="xMidYMid meet" aria-hidden="true"><g class="chart-grid-lines">${grid}</g>${bars}</svg>`;
  };

  const applyEnergySummary = (summary) => {
    const hasNumber = (value) => value !== null && value !== '' && Number.isFinite(Number(value));
    const totalEnergy = summary && summary.total_energy_kwh;
    const dailyEnergy = summary && summary.daily_energy_kwh;
    byId('metric-total-energy').textContent = hasNumber(totalEnergy) ? Number(totalEnergy).toFixed(3) : '--';
    byId('metric-total-energy-time').textContent = hasNumber(totalEnergy)
      ? `${formatTime(summary.total_energy_at)} 电表累计读数`
      : '设备未上报累计电量';
    byId('metric-daily-energy').textContent = hasNumber(dailyEnergy) ? Number(dailyEnergy).toFixed(3) : '--';
    const qualityText = {
      'no-total-energy': '设备未上报累计电量',
      'no-reading': '今日尚无累计读数',
      'missing-baseline': '缺少昨日最后一笔读数',
      'counter-reset': '累计计数器已重置',
      estimated: '瞬时功率积分估算值',
      collecting: '正在累计功率采样'
    };
    byId('metric-daily-energy-foot').textContent = summary && summary.daily_energy_quality === 'ok'
      ? `${Number(summary.daily_energy_latest_kwh).toFixed(3)} − ${Number(summary.daily_energy_baseline_kwh).toFixed(3)} kWh`
      : (qualityText[summary && summary.daily_energy_quality] || '今日末值 − 昨日末值');
    renderDailyEnergyChart(byId('chart-daily-energy'), summary && summary.daily_energy);
  };

  const renderFrames = (frames, collection = null) => {
    const rows = byId('frame-rows');
    renderCollectionChart(byId('chart-collection'), collection);
    byId('row-count').textContent = `${frames.length} 条`;
    byId('empty-state').hidden = frames.length > 0;
    rows.innerHTML = frames.map((frame) => {
      const direction = frame.direction === 'rx' ? 'rx' : 'tx';
      const hex = frame.frame_hex.replace(/\s|:/g, '').toUpperCase();
      return `<tr><td><time datetime="${escapeHtml(frame.captured_at)}">${escapeHtml(formatTime(frame.captured_at))}<small>${escapeHtml(formatDate(frame.captured_at))}</small></time></td><td><strong>${escapeHtml(frame.device_id)}</strong><small>${escapeHtml(frame.measurement_point_id)}</small></td><td><span class="direction direction-${direction}">${direction.toUpperCase()}</span></td><td><code>${escapeHtml(frame.sequence)}</code></td><td><code class="hex-preview">${escapeHtml(hex)}</code></td><td><button class="row-action" type="button" data-frame-id="${escapeHtml(frame.id)}">查看</button></td></tr>`;
    }).join('');
    byId('footer-updated').textContent = `同步于 ${new Date().toLocaleTimeString('zh-CN')}`;
    rows.querySelectorAll('.row-action').forEach((button) => button.addEventListener('click', () => showDetail(frames.find((frame) => String(frame.id) === button.dataset.frameId))));
  };

  const showDetail = (frame) => {
    if (!frame) return;
    byId('frame-detail').innerHTML = [['设备', frame.device_id], ['测点', frame.measurement_point_id], ['指标', frame.metric_key || 'raw-frame'], ['方向', frame.direction.toUpperCase()], ['序列', frame.sequence], ['捕获时间', frame.captured_at], ['接收时间', frame.received_at], ['请求 ID', frame.request_id]].map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`).join('');
    byId('dialog-hex').textContent = frame.frame_hex.replace(/\s|:/g, '').toUpperCase();
    const dialog = byId('frame-dialog');
    if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
  };

  const trendTitles = {
    'instantaneous-active-power': '瞬时有功功率趋势',
    'voltage-a': 'A 相电压趋势',
    'current-a': 'A 相电流趋势',
    temperature: '模块温度趋势',
    collection: '采集成功 / 失败趋势',
    'daily-energy': '每日累计用电量'
  };

  const setTrendWindow = (windowKey) => {
    const end = new Date();
    const hours = { '1h': 1, '6h': 6, '24h': 24, '7d': 24 * 7 }[windowKey] || 1;
    const start = new Date(end.getTime() - hours * 60 * 60 * 1000);
    byId('trend-start').value = toLocalInputValue(start);
    byId('trend-end').value = toLocalInputValue(end);
    document.querySelectorAll('.trend-range button').forEach((button) => button.classList.toggle('is-active', button.dataset.window === windowKey));
  };

  const loadExpandedTrend = async () => {
    const startValue = byId('trend-start').value;
    const endValue = byId('trend-end').value;
    const start = new Date(startValue);
    const end = new Date(endValue);
    const chart = byId('trend-dialog-chart');
    const status = byId('trend-dialog-status');
    if (!startValue || !endValue || Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || start >= end) {
      status.textContent = '请选择有效的起止时间';
      return;
    }
    const isDailyEnergy = state.trendKey === 'daily-energy';
    const isCollection = state.trendKey === 'collection';
    const params = new URLSearchParams(isDailyEnergy
      ? { days: '30', start_at: start.toISOString(), end_at: end.toISOString() }
      : isCollection
      ? { limit: '1000', view: 'trend', summary: '0', total: '0', start_at: start.toISOString(), end_at: end.toISOString() }
      : { limit: '1000', start_at: start.toISOString(), end_at: end.toISOString(), metric_key: state.trendKey });
    if (state.device) params.set('device_id', state.device);
    if (state.direction && isCollection) params.set('direction', state.direction);
    status.textContent = '正在查询…';
    try {
      const endpoint = isDailyEnergy
        ? '/api/v1/dlt645/summary'
        : isCollection ? '/api/v1/dlt645/frame' : '/api/v1/dlt645/trends';
      const response = await fetch(`${endpoint}?${params.toString()}`, { headers: { Accept: 'application/json' }, cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      if (state.trendKey === 'daily-energy') {
        renderDailyEnergyChart(chart, result.daily_energy || []);
      } else if (state.trendKey === 'collection') {
        renderCollectionChart(chart, result.collection || null);
      } else {
        const metric = (result.metrics || []).find((item) => item.key === state.trendKey) || { points: [], unit: '' };
        const config = metricMap[state.trendKey] || { color: '#2563eb', digits: 1 };
        renderChart(chart, metric.points || [], config.color, metric.unit, config.digits);
      }
      const count = state.trendKey === 'daily-energy'
        ? (result.daily_energy || []).length
        : state.trendKey === 'collection'
        ? ((result.collection && result.collection.points) || []).length
        : (((result.metrics || []).find((item) => item.key === state.trendKey) || {}).points || []).length;
      status.textContent = `${start.toLocaleString('zh-CN')} 至 ${end.toLocaleString('zh-CN')} · ${count} 个点`;
    } catch (error) {
      chart.classList.add('is-empty');
      chart.innerHTML = '<span>趋势查询失败</span>';
      status.textContent = '无法读取指定时间段数据';
    }
  };

  const openTrend = (trendKey) => {
    state.trendKey = trendKey;
    byId('trend-dialog-title').textContent = trendTitles[trendKey] || '趋势详情';
    setTrendWindow('1h');
    const dialog = byId('trend-dialog');
    if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
    loadExpandedTrend();
  };

  const loadStatus = async () => {
    const requestedDevice = state.device;
    const params = new URLSearchParams();
    if (requestedDevice) params.set('device_id', requestedDevice);
    try {
      const response = await fetch(`/api/v1/dlt645/status?${params.toString()}`, { headers: { Accept: 'application/json' }, cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      if (state.device !== requestedDevice) return;
      byId('metric-total').textContent = Number(result.total_frames || 0).toLocaleString('zh-CN');
      byId('metric-latest-date').textContent = result.last_received_at
        ? `${formatTime(result.last_received_at)} 最近接收`
        : '等待数据';
      byId('connection-label').textContent = result.online ? '设备在线' : '设备离线';
      byId('sync-label').textContent = '归档正常';
    } catch (error) {
      byId('connection-label').textContent = '状态异常';
      byId('sync-label').textContent = '等待 API';
    }
  };

  const loadFrames = async () => {
    const requestKey = `${state.device}|${state.direction}`;
    const params = new URLSearchParams({ limit: '100', summary: '0', total: '0' });
    if (state.device) params.set('device_id', state.device);
    if (state.direction) params.set('direction', state.direction);
    try {
      const response = await fetch(`/api/v1/dlt645/frame?${params.toString()}`, { headers: { Accept: 'application/json' }, cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      if (`${state.device}|${state.direction}` !== requestKey) return;
      renderFrames(result.frames || [], result.collection || null);
    } catch (error) {
      byId('toolbar-status').textContent = '最近报文读取失败 · 状态仍会继续刷新';
    }
  };

  const loadTrends = async () => {
    const requestedDevice = state.device;
    resetTrendCacheIfNeeded(requestedDevice);
    const end = new Date();
    // 首次打开或当天缓存为空时查询当天数据，设备长时间掉线后刷新页面仍能恢复最后趋势点。
    const start = Object.keys(state.trendCache).length
      ? new Date(end.getTime() - 60 * 60 * 1000)
      : new Date(end.getFullYear(), end.getMonth(), end.getDate());
    const params = new URLSearchParams({ limit: '1000', start_at: start.toISOString(), end_at: end.toISOString() });
    ['voltage-a', 'current-a', 'instantaneous-active-power', 'temperature'].forEach((key) => params.append('metric_key', key));
    if (requestedDevice) params.set('device_id', requestedDevice);
    try {
      const response = await fetch(`/api/v1/dlt645/trends?${params.toString()}`, { headers: { Accept: 'application/json' }, cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      if (state.device !== requestedDevice) return;
      const metrics = result.metrics || [];
      Object.keys(metricMap).forEach((key) => {
        const metric = metrics.find((item) => item.key === key);
        const points = metric ? pointsFromToday(metric.points || []) : [];
        if (points.length) state.trendCache[key] = points;
      });
      renderMetrics(Object.keys(metricMap).map((key) => ({
        key,
        unit: (metrics.find((item) => item.key === key) || {}).unit || '',
        points: state.trendCache[key] || []
      })));
    } catch (error) {
      renderMetrics(Object.keys(metricMap).map((key) => ({
        key,
        unit: '',
        points: state.trendCache[key] || []
      })));
    }
  };

  const loadEnergy = async () => {
    const requestedDevice = state.device;
    const params = new URLSearchParams({ days: '30' });
    if (requestedDevice) params.set('device_id', requestedDevice);
    try {
      const response = await fetch(`/api/v1/dlt645/summary?${params.toString()}`, { headers: { Accept: 'application/json' }, cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      if (state.device === requestedDevice) applyEnergySummary(result);
    } catch (error) {
      byId('metric-total-energy-time').textContent = '累计电量读取失败';
    }
  };

  const refreshAll = () => {
    byId('toolbar-status').textContent = '状态 5 秒 · 报文 20 秒 · 趋势 5 秒 / 最近 60 点 · 电量 60 秒';
    loadStatus();
    loadFrames();
    loadTrends();
    loadEnergy();
  };

  const scheduleFilterRefresh = () => {
    if (state.filterTimer) window.clearTimeout(state.filterTimer);
    state.filterTimer = window.setTimeout(refreshAll, 300);
  };

  byId('device-filter').addEventListener('input', (event) => { state.device = event.target.value.trim(); scheduleFilterRefresh(); });
  byId('direction-filter').addEventListener('change', (event) => { state.direction = event.target.value; loadFrames(); });
  byId('clear-button').addEventListener('click', () => { state.device = ''; state.direction = ''; byId('device-filter').value = ''; byId('direction-filter').value = ''; refreshAll(); });
  byId('refresh-button').addEventListener('click', refreshAll);
  byId('close-dialog').addEventListener('click', () => byId('frame-dialog').close());
  byId('frame-dialog').addEventListener('click', (event) => { if (event.target === byId('frame-dialog')) byId('frame-dialog').close(); });
  document.querySelectorAll('.chart-expand').forEach((button) => button.addEventListener('click', () => openTrend(button.dataset.trend)));
  document.querySelectorAll('.trend-range button').forEach((button) => button.addEventListener('click', () => { setTrendWindow(button.dataset.window); loadExpandedTrend(); }));
  byId('trend-query').addEventListener('click', loadExpandedTrend);
  byId('close-trend-dialog').addEventListener('click', () => byId('trend-dialog').close());
  byId('trend-dialog').addEventListener('click', (event) => { if (event.target === byId('trend-dialog')) byId('trend-dialog').close(); });
  refreshAll();
  state.timers = [
    window.setInterval(loadStatus, 5000),
    window.setInterval(loadFrames, 20000),
    window.setInterval(loadTrends, 5000),
    window.setInterval(loadEnergy, 60000)
  ];
})();
