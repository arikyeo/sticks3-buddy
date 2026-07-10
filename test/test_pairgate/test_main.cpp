// Unit tests for src/logic/pairing_gate_logic.h — the pure onConnect gate
// decision behind ble_bridge.cpp. Full truth table plus the edge cases the
// pairing-window rework (track-f-pairfix) is contractually bound to:
//   - window open = exclusive courting of NEW devices (bonded peers dropped)
//   - normal mode = anti drive-by-bonding (unbonded peers dropped)
//   - zero-bond device stays open in BOTH modes (out-of-box flow)
//   - a host whose other side deleted its keys is still bonded here and
//     cannot re-pair through a window (must be forgotten first)
#include <unity.h>
#include "logic/pairing_gate_logic.h"

void setUp() {}
void tearDown() {}

// --- normal mode (window closed) ---------------------------------------------

void test_normal_bonded_peer_accepted() {
  // Stock reconnect path: known host comes back.
  TEST_ASSERT_EQUAL(PAIRGATE_ACCEPT, pairingGateDecision(false, true, 1));
  TEST_ASSERT_EQUAL(PAIRGATE_ACCEPT, pairingGateDecision(false, true, 7));
}

void test_normal_unbonded_peer_rejected_driveby_guard() {
  // Anti drive-by-bonding: stranger dropped before SMP can start.
  TEST_ASSERT_EQUAL(PAIRGATE_REJECT, pairingGateDecision(false, false, 1));
  TEST_ASSERT_EQUAL(PAIRGATE_REJECT, pairingGateDecision(false, false, 7));
}

void test_normal_zero_bonds_stays_open_out_of_box() {
  // Factory-fresh device: first pairing needs no menu trip.
  TEST_ASSERT_EQUAL(PAIRGATE_ACCEPT, pairingGateDecision(false, false, 0));
  // Defensive: negative count from a failed bond-store read acts like zero.
  TEST_ASSERT_EQUAL(PAIRGATE_ACCEPT, pairingGateDecision(false, false, -1));
}

// --- pairing window open -------------------------------------------------------

void test_window_unbonded_peer_accepted_to_pair() {
  // The whole point of the window: the new machine may court.
  TEST_ASSERT_EQUAL(PAIRGATE_ACCEPT, pairingGateDecision(true, false, 1));
  TEST_ASSERT_EQUAL(PAIRGATE_ACCEPT, pairingGateDecision(true, false, 7));
}

void test_window_bonded_peer_rejected_inverted_gate() {
  // The live-hardware defect this branch fixes: a bonded host that
  // auto-reconnects (~7s backoff) must NOT re-occupy the link while the
  // user is trying to pair a new machine.
  TEST_ASSERT_EQUAL(PAIRGATE_REJECT, pairingGateDecision(true, true, 1));
  TEST_ASSERT_EQUAL(PAIRGATE_REJECT, pairingGateDecision(true, true, 7));
}

void test_window_zero_bonds_unchanged() {
  // Edge (a): on a zero-bond device every peer is unbonded, so the window
  // changes nothing — still open.
  TEST_ASSERT_EQUAL(PAIRGATE_ACCEPT, pairingGateDecision(true, false, 0));
}

void test_window_host_that_lost_its_own_keys_still_rejected() {
  // Edge (b): the peer deleted its keys on ITS side, but our bond store
  // still lists it — peerBonded is identity-level truth from our store, so
  // it gates as bonded and cannot re-pair through the window. The user
  // must "forget host" first, then open the window.
  TEST_ASSERT_EQUAL(PAIRGATE_REJECT, pairingGateDecision(true, true, 1));
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_normal_bonded_peer_accepted);
  RUN_TEST(test_normal_unbonded_peer_rejected_driveby_guard);
  RUN_TEST(test_normal_zero_bonds_stays_open_out_of_box);
  RUN_TEST(test_window_unbonded_peer_accepted_to_pair);
  RUN_TEST(test_window_bonded_peer_rejected_inverted_gate);
  RUN_TEST(test_window_zero_bonds_unchanged);
  RUN_TEST(test_window_host_that_lost_its_own_keys_still_rejected);
  return UNITY_END();
}
