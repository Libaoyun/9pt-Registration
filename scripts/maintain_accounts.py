"""账号库维护：去 BOM、合并重复行、保留信息最完整的一条。

背景（真实踩到的坑）：
  - 用带 BOM 的 UTF-8 写账号清单时，首个邮箱会被写成 `\ufeffxxx@icloud.com`，
    导致按邮箱查询/更新匹配失败；
  - 同一邮箱被多次写入会留下多行，其中可能有一行是早期不完整（无密钥/无证据）的版本。

用法::

    python scripts/maintain_accounts.py --dry-run     # 只看会改什么
    python scripts/maintain_accounts.py               # 实际整理并写回（自动备份）
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gpt-auto-register"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 合并同邮箱多行时的优先级：密钥 > 已验证证据 > 计划明确 > 行更长
def _richness(parts: list[str]) -> tuple:
    secret = parts[11].strip() if len(parts) > 11 else ""
    has_secret = bool(secret) and secret not in ("未开启", "N/A")
    evidence = {}
    if len(parts) > 12:
        try:
            evidence = json.loads(parts[12])
        except (ValueError, TypeError):
            evidence = {}
    verified = bool(evidence.get("verified"))
    plan = parts[6].strip() if len(parts) > 6 else ""
    plan_known = plan not in ("", "未检测")
    return (has_secret, verified, plan_known, sum(len(p) for p in parts))


def main() -> int:
    parser = argparse.ArgumentParser(description="账号库去 BOM / 合并重复行")
    parser.add_argument("--file", default="", help="账号文件路径（默认取配置）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from app.config import cfg, PROJECT_ROOT

    path = Path(args.file) if args.file else Path(cfg.files.accounts_file)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.is_file():
        print(f"❌ 找不到账号文件: {path}")
        return 1

    raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    merged: dict[str, list[str]] = {}
    order: list[str] = []
    bom_fixed = 0
    for line in raw_lines:
        if not line.strip():
            continue
        cleaned = line.lstrip("\ufeff")
        if cleaned != line:
            bom_fixed += 1
        parts = [p.strip() for p in cleaned.split("|")]
        if not parts or "@" not in parts[0]:
            continue
        email = parts[0].lstrip("\ufeff")
        parts[0] = email
        key = email.lower()
        if key not in merged:
            merged[key] = parts
            order.append(key)
        else:
            keep = merged[key] if _richness(merged[key]) >= _richness(parts) else parts
            merged[key] = keep

    duplicates = len([1 for line in raw_lines if line.strip() and "@" in line]) - len(order)
    print(f"📄 文件: {path}")
    print(f"   原始行数: {len([1 for line in raw_lines if line.strip()])}")
    print(f"   去除 BOM 的行: {bom_fixed}")
    print(f"   合并掉的重复行: {max(0, duplicates)}")
    print(f"   整理后唯一邮箱: {len(order)}")

    if args.dry_run:
        print("\n（--dry-run 模式，未写入）")
        return 0

    backup = path.with_suffix(path.suffix + f".bak_{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copy2(path, backup)
    path.write_text("\n".join("|".join(merged[key]) for key in order) + "\n", encoding="utf-8")
    print(f"   💾 已写回（备份: {backup.name}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
