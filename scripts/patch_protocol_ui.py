from pathlib import Path
import sys

root = sys.argv[1]
p = Path(root) / 'gpt-protocol-register' / 'static' / 'index.html'
t = p.read_text(encoding='utf-8')
changes = []

THEME_BTN = (
    '<button id="btnToggleTheme" class="theme-toggle action-btn-sm" onclick="toggleTheme()" '
    'title="切换深色 / 浅色模式（记忆选择）" '
    'style="margin-left:auto;padding:5px 9px;font-size:11px;white-space:nowrap;">'
    '🌙 深色模式</button>'
)

BATCH_BTN = (
    '<button id="btnBatchMode" class="action-btn-sm" onclick="toggleBatchMode()" '
    'title="开启后可勾选多行并批量复制">'
    '<span>☑️</span> 批量模式</button>'
)

BATCH_BAR = '''<div class="batch-bar">
                    <span>\u2611\ufe0f \u6279\u91cf\u6a21\u5f0f\uff1a\u5df2\u9009 <b id="batchCount">0</b> \u4e2a</span>
                    <label style="display:flex;align-items:center;gap:6px;cursor:pointer;">
                        <input type="checkbox" id="batchSelectAll" onchange="toggleSelectAll(this.checked)"> \u5168\u9009/\u53d6\u6d88
                    </label>
                    <button class="action-btn-sm" onclick="batchExportSelected()" title="\u590d\u5236\u6240\u9009\u8d26\u53f7\uff08\u90ae\u7bb1-----\u5bc6\u7801-----2FA\uff09">
                        <span>\U0001f4cb</span> \u590d\u5236\u6240\u9009
                    </button>
                </div>

'''

# 1) 品牌区加主题按钮
if 'btnToggleTheme' not in t:
    marker = '</div>\n\n        <nav class="nav-menu">'
    if marker in t:
        t = t.replace(marker, '            ' + THEME_BTN + '\n        ' + marker, 1)
        changes.append('theme-button')
    elif '</div>\n        <nav class="nav-menu">' in t:
        marker2 = '</div>\n        <nav class="nav-menu">'
        t = t.replace(marker2, '            ' + THEME_BTN + '\n        ' + marker2, 1)
        changes.append('theme-button')

# 2) 批量模式按钮（放在敏感信息按钮后）
if 'btnBatchMode' not in t and 'btnToggleSensitive' in t:
    idx = t.index('btnToggleSensitive')
    end = t.index('</button>', idx) + len('</button>')
    t = t[:end] + '\n                            ' + BATCH_BTN + t[end:]
    changes.append('batch-button')

# 3) 批量条 + 排序表头（报表区）
if 'batch-bar' not in t:
    anchor = '<div class="table-container">'
    if anchor in t:
        t = t.replace(anchor, BATCH_BAR + anchor, 1)
        changes.append('batch-bar')

if 'col-email sticky-col sortable' not in t and 'col-email sticky-col' in t:
    t = t.replace('<th class="col-email sticky-col">',
                  '<th class="batch-col"><span title="\u6279\u91cf\u9009\u62e9">☑</span></th>\n'
                  '                                '
                  '<th class="col-email sticky-col sortable" data-sort-key="email" '
                  'onclick="toggleReportSort(\'email\')">', 1)
    changes.append('email-th')

sort_map = [
    ('<th class="col-time">', '<th class="col-time sortable" data-sort-key="register_time" '
     'onclick="toggleReportSort(\'register_time\')">'),
    ('<th class="col-trial">', '<th class="col-trial sortable" data-sort-key="trial" '
     'onclick="toggleReportSort(\'trial\')">'),
    ('<th class="col-plan">', '<th class="col-plan sortable" data-sort-key="plus" '
     'onclick="toggleReportSort(\'plus\')">'),
]
for old, new in sort_map:
    if old in t and 'sortable' not in old:
        t = t.replace(old, new, 1)
        changes.append('sort-th:' + old[5:-2])

p.write_text(t, encoding='utf-8')
print('protocol index.html changes:', changes if changes else 'none (already present)')
