(() => {
  const palette = {
    power: '#2563eb',
    current: '#13845c',
    voltage: '#0088a8',
    energy: '#bf7100'
  };

  const state = {
    range: '24h',
    nudge: 0
  };

  const rangeConfig = {
    '24h': { count: 25, step: 1, label: '时', labelEvery: 4 },
    '7d': { count: 29, step: 6, label: '日', labelEvery: 4 },
    '30d': { count: 31, step: 24, label: '日', labelEvery: 5 }
  };

  const byId = (id) => document.getElementById(id);

  const formatNumber = (value, digits = 0) => value.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  });

  const makeSeries = () => {
    const config = rangeConfig[state.range];
    const labels = [];
    const power = [];
    const current = [];
    const voltage = [];
    const energy = [];
    let cumulative = state.range === '30d' ? 98.4 : state.range === '7d' ? 120.2 : 127.9;

    for (let index = 0; index < config.count; index += 1) {
      const daylight = Math.max(0, Math.sin(((index / (config.count - 1)) * Math.PI) - 0.38));
      const ripple = Math.sin(index * 1.47 + state.nudge * 0.08) * 34;
      const powerValue = Math.max(20, 360 + daylight * 1060 + ripple);
      const currentValue = Math.max(0.2, powerValue / 224 + Math.sin(index * 0.74) * 0.1);
      const voltageValue = 229.2 + Math.sin(index * 0.68 + 0.5) * 1.3 + Math.cos(index * 0.21) * 0.35;
      cumulative += state.range === '24h' ? powerValue / 1000 * 0.36 : powerValue / 1000 * 0.12;
      const date = new Date(Date.now() - ((config.count - index - 1) * config.step * 60 * 60 * 1000));
      labels.push(state.range === '24h'
        ? `${String(date.getHours()).padStart(2, '0')}:00`
        : `${date.getMonth() + 1}/${date.getDate()}`);
      power.push(powerValue);
      current.push(currentValue);
      voltage.push(voltageValue);
      energy.push(cumulative);
    }

    return { labels, power, current, voltage, energy };
  };

  const escapeAttribute = (value) => String(value).replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[character]));

  const renderChart = (target, values, labels, color, unit, digits = 0, showArea = true) => {
    const width = 760;
    const height = target.classList.contains('chart-wrap-small') ? 205 : 276;
    const margin = { top: 16, right: 14, bottom: 28, left: 49 };
    const chartWidth = width - margin.left - margin.right;
    const chartHeight = height - margin.top - margin.bottom;
    const rawMin = Math.min(...values);
    const rawMax = Math.max(...values);
    const span = Math.max(rawMax - rawMin, rawMax * 0.08, 1);
    const min = rawMin - span * 0.18;
    const max = rawMax + span * 0.18;
    const x = (index) => margin.left + (index / (values.length - 1)) * chartWidth;
    const y = (value) => margin.top + (1 - ((value - min) / (max - min))) * chartHeight;
    const linePath = values.map((value, index) => `${index === 0 ? 'M' : 'L'} ${x(index).toFixed(2)} ${y(value).toFixed(2)}`).join(' ');
    const areaPath = `${linePath} L ${x(values.length - 1).toFixed(2)} ${(height - margin.bottom).toFixed(2)} L ${x(0).toFixed(2)} ${(height - margin.bottom).toFixed(2)} Z`;
    const tickCount = 4;
    const grid = [];
    for (let index = 0; index <= tickCount; index += 1) {
      const value = min + ((max - min) * index / tickCount);
      const yPosition = y(value);
      grid.push(`<line class="chart-grid-line" x1="${margin.left}" y1="${yPosition.toFixed(2)}" x2="${width - margin.right}" y2="${yPosition.toFixed(2)}"></line>`);
      grid.push(`<text class="chart-axis-label" x="${margin.left - 9}" y="${(yPosition + 3).toFixed(2)}" text-anchor="end">${escapeAttribute(formatNumber(value, digits))}</text>`);
    }

    const labelEvery = rangeConfig[state.range].labelEvery;
    const xLabels = labels.map((label, index) => {
      if (index % labelEvery !== 0 && index !== labels.length - 1) return '';
      return `<text class="chart-axis-label" x="${x(index).toFixed(2)}" y="${height - 7}" text-anchor="middle">${escapeAttribute(label)}</text>`;
    }).join('');
    const points = values.map((value, index) => (
      `<circle class="chart-point" cx="${x(index).toFixed(2)}" cy="${y(value).toFixed(2)}" r="${index === values.length - 1 ? 4 : 2.6}" fill="${color}"><title>${escapeAttribute(labels[index])}: ${escapeAttribute(formatNumber(value, digits))} ${unit}</title></circle>`
    )).join('');
    const description = `最近 ${labels.length} 个采样点，${formatNumber(values[values.length - 1], digits)} ${unit}`;
    target.setAttribute('aria-label', description);
    target.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="${escapeAttribute(description)}">
      <title>${escapeAttribute(description)}</title>
      ${grid.join('')}
      ${xLabels}
      ${showArea ? `<path class="chart-area" fill="${color}" d="${areaPath}"></path>` : ''}
      <path class="chart-line" stroke="${color}" d="${linePath}"></path>
      ${points}
    </svg>`;
  };

  const render = () => {
    const series = makeSeries();
    const latest = series.power.length - 1;
    renderChart(byId('power-chart'), series.power, series.labels, palette.power, 'W', 0, true);
    renderChart(byId('current-chart'), series.current, series.labels, palette.current, 'A', 2, false);
    renderChart(byId('voltage-chart'), series.voltage, series.labels, palette.voltage, 'V', 1, false);
    renderChart(byId('energy-chart'), series.energy, series.labels, palette.energy, 'kWh', 2, true);

    byId('current-power').textContent = formatNumber(series.power[latest], 0);
    byId('power-panel-value').textContent = formatNumber(series.power[latest], 0);
    byId('today-energy').textContent = formatNumber(series.energy[latest] - series.energy[0], 2);
    byId('energy-panel-value').textContent = formatNumber(series.energy[latest], 2);
    byId('current-current').textContent = formatNumber(series.current[latest], 2);
    byId('current-panel-value').textContent = formatNumber(series.current[latest], 2);
    byId('current-voltage').textContent = formatNumber(series.voltage[latest], 1);
    byId('voltage-panel-value').textContent = formatNumber(series.voltage[latest], 1);
    byId('last-updated').textContent = `更新于 ${new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
    byId('accessible-data').innerHTML = series.labels.slice(-6).map((label, offset) => {
      const index = series.labels.length - 6 + offset;
      return `<tr><th scope="row">${escapeAttribute(label)}</th><td>${formatNumber(series.power[index], 0)}</td><td>${formatNumber(series.current[index], 2)}</td><td>${formatNumber(series.voltage[index], 1)}</td><td>${formatNumber(series.energy[index], 2)}</td></tr>`;
    }).join('');
  };

  document.querySelectorAll('.range-button').forEach((button) => {
    button.addEventListener('click', () => {
      state.range = button.dataset.range;
      document.querySelectorAll('.range-button').forEach((item) => {
        const active = item === button;
        item.classList.toggle('is-active', active);
        item.setAttribute('aria-pressed', String(active));
      });
      render();
    });
  });

  byId('refresh-button').addEventListener('click', () => {
    state.nudge += 1;
    render();
  });

  render();
  window.setInterval(() => {
    state.nudge += 1;
    render();
  }, 5000);
})();
