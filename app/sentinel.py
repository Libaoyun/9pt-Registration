"""
OpenAI Sentinel 反爬 Proof-of-Work (PoW) 令牌计算模块
基于 auth.openai.com JS 挑战算法的 FNV-1a 32位哈希逆向实现。
"""

import base64
import json
import random
import time
import uuid


class Sentinel:
    """计算 OpenAI Sentinel 反爬系统的 Proof-of-Work 校验 Token"""

    MAX_ATTEMPTS = 500000

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.sid = str(uuid.uuid4())
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _config(self) -> list:
        perf_now = random.uniform(1000, 50000)
        return [
            "1920x1080",
            time.strftime("%a %b %d %Y %H:%M:%S GMT+0000", time.gmtime()),
            4294705152,
            random.random(),
            self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None,
            None,
            "en-US",
            random.random(),
            random.choice(["plugins-undefined", "mimeTypes-undefined"]),
            random.choice(["location", "documentURI"]),
            random.choice(["Object", "parseFloat"]),
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            time.time() * 1000 - perf_now,
        ]

    def _b64(self, data) -> str:
        return base64.b64encode(
            json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
        ).decode("ascii")

    def _requirements_token(self) -> str:
        data = self._config()
        data[3] = 1
        data[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._b64(data)

    def _pow_token(self, seed: str, difficulty: str) -> str:
        diff = str(difficulty or "0")
        start = time.time()
        for i in range(self.MAX_ATTEMPTS):
            data = self._config()
            data[3] = i
            data[9] = round((time.time() - start) * 1000)
            payload = self._b64(data)
            if self._fnv1a_32(seed + payload)[: len(diff)] <= diff:
                return "gAAAAAB" + payload + "~S"
        return "gAAAAAB" + "wQ8Lk5F" * 10 + self._b64(str(None))

    def get(self, session, flow: str) -> str:
        """为指定的 flow 计算并获取完整的 Sentinel Token"""
        r = session.post(
            "https://sentinel.openai.com/backend-api/sentinel/req",
            data=json.dumps({
                "p": self._requirements_token(),
                "id": self.device_id,
                "flow": flow,
            }),
            headers={
                "Content-Type": "text/plain;charset=UTF-8",
                "Origin": "https://sentinel.openai.com",
                "User-Agent": self.user_agent,
            },
            timeout=30,
        )

        if not r.ok:
            raise RuntimeError(f"Sentinel 挑战请求失败: 状态码 {r.status_code}")

        data = r.json()
        token = str(data.get("token") or "")
        if not token:
            raise RuntimeError("Sentinel 返回的 Token 为空")

        pow_data = data.get("proofofwork") or {}
        if pow_data.get("required") and pow_data.get("seed"):
            p = self._pow_token(str(pow_data["seed"]), str(pow_data.get("difficulty", "0")))
        else:
            p = self._requirements_token()

        return json.dumps({
            "p": p,
            "t": "",
            "c": token,
            "id": self.device_id,
            "flow": flow,
        })
