// Regression suite for the live 2026-07-11 pairing-window defect: after a
// successful "add host..." flow the hosts list contained ONLY the newly
// paired host — the first host's registry entry appeared to vanish, while
// both Bluedroid bonds survived (so the next boot's reconcile re-adopted
// the "missing" host as Host-N and masked the corruption).
//
// Mechanism: during the 60s window the evicted host auto-reconnects every
// ~7s; each retry is gate-rejected, and the rejected link's lifecycle
// events clobbered the shared link state, so hostGlueOnHello applied the
// NEW host's hello through a stale cached slot — renaming the FIRST host's
// record in place instead of touching the new host's own record.
//
// The registry-level invariant under test: a hello may only ever modify
// the record whose bda matches the link it arrived on (hostTabHelloRoute),
// and adopting/helloing a second host never disturbs the first record.
#include <unity.h>
#include <string.h>
#include "host_registry.h"

void setUp() {}
void tearDown() {}

static void bdaSet(uint8_t out[6], uint8_t seed) {
  for (int i = 0; i < 6; i++) out[i] = (uint8_t)(seed + i);
}

// The live incident, replayed at table level: Windows host bonded + named;
// pairing window opens; the Mac pairs and its hello arrives while the
// glue's cached slot still pointed at the Windows record. Routing by bda
// must append the Mac and leave the Windows record untouched.
void test_adopt_second_host_preserves_first() {
  HostTable t;
  hostTabInit(&t);
  uint8_t win[6], mac[6];
  bdaSet(win, 0x40);
  bdaSet(mac, 0x80);

  // Initial out-of-box pairing: Windows adopted, hello names it.
  TEST_ASSERT_EQUAL_INT(0, hostTabAdopt(&t, win));
  TEST_ASSERT_EQUAL_INT(0, hostTabHelloRoute(&t, win, "w1w1w1w1", "ARIK-PC",
                                             "claude"));
  TEST_ASSERT_EQUAL_UINT(1, t.count);

  // Pairing-window flow: the Mac's hello is keyed by ITS bda — even though
  // the glue's cached slot was stale (still 0), bda routing adopts a fresh
  // record instead of renaming the Windows one.
  TEST_ASSERT_EQUAL_INT(1, hostTabHelloRoute(&t, mac, "m1m1m1m1",
                                             "Arik's MacBook", "claude"));
  TEST_ASSERT_EQUAL_UINT(2, t.count);

  // First host fully preserved: slot, bda, name, hostId.
  TEST_ASSERT_EQUAL_INT(0, hostTabFindBda(&t, win));
  TEST_ASSERT_EQUAL_STRING("ARIK-PC", t.h[0].name);
  TEST_ASSERT_EQUAL_STRING("w1w1w1w1", t.h[0].hostId);
  // Second host is its own record.
  TEST_ASSERT_EQUAL_INT(1, hostTabFindBda(&t, mac));
  TEST_ASSERT_EQUAL_STRING("Arik's MacBook", t.h[1].name);
  TEST_ASSERT_EQUAL_STRING("m1m1m1m1", t.h[1].hostId);
}

// Known peer: route updates the matching record in place — no duplicate.
void test_hello_route_known_bda_updates_in_place() {
  HostTable t;
  hostTabInit(&t);
  uint8_t a[6];
  bdaSet(a, 0x10);
  hostTabAdopt(&t, a);
  TEST_ASSERT_EQUAL_INT(0, hostTabHelloRoute(&t, a, "aaaaaaaa", "First",
                                             "claude"));
  TEST_ASSERT_EQUAL_INT(0, hostTabHelloRoute(&t, a, "aaaaaaaa", "Renamed",
                                             "codex"));
  TEST_ASSERT_EQUAL_UINT(1, t.count);
  TEST_ASSERT_EQUAL_STRING("Renamed", t.h[0].name);
  TEST_ASSERT_EQUAL_STRING("codex", t.h[0].app);
}

// Unknown peer on an encrypted link = bonded but not yet in the table (its
// adopt was missed — e.g. the secure transition got clobbered): the hello
// itself adopts, then applies the fields, and the fresh record carries the
// newest LRU stamp.
void test_hello_route_unknown_bda_adopts() {
  HostTable t;
  hostTabInit(&t);
  uint8_t a[6], b[6];
  bdaSet(a, 0x10);
  bdaSet(b, 0x20);
  hostTabAdopt(&t, a);
  TEST_ASSERT_EQUAL_INT(1, hostTabHelloRoute(&t, b, "bbbbbbbb", "NewBox",
                                             "claude"));
  TEST_ASSERT_EQUAL_UINT(2, t.count);
  TEST_ASSERT_EQUAL_STRING("NewBox", t.h[1].name);
  TEST_ASSERT_EQUAL_INT(0, hostTabLru(&t));   // newcomer is NOT the victim
}

// Full table: route must NOT evict or corrupt — it reports -1 and leaves
// every record byte-identical (boot reconcile owns capacity truth; a hello
// never evicts).
void test_hello_route_full_table_no_write() {
  HostTable t;
  hostTabInit(&t);
  uint8_t a[6];
  for (uint8_t i = 0; i < BUDDY_MAX_HOSTS; i++) {
    bdaSet(a, (uint8_t)(i * 8));
    hostTabAdopt(&t, a);
  }
  HostTable before;
  memcpy(&before, &t, sizeof(t));
  uint8_t x[6];
  bdaSet(x, 0xE0);
  TEST_ASSERT_EQUAL_INT(-1, hostTabHelloRoute(&t, x, "xxxxxxxx", "Late",
                                              "claude"));
  TEST_ASSERT_EQUAL_UINT(BUDDY_MAX_HOSTS, t.count);
  TEST_ASSERT_EQUAL_MEMORY(&before, &t, sizeof(t));
}

// First pairing of a privacy-enabled host (macOS, modern Windows): the
// link connects under an unresolvable RPA, so the record is adopted — and
// possibly hello-named — under a one-time address, while the Bluedroid
// bond store keys the bond by the IDENTITY address exchanged during SMP.
// The registry re-keys the SAME record once the identity is known: no
// duplicate row, the hello name survives, and both a same-session
// identity-resolved reconnect and the next boot's reconcile against the
// identity-keyed bond store find the record. (Without the re-key, the
// reconcile dropped the RPA record — "bond gone" — and re-adopted the
// identity as a placeholder Host-N, losing the name.)
void test_rekey_rpa_record_to_identity_preserves_hello() {
  HostTable t;
  hostTabInit(&t);
  uint8_t rpa[6], identity[6];
  bdaSet(rpa, 0x60);
  bdaSet(identity, 0xC0);

  // Adopt at the secure transition under the connect-time RPA, then the
  // hello names the record over the encrypted link.
  int i = hostTabAdopt(&t, rpa);
  TEST_ASSERT_EQUAL_INT(0, i);
  TEST_ASSERT_EQUAL_INT(0, hostTabHelloRoute(&t, rpa, "m1m1m1m1",
                                             "Arik's MacBook", "claude"));

  // Identity learned (auth-complete): re-key instead of duplicating.
  TEST_ASSERT_TRUE(hostTabRekey(&t, i, identity));
  TEST_ASSERT_EQUAL_UINT(1, t.count);

  // Identity-resolved reconnect finds the named record; the dead RPA
  // matches nothing.
  TEST_ASSERT_EQUAL_INT(0, hostTabFindBda(&t, identity));
  TEST_ASSERT_EQUAL_INT(-1, hostTabFindBda(&t, rpa));
  TEST_ASSERT_EQUAL_STRING("Arik's MacBook", t.h[0].name);
  TEST_ASSERT_EQUAL_STRING("m1m1m1m1", t.h[0].hostId);

  // A later hello routed by the identity updates in place — count stays 1.
  TEST_ASSERT_EQUAL_INT(0, hostTabHelloRoute(&t, identity, "m1m1m1m1",
                                             "Arik's MacBook", "codex"));
  TEST_ASSERT_EQUAL_UINT(1, t.count);
  TEST_ASSERT_EQUAL_STRING("codex", t.h[0].app);
}

// The pre-fix failure, encoded as a counter-example: WITHOUT the re-key, a
// same-session identity-resolved reconnect misses the RPA-keyed record and
// adopts a duplicate row — two records for one machine, and the bond-store
// key (identity) points at the nameless one.
void test_no_rekey_identity_reconnect_would_duplicate() {
  HostTable t;
  hostTabInit(&t);
  uint8_t rpa[6], identity[6];
  bdaSet(rpa, 0x60);
  bdaSet(identity, 0xC0);
  hostTabAdopt(&t, rpa);
  hostTabHello(&t, 0, "m1m1m1m1", "Arik's MacBook", "claude");
  // Reconnect arrives identity-resolved; find misses, adopt duplicates.
  TEST_ASSERT_EQUAL_INT(-1, hostTabFindBda(&t, identity));
  TEST_ASSERT_EQUAL_INT(1, hostTabAdopt(&t, identity));
  TEST_ASSERT_EQUAL_UINT(2, t.count);                       // one machine, two rows
  TEST_ASSERT_EQUAL_STRING("Host-2", t.h[1].name);          // and the bond key
                                                            // owns the nameless one
}

// The defect, encoded as a counter-example: applying a hello through a
// stale slot index (what hostGlueOnHello used to do) writes the second
// host's identity into the first host's record — one row, new host's name,
// old host's bda. Documents WHY routing must key on bda.
void test_stale_slot_hello_would_corrupt_first_host() {
  HostTable t;
  hostTabInit(&t);
  uint8_t win[6], mac[6];
  bdaSet(win, 0x40);
  bdaSet(mac, 0x80);
  hostTabAdopt(&t, win);
  hostTabHello(&t, 0, "w1w1w1w1", "ARIK-PC", "claude");
  // Stale-slot write (the bug's effect): Mac fields land in the Windows
  // record.
  hostTabHello(&t, 0, "m1m1m1m1", "Arik's MacBook", "claude");
  TEST_ASSERT_EQUAL_UINT(1, t.count);                        // no second row
  TEST_ASSERT_EQUAL_STRING("Arik's MacBook", t.h[0].name);   // first host "gone"
  TEST_ASSERT_EQUAL_INT(0, hostTabFindBda(&t, win));         // but bda is Windows'
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_adopt_second_host_preserves_first);
  RUN_TEST(test_hello_route_known_bda_updates_in_place);
  RUN_TEST(test_hello_route_unknown_bda_adopts);
  RUN_TEST(test_hello_route_full_table_no_write);
  RUN_TEST(test_rekey_rpa_record_to_identity_preserves_hello);
  RUN_TEST(test_no_rekey_identity_reconnect_would_duplicate);
  RUN_TEST(test_stale_slot_hello_would_corrupt_first_host);
  return UNITY_END();
}
