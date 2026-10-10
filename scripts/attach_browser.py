"""附加到正在运行的自动化 Chrome，读取当前真实页面状态（只读，不干扰自动化）。

用途：当自动化卡住/失败时，实时看清页面到底停在哪一步、有没有官方报错。

用法::

    python scripts/attach_browser.py                # 自动发现调试端口
    python scripts/attach_browser.py --port 64726   # 指定端口
    python scripts/attach_browser.py --shot out.png # 顺便截图
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gpt-auto-register"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def discover_port() -> int | None:
    import subprocess

    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
             "Where-Object { $_.CommandLine -match 'remote-debugging-port' } | "
             "ForEach-Object { if ($_.CommandLine -match '--remote-debugging-port=(\\d+)') { $Matches[1] } }"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 无法枚举进程: {exc}")
        return None
    for token in out.split():
        if token.isdigit():
            return int(token)
    return None


def list_tabs(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


INSPECT_JS = r"""
const modalText = (() => {
  for (const sel of ['[role="dialog"]', 'div[class*="modal" i]', 'form']) {
    const nodes = Array.from(document.querySelectorAll(sel)).filter(n => n.offsetParent !== null);
    if (nodes.length) return nodes[0].innerText.slice(0, 1200);
  }
  return (document.body.innerText || '').slice(0, 800);
})();
const inputs = Array.from(document.querySelectorAll('input')).map(i => ({
  type: i.type, name: i.name, autocomplete: i.autocomplete,
  placeholder: i.placeholder, value: (i.value || '').slice(0, 60),
  visible: i.offsetParent !== null,
}));
const buttons = Array.from(document.querySelectorAll('button')).filter(b => b.offsetParent !== null)
  .map(b => ({ text: (b.innerText || '').trim().slice(0, 40), type: b.type, disabled: b.disabled }));
const alerts = Array.from(document.querySelectorAll('[role="alert"], [class*="error" i]'))
  .filter(n => n.offsetParent !== null).map(n => (n.innerText || '').trim()).filter(Boolean);
return { url: location.href, title: document.title, modalText, inputs, buttons, alerts };
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="挂到运行中的自动化 Chrome 读取真实页面状态")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--shot", default="")
    args = parser.parse_args()

    port = args.port or discover_port()
    if not port:
        print("❌ 未发现带 remote-debugging-port 的 Chrome")
        return 1
    print(f"🔌 调试端口: {port}")

    try:
        tabs = list_tabs(port)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 无法连接调试端口: {exc}")
        return 1
    for tab in tabs:
        if tab.get("type") == "page":
            print(f"   标签: {tab.get('title')} | {tab.get('url')}")

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        info = driver.execute_script(INSPECT_JS)
        print("\n" + "=" * 72)
        print(f"URL  : {info['url']}")
        print(f"标题 : {info['title']}")
        if info["alerts"]:
            print(f"⚠️ 官方提示: {info['alerts']}")
        print("\n可见输入框:")
        for item in info["inputs"]:
            if item["visible"]:
                print(f"  - type={item['type']} name={item['name']} "
                      f"autocomplete={item['autocomplete']} placeholder={item['placeholder']} value={item['value']!r}")
        print("\n可见按钮:")
        for item in info["buttons"]:
            print(f"  - {item['text']!r} type={item['type']} disabled={item['disabled']}")
        print("\n弹窗/表单正文:")
        print(info["modalText"])
        print("=" * 72)
        if args.shot:
            driver.save_screenshot(args.shot)
            print(f"📸 截图: {args.shot}")
    finally:
        # 只断开连接，不关闭用户正在使用的浏览器
        if driver is not None:
            try:
                driver.service.stop() if getattr(driver, "service", None) else None
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
