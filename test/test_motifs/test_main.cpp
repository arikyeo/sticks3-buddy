// Unit tests for audio/motifs.h — the sound-vocabulary table (motif data)
// and its pure non-blocking scheduler (MotifPlayer). motifs.h has zero
// Arduino dependency (stdint.h only, see its own header comment), so this
// runs natively under [env:native] — no stubs, no -DBUDDY_NATIVE_TEST
// branching needed.
#include <unity.h>
#include "audio/motifs.h"

void setUp() {}
void tearDown() {}

// --- table sanity ------------------------------------------------------------

void test_every_motif_id_has_at_least_one_note() {
  for (uint8_t id = 0; id < MOTIF_COUNT; id++) {
    TEST_ASSERT_TRUE(MOTIFS[id].len > 0);
    TEST_ASSERT_TRUE(MOTIFS[id].n != nullptr);
  }
}

void test_every_note_has_a_positive_frequency_and_duration() {
  for (uint8_t id = 0; id < MOTIF_COUNT; id++) {
    const Motif& m = MOTIFS[id];
    for (uint8_t i = 0; i < m.len; i++) {
      TEST_ASSERT_TRUE(m.n[i].freq > 0);
      TEST_ASSERT_TRUE(m.n[i].durMs > 0);
    }
  }
}

// The last note is the one after which motifTick() drops the player idle
// (see below) — its gapMs is dead time nobody ever waits on, so by
// convention it's 0. Table hygiene, not something the scheduler enforces.
void test_last_note_of_each_motif_has_no_trailing_gap() {
  for (uint8_t id = 0; id < MOTIF_COUNT; id++) {
    const Motif& m = MOTIFS[id];
    TEST_ASSERT_EQUAL_UINT16(0, m.n[m.len - 1].gapMs);
  }
}

// MOTIF_OTA_OK is the one deliberate alias (celebrate reuse — see the
// comment on the MOTIFS table in motifs.h): every other id must point at
// its own distinct note sequence, or the vocabulary stops being
// distinguishable, which is the entire point of this table.
void test_motifs_are_distinct_except_the_documented_ota_ok_alias() {
  for (uint8_t a = 0; a < MOTIF_COUNT; a++) {
    for (uint8_t b = a + 1; b < MOTIF_COUNT; b++) {
      bool aliasPair = (a == MOTIF_DONE && b == MOTIF_OTA_OK) ||
                        (a == MOTIF_OTA_OK && b == MOTIF_DONE);
      bool samePtr = MOTIFS[a].n == MOTIFS[b].n;
      TEST_ASSERT_TRUE(samePtr == aliasPair);
    }
  }
}

// The UX spec calls out PASSKEY (1800Hz) as needing to "verify distinct" —
// confirm no other motif's notes share its frequency.
void test_passkey_frequency_is_unique_to_passkey() {
  uint16_t pkFreq = MOTIFS[MOTIF_PASSKEY].n[0].freq;
  for (uint8_t id = 0; id < MOTIF_COUNT; id++) {
    if (id == MOTIF_PASSKEY) continue;
    const Motif& m = MOTIFS[id];
    for (uint8_t i = 0; i < m.len; i++) {
      TEST_ASSERT_TRUE(m.n[i].freq != pkFreq);
    }
  }
}

// --- scheduler -----------------------------------------------------------------

void test_idle_player_ticks_to_nothing() {
  MotifPlayer p; motifReset(&p);
  TEST_ASSERT_FALSE(motifActive(&p));
  TEST_ASSERT_TRUE(motifTick(&p, 12345) == nullptr);
}

void test_single_note_motif_fires_once_then_goes_idle() {
  MotifPlayer p; motifReset(&p);
  motifStart(&p, MOTIF_APPROVE, 1000);
  TEST_ASSERT_TRUE(motifActive(&p));
  const MotifNote* n = motifTick(&p, 1000);
  TEST_ASSERT_TRUE(n != nullptr);
  TEST_ASSERT_EQUAL_UINT16(2400, n->freq);
  TEST_ASSERT_FALSE(motifActive(&p));   // single-note motif: idle right after
}

void test_multi_note_motif_plays_in_order_then_goes_idle() {
  MotifPlayer p; motifReset(&p);
  motifStart(&p, MOTIF_PROMPT, 0);           // 1200/1600/2000, 70ms+20ms gap x2
  const MotifNote* n0 = motifTick(&p, 0);
  TEST_ASSERT_TRUE(n0 != nullptr);
  TEST_ASSERT_EQUAL_UINT16(1200, n0->freq);
  TEST_ASSERT_TRUE(motifActive(&p));
  TEST_ASSERT_TRUE(motifTick(&p, 1) == nullptr);   // note 2 not due yet
  const MotifNote* n1 = motifTick(&p, 90);         // 70 + 20
  TEST_ASSERT_TRUE(n1 != nullptr);
  TEST_ASSERT_EQUAL_UINT16(1600, n1->freq);
  TEST_ASSERT_TRUE(motifActive(&p));
  const MotifNote* n2 = motifTick(&p, 180);        // 90 + 70 + 20
  TEST_ASSERT_TRUE(n2 != nullptr);
  TEST_ASSERT_EQUAL_UINT16(2000, n2->freq);
  TEST_ASSERT_FALSE(motifActive(&p));              // last note sent -> idle
}

void test_start_with_out_of_range_id_resets_to_idle() {
  MotifPlayer p; motifReset(&p);
  motifStart(&p, MOTIF_COUNT, 500);   // one past the last valid id
  TEST_ASSERT_FALSE(motifActive(&p));
}

void test_late_start_interrupts_a_motif_already_in_progress() {
  MotifPlayer p; motifReset(&p);
  motifStart(&p, MOTIF_PROMPT, 0);
  motifTick(&p, 0);                    // consumes PROMPT's note 1
  motifStart(&p, MOTIF_DENY, 50);      // a new event pre-empts it
  const MotifNote* n = motifTick(&p, 50);
  TEST_ASSERT_TRUE(n != nullptr);
  TEST_ASSERT_EQUAL_UINT16(600, n->freq);   // DENY's note, not PROMPT's note 2
  TEST_ASSERT_FALSE(motifActive(&p));       // DENY is single-note
}

void test_survives_millis_wrap() {
  MotifPlayer p; motifReset(&p);
  uint32_t start = (uint32_t)0u - 10;       // 10ms before the wrap
  motifStart(&p, MOTIF_DONE, start);        // 2000 (100+30 gap), 2800
  const MotifNote* n0 = motifTick(&p, start);
  TEST_ASSERT_TRUE(n0 != nullptr);
  TEST_ASSERT_EQUAL_UINT16(2000, n0->freq);
  uint32_t now1 = start + 5;                // not due yet (pre-wrap still)
  TEST_ASSERT_TRUE(motifTick(&p, now1) == nullptr);
  uint32_t now2 = start + 130;              // wraps past 0; exactly due
  const MotifNote* n1 = motifTick(&p, now2);
  TEST_ASSERT_TRUE(n1 != nullptr);
  TEST_ASSERT_EQUAL_UINT16(2800, n1->freq);
  TEST_ASSERT_FALSE(motifActive(&p));
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_every_motif_id_has_at_least_one_note);
  RUN_TEST(test_every_note_has_a_positive_frequency_and_duration);
  RUN_TEST(test_last_note_of_each_motif_has_no_trailing_gap);
  RUN_TEST(test_motifs_are_distinct_except_the_documented_ota_ok_alias);
  RUN_TEST(test_passkey_frequency_is_unique_to_passkey);
  RUN_TEST(test_idle_player_ticks_to_nothing);
  RUN_TEST(test_single_note_motif_fires_once_then_goes_idle);
  RUN_TEST(test_multi_note_motif_plays_in_order_then_goes_idle);
  RUN_TEST(test_start_with_out_of_range_id_resets_to_idle);
  RUN_TEST(test_late_start_interrupts_a_motif_already_in_progress);
  RUN_TEST(test_survives_millis_wrap);
  return UNITY_END();
}
