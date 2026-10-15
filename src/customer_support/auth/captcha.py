"""图形验证码（自绘 SVG + 服务端一次性核对）。

为什么自绘而不是用第三方服务：不引额外依赖、不外发请求（内网部署也能用）。

## 安全设计（这里踩过一个真实的坑）

**旧实现把答案写在了 ``captcha_id`` 里**：``captcha_id = base64(过期时间.答案.随机数) + HMAC 签名``。
作者当时的想法是「答案用签名保护，只有持密钥的一方能解出」——但 **base64 不是加密**，
任何客户端都能把 ``captcha_id`` 解回明文直接读出答案：

    >>> base64.urlsafe_b64decode("17...1.BZT3.8b18a848".split(".")[0] + "==")
    b"1791980201.BZT3.8b18a848"

于是「防脚本撞库」形同虚设；而且同一 ``captcha_id`` 在有效期内可以无限次重复提交，
连「答错要重来」都做不到。

**现在的做法**：``captcha_id`` 只是一個 192 位随机串（不含任何答案信息），
答案存在服务端的进程内表里，特征三件套：

1. **不回传**：答案只存在于服务端内存，客户端拿到的 id 反推不出答案；
2. **一次性**：校验通过即删除（``pop``），防重放；
3. **限次 + TTL**：单张验证码最多试 ``MAX_ATTEMPTS`` 次，到期即失效，并带容量上限防内存增长。

**取舍（工程权衡）**：这放弃了「无状态」——多副本部署时各副本的表不共享，
需要把 ``_STORE`` 换成 Redis。原因是原方案的「无状态」是**靠泄密换来的**：
不存答案就必须把答案放进票据，而票据是公开的。安全 > 无状态，
本项目单机部署形态下进程内表完全够用（与 ``SessionStore`` 同一取舍）。
"""
from __future__ import annotations

import hmac
import random
import secrets
import threading
import time
from html import escape
from typing import NamedTuple

# 去掉 0/O/1/I/l 等易混字符，降低真人误输
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 4

# 单张验证码最多允许的校验次数：够真人容错，又不足以在 TTL 内穷举答案空间
MAX_ATTEMPTS = 5
# 内存上限：超过后按插入顺序淘汰最旧的（防恶意刷 /captcha 撑爆内存）
MAX_ENTRIES = 5000


class _Entry(NamedTuple):
    code: str
    expires_at: float
    attempts: int


_STORE: dict[str, _Entry] = {}
_STORE_LOCK = threading.Lock()


def _purge_locked(now: float) -> None:
    """清掉过期条目；再按插入顺序淘汰超额条目（调用方需持锁）。"""
    if not _STORE:
        return
    for key in [k for k, e in _STORE.items() if e.expires_at <= now]:
        _STORE.pop(key, None)
    while len(_STORE) > MAX_ENTRIES:
        _STORE.pop(next(iter(_STORE)), None)


def create_captcha(
    *, secret: str, ttl_seconds: int = 300, length: int = _CODE_LENGTH
) -> tuple[str, str, str]:
    """生成验证码，返回 ``(code, captcha_id, svg)``。

    ``code`` 供 SVG 绘制与（仅限进程内的）测试使用，**绝不回传给客户端**；
    ``captcha_id`` 是不含任何答案信息的随机句柄。

    Args:
        secret: 保留参数（历史签名方案使用）。当前实现不把答案放进票据，
            因此不再需要密钥；保留是为了不改调用方签名。
        ttl_seconds: 有效期
        length: 字符数
    """
    del secret  # 见 docstring：答案不进票据后不再需要密钥参与
    now = time.time()
    # 答案也用 CSPRNG：旧实现用 random.choice（Mersenne Twister 可被预测）
    code = "".join(secrets.choice(_ALPHABET) for _ in range(max(3, length)))
    captcha_id = secrets.token_urlsafe(24)  # 192 bit，不可枚举

    with _STORE_LOCK:
        _purge_locked(now)
        _STORE[captcha_id] = _Entry(code=code, expires_at=now + ttl_seconds, attempts=0)
    return code, captcha_id, render_svg(code)


def verify_captcha(captcha_id: str, answer: str, *, secret: str = "") -> bool:
    """校验验证码；**通过即作废**（一次性），失败累计次数，超限或过期即删。

    - 大小写不敏感、首尾空白忽略（真人友好）；
    - 未知 / 过期 / 超限的 id 一律返回 ``False``，不区分原因（不给试探线索）。
    """
    del secret  # 保留参数，语义同 create_captcha
    if not captcha_id or not answer:
        return False

    now = time.time()
    with _STORE_LOCK:
        entry = _STORE.get(captcha_id)
        if entry is None:
            return False
        if entry.expires_at <= now:
            _STORE.pop(captcha_id, None)
            return False
        entry = entry._replace(attempts=entry.attempts + 1)
        _STORE[captcha_id] = entry

    if hmac.compare_digest(entry.code.upper(), answer.strip().upper()):
        with _STORE_LOCK:
            _STORE.pop(captcha_id, None)  # 一次性：用完即焚，防重放
        return True

    if entry.attempts >= MAX_ATTEMPTS:
        with _STORE_LOCK:
            _STORE.pop(captcha_id, None)
    return False


def peek_code(captcha_id: str) -> str | None:
    """读取某张验证码的答案——**仅供进程内测试使用**。

    原先测试是从 ``captcha_id`` 里 base64 解出答案的，那恰好证明了旧实现的漏洞；
    修复后答案不再离开服务端，测试改为直接读进程内表（与生产同进程）。
    """
    with _STORE_LOCK:
        entry = _STORE.get(captcha_id)
    return entry.code if entry else None


def reset_store() -> None:
    """清空验证码表（测试用）。"""
    with _STORE_LOCK:
        _STORE.clear()


def render_svg(code: str, *, width: int = 132, height: int = 44) -> str:
    """把验证码画成带干扰线/噪点的 SVG（内联返回，前端无需额外请求）。"""
    rng = random.Random(secrets.randbits(32))
    safe_code = escape(code)
    per_char_width = (width - 16) / max(len(code), 1)

    lines = []
    for _ in range(4):
        x1, y1 = rng.randint(0, width), rng.randint(0, height)
        x2, y2 = rng.randint(0, width), rng.randint(0, height)
        hue = rng.randint(0, 360)
        lines.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="hsl({hue},70%,70%)" stroke-width="1.4"/>'
        )

    dots = []
    for _ in range(28):
        hue = rng.randint(0, 360)
        dots.append(
            f'<circle cx="{rng.randint(0, width)}" cy="{rng.randint(0, height)}" r="1.2" '
            f'fill="hsl({hue},65%,72%)"/>'
        )

    chars = []
    for index, char in enumerate(safe_code):
        x = 10 + index * per_char_width
        y = height * 0.72 + rng.randint(-4, 4)
        rotation = rng.randint(-22, 22)
        hue = rng.randint(200, 280)
        chars.append(
            f'<text x="{x:.1f}" y="{y:.1f}" transform="rotate({rotation} {x:.1f} {y:.1f})" '
            f'font-family="Menlo,Consolas,monospace" font-size="26" font-weight="700" '
            f'fill="hsl({hue},55%,32%)">{escape(char)}</text>'
        )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="图形验证码">'
        f'<rect width="{width}" height="{height}" rx="6" fill="#f4f6fb"/>'
        + "".join(lines)
        + "".join(dots)
        + "".join(chars)
        + "</svg>"
    )
