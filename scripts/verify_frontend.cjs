// Exercise displayed counts and filters without calling external services.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

(async () => {
    for (const edition of ['gpt-auto-register', 'gpt-protocol-register']) {
        const elements = new Map();
        const element = id => {
            if (!elements.has(id)) elements.set(id, { textContent: '', value: '', innerHTML: '' });
            return elements.get(id);
        };
        const context = vm.createContext({
            window: {},
            document: { addEventListener() {}, getElementById: element },
            console,
        });
        const source = fs.readFileSync(path.join(__dirname, '..', edition, 'static', 'script.js'), 'utf8');
        vm.runInContext(source, context);
        const accounts = [
            { email: 'legacy', plan: 'Free', trial_status: '享有试用资格', status: '已注册', account_status: '🟢 正常可用' },
            { email: 'negative', plan: '未检测', trial_status: '没有试用资格' },
            { email: 'confirmed', plan: 'Free', trial_status: '已确认有试用资格' },
            { email: 'offline', plan: '未验证 (Plus)', account_status: '⚪ 未在线验证' },
            { email: 'verified', plan: 'Plus', verified: true, account_status: '🟢 在线已验证' },
        ];
        context.updateAccountStats(accounts);
        assert.equal(element('statNormalAccs').textContent, 1);
        assert.equal(element('statTrialAccs').textContent, 1);
        assert.equal(element('tabCountFree').textContent, 2);
        assert.equal(element('tabCountPaid').textContent, 1);
        context.window.allAccounts = accounts;
        let displayed;
        context.renderAccounts = rows => { displayed = rows.map(row => row.email); };
        vm.runInContext("currentAccountFilter = 'trial'; filterAccounts();", context);
        assert.deepEqual(Array.from(displayed), ['confirmed']);
        vm.runInContext("currentAccountFilter = 'free'; filterAccounts();", context);
        assert.deepEqual(Array.from(displayed), ['legacy', 'confirmed']);
        let toast;
        context.showToast = (message, type) => { toast = { message, type }; };
        context.fetch = async () => ({ ok: false, json: async () => ({ error: '凭证类型不匹配' }) });
        context.loadChat2ApiStatus = () => { throw new Error('Rejected sync must not report success'); };
        await context.syncTokensToChat2Api();
        assert.deepEqual(toast, { message: '凭证类型不匹配', type: 'error' });
        context.fetch = async () => ({ ok: true, json: async () => ({ verified: false, persisted: true }) });
        context.loadAccounts = async () => {};
        await context.checkSingleAccount('owner', null);
        assert.equal(toast.type, 'info');
        assert.ok(toast.message.includes('未知'));
        assert.ok(!toast.message.includes('永久'));
        console.log(`${edition}: counts, filters and rejected sync passed`);
    }
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
