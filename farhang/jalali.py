"""Solar Hijri (Jalali) <-> Gregorian conversion and Persian date parsing.

Conversion uses the 33-year arithmetic rule as implemented in jalaali-js
(Borkowski's algorithm), which matches the official Iranian calendar for 1178-1633 AP.
"""

import re

from .normalize import normalize, to_ascii_digits

_BREAKS = [-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210,
           1635, 2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178]

JALALI_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
                 "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
GREGORIAN_MONTHS = ["january", "february", "march", "april", "may", "june", "july",
                    "august", "september", "october", "november", "december"]
GREGORIAN_MONTHS_FA = ["ژانویه", "فوریه", "مارس", "آوریل", "مه", "ژوئن", "ژوئیه",
                       "اوت", "سپتامبر", "اکتبر", "نوامبر", "دسامبر"]


def _div(a, b):
    """Integer division truncating toward zero, as in the reference JavaScript."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def _mod(a, b):
    return a - _div(a, b) * b


def _jal_cal(jy):
    gy = jy + 621
    leap_j = -14
    jp = _BREAKS[0]
    if jy < jp or jy >= _BREAKS[-1]:
        raise ValueError(f"Jalali year out of range: {jy}")
    jump = 0
    for jm in _BREAKS[1:]:
        jump = jm - jp
        if jy < jm:
            break
        leap_j += _div(jump, 33) * 8 + _div(_mod(jump, 33), 4)
        jp = jm
    n = jy - jp
    leap_j += _div(n, 33) * 8 + _div(_mod(n, 33) + 3, 4)
    if _mod(jump, 33) == 4 and jump - n == 4:
        leap_j += 1
    leap_g = _div(gy, 4) - _div((_div(gy, 100) + 1) * 3, 4) - 150
    march = 20 + leap_j - leap_g
    if jump - n < 6:
        n = n - jump + _div(jump + 4, 33) * 33
    leap = _mod(_mod(n + 1, 33) - 1, 4)
    if leap == -1:
        leap = 4
    return leap, gy, march


def is_jalali_leap(jy: int) -> bool:
    return _jal_cal(jy)[0] == 0


def _g2d(gy, gm, gd):
    d = (_div((gy + _div(gm - 8, 6) + 100100) * 1461, 4)
         + _div(153 * _mod(gm + 9, 12) + 2, 5) + gd - 34840408)
    return d - _div(_div(gy + 100100 + _div(gm - 8, 6), 100) * 3, 4) + 752


def _d2g(jdn):
    j = 4 * jdn + 139361631
    j = j + _div(_div(4 * jdn + 183187720, 146097) * 3, 4) * 4 - 3908
    i = _div(_mod(j, 1461), 4) * 5 + 308
    gd = _div(_mod(i, 153), 5) + 1
    gm = _mod(_div(i, 153), 12) + 1
    gy = _div(j, 1461) - 100100 + _div(8 - gm, 6)
    return gy, gm, gd


def _j2d(jy, jm, jd):
    _, gy, march = _jal_cal(jy)
    return _g2d(gy, 3, march) + (jm - 1) * 31 - _div(jm, 7) * (jm - 7) + jd - 1


def _d2j(jdn):
    gy = _d2g(jdn)[0]
    jy = gy - 621
    leap, _, march = _jal_cal(jy)
    k = jdn - _g2d(gy, 3, march)
    if k >= 0:
        if k <= 185:
            return jy, 1 + _div(k, 31), _mod(k, 31) + 1
        k -= 186
    else:
        jy -= 1
        k += 179
        if leap == 1:
            k += 1
    return jy, 7 + _div(k, 30), _mod(k, 30) + 1


def jalali_to_gregorian(jy, jm, jd):
    return _d2g(_j2d(jy, jm, jd))


def gregorian_to_jalali(gy, gm, gd):
    return _d2j(_g2d(gy, gm, gd))


def jalali_add_days(jy, jm, jd, days):
    return _d2j(_j2d(jy, jm, jd) + days)


def jalali_month_length(jy, jm):
    if jm <= 6:
        return 31
    if jm <= 11:
        return 30
    return 30 if is_jalali_leap(jy) else 29


# ---------------------------------------------------------------- parsing

def _month_index(token, names):
    token = normalize(token)
    for i, name in enumerate(names):
        if normalize(name) == token:
            return i + 1
    return None


def parse_jalali(text):
    """Return the last (y, m, d) Jalali date found in text, or None.

    Accepts ``1403/01/06``, ``1403-1-6`` and ``۶ فروردین ۱۴۰۳`` (numeric day only).
    """
    return _parse(text, JALALI_MONTHS, ymd_first=True)


def parse_gregorian(text):
    """Return the last (y, m, d) Gregorian date found in text, or None.

    Accepts ``2025-03-21``, ``21 March 2025``, ``March 21, 2025`` and ``۲۱ مارس ۲۰۲۵``.
    """
    found = _parse(text, GREGORIAN_MONTHS, ymd_first=True)
    fa = _parse(text, GREGORIAN_MONTHS_FA, ymd_first=True)
    return found or fa


def _parse(text, names, ymd_first):
    t = to_ascii_digits(normalize(text))
    candidates = []
    for m in re.finditer(r"(\d{3,4})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{1,2})", to_ascii_digits(text)):
        candidates.append((m.start(), (int(m.group(1)), int(m.group(2)), int(m.group(3)))))
    month_alt = "|".join(re.escape(normalize(n)) for n in names)
    for m in re.finditer(r"(\d{1,2})(?:st|nd|rd|th|م|ام)?\s+(" + month_alt + r")\s+(?:سال\s+|ماه\s+)?(\d{3,4})", t):
        candidates.append((m.start(), (int(m.group(3)), _month_index(m.group(2), names), int(m.group(1)))))
    for m in re.finditer(r"(" + month_alt + r")\s+(\d{1,2})(?:st|nd|rd|th)?\s+(\d{3,4})", t):
        candidates.append((m.start(), (int(m.group(3)), _month_index(m.group(1), names), int(m.group(2)))))
    if not candidates:
        return None
    return max(candidates)[1]


def parse_date_spec(spec):
    """Parse a gold date written as ``YYYY-MM-DD``."""
    y, m, d = (int(x) for x in to_ascii_digits(spec).split("-"))
    return y, m, d
