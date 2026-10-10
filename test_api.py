"""
Chat2Api 接口连通性测试脚本
"""
import requests
import json
import sys

BASE_URL = "http://127.0.0.1:5005"

def test_health():
    print(f"[*] 正在测试服务连通性: {BASE_URL}/docs ...")
    try:
        resp = requests.get(f"{BASE_URL}/docs", timeout=5)
        if resp.status_code == 200:
            print("[+] 服务正常运行中！Swagger 文档状态码 200 OK")
            return True
        else:
            print(f"[-] 服务返回异常状态码: {resp.status_code}")
            return False
    except requests.exceptions.ConnectionError:
        print("[-] 无法连接到服务，请先运行 start.bat 启动服务！")
        return False
    except Exception as e:
        print(f"[-] 发生错误: {e}")
        return False

def test_tokens_page():
    print(f"[*] 正在检查 Tokens 管理页: {BASE_URL}/tokens ...")
    try:
        resp = requests.get(f"{BASE_URL}/tokens", timeout=5)
        if resp.status_code == 200:
            print("[+] Tokens 管理页面正常开启 (200 OK)")
            return True
        else:
            print(f"[-] 页面状态码: {resp.status_code}")
            return False
    except Exception as e:
        print(f"[-] 发生错误: {e}")
        return False

if __name__ == "__main__":
    print("=" * 50)
    print("        Chat2Api 自动化自检工具")
    print("=" * 50)
    h_ok = test_health()
    if h_ok:
        test_tokens_page()
        print("\n[+] 自检完成！服务已准备就绪，可以正常接收请求。")
    else:
        sys.exit(1)
