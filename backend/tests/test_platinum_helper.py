import unittest
from random import Random

from platinum_helper.core import (
    MAX_CONSULTATIONS, TOPICS, HelperService, generate_profile,
)


class PlatinumHelperTests(unittest.TestCase):
    def setUp(self):
        self.profile = generate_profile(rng=Random(12))
        self.service = HelperService(self.profile)
        self.options = ("A", "B", "C", "D")

    def test_profile_is_stable_and_private(self):
        self.assertEqual(len(self.profile.skills), 20)
        self.assertEqual(set(self.profile.skills), set(TOPICS))
        self.assertEqual(self.profile.skills["Fire Safety"], "HIGH")
        self.assertTrue(0 <= self.profile.erudition <= 100)
        card = self.profile.public_card(3)
        self.assertNotIn("skills", card)
        self.assertTrue(set(card["strong_topics"]) <= {
            t for t, s in self.profile.skills.items() if s == "HIGH"
        })
        self.assertTrue(set(card["weak_topics"]) <= {
            t for t, s in self.profile.skills.items() if s == "LOW"
        })
        self.assertLessEqual(len(card["strong_topics"]), 3)
        self.assertLessEqual(len(card["weak_topics"]), 3)
        self.assertEqual(card, self.profile.public_card(3))

    def test_retry_keeps_choice_and_does_not_charge_twice(self):
        first = self.service.start("call-1", "q-1", "History", self.options, "C", rng=Random(4))
        again = self.service.start("call-1", "q-1", "History", self.options, "C", rng=Random(987))
        self.assertIs(first, again)
        self.service.complete("call-1", "", verified=False)
        self.assertEqual(self.service.remaining, 3)
        self.service.complete("call-1", "Answer", verified=True)
        self.service.complete("call-1", "Another answer", verified=True)
        self.assertEqual(self.service.remaining, 2)
        self.assertEqual(first.response, "Answer")

    def test_limit_and_wrong_key_is_never_correct(self):
        for i in range(MAX_CONSULTATIONS):
            request = self.service.start(f"call-{i}", f"q-{i}", "Comics",
                                         self.options, "C", rng=Random(i))
            if request.outcome == "MISTAKEN":
                self.assertIn(request.suggested_answer, ("A", "B", "D"))
            self.service.complete(request.request_id, "OK", verified=True)
        self.assertEqual(self.service.remaining, 0)
        with self.assertRaises(ValueError):
            self.service.start("call-4", "q-4", "Comics", self.options, "C")

    def test_gaussian_coverage_and_professional_guarantees(self):
        rng = Random(1977)
        profiles = [generate_profile(rng=rng) for _ in range(1000)]
        self.assertTrue(all(p.skills["Fire Safety"] == "HIGH" for p in profiles))
        avg_erudition = sum(p.erudition for p in profiles) / len(profiles)
        self.assertTrue(47 < avg_erudition < 53)
        for profile in profiles:
            extra = sum(s == "HIGH" for t, s in profile.skills.items() if t != "Fire Safety")
            expected = (0, 1) if profile.erudition <= 20 else (
                (2, 3) if profile.erudition <= 40 else
                (4, 6) if profile.erudition <= 60 else
                (7, 9) if profile.erudition <= 80 else (10, 12)
            )
            self.assertTrue(expected[0] <= extra <= expected[1])

    def test_conflicting_idempotency_key_rejected(self):
        self.service.start("same", "q1", "History", self.options, "C")
        with self.assertRaises(ValueError):
            self.service.start("same", "q2", "History", self.options, "C")


if __name__ == "__main__":
    unittest.main()
