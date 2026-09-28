"""本地二维码适配器（纯 Python 标准库实现）。

- QR 编码器：Byte 模式、纠错等级 M、版本 1-10 自动选择、八种掩码择优
- 输出 SVG（不依赖任何第三方库）
- LocalScanner：本地"扫描"适配抽象。真实部署时把摄像头解码结果喂给 scan()；
  演示环境中由页面上的"模拟扫描"按钮直接回传印刷在标签上的载荷。

载荷约定：
    RES:<住户代号>     住户腕带/床头卡
    MEAL:<餐盒编号>    餐盒标签
    ROUTE:<路线编号>   路线交接码
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# GF(256) Reed-Solomon（本原多项式 0x11D）
# ---------------------------------------------------------------------------

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(degree: int) -> list[int]:
    """生成多项式系数（高位到低位，长度 degree+1），Nayuki 经典构造。"""
    coefs = [0] * (degree + 1)
    coefs[degree] = 1
    root = 1
    for _ in range(degree):
        for j in range(degree):
            coefs[j] = _gf_mul(coefs[j], root) ^ coefs[j + 1]
        coefs[degree] = _gf_mul(coefs[degree], root)
        root = _gf_mul(root, 2)
    return coefs


def _rs_remainder(data: bytes, ec_len: int) -> bytes:
    coefs = _rs_generator(ec_len)
    res = [0] * ec_len
    for byte in data:
        factor = byte ^ res[0]
        res = res[1:] + [0]
        for i in range(ec_len):
            res[i] ^= _gf_mul(coefs[i + 1], factor)
    return bytes(res)


# ---------------------------------------------------------------------------
# 版本表（纠错等级 M）：(每组纠错码字数, G1块数, G1每块数据码, G2块数, G2每块数据码)
# 版本 1-10，对本平台的短载荷绰绰有余（v10 可容纳 ~258 字节）
# ---------------------------------------------------------------------------

_EC_M = {
    1:  (10, 1, 16, 0, 0),
    2:  (16, 1, 28, 0, 0),
    3:  (26, 1, 44, 0, 0),
    4:  (18, 2, 32, 0, 0),
    5:  (24, 2, 43, 0, 0),
    6:  (16, 4, 27, 0, 0),
    7:  (18, 4, 31, 0, 0),
    8:  (22, 2, 38, 2, 39),
    9:  (22, 3, 36, 2, 37),
    10: (26, 4, 43, 1, 44),
}

_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46],
    10: [6, 28, 50],
}


def _choose_version(data_len: int) -> int:
    for version in range(1, 11):
        _, b1, d1, b2, d2 = _EC_M[version]
        capacity = b1 * d1 + b2 * d2
        # Byte 模式头部：版本 1-9 为 4+8 位，10 为 4+16 位
        header = 2 if version < 10 else 3
        if data_len + header <= capacity:
            return version
    raise ValueError("载荷过长，超出版本 10 / 纠错 M 的容量")


def _bit_stream(payload: bytes, version: int) -> str:
    _, b1, d1, b2, d2 = _EC_M[version]
    total_data = b1 * d1 + b2 * d2
    bits = ["0100"]  # Byte 模式
    if version < 10:
        bits.append(f"{len(payload):08b}")
    else:
        bits.append(f"{len(payload):016b}")
    for b in payload:
        bits.append(f"{b:08b}")
    stream = "".join(bits)
    cap_bits = total_data * 8
    stream += "0" * min(4, cap_bits - len(stream))
    stream += "0" * ((8 - len(stream) % 8) % 8)
    codewords = [int(stream[i:i + 8], 2) for i in range(0, len(stream), 8)]
    idx = 0
    while len(codewords) < total_data:
        codewords.append(0xEC if idx % 2 == 0 else 0x11)
        idx += 1
    return bytes(codewords), total_data, (b1, d1, b2, d2)


def _build_blocks(codewords: bytes, ec_len: int, b1: int, d1: int, b2: int, d2: int):
    blocks = []
    pos = 0
    for _ in range(b1):
        data = codewords[pos:pos + d1]
        pos += d1
        blocks.append((data, _rs_remainder(data, ec_len)))
    for _ in range(b2):
        data = codewords[pos:pos + d2]
        pos += d2
        blocks.append((data, _rs_remainder(data, ec_len)))
    return blocks


def _interleave(blocks):
    out = bytearray()
    max_data = max(len(b[0]) for b in blocks)
    for k in range(max_data):
        for data, _ in blocks:
            if k < len(data):
                out.append(data[k])
    ec_len = len(blocks[0][1])
    for k in range(ec_len):
        for _, ec in blocks:
            out.append(ec[k])
    return bytes(out)


# ---------------------------------------------------------------------------
# BCH 格式 / 版本信息
# ---------------------------------------------------------------------------

def _bch(data: int, poly: int, m: int) -> int:
    rem = data << m
    top = poly.bit_length() - 1
    for i in range(rem.bit_length() - 1, m - 1, -1):
        if (rem >> i) & 1:
            rem ^= poly << (i - top)
    return rem & ((1 << m) - 1)


def _format_bits(mask: int) -> int:
    # 纠错等级 M = 0b00
    data = mask  # (0b00 << 3) | mask
    return ((data << 10) | _bch(data, 0x537, 10)) ^ 0x5415


def _version_bits(version: int) -> int:
    return (version << 12) | _bch(version, 0x1F25, 12)


# ---------------------------------------------------------------------------
# 矩阵构建
# ---------------------------------------------------------------------------

def _make_matrix(version: int):
    n = 17 + 4 * version
    m = [[-1] * n for _ in range(n)]   # -1 空, 0/1 数据/功能模块
    fn = [[False] * n for _ in range(n)]  # 功能模块标记（不加掩码）

    def place_finder(r, c):
        # 9x9 区域（含外圈分隔符）全部标记为功能模块
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                rr, cc = r + dr, c + dc
                if not (0 <= rr < n and 0 <= cc < n):
                    continue
                fn[rr][cc] = True
                if dr in (-1, 7) or dc in (-1, 7):
                    m[rr][cc] = 0  # 分隔符
                else:
                    edge = dr in (0, 6) or dc in (0, 6)
                    core = 2 <= dr <= 4 and 2 <= dc <= 4
                    m[rr][cc] = 1 if (edge or core) else 0

    place_finder(0, 0)
    place_finder(0, n - 7)
    place_finder(n - 7, 0)

    # 定时图形
    for i in range(8, n - 8):
        if m[6][i] == -1:
            m[6][i] = i % 2 == 0
            fn[6][i] = True
        if m[i][6] == -1:
            m[i][6] = i % 2 == 0
            fn[i][6] = True

    # 校正图形
    centers = _ALIGN[version]
    for r in centers:
        for c in centers:
            if (r, c) in ((6, 6), (6, n - 7), (n - 7, 6)):
                continue
            if m[r][c] != -1:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    fn[r + dr][c + dc] = True
                    ring = max(abs(dr), abs(dc))
                    m[r + dr][c + dc] = 1 if ring in (0, 2) else 0

    # 预留格式信息区域
    for i in range(9):
        fn[8][i] = fn[i][8] = True
    for i in range(n - 8, n):
        fn[8][i] = fn[i][8] = True
    fn[n - 8][8] = True  # 黑模块旁预留

    # 版本信息（v>=7）
    if version >= 7:
        vb = _version_bits(version)
        for k in range(18):
            bit = (vb >> k) & 1
            r = k // 3
            c = n - 11 + k % 3
            m[r][c] = bit
            fn[r][c] = True
            r2 = n - 11 + k % 3
            c2 = k // 3
            m[r2][c2] = bit
            fn[r2][c2] = True

    return m, fn, n


def _place_data(m, fn, n, bits: str):
    idx = 0
    upward = True
    col = n - 1
    while col > 0:
        if col == 6:
            col = 5
        for step in range(n):
            row = n - 1 - step if upward else step
            for c in (col, col - 1):
                if not fn[row][c] and m[row][c] == -1:
                    m[row][c] = int(bits[idx]) if idx < len(bits) else 0
                    idx += 1
        upward = not upward
        col -= 2


def _mask_bit(mask: int, r: int, c: int) -> int:
    if mask == 0:
        return (r + c) % 2 == 0
    if mask == 1:
        return r % 2 == 0
    if mask == 2:
        return c % 3 == 0
    if mask == 3:
        return (r + c) % 3 == 0
    if mask == 4:
        return (r // 2 + c // 3) % 2 == 0
    if mask == 5:
        return (r * c) % 2 + (r * c) % 3 == 0
    if mask == 6:
        return ((r * c) % 2 + (r * c) % 3) % 2 == 0
    return ((r + c) % 2 + (r * c) % 3) % 2 == 0


def _penalty(m, n) -> int:
    score = 0
    # N1 同行/列 连 5+
    for r in range(n):
        run = 1
        for c in range(1, n):
            if m[r][c] == m[r][c - 1]:
                run += 1
            else:
                score += max(0, run - 2) if run >= 5 else 0
                run = 1
        if run >= 5:
            score += run - 2
    for c in range(n):
        run = 1
        for r in range(1, n):
            if m[r][c] == m[r - 1][c]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2
    # N2 2x2 同色块
    for r in range(n - 1):
        for c in range(n - 1):
            if m[r][c] == m[r + 1][c] == m[r][c + 1] == m[r + 1][c + 1]:
                score += 3
    # N3 类定位图形 1011101 在行/列
    pat1 = (1, 0, 1, 1, 1, 0, 1)
    pad = (0, 0, 0, 0)
    for r in range(n):
        row = tuple(m[r])
        for c in range(n - 10):
            win = row[c:c + 11]
            if win[:7] == pat1 and win[7:] == pad or win[4:] == pat1 and win[:4] == pad:
                score += 40
    for c in range(n):
        col = tuple(m[r][c] for r in range(n))
        for r in range(n - 10):
            win = col[r:r + 11]
            if win[:7] == pat1 and win[7:] == pad or win[4:] == pat1 and win[:4] == pad:
                score += 40
    # N4 黑模块占比
    dark = sum(m[r][c] for r in range(n) for c in range(n))
    pct = dark * 100 // (n * n)
    score += (abs(pct - 50) // 5) * 10
    return score


def _place_format(m, n, mask: int):
    bits = _format_bits(mask)
    # 左上
    for i in range(6):
        m[8][i] = (bits >> i) & 1
    m[8][7] = (bits >> 6) & 1
    m[8][8] = (bits >> 7) & 1
    m[7][8] = (bits >> 8) & 1
    for i in range(9, 15):
        m[14 - i][8] = (bits >> i) & 1
    # 另两份
    for i in range(7):
        m[n - 1 - i][8] = (bits >> i) & 1          # 行 n-1..n-7 ← bits 0..6
    for i in range(7, 15):
        m[8][n - 1 - (i - 7)] = (bits >> i) & 1    # 列 n-1..n-8 ← bits 7..14
    m[n - 8][8] = 1  # 固定黑模块


def encode(payload: str) -> list[list[int]]:
    """把文本载荷编码为 0/1 矩阵（含功能图形，已选掩码）。"""
    raw = payload.encode("utf-8")
    version = _choose_version(len(raw))
    codewords, _total, (b1, d1, b2, d2) = _bit_stream(raw, version)
    ec_len = _EC_M[version][0]
    blocks = _build_blocks(codewords, ec_len, b1, d1, b2, d2)
    interleaved = _interleave(blocks)
    bits = "".join(f"{b:08b}" for b in interleaved)

    base, fn, n = _make_matrix(version)
    best = None
    for mask in range(8):
        import copy
        m = copy.deepcopy(base)
        _place_data(m, fn, n, bits)
        for r in range(n):
            for c in range(n):
                if not fn[r][c] and _mask_bit(mask, r, c):
                    m[r][c] ^= 1
        _place_format(m, n, mask)
        score = _penalty(m, n)
        if best is None or score < best[0]:
            best = (score, m)
    return best[1]


def to_svg(matrix, fg: str = "#263238", bg: str = "#FFFFFF", quiet: int = 4,
           px: int = 8) -> str:
    n = len(matrix)
    size = (n + quiet * 2) * px
    rects = [f'<rect width="{size}" height="{size}" fill="{bg}"/>']
    for r, row in enumerate(matrix):
        for c, v in enumerate(row):
            if v:
                rects.append(
                    f'<rect x="{(c + quiet) * px}" y="{(r + quiet) * px}" '
                    f'width="{px}" height="{px}" fill="{fg}"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
            f'width="{size}" height="{size}" shape-rendering="crispEdges">'
            + "".join(rects) + "</svg>")


# ---------------------------------------------------------------------------
# 载荷与本地扫描适配器
# ---------------------------------------------------------------------------

VALID_PREFIXES = ("RES", "MEAL", "ROUTE")


def build_payload(kind: str, code: str) -> str:
    if kind not in VALID_PREFIXES:
        raise ValueError(f"未知载荷类型 {kind}")
    return f"{kind}:{code}"


class ScanError(ValueError):
    """扫描内容无法识别。"""


class LocalScanner:
    """本地扫描适配器。

    真实硬件：把摄像头/扫码枪得到的文本传入 scan()。
    演示：点击页面标签上的"模拟扫描"即等价于一次扫码。
    额外支持从命令行风格输入（大小写/空白容错）。
    """

    def scan(self, raw: str) -> tuple[str, str]:
        if raw is None:
            raise ScanError("空扫描内容")
        text = raw.strip().upper()
        if ":" not in text:
            raise ScanError(f"无法识别的二维码：{raw!r}")
        kind, code = text.split(":", 1)
        if kind not in VALID_PREFIXES or not code:
            raise ScanError(f"无法识别的二维码：{raw!r}")
        return kind, code
