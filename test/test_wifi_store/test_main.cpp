// Unit tests for logic/wifi_store_logic.h — the pure multi-network WiFi
// list model: upsert dedup (replace-in-place, position kept), append order,
// reject-at-cap with NO eviction (stored networks must never silently
// drop), remove compaction, clear scrubbing, the legacy single-cred
// migration shape (upsert into an empty list lands in slot 0), and the
// names-only list formatter (a pass must never appear in any output).
#include <unity.h>
#include <string.h>
#include "logic/wifi_store_logic.h"

void setUp() {}
void tearDown() {}

// Fresh list per test, cap selectable so edges are cheap to reach.
static WifiCred g_slots[WIFI_MAX];
static WifiList mk(uint16_t cap = WIFI_MAX) {
  memset(g_slots, 0, sizeof(g_slots));
  WifiList l = { g_slots, 0, cap };
  return l;
}

void test_upsert_appends_in_order_and_returns_count() {
  WifiList l = mk();
  TEST_ASSERT_EQUAL_INT16(1, wifiListUpsert(&l, "alpha", "pw-a"));
  TEST_ASSERT_EQUAL_INT16(2, wifiListUpsert(&l, "beta", "pw-b"));
  TEST_ASSERT_EQUAL_INT16(3, wifiListUpsert(&l, "gamma", ""));
  TEST_ASSERT_EQUAL_UINT16(3, l.n);
  TEST_ASSERT_EQUAL_STRING("alpha", l.c[0].ssid);
  TEST_ASSERT_EQUAL_STRING("beta",  l.c[1].ssid);
  TEST_ASSERT_EQUAL_STRING("gamma", l.c[2].ssid);
  TEST_ASSERT_EQUAL_STRING("pw-b",  l.c[1].pass);
  TEST_ASSERT_EQUAL_STRING("",      l.c[2].pass);   // open network
}

void test_upsert_dedups_by_ssid_and_keeps_position() {
  WifiList l = mk();
  wifiListUpsert(&l, "alpha", "old-a");
  wifiListUpsert(&l, "beta",  "old-b");
  wifiListUpsert(&l, "gamma", "old-c");
  TEST_ASSERT_EQUAL_INT16(3, wifiListUpsert(&l, "beta", "new-b"));  // count unchanged
  TEST_ASSERT_EQUAL_UINT16(3, l.n);
  TEST_ASSERT_EQUAL_STRING("beta",  l.c[1].ssid);   // position kept
  TEST_ASSERT_EQUAL_STRING("new-b", l.c[1].pass);   // pass replaced
  TEST_ASSERT_EQUAL_STRING("alpha", l.c[0].ssid);   // neighbors untouched
  TEST_ASSERT_EQUAL_STRING("gamma", l.c[2].ssid);
}

void test_upsert_validates_lengths() {
  WifiList l = mk();
  char s33[34], p64[65], s32[33], p63[64];
  memset(s33, 'a', 33); s33[33] = 0;
  memset(p64, 'p', 64); p64[64] = 0;
  memset(s32, 'b', 32); s32[32] = 0;
  memset(p63, 'q', 63); p63[63] = 0;
  TEST_ASSERT_EQUAL_INT16(WIFI_LIST_ERR, wifiListUpsert(&l, "", "x"));      // empty ssid
  TEST_ASSERT_EQUAL_INT16(WIFI_LIST_ERR, wifiListUpsert(&l, nullptr, "x")); // no ssid
  TEST_ASSERT_EQUAL_INT16(WIFI_LIST_ERR, wifiListUpsert(&l, s33, "x"));     // 33-byte ssid
  TEST_ASSERT_EQUAL_INT16(WIFI_LIST_ERR, wifiListUpsert(&l, "net", p64));   // 64-byte pass
  TEST_ASSERT_EQUAL_UINT16(0, l.n);                     // rejects left nothing behind
  TEST_ASSERT_EQUAL_INT16(1, wifiListUpsert(&l, s32, p63));    // maxima are legal
  TEST_ASSERT_EQUAL_INT16(2, wifiListUpsert(&l, "open", nullptr));  // NULL pass = ""
  TEST_ASSERT_EQUAL_STRING("", l.c[1].pass);
}

void test_full_rejects_new_but_still_replaces() {
  WifiList l = mk(3);                                   // small cap, same code path
  wifiListUpsert(&l, "a", "1");
  wifiListUpsert(&l, "b", "2");
  wifiListUpsert(&l, "c", "3");
  TEST_ASSERT_EQUAL_INT16(WIFI_LIST_FULL, wifiListUpsert(&l, "d", "4"));
  TEST_ASSERT_EQUAL_UINT16(3, l.n);
  TEST_ASSERT_EQUAL_STRING("a", l.c[0].ssid);           // nothing evicted
  TEST_ASSERT_EQUAL_STRING("c", l.c[2].ssid);
  TEST_ASSERT_EQUAL_INT16(3, wifiListUpsert(&l, "b", "2new"));  // replace still fine
  TEST_ASSERT_EQUAL_STRING("2new", l.c[1].pass);
}

void test_full_at_wifi_max() {
  WifiList l = mk();
  char ssid[33];
  for (int i = 0; i < WIFI_MAX; i++) {
    snprintf(ssid, sizeof(ssid), "net-%d", i);
    TEST_ASSERT_EQUAL_INT16(i + 1, wifiListUpsert(&l, ssid, "pw"));
  }
  TEST_ASSERT_EQUAL_UINT16(WIFI_MAX, l.n);
  TEST_ASSERT_EQUAL_INT16(WIFI_LIST_FULL, wifiListUpsert(&l, "one-more", "pw"));
  TEST_ASSERT_EQUAL_UINT16(WIFI_MAX, l.n);
  TEST_ASSERT_EQUAL_STRING("net-0", l.c[0].ssid);       // oldest survives (no LRU)
}

void test_remove_compacts_and_preserves_order() {
  WifiList l = mk();
  wifiListUpsert(&l, "a", "1");
  wifiListUpsert(&l, "b", "2");
  wifiListUpsert(&l, "c", "3");
  TEST_ASSERT_EQUAL_INT16(2, wifiListRemove(&l, "b"));
  TEST_ASSERT_EQUAL_UINT16(2, l.n);
  TEST_ASSERT_EQUAL_STRING("a", l.c[0].ssid);
  TEST_ASSERT_EQUAL_STRING("c", l.c[1].ssid);
  TEST_ASSERT_EQUAL_STRING("3", l.c[1].pass);           // record moved whole
  TEST_ASSERT_EQUAL_STRING("", l.c[2].ssid);            // vacated slot scrubbed
  TEST_ASSERT_EQUAL_STRING("", l.c[2].pass);
  TEST_ASSERT_EQUAL_INT16(WIFI_LIST_ERR, wifiListRemove(&l, "b"));   // gone is gone
  TEST_ASSERT_EQUAL_INT16(WIFI_LIST_ERR, wifiListRemove(&l, "nope"));
  TEST_ASSERT_EQUAL_UINT16(2, l.n);
}

void test_clear_empties_and_scrubs() {
  WifiList l = mk();
  wifiListUpsert(&l, "a", "s3cret");
  wifiListUpsert(&l, "b", "s3cret2");
  wifiListClear(&l);
  TEST_ASSERT_EQUAL_UINT16(0, l.n);
  TEST_ASSERT_EQUAL_STRING("", l.c[0].ssid);            // bytes gone, not just n=0
  TEST_ASSERT_EQUAL_STRING("", l.c[0].pass);
  TEST_ASSERT_EQUAL_STRING("", l.c[1].pass);
}

// The device migration path (wifiStoreInit) is exactly an upsert of the
// legacy NVS pair into a fresh list — assert it lands in slot 0 intact.
void test_migrate_from_single_lands_in_slot0() {
  WifiList l = mk();
  TEST_ASSERT_EQUAL_INT16(1, wifiListUpsert(&l, "HomeNet", "hunter22"));
  TEST_ASSERT_EQUAL_UINT16(1, l.n);
  TEST_ASSERT_EQUAL_STRING("HomeNet",  l.c[0].ssid);
  TEST_ASSERT_EQUAL_STRING("hunter22", l.c[0].pass);
  // re-running migration (keys read again before deletion stuck) is benign
  TEST_ASSERT_EQUAL_INT16(1, wifiListUpsert(&l, "HomeNet", "hunter22"));
  TEST_ASSERT_EQUAL_UINT16(1, l.n);
}

void test_list_format_emits_names_never_pass() {
  WifiList l = mk();
  // The struct genuinely carries a pass (guards against someone "fixing"
  // the leak by dropping storage instead of output)...
  TEST_ASSERT_EQUAL_UINT(64u, (unsigned)sizeof(l.c[0].pass));
  wifiListUpsert(&l, "HomeNet", "s3cret-one");
  wifiListUpsert(&l, "CafeOpen", "");
  wifiListUpsert(&l, "Office", "s3cret-two");
  char out[256];
  TEST_ASSERT_EQUAL_UINT16(3, wifiListFormatSsids(&l, out, sizeof(out)));
  // ...but the formatter emits names alone.
  TEST_ASSERT_EQUAL_STRING("\"HomeNet\",\"CafeOpen\",\"Office\"", out);
  TEST_ASSERT_NULL(strstr(out, "s3cret"));
}

void test_list_format_escapes_json() {
  WifiList l = mk();
  wifiListUpsert(&l, "say \"hi\"", "x");
  wifiListUpsert(&l, "back\\slash", "x");
  char ctl[8] = { 'a', 0x01, 'b', 0 };
  wifiListUpsert(&l, ctl, "x");
  char out[128];
  TEST_ASSERT_EQUAL_UINT16(3, wifiListFormatSsids(&l, out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("\"say \\\"hi\\\"\",\"back\\\\slash\",\"a?b\"", out);
}

void test_list_format_truncates_whole_names_only() {
  WifiList l = mk();
  wifiListUpsert(&l, "aaaaaaaa", "x");   // "aaaaaaaa" -> 10 chars quoted
  wifiListUpsert(&l, "bbbbbbbb", "x");
  wifiListUpsert(&l, "cccccccc", "x");
  char out[24];                          // fits two quoted names + comma = 21
  TEST_ASSERT_EQUAL_UINT16(2, wifiListFormatSsids(&l, out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("\"aaaaaaaa\",\"bbbbbbbb\"", out);
  char tiny[4];                          // nothing whole fits -> empty, count 0
  TEST_ASSERT_EQUAL_UINT16(0, wifiListFormatSsids(&l, tiny, sizeof(tiny)));
  TEST_ASSERT_EQUAL_STRING("", tiny);
  TEST_ASSERT_EQUAL_UINT16(0, wifiListFormatSsids(&l, nullptr, 0));   // degenerate
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_upsert_appends_in_order_and_returns_count);
  RUN_TEST(test_upsert_dedups_by_ssid_and_keeps_position);
  RUN_TEST(test_upsert_validates_lengths);
  RUN_TEST(test_full_rejects_new_but_still_replaces);
  RUN_TEST(test_full_at_wifi_max);
  RUN_TEST(test_remove_compacts_and_preserves_order);
  RUN_TEST(test_clear_empties_and_scrubs);
  RUN_TEST(test_migrate_from_single_lands_in_slot0);
  RUN_TEST(test_list_format_emits_names_never_pass);
  RUN_TEST(test_list_format_escapes_json);
  RUN_TEST(test_list_format_truncates_whole_names_only);
  return UNITY_END();
}
