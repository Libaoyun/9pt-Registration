"""把浏览器版（gpt-auto-register）的共享模块同步到协议版（gpt-protocol-register）。

两个控制台共用同一套注册/检测/2FA/存储逻辑，只有引擎与入口不同：

  共享模块（本脚本负责拷贝，保持字节一致）:
    account_probe.py        真实账号状态采集器
    account_checker.py      检测 + 证据采集桥接
    two_factor_service.py   真实 2FA (TOTP) 绑定
    account_perfector.py    账号完善（登录/凭据/2FA/权益）
    stored_accounts.py      账号记录读写
    pipeline.py             全自动流水线编排
    browser.py              浏览器自动化
    email_providers.py      邮箱服务注册表
    mailtm_service.py / gptmail_service.py / tempmail_lol_service.py
    temporam_service.py / custom2925_service.py / icloud_service.py
    utils.py                （拷贝后按协议版约定回填差异）

  各自独立、不拷贝:
    main.py / server.py / protocol_register.py / sentinel.py / config.py / static/*

用法:
    python scripts/sync_shared_modules.py            # 同步并校验
    python scripts/sync_shared_modules.py --check    # 只校验，不写入
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

try:  # Windows 控制台默认 GBK，这里强制 UTF-8 以免 emoji 打印报错
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
BROWSER = ROOT / "gpt-auto-register"
PROTOCOL = ROOT / "gpt-protocol-register"

SHARED_MODULES = (
    "account_probe.py",
    "account_checker.py",
    "two_factor_service.py",
    "account_perfector.py",
    "stored_accounts.py",
    "pipeline.py",
    "browser.py",
    "email_providers.py",
    "mailtm_service.py",
    "gptmail_service.py",
    "tempmail_lol_service.py",
    "temporam_service.py",
    "custom2925_service.py",
    "icloud_service.py",
    "utils.py",
)

# utils.py 的协议版差异：证据里记录 register_mode=protocol，并额外提供假人资料生成
UTILS_PATCHES = (
    ('evidence_val["register_mode"] = "browser"', 'evidence_val["register_mode"] = "protocol"'),
    ('merged_evidence["register_mode"] = "browser"', 'merged_evidence["register_mode"] = "protocol"'),
)

PERSON_HELPER = '''

def get_random_person_info() -> dict:
    """生成随机假人信息 (含全名与 ISO 格式生日)"""
    info = generate_user_info()
    name = info['name']
    year = info['year']
    month = info['month']
    day = info['day']
    iso_date = f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    return {
        "full_name": name,
        "birthday_iso": iso_date,
        "year": year,
        "month": month,
        "day": day,
    }
'''


def digest(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def apply_utils_patches(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new in UTILS_PATCHES:
        text = text.replace(old, new)
    if "def get_random_person_info(" not in text:
        text = text.rstrip() + "\n" + PERSON_HELPER
    path.write_text(text, encoding="utf-8")


def expected_target_text(name: str, source_text: str) -> str:
    """协议版对 utils.py 有约定差异，校验时按同样规则模拟。"""
    if name != "utils.py":
        return source_text
    text = source_text
    for old, new in UTILS_PATCHES:
        text = text.replace(old, new)
    if "def get_random_person_info(" not in text:
        text = text.rstrip() + "\n" + PERSON_HELPER
    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验，不写入")
    args = parser.parse_args()

    changed: list[str] = []
    for name in SHARED_MODULES:
        source = BROWSER / "app" / name
        target = PROTOCOL / "app" / name
        if not source.is_file():
            print(f"❌ 缺少源文件: {source}")
            return 1
        expected = expected_target_text(name, source.read_text(encoding="utf-8"))
        same = target.is_file() and target.read_text(encoding="utf-8") == expected
        if same:
            continue
        if args.check:
            print(f"⚠️ 不一致: {name}")
            changed.append(name)
            continue
        shutil.copy2(source, target)
        if name == "utils.py":
            apply_utils_patches(target)
        print(f"✅ 已同步: {name}")
        changed.append(name)

    if args.check and changed:
        print(f"\n共 {len(changed)} 个文件需要同步")
        return 1
    print("🎉 共享模块已保持一致" if not changed else f"🎉 同步完成，共 {len(changed)} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
