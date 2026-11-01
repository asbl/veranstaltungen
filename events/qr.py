"""Minimaler QR-Code-Encoder (Byte-Modus, Fehlerkorrektur M, Version 1–10).

Bewusst ohne externe Abhängigkeit: Das Web-Image wird von der
Container-Security-Automatik gebaut, die nur freigegebene Pakete nachrüstet.
Für BGG-Links (ca. 45 Zeichen) reicht Version 4. Aufbau nach ISO/IEC 18004;
die Tests vergleichen die Matrix mit der Referenzbibliothek segno.
"""
from django.utils.html import escape
from django.utils.safestring import mark_safe

# Version: (EC-Codewörter je Block, [(Anzahl Blöcke, Datencodewörter je Block), ...]) für Stufe M
_BLOCKS_M = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
_ALIGNMENT = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}
_ECL_M_FORMAT_BITS = 0b00

_MASKS = [
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]


def _gf_mul(a, b):
    result = 0
    while b:
        if b & 1:
            result ^= a
        a <<= 1
        if a & 0x100:
            a ^= 0x11D
        b >>= 1
    return result


def _rs_remainder(data, degree):
    divisor = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            divisor[j] = _gf_mul(divisor[j], root)
            if j + 1 < degree:
                divisor[j] ^= divisor[j + 1]
        root = _gf_mul(root, 2)
    result = [0] * degree
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _codewords(payload, version):
    ec_len, groups = _BLOCKS_M[version]
    capacity = sum(count * size for count, size in groups)
    bits = [0, 1, 0, 0]
    count_bits = 8 if version < 10 else 16
    bits += [(len(payload) >> i) & 1 for i in reversed(range(count_bits))]
    for byte in payload:
        bits += [(byte >> i) & 1 for i in reversed(range(8))]
    bits += [0] * min(4, capacity * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = [0xEC, 0x11]
    while len(data) < capacity:
        data.append(pad[(len(data) - len(bits) // 8) % 2])
    blocks, pos = [], 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(data[pos:pos + size])
            pos += size
    ecc = [_rs_remainder(block, ec_len) for block in blocks]
    result = []
    for i in range(max(len(block) for block in blocks)):
        result += [block[i] for block in blocks if i < len(block)]
    for i in range(ec_len):
        result += [block[i] for block in ecc]
    return result


class _Matrix:
    def __init__(self, version):
        self.version = version
        self.size = version * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]

    def set(self, x, y, dark):
        self.modules[y][x] = dark
        self.function[y][x] = True

    def draw_function_patterns(self):
        size = self.size
        for i in range(size):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        self.set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        positions = _ALIGNMENT[self.version]
        last = len(positions) - 1
        for i, ax in enumerate(positions):
            for j, ay in enumerate(positions):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format_bits(0)
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.version << 12 | rem
            for i in range(18):
                dark = (bits >> i) & 1 == 1
                a, b = size - 11 + i % 3, i // 3
                self.set(a, b, dark)
                self.set(b, a, dark)

    def draw_format_bits(self, mask):
        data = _ECL_M_FORMAT_BITS << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1
        size = self.size
        for i in range(6):
            self.set(8, i, bit(i))
        self.set(8, 7, bit(6))
        self.set(8, 8, bit(7))
        self.set(7, 8, bit(8))
        for i in range(9, 15):
            self.set(14 - i, 8, bit(i))
        for i in range(8):
            self.set(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set(8, size - 15 + i, bit(i))
        self.set(8, size - 8, True)

    def draw_codewords(self, data):
        size, i = self.size, 0
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = size - 1 - vert if upward else vert
                    if not self.function[y][x] and i < len(data) * 8:
                        self.modules[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        test = _MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and test(x, y):
                    self.modules[y][x] = not self.modules[y][x]

    def penalty(self):
        size, modules, score = self.size, self.modules, 0
        lines = modules + [list(col) for col in zip(*modules)]
        finder_like = ([True, False, True, True, True, False, True, False, False, False, False],
                       [False, False, False, False, True, False, True, True, True, False, True])
        for line in lines:
            run = 1
            for a, b in zip(line, line[1:]):
                if a == b:
                    run += 1
                else:
                    score += run - 2 if run >= 5 else 0
                    run = 1
            score += run - 2 if run >= 5 else 0
            for start in range(len(line) - 10):
                if line[start:start + 11] in finder_like:
                    score += 40
        for y in range(size - 1):
            for x in range(size - 1):
                if modules[y][x] == modules[y][x + 1] == modules[y + 1][x] == modules[y + 1][x + 1]:
                    score += 3
        dark = sum(map(sum, modules))
        score += abs(dark * 20 - size * size * 10) // (size * size) * 10
        return score


def qr_matrix(text, mask=None):
    """Liefert die QR-Matrix als Liste von Zeilen (True = dunkel)."""
    payload = text.encode("utf-8")
    for version, (_, groups) in _BLOCKS_M.items():
        capacity = sum(count * size for count, size in groups)
        header_bytes = 2 if version < 10 else 3
        if len(payload) + header_bytes <= capacity:
            break
    else:
        raise ValueError("Text ist zu lang für den QR-Code")
    codewords = _codewords(payload, version)
    best = None
    for candidate in ([mask] if mask is not None else range(8)):
        matrix = _Matrix(version)
        matrix.draw_function_patterns()
        matrix.draw_codewords(codewords)
        matrix.apply_mask(candidate)
        matrix.draw_format_bits(candidate)
        score = matrix.penalty()
        if best is None or score < best[0]:
            best = (score, matrix)
    return best[1].modules


def qr_svg(text, label=""):
    """QR-Code als eingebettetes SVG mit 4 Modulen Ruhezone."""
    modules = qr_matrix(text)
    border = 4
    size = len(modules) + 2 * border
    path = "".join(
        f"M{x + border} {y + border}h1v1h-1z"
        for y, row in enumerate(modules)
        for x, dark in enumerate(row)
        if dark
    )
    return mark_safe(
        f'<svg class="qr" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'shape-rendering="crispEdges" role="img" aria-label="{escape(label or text)}">'
        f'<rect width="{size}" height="{size}" fill="#fff"/><path d="{path}" fill="#000"/></svg>'
    )
