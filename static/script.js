function isTrialEligible(status) {
    const s = (status || '').trim();
    return s.startsWith('已确认有试用资格') || s === '已确认有试用资格';
}

// ==========================================================================
// 🔐 真实 TOTP 动态验证码（前端实时计算，RFC 6238 / HMAC-SHA1）
// 说明：这是纯前端本地计算（WebCrypto），**不会发起任何网络请求**，
//       每秒仅对"有 32 位密钥的行"做一次 HMAC-SHA1（单条约 0.01ms），性能开销可忽略。
//       若你想暂时关闭实时动态码展示，把下面的 true 改成 false 即可（功能保留，不删代码）。
// ==========================================================================
const SHOW_LIVE_TOTP = true;
const TOTP_B32_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';

function base32ToBytes(secret) {
    const clean = (secret || '').replace(/[\s-]/g, '').toUpperCase().replace(/=+$/, '');
    let bits = 0;
    let value = 0;
    const bytes = [];
    for (const char of clean) {
        const index = TOTP_B32_ALPHABET.indexOf(char);
        if (index === -1) continue;
        value = (value << 5) | index;
        bits += 5;
        if (bits >= 8) {
            bytes.push((value >>> (bits - 8)) & 0xff);
            bits -= 8;
        }
    }
    return new Uint8Array(bytes);
}

async function computeTotp(secret, timestampSeconds) {
    const keyBytes = base32ToBytes(secret);
    if (!keyBytes.length || !window.crypto || !window.crypto.subtle) return '';
    const counter = Math.floor((timestampSeconds || Math.floor(Date.now() / 1000)) / 30);
    const buffer = new ArrayBuffer(8);
    const view = new DataView(buffer);
    view.setUint32(0, Math.floor(counter / 4294967296));
    view.setUint32(4, counter >>> 0);
    try {
        const key = await window.crypto.subtle.importKey(
            'raw', keyBytes, { name: 'HMAC', hash: 'SHA-1' }, false, ['sign']
        );
        const signature = new Uint8Array(await window.crypto.subtle.sign('HMAC', key, buffer));
        const offset = signature[signature.length - 1] & 0x0f;
        const code = ((signature[offset] & 0x7f) << 24)
            | (signature[offset + 1] << 16)
            | (signature[offset + 2] << 8)
            | signature[offset + 3];
        return String(code % 1000000).padStart(6, '0');
    } catch (e) {
        return '';
    }
}

function totpSecondsLeft() {
    return 30 - (Math.floor(Date.now() / 1000) % 30);
}

// 前端 tick 用的密钥表（不依赖轮询，秒级刷新）
window.__totpSecrets = {};

function registerTotpCell(email, secret) {
    if (!email || !secret) return;
    window.__totpSecrets[email] = secret;
}

let totpTickerStarted = false;

function startTotpTicker() {
    // SHOW_LIVE_TOTP = false 时不启动定时器（功能保留，只是不实时计算/展示动态码）
    if (!SHOW_LIVE_TOTP) return;
    if (totpTickerStarted || typeof setInterval !== 'function') return;
    totpTickerStarted = true;
    const tick = async () => {
        const now = Math.floor(Date.now() / 1000);
        const remaining = 30 - (now % 30);
        for (const [email, secret] of Object.entries(window.__totpSecrets || {})) {
            const codeEl = document.querySelector(`[data-totp-code="${CSS.escape(email)}"]`);
            const barEl = document.querySelector(`[data-totp-bar="${CSS.escape(email)}"]`);
            if (!codeEl && !barEl) continue;
            const code = await computeTotp(secret, now);
            if (codeEl && code) codeEl.textContent = `${code.slice(0, 3)} ${code.slice(3)}`;
            if (barEl) barEl.style.width = `${Math.round((remaining / 30) * 100)}%`;
        }
    };
    tick();
    setInterval(tick, 1000);
}

// ==========================================================================
// 🌙 深色模式切换（默认浅色；记忆到 localStorage）
// ==========================================================================
const THEME_KEY = 'gptRegTheme_v1';

function initTheme() {
    const saved = localStorage.getItem(THEME_KEY) || 'light';
    applyTheme(saved);
}

function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    const btn = document.getElementById('btnToggleTheme');
    if (btn) {
        btn.textContent = theme === 'dark' ? '☀️ 浅色模式' : '🌙 深色模式';
        btn.title = theme === 'dark' ? '切换到浅色模式' : '切换到深色模式';
    }
}

function toggleTheme() {
    const cur = document.documentElement.getAttribute('data-theme') || 'light';
    const next = cur === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    localStorage.setItem(THEME_KEY, next);
    if (typeof showToast === 'function') {
        showToast(next === 'dark' ? '已切换到深色模式' : '已切换到浅色模式', 'info');
    }
}

// ==========================================================================
// ☑️ 批量选择模式（默认关闭；打开后显示复选框与批量操作条）
// ==========================================================================
let BATCH_MODE = false;
const selectedEmails = new Set();

function toggleBatchMode() {
    BATCH_MODE = !BATCH_MODE;
    document.body.classList.toggle('batch-mode', BATCH_MODE);
    if (!BATCH_MODE) selectedEmails.clear();
    const btn = document.getElementById('btnBatchMode');
    if (btn) btn.textContent = BATCH_MODE ? '✖ 退出批量' : '☑️ 批量模式';
    updateBatchBar();
    try {
        if (typeof filterReport === 'function') filterReport();
        else if (typeof filterAccounts === 'function') filterAccounts();
    } catch (e) { /* ignore */ }
    if (typeof showToast === 'function') {
        showToast(BATCH_MODE ? '已进入批量模式（可勾选多行）' : '已退出批量模式', 'info');
    }
}

function updateBatchBar() {
    const bar = document.getElementById('batchCount');
    if (bar) bar.textContent = selectedEmails.size;
}

function toggleRowSelect(email, checked) {
    if (checked) selectedEmails.add(email); else selectedEmails.delete(email);
    updateBatchBar();
}

function toggleSelectAll(checked) {
    selectedEmails.clear();
    document.querySelectorAll('.batch-check').forEach(cb => {
        cb.checked = checked;
        if (checked) selectedEmails.add(cb.dataset.email);
    });
    updateBatchBar();
}

function batchExportSelected() {
    if (!selectedEmails.size) {
        if (typeof showToast === 'function') showToast('请先勾选账号', 'warning');
        return;
    }
    const rows = (typeof lastReportRows !== 'undefined' ? lastReportRows : [])
        .filter(r => selectedEmails.has(r.email));
    const lines = rows.map(r => `${r.email}-----${r.password || 'N/A'}-----${r.two_factor_secret || ''}`);
    if (typeof copyText === 'function') {
        copyText(lines.join('\n'), `已复制 ${rows.length} 个账号（5横杠格式）`);
    }
}

// ==========================================================================
// ↕️ 报表排序（点击表头切换 升/降/取消；默认不排序）
// ==========================================================================
let reportSort = { key: null, dir: null };   // dir: 'asc' | 'desc' | null

function toggleReportSort(key) {
    const order = ['asc', 'desc', null];
    const curIdx = order.indexOf(reportSort.dir);
    const nextIdx = (reportSort.key === key) ? (curIdx + 1) % 3 : 0;
    reportSort = { key: nextIdx === 2 ? null : key, dir: order[nextIdx] };
    applySortIndicators();
    if (typeof filterReport === 'function') filterReport();
}

function applySortIndicators() {
    document.querySelectorAll('th.sortable').forEach(th => {
        th.classList.remove('asc', 'desc');
        if (th.dataset.sortKey === reportSort.key && reportSort.dir) {
            th.classList.add(reportSort.dir);
        }
    });
}

function sortRows(rows, key, dir) {
    if (!key || !dir) return rows;
    const val = r => {
        switch (key) {
            case 'email': return (r.email || '').toLowerCase();
            case 'register_time': return r.register_time || '';
            case 'plan': return (r.plan || '').toLowerCase();
            case 'trial': return r.trial_eligible === true ? 2 : (r.trial_eligible === false ? 0 : 1);
            case 'plus': return r.is_plus === true ? 1 : 0;
            default: return '';
        }
    };
    const sorted = [...rows].sort((a, b) => {
        const va = val(a), vb = val(b);
        if (va < vb) return -1;
        if (va > vb) return 1;
        return 0;
    });
    return dir === 'desc' ? sorted.reverse() : sorted;
}

// ==========================================================================
// 👁️ 敏感信息显示/隐藏（纯展示层：不改变数据，不影响功能
// ==========================================================================
let SENSITIVE_VISIBLE = false;

function maskSecret(value) {
    // 显示前 4 位 + 掩码，保持可读性同时避免明文铺满屏幕
    const text = String(value || '');
    if (!text || text === 'N/A') return text;
    if (text.length <= 8) return text.slice(0, 2) + '••••';
    return text.slice(0, 4) + '••••••' + text.slice(-4);
}

function displaySecret(value) {
    return SENSITIVE_VISIBLE ? (value || 'N/A') : maskSecret(value);
}

function toggleSensitiveDisplay() {
    SENSITIVE_VISIBLE = !SENSITIVE_VISIBLE;
    const btn = document.getElementById('btnToggleSensitive');
    if (btn) {
        btn.textContent = SENSITIVE_VISIBLE ? '🙈 隐藏敏感信息' : '👁️ 显示敏感信息';
        btn.classList.toggle('action-btn-primary', SENSITIVE_VISIBLE);
    }
    // 重新渲染当前视图（保留筛选与滚动）
    try {
        if (typeof filterReport === 'function' && document.getElementById('reportTableBody')) {
            filterReport();
        } else if (typeof filterAccounts === 'function') {
            filterAccounts();
        }
    } catch (e) {
        // ignore
    }
    showToast(SENSITIVE_VISIBLE ? '已显示密码 / 2FA 明文' : '已隐藏密码 / 2FA（点击可显示）', 'info');
}

function totpCellHtml(email, secret) {
    if (!secret) {
        return '<span style="color:var(--text-dim);font-size:12px;">未开启 / 未取得</span>';
    }
    // SHOW_LIVE_TOTP = false 时只展示 32 位密钥，不注册/计算 6 位动态码
    if (SHOW_LIVE_TOTP) {
        registerTotpCell(email, secret);
    }
    const shownSecret = SENSITIVE_VISIBLE ? secret : maskSecret(secret);
    return `
        <div style="display:flex;flex-direction:column;gap:3px;">
            <span class="copyable" onclick="copyText('${secret}', '2FA密钥')"
                  style="font-family:'JetBrains Mono',monospace;font-size:11.5px;color:var(--primary);font-weight:600;"
                  title="点击复制完整 32 位 2FA 密钥">🔐 ${shownSecret}</span>
            ${SHOW_LIVE_TOTP ? `
            <span class="copyable" data-totp-code="${email}" onclick="copyText(document.querySelector('[data-totp-code=\\'${email}\\']').textContent.replace(/\\s/g,''), '动态验证码')"
                  style="font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;letter-spacing:1px;color:#22c55e;"
                  title="当前真实动态验证码（每 30 秒自动刷新）">------</span>
            <span style="display:block;height:3px;border-radius:2px;background:rgba(148,163,184,.25);overflow:hidden;">
                <span data-totp-bar="${email}" style="display:block;height:100%;width:100%;background:#22c55e;"></span>
            </span>` : ''}
        </div>`;
}

function trialBadgeHtml(trialStatus, trialEligible) {
    if (trialEligible === true || isTrialEligible(trialStatus)) {
        return `<span class="badge badge-trial-eligible">🎁 有 1 个月 Plus 试用资格</span>`;
    }
    if (trialEligible === false) {
        const text = trialStatus || '官方明确返回无试用资格';
        return `<span class="badge badge-trial-ineligible" title="${text}">✖ 无试用资格</span>`;
    }
    const text = trialStatus || '待官方确认';
    if (text.includes('会员')) {
        return `<span class="badge badge-trial-member">⭐ ${text}</span>`;
    }
    return `<span class="badge badge-unknown" title="${text}">❔ ${text}</span>`;
}

function plusBadgeHtml(record) {
    const plan = record.plan || '未检测';
    if (record.is_plus === true || (record.is_paid === true && (plan || '').toLowerCase() === 'plus')) {
        return `<span class="badge badge-paid">✅ Plus 生效 (${plan})</span>`;
    }
    if (record.is_plus === false || record.is_paid === false) {
        const suffix = plan && plan !== '未检测' ? ` · ${plan}` : '';
        return `<span class="badge badge-free" title="官方接口返回未激活 Plus${suffix}">❌ 非 Plus${suffix}</span>`;
    }
    return `<span class="badge badge-unknown">❔ ${plan}</span>`;
}

let isRunning = false;
let logIndex = 0;
let pollInterval = null;
let currentAccountFilter = 'all';
let lastPipelinePhase = '';

// Toast 提示与快捷复制
function showToast(message, type = 'info') {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = message;
    toast.className = 'toast show';
    if (type === 'error') {
        toast.style.borderLeftColor = '#ef4444';
    } else if (type === 'success') {
        toast.style.borderLeftColor = '#22c55e';
    } else {
        toast.style.borderLeftColor = '#6366f1';
    }
    if (window.toastTimeout) clearTimeout(window.toastTimeout);
    window.toastTimeout = setTimeout(() => {
        toast.className = 'toast hidden';
    }, 2800);
}

function copyText(text, label = '内容') {
    if (!text || text === 'N/A') return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => {
            const preview = text.length > 25 ? text.substring(0, 22) + '...' : text;
            showToast(`已复制 ${label}: ${preview}`, 'success');
        }).catch(() => fallbackCopy(text, label));
    } else {
        fallbackCopy(text, label);
    }
}

function fallbackCopy(text, label) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try {
        document.execCommand('copy');
        showToast(`已复制 ${label}`, 'success');
    } catch (e) {
        showToast('复制失败', 'error');
    }
    document.body.removeChild(ta);
}

// ==========================================================================
// 📏 表格列宽可拖拽调整（类似 ElementPlus 的 resizable），宽度记忆到 localStorage
// ==========================================================================
const COL_WIDTH_STORAGE_KEY = 'gptRegColWidths_v1';

function _colKey(table, index) {
    const holder = table.closest('[id]');
    const tableId = holder ? holder.id : (table.className || 'table');
    return tableId + '::' + index;
}

function _readColWidths() {
    try {
        return JSON.parse(localStorage.getItem(COL_WIDTH_STORAGE_KEY) || '{}');
    } catch (e) {
        return {};
    }
}

function _saveColWidths() {
    const data = {};
    document.querySelectorAll('.table-container table').forEach(table => {
        table.querySelectorAll('thead th').forEach((th, index) => {
            if (th.style.width) data[_colKey(table, index)] = th.style.width;
        });
    });
    try {
        localStorage.setItem(COL_WIDTH_STORAGE_KEY, JSON.stringify(data));
    } catch (e) {
        // 忽略隐私模式下的写入失败
    }
}

function _restoreColWidths() {
    const data = _readColWidths();
    document.querySelectorAll('.table-container table').forEach(table => {
        table.querySelectorAll('thead th').forEach((th, index) => {
            const width = data[_colKey(table, index)];
            if (width) {
                th.style.width = width;
                th.style.minWidth = width;
            }
        });
    });
}

function _makeColumnResizable(th, table) {
    if (th.querySelector('.col-resizer')) return;
    th.style.position = 'relative';
    const resizer = document.createElement('div');
    resizer.className = 'col-resizer';
    resizer.title = '左右拖动调整列宽（双击恢复默认）';
    th.appendChild(resizer);

    let startX = 0;
    let startWidth = 0;
    let dragging = false;

    const onMouseMove = (event) => {
        if (!dragging) return;
        const width = Math.max(60, startWidth + (event.pageX - startX));
        th.style.width = width + 'px';
        th.style.minWidth = width + 'px';
    };
    const onMouseUp = () => {
        if (!dragging) return;
        dragging = false;
        document.body.style.cursor = '';
        document.body.classList.remove('col-resizing');
        _saveColWidths();
    };

    resizer.addEventListener('mousedown', (event) => {
        dragging = true;
        startX = event.pageX;
        startWidth = th.offsetWidth;
        document.body.style.cursor = 'col-resize';
        document.body.classList.add('col-resizing');
        event.preventDefault();
        event.stopPropagation();
    });
    // 双击恢复默认宽度
    resizer.addEventListener('dblclick', () => {
        th.style.width = '';
        th.style.minWidth = '';
        _saveColWidths();
    });

    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
}

function initResizableColumns() {
    document.querySelectorAll('.table-container table').forEach(table => {
        table.querySelectorAll('thead th').forEach(th => _makeColumnResizable(th, table));
    });
    _restoreColWidths();
}

// 初始化
document.addEventListener('DOMContentLoaded', () => {
    initTheme();               // 🌙 恢复主题（默认浅色）
    switchTab('dashboard');
    startPolling();
    loadProviders();
    loadSettings();
    loadTokenImportSettings();
    startTotpTicker();
    initResizableColumns();
});

// 切换视图
function switchTab(tabName) {
    document.querySelectorAll('.view-section').forEach(el => {
        el.classList.remove('active');
        el.classList.add('hidden');
    });
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));

    const section = document.getElementById(`view-${tabName}`);
    if (section) {
        section.classList.add('active');
        section.classList.remove('hidden');
    }
    const activeNav = document.querySelector(`.nav-item[data-tab="${tabName}"]`);
    if (activeNav) activeNav.classList.add('active');

    if (tabName === 'accounts') {
        loadAccounts();
    }
    if (tabName === 'report') {
        loadAutopilotReport();
    }
    if (tabName === 'tokens') {
        loadTokenImportSettings();
    }
    if (tabName === 'api-service') {
        loadChat2ApiStatus();
    }
}

// 轮询状态
function startPolling() {
    pollStatus();
    pollInterval = setInterval(pollStatus, 1000);
}

async function pollStatus() {
    try {
        const res = await fetch(`/api/status?log_index=${logIndex}`);
        const data = await res.json();
        updateUI(data);
    } catch (e) {
        console.error("Polling error:", e);
    }
}

function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text ?? '';
}

function updateUI(data) {
    const progress = data.progress || {};

    setText('valAction', data.current_action);
    setText('valSuccess', data.success);
    setText('valFail', data.fail);
    setText('valInventory', data.total_inventory);
    setText('valTaskTotal', progress.total ?? 0);
    setText('valCompleted', progress.completed ?? 0);
    setText('valSkipped', progress.skipped ?? 0);
    setText('valRemaining', progress.remaining ?? 0);
    setText('tokenTotal', progress.total ?? 0);
    setText('tokenCompleted', progress.completed ?? 0);
    setText('tokenFail', data.fail ?? 0);
    setText('tokenSkipped', progress.skipped ?? 0);
    setText('tokenRemaining', progress.remaining ?? 0);

    isRunning = data.is_running;
    const btnStart = document.getElementById('btnStart');
    const btnStop = document.getElementById('btnStop');
    const statusDot = document.getElementById('statusDot');
    const statusText = document.getElementById('statusText');

    renderPipeline(data.pipeline);

    if (isRunning) {
        if (btnStart) btnStart.classList.add('hidden');
        if (btnStop) btnStop.classList.remove('hidden');
        if (statusDot) statusDot.classList.add('running');
        if (statusText) statusText.textContent = "运行中";
    } else {
        if (btnStart) btnStart.classList.remove('hidden');
        if (btnStop) btnStop.classList.add('hidden');
        if (statusDot) statusDot.classList.remove('running');
        if (statusText) statusText.textContent = "系统空闲";
    }

    const monitorImg = document.getElementById('liveMonitor');
    const noSignal = document.getElementById('noSignal');
    const monitorStatus = document.getElementById('monitorStatus');

    if (monitorImg) {
        if (isRunning) {
            monitorImg.classList.remove('hidden');
            if (noSignal) noSignal.classList.add('hidden');

            if (!monitorImg.src || monitorImg.src.indexOf('/video_feed') === -1) {
                monitorImg.src = "/video_feed";
            }
        } else {
            monitorImg.classList.add('hidden');
            if (noSignal) noSignal.classList.remove('hidden');
        }
    }

    if (monitorStatus) {
        if (isRunning) {
            monitorStatus.textContent = "LIVE";
            monitorStatus.classList.remove('neutral');
            monitorStatus.classList.add('success');
        } else {
            monitorStatus.textContent = "OFFLINE";
            monitorStatus.classList.remove('success');
            monitorStatus.classList.add('neutral');
        }
    }

    if (data.logs && data.logs.length > 0) {
        const container = document.getElementById('logContainer');

        const placeholder = container.querySelector('.log-placeholder');
        if (placeholder) placeholder.remove();

        data.logs.forEach(logLine => {
            const div = document.createElement('div');
            div.className = 'log-entry';
            div.textContent = logLine;
            container.appendChild(div);
        });

        container.scrollTop = container.scrollHeight;
        logIndex += data.logs.length;
    }
}

// ==========================================
// 📬 邮箱提供商管理
// ==========================================

async function loadProviders() {
    try {
        const res = await fetch('/api/providers');
        const providers = await res.json();
        renderProviders(providers);
    } catch (e) {
        console.error("加载提供商失败:", e);
    }
}

function renderProviders(providers) {
    const container = document.getElementById('providerList');
    container.innerHTML = '';

    providers.forEach(p => {
        const item = document.createElement('div');
        item.className = 'provider-item';

        const label = document.createElement('label');
        label.className = 'provider-label';

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.value = p.id;
        checkbox.checked = p.selected;
        checkbox.style.cssText = 'width:15px; height:15px; cursor:pointer; accent-color:var(--primary);';
        checkbox.addEventListener('change', onProviderToggle);

        label.appendChild(checkbox);
        label.appendChild(document.createTextNode(p.name));

        const tag = document.createElement('span');
        tag.className = 'provider-tag';
        tag.textContent = '可用';

        item.appendChild(label);
        item.appendChild(tag);
        container.appendChild(item);
    });
}

async function onProviderToggle() {
    const checkboxes = document.querySelectorAll('#providerList input[type=checkbox]');
    const selected = Array.from(checkboxes).filter(c => c.checked).map(c => c.value);

    if (selected.length === 0) {
        // 至少保留一个，恢复刚才取消的
        this.checked = true;
        return;
    }

    try {
        await fetch('/api/providers', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ selected })
        });
    } catch (e) {
        console.error("更新提供商失败:", e);
    }
}

// ==========================================
// ⚙️ 高级设置与代理
// ==========================================

function toggleProxyAuthFields() {
    const useAuth = document.getElementById('proxyUseAuth').checked;
    const fields = document.getElementById('proxyAuthFields');
    if (!fields) return;
    if (useAuth) {
        fields.classList.remove('hidden');
        fields.style.display = 'grid';
    } else {
        fields.classList.add('hidden');
        fields.style.display = 'none';
    }
}

async function loadSettings() {
    try {
        const res = await fetch('/api/settings');
        const data = await res.json();
        document.getElementById('parallelCount').value = data.parallel ?? 1;
        document.getElementById('headlessMode').checked = data.headless ?? false;

        const enable2faEl = document.getElementById('enable2fa');
        if (enable2faEl) enable2faEl.checked = data.enable_2fa ?? true;
        const refUrlEl = document.getElementById('referralUrl');
        if (refUrlEl) refUrlEl.value = data.referral_url || '';

        const proxy = data.proxy || {};
        const pEnabled = document.getElementById('proxyEnabled');
        const pType = document.getElementById('proxyType');
        const pHost = document.getElementById('proxyHost');
        const pPort = document.getElementById('proxyPort');
        const pUseAuth = document.getElementById('proxyUseAuth');
        const pUsername = document.getElementById('proxyUsername');
        const pPassword = document.getElementById('proxyPassword');

        if (pEnabled) pEnabled.checked = proxy.enabled ?? false;
        if (pType) pType.value = proxy.type ?? 'socks5';
        if (pHost) pHost.value = proxy.host ?? '127.0.0.1';
        if (pPort) pPort.value = proxy.port ?? 7890;
        if (pUseAuth) pUseAuth.checked = proxy.use_auth ?? false;
        if (pUsername) pUsername.value = proxy.username ?? '';
        if (pPassword) pPassword.value = proxy.password ?? '';

        toggleProxyAuthFields();
    } catch (e) {
        console.error("加载设置失败:", e);
    }
}

async function saveSettings() {
    const parallel = parseInt(document.getElementById('parallelCount').value) || 1;
    const headless = document.getElementById('headlessMode').checked;
    const enable_2fa = document.getElementById('enable2fa') ? document.getElementById('enable2fa').checked : true;
    const referral_url = document.getElementById('referralUrl') ? document.getElementById('referralUrl').value.trim() : '';

    const proxy = {
        enabled: document.getElementById('proxyEnabled') ? document.getElementById('proxyEnabled').checked : false,
        type: document.getElementById('proxyType') ? document.getElementById('proxyType').value : 'socks5',
        host: document.getElementById('proxyHost') ? document.getElementById('proxyHost').value.trim() : '127.0.0.1',
        port: document.getElementById('proxyPort') ? (parseInt(document.getElementById('proxyPort').value) || 7890) : 7890,
        use_auth: document.getElementById('proxyUseAuth') ? document.getElementById('proxyUseAuth').checked : false,
        username: document.getElementById('proxyUsername') ? document.getElementById('proxyUsername').value.trim() : '',
        password: document.getElementById('proxyPassword') ? document.getElementById('proxyPassword').value.trim() : ''
    };

    try {
        await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ parallel, headless, proxy, enable_2fa, referral_url })
        });
    } catch (e) {
        console.error("保存设置失败:", e);
    }
}

async function testProxyConnection() {
    const btn = document.getElementById('btnTestProxy');
    const resDiv = document.getElementById('proxyTestResult');
    if (btn) btn.disabled = true;
    if (resDiv) {
        resDiv.style.display = 'block';
        resDiv.style.color = '#64748b';
        resDiv.innerHTML = '⏳ 正在测试代理连通性及出口节点...';
    }

    const proxy = {
        enabled: true,
        type: document.getElementById('proxyType').value,
        host: document.getElementById('proxyHost').value.trim(),
        port: parseInt(document.getElementById('proxyPort').value) || 7890,
        use_auth: document.getElementById('proxyUseAuth').checked,
        username: document.getElementById('proxyUsername').value.trim(),
        password: document.getElementById('proxyPassword').value.trim()
    };

    try {
        const res = await fetch('/api/proxy/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ proxy })
        });
        const data = await res.json();
        if (data.success) {
            if (resDiv) {
                resDiv.style.color = '#16a34a';
                resDiv.innerHTML = `✅ <b>连通正常</b> (${data.latency_ms}ms)<br>出口IP: ${data.query || '-'}<br>地区: ${data.country || '-'} / ${data.city || '-'}`;
            }
            showToast(`代理连通测试成功！延迟 ${data.latency_ms}ms (${data.country || ''})`, 'success');
        } else {
            if (resDiv) {
                resDiv.style.color = '#dc2626';
                resDiv.innerHTML = `❌ <b>连接失败</b>: ${data.error || '节点无响应'}`;
            }
            showToast('代理连通失败: ' + (data.error || '网络超时'), 'error');
        }
    } catch (e) {
        if (resDiv) {
            resDiv.style.color = '#dc2626';
            resDiv.innerHTML = `❌ <b>测试异常</b>: ${e}`;
        }
        showToast('测试异常: ' + e, 'error');
    } finally {
        if (btn) btn.disabled = false;
    }
}

// ==========================================
// 🚀 任务控制
// ==========================================

async function startTask() {
    const count = parseInt(document.getElementById('targetCount').value) || 1;

    clearLogs();

    try {
        const res = await fetch('/api/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ count: count })
        });

        if (!res.ok) {
            alert("启动失败: " + await res.text());
        }
    } catch (e) {
        alert("请求失败: " + e);
    }
}

async function stopTask() {
    if (!confirm("确定要停止当前任务吗？")) return;

    try {
        await fetch('/api/stop', { method: 'POST' });
    } catch (e) {
        console.error(e);
    }
}

async function loadTokenImportSettings() {
    try {
        const res = await fetch('/api/token-import/settings');
        const data = await res.json();
        document.getElementById('tokenAccountsFile').value = data.accounts_file || '';
        document.getElementById('tokenOutputDir').value = data.output_dir || '';
    } catch (e) {
        console.error("加载 Token 设置失败:", e);
    }
}

async function startTokenImportTask() {
    const accountsFile = document.getElementById('tokenAccountsFile').value.trim();
    const outputDir = document.getElementById('tokenOutputDir').value.trim();

    if (!accountsFile || !outputDir) {
        alert('请填写 TXT 路径和输出目录');
        return;
    }

    clearLogs();

    try {
        const res = await fetch('/api/token-import/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                accounts_file: accountsFile,
                output_dir: outputDir
            })
        });

        if (!res.ok) {
            const data = await res.text();
            alert("启动失败: " + data);
            return;
        }

        switchTab('dashboard');
    } catch (e) {
        alert("请求失败: " + e);
    }
}

function clearLogs() {
    document.getElementById('logContainer').innerHTML = '<div class="log-placeholder">等待任务启动...</div>';
}

// ==========================================
// 👥 账号管理与资格检测
// ==========================================

async function loadAccounts() {
    const tbody = document.getElementById('accountTableBody');
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center">加载中...</td></tr>';

    try {
        const res = await fetch('/api/accounts');
        const accounts = await res.json();
        window.allAccounts = accounts;
        updateAccountStats(accounts);
        renderAccounts(accounts);
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:red">加载失败: ${e}</td></tr>`;
    }
}

function updateAccountStats(accounts) {
    const totalEl = document.getElementById('statTotalAccs');
    const normalEl = document.getElementById('statNormalAccs');
    const paidEl = document.getElementById('statPaidAccs');
    const trialEl = document.getElementById('statTrialAccs');
    const errEl = document.getElementById('statErrAccs');

    const total = accounts.length;
    const plusAll = accounts.filter(a =>
        a.is_paid ||
        ['plus', 'team', 'pro'].includes((a.plan || '').toLowerCase()) ||
        isTrialEligible(a.trial_status)
    ).length;
    const paid = accounts.filter(a => a.is_paid || ['plus', 'team', 'pro'].includes((a.plan || '').toLowerCase())).length;
    const trial = accounts.filter(a => isTrialEligible(a.trial_status)).length;
    const fail = accounts.filter(a =>
        (a.status || '').includes('失败') ||
        (a.status || '').includes('错误') ||
        (a.account_status || '').includes('封禁') ||
        (a.account_status || '').includes('失效') ||
        (a.account_status || '').includes('失败')
    ).length;
    const normal = accounts.filter(a => a.verified === true && a.account_status === '🟢 在线已验证').length;
    const free = accounts.filter(a => (a.plan || '').toLowerCase() === 'free' && !(a.status || '').includes('失败') && !(a.account_status || '').includes('封禁')).length;

    if (totalEl) totalEl.textContent = total;
    if (normalEl) normalEl.textContent = normal > 0 ? normal : 0;
    if (paidEl) paidEl.textContent = paid;
    if (trialEl) trialEl.textContent = trial;
    if (errEl) errEl.textContent = fail;

    const elAll = document.getElementById('tabCountAll');
    const elBrowser = document.getElementById('tabCountBrowser');
    const elProtocol = document.getElementById('tabCountProtocol');
    const elImport = document.getElementById('tabCountImport');
    const elPlusAll = document.getElementById('tabCountPlusAll');
    const elTrial = document.getElementById('tabCountTrial');
    const elPaid = document.getElementById('tabCountPaid');
    const elFree = document.getElementById('tabCountFree');
    const elFail = document.getElementById('tabCountFail');

    const browserCount = accounts.filter(a => (a.register_mode || '').toLowerCase() === 'browser').length;
    const protocolCount = accounts.filter(a => (a.register_mode || '').toLowerCase() === 'protocol').length;
    const importCount = accounts.filter(a => (a.register_mode || '').toLowerCase() === 'import').length;

    if (elAll) elAll.textContent = total;
    if (elBrowser) elBrowser.textContent = browserCount;
    if (elProtocol) elProtocol.textContent = protocolCount;
    if (elImport) elImport.textContent = importCount;
    if (elPlusAll) elPlusAll.textContent = plusAll;
    if (elTrial) elTrial.textContent = trial;
    if (elPaid) elPaid.textContent = paid;
    if (elFree) elFree.textContent = free;
    if (elFail) elFail.textContent = fail;
}

function setAccountFilter(filter) {
    currentAccountFilter = filter;
    document.querySelectorAll('.filter-btn').forEach(btn => {
        if (btn.getAttribute('data-filter') === filter) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });
    filterAccounts();
}

function renderAccounts(accounts) {
    const tbody = document.getElementById('accountTableBody');
    tbody.innerHTML = '';

    if (!accounts || accounts.length === 0) {
        tbody.innerHTML = '<tr><td colspan="11" style="text-align:center;color:#64748b;padding:32px;">暂无符合条件的账号数据</td></tr>';
        return;
    }

    accounts.forEach(acc => {
        // 健康状态 Badge
        let healthBadge = '';
        const h = (acc.account_status || '').toLowerCase();
        if (h.includes('封禁') || h.includes('停用') || h.includes('deactivated')) {
            healthBadge = `<span class="badge badge-status-banned">🚫 已封禁</span>`;
        } else if (h.includes('失效') || h.includes('expired') || h.includes('过期') || h.includes('需重登')) {
            healthBadge = `<span class="badge badge-status-warn">⚠️ Token失效</span>`;
        } else if (h.includes('失败') || h.includes('错误')) {
            healthBadge = `<span class="badge badge-status-banned">❌ 异常失败</span>`;
        } else if (acc.verified === true && acc.account_status === '🟢 在线已验证') {
            healthBadge = `<span class="badge badge-status-normal">🟢 在线验证通过</span>`;
        } else if (h.includes('正常') || h.includes('在线已验证')) {
            healthBadge = '<span class="badge badge-unknown">⚪ 历史未验证</span>';
        } else {
            healthBadge = `<span class="badge badge-unknown">⚪ ${acc.account_status || '待检测'}</span>`;
        }

        // 订阅/支付状态 Badge（真实 is_plus 字段优先，未取得证据时显示未知）
        const planBadge = plusBadgeHtml(acc);

        // 额度 Badge
        let quotaBadge = '';
        const quotaText = acc.quota || '未知，请在官方页面确认';
        if (acc.is_paid) {
            quotaBadge = `<span class="badge badge-quota-plus" title="当前模型额度">⚡ ${quotaText}</span>`;
        } else {
            quotaBadge = `<span class="badge badge-quota" title="当前模型额度">🪙 ${quotaText}</span>`;
        }

        // 会员/试用到期时间 Badge
        let expiryBadge = '';
        const expiryText = acc.expires_at || '未知，未取得订阅到期时间';
        if (acc.is_paid || expiryText.includes('自动续订') || expiryText.includes('202')) {
            expiryBadge = `<span class="badge badge-expiry-active" title="会员到期时间">⏳ ${expiryText}</span>`;
        } else if (expiryText.includes('激活') || expiryText.includes('试用')) {
            expiryBadge = `<span class="badge badge-expiry-warn" title="试用到期情况">🎁 ${expiryText}</span>`;
        } else {
            expiryBadge = `<span class="badge badge-expiry" title="到期情况">♾️ ${expiryText}</span>`;
        }

        // 试用资格 Badge（以官方返回为准，trial_eligible 三态）
        const trialBadge = trialBadgeHtml(acc.trial_status, acc.trial_eligible);

        // 2FA 密钥 + 实时动态验证码单元格
        const twoFaCell = totpCellHtml(acc.email, (acc.two_factor_secret || '').trim() === '未开启' ? '' : acc.two_factor_secret);

        // 注册时间格式化显示
        const regTime = acc.time || 'N/A';

        // 临时邮箱收件箱单元格
        let inboxCell;
        const providerLabel = acc.provider_name || acc.provider;

        if (acc.has_password && acc.temp_credential) {
            inboxCell = `
                <div class="inbox-cell">
                    <div><span class="inbox-tag">[${providerLabel}]</span> <span class="copyable" onclick="copyText('${acc.email}', '临时邮箱')" style="font-family:'JetBrains Mono',monospace;font-size:11.5px" title="点击复制">${acc.email}</span></div>
                    <div style="display:flex;align-items:center;gap:6px;margin-top:2px;">
                        <span class="copyable" onclick="copyText('${acc.temp_credential}', '邮箱密码')" style="font-family:'JetBrains Mono',monospace;font-size:11px;color:#64748b" title="点击复制密码">密: ${acc.temp_credential}</span>
                        <a href="${acc.inbox_url}" target="_blank" class="action-btn-sm" style="font-size:10.5px;padding:2px 6px;">打开</a>
                    </div>
                </div>`;
        } else if (!acc.has_password && acc.temp_credential) {
            const shortToken = acc.temp_credential.length > 15
                ? acc.temp_credential.substring(0, 12) + '...'
                : acc.temp_credential;
            inboxCell = `
                <div class="inbox-cell">
                    <div><span class="inbox-tag">[${providerLabel}]</span></div>
                    <div style="display:flex;align-items:center;gap:6px;margin-top:2px;">
                        <span class="copyable" onclick="copyText('${acc.temp_credential}', '邮箱Token')" style="font-family:'JetBrains Mono',monospace;font-size:10.5px;color:#64748b" title="点击复制完整Token">Token: ${shortToken}</span>
                        <a href="${acc.inbox_url}" target="_blank" class="action-btn-sm" style="font-size:10.5px;padding:2px 6px;">打开</a>
                    </div>
                </div>`;
        } else {
            inboxCell = `
                <div class="inbox-cell">
                    <div style="display:flex;align-items:center;gap:6px;">
                        <span class="inbox-tag">[${providerLabel}]</span>
                        <a href="${acc.inbox_url}" target="_blank" class="action-btn-sm" style="font-size:10.5px;padding:2px 6px;">打开收件箱</a>
                    </div>
                </div>`;
        }

        // 创建方式 Badge
        let modeBadge = '';
        const m = (acc.register_mode || '').toLowerCase();
        if (m === 'protocol' || m.includes('协议')) {
            modeBadge = `<span class="badge badge-mode-protocol" title="通过极速协议版底层发包创建">⚡ 极速协议</span>`;
        } else if (m === 'import' || m.includes('导入')) {
            modeBadge = `<span class="badge badge-mode-import" title="通过外部批量导入录入">📥 批量导入</span>`;
        } else {
            modeBadge = `<span class="badge badge-mode-browser" title="通过真实浏览器模拟渲染创建">🌐 浏览器自动化</span>`;
        }

        const tr = document.createElement('tr');
        const evidence = acc.profile_evidence || {};
        tr.title = evidence.checked_at ? `上次检测: ${evidence.checked_at}；来源: ${evidence.source || '未知'}` : '历史记录，无可验证的检测时间与来源';
        tr.innerHTML = `
            <td class="col-email sticky-col"><b class="copyable" onclick="copyText('${acc.email}', '邮箱账号')" title="点击复制邮箱">${acc.email}</b></td>
            <td class="col-pwd"><span class="copyable" onclick="copyText('${acc.password}', '密码')" style="font-family:'JetBrains Mono',monospace;font-size:12px;" title="点击复制密码">${acc.password || 'N/A'}</span></td>
            <td class="col-2fa">${twoFaCell}</td>
            <td class="col-channel">${modeBadge}</td>
            <td class="col-health">${healthBadge}</td>
            <td class="col-plan">${planBadge}</td>
            <td class="col-quota">${quotaBadge}</td>
            <td class="col-expiry">${expiryBadge}</td>
            <td class="col-trial">${trialBadge}</td>
            <td class="col-time" style="font-size:12px;color:var(--text-muted);white-space:nowrap">${regTime}</td>
            <td class="col-inbox">${inboxCell}</td>
            <td class="col-action">
                <div style="display:flex;gap:4px;justify-content:center;align-items:center;">
                    <button class="action-btn-sm" onclick="checkSingleAccount('${acc.email}', this)" style="padding:4px 8px;font-size:11.5px;min-width:48px;" title="仅在线检测健康状态与资格">
                        <span>🔍</span> 检测
                    </button>
                    <button class="action-btn-sm" onclick="perfectSingleAccount('${acc.email}', this)" style="padding:4px 8px;font-size:11.5px;min-width:48px;background:linear-gradient(135deg, #10b981 0%, #059669 100%);color:#fff;border:none;" title="一键全自动补全密码、开启真实2FA并同步试用资格画像">
                        <span>⚡</span> 完善
                    </button>
                </div>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function filterAccounts() {
    const term = (document.getElementById('searchInput').value || '').toLowerCase().trim();
    if (!window.allAccounts) return;

    let filtered = window.allAccounts;

    if (currentAccountFilter === 'mode_protocol') {
        filtered = filtered.filter(a => (a.register_mode || '').toLowerCase() === 'protocol' || (a.register_mode || '').includes('协议'));
    } else if (currentAccountFilter === 'mode_browser') {
        filtered = filtered.filter(a => (a.register_mode || '').toLowerCase() === 'browser' || (a.register_mode || '').includes('浏览器'));
    } else if (currentAccountFilter === 'mode_import') {
        filtered = filtered.filter(a => (a.register_mode || '').toLowerCase() === 'import' || (a.register_mode || '').includes('导入'));
    } else if (currentAccountFilter === 'plus_all') {
        filtered = filtered.filter(a =>
            a.is_paid ||
            ['plus', 'team', 'pro'].includes((a.plan || '').toLowerCase()) ||
            isTrialEligible(a.trial_status)
        );
    } else if (currentAccountFilter === 'trial') {
        filtered = filtered.filter(a => isTrialEligible(a.trial_status));
    } else if (currentAccountFilter === 'paid') {
        filtered = filtered.filter(a => a.is_paid || ['plus', 'team', 'pro'].includes((a.plan || '').toLowerCase()));
    } else if (currentAccountFilter === 'free') {
        filtered = filtered.filter(a => (a.plan || '').toLowerCase() === 'free' && !(a.status || '').includes('失败') && !(a.account_status || '').includes('封禁'));
    } else if (currentAccountFilter === 'fail') {
        filtered = filtered.filter(a =>
            (a.status || '').includes('失败') ||
            (a.status || '').includes('错误') ||
            (a.account_status || '').includes('封禁') ||
            (a.account_status || '').includes('失效') ||
            (a.account_status || '').includes('失败')
        );
    }

    if (term) {
        filtered = filtered.filter(acc =>
            (acc.email || '').toLowerCase().includes(term) ||
            (acc.password || '').toLowerCase().includes(term) ||
            (acc.two_factor_secret || '').toLowerCase().includes(term) ||
            (acc.status || '').toLowerCase().includes(term) ||
            (acc.account_status || '').toLowerCase().includes(term) ||
            (acc.plan || '').toLowerCase().includes(term) ||
            (acc.quota || '').toLowerCase().includes(term) ||
            (acc.expires_at || '').toLowerCase().includes(term) ||
            (acc.trial_status || '').toLowerCase().includes(term) ||
            (acc.provider || '').toLowerCase().includes(term) ||
            (acc.time || '').toLowerCase().includes(term) ||
            (acc.register_mode || '').toLowerCase().includes(term) ||
            ((acc.register_mode === 'protocol' || acc.register_mode === '极速协议') && '极速协议'.includes(term)) ||
            ((acc.register_mode === 'browser' || acc.register_mode === '浏览器自动化') && '浏览器自动化'.includes(term)) ||
            ((acc.register_mode === 'import' || acc.register_mode === '批量导入') && '批量导入'.includes(term))
        );
    }

    renderAccounts(filtered);
}

function exportAccounts(format) {
    const filter = currentAccountFilter || 'all';
    const ext = format === 'csv' ? 'csv' : 'txt';
    const url = `/api/accounts/export?format=${format}&filter=${filter}`;
    const a = document.createElement('a');
    a.href = url;
    a.download = `chatgpt_accounts_${format}_${filter}_${Date.now()}.${ext}`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    const formatLabel = format === '2fa' ? '2FA 格式 (5横杠)' : format.toUpperCase();
    showToast(`正在导出 [${filter}] 分类的 ${formatLabel} 文件...`, 'success');
}

async function copyAccountsFormatted(format) {
    try {
        const filter = currentAccountFilter || 'all';
        const res = await fetch(`/api/accounts/export?format=${format}&filter=${filter}`);
        if (!res.ok) throw new Error('导出接口错误');
        const text = await res.text();
        if (!text.trim()) {
            showToast(`当前 [${filter}] 分类暂无数据可复制`, 'info');
            return;
        }
        await navigator.clipboard.writeText(text);
        const formatLabel = format === '2fa' ? '2FA 格式 (邮箱-----密码-----32位2fa)' : '账号列表';
        showToast(`已成功复制 [${filter}] 分类的 ${formatLabel} 到剪贴板！`, 'success');
    } catch (e) {
        showToast('复制失败: ' + e, 'error');
    }
}

async function checkSingleAccount(email, btn) {
    if (btn) {
        btn.disabled = true;
        btn.textContent = '⏳ 检测中...';
    }

    try {
        const res = await fetch('/api/accounts/check-one', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email })
        });
        const data = await res.json();
        if (res.ok) {
            showToast(`检查结果: ${email} | ${data.account_status || '未验证'} | 计划: ${data.plan || '未知'} | 额度: ${data.quota || '未知'} | 到期: ${data.expires_at || '未知'}`, data.verified === true ? 'success' : 'info');
            if (data.persisted === false) showToast('结果未保存: ' + (data.persistence_error || '写入失败'), 'error');
            await loadAccounts();
        } else {
            showToast('检测失败: ' + (data.error || '未知错误'), 'error');
            if (btn) {
                btn.disabled = false;
                btn.textContent = '🔍 检测状态';
            }
        }
    } catch (e) {
        showToast('请求出错: ' + e, 'error');
        if (btn) {
            btn.disabled = false;
            btn.textContent = '🔍 检测状态';
        }
    }
}

async function checkAllAccounts() {
    const btn = document.getElementById('btnCheckAll');
    if (btn) {
        btn.disabled = true;
        btn.textContent = '⏳ 检测已启动...';
    }

    try {
        const res = await fetch('/api/accounts/check-all', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await res.json();
        if (res.ok) {
            showToast('已启动全量账号状态与试用资格检测任务！', 'success');
            switchTab('dashboard');
        } else {
            showToast('启动全量检测失败: ' + (data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showToast('请求失败: ' + e, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<span>🔍</span> 一键全量检测';
        }
    }
}

async function perfectSingleAccount(email, btn) {
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span>⏳</span> 完善中...';
    }
    showToast(`正在启动账号 [${email}] 的全自动完善流程 (补全密码+开启真实2FA+同步试用资格)...`, 'info');

    try {
        const res = await fetch('/api/accounts/perfect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email: email })
        });
        let data;
        const text = await res.text();
        try {
            data = JSON.parse(text);
        } catch (parseErr) {
            showToast(`服务器响应异常 (HTTP ${res.status}): ${text.substring(0, 100)}`, 'error');
            return;
        }

        if (res.ok && data.success) {
            showToast(`✅ [${email}] 完善成功！2FA密钥: ${data.two_factor_secret || '已就绪'} | 试用: ${data.trial_status || '已同步'}`, 'success');
            await loadAccounts();
        } else {
            showToast(`❌ [${email}] 完善提示: ${data.error || '执行未完成'}`, 'error');
            await loadAccounts();
        }
    } catch (e) {
        showToast('请求出错: ' + e, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<span>⚡</span> 完善';
        }
    }
}

async function perfectAllAccounts() {
    const btn = document.getElementById('btnPerfectAll');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span>⏳</span> 批量完善已启动...';
    }

    try {
        const res = await fetch('/api/accounts/perfect-all', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        let data;
        const text = await res.text();
        try {
            data = JSON.parse(text);
        } catch (parseErr) {
            showToast(`服务器响应异常 (HTTP ${res.status}): ${text.substring(0, 100)}`, 'error');
            return;
        }
        if (res.ok) {
            showToast('已启动全量账号完善任务（全自动设置密码、开启真实2FA并同步试用资格画像）！', 'success');
            switchTab('dashboard');
        } else {
            showToast('启动批量完善失败: ' + (data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showToast('请求失败: ' + e, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<span>⚡</span> 一键全量完善(密码+2FA)';
        }
    }
}

// ==========================================
// 🌐 Chat2Api 联动管理
// ==========================================

async function loadChat2ApiStatus() {
    const statusTextEl = document.getElementById('c2aStatusText');
    const tokenCountEl = document.getElementById('c2aTokenCount');
    const btnStart = document.getElementById('btnStartC2A');
    const btnStop = document.getElementById('btnStopC2A');

    try {
        const res = await fetch('/api/chat2api/status');
        const data = await res.json();
        if (data.running) {
            if (statusTextEl) statusTextEl.innerHTML = '<span style="color:#16a34a">🟢 服务运行中 (端口: 5005)</span>';
            if (btnStart) {
                btnStart.disabled = true;
                btnStart.style.opacity = '0.5';
            }
            if (btnStop) {
                btnStop.disabled = false;
                btnStop.style.opacity = '1';
            }
        } else {
            if (statusTextEl) statusTextEl.innerHTML = '<span style="color:#dc2626">🔴 服务已停止</span>';
            if (btnStart) {
                btnStart.disabled = false;
                btnStart.style.opacity = '1';
            }
            if (btnStop) {
                btnStop.disabled = true;
                btnStop.style.opacity = '0.5';
            }
        }
        if (tokenCountEl) tokenCountEl.textContent = data.token_count ?? 0;
    } catch (e) {
        if (statusTextEl) statusTextEl.textContent = '检测异常';
    }
}

async function startChat2ApiService() {
    const btn = document.getElementById('btnStartC2A');
    if (btn) btn.disabled = true;
    showToast('正在启动 Chat2Api 引擎...', 'info');

    try {
        const res = await fetch('/api/chat2api/start', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'running') {
            showToast('Chat2Api 引擎已就绪！统一代理端口 8888 生效', 'success');
            await loadChat2ApiStatus();
        } else {
            showToast('启动结果: ' + (data.message || data.error || '失败'), 'error');
        }
    } catch (e) {
        showToast('请求启动失败: ' + e, 'error');
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function stopChat2ApiService() {
    if (!confirm("确定要停止 Chat2Api 引擎服务吗？")) return;
    const btn = document.getElementById('btnStopC2A');
    if (btn) btn.disabled = true;

    try {
        const res = await fetch('/api/chat2api/stop', { method: 'POST' });
        const data = await res.json();
        showToast('Chat2Api 引擎已停止', 'info');
        await loadChat2ApiStatus();
    } catch (e) {
        showToast('停止请求失败: ' + e, 'error');
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function syncTokensToChat2Api() {
    try {
        const res = await fetch('/api/chat2api/sync', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        const data = await res.json();
        if (!res.ok) {
            showToast(data.error || '凭证同步不可用', 'error');
            return;
        }
        showToast(`同步成功！新增注入 ${data.added_count} 个 Token，池中总计 ${data.total_pool_count} 个`, 'success');
        loadChat2ApiStatus();
    } catch (e) {
        showToast('同步请求失败: ' + e, 'error');
    }
}

// ==========================================
// 📥 账号批量导入控制器
// ==========================================
function openImportModal() {
    const modal = document.getElementById('importModal');
    if (modal) {
        modal.classList.remove('hidden');
        const ta = document.getElementById('importTextarea');
        if (ta) {
            ta.value = '';
            ta.focus();
        }
    }
}

function closeImportModal() {
    const modal = document.getElementById('importModal');
    if (modal) {
        modal.classList.add('hidden');
    }
}

async function submitImportAccounts() {
    const ta = document.getElementById('importTextarea');
    const text = ta ? ta.value.trim() : '';
    if (!text) {
        showToast('请先粘贴要导入的账号或邮箱内容', 'error');
        return;
    }

    const btn = document.getElementById('btnSubmitImport');
    if (btn) btn.disabled = true;

    try {
        const res = await fetch('/api/accounts/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: text })
        });
        const data = await res.json();
        if (data.success) {
            showToast(data.message || '账号导入成功！', 'success');
            closeImportModal();
            await loadAccounts();
        } else {
            showToast('导入失败: ' + (data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showToast('导入请求异常: ' + e, 'error');
    } finally {
        if (btn) btn.disabled = false;
    }
}

// ==========================================================================
// 🤖 全自动流水线：注册 → 真实补全 → 官方检测 → 最终报表
// ==========================================================================

const PHASE_LABELS = {
    starting: '准备中',
    register: '① 批量注册',
    perfect: '② 真实补全 (Web凭据 + 2FA)',
    verify: '③ 官方接口检测',
    report: '④ 生成报表',
    done: '✅ 全部完成',
    error: '❌ 流水线异常',
    idle: '空闲'
};

async function startAutopilot() {
    const countEl = document.getElementById('targetCount');
    const count = countEl ? parseInt(countEl.value || '1', 10) : 1;
    const verifyAllEl = document.getElementById('verifyAll');
    const staggerEl = document.getElementById('staggerSeconds');
    const skipEl = document.getElementById('skipCompletion');
    const stagger = staggerEl ? parseInt(staggerEl.value || '0', 10) : 0;
    const skipCompletion = skipEl ? skipEl.checked : false;

    const steps = skipCompletion
        ? `① 注册 ${count} 个账号（${stagger > 0 ? `每个间隔 ${stagger}s` : '连续'}）\n② 跳过浏览器补全（纯极速：不抓 Web 凭据、不绑 2FA）\n③ 官方接口检测\n④ 生成最终报表`
        : `① 注册 ${count} 个账号（${stagger > 0 ? `每个间隔 ${stagger}s` : '连续'}）\n② 真实补全（抓取真实 Web 凭据 + 官方确认后绑定真实 2FA）\n③ 通过官方接口检测真实订阅状态与 1 个月 Plus 试用资格\n④ 生成最终报表`;

    if (!window.confirm(`确认启动全自动流水线？\n\n将自动完成：\n${steps}\n\n运行期间请勿手动操作浏览器窗口。`)) {
        return;
    }

    const btn = document.getElementById('btnAutopilot');
    if (btn) btn.disabled = true;

    try {
        const res = await fetch('/api/autopilot/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                count,
                verify_all: verifyAllEl ? verifyAllEl.checked : true,
                stagger_seconds: stagger,
                skip_completion: skipCompletion,
            })
        });
        const data = await res.json();
        if (res.ok) {
            showToast('全自动流水线已启动，可在仪表盘查看实时进度', 'success');
            const box = document.getElementById('pipelineBox');
            if (box) box.classList.remove('hidden');
            switchTab('dashboard');
        } else {
            showToast('启动失败: ' + (data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showToast('启动请求异常: ' + e, 'error');
    } finally {
        if (btn) btn.disabled = false;
    }
}

function renderPipeline(pipeline) {
    if (!pipeline) return;
    const box = document.getElementById('pipelineBox');
    if (!box) return;

    if (pipeline.phase && pipeline.phase !== 'idle') {
        box.classList.remove('hidden');
    }

    const phaseEl = document.getElementById('pipelinePhase');
    const countEl = document.getElementById('pipelineCount');
    const barEl = document.getElementById('pipelineBar');
    const msgEl = document.getElementById('pipelineMessage');

    const label = PHASE_LABELS[pipeline.phase] || pipeline.phase || '';
    if (phaseEl) phaseEl.textContent = label;
    if (countEl) countEl.textContent = `${pipeline.done || 0}/${pipeline.total || 0}`;
    if (barEl) {
        const total = pipeline.total || 0;
        const percent = total > 0 ? Math.min(100, Math.round(((pipeline.done || 0) / total) * 100)) : 0;
        barEl.style.width = `${percent}%`;
    }
    if (msgEl) msgEl.textContent = pipeline.error ? pipeline.error : (pipeline.message || '');

    // 流水线刚跑完时自动把最终报表拉出来
    const finishedPhase = pipeline.phase === 'done';
    if (finishedPhase && lastPipelinePhase !== 'done' && pipeline.has_report) {
        showToast('全自动流水线完成，正在加载最终报表...', 'success');
        loadAutopilotReport();
        loadAccounts();
    }
    lastPipelinePhase = pipeline.phase;
}

async function loadAutopilotReport() {
    const tbody = document.getElementById('reportTableBody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:#64748b;padding:24px;">加载中...</td></tr>';
    try {
        const res = await fetch('/api/autopilot/report');
        if (res.status === 404) {
            tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:#64748b;padding:32px;">还没有报表数据：请先点击左侧「🤖 一键全自动流水线」</td></tr>';
            renderReportStats([]);
            return;
        }
        const data = await res.json();
        renderReport(data);
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="10" style="text-align:center;color:#ef4444;padding:24px;">加载失败: ${e}</td></tr>`;
    }
}

function renderReport(data) {
    window.reportRows = (data && data.rows) || [];
    renderReportRows(window.reportRows);

    const statusEl = document.getElementById('reportStatusLine');
    if (statusEl) {
        const summary = (data && data.summary) || {};
        const files = summary.files || {};
        statusEl.innerHTML = `生成时间: <b>${(data && data.generated_at) || '-'}</b> ｜ 账号总数: <b>${window.reportRows.length}</b> ｜ `
            + `注册成功: <b>${summary.registered_ok ?? '-'}</b> ｜ 补全成功: <b>${summary.perfect_ok ?? '-'}</b> ｜ `
            + `官方确认: <b>${summary.verified ?? '-'}</b>`
            + (files.markdown ? ` ｜ <a href="/token_exports/" target="_blank" rel="noopener noreferrer">报表文件</a>` : '');
    }
}

function filterReport() {
    const term = ((document.getElementById('reportSearch') || {}).value || '').toLowerCase().trim();
    const rows = window.reportRows || [];
    const filtered = term
        ? rows.filter(row => JSON.stringify(row).toLowerCase().includes(term))
        : rows;
    const visible = filtered.filter(row => !(row._hidden));
    // 应用排序（若已点击表头）
    const sorted = (reportSort.key && reportSort.dir)
        ? sortRows(visible, reportSort.key, reportSort.dir)
        : visible;
    window.lastReportRows = sorted;   // 供批量导出使用
    renderReportRows(sorted);
}

function renderReportRows(rows) {
    const tbody = document.getElementById('reportTableBody');
    if (!tbody) return;
    if (!rows || rows.length === 0) {
        tbody.innerHTML = '<tr><td colspan="11" style="text-align:center;color:#64748b;padding:32px;">暂无账号数据</td></tr>';
        renderReportStats([]);
        return;
    }

    tbody.innerHTML = '';
    rows.forEach(row => {
        const tr = document.createElement('tr');
        const evidenceTitle = (row.sources || []).map(s => `${s.name || '?'}: HTTP ${s.status ?? '?'}`).join(' / ');
        tr.title = `证据: ${evidenceTitle || '无'}${row.checked_at ? ` ；检测时间: ${row.checked_at}` : ''}`;
        tr.innerHTML = `
            <td class="batch-col"><input type="checkbox" class="batch-check" data-email="${row.email}"
                ${selectedEmails.has(row.email) ? 'checked' : ''}
                onchange="toggleRowSelect('${row.email}', this.checked)"></td>
            <td class="col-email sticky-col"><b class="copyable" onclick="copyText('${row.email}', '邮箱')" title="${row.email}">${row.email}</b></td>
            <td class="col-pwd"><span class="copyable" onclick="copyText('${row.password}', '密码')" style="font-family:'JetBrains Mono',monospace;font-size:12px;" title="点击复制真实密码">${displaySecret(row.password)}</span></td>
            <td class="col-2fa">${totpCellHtml(row.email, row.two_factor_secret)}</td>
            <td>${row.two_factor_secret
                ? `<span class="copyable" onclick="copyText((document.querySelector('[data-totp-code=\\'${row.email}\\']')||{}).textContent?.replace(/\\s/g,'') || '', '动态验证码')" style="font-family:'JetBrains Mono',monospace;font-weight:700;font-size:14px;color:#22c55e;">${row.two_factor_now ? row.two_factor_now.slice(0, 3) + ' ' + row.two_factor_now.slice(3) : '------'}</span>`
                : '<span style="color:var(--text-dim);font-size:12px;">无 2FA</span>'}</td>
            <td class="col-time" style="font-size:12px;color:var(--text-muted);white-space:nowrap;">${row.register_time || '-'}</td>
            <td class="col-trial">${trialBadgeHtml(row.trial_status, row.trial_eligible)}</td>
            <td class="col-plan">${plusBadgeHtml(row)}</td>
            <td class="col-expiry" style="font-size:12px;">${row.expires_at || '未知'}</td>
            <td class="col-health" style="font-size:11.5px;">
                <div>${row.account_status || '未验证'}</div>
                <div style="color:var(--text-dim);font-size:10.5px;margin-top:2px;">${row.quota || ''}</div>
            </td>
            <td class="col-inbox" style="font-size:11px;">
                <div>[${row.provider || '-'}]</div>
                <div style="color:var(--text-dim);font-size:10.5px;word-break:break-all;">${(row.mailbox_credential || '').slice(0, 40)}</div>
            </td>
        `;
        tbody.appendChild(tr);
    });
    renderReportStats(rows);
    if (typeof startTotpTicker === 'function') startTotpTicker();
}

function renderReportStats(rows) {
    const list = rows || [];
    const set = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    };
    set('reportTotal', list.length);
    set('reportVerified', list.filter(r => r.verified).length);
    set('reportPlus', list.filter(r => r.is_plus === true).length);
    set('reportTrial', list.filter(r => r.trial_eligible === true || isTrialEligible(r.trial_status)).length);
    set('report2fa', list.filter(r => r.two_factor_secret).length);
}

async function copyReport2fa() {
    const rows = window.reportRows || [];
    if (!rows.length) {
        showToast('暂无报表数据', 'info');
        return;
    }
    const text = rows
        .map(row => `${row.email}-----${row.password || 'N/A'}-----${row.two_factor_secret || '未开启2FA'}`)
        .join('\n');
    try {
        await navigator.clipboard.writeText(text);
        showToast(`已复制 ${rows.length} 条 邮箱-----密码-----2FA 到剪贴板`, 'success');
    } catch (e) {
        showToast('复制失败: ' + e, 'error');
    }
}

async function exportAutopilotReport() {
    try {
        const res = await fetch('/api/autopilot/export', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            showToast('报表已落盘: ' + Object.values(data.files || {}).join(' | '), 'success');
        } else {
            showToast('导出失败: ' + (data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showToast('导出请求异常: ' + e, 'error');
    }
}



