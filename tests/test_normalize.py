import unittest

from farhang.normalize import char_similarity, contains_phrase, normalize, strict_clean


class NormalizeTest(unittest.TestCase):
    def test_arabic_letters_fold_to_persian(self):
        self.assertEqual(normalize("كتاب علي"), normalize("کتاب علی"))

    def test_zwnj_and_space_are_equivalent(self):
        self.assertEqual(normalize("می‌خواهم"), normalize("می خواهم"))

    def test_digits_fold_to_ascii(self):
        self.assertEqual(normalize("۱۴۰۳ ١٢"), "1403 12")

    def test_diacritics_and_ezafe_hamza_removed(self):
        self.assertEqual(normalize("خانهٔ"), "خانه")
        self.assertEqual(normalize("خانۀ"), "خانه")
        self.assertEqual(normalize("کِتابْ"), "کتاب")

    def test_strict_clean_keeps_character_identity(self):
        self.assertNotEqual(strict_clean("می‌خواهم"), strict_clean("می خواهم"))
        self.assertNotEqual(strict_clean("علي"), strict_clean("علی"))
        self.assertEqual(strict_clean("«سلام»"), "سلام")

    def test_contains_phrase_respects_word_boundaries(self):
        self.assertTrue(contains_phrase("جواب: نادرست", "نادرست"))
        self.assertFalse(contains_phrase("نادرست", "درست"))
        self.assertTrue(contains_phrase("سرتون درد نکنه!", "سرتون درد نکنه"))

    def test_char_similarity(self):
        self.assertEqual(char_similarity("از جدایی‌ها حکایت می‌کند", "از جدایی ها حکایت میکند"), 1.0)
        self.assertLess(char_similarity("از جدایی‌ها شکایت می‌کند", "از جدایی‌ها حکایت می‌کند"), 1.0)


if __name__ == "__main__":
    unittest.main()
