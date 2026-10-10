"""Clash 自动节点轮换 —— 批量注册的"无限 IP 配额"方案。

原理：Clash/Verge 内核提供 RESTful API（默认 127.0.0.1:9097），
可列出代理组与节点、切换选中节点。每切换一次节点 = 换一个出口 IP
= OpenAI 侧配额重置。

使用前提（Clash Verge）：
    设置 → Clash 内核 → 外部控制 → 启用外部控制器
    监听地址 127.0.0.1:9097；API 访问密钥留空（或有值则 --secret 传入）

用法::

    # 查看当前代理组与节点
    python scripts/clash_rotate.py --list

    # 切换到下一个节点（自动选"当前选中之外的"）
    python scripts/clash_rotate.py --next

    # 在批量注册脚本里集成：每注册 N 个自动 --next
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _api(base: str, secret: str, path: str, method: str = "GET", body: dict | None = None) -> any:
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", "application/json")
    if secret:
        req.add_header("Authorization", f"Bearer {secret}")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=8) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        body_txt = ""
        try:
            body_txt = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Clash API {method} {path} -> HTTP {exc.code}: {body_txt[:200]}") from exc


def find_selector(base: str, secret: str, prefer_names: tuple[str, ...] = ()) -> tuple[str, list[str], str]:
    """找到"可切换的代理组"（type=Selector），返回 (组名, 节点列表, 当前节点)。

    prefer_names：优先匹配的组名关键词（如 Proxy / 节点选择 / GLOBAL）。
    """
    data = _api(base, secret, "/proxies")
    proxies = data.get("proxies", {}) if isinstance(data, dict) else {}
    selectors = []
    for name, info in proxies.items():
        if not isinstance(info, dict):
            continue
        if info.get("type") not in ("Selector", "URLTest", "Fallback"):
            continue
        all_nodes = info.get("all") or []
        # 过滤掉内置占位项
        nodes = [n for n in all_nodes if n not in ("DIRECT", "REJECT", "GLOBAL", "PROXY", name)]
        if len(nodes) >= 2:
            selectors.append((name, nodes, info.get("now") or ""))
    # 优先按关键词匹配
    for prefer in prefer_names:
        for name, nodes, now in selectors:
            if prefer.lower() in name.lower():
                return name, nodes, now
    if selectors:
        # 选节点数最多的组
        selectors.sort(key=lambda x: len(x[1]), reverse=True)
        name, nodes, now = selectors[0]
        return name, nodes, now
    raise RuntimeError("未找到可切换的代理组（确认 Clash 已运行且外部控制器已启用）")


def rotate(base: str, secret: str, prefer_names: tuple[str, ...] = (), avoid: str = "") -> str:
    """切换到组内"当前选中之外"的下一个节点，返回新节点名。"""
    name, nodes, now = find_selector(base, secret, prefer_names)
    candidates = [n for n in nodes if n != now]
    if not candidates:
        raise RuntimeError(f"组 {name} 只有当前节点可用，无法轮换")
    # 顺序轮换：取当前节点在列表中的下一个
    try:
        idx = nodes.index(now)
        nxt = nodes[(idx + 1) % len(nodes)]
    except ValueError:
        nxt = candidates[0]
    _api(base, secret, f"/proxies/{urllib.parse.quote(name)}", method="PUT", body={"name": nxt})
    return nxt


def main() -> int:
    parser = argparse.ArgumentParser(description="Clash 节点自动轮换")
    parser.add_argument("--api", default="http://127.0.0.1:9097", help="Clash 外部控制地址")
    parser.add_argument("--secret", default="", help="API 访问密钥（未设则留空）")
    parser.add_argument("--list", action="store_true", help="列出可切换的代理组与节点")
    parser.add_argument("--next", action="store_true", help="切换到下一个节点")
    parser.add_argument("--group", default="", help="指定代理组名（默认自动选择）")
    parser.add_argument("--wait", type=int, default=3, help="切换后等待秒数（让连接稳定）")
    args = parser.parse_args()

    prefer = ("Proxy", "节点选择", "PROXY", "GLOBAL", args.group) if args.group else \
             ("Proxy", "节点选择", "PROXY", "GLOBAL")

    if args.list:
        name, nodes, now = find_selector(args.api, args.secret, prefer)
        print(f"代理组: {name}\n当前节点: {now}\n可选节点 ({len(nodes)}):")
        for n in nodes:
            mark = " ← 当前" if n == now else ""
            print(f"  - {n}{mark}")
        return 0

    if args.next:
        import time as _time
        # 先拿当前（用于 avoid）
        _, nodes, now = find_selector(args.api, args.secret, prefer)
        new_node = rotate(args.api, args.secret, prefer, avoid=now)
        print(f"✅ 节点已切换: {now} → {new_node}")
        _time.sleep(args.wait)
        # 验证出口 IP 已变化
        try:
            import urllib.request
            proxy_handler = urllib.request.ProxyHandler({
                "http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})
            opener = urllib.request.build_opener(proxy_handler)
            ip = opener.open("https://api.ipify.org", timeout=12).read().decode()
            print(f"✅ 新出口 IP: {ip}")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ 出口 IP 查询失败: {exc}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    import urllib.parse  # noqa: F401  (rotate 内使用)
    raise SystemExit(main())
