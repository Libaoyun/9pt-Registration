"""邮箱别名分裂工具：1 分为 2（或 N），全局随机打乱。

原理：iCloud 等邮箱支持 plus 别名（`前缀+随机@域名`），
所有别名的邮件进入原始收件箱 → 注册量翻倍而收件箱不用换。

用法::

    # 从卡密文件分裂（每行 邮箱----URL 或纯邮箱）
    python scripts/split_alias.py --input "D:\\Downloads\\卡密导出.txt" --copies 2

    # 输出到指定文件
    python scripts/split_alias.py --input in.txt --copies 2 --out out.txt

    # 直接对接注册（跳过已注册的原始邮箱）
    python scripts/split_alias.py --input in.txt --copies 2 --skip-registered
"""

from __future__ import annotations

import argparse
import random
import re
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gpt-auto-register"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def split_email_alias(email: str, copies: int, rng: random.Random) -> list[str]:
    """把 email 分裂成 copies 个 plus 别名。

    规则：`前缀+6位随机字符@域名`
    """
    m = re.match(r"^([^+@]+)@([^+@]+)$", email.strip())
    if not m:
        return [email] * copies if email else []
    prefix, domain = m.group(1), m.group(2)
    result = []
    used = set()
    for _ in range(copies):
        while True:
            tag = "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(6))
            alias = f"{prefix}+{tag}@{domain}"
            if alias not in used:
                used.add(alias)
                result.append(alias)
                break
    return result


def parse_line(line: str) -> tuple[str, str]:
    """解析一行，返回 (邮箱, 其余部分如 URL)。"""
    line = line.strip().rstrip(";").lstrip("\ufeff")
    if not line or line.startswith("#"):
        return "", ""
    for sep in ("----", "---", "|", "\t"):
        if sep in line:
            email, _, rest = line.partition(sep)
            return email.strip(), rest.strip()
    return line.strip(), ""


def main() -> int:
    parser = argparse.ArgumentParser(description="邮箱别名分裂：1 分为 N + 全局打乱")
    parser.add_argument("--input", "-i", required=True, help="输入文件（每行 邮箱----URL 或纯邮箱）")
    parser.add_argument("--copies", "-c", type=int, default=2, help="每个邮箱分裂成几个别名（默认2）")
    parser.add_argument("--out", "-o", default="", help="输出文件（默认与输入同目录 _分裂_打乱.txt）")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（可复现）")
    parser.add_argument("--skip-registered", action="store_true",
                        help="跳过账号库中已注册成功的原始邮箱")
    args = parser.parse_args()

    in_path = Path(args.input)
    if not in_path.is_file():
        print(f"❌ 输入文件不存在: {in_path}")
        return 1

    rng = random.Random(args.seed) if args.seed is not None else random.SystemRandom()

    # 可选：加载已注册的原始邮箱列表
    registered = set()
    if args.skip_registered:
        try:
            from app.stored_accounts import load_accounts_from_file
            from app.config import cfg, PROJECT_ROOT

            acc_path = Path(cfg.files.accounts_file)
            if not acc_path.is_absolute():
                acc_path = PROJECT_ROOT / acc_path
            if acc_path.exists():
                for record in load_accounts_from_file(str(acc_path)):
                    registered.add(record["email"].lower().split("+")[0])
                print(f"📖 已加载账号库：{len(registered)} 个已注册原始邮箱（将跳过其分裂）")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ 读取账号库失败（忽略跳过逻辑）: {exc}")

    out_lines = []
    total_src = skipped = 0
    for line in in_path.read_text(encoding="utf-8", errors="replace").splitlines():
        email, rest = parse_line(line)
        if not email:
            continue
        total_src += 1
        base_lower = email.lower().split("+")[0]
        if args.skip_registered and base_lower in registered:
            skipped += 1
            print(f"  ⏭️ 跳过已注册: {email}")
            continue
        aliases = split_email_alias(email, args.copies, rng)
        for alias in aliases:
            if rest:
                out_lines.append(f"{alias}----{rest}")
            else:
                out_lines.append(alias)

    # 全局随机打乱
    rng.shuffle(out_lines)

    out_path = Path(args.out) if args.out else in_path.parent / f"{in_path.stem}_分裂_打乱.txt"
    out_path.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")

    print(f"\n📊 分裂完成")
    print(f"   源文件: {in_path}  ({total_src} 个邮箱)")
    if skipped:
        print(f"   跳过已注册: {skipped} 个")
    print(f"   分裂后: {len(out_lines)} 个（每个源 × {args.copies}）")
    print(f"   全局打乱: ✅")
    print(f"   输出: {out_path}")
    print(f"\n前 5 行预览:")
    for line in out_lines[:5]:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
