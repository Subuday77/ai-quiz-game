import unittest
from random import Random
from uuid import UUID

from platinum_helper.core import (
    MAX_CONSULTATIONS, TOPICS, HelperService, generate_profile, CATEGORY_TOPICS,
    choose_delivery_style, choose_presentation_style, outcome_weights,
    PROFESSIONAL_RELATED_SKILLS, PROFESSIONAL_SKILLS, choose_outcome,
)


class PlatinumHelperTests(unittest.TestCase):
    def setUp(self):
        self.profile = generate_profile(rng=Random(12))
        self.service = HelperService(self.profile)
        self.options = ("A", "B", "C", "D")

    def test_speech_style_is_stable_and_independent_of_outcome(self):
        confident_id, tentative_id = UUID(int=0), UUID(int=1)
        self.assertEqual(choose_delivery_style(confident_id), "CONFIDENT")
        self.assertEqual(choose_delivery_style(tentative_id), "TENTATIVE")
        for _ in range(3):
            self.assertEqual(choose_delivery_style(confident_id), "CONFIDENT")
            self.assertEqual(choose_delivery_style(tentative_id), "TENTATIVE")

    def test_presentation_style_stable_independent_and_all_three_reachable(self):
        from uuid import UUID
        selected = {choose_presentation_style(UUID(int=i)) for i in range(300)}
        self.assertEqual(selected, {"DIRECT", "PERSONAL", "CASUAL"})
        for i in range(100):
            call_id = UUID(int=i)
            first = choose_presentation_style(call_id)
            self.assertEqual(first, choose_presentation_style(call_id))
            self.assertIn(first, selected)
        # There is no outcome/correctness parameter: UUID is independent of
        # KNOW/MISTAKEN and based only on the saved consultation identity.

    def test_profile_is_stable_and_private(self):
        self.assertEqual(len(self.profile.skills), 20)
        self.assertEqual(set(self.profile.skills), set(TOPICS))
        self.assertEqual(self.profile.skills["Engineering"], "HIGH")
        self.assertEqual(self.profile.skills["Chemistry"], "HIGH")
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

    def test_related_skills_never_roll_low_and_keep_extra_high_budget(self):
        for profession, related in PROFESSIONAL_RELATED_SKILLS.items():
            for seed in range(30):
                profile = generate_profile(profession, rng=Random(seed))
                self.assertTrue(all(profile.skills[t] == "HIGH"
                                    for t in PROFESSIONAL_SKILLS[profession]))
                self.assertTrue(all(profile.skills[t] in ("MEDIUM", "HIGH")
                                    for t in related), (profession, seed))
                extra = sum(level == "HIGH" for topic, level in profile.skills.items()
                            if topic not in PROFESSIONAL_SKILLS[profession])
                lo, hi = ((0, 1) if profile.erudition <= 20 else
                          (2, 3) if profile.erudition <= 40 else
                          (4, 6) if profile.erudition <= 60 else
                          (7, 9) if profile.erudition <= 80 else (10, 12))
                self.assertTrue(lo <= extra <= hi)

    def test_historian_understands_related_mythology_and_culture(self):
        for seed in range(100):
            profile = generate_profile("Historian", rng=Random(seed))
            self.assertEqual(profile.skills["History"], "HIGH")
            for area in ("Mythology", "Culture"):
                self.assertNotEqual(profile.skills[area], "LOW")

    def test_outcome_probability_schedule(self):
        for skill, decay in (("HIGH", 0.5), ("MEDIUM", 2.5), ("LOW", 5.0)):
            for number in (1, 5, 10, 15):
                know, mistaken, unsure = outcome_weights(skill, number)
                self.assertAlmostEqual(know, 100 - number * decay)
                self.assertAlmostEqual(mistaken, (100 - know) / 3)
                self.assertAlmostEqual(unsure, (100 - know) * 2 / 3)
                self.assertAlmostEqual(know + mistaken + unsure, 100)
        self.assertEqual(outcome_weights("LOW", 15), (25, 25, 50))
        for n in (0, 16):
            with self.assertRaises(ValueError):
                outcome_weights("HIGH", n)

    def test_outcome_draw_respects_dynamic_cutoffs(self):
        class FixedRandom:
            def __init__(self, value):
                self.value = value
            def random(self):
                return self.value
        self.assertEqual(choose_outcome("LOW", FixedRandom(0.24), 15), "KNOW")
        self.assertEqual(choose_outcome("LOW", FixedRandom(0.30), 15), "MISTAKEN")
        self.assertEqual(choose_outcome("LOW", FixedRandom(0.75), 15), "UNSURE")

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
        self.assertTrue(all(p.skills["Engineering"] == "HIGH" and p.skills["Chemistry"] == "HIGH" for p in profiles))
        avg_erudition = sum(p.erudition for p in profiles) / len(profiles)
        self.assertTrue(47 < avg_erudition < 53)
        for profile in profiles:
            extra = sum(s == "HIGH" for t, s in profile.skills.items() if t not in ("Engineering", "Chemistry"))
            expected = (0, 1) if profile.erudition <= 20 else (
                (2, 3) if profile.erudition <= 40 else
                (4, 6) if profile.erudition <= 60 else
                (7, 9) if profile.erudition <= 80 else (10, 12)
            )
            self.assertTrue(expected[0] <= extra <= expected[1])

    def test_every_competence_is_playable_and_fire_safety_is_not_hidden(self):
        self.assertNotIn("Fire Safety", TOPICS)
        self.assertEqual(set(TOPICS), set(CATEGORY_TOPICS.values()))

    def test_new_character_has_stable_localizable_tagline_fields(self):
        p = generate_profile("Historian", name="Jean Dupont",
                             gender="male", nationality="France", rng=Random(7))
        self.assertEqual(p.name, "Jean Dupont")
        self.assertEqual(p.gender, "male")
        self.assertEqual(p.nationality, "France")
        self.assertIn(p.tagline_high, p.featured_high)
        if p.featured_low:
            self.assertIn(p.tagline_low, p.featured_low)
        else:
            self.assertEqual(p.tagline_low, "")
        self.assertIn(p.tagline_id, (1, 2, 3, 4))
        self.assertFalse(p.tagline, "Do not freeze an English template in the database")

    def test_conflicting_idempotency_key_rejected(self):
        self.service.start("same", "q1", "History", self.options, "C")
        with self.assertRaises(ValueError):
            self.service.start("same", "q2", "History", self.options, "C")


if __name__ == "__main__":
    unittest.main()
