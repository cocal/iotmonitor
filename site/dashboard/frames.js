(() => {
  const state = { device: '', direction: '', timer: null };
  const byId = (id) => document.getElementById(id);
  const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
  const formatTime = (value) => {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '--:--:--';
    return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  };
  const formatDate = (value) => {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '等待数据';
    return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
  };

  const metricMap = {
    'voltage-a': { card: 'metric-voltage', time: 'metric-voltage-time', chart: 'chart-voltage', current: 'chart-voltage-value', color: '#0088a8', digits: 1 },
    'current-a': { card: 'metric-current', time: 'metric-current-time', chart: 'chart-current', current: 'chart-current-value', color: '#13845c', digits: 3 },
    'instantaneous-active-power': { card: 'metric-power', time: 'metric-power-time', chart: 'chart-power', current: 'chart-power-value', color: '#2563eb', digits: 1 }
  };

  const renderChart = (target, points, color, unit, digits) => {
    if (!points.length) {
      target.classList.add('is-empty');
      target.innerHTML = '<span>等待有效测量数据</span>';
      return;
    }
    target.classList.remove('is-empty');
    const width = 760;
    const height = target.classList.contains('trend-chart-small') ? 210 : 270;
    const margin = { top: 16, right: 14, bottom: 28, left: 50 };
    const values = points.map((point) => Number(point.value));
    const rawMin = Math.min(...values);
    const rawMax = Math.max(...values);
    const span = Math.max(rawMax - rawMin, Math.abs(rawMax) * 0.05, 1);
    const min = rawMin - span * 0.15;
    const max = rawMax + span * 0.15;
    const x = (index) => margin.left + index / Math.max(points.length - 1, 1) * (width - margin.left - margin.right);
    const y = (value) => margin.top + (1 - (value - min) / (max - min)) * (height - margin.top - margin.bottom);
    const path = values.map((value, index) => `${index ? 'L' : 'M'} ${x(index).toFixed(2)} ${y(value).toFixed(2)}`).join(' ');
    const grid = Array.from({ length: 5 }, (_, index) => {
      const value = min + (max - min) * index / 4;
      const position = y(value);
      return `<line x1="${margin.left}" y1="${position}" x2="${width - margin.right}" y2="${position}"></line><text x="${margin.left - 8}" y="${position + 3}" text-anchor="end">${value.toFixed(digits)}</text>`;
    }).join('');
    const labels = points.map((point, index) => {
      if (index !== 0 && index !== points.length - 1 && index % Math.max(1, Math.ceil(points.length / 5)) !== 0) return '';
      return `<text x="${x(index)}" y="${height - 7}" text-anchor="middle">${escapeHtml(formatTime(point.captured_at))}</text>`;
    }).join('');
    const dots = points.map((point, index) => `<circle cx="${x(index)}" cy="${y(values[index])}" r="${index === points.length - 1 ? 4 : 2.5}" fill="${color}"><title>${escapeHtml(formatTime(point.captured_at))}: ${values[index].toFixed(digits)} ${unit}</title></circle>`).join('');
    target.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true"><g class="chart-grid-lines">${grid}</g>${labels}<path class="chart-line" stroke="${color}" d="${path}"></path>${dots}</svg>`;
  };

  const renderMetrics = (metrics) => {
    Object.entries(metricMap).forEach(([key, config]) => {
      const metric = metrics.find((item) => item.key === key) || { unit: '', points: [] };
      const latest = metric.points[metric.points.length - 1];
      const value = latest ? Number(latest.value).toFixed(config.digits) : '--';
      byId(config.card).textContent = value;
      byId(config.current).textContent = value;
      byId(config.time).textContent = latest ? `${formatTime(latest.captured_at)} 更新` : '等待有效帧';
      renderChart(byId(config.chart), metric.points, config.color, metric.unit, config.digits);
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
    const width = 760;
    const height = 270;
    const margin = { top: 16, right: 14, bottom: 28, left: 50 };
    const max = Math.max(1, ...values.flatMap((point) => [point.success, point.failure]));
    const x = (index) => margin.left + index / Math.max(values.length - 1, 1) * (width - margin.left - margin.right);
    const y = (value) => margin.top + (1 - value / max) * (height - margin.top - margin.bottom);
    const pathFor = (key) => values.map((point, index) => `${index ? 'L' : 'M'} ${x(index).toFixed(2)} ${y(point[key]).toFixed(2)}`).join(' ');
    const grid = Array.from({ length: 5 }, (_, index) => {
      const value = max * (4 - index) / 4;
      const position = y(value);
      return `<line x1="${margin.left}" y1="${position}" x2="${width - margin.right}" y2="${position}"></line><text x="${margin.left - 8}" y="${position + 3}" text-anchor="end">${Math.round(value)}</text>`;
    }).join('');
    const labels = values.map((point, index) => {
      if (index !== 0 && index !== values.length - 1 && index % Math.max(1, Math.ceil(values.length / 5)) !== 0) return '';
      return `<text x="${x(index)}" y="${height - 7}" text-anchor="middle">${escapeHtml(formatTime(point.captured_at))}</text>`;
    }).join('');
    target.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true"><g class="chart-grid-lines">${grid}</g>${labels}<path class="chart-line" stroke="#13845c" d="${pathFor('success')}"></path><path class="chart-line" stroke="#c2413b" d="${pathFor('failure')}"></path></svg>`;
  };

  const render = (frames, total, metrics = [], collection = null) => {
    const rows = byId('frame-rows');
    const latest = frames[0];
    byId('metric-total').textContent = total.toLocaleString('zh-CN');
    byId('metric-latest-date').textContent = latest ? `${formatTime(latest.received_at)} 最近接收` : '等待数据';
    renderMetrics(metrics);
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
    byId('frame-detail').innerHTML = [['设备', frame.device_id], ['测点', frame.measurement_point_id], ['方向', frame.direction.toUpperCase()], ['序列', frame.sequence], ['捕获时间', frame.captured_at], ['接收时间', frame.received_at], ['请求 ID', frame.request_id]].map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`).join('');
    byId('dialog-hex').textContent = frame.frame_hex.replace(/\s|:/g, '').toUpperCase();
    const dialog = byId('frame-dialog');
    if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
  };

  const load = async () => {
    const params = new URLSearchParams({ limit: '100' });
    if (state.device) params.set('device_id', state.device);
    if (state.direction) params.set('direction', state.direction);
    try {
      const response = await fetch(`/api/v1/dlt645/frame?${params.toString()}`, { headers: { Accept: 'application/json' }, cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      render(result.frames || [], result.total || 0, result.metrics || [], result.collection || null);
      byId('connection-label').textContent = '实时连接';
      byId('sync-label').textContent = '归档正常';
      byId('toolbar-status').textContent = `每 5 秒自动刷新 · 共 ${result.total || 0} 条`;
    } catch (error) {
      byId('connection-label').textContent = '连接异常';
      byId('sync-label').textContent = '等待 API';
      byId('toolbar-status').textContent = '无法读取原始帧 API';
      render([], 0);
    }
  };

  byId('device-filter').addEventListener('input', (event) => { state.device = event.target.value.trim(); load(); });
  byId('direction-filter').addEventListener('change', (event) => { state.direction = event.target.value; load(); });
  byId('clear-button').addEventListener('click', () => { state.device = ''; state.direction = ''; byId('device-filter').value = ''; byId('direction-filter').value = ''; load(); });
  byId('refresh-button').addEventListener('click', load);
  byId('close-dialog').addEventListener('click', () => byId('frame-dialog').close());
  byId('frame-dialog').addEventListener('click', (event) => { if (event.target === byId('frame-dialog')) byId('frame-dialog').close(); });
  load();
  state.timer = window.setInterval(load, 5000);
})();
