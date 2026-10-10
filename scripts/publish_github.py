"""把工作区里的各个项目分别发布到 GitHub 仓库的不同分支。

约定（与用户要求一致）：
  - 一个项目 = 一个分支
  - main 分支放总说明与验证工具脚本
  - **绝不**上传真实账号数据（data/、token_exports/、tokens、诊断截图）与虚拟环境

用法::

    python scripts/publish_github.py --repo https://github.com/Libaoyun/9pt-Registration.git --push
    python scripts/publish_github.py --repo <url>            # 只本地提交，不推送
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE_DIR = ROOT / ".release" / "9pt-Registration"

# 分支名 -> 源目录（None 表示 main 分支，只放说明与脚本）
BRANCHES: list[tuple[str, str | None]] = [
    ("main", None),
    ("gpt-auto-register", "gpt-auto-register"),
    ("gpt-protocol-register", "gpt-protocol-register"),
    ("chat2api", "chat2api"),
]

# 任何情况下都不进仓库的路径/文件名
EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", "data", "token_exports",
    ".release", ".workbuddy", "archive", "dist", "build", ".pytest_cache", ".mypy_cache",
    "diagnostics",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".lock", ".png", ".jpg", ".jpeg"}

MAIN_README = """# 9pt-Registration

本仓库用**分支**承载多个项目（一个项目一个分支）：

| 分支 | 项目 | 说明 |
| --- | --- | --- |
| `main` | 说明与验证工具 | 本文件 + `scripts/` 下的本地回归与真机验证工具 |
| `gpt-auto-register` | 浏览器自动化版 | Flask + Selenium/undetected-chromedriver，控制台端口 8888 |
| `gpt-protocol-register` | 极速协议版 | curl_cffi + Sentinel PoW，控制台端口 8889 |
| `chat2api` | ChatGPT Web 兼容网关 | FastAPI，端口 5005 |

## 一键全自动流水线

`注册 → 真实补全（Web 凭据 + 官方确认的 2FA）→ 官方接口检测 → 直接产出报表`

- 面板：左侧 **🤖 一键全自动流水线**，跑完在 **🧾 最终报表** 查看
  `邮箱 / 密码 / 2FA 密钥(32位) / 动态验证码 / 注册时间 / 1 个月 Plus 试用资格 / 当前 Plus 状态 / 到期时间 / 在线证据`
- 命令行：`python -m app.main --auto --count 3`
- 报表落盘：`token_exports/final_<时间戳>/`（JSON / CSV / Markdown / `邮箱-----密码-----2FA`）

## 数据真实性

所有字段只写官方接口真实返回的内容，并保留原始报文证据（`data/tokens/state-<邮箱>.json`）；
拿不到证据一律写“未知”。2FA 密钥只在官方接口返回后写入，不会生成占位值。

## 安全说明

本仓库**不包含**任何真实账号数据、Cookie、Token 或虚拟环境；这些都在 `.gitignore` 中排除。
"""

GITIGNORE = """# 真实数据与运行产物（绝不入库）
data/
token_exports/
*.lock
*.log
.release/
.workbuddy/
archive/
node_modules/
.venv/
venv/
__pycache__/
*.pyc
*.pyo
.pytest_cache/
.mypy_cache/
dist/
build/
.idea/
.vscode/
"""


def run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    if check and result.returncode:
        print(f"  ⚠️ 命令失败: {' '.join(args)}\n{result.stdout[-800:]}\n{result.stderr[-800:]}")
    return result


def copy_tree(src: Path, dst: Path) -> int:
    count = 0
    for path in src.rglob("*"):
        relative = path.relative_to(src)
        if any(part in EXCLUDE_DIRS for part in relative.parts):
            continue
        if path.suffix.lower() in EXCLUDE_SUFFIXES and path.is_file():
            # 保留 requirements/lock 之外的二进制与日志都不要
            if path.suffix.lower() != ".lock":
                continue
        target = dst / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            count += 1
    return count


def clear_worktree(repo: Path) -> None:
    for entry in repo.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="按分支发布各项目到 GitHub 仓库")
    parser.add_argument("--repo", required=True, help="远端仓库 URL")
    parser.add_argument("--push", action="store_true", help="提交后推送到远端")
    parser.add_argument("--force-push", action="store_true", help="使用 --force 推送")
    args = parser.parse_args()

    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    if not (RELEASE_DIR / ".git").is_dir():
        run(["git", "init", "-b", "main"], RELEASE_DIR)
    run(["git", "config", "core.autocrlf", "false"], RELEASE_DIR)
    run(["git", "remote", "remove", "origin"], RELEASE_DIR, check=False)
    run(["git", "remote", "add", "origin", args.repo], RELEASE_DIR)

    for branch, source in BRANCHES:
        print(f"\n=== 分支 {branch} ===")
        clear_worktree(RELEASE_DIR)
        run(["git", "checkout", "--orphan", branch], RELEASE_DIR, check=False)

        if source is None:
            (RELEASE_DIR / "README.md").write_text(MAIN_README, encoding="utf-8")
            (RELEASE_DIR / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
            scripts_dir = RELEASE_DIR / "scripts"
            scripts_dir.mkdir(exist_ok=True)
            for script in (ROOT / "scripts").glob("*"):
                if script.is_file() and script.suffix.lower() in {".py", ".cjs"}:
                    shutil.copy2(script, scripts_dir / script.name)
            # 根目录的中文说明文档一并发布（焚决 / 使用说明书 / 验证报告）
            docs_copied = []
            for doc in ("焚决.md", "使用说明书.md", "优化验证报告.md"):
                src_doc = ROOT / doc
                if src_doc.is_file():
                    shutil.copy2(src_doc, RELEASE_DIR / doc)
                    docs_copied.append(doc)
            extra = f"，文档: {'/'.join(docs_copied)}" if docs_copied else ""
            print(f"  main: 已写入 README/.gitignore/scripts{extra}")
        else:
            src = ROOT / source
            if not src.is_dir():
                print(f"  ⚠️ 源目录不存在，跳过: {src}")
                continue
            (RELEASE_DIR / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
            copied = copy_tree(src, RELEASE_DIR)
            print(f"  已拷贝 {copied} 个文件")

        run(["git", "add", "-A"], RELEASE_DIR)
        status = run(["git", "status", "--porcelain"], RELEASE_DIR)
        if not status.stdout.strip():
            print("  无变更，跳过提交")
            continue
        run(["git", "commit", "-m", f"feat: {branch} 项目上传（全自动流水线 + 真实数据证据）"], RELEASE_DIR)
        head = run(["git", "rev-parse", "HEAD"], RELEASE_DIR).stdout.strip()[:8]
        print(f"  ✅ 已提交 {head}")

    run(["git", "checkout", "main"], RELEASE_DIR, check=False)

    if args.push:
        print("\n=== 推送到远端 ===")
        push_args = ["git", "push", "-u", "origin"]
        if args.force_push:
            push_args.append("--force")
        push_args.append("main")
        result = run(push_args, RELEASE_DIR, check=False)
        if result.returncode:
            print("❌ main 推送失败（可能需要凭据）")
            print(result.stderr[-1200:])
            return 1
        for branch, _ in BRANCHES[1:]:
            push_cmd = ["git", "push", "-u", "origin", branch]
            if args.force_push:
                push_cmd.insert(3, "--force")
            result = run(push_cmd, RELEASE_DIR, check=False)
            if result.returncode:
                print(f"❌ {branch} 推送失败")
                print(result.stderr[-800:])
                return 1
            print(f"  ✅ 已推送分支 {branch}")
        print("🎉 全部分支推送完成")
    else:
        print(f"\n本地已提交到 {RELEASE_DIR}；加 --push 可推送")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
