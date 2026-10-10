"""
工具函数模块
包含通用的辅助函数
"""

import random
import string
import os
import sys
import time
import re
import json
import contextlib
from datetime import datetime
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import threading

from .config import (
    cfg,
    PROJECT_ROOT,
    PASSWORD_LENGTH,
    PASSWORD_CHARS,
    HTTP_MAX_RETRIES,
    USER_AGENT,
    MIN_AGE,
    MAX_AGE
)

# 尝试导入 Faker 库
try:
    from faker import Faker
    # 创建多语言环境的 Faker 实例（英语为主，增加真实感）
    fake = Faker(['en_US', 'en_GB'])
    # 设置随机种子以确保可重复性（可选）
    # Faker.seed(0)
    FAKER_AVAILABLE = True
    print("✅ Faker 库已加载，将使用更真实的假数据")
except ImportError:
    FAKER_AVAILABLE = False
    print("⚠️ Faker 库未安装，将使用内置姓名列表")
    print("   安装命令: pip install Faker")

# ============================================================
# 常用英文名字库（用于随机生成用户姓名）
# ============================================================

FIRST_NAMES = [
    # 男性名字
    "James", "John", "Robert", "Michael", "William", "David", "Richard", "Joseph",
    "Thomas", "Charles", "Christopher", "Daniel", "Matthew", "Anthony", "Mark",
    "Donald", "Steven", "Paul", "Andrew", "Joshua", "Kenneth", "Kevin", "Brian",
    "George", "Timothy", "Ronald", "Edward", "Jason", "Jeffrey", "Ryan",
    # 女性名字
    "Mary", "Patricia", "Jennifer", "Linda", "Barbara", "Elizabeth", "Susan",
    "Jessica", "Sarah", "Karen", "Lisa", "Nancy", "Betty", "Margaret", "Sandra",
    "Ashley", "Kimberly", "Emily", "Donna", "Michelle", "Dorothy", "Carol",
    "Amanda", "Melissa", "Deborah", "Stephanie", "Rebecca", "Sharon", "Laura", "Cynthia"
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson",
    "Walker", "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen",
    "Hill", "Flores", "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell"
]


def create_http_session():
    """
    创建带有重试机制的 HTTP Session
    
    返回:
        requests.Session: 配置好重试策略的 Session 对象
    """
    session = requests.Session()
    retry_strategy = Retry(
        total=HTTP_MAX_RETRIES,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST", "OPTIONS"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# 创建全局 HTTP Session
http_session = create_http_session()


def build_requests_proxies(proxy: dict) -> dict:
    """
    将代理配置字典转换为 requests 库的 proxies 参数格式。
    proxy 未启用时返回空字典（不使用代理）。
    """
    if not proxy or not proxy.get("enabled"):
        return {}
    host = proxy.get("host", "").strip()
    port = int(proxy.get("port", 8080))
    ptype = proxy.get("type", "http").lower()
    use_auth = proxy.get("use_auth", False)
    username = proxy.get("username", "")
    password = proxy.get("password", "")

    # socks5h 让代理服务器解析 DNS（避免 DNS 泄漏）
    scheme = "socks5h" if ptype == "socks5" else "http"
    if use_auth and username:
        proxy_url = f"{scheme}://{username}:{password}@{host}:{port}"
    else:
        proxy_url = f"{scheme}://{host}:{port}"

    return {"http": proxy_url, "https": proxy_url}


def configure_http_proxy(proxy: dict):
    """更新全局 http_session 的代理设置（影响 mailtm 等所有 requests 调用）。"""
    proxies = build_requests_proxies(proxy)
    if proxies:
        http_session.proxies.update(proxies)
        print(f"  🌐 HTTP Session 已配置代理: {list(proxies.values())[0]}")
    else:
        http_session.proxies.clear()


def get_user_agent():
    """
    获取 User-Agent 字符串
    
    返回:
        str: User-Agent
    """
    return USER_AGENT


def generate_random_password(length=None):
    """
    生成随机密码
    确保密码包含大写字母、小写字母、数字和特殊字符
    
    参数:
        length: 密码长度，默认使用配置文件中的值
    
    返回:
        str: 生成的密码
    """
    if length is None:
        length = PASSWORD_LENGTH
    
    # 先随机生成指定长度的密码
    password = ''.join(random.choice(PASSWORD_CHARS) for _ in range(length))
    
    # 确保包含各类字符（替换前4位）
    password = (
        random.choice(string.ascii_uppercase) +   # 大写字母
        random.choice(string.ascii_lowercase) +   # 小写字母
        random.choice(string.digits) +            # 数字
        random.choice("!@#$%") +                  # 特殊字符
        password[4:]                              # 剩余部分
    )
    
    print(f"✅ 已生成密码: {password}")
    return password


_accounts_file_lock = threading.Lock()


@contextlib.contextmanager
def _interprocess_lock(lock_path: str):
    """跨进程锁：在 threading.Lock 基础上保障双版本并发写入安全"""
    p = str(lock_path) + ".lock"
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    except Exception:
        pass
    f = None
    try:
        f = open(p, "a+b")
        if sys.platform == "win32":
            import msvcrt
            for _ in range(40):
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:

        if f:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    try:
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                f.close()
            except Exception:
                pass


def save_to_txt(
    email: str,
    password: str = None,
    status="已注册",
    mailtm_password: str = None,
    provider: str = "mailtm",
    plan: str = None,
    trial_status: str = None,
    quota: str = None,
    expires_at: str = None,
    account_status: str = None,
    two_factor_secret: str = None,
    evidence: dict = None,
    **kwargs,
):


    """
    保存账号信息到 TXT 文件
    格式: 邮箱|ChatGPT密码|时间|状态|临时邮箱凭证|提供商|订阅状态|试用资格|额度说明|会员到期时间|账号健康状态|2FA密钥(Base32)
    如果账号已存在，则更新其信息
    """
    try:
        file_path = cfg.files.accounts_file
        if not os.path.isabs(file_path):
            file_path = os.path.join(PROJECT_ROOT, file_path)
        os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        from .stored_accounts import default_quota_for_plan, default_expiry_for_plan, derive_health_status

        with _accounts_file_lock, _interprocess_lock(file_path):
            # 读取现有内容
            lines = []
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()

            # 检查是否已存在，存在则更新
            found = False
            credential = mailtm_password or ""
            plan_val = plan or "未检测"
            trial_val = trial_status or "待官方确认"
            quota_val = quota or default_quota_for_plan(plan_val)
            expires_val = expires_at or default_expiry_for_plan(plan_val, trial_val)
            health_val = account_status or derive_health_status(status)
            two_factor_val = two_factor_secret or ""

            evidence_val = dict(evidence) if isinstance(evidence, dict) else {}
            if "register_mode" not in evidence_val:
                evidence_val["register_mode"] = "protocol"
            evidence_json_str = json.dumps(evidence_val, ensure_ascii=False) if evidence_val else ""

            new_line_content = f"{email}|{password or 'N/A'}|{current_date}|{status}|{credential}|{provider}|{plan_val}|{trial_val}|{quota_val}|{expires_val}|{health_val}|{two_factor_val}{f'|{evidence_json_str}' if evidence_json_str else ''}\n"

            for i, line in enumerate(lines):
                if line.startswith(f"{email}|"):
                    parts = line.strip().split("|")
                    final_password = password if password else (parts[1] if len(parts) > 1 else "N/A")
                    final_cred = credential if credential else (parts[4] if len(parts) > 4 else "")
                    final_provider = provider if provider else (parts[5] if len(parts) > 5 else "mailtm")
                    final_plan = plan if plan is not None else (parts[6].strip() if len(parts) > 6 else plan_val)
                    final_trial = trial_status if trial_status is not None else (parts[7].strip() if len(parts) > 7 else trial_val)
                    final_quota = quota if quota is not None else (parts[8].strip() if len(parts) > 8 else default_quota_for_plan(final_plan))
                    final_expires = expires_at if expires_at is not None else (parts[9].strip() if len(parts) > 9 else default_expiry_for_plan(final_plan, final_trial))
                    final_health = account_status if account_status is not None else (parts[10].strip() if len(parts) > 10 else derive_health_status(status))
                    final_2fa = two_factor_secret if two_factor_secret is not None else (parts[11].strip() if len(parts) > 11 else "")

                    old_evidence = {}
                    if len(parts) > 12 and parts[12].strip():
                        try:
                            old_evidence = json.loads(parts[12].strip())
                        except Exception:
                            pass
                    merged_evidence = dict(old_evidence)
                    if isinstance(evidence, dict):
                        merged_evidence.update(evidence)
                    if "register_mode" not in merged_evidence:
                        merged_evidence["register_mode"] = "protocol"
                    merged_ev_str = f"|{json.dumps(merged_evidence, ensure_ascii=False)}" if merged_evidence else ""

                    lines[i] = f"{email}|{final_password}|{current_date}|{status}|{final_cred}|{final_provider}|{final_plan}|{final_trial}|{final_quota}|{final_expires}|{final_health}|{final_2fa}{merged_ev_str}\n"
                    found = True
                    break

            if not found:
                lines.append(new_line_content)

            # 写回文件
            with open(file_path, "w", encoding="utf-8") as f:
                f.writelines(lines)

        print(f"💾 账号状态已更新: {status} | 计划: {final_plan if found else plan_val} | 额度: {final_quota if found else quota_val} | 到期: {final_expires if found else expires_val}")

    except Exception as e:
        print(f"❌ 保存/更新账号信息失败: {e}")
        raise


def update_account_status(
    email: str,
    new_status: str = None,
    password: str = None,
    provider: str = "mailtm",
    plan: str = None,
    trial_status: str = None,
    quota: str = None,
    expires_at: str = None,
    account_status: str = None,
    two_factor_secret: str = None,
    status: str = None,
    evidence: dict = None,
    **kwargs,
):
    """更新账号状态"""
    final_status = new_status or status or "已注册"
    save_to_txt(
        email,
        password,
        final_status,
        provider=provider,
        plan=plan,
        trial_status=trial_status,
        quota=quota,
        expires_at=expires_at,
        account_status=account_status,
        two_factor_secret=two_factor_secret,
        evidence=evidence,
    )



def extract_verification_code(content: str):
    """
    从邮件内容中提取 6 位数字验证码
    
    参数:
        content: 邮件内容（HTML 或纯文本）
    
    返回:
        str: 提取到的验证码，未找到返回 None
    """
    if not content:
        return None
    
    # 验证码匹配模式（按优先级排列）
    patterns = [
        r'代码为\s*(\d{6})',                   # 中文格式
        r'code is\s*(\d{6})',                  # 英文格式
        r'verification code[:\s]*(\d{6})',     # 完整英文格式
        r'your code[:\s]*(\d{6})',             # "Your code: XXXXXX"
        r'(?:^|\s|>)(\d{6})(?:\s|<|$)',       # 独立出现的 6 位数（带边界）
    ]

    # 过滤掉明显不是验证码的数字（年月、版权年等）
    false_positives = {
        "202601", "202602", "202603", "202604", "202605", "202606",
        "202607", "202608", "202609", "202610", "202611", "202612",
        "202501", "202512", "123456", "000000", "100000",
    }

    for pattern in patterns:
        matches = re.findall(pattern, content, re.IGNORECASE)
        for code in matches:
            if code not in false_positives:
                print(f"  ✅ 提取到验证码: {code}")
                return code

    return None


def generate_random_name():
    """
    生成随机英文姓名
    
    使用 Faker 库生成更真实的姓名，如果 Faker 不可用则回退到内置列表
    
    返回:
        str: 格式为 "FirstName LastName" 的随机姓名
    """
    if FAKER_AVAILABLE:
        # 使用 Faker 直接生成名和姓，避免前缀后缀问题
        # 随机选择生成男性或女性名字
        if random.choice([True, False]):
            first_name = fake.first_name_male()
        else:
            first_name = fake.first_name_female()
        
        last_name = fake.last_name()
        full_name = f"{first_name} {last_name}"
    else:
        # 回退到内置列表
        first_name = random.choice(FIRST_NAMES)
        last_name = random.choice(LAST_NAMES)
        full_name = f"{first_name} {last_name}"
    
    print(f"✅ 已生成随机姓名: {full_name}")
    return full_name


def generate_random_birthday():
    """
    生成随机生日
    确保年龄在配置的范围内（MIN_AGE 到 MAX_AGE）
    
    使用 Faker 库生成更真实的生日日期
    
    返回:
        tuple: (年份字符串, 月份字符串, 日期字符串)
               例如: ("1995", "03", "15")
    """
    if FAKER_AVAILABLE:
        # 使用 Faker 生成符合年龄范围的生日
        birthday = fake.date_of_birth(minimum_age=MIN_AGE, maximum_age=MAX_AGE)
        year_str = str(birthday.year)
        month_str = str(birthday.month).zfill(2)
        day_str = str(birthday.day).zfill(2)
    else:
        # 回退到原始逻辑
        from datetime import datetime as dt
        today = dt.now()
        
        min_birth_year = today.year - MAX_AGE
        max_birth_year = today.year - MIN_AGE
        birth_year = random.randint(min_birth_year, max_birth_year)
        birth_month = random.randint(1, 12)
        
        if birth_month in [1, 3, 5, 7, 8, 10, 12]:
            max_day = 31
        elif birth_month in [4, 6, 9, 11]:
            max_day = 30
        else:
            if (birth_year % 4 == 0 and birth_year % 100 != 0) or (birth_year % 400 == 0):
                max_day = 29
            else:
                max_day = 28
        
        birth_day = random.randint(1, max_day)
        
        year_str = str(birth_year)
        month_str = str(birth_month).zfill(2)
        day_str = str(birth_day).zfill(2)
    
    print(f"✅ 已生成随机生日: {year_str}/{month_str}/{day_str}")
    return year_str, month_str, day_str


def generate_user_info():
    """
    生成完整的随机用户信息
    
    返回:
        dict: 包含姓名和生日的字典
              {
                  'name': 'John Smith',
                  'year': '1995',
                  'month': '03',
                  'day': '15'
              }
    """
    name = generate_random_name()
    year, month, day = generate_random_birthday()
    
    return {
        'name': name,
        'year': year,
        'month': month,
        'day': day
    }


def generate_japan_address():
    """
    生成随机日本地址
    使用 Faker 生成更真实多样的日本地址
    """
    if FAKER_AVAILABLE:
        # 日本主要城市的区域信息
        tokyo_wards = [
            {"ward": "Chiyoda-ku", "zip_prefix": "100"},
            {"ward": "Shibuya-ku", "zip_prefix": "150"},
            {"ward": "Shinjuku-ku", "zip_prefix": "160"},
            {"ward": "Minato-ku", "zip_prefix": "105"},
            {"ward": "Meguro-ku", "zip_prefix": "153"},
            {"ward": "Setagaya-ku", "zip_prefix": "154"},
            {"ward": "Nakano-ku", "zip_prefix": "164"},
            {"ward": "Toshima-ku", "zip_prefix": "170"},
        ]
        
        osaka_areas = [
            {"area": "Kita-ku", "zip_prefix": "530"},
            {"area": "Chuo-ku", "zip_prefix": "540"},
            {"area": "Nishi-ku", "zip_prefix": "550"},
            {"area": "Tennoji-ku", "zip_prefix": "543"},
        ]
        
        # 随机选择城市
        if random.random() < 0.7:  # 70% 东京
            ward_info = random.choice(tokyo_wards)
            addr = {
                "zip": f"{ward_info['zip_prefix']}-{random.randint(1000, 9999)}",
                "state": "Tokyo",
                "city": ward_info["ward"],
                "address1": f"{random.randint(1, 9)}-{random.randint(1, 30)}-{random.randint(1, 20)}"
            }
        else:  # 30% 大阪
            area_info = random.choice(osaka_areas)
            addr = {
                "zip": f"{area_info['zip_prefix']}-{random.randint(1000, 9999)}",
                "state": "Osaka",
                "city": area_info["area"],
                "address1": f"{random.randint(1, 9)}-{random.randint(1, 30)}-{random.randint(1, 20)}"
            }
    else:
        # 回退到旧的固定地址列表
        addresses = [
            {"zip": "100-0005", "state": "Tokyo", "city": "Chiyoda-ku", "address1": "1-1 Marunouchi"},
            {"zip": "160-0022", "state": "Tokyo", "city": "Shinjuku-ku", "address1": "3-14-1 Shinjuku"},
            {"zip": "150-0002", "state": "Tokyo", "city": "Shibuya-ku", "address1": "2-21-1 Shibuya"},
            {"zip": "530-0001", "state": "Osaka", "city": "Osaka-shi", "address1": "1-1 Umeda"},
        ]
        addr = random.choice(addresses)
        random_suffix = f"{random.randint(1, 9)}-{random.randint(1, 20)}"
        addr["address1"] = f"{addr['address1']} {random_suffix}"
    
    print(f"✅ 已生成日本地址: {addr['state']} {addr['city']} {addr['address1']}")
    return addr


def generate_us_address():
    """
    生成随机美国地址
    使用 Faker 生成真实风格的美国地址
    """
    if FAKER_AVAILABLE:
        # 使用美国 Faker
        fake_us = Faker('en_US')
        
        # 常见的免税或低税州（对支付友好）
        states = [
            {"name": "Delaware", "code": "DE", "cities": ["Wilmington", "Dover", "Newark"]},
            {"name": "Oregon", "code": "OR", "cities": ["Portland", "Salem", "Eugene"]},
            {"name": "Montana", "code": "MT", "cities": ["Billings", "Missoula", "Helena"]},
            {"name": "New Hampshire", "code": "NH", "cities": ["Manchester", "Nashua", "Concord"]},
        ]
        
        state_info = random.choice(states)
        city = random.choice(state_info["cities"])
        
        # 生成街道地址
        street_number = random.randint(100, 9999)
        street_names = ["Main St", "Oak Ave", "Maple Dr", "Cedar Ln", "Park Blvd", 
                       "Washington St", "Lincoln Ave", "Jefferson Dr", "Madison Ln"]
        street = random.choice(street_names)
        
        addr = {
            "zip": fake_us.zipcode_in_state(state_info["code"]) if hasattr(fake_us, 'zipcode_in_state') else f"{random.randint(10000, 99999)}",
            "state": state_info["name"],
            "city": city,
            "address1": f"{street_number} {street}"
        }
    else:
        # 回退到固定地址
        addr = {
            "zip": "10001",
            "state": "New York",
            "city": "New York",
            "address1": f"{random.randint(100, 999)} Main St"
        }
    
    print(f"✅ 已生成美国地址: {addr['city']}, {addr['state']} {addr['zip']}")
    return addr


def generate_billing_info(country="JP"):
    """
    生成完整的支付账单信息（姓名 + 地址）
    
    参数:
        country: 国家代码，"JP" 或 "US"
    
    返回:
        dict: 包含姓名和地址的完整账单信息
    """
    # 生成姓名
    name = generate_random_name()
    
    # 根据国家生成地址
    if country.upper() == "US":
        address = generate_us_address()
    else:
        address = generate_japan_address()
    
    billing_info = {
        "name": name,
        "zip": address["zip"],
        "state": address["state"],
        "city": address["city"],
        "address1": address["address1"],
        "country": country.upper()
    }
    
    print("📋 完整账单信息已生成:")
    print(f"   姓名: {billing_info['name']}")
    print(f"   地址: {billing_info['address1']}, {billing_info['city']}")
    print(f"   州/省: {billing_info['state']}, 邮编: {billing_info['zip']}")
    
    return billing_info


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
