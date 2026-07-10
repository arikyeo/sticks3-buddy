#pragma once
#include <stdint.h>

// Connection gate for ble_bridge.cpp's onConnect, extracted as a pure
// decision function so the truth table unit-tests on the native host
// (test_pairgate). ble_bridge feeds it live stack state; nothing here may
// touch Arduino or Bluedroid.
//
//   windowActive  the user-initiated pairing window is currently open
//   peerBonded    the connecting peer's identity address is in our bond
//                 store (RPAs are resolved by the controller before the
//                 connect event, so this is identity-level truth)
//   bondCount     number of bonds we hold in total (zero-bond device is
//                 factory-fresh and stays open by design)

enum PairGateDecision : uint8_t {
  PAIRGATE_ACCEPT = 0,   // let the link proceed (to SMP pairing if unbonded)
  PAIRGATE_REJECT = 1,   // disconnect immediately, before SMP can start
};

inline PairGateDecision pairingGateDecision(bool windowActive, bool peerBonded,
                                            int bondCount) {
  if (windowActive) {
    // Pairing window = exclusive courting of NEW devices. A peer we already
    // hold a bond for is dropped politely — its own reconnect backoff will
    // retry after the window — and only unbonded peers may proceed to SMP.
    // This INVERTS the normal-mode gate below: without it, a bonded host
    // that auto-reconnects (stock Claude Desktop retries ~7s after any
    // disconnect) re-occupies the single BLE link before the new machine
    // ever sees the device advertise.
    //
    // Corollary (deliberate): a host whose OTHER side deleted its keys is
    // still bonded on the device, so it cannot re-pair through the window.
    // The user must "hosts > forget..." it first, then "add host...".
    return peerBonded ? PAIRGATE_REJECT : PAIRGATE_ACCEPT;
  }
  // Normal mode: anti drive-by-bonding. Unbonded peers are dropped before
  // SMP pairing can start. Exception: a device with zero stored bonds stays
  // open so the first out-of-box pairing needs no menu trip (on such a
  // device the window changes nothing — every peer is unbonded and may
  // court either way).
  if (bondCount <= 0) return PAIRGATE_ACCEPT;
  return peerBonded ? PAIRGATE_ACCEPT : PAIRGATE_REJECT;
}
