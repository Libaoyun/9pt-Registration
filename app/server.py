import threading
import time
import builtins
import os
import random
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import subprocess
import sys
from flask import Flask, jsonify, request, send_from_directory, Response, stream_with_context

# 导入业务逻辑
from . import main
from . import browser
from . import custom2925_service
from . import mailtm_service
from . import temporam_service
from . import email_providers
from . import token_batch_service
from .config import PROJECT_ROOT, cfg

STATIC_DIR = PROJECT_ROOT / "static"

app = Flask(__name__, static_url_path="", static_folder=str(STATIC_DIR))

# ==========================================
# 🔧 状态管理与日志捕获
# ==========================================

# 全局状态
class AppState:
    def __init__(self):
        self.is_running = False
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.current_action = "等待启动"
        self.task_type = "idle"
        self.progress_total = 0
        self.progress_completed = 0
        self.progress_processed = 0
        self.progress_skipped = 0
        self.logs = []
        self.lock = threading.Lock()

        # 选中的邮箱提供商列表（默认启用公开 provider）
        self.selected_providers = list(email_providers.DEFAULT_PROVIDERS)

        # 并行注册数（1 = 串行）
        self.parallel_count = 1

        # 是否使用 headless 浏览器
        self.headless = False

        # 代理配置
        self.proxy = {
            "enabled": False,
            "type": "http",
            "host": "",
            "port": 8080,
            "use_auth": False,
            "username": "",
            "password": "",
        }

        # 邀请链接：有效性和试用资格须由官方页面确认
        self.referral_url = getattr(cfg.registration, "referral_url", "")

        # 是否自动绑定 2FA TOTP 并保存 32 位密钥
        self.enable_2fa = getattr(cfg.registration, "enable_2fa", True)

        # MJPEG 流缓冲区
        self.last_frame = None
        self.frame_lock = threading.Lock()

        # 全自动流水线状态（注册 → 补全 → 检测 → 报表）
        self.pipeline_phase = "idle"
        self.pipeline_done = 0
        self.pipeline_total = 0
        self.pipeline_message = ""
        self.pipeline_report = None
        self.pipeline_error = ""

    def reset_pipeline(self, total=0):
        with self.lock:
            self.pipeline_phase = "starting"
            self.pipeline_done = 0
            self.pipeline_total = total
            self.pipeline_message = "正在启动全自动流水线"
            self.pipeline_report = None
            self.pipeline_error = ""

    def update_pipeline(self, phase=None, done=None, total=None, message=None):
        with self.lock:
            if phase is not None:
                self.pipeline_phase = phase
            if done is not None:
                self.pipeline_done = done
            if total is not None:
                self.pipeline_total = total
            if message is not None:
                self.pipeline_message = message

    def get_pipeline(self):
        with self.lock:
            return {
                "phase": self.pipeline_phase,
                "done": self.pipeline_done,
                "total": self.pipeline_total,
                "message": self.pipeline_message,
                "error": self.pipeline_error,
                "has_report": bool(self.pipeline_report),
                "summary": (self.pipeline_report or {}).get("summary", {}),
            }

    def add_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.logs.append(f"[{timestamp}] {message}")
            if len(self.logs) > 1000:
                self.logs.pop(0)

    def get_logs(self, start_index=0):
        with self.lock:
            return list(self.logs[start_index:])

    def update_frame(self, frame_bytes):
        with self.frame_lock:
            self.last_frame = frame_bytes

    def get_frame(self):
        with self.frame_lock:
            return self.last_frame

    def reset_progress(self, task_type="idle", total=0):
        with self.lock:
            self.task_type = task_type
            self.progress_total = total
            self.progress_completed = 0
            self.progress_processed = 0
            self.progress_skipped = 0

    def update_progress(self, *, task_type=None, total=None, completed=None, processed=None, skipped=None):
        with self.lock:
            if task_type is not None:
                self.task_type = task_type
            if total is not None:
                self.progress_total = total
            if completed is not None:
                self.progress_completed = completed
            if processed is not None:
                self.progress_processed = processed
            if skipped is not None:
                self.progress_skipped = skipped

    def get_progress(self):
        with self.lock:
            remaining = max(self.progress_total - self.progress_completed, 0)
            return {
                "task_type": self.task_type,
                "total": self.progress_total,
                "completed": self.progress_completed,
                "processed": self.progress_processed,
                "skipped": self.progress_skipped,
                "remaining": remaining,
            }

state = AppState()

# Hack: 劫持 print 函数以捕获日志
original_print = builtins.print
def hooked_print(*args, **kwargs):
    sep = kwargs.get('sep', ' ')
    msg = sep.join(map(str, args))
    state.add_log(msg)
    original_print(*args, **kwargs)

# 应用劫持到所有服务模块
main.print = hooked_print
browser.print = hooked_print
custom2925_service.print = hooked_print
mailtm_service.print = hooked_print
temporam_service.print = hooked_print
email_providers.print = hooked_print
token_batch_service.print = hooked_print

# ==========================================
# 🧵 后台工作线程
# ==========================================
def worker_thread(count, selected_providers, parallel, headless, proxy):
    state.is_running = True
    state.stop_requested = False
    state.success_count = 0
    state.fail_count = 0
    state.current_action = f"🚀 任务启动，目标: {count}"
    state.reset_progress(task_type="registration", total=count)
    state.update_frame(None)

    main.print(f"🚀 开始批量任务，计划注册: {count} 个，并行数: {parallel}")
    main.print(f"📬 邮箱服务: {', '.join(selected_providers)}")
    main.print(f"🖥️ 浏览器模式: {'Headless' if headless else '有界面'}")
    if state.referral_url:
        main.print(f"🎁 试用邀请链接: {state.referral_url}")
    main.print(f"🔐 自动 2FA 绑定: {'已启用 (TOTP 32位密钥)' if state.enable_2fa else '已停用'}")
    if proxy and proxy.get("enabled"):
        main.print(f"🌐 代理: {proxy.get('type','http')}://{proxy.get('host','')}:{proxy.get('port','')}")

    counter_lock = threading.Lock()
    started = [0]

    def monitor(driver, _step):
        if state.stop_requested:
            main.print("🛑 检测到停止请求，正在中断任务...")
            raise InterruptedError("用户请求停止")
        try:
            state.update_frame(driver.get_screenshot_as_png())
        except Exception:
            pass

    def do_one(_):
        if state.stop_requested:
            return
        with counter_lock:
            started[0] += 1
            idx = started[0]
        state.current_action = f"正在注册 ({idx}/{count})..."
        provider = random.choice(selected_providers)
        try:
            res = main.register_one_account(
                monitor_callback=monitor,
                email_provider=provider,
                headless=headless,
                proxy=proxy,
                referral_url=state.referral_url,
                enable_2fa=state.enable_2fa,
            )
            email, password, success = res
            is_skipped = getattr(res, "skipped", False)
            with counter_lock:
                if is_skipped:
                    state.progress_skipped += 1
                    state.success_count += 1
                elif success:
                    state.success_count += 1
                else:
                    state.fail_count += 1
                state.update_progress(
                    completed=state.success_count + state.fail_count,
                    processed=state.success_count,
                    skipped=state.progress_skipped,
                )
        except InterruptedError:
            main.print("🛑 任务已中断")
        except Exception as e:
            with counter_lock:
                state.fail_count += 1
                state.update_progress(
                    completed=state.success_count + state.fail_count,
                    processed=state.success_count,
                )
            main.print(f"❌ 异常: {str(e)}")

    try:
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = [executor.submit(do_one, i) for i in range(count)]
            for future in as_completed(futures):
                if state.stop_requested:
                    break
                try:
                    future.result()
                except Exception:
                    pass
    except Exception as e:
        main.print(f"💥 严重错误: {e}")
    finally:
        state.is_running = False
        state.current_action = "任务已完成"
        main.print("🏁 任务结束")


def autopilot_worker(count, selected_providers, parallel, headless, proxy, referral_url,
                     enable_2fa, verify_all, stagger_seconds=0, skip_completion=False):
    """全自动流水线：注册 → 真实补全(Web凭据/2FA) → 官方接口全量检测 → 生成最终报表。

    ``stagger_seconds`` 注册间隔（防风控）；``skip_completion`` 纯极速模式（跳过浏览器补全）。
    """
    from .pipeline import AutoPipeline

    state.is_running = True
    state.stop_requested = False
    state.success_count = 0
    state.fail_count = 0
    state.reset_progress(task_type="autopilot", total=count)
    state.reset_pipeline(total=count)
    state.current_action = f"🤖 全自动流水线启动，目标 {count} 个账号"

    def _on_phase(phase, done, total, message):
        state.update_pipeline(phase=phase, done=done, total=total, message=message)
        if phase == "register":
            state.update_progress(completed=done, total=total or count)
        else:
            state.update_progress(task_type="autopilot", total=total, completed=done)

    pipeline = AutoPipeline(
        mode="browser",
        log=lambda message: main.print(message),
        stop_check=lambda: state.stop_requested,
        phase_callback=_on_phase,
    )

    try:
        report = pipeline.run(
            count=count,
            providers=selected_providers,
            parallel=parallel,
            headless=headless,
            proxy=proxy,
            referral_url=referral_url,
            enable_2fa=enable_2fa,
            verify_all=verify_all,
            stagger_seconds=stagger_seconds,
            skip_completion=skip_completion,
        )
        state.pipeline_report = report
        summary = report.get("summary", {})
        state.success_count = summary.get("registered_ok", 0)
        state.fail_count = summary.get("registered_fail", 0)
        state.update_pipeline(phase="done", done=report.get("total", 0), total=report.get("total", 0),
                              message="全自动流水线完成，最终报表已生成")
        state.current_action = (
            f"✅ 全自动完成: 注册 {summary.get('registered_ok', 0)} 个 | 含真实2FA "
            f"{summary.get('with_2fa', 0)} | Plus {summary.get('plus_active', 0)} | 试用资格 "
            f"{summary.get('trial_eligible', 0)}"
        )
    except Exception as exc:  # noqa: BLE001
        state.pipeline_error = str(exc)
        state.update_pipeline(phase="error", message=f"流水线异常: {exc}")
        main.print(f"💥 全自动流水线异常: {exc}")
    finally:
        state.is_running = False


def token_worker_thread(accounts_file, output_dir, proxy):
    state.is_running = True
    state.stop_requested = False
    state.success_count = 0
    state.fail_count = 0
    state.reset_progress(task_type="token_import", total=0)
    state.current_action = "正在导入账号并获取 Token..."
    state.update_frame(None)

    main.print("📥 开始批量获取 Token")
    main.print(f"📄 TXT 路径: {accounts_file}")
    main.print(f"📁 输出目录: {output_dir}")

    def on_progress(progress):
        state.success_count = progress["success"]
        state.fail_count = progress["fail"]
        state.update_progress(
            task_type=progress["task_type"],
            total=progress["total"],
            completed=progress["completed"],
            processed=progress["processed"],
            skipped=progress["skipped"],
        )
        if progress["total"] > 0:
            state.current_action = (
                f"Token 获取中: {progress['completed']}/{progress['total']} "
                f"(成功 {progress['success']} / 失败 {progress['fail']} / 跳过 {progress['skipped']})"
            )
        if progress.get("current_email"):
            state.current_action += f" - {progress['current_email']}"

    try:
        result = token_batch_service.process_accounts_from_file(
            accounts_file=accounts_file,
            output_dir=output_dir,
            proxy=proxy,
            stop_requested=lambda: state.stop_requested,
            progress_callback=on_progress,
        )
        state.success_count = result["success"]
        state.fail_count = result["fail"]
        state.update_progress(
            task_type="token_import",
            total=result["total"],
            completed=result["completed"],
            processed=result["processed"],
            skipped=result["skipped"],
        )
        state.current_action = (
            f"Token 获取完成: {result['completed']}/{result['total']} "
            f"(成功 {result['success']} / 失败 {result['fail']} / 跳过 {result['skipped']})"
        )
        main.print(f"🏁 Token 获取完成，输出目录: {result['output_dir']}")
    except Exception as e:
        state.fail_count += 1
        state.current_action = "Token 获取失败"
        main.print(f"❌ Token 获取任务失败: {e}")
    finally:
        state.is_running = False

# ==========================================
# 🌊 MJPEG 流生成器
# ==========================================
def gen_frames():
    """生成流数据的生成器"""
    while True:
        frame = state.get_frame()
        if frame:
            yield (b'--frame\r\n'
                   b'Content-Type: image/png\r\n\r\n' + frame + b'\r\n')
        else:
            pass

        time.sleep(0.5)

@app.route('/video_feed')
def video_feed():
    return Flask.response_class(gen_frames(),
                               mimetype='multipart/x-mixed-replace; boundary=frame')

# ==========================================
# 🌐 API 接口
# ==========================================

@app.route('/')
def index():
    return send_from_directory(str(STATIC_DIR), 'index.html')

@app.route('/api/status')
def get_status():
    total_inventory = 0
    accounts_path = _resolve_repo_path(cfg.files.accounts_file)
    if accounts_path.exists():
        try:
            with open(accounts_path, 'r', encoding='utf-8') as f:
                total_inventory = sum(1 for line in f if '@' in line)
        except Exception:
            pass

    return jsonify({
        "is_running": state.is_running,
        "current_action": state.current_action,
        "success": state.success_count,
        "fail": state.fail_count,
        "progress": state.get_progress(),
        "pipeline": state.get_pipeline(),
        "total_inventory": total_inventory,
        "logs": state.get_logs(int(request.args.get('log_index', 0)))
    })


@app.route('/api/autopilot/start', methods=['POST'])
def start_autopilot():
    """一键全自动：注册 → 补全 → 检测 → 出报表。"""
    if state.is_running:
        return jsonify({"error": "已有任务正在运行中"}), 400

    data = request.json or {}
    count = max(1, int(data.get('count', 1)))
    verify_all = bool(data.get('verify_all', False))
    stagger_seconds = max(0, int(data.get('stagger_seconds', 0) or 0))
    skip_completion = bool(data.get('skip_completion', False))
    providers = list(state.selected_providers) or ["mailtm"]

    threading.Thread(
        target=autopilot_worker,
        args=(
            count, providers, state.parallel_count, state.headless,
            dict(state.proxy), state.referral_url, state.enable_2fa, verify_all,
            stagger_seconds, skip_completion,
        ),
        daemon=True,
    ).start()
    return jsonify({
        "status": "started", "count": count, "verify_all": verify_all,
        "stagger_seconds": stagger_seconds, "skip_completion": skip_completion,
    })


@app.route('/api/autopilot/status')
def autopilot_status():
    payload = state.get_pipeline()
    report = state.pipeline_report
    if report:
        payload["summary"] = report.get("summary", {})
        payload["generated_at"] = report.get("generated_at")
    return jsonify(payload)


@app.route('/api/autopilot/report')
def autopilot_report():
    """返回最终报表（含实时动态 2FA 验证码）。"""
    from .pipeline import accounts_file_path, build_report_now
    from .stored_accounts import load_accounts_from_file
    from .two_factor_service import generate_totp_code, totp_seconds_remaining

    report = state.pipeline_report
    if not report:
        try:
            report = build_report_now()
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"生成报表失败: {exc}"}), 500
        if not report.get("rows"):
            return jsonify({"error": "暂无报表数据，请先运行全自动流水线"}), 404

    # 动态 2FA 每次请求都重新计算，保证面板显示的是当下真实可用的验证码
    rows = []
    try:
        records = {r["email"]: r for r in load_accounts_from_file(str(accounts_file_path()))}
    except Exception:
        records = {}
    for row in report.get("rows", []):
        fresh = dict(row)
        secret = (fresh.get("two_factor_secret") or "").strip()
        if secret:
            fresh["two_factor_now"] = generate_totp_code(secret)
            fresh["two_factor_remaining"] = totp_seconds_remaining()
        live = records.get(fresh.get("email"))
        if live:
            fresh["plan"] = live.get("plan", fresh.get("plan"))
            fresh["is_plus"] = live.get("is_plus")
            fresh["is_paid"] = live.get("is_paid")
            fresh["trial_status"] = live.get("trial_status", fresh.get("trial_status"))
            fresh["trial_eligible"] = live.get("trial_eligible")
            fresh["quota"] = live.get("quota", fresh.get("quota"))
            fresh["expires_at"] = live.get("expires_at", fresh.get("expires_at"))
            fresh["account_status"] = live.get("account_status", fresh.get("account_status"))
            fresh["verified"] = bool(live.get("verified"))
            fresh["checked_at"] = live.get("checked_at", "")
            fresh["sources"] = live.get("sources", [])
            fresh["evidence_file"] = live.get("evidence_file", "")
            fresh["password"] = live.get("password", fresh.get("password"))
            live_secret = (live.get("two_factor_secret") or "").strip()
            if live_secret in ("未开启", "N/A"):
                live_secret = ""
            fresh["two_factor_secret"] = live_secret or secret
            if not fresh["two_factor_secret"]:
                fresh["two_factor_now"] = ""
                fresh["two_factor_remaining"] = 0
        rows.append(fresh)

    payload = dict(report)
    payload["rows"] = rows
    payload["total"] = len(rows)
    payload["summary"] = state.get_pipeline().get("summary", {})
    return jsonify(payload)


@app.route('/api/autopilot/export', methods=['POST'])
def autopilot_export():
    """把当前报表重新落盘（JSON / 5横杠 2FA / CSV / Markdown）。"""
    from .pipeline import build_report_now, write_report_files

    try:
        report = build_report_now()
        files = write_report_files(report)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"success": False, "error": str(exc)}), 500
    return jsonify({"success": True, "files": files, "total": report.get("total", 0)})

@app.route('/api/start', methods=['POST'])
def start_task():
    if state.is_running:
        return jsonify({"error": "Already running"}), 400

    data = request.json
    count = data.get('count', 1)

    # 使用当前选中的提供商列表
    providers = list(state.selected_providers)
    if not providers:
        providers = ["mailtm"]

    threading.Thread(
        target=worker_thread,
        args=(count, providers, state.parallel_count, state.headless, dict(state.proxy)),
        daemon=True
    ).start()
    return jsonify({"status": "started"})

@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify({
        "parallel": state.parallel_count,
        "headless": state.headless,
        "proxy": state.proxy,
        "referral_url": state.referral_url,
        "enable_2fa": state.enable_2fa,
    })


@app.route('/api/token-import/settings', methods=['GET'])
def get_token_import_settings():
    return jsonify({
        "accounts_file": str(_resolve_repo_path(cfg.files.accounts_file)),
        "output_dir": str(_resolve_repo_path(cfg.oauth.token_json_dir)),
    })


@app.route('/api/token-import/start', methods=['POST'])
def start_token_import():
    if state.is_running:
        return jsonify({"error": "Already running"}), 400

    data = request.json or {}
    accounts_file = str(_resolve_request_path(data.get("accounts_file", cfg.files.accounts_file)))
    output_dir = str(_resolve_request_path(data.get("output_dir", cfg.oauth.token_json_dir)))

    if not os.path.exists(accounts_file):
        return jsonify({"error": "账号 TXT 文件不存在"}), 400

    threading.Thread(
        target=token_worker_thread,
        args=(accounts_file, output_dir, dict(state.proxy)),
        daemon=True
    ).start()
    return jsonify({"status": "started", "accounts_file": accounts_file, "output_dir": output_dir})

@app.route('/api/settings', methods=['POST'])
def set_settings():
    data = request.json
    if "parallel" in data:
        state.parallel_count = max(1, min(10, int(data["parallel"])))
    if "headless" in data:
        state.headless = bool(data["headless"])
    if "referral_url" in data:
        state.referral_url = str(data["referral_url"]).strip()
    if "enable_2fa" in data:
        state.enable_2fa = bool(data["enable_2fa"])
    if "proxy" in data:
        p = data["proxy"]
        state.proxy = {
            "enabled":  bool(p.get("enabled", False)),
            "type":     p.get("type", "http"),
            "host":     str(p.get("host", "")),
            "port":     int(p.get("port", 8080)),
            "use_auth": bool(p.get("use_auth", False)),
            "username": str(p.get("username", "")),
            "password": str(p.get("password", "")),
        }
    return jsonify({
        "status": "ok",
        "parallel": state.parallel_count,
        "headless": state.headless,
        "proxy": state.proxy,
        "referral_url": state.referral_url,
        "enable_2fa": state.enable_2fa,
    })


@app.route('/api/proxy/test', methods=['POST'])
def test_proxy():
    data = request.json or {}
    p = data.get("proxy", data)
    ptype = str(p.get("type", "http")).lower()
    host = str(p.get("host", "")).strip()
    port = int(p.get("port", 0) or 0)
    use_auth = bool(p.get("use_auth", False))
    username = str(p.get("username", "")).strip()
    password = str(p.get("password", "")).strip()

    if not host or not port:
        return jsonify({"success": False, "error": "请填写代理主机和端口"}), 400

    proxy_dict = {
        "enabled": True,
        "type": ptype,
        "host": host,
        "port": port,
        "use_auth": use_auth,
        "username": username,
        "password": password,
    }
    from .utils import build_requests_proxies
    proxies = build_requests_proxies(proxy_dict)

    import time
    start_t = time.time()
    try:
        import requests as standard_requests
        resp = standard_requests.get(
            "http://ip-api.com/json/",
            proxies=proxies,
            timeout=7,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        latency_ms = int((time.time() - start_t) * 1000)
        if resp.status_code == 200:
            ip_info = resp.json()
            return jsonify({
                "success": True,
                "latency_ms": latency_ms,
                "ip": ip_info.get("query", "未知"),
                "query": ip_info.get("query", "未知"),
                "country": ip_info.get("country", "未知"),
                "country_code": ip_info.get("countryCode", ""),
                "city": ip_info.get("city", ""),
                "isp": ip_info.get("isp", ""),
            })
        else:
            return jsonify({
                "success": True,
                "latency_ms": latency_ms,
                "ip": resp.text[:40],
                "country": "连通正常",
            })
    except Exception as exc:
        return jsonify({
            "success": False,
            "error": f"代理测试失败: {str(exc)[:100]}"
        }), 200


@app.route('/api/stop', methods=['POST'])
def stop_task():
    if not state.is_running:
        return jsonify({"error": "Not running"}), 400

    state.stop_requested = True
    return jsonify({"status": "stopping"})

@app.route('/api/providers', methods=['GET'])
def get_providers():
    """获取所有提供商列表及当前选中状态"""
    result = []
    for pid, info in email_providers.PROVIDERS.items():
        result.append({
            "id": pid,
            "name": info["name"],
            "inbox_url": info["inbox_url"],
            "has_password": info["has_password"],
            "selected": pid in state.selected_providers
        })
    return jsonify(result)

@app.route('/api/providers', methods=['POST'])
def set_providers():
    """更新选中的提供商列表"""
    data = request.json
    selected = data.get('selected', [])

    # 过滤出合法的提供商 ID
    valid = [p for p in selected if p in email_providers.PROVIDERS]
    if not valid:
        return jsonify({"error": "至少需要选择一个提供商"}), 400

    state.selected_providers = valid
    return jsonify({"status": "ok", "selected": valid})

@app.route('/api/accounts')
def get_accounts():
    accounts = []
    accounts_path = _resolve_repo_path(cfg.files.accounts_file)
    if accounts_path.exists():
        try:
            from .stored_accounts import load_accounts_from_file
            from .two_factor_service import generate_totp_code, totp_seconds_remaining

            records = load_accounts_from_file(str(accounts_path))
            for r in records:
                provider_id = r.get("provider", "mailtm")
                provider_info = email_providers.get_provider_info(provider_id)
                secret = (r.get("two_factor_secret") or "").strip()
                has_secret = bool(secret) and secret not in ("未开启", "N/A")
                accounts.append({                    "email": r["email"],
                    "password": r["password"],
                    "time": r["timestamp"],
                    "status": r["status"],
                    "temp_credential": r["mailbox_credential"],
                    "provider": provider_id,
                    "provider_name": provider_info["name"] if provider_info else provider_id,
                    "inbox_url": provider_info["inbox_url"] if provider_info else "https://mail.tm",
                    "has_password": provider_info["has_password"] if provider_info else True,
                    "plan": r["plan"],
                    "trial_status": r["trial_status"],
                    "trial_eligible": r.get("trial_eligible"),
                    "is_paid": r["is_paid"],
                    "is_plus": r.get("is_plus"),
                    "verified": r.get("verified", False),
                    "checked_at": r.get("checked_at", ""),
                    "sources": r.get("sources", []),
                    "evidence_file": r.get("evidence_file", ""),
                    "profile_evidence": r.get("profile_evidence", {}),
                    "quota": r.get("quota", "未知，请在官方页面确认"),
                    "expires_at": r.get("expires_at", "未知，未取得订阅到期时间"),
                    "account_status": r.get("account_status", "⚪ 未在线验证"),
                    "two_factor_secret": secret if has_secret else "",
                    "two_factor_now": generate_totp_code(secret) if has_secret else "",
                    "two_factor_remaining": totp_seconds_remaining() if has_secret else 0,
                    "otpauth_url": (
                        f"otpauth://totp/OpenAI:{r['email']}?secret={secret}&issuer=OpenAI"
                        if has_secret else ""
                    ),
                    "register_mode": r.get("register_mode", "browser"),
                })
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify(accounts[::-1])


@app.route('/api/accounts/check-one', methods=['POST'])
def check_one_account():
    data = request.json or {}
    email = data.get('email', '').strip()
    if not email:
        return jsonify({"error": "缺少邮箱参数"}), 400

    from .account_checker import refresh_account_state

    accounts_path = _resolve_repo_path(cfg.files.accounts_file)
    res = refresh_account_state(email, str(accounts_path), proxy=dict(state.proxy), log=state.add_log)
    res.pop("state", None)
    return jsonify(res)


@app.route('/api/accounts/check-all', methods=['POST'])
def check_all_accounts():
    if state.is_running:
        return jsonify({"error": "已有任务正在运行中"}), 400

    def _batch_check_worker():
        from .account_checker import refresh_account_state
        from .stored_accounts import load_accounts_from_file

        accounts_path = _resolve_repo_path(cfg.files.accounts_file)
        if not accounts_path.exists():
            return

        state.is_running = True
        state.stop_requested = False
        state.current_action = "正在通过官方接口全量检测账号真实资格与支付状态"
        state.add_log("🔍 开始全量检测所有账号的真实订阅状态、试用资格与到期时间...")

        try:
            records = load_accounts_from_file(str(accounts_path))
            total = len(records)
            state.reset_progress(task_type="check_all", total=total)
            for idx, rec in enumerate(records, 1):
                if state.stop_requested:
                    state.add_log("🛑 用户已停止批量检测")
                    break
                em = rec["email"]
                state.current_action = f"检测中 ({idx}/{total}): {em}"
                try:
                    res = refresh_account_state(
                        em, str(accounts_path), proxy=dict(state.proxy), log=state.add_log
                    )
                    state.add_log(
                        f"  ℹ️ {em} -> {res.get('account_status')} | 计划: {res['plan']} | "
                        f"Plus: {res.get('is_plus')} | 试用: {res.get('trial_status')} | 到期: {res.get('expires_at')}"
                    )
                except Exception as err:  # noqa: BLE001
                    state.add_log(f"  ⚠️ {em} 检测失败: {err}")
                state.update_progress(completed=idx, processed=idx)
        finally:
            state.is_running = False
            if state.stop_requested:
                state.current_action = "全量检测已中止"
                state.add_log("🛑 全量检测已中止")
            else:
                state.current_action = "全量检测完成"
                state.add_log("🎉 全量账号状态、额度与到期时间检测完成")

    threading.Thread(target=_batch_check_worker, daemon=True).start()
    return jsonify({"status": "started"})


@app.route('/api/accounts/perfect', methods=['POST'])
def perfect_account():
    data = request.json or {}
    email = data.get('email', '').strip()
    if not email:
        return jsonify({"error": "缺少邮箱参数"}), 400

    try:
        from .account_perfector import perfect_single_account
        proxy = dict(state.proxy)
        headless = data.get('headless', state.headless)
        state.add_log(f"⚡ 开始单账号完善流程: {email}")

        res = perfect_single_account(
            email=email,
            proxy=proxy,
            headless=headless,
            log_func=lambda msg: state.add_log(msg),
        )
        if res.get("success"):
            state.add_log(f"✅ [{email}] 完善成功！2FA密钥: {res.get('two_factor_secret')} | 试用: {res.get('trial_status')}")
        else:
            state.add_log(f"⚠️ [{email}] 完善未完成: {res.get('error')}")
        return jsonify(res)
    except Exception as exc:
        state.add_log(f"❌ [{email}] 完善异常: {exc}")
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route('/api/accounts/perfect-all', methods=['POST'])
def perfect_all_accounts():
    if state.is_running:
        return jsonify({"error": "已有任务正在运行中"}), 400

    def _batch_perfect_worker():
        from .account_perfector import perfect_single_account
        from .pipeline import _needs_perfect
        from .stored_accounts import load_accounts_from_file

        accounts_path = _resolve_repo_path(cfg.files.accounts_file)
        if not accounts_path.exists():
            return

        state.is_running = True
        state.stop_requested = False
        state.current_action = "正在批量完善账号 (真实密码/真实2FA/官方权益证据)"
        state.add_log("⚡ 开始批量完善账号...")

        try:
            records = load_accounts_from_file(str(accounts_path))
            # 缺 2FA / 缺官方在线证据 / 权益未知 的账号都要补全（未注册成功的也包含在内）
            targets = [r for r in records if _needs_perfect(r)] or records
            total = len(targets)
            workers = min(max(1, getattr(state, "parallel_count", 3) or 3), max(1, total))
            state.reset_progress(task_type="perfect_all", total=total)
            state.add_log(f"🎯 待完善目标账号数: {total} 个，已开启后台并发池 (工作线程: {workers})")

            from concurrent.futures import ThreadPoolExecutor, as_completed
            completed_lock = threading.Lock()
            completed_cnt = 0

            def _worker_fn(item_rec, item_idx):
                if state.stop_requested:
                    return
                em = item_rec["email"]
                state.add_log(f"[{item_idx}/{total}] 🚀 启动完善: {em}")
                try:
                    res = perfect_single_account(
                        email=em,
                        proxy=dict(state.proxy),
                        headless=True,
                        log_func=lambda msg: state.add_log(f"[{em}] {msg}"),
                    )
                    if res.get("success"):
                        state.add_log(f"[{item_idx}/{total}] ✅ {em} 完善成功: 2FA密钥已生成 | 试用={res.get('trial_status')}")
                    else:
                        state.add_log(f"[{item_idx}/{total}] ⚠️ {em} 完善提示: {res.get('error')}")
                except Exception as err:
                    state.add_log(f"[{item_idx}/{total}] ❌ {em} 完善失败: {err}")
                finally:
                    nonlocal completed_cnt
                    with completed_lock:
                        completed_cnt += 1
                        state.update_progress(completed=completed_cnt, processed=completed_cnt)

            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(_worker_fn, rec, idx) for idx, rec in enumerate(targets, 1)]
                for f in as_completed(futures):
                    if state.stop_requested:
                        for rem in futures:
                            rem.cancel()
                        break
        finally:
            state.is_running = False
            state.current_action = "批量完善完成"
            state.add_log("🎉 账号完善流程执行完毕")

    threading.Thread(target=_batch_perfect_worker, daemon=True).start()
    return jsonify({"status": "started"})


@app.route('/api/chat2api/status')
def get_chat2api_status():
    chat2api_dir = PROJECT_ROOT.parent / "chat2api"
    token_file = chat2api_dir / "data" / "token.txt"
    token_count = 0
    if token_file.exists():
        try:
            token_count = sum(1 for line in token_file.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
        except Exception:
            pass

    # 检查 5005 端口是否正在监听
    is_running = False
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        res = sock.connect_ex(('127.0.0.1', 5005))
        sock.close()
        is_running = (res == 0)
    except Exception:
        pass

    return jsonify({
        "running": is_running,
        "token_count": token_count,
        "api_endpoint": "http://127.0.0.1:5005/v1/chat/completions",
        "docs_url": "http://127.0.0.1:5005/docs",
        "tokens_url": "http://127.0.0.1:5005/tokens",
    })


@app.route('/api/chat2api/sync', methods=['POST'])
def sync_to_chat2api():
    chat2api_dir = PROJECT_ROOT.parent / "chat2api"
    token_file = chat2api_dir / "data" / "token.txt"
    token_dir = _resolve_repo_path(cfg.oauth.token_json_dir)

    web_tokens = []
    if token_dir.exists():
        try:
            for jf in token_dir.glob("*.json"):
                try:
                    data = json.loads(jf.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and data.get("type") == "web" and data.get("access_token"):
                        web_tokens.append(data["access_token"])
                except Exception:
                    pass
        except Exception:
            pass

    if not web_tokens:
        return jsonify({
            "status": "unsupported",
            "error": "Codex OAuth 凭证不能自动作为 ChatGPT Web 网关凭证使用。请使用官方 Codex 登录或官方 API 接入。",
            "added_count": 0,
        }), 409

    token_file.parent.mkdir(parents=True, exist_ok=True)
    existing_tokens = set()
    if token_file.exists():
        try:
            existing_tokens = set(line.strip() for line in token_file.read_text(encoding="utf-8").splitlines() if line.strip())
        except Exception:
            pass

    new_added = [t for t in web_tokens if t not in existing_tokens]
    if new_added:
        with open(token_file, "a", encoding="utf-8") as f:
            for t in new_added:
                f.write(f"{t}\n")

    return jsonify({
        "status": "ok",
        "added_count": len(new_added),
        "total_pool_count": len(existing_tokens) + len(new_added),
    })


@app.route('/api/accounts/export')
def export_accounts():
    fmt = request.args.get('format', 'csv').lower()
    accounts_path = _resolve_repo_path(cfg.files.accounts_file)
    records = []
    if accounts_path.exists():
        from .stored_accounts import load_accounts_from_file, is_trial_eligible
        try:
            records = load_accounts_from_file(str(accounts_path))
        except Exception:
            records = []

    filter_type = request.args.get('filter', 'all').lower()
    if filter_type == 'trial':
        records = [r for r in records if is_trial_eligible(r.get('trial_status', ''))]
    elif filter_type == 'paid':
        records = [r for r in records if r.get('is_paid') or (r.get('plan') or '').lower() in ['plus', 'team', 'pro']]
    elif filter_type == 'plus_all':
        records = [
            r for r in records
            if r.get('is_paid')
            or (r.get('plan') or '').lower() in ['plus', 'team', 'pro']
            or is_trial_eligible(r.get('trial_status', ''))
        ]
    elif filter_type == 'free':
        records = [r for r in records if r.get('plan', '').lower() == 'free']
    elif filter_type == 'fail':
        records = [
            r for r in records
            if '失败' in r.get('status', '')
            or '错误' in r.get('status', '')
            or '封禁' in r.get('account_status', '')
            or '失效' in r.get('account_status', '')
        ]

    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    if fmt == '2fa':
        # 用户指定格式: 邮箱-----密码-----32位2fa (使用 5 个连字符)
        lines = [
            f"{r.get('email', '')}-----{r.get('password', '')}-----{r.get('two_factor_secret', '') or '未开启2FA'}"
            for r in records
        ]
        content = "\n".join(lines)
        return Response(
            content,
            mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=chatgpt_accounts_2fa_{filter_type}_{date_str}.txt"}
        )
    elif fmt == 'csv':
        import io
        import csv
        output = io.StringIO()
        output.write('\ufeff')
        writer = csv.writer(output)
        writer.writerow(["ChatGPT邮箱", "密码", "2FA密钥(Base32)", "账号健康状态", "订阅/支付状态", "模型/API额度", "会员/试用到期时间", "1个月试用资格", "注册时间", "邮箱渠道", "临时邮箱凭证"])
        for r in records:
            writer.writerow([
                r.get("email", ""),
                r.get("password", ""),
                r.get("two_factor_secret", ""),
                r.get("account_status", "⚪ 未在线验证"),
                r.get("plan", "未检测"),
                r.get("quota", "未知，请在官方页面确认"),
                r.get("expires_at", "未知，未取得订阅到期时间"),
                r.get("trial_status", "未检测"),
                r.get("timestamp", ""),
                r.get("provider", ""),
                r.get("mailbox_credential", ""),
            ])
        content = output.getvalue()
        return Response(
            content,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=chatgpt_accounts_{filter_type}_{date_str}.csv"}
        )
    elif fmt == 'combo':
        lines = [
            f"{r.get('email', '')}----{r.get('password', '')}----{r.get('plan', '未检测')}----{r.get('trial_status', '待检测')}----{r.get('quota', '未知，请在官方页面确认')}----{r.get('expires_at', '未知，未取得订阅到期时间')}----{r.get('account_status', '⚪ 未在线验证')}----{r.get('two_factor_secret', '') or '无2FA'}"
            for r in records
        ]
        content = "\n".join(lines)
        return Response(
            content,
            mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=accounts_combo_{filter_type}_{date_str}.txt"}
        )
    else:
        lines = [f"{r.get('email', '')}----{r.get('password', '')}" for r in records]
        content = "\n".join(lines)
        return Response(
            content,
            mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=accounts_{filter_type}_{date_str}.txt"}
        )


@app.route('/api/accounts/import', methods=['POST'])
def import_accounts():
    data = request.json or {}
    text = data.get("text", "")
    file_path = data.get("file_path", "")

    accounts_path = _resolve_repo_path(cfg.files.accounts_file)

    if file_path:
        p = _resolve_request_path(file_path)
        if not p.exists():
            return jsonify({"success": False, "error": f"导入文件不存在: {file_path}"}), 400
        text = p.read_text(encoding="utf-8", errors="replace")

    if not text or not text.strip():
        return jsonify({"success": False, "error": "导入内容为空，请粘贴账号文本或指定有效文件"}), 400

    from .stored_accounts import import_accounts_from_text
    try:
        result = import_accounts_from_text(str(accounts_path), text)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"success": False, "error": f"导入处理异常: {str(exc)}"}), 500


_chat2api_proc = None
_chat2api_proc_lock = threading.Lock()


def _check_chat2api_alive():
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.3)
    res = sock.connect_ex(('127.0.0.1', 5005))
    sock.close()
    return res == 0


def _ensure_chat2api_running():
    global _chat2api_proc
    if _check_chat2api_alive():
        return True
    with _chat2api_proc_lock:
        if _check_chat2api_alive():
            return True

        chat2api_dir = PROJECT_ROOT.parent / "chat2api"
        python_exe = chat2api_dir / ".venv" / "Scripts" / "python.exe"
        if not python_exe.exists():
            python_exe = Path(sys.executable)

        app_py = chat2api_dir / "app.py"
        if not app_py.exists():
            return False

        creationflags = 0x08000000 if os.name == 'nt' else 0
        try:
            _chat2api_proc = subprocess.Popen(
                [str(python_exe), "app.py"],
                cwd=str(chat2api_dir),
                creationflags=creationflags
            )
            for _ in range(35):
                time.sleep(0.1)
                if _check_chat2api_alive():
                    return True
        except Exception as e:
            print(f"⚠️ 启动 Chat2Api 失败: {e}")
            return False
    return False


@app.route('/api/chat2api/start', methods=['POST'])
def start_chat2api_service():
    if _ensure_chat2api_running():
        return jsonify({"status": "running", "message": "Chat2Api 服务已在端口 5005 运行"})
    return jsonify({"status": "error", "message": "启动 Chat2Api 失败，请检查环境"}), 500


@app.route('/api/chat2api/stop', methods=['POST'])
def stop_chat2api_service():
    global _chat2api_proc
    with _chat2api_proc_lock:
        if _chat2api_proc:
            try:
                if os.name == 'nt':
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(_chat2api_proc.pid)], capture_output=True)
                else:
                    _chat2api_proc.terminate()
            except Exception:
                pass
            _chat2api_proc = None
    return jsonify({"status": "stopped"})


@app.route('/v1/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS', 'HEAD'])
def reverse_proxy_chat2api(path):
    if request.method == 'OPTIONS':
        return ('', 204, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': '*',
            'Access-Control-Allow-Headers': '*'
        })

    if not _ensure_chat2api_running():
        return jsonify({
            "error": {
                "message": "Chat2Api 接口后端 (端口 5005) 未就绪，请在面板启动或稍后重试",
                "type": "server_error"
            }
        }), 503

    import requests as standard_requests
    target_url = f"http://127.0.0.1:5005/v1/{path}"

    headers = {k: v for k, v in request.headers if k.lower() not in ['host', 'content-length']}

    try:
        upstream_resp = standard_requests.request(
            method=request.method,
            url=target_url,
            headers=headers,
            data=request.get_data(),
            stream=True,
            timeout=180
        )

        def generate():
            # chunk_size=None 保证实时 unbuffered SSE 流式推流打字
            for chunk in upstream_resp.iter_content(chunk_size=None):
                if chunk:
                    yield chunk

        resp_headers = [(k, v) for k, v in upstream_resp.headers.items()
                        if k.lower() not in ['content-length', 'transfer-encoding', 'content-encoding']]
        resp_headers.append(('Access-Control-Allow-Origin', '*'))
        return Response(stream_with_context(generate()), status=upstream_resp.status_code, headers=resp_headers)
    except Exception as exc:
        return jsonify({
            "error": {
                "message": f"代理请求失败: {str(exc)}",
                "type": "gateway_error"
            }
        }), 502



def serve_app():
    from waitress import serve
    print("🌐 Web Server started at http://localhost:8888")
    serve(app, host='0.0.0.0', port=8888, threads=6)


def _resolve_repo_path(path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _resolve_request_path(path_value: str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path_value))))


if __name__ == '__main__':
    serve_app()
