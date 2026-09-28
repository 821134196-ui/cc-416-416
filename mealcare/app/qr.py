"""本地二维码适配器 —— 纯 Python 标准库实现的 QR Code(Model 2) 编码器。

- 字节模式(byte mode),纠错等级 M,版本 1-10 自动选择
- Reed-Solomon GF(256) 纠错码
- 8 种掩码按规范罚分择优
- 输出 SVG(无第三方依赖)

真实的 USB / 蓝牙扫码枪在本系统中以"键盘楔入(keyboard wedge)"方式工作:
扫描结果作为文本输入到核对页的扫描框并回车;没有硬件时,点击页面上的
二维码同样会触发一次"本地扫描"(演示适配器),两条路径共用同一套核对逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass
from xml.sax.saxutils import quoteattr

# ---- GF(256),本原多项式 x^8+x^4+x^3+x^2+1 = 0x11D -------------------------

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


def _rs_generator(degree: int):
    """生成多项式 ∏(x - α^i),返回升幂系数。"""
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= _gf_mul(coef, _EXP[i])
            nxt[j + 1] ^= coef
        poly = nxt
    return poly


def _rs_remainder(data, ec_len: int):
    gen = _rs_generator(ec_len)
    res = [0] * ec_len
    for cw in data:
        factor = cw ^ res[0]
        res = res[1:] + [0]
        # gen 为升幂 [g0..g_n];移位后 res[i] 对应 g_{n-1-i}
        for j in range(ec_len):
            res[j] ^= _gf_mul(gen[ec_len - 1 - j], factor)
    return res


# ---- 版本容量表(纠错等级 M) ------------------------------------------------
# 每条: (数据码字数, [每组 (块数, 总码字数, 数据码字数), ...])
_EC_M = {
    1: (16, [(1, 26, 16)]),
    2: (28, [(1, 44, 28)]),
    3: (44, [(1, 70, 44)]),
    4: (64, [(2, 50, 32)]),
    5: (86, [(2, 67, 43)]),
    6: (108, [(4, 43, 27)]),
    7: (124, [(4, 49, 31)]),
    8: (154, [(2, 60, 38), (2, 61, 39)]),
    9: (182, [(3, 58, 36), (2, 59, 37)]),
    10: (216, [(4, 69, 43), (1, 70, 44)]),
}

# 字节模式计数指示位:版本 1-9 为 8 位,10-40 为 16 位
def _byte_count_bits(version: int) -> int:
    return 8 if version <= 9 else 16


# 字节模式在该版本下可容纳字节数
def _byte_capacity(version: int) -> int:
    data_cw = _EC_M[version][0]
    return (data_cw * 8 - 4 - _byte_count_bits(version)) // 8


_ALIGN = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}

_ECC_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}
_FORMAT_MASK = 0x5412


def _bch15_5(data5: int) -> int:
    rem = data5 << 10
    for i in range(14, 9, -1):
        if (rem >> i) & 1:
            rem ^= 0x537 << (i - 10)
    return (data5 << 10) | (rem & 0x3FF)


def _bch18_6(version: int) -> int:
    rem = version << 12
    for i in range(17, 11, -1):
        if (rem >> i) & 1:
            rem ^= 0x1F25 << (i - 12)
    return (version << 12) | (rem & 0xFFF)


def _mask_condition(mask: int, r: int, c: int) -> bool:
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


@dataclass
class QrResult:
    matrix: list
    version: int
    mask: int
    size: int


def _build_payload_bits(text: str, version: int) -> list:
    raw = text.encode("utf-8")
    cap = _byte_capacity(version)
    if len(raw) > cap:
        raise ValueError(f"数据过长: {len(raw)} 字节,版本 {version}-M 容量 {cap}")
    data_cw = _EC_M[version][0]
    total_bits = data_cw * 8
    bits = []
    bits += [0, 1, 0, 0]                       # 字节(8-bit byte)模式指示
    cbits = _byte_count_bits(version)
    bits += [(len(raw) >> b) & 1 for b in range(cbits - 1, -1, -1)]  # 字符计数
    for byte in raw:
        bits += [(byte >> b) & 1 for b in range(7, -1, -1)]
    bits += [0] * min(4, total_bits - len(bits))              # 终止符
    while len(bits) % 8:
        bits.append(0)
    codewords = []
    for i in range(0, len(bits), 8):
        codewords.append(sum(b << (7 - j) for j, b in enumerate(bits[i:i + 8])))
    pad = (0xEC, 0x11)
    pad_i = 0
    while len(codewords) < data_cw:
        codewords.append(pad[pad_i % 2])  # 先 EC 后 11 交替
        pad_i += 1
    return codewords[:data_cw]


def _interleave(codewords, version):
    groups = _EC_M[version][1]
    blocks = []
    pos = 0
    for count, total, data_len in groups:
        ec_len = total - data_len
        for _ in range(count):
            data = codewords[pos:pos + data_len]
            pos += data_len
            blocks.append((data, _rs_remainder(data, ec_len), ec_len))
    out = []
    max_data = max(len(b[0]) for b in blocks)
    for col in range(max_data):
        for data, _ec, _n in blocks:
            if col < len(data):
                out.append(data[col])
    ec_len = blocks[0][2]
    for col in range(ec_len):
        for _data, ec, _n in blocks:
            out.append(ec[col])
    return out


def _make_matrix(version: int, mask: int, stream_bits) -> tuple:
    n = 21 + 4 * (version - 1)
    m = [[0] * n for _ in range(n)]
    reserved = [[False] * n for _ in range(n)]

    def setmod(r, c, v, reserve=True):
        if 0 <= r < n and 0 <= c < n:
            m[r][c] = v
            if reserve:
                reserved[r][c] = True

    # 寻像图形 + 分隔符
    def finder(br, bc):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = br + dr, bc + dc
                if not (0 <= r < n and 0 <= c < n):
                    continue
                if dr in (-1, 7) or dc in (-1, 7):
                    setmod(r, c, 0)
                else:
                    v = dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)
                    setmod(r, c, 1 if v else 0)

    finder(0, 0)
    finder(0, n - 7)
    finder(n - 7, 0)

    # 定时图形
    for i in range(8, n - 8):
        setmod(6, i, (i + 1) % 2)
        setmod(i, 6, (i + 1) % 2)

    # 校正图形
    centers = _ALIGN[version]
    for cr in centers:
        for cc in centers:
            if (cr, cc) in ((6, 6), (6, centers[-1]), (centers[-1], 6)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    edge = dr in (-2, 2) or dc in (-2, 2) or (dr == 0 and dc == 0)
                    setmod(cr + dr, cc + dc, 1 if edge else 0)

    # 固定暗块
    setmod(4 * version + 9, 8, 1)

    # 格式信息预留位(先占位,掩码择优后写入)
    # 第一副本: 列 8 行 0..5,7,8;行 8 列 0..5,7,8
    for r in (0, 1, 2, 3, 4, 5, 7, 8):
        reserved[r][8] = True
    for c in (0, 1, 2, 3, 4, 5, 7, 8):
        reserved[8][c] = True
    # 第二副本: 行 8 列 n-8..n-1;列 8 行 n-7..n-1
    for c in range(n - 8, n):
        reserved[8][c] = True
    for r in range(n - 7, n):
        reserved[r][8] = True
    reserved[4 * version + 9][8] = True

    # 版本信息(version >= 7),两份 6x3,bit0 起按 LSB 放置
    if version >= 7:
        bits18 = _bch18_6(version)
        for i in range(18):
            bit = (bits18 >> i) & 1
            setmod(i // 3, n - 11 + i % 3, bit)          # 右上 6 行 × 3 列
            setmod(n - 11 + i % 3, i // 3, bit)          # 左下 3 行 × 6 列

    # 数据 Zigzag 填充(同时施加掩码)
    # 列对: 最右向左的偶数列对,越过定时列 6 后改走 (5,4)(3,2)(1,0)
    rights = list(range(n - 1, 7, -2)) + [5, 3, 1]
    bit_idx = 0
    for pair, col in enumerate(rights):
        upward = pair % 2 == 0   # 第 0、2、4… 对列从底部向上
        for vert in range(n):
            r = (n - 1 - vert) if upward else vert
            for j in range(2):
                c = col - j
                if reserved[r][c]:
                    continue
                bit = stream_bits[bit_idx]
                bit_idx += 1
                m[r][c] = bit ^ (1 if _mask_condition(mask, r, c) else 0)
    free_modules = sum(row.count(False) for row in reserved)
    assert bit_idx == len(stream_bits) == free_modules, (
        version, bit_idx, len(stream_bits), free_modules)

    # 格式信息(两份,15 位,bit0 为最低位;坐标按 x=列,y=行)
    fmt = _bch15_5((_ECC_BITS["M"] << 3) | mask) ^ _FORMAT_MASK
    # 第一副本: 竖条 col8 行0..5 放 bit0..5;行8 放 bit6..8;行8 列5..0 放 bit9..14
    for i in range(6):
        m[i][8] = (fmt >> i) & 1
    m[7][8] = (fmt >> 6) & 1
    m[8][8] = (fmt >> 7) & 1
    m[8][7] = (fmt >> 8) & 1
    for i in range(9, 15):
        m[8][14 - i] = (fmt >> i) & 1
    # 第二副本: 行8 列 n-1..n-8 放 bit0..7;列8 行 n-7..n-1 放 bit8..14
    for i in range(8):
        m[8][n - 1 - i] = (fmt >> i) & 1
    for i in range(8, 15):
        m[n - 15 + i][8] = (fmt >> i) & 1
    # 固定暗块(row=4v+9, col=8)最后写,覆盖该位置上的 bit8
    m[4 * version + 9][8] = 1
    return m


def _penalty(m) -> int:
    n = len(m)
    score = 0
    # N1: 同行/列连续同色
    for line in [m, [list(r) for r in zip(*m)]]:
        for row in line:
            run = 1
            for i in range(1, n):
                if row[i] == row[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run = 1
            if run >= 5:
                score += 3 + run - 5
    # N2: 2x2 同色块
    for r in range(n - 1):
        for c in range(n - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    # N3: 1011101 两侧四白
    for r in range(n):
        for c in range(n - 10):
            w = tuple(m[r][c:c + 11])
            if w == (0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1) or \
               w == (1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0):
                score += 40
    for c in range(n):
        for r in range(n - 10):
            w = tuple(m[r + k][c] for k in range(11))
            if w == (0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1) or \
               w == (1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0):
                score += 40
    # N4: 深色比例
    dark = sum(sum(row) for row in m)
    pct = dark * 200 // (n * n)          # 深色比例 *2
    score += (abs(pct - 100) // 10) * 10
    return score


def encode(text: str, max_version: int = 10) -> QrResult:
    """编码文本,自动选择最小版本与最优掩码。"""
    raw = text.encode("utf-8")
    version = None
    for v in range(1, max_version + 1):
        if len(raw) <= _byte_capacity(v):
            version = v
            break
    if version is None:
        raise ValueError(f"超出最大版本 {max_version}-M 容量")
    codewords = _build_payload_bits(text, version)
    stream = []
    for cw in _interleave(codewords, version):
        stream += [(cw >> b) & 1 for b in range(7, -1, -1)]
    # 剩余位(remainder bits):部分版本数据位总数不是 8 的倍数,补 0
    raw_modules = (16 * version + 128) * version + 64
    if version >= 2:
        num_align = version // 7 + 2
        raw_modules -= (25 * num_align - 10) * num_align - 55
        if version >= 7:
            raw_modules -= 36
    stream += [0] * (raw_modules - len(stream))
    best = None
    for mask in range(8):
        m = _make_matrix(version, mask, stream)
        p = _penalty(m)
        if best is None or p < best[0]:
            best = (p, mask, m)
    return QrResult(matrix=best[2], version=version, mask=best[1],
                    size=len(best[2]))


def to_svg(text: str, module: int = 8, quiet: int = 4) -> str:
    res = encode(text)
    n = res.size
    dim = (n + quiet * 2) * module
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{dim}" height="{dim}" '
        f'viewBox="0 0 {dim} {dim}" role="img" aria-label={quoteattr(text)}>',
        f'<rect width="{dim}" height="{dim}" fill="#fffdf7"/>',
    ]
    for r in range(n):
        for c in range(n):
            if res.matrix[r][c]:
                parts.append(
                    f'<rect x="{(c + quiet) * module}" y="{(r + quiet) * module}" '
                    f'width="{module}" height="{module}" fill="#23303a"/>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 业务侧扫码适配器:统一住户码 / 餐盒码的编码与解析
# ---------------------------------------------------------------------------

PREFIX_RESIDENT = "RES"
PREFIX_BOX = "BOX"


def resident_code(resident_id: str) -> str:
    return f"{PREFIX_RESIDENT}:{resident_id}"


def box_code(box_code_id: str) -> str:
    return f"{PREFIX_BOX}:{box_code_id}"


def parse_scan(payload: str):
    """解析一次扫描。返回 (kind, id),kind ∈ {'resident','box'};无法识别抛 ValueError。"""
    payload = (payload or "").strip().upper()
    if payload.startswith(PREFIX_RESIDENT + ":"):
        rid = payload.split(":", 1)[1].strip()
        if rid:
            return "resident", rid
    if payload.startswith(PREFIX_BOX + ":"):
        bid = payload.split(":", 1)[1].strip()
        if bid:
            return "box", bid
    raise ValueError(f"无法识别的二维码: {payload!r}")
