"""توليد رسومات الباركود (SVG) بدون مكتبات خارجية: EAN-13 / EAN-8 / UPC-A وCode 128.

الرسم يُعاد كنص SVG يُدرج مباشرة في صفحة الطباعة، فيُطبع بدقة الطابعة أياً كان مقاس الملصق.
"""
from html import escape

CODE128 = [
    "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312", "132212", "221213",
    "221312", "231212", "112232", "122132", "122231", "113222", "123122", "123221", "223211", "221132",
    "221231", "213212", "223112", "312131", "311222", "321122", "321221", "312212", "322112", "322211",
    "212123", "212321", "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
    "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121", "313121", "211331",
    "231131", "213113", "213311", "213131", "311123", "311321", "331121", "312113", "312311", "332111",
    "314111", "221411", "431111", "111224", "111422", "121124", "121421", "141122", "141221", "112214",
    "112412", "122114", "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
    "111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112", "421211", "212141",
    "214121", "412121", "111143", "111341", "131141", "114113", "114311", "411113", "411311", "113141",
    "114131", "311141", "411131", "211412", "211214", "211232", "2331112",
]
START_B, START_C, CODE_B, CODE_C, STOP = 104, 105, 100, 99, 106

EAN_L = ["0001101", "0011001", "0010011", "0111101", "0100011", "0110001", "0101111", "0111011", "0110111",
         "0001011"]
EAN_G = ["0100111", "0110011", "0011011", "0100001", "0011101", "0111001", "0000101", "0010001", "0001001",
         "0010111"]
EAN_R = ["1110010", "1100110", "1101100", "1000010", "1011100", "1001110", "1010000", "1000100", "1001000",
         "1110100"]
EAN_PARITY = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG", "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"]


class BarcodeError(ValueError):
    pass


def ean_checksum(digits):
    """رقم التحقق لـ EAN/UPC من الأرقام بدونه."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return str((10 - total % 10) % 10)


def is_valid_ean(code):
    return code.isdigit() and len(code) in (8, 12, 13) and ean_checksum(code[:-1]) == code[-1]


def _ean13_modules(code):
    first, left, right = int(code[0]), code[1:7], code[7:]
    parity = EAN_PARITY[first]
    bits = "101"
    bits += "".join((EAN_L if p == "L" else EAN_G)[int(d)] for p, d in zip(parity, left))
    bits += "01010" + "".join(EAN_R[int(d)] for d in right) + "101"
    return bits


def _ean8_modules(code):
    return "101" + "".join(EAN_L[int(d)] for d in code[:4]) + "01010" + "".join(EAN_R[int(d)] for d in code[4:]) + "101"


def _code128_values(text):
    if not text or any(not 32 <= ord(c) <= 126 for c in text):
        raise BarcodeError("Code 128 يقبل الأحرف الإنجليزية والأرقام والرموز فقط.")
    values = []
    # الأرقام المتتالية تُضغط زوجين في رمز واحد (المجموعة C) ليقصر الباركود
    if text.isdigit() and len(text) >= 2:
        even = text[: len(text) - len(text) % 2]
        values = [START_C] + [int(even[i:i + 2]) for i in range(0, len(even), 2)]
        if len(text) % 2:
            values += [CODE_B, ord(text[-1]) - 32]
    else:
        values = [START_B] + [ord(c) - 32 for c in text]
    checksum = (values[0] + sum(i * v for i, v in enumerate(values[1:], start=1))) % 103
    return values + [checksum, STOP]


def _code128_modules(text):
    bits = ""
    for value in _code128_values(text):
        for i, width in enumerate(CODE128[value]):
            bits += ("1" if i % 2 == 0 else "0") * int(width)
    return bits


def encode(code):
    """يعيد (نوع الباركود، سلسلة الوحدات 1/0، هامش يسار، هامش يمين) بالوحدات."""
    code = (code or "").strip()
    if is_valid_ean(code):
        if len(code) == 12:  # UPC-A هو EAN-13 بصفر في أوله
            return "UPC-A", _ean13_modules("0" + code), 11, 7
        if len(code) == 13:
            return "EAN-13", _ean13_modules(code), 11, 7
        return "EAN-8", _ean8_modules(code), 7, 7
    return "Code 128", _code128_modules(code), 10, 10


def svg(code, height=40):
    """رسم الباركود SVG قابل للتمدد بعرض الملصق (الهوامش البيضاء محسوبة ضمنه)."""
    _kind, bits, quiet_left, quiet_right = encode(code)
    width = quiet_left + len(bits) + quiet_right
    rects, x, i = [], quiet_left, 0
    while i < len(bits):
        if bits[i] == "1":
            run = len(bits[i:]) - len(bits[i:].lstrip("1"))
            rects.append(f'<rect x="{x + i}" y="0" width="{run}" height="{height}"/>')
            i += run
        else:
            i += 1
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
            f'shape-rendering="crispEdges" role="img" aria-label="{escape(code)}">'
            f'<rect width="{width}" height="{height}" fill="#fff"/><g fill="#000">{"".join(rects)}</g></svg>')


def internal_ean13(sequence):
    """باركود داخلي للمتجر (بادئة 200 المخصصة للاستخدام الداخلي) من رقم تسلسلي."""
    body = "200" + str(int(sequence)).zfill(9)[-9:]
    return body + ean_checksum(body)
