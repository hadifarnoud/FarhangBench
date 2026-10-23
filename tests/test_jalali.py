import unittest

from farhang.jalali import (gregorian_to_jalali, is_jalali_leap, jalali_add_days, jalali_month_length,
                            jalali_to_gregorian, parse_gregorian, parse_jalali)

# (Jalali, Gregorian) pairs from the official Iranian calendar.
KNOWN = [
    ((1357, 11, 22), (1979, 2, 11)),
    ((1403, 1, 1), (2024, 3, 20)),
    ((1403, 9, 30), (2024, 12, 20)),
    ((1403, 12, 30), (2025, 3, 20)),
    ((1404, 1, 1), (2025, 3, 21)),
    ((1405, 8, 1), (2026, 10, 23)),
]


class JalaliTest(unittest.TestCase):
    def test_known_dates_both_directions(self):
        for j, g in KNOWN:
            self.assertEqual(jalali_to_gregorian(*j), g)
            self.assertEqual(gregorian_to_jalali(*g), j)

    def test_leap_years(self):
        self.assertTrue(is_jalali_leap(1399))
        self.assertTrue(is_jalali_leap(1403))
        self.assertFalse(is_jalali_leap(1402))
        self.assertEqual(jalali_month_length(1402, 12), 29)
        self.assertEqual(jalali_month_length(1403, 12), 30)

    def test_add_days_crosses_new_year(self):
        self.assertEqual(jalali_add_days(1402, 12, 25, 10), (1403, 1, 6))

    def test_round_trip_every_day_for_a_decade(self):
        y, m, d = 1400, 1, 1
        for _ in range(3653):
            self.assertEqual(gregorian_to_jalali(*jalali_to_gregorian(y, m, d)), (y, m, d))
            y, m, d = jalali_add_days(y, m, d, 1)

    def test_parsing(self):
        self.assertEqual(parse_jalali("پاسخ: ۶ فروردین ۱۴۰۳"), (1403, 1, 6))
        self.assertEqual(parse_jalali("۱۴۰۳/۰۱/۰۶"), (1403, 1, 6))
        self.assertEqual(parse_gregorian("It was March 21, 2025."), (2025, 3, 21))
        self.assertEqual(parse_gregorian("21 March 2025"), (2025, 3, 21))
        self.assertEqual(parse_gregorian("۲۱ مارس ۲۰۲۵"), (2025, 3, 21))
        self.assertIsNone(parse_jalali("نمی‌دانم"))


if __name__ == "__main__":
    unittest.main()
