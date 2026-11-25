(function () {
  var state = { payload: null, kind: 'bars', run: null, approved: false };
  var $ = function (id) { return document.getElementById(id); };
  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch];
    });
  }
  async function request(url, options) {
    var response = await fetch(url, Object.assign({headers: {'Content-Type': 'application/json'}}, options || {}));
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
  }
  function setNotice(text) { $('notice').textContent = text || ''; }
  function renderIssues(data) {
    $('issue-count').textContent = data.issues.length + ' 条';
    $('issue-list').innerHTML = data.issues.length ? data.issues.map(function (item) {
      return '<li><span>' + esc(item.line || '-') + '</span><span>' + esc(item.key || '无主键') + '</span><code>' + esc(item.code) + '</code></li>';
    }).join('') : '<li class="empty">暂无异常</li>';
  }
  function renderBreakdown(items) {
    if (!items || !items.length) return '';
    return '<details><summary>' + items.length + ' 项</summary><ul>' + items.map(function (item) {
      return '<li>' + esc(item) + '</li>';
    }).join('') + '</ul></details>';
  }
  function renderRows(data) {
    var labels = Object.assign({label: '记录'}, state.resultColumns);
    var source = data.row_kind === 'source';
    var hasBreakdown = data.rows.some(function (row) { return row.breakdown && row.breakdown.length; });
    var keys = source ? state.inputColumns : Object.keys(labels).filter(function (key) {
      return data.rows.some(function (row) { return Object.hasOwn(row, key); });
    });
    $('row-count').textContent = source ? data.rows.length + ' 行预览' :
      data.summary.PASS + ' 通过 / ' + data.summary.REVIEW + ' 复核 / ' + data.summary.REJECT + ' 阻断';
    var tail = source ? '' : '<th>结论</th>' + (hasBreakdown ? '<th>计算分项</th>' : '');
    $('row-head').innerHTML = '<tr>' + keys.map(function (key) { return '<th>' + esc(labels[key] || key) + '</th>'; }).join('') + tail + '</tr>';
    $('row-body').innerHTML = data.rows.length ? data.rows.map(function (row) {
      var values = keys.map(function (key) { return '<td class="' + (typeof row[key] === 'number' ? 'num' : '') + '">' + esc(row[key]) + '</td>'; }).join('');
      var verdict = source ? '' : '<td><span class="verdict" data-v="' + esc(row.verdict) + '">' + esc(row.verdict || '') + '</span><br><small>' + esc(row.reason || '') + '</small></td>';
      var breakdown = hasBreakdown ? '<td>' + renderBreakdown(row.breakdown) + '</td>' : '';
      return '<tr>' + values + verdict + breakdown + '</tr>';
    }).join('') : '<tr><td class="empty" colspan="' + (keys.length + (source ? 0 : 1)) + '">没有可展示条目</td></tr>';
  }
  function renderCertification(cert) {
    var body = $('cert-body');
    if (!body) return;
    if (!cert || !cert.batches || !cert.batches.length) {
      body.innerHTML = '<tr><td colspan="5" class="empty">暂无认证口径；填写约定线和认证来源后试算</td></tr>';
      if ($('cert-summary')) $('cert-summary').textContent = '尚未计算';
      return;
    }
    if ($('cert-summary')) {
      $('cert-summary').textContent = cert.counts.GUARANTEED + ' 批可保证达标 / ' +
        cert.counts.REVIEW + ' 批复核 / ' + cert.counts.REJECT + ' 批阻断';
    }
    body.innerHTML = cert.batches.map(function (row) {
      var status = row.status || row.verdict || 'REVIEW';
      var ratio = row.committed_ratio_text || (row.committed_ratio === null || row.committed_ratio === undefined ? '证据不足' : (row.committed_ratio * 100).toFixed(2) + '%');
      var average = row.average_ratio_text || '--';
      var basis = renderBreakdown(row.basis || row.breakdown || []);
      return '<tr>' +
        '<td>' + esc(row.label || row.lot_id) + '</td>' +
        '<td class="num cert-ratio cert-' + esc(status) + '">' + esc(ratio) + '</td>' +
        '<td class="num">' + esc(average) + '</td>' +
        '<td><span class="verdict" data-v="' + esc(row.verdict) + '">' + esc(status) + '</span><br><small>' + esc(row.reason || '') + '</small></td>' +
        '<td class="cert-basis">' + basis + '</td>' +
      '</tr>';
    }).join('');
  }
  function renderLedger(data) {
    if (!data.ledger) { $('ledger-list').innerHTML = '<p class="empty">运行对账后显示</p>'; return; }
    var values = data.ledger;
    $('ledger-list').innerHTML = [
      ['来源行', values.counts.input], ['采信行', values.counts.accepted], ['隔离行', values.counts.isolated],
      ['精确重复', values.counts.duplicates], ['忽略行', values.counts.ignored], ['计算条目', values.counts.computed]
    ].map(function (row) { return '<div class="ledger-row"><span>' + row[0] + '</span><strong>' + row[1] + '</strong></div>'; }).join('') +
      '<div class="ledger-row"><span>' + esc(values.metric_field) + '</span><strong>' + esc(values.metric_total) + '</strong></div>' +
      values.checks.map(function (check) { return '<div class="ledger-row"><span>' + esc(check.label) + '</span><strong class="' + (check.ok ? 'check-ok' : 'check-bad') + '">' + (check.ok ? '通过' : '异常') + '</strong></div>'; }).join('');
  }
  function render(data) {
    state.payload = data;
    $('chart-label').textContent = Object.values(state.resultColumns)[0];
    $('m-input').textContent = data.counts.input;
    $('m-accepted').textContent = data.counts.accepted;
    $('m-isolated').textContent = data.counts.isolated + data.counts.duplicates;
    $('m-rejected').textContent = data.summary.REJECT;
    renderRows(data);
    renderCertification(data.certification);
    renderIssues(data);
    renderLedger(data);
    window.renderChart($('chart'), data.chart && data.chart.length ? (state.kind || 'bars') : 'bars', data.chart || []);
  }
  function stageButtons(stages) {
    $('stage-nav').innerHTML = stages.map(function (stage, index) {
      return '<button type="button" data-stage="' + stage.id + '" aria-pressed="' + (index === 0 ? 'true' : 'false') + '">' + esc(stage.name) + '</button>';
    }).join('');
    Array.from(document.querySelectorAll('[data-stage]')).forEach(function (button) {
      button.addEventListener('click', function () {
        document.querySelectorAll('[data-stage]').forEach(function (other) { other.setAttribute('aria-pressed', String(other === button)); });
        loadStage(button.getAttribute('data-stage'));
      });
    });
  }
  async function loadStage(stage) {
    try {
      setNotice('');
      document.body.setAttribute('aria-busy', 'true');
      var payload = await request('/api/stages/' + stage, {method: 'POST', body: JSON.stringify({profile: $('profile').value})});
      render(payload);
    } catch (error) { setNotice(error.message); }
    finally { document.body.removeAttribute('aria-busy'); }
  }
  async function runBatch() {
    try {
      setNotice('');
      $('run').disabled = true;
      var run = await request('/api/runs', {method: 'POST', body: JSON.stringify({profile: $('profile').value})});
      state.run = run;
      state.approved = run.status === 'APPROVED';
      render(run);
      $('run-status').textContent = run.status + ' · rev ' + run.revision;
      $('revision').textContent = 'rev ' + run.revision;
      $('sign').disabled = run.summary.REJECT > 0 || state.approved;
      $('hold').disabled = state.approved;
      $('download').hidden = !state.approved;
      if (state.approved) $('download').href = '/api/runs/' + run.run_id + '/evidence';
    } catch (error) { setNotice(error.message); }
    finally { $('run').disabled = false; }
  }
  async function review(decision) {
    if (!state.run) return;
    try {
      var run = await request('/api/runs/' + state.run.run_id + '/review', {
        method: 'POST',
        body: JSON.stringify({decision: decision, comment: $('comment').value, expected_revision: state.run.revision})
      });
      state.run = run;
      state.approved = run.status === 'APPROVED';
      $('run-status').textContent = run.status + ' · rev ' + run.revision;
      $('revision').textContent = 'rev ' + run.revision;
      $('sign').disabled = true;
      $('hold').disabled = true;
      $('download').hidden = !state.approved;
      if (state.approved) $('download').href = '/api/runs/' + run.run_id + '/evidence';
    } catch (error) { setNotice(error.message); }
  }
  function parseCertSources() {
    var input = $('cert-sources');
    if (!input || !input.value.trim()) return null;
    var parsed = JSON.parse(input.value);
    if (Array.isArray(parsed)) return parsed;
    if (parsed && typeof parsed === 'object') {
      return Object.keys(parsed).map(function (lotId) {
        var value = parsed[lotId];
        return typeof value === 'object' ? Object.assign({lot_id: lotId}, value) : {lot_id: lotId, certified_qty: value};
      });
    }
    return null;
  }
  function certConfigBody() {
    return {
      profile: $('profile').value,
      threshold: $('cert-threshold').value || null,
      certified: parseCertSources()
    };
  }
  async function recalculateCertification() {
    try {
      setNotice('');
      var payload = await request('/api/stages/decision', {method: 'POST', body: JSON.stringify(certConfigBody())});
      render(payload);
      setNotice('已按保守口径重算；未归属损耗优先视为认证原料损失。');
    } catch (error) {
      setNotice(error.message);
    }
  }
  async function submitSortingRecord(event) {
    event.preventDefault();
    var feedback = $('sorting-feedback');
    if (feedback) feedback.textContent = '提交中…';
    var record = {
      record_id: $('sort-record-id').value.trim(),
      lot_id: $('sort-lot-id').value.trim(),
      certified_loss_qty: $('sort-cert-qty').value || 0,
      uncertified_loss_qty: $('sort-uncert-qty').value || 0,
      threshold: $('cert-threshold').value || null,
      certified: parseCertSources()
    };
    try {
      var url = state.run ? '/api/runs/' + state.run.run_id + '/sorting-records' : '/api/certification/sorting-records';
      var data = await request(url, {method: 'POST', body: JSON.stringify(record)});
      if (data.run_id) {
        state.run = data;
        state.approved = data.status === 'APPROVED';
        render(data);
        $('run-status').textContent = data.status + ' · rev ' + data.revision;
        $('revision').textContent = 'rev ' + data.revision;
        $('sign').disabled = data.summary.REJECT > 0 || state.approved;
        $('hold').disabled = state.approved;
      } else {
        renderCertification(data);
      }
      if (feedback) {
        var outcome = data.posting && data.posting.outcomes ? data.posting.outcomes[0] : data.posting;
        feedback.textContent = outcome && outcome.duplicate ? '同份记录已处理，未重复扣减' : '已登记并重算受影响批次';
      }
      $('sort-record-id').value = '';
      $('sort-cert-qty').value = '';
      $('sort-uncert-qty').value = '';
    } catch (error) {
      if (feedback) feedback.textContent = '';
      setNotice(error.message);
    }
  }
  async function boot() {
    try {
      var catalog = await request('/api/catalog');
      state.kind = catalog.chart;
      state.resultColumns = catalog.result_columns;
      state.inputColumns = catalog.columns;
      stageButtons(catalog.stages.slice(0, 5));
      document.querySelector('.rail-note').textContent = catalog.code + ' · ' + catalog.stages[6].name;
      await loadStage('intake');
    } catch (error) { setNotice(error.message); }
    $('run').addEventListener('click', runBatch);
    var certButton = $('cert-recalc');
    if (certButton) certButton.addEventListener('click', recalculateCertification);
    var demoButton = $('cert-demo');
    if (demoButton) demoButton.addEventListener('click', function () {
      $('cert-threshold').value = '0.90';
      $('cert-sources').value = '[{"lot_id":"L1","certified_qty":900}]';
      recalculateCertification();
    });
    var sortingForm = $('sorting-form');
    if (sortingForm) sortingForm.addEventListener('submit', submitSortingRecord);
    $('sign').addEventListener('click', function () { review('APPROVED'); });
    $('hold').addEventListener('click', function () { review('HELD'); });
    $('profile').addEventListener('change', function () {
      state.run = null; state.approved = false;
      $('sign').disabled = true; $('hold').disabled = true; $('download').hidden = true;
      $('run-status').textContent = '未执行批次'; $('revision').textContent = 'rev 0';
      loadStage('intake');
    });
  }
  document.addEventListener('DOMContentLoaded', boot);
})();
