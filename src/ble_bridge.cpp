#include "ble_bridge.h"
#include "config.h"
#include "logic/pairing_gate_logic.h"
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLESecurity.h>
#include <BLE2902.h>
#include <esp_gap_ble_api.h>
#include <Arduino.h>
#include <string.h>

// Nordic UART Service UUIDs — every BLE serial example uses these, so
// existing tools (nRF Connect, bluefy, Web Bluetooth examples) can talk to
// us without custom UUIDs.
#define NUS_SERVICE_UUID "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
#define NUS_RX_UUID      "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
#define NUS_TX_UUID      "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

// Incoming bytes are buffered in a simple ring for bleRead()/bleAvailable().
// Sized to hold a transcript snapshot JSON plus headroom; the GATT layer
// will flow-control if we fall behind.
//
// rxPush runs in the BLE stack task (Core 0); bleRead/bleAvailable run in the
// main loop (Core 1). volatile alone is not sufficient on dual-core ESP32 —
// use a portMUX spinlock to prevent torn head/tail reads.
static const size_t RX_CAP = BUDDY_RX_CAP;   // per-board, see config.h
static uint8_t  rxBuf[RX_CAP];
static size_t   rxHead = 0;
static size_t   rxTail = 0;
static portMUX_TYPE rxMux = portMUX_INITIALIZER_UNLOCKED;

static BLEServer*         server = nullptr;
static BLECharacteristic* txChar = nullptr;
static BLECharacteristic* rxChar = nullptr;
static volatile bool      connected = false;
static volatile bool      secure = false;
static volatile uint32_t  passkey = 0;
static volatile uint16_t  mtu = 23;

// BD address of the currently connected peer. Set on connect, cleared on
// disconnect. Used by bleRemoveCurrentBond() so "unpair" only removes the
// host that sent the command rather than wiping all stored bonds.
static esp_bd_addr_t      peerAddr;
static volatile bool      hasPeer = false;

// Bond-store key resolution for the current link. A privacy-enabled host's
// FIRST pairing connects under an unresolvable RPA (its IRK isn't in our
// resolving list yet), so peerAddr is a one-time address — while the
// Bluedroid bond store files the bond under its own key. Which address
// that is (SMP identity vs the pairing/pseudo address) is a stack detail
// that has shifted between IDF releases, so we don't hardcode either:
// bleBondKeyBda() resolves the key by matching the live link's candidate
// addresses against esp_ble_get_bond_device_list() — the same list the
// boot reconcile and esp_ble_remove_bond_device() consume, which makes
// registry key == reconcile key == removal key true by construction.
// Candidates, captured on the BLE stack task during SMP:
//   pidAddr  — the peer identity address from the ESP_LE_KEY_PID key
//              distribution (custom GAP handler below),
//   authAddr — the address the auth-complete event reported (the
//              pairing/pseudo address on 4.4-era Bluedroid),
//   peerAddr — the connect-time address (identity-or-pseudo when the
//              stack resolved the peer, raw RPA on a first pairing).
// peerAddr itself stays connect-time on purpose: the controller tracks
// the live link by that address, so conn-param updates and disconnect
// attribution keep working from it.
static esp_bd_addr_t      pidAddr;
static volatile bool      hasPidAddr = false;
static esp_bd_addr_t      authAddr;
static volatile bool      hasAuthAddr = false;

// Pairing window (Track F P4): while non-zero and in the future, ONLY
// unbonded peers may connect and pair — bonded hosts are evicted/rejected
// for the window's duration so a new machine gets the link to itself (see
// pairingGateDecision). Written from the main loop, read in the BLE stack
// task's onConnect.
static volatile uint32_t  pairWinUntil = 0;

// Deadline for the advertising fallback after the window-open eviction:
// normally the evicted peer's disconnect event lands within a connection
// interval and onDisconnect restarts advertising, but if the event stalls
// (peer vanished — supervision timeout still counting), blePairingTick()
// force-starts advertising at this deadline so the window is never dark.
// Cleared by onDisconnect (event landed) or on window close.
static volatile uint32_t  pairWinAdvKickAt = 0;

// Requested link mode (Track F P7): main.cpp latches the intent (relaxed on
// screen-off, tight on wake); the wire update is (re)applied on every call
// with a live peer AND on connect, so a host that reconnects while the
// device is already idle lands straight on the battery-friendly interval.
static volatile bool      linkRelaxed = false;

// Push the latched link mode to the controller. Values follow the ttpears
// power work, retuned to the P7 spec: relaxed = 120-200 ms interval with
// slave latency 4 (worst-case ~1 s delivery lag, radio sleeps through idle
// connection events), tight = 15-30 ms, no latency. Supervision timeouts
// keep > 2x the effective interval so a missed event doesn't drop the link.
// Units: interval 1.25 ms, timeout 10 ms. Safe from either core — the GAP
// call just queues a controller request.
static void _bleApplyConnParams() {
  if (!connected || !hasPeer) return;
  esp_ble_conn_update_params_t cp = {};
  memcpy(cp.bda, (const void*)peerAddr, sizeof(cp.bda));
  if (linkRelaxed) {
    cp.min_int = 96;    // 120 ms
    cp.max_int = 160;   // 200 ms
    cp.latency = 4;     // effective worst ~1 s
    cp.timeout = 600;   // 6 s supervision (> (1+4)*200ms*2)
  } else {
    cp.min_int = 12;    // 15 ms
    cp.max_int = 24;    // 30 ms
    cp.latency = 0;
    cp.timeout = 400;   // 4 s
  }
  esp_ble_gap_update_conn_params(&cp);
}

static bool _pairWindowOpen() {
  uint32_t u = pairWinUntil;
  return u != 0 && (int32_t)(millis() - u) < 0;
}

// Is this address in the Bluedroid bond store? Called from the BLE task's
// onConnect; the static buffer is safe because GATTS events are serialized.
// Fails open on API error — the gate is defense-in-depth on top of the
// encrypted-only characteristics, and a false lockout would be worse.
static bool _isBonded(const uint8_t* bda) {
  int n = esp_ble_get_bond_device_num();
  if (n <= 0) return false;
  static esp_ble_bond_dev_t list[BUDDY_MAX_HOSTS + 2];
  if (n > (int)(sizeof(list) / sizeof(list[0]))) n = sizeof(list) / sizeof(list[0]);
  if (esp_ble_get_bond_device_list(&n, list) != ESP_OK) return true;
  for (int i = 0; i < n; i++) {
    if (memcmp(bda, list[i].bd_addr, 6) == 0) return true;
  }
  return false;
}

static void rxPush(const uint8_t* p, size_t n) {
  portENTER_CRITICAL(&rxMux);
  for (size_t i = 0; i < n; i++) {
    size_t next = (rxHead + 1) % RX_CAP;
    if (next == rxTail) break;  // full — drop tail, keep whatever fits
    rxBuf[rxHead] = p[i];
    rxHead = next;
  }
  portEXIT_CRITICAL(&rxMux);
}

class RxCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* c) override {
    std::string v = c->getValue();
    if (!v.empty()) rxPush((const uint8_t*)v.data(), v.size());
  }
};

class ServerCallbacks : public BLEServerCallbacks {
  // Use the extended overload to capture the peer BD address.
  void onConnect(BLEServer* s, esp_ble_gatts_cb_param_t* param) override {
    // Gate on a LOCAL copy of the address — commit to the shared peerAddr
    // only on ACCEPT. Committing before the decision let a gate-rejected
    // reconnect retry (the evicted host redials ~7s apart for the whole
    // pairing window) clobber the ACCEPTED link's identity mid-SMP, which
    // is how the new host's hello later landed in the old host's registry
    // record (live 2026-07-11 "hosts list lost the first host").
    esp_bd_addr_t who;
    memcpy(who, param->connect.remote_bda, sizeof(esp_bd_addr_t));
    // Connection gate — decision table in logic/pairing_gate_logic.h.
    // Normal mode: anti drive-by-bonding, drop unbonded peers before SMP
    // can start (zero-bond device stays open for the out-of-box flow).
    // Pairing window: INVERTED — the window courts new devices exclusively,
    // so already-bonded hosts are dropped (they auto-retry after the window)
    // and only unbonded peers proceed to SMP. (Bonded peers using RPAs are
    // resolved to their identity address by the stack before this event,
    // since their IRKs sit in the controller resolving list — so a host
    // that deleted its own keys still gates as bonded here and must be
    // forgotten on the device before it can re-pair through a window.)
    bool win = _pairWindowOpen();
    if (pairingGateDecision(win, _isBonded(who),
                            esp_ble_get_bond_device_num()) == PAIRGATE_REJECT) {
      if (win) {
        Serial.printf("[ble] pairwin reject bonded peer "
                      "%02x:%02x:%02x:%02x:%02x:%02x (window is for new hosts)\n",
                      who[0], who[1], who[2], who[3], who[4], who[5]);
      } else {
        Serial.println("[ble] rejecting unbonded peer (pairing window closed)");
      }
      s->disconnect(param->connect.conn_id);
      return;
    }
    memcpy((void*)peerAddr, who, sizeof(esp_bd_addr_t));
    hasPeer = true;
    hasPidAddr = false;    // fresh link — key material arrives during SMP
    hasAuthAddr = false;
    connected = true;
    // Apply the latched link mode: tight if the device is awake, relaxed if
    // it's reconnecting while the screen is off (P7 power).
    _bleApplyConnParams();
    Serial.printf("[ble] connected %02x:%02x:%02x:%02x:%02x:%02x\n",
      peerAddr[0], peerAddr[1], peerAddr[2],
      peerAddr[3], peerAddr[4], peerAddr[5]);
  }
  // Extended overload (both are dispatched by BLEServer) so the event can
  // be attributed to a specific link. A gate-rejected link's teardown MUST
  // NOT clobber the accepted link's state: during the pairing window the
  // evicted host's ~7s reconnect retries each ended here and yanked
  // connected/secure to false while the NEW host was mid-SMP — its adopt
  // transition was lost and advertising restarted on top of a live link
  // (inviting a second central onto the single-link design).
  void onDisconnect(BLEServer* s, esp_ble_gatts_cb_param_t* param) override {
    if (connected && hasPeer &&
        memcmp(param->disconnect.remote_bda, (const void*)peerAddr, 6) != 0) {
      Serial.println("[ble] foreign-link disconnect ignored (gate reject)");
      return;
    }
    connected = false;
    secure = false;
    passkey = 0;
    mtu = 23;
    hasPeer = false;
    hasPidAddr = false;
    hasAuthAddr = false;
    // The disconnect event landed — the advertising restart below covers the
    // pairing-window eviction too, so the tick fallback can stand down.
    pairWinAdvKickAt = 0;
    Serial.println("[ble] disconnected");
    // Restart advertising so the next client can find us.
    BLEDevice::startAdvertising();
  }
  void onMtuChanged(BLEServer*, esp_ble_gatts_cb_param_t* param) override {
    mtu = param->mtu.mtu;
    Serial.printf("[ble] mtu=%u\n", mtu);
  }
};

// LE Secure Connections, passkey-entry: we are DisplayOnly, the central
// is KeyboardOnly. The stack picks a random 6-digit passkey, calls
// onPassKeyNotify here, and the user types it on the desktop. main.cpp
// polls blePasskey() to render it.
class SecCallbacks : public BLESecurityCallbacks {
  uint32_t onPassKeyRequest() override { return 0; }
  bool onConfirmPIN(uint32_t) override { return false; }
  bool onSecurityRequest() override { return true; }
  void onPassKeyNotify(uint32_t pk) override {
    passkey = pk;
    Serial.printf("[ble] passkey %06lu\n", (unsigned long)pk);
  }
  void onAuthenticationComplete(esp_ble_auth_cmpl_t cmpl) override {
    // No accepted link -> this event belongs to a gate-rejected bonded
    // peer whose LTK re-encryption raced our disconnect. Latching secure
    // here would fire a premature registry adopt the moment the next peer
    // connects (before its own SMP has run).
    if (!connected) {
      Serial.println("[ble] stray auth event ignored (no accepted link)");
      return;
    }
    passkey = 0;
    if (cmpl.success) {
      // A key-resolution candidate, NOT trusted as the identity: on
      // 4.4-era Bluedroid this is the pairing/pseudo address forwarded
      // unchanged from SMP. Captured BEFORE latching 'secure' so the
      // registry poll can never observe a secure link without the
      // candidates being in place (the PID key, when the peer
      // distributes one, has already arrived by now — key distribution
      // precedes the auth-complete event).
      memcpy((void*)authAddr, cmpl.bd_addr, sizeof(esp_bd_addr_t));
      hasAuthAddr = true;
    }
    secure = cmpl.success;
    Serial.printf("[ble] auth %s\n", cmpl.success ? "ok" : "FAIL");
    if (!cmpl.success && server) server->disconnect(server->getConnId());
  }
};

// GAP watcher (chained after the library's own handling): capture the peer
// identity address from the SMP key-distribution phase. ESP_GAP_BLE_KEY_EVT
// with ESP_LE_KEY_PID carries the peer's identity address + IRK — the only
// place the stack hands the identity to the app on this Bluedroid vintage
// (the auth-complete event reports the pairing address instead). Runs on
// the BLE stack task; the 'connected' guard mirrors onAuthenticationComplete
// so a gate-rejected link's stray SMP traffic can't seed the accepted
// link's candidates.
static void _gapKeyWatch(esp_gap_ble_cb_event_t event,
                         esp_ble_gap_cb_param_t* param) {
  if (event != ESP_GAP_BLE_KEY_EVT) return;
  if (!connected) return;
  if (param->ble_security.ble_key.key_type != ESP_LE_KEY_PID) return;
  memcpy((void*)pidAddr,
         param->ble_security.ble_key.p_key_value.pid_key.static_addr,
         sizeof(esp_bd_addr_t));
  hasPidAddr = true;
}

void bleInit(const char* deviceName, uint8_t pairMode) {
  BLEDevice::init(deviceName);
  BLEDevice::setCustomGapHandler(_gapKeyWatch);
  // Request the biggest MTU we can get. macOS negotiates to 185 typically.
  BLEDevice::setMTU(517);

  // pairMode 0 (default): LE Secure Connections passkey-entry — we are
  // DisplayOnly, the desktop types the 6-digit code (MITM protection).
  // pairMode 1: LE Secure Connections just-works — no passkey, still bonded
  // and AES-CCM encrypted, just without MITM authentication. The NUS
  // characteristics keep their encrypted-only permissions in both modes.
  bool justWorks = pairMode == 1;
  BLEDevice::setEncryptionLevel(justWorks ? ESP_BLE_SEC_ENCRYPT
                                          : ESP_BLE_SEC_ENCRYPT_MITM);
  BLEDevice::setSecurityCallbacks(new SecCallbacks());

  server = BLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());

  BLEService* svc = server->createService(NUS_SERVICE_UUID);

  txChar = svc->createCharacteristic(
    NUS_TX_UUID,
    BLECharacteristic::PROPERTY_NOTIFY
  );
  txChar->setAccessPermissions(ESP_GATT_PERM_READ_ENCRYPTED);
  BLE2902* cccd = new BLE2902();
  cccd->setAccessPermissions(ESP_GATT_PERM_READ_ENCRYPTED | ESP_GATT_PERM_WRITE_ENCRYPTED);
  txChar->addDescriptor(cccd);

  rxChar = svc->createCharacteristic(
    NUS_RX_UUID,
    BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR
  );
  rxChar->setAccessPermissions(ESP_GATT_PERM_WRITE_ENCRYPTED);
  rxChar->setCallbacks(new RxCallbacks());

  svc->start();

  BLESecurity* sec = new BLESecurity();
  sec->setAuthenticationMode(justWorks ? ESP_LE_AUTH_REQ_SC_BOND
                                       : ESP_LE_AUTH_REQ_SC_MITM_BOND);
  sec->setCapability(justWorks ? ESP_IO_CAP_NONE : ESP_IO_CAP_OUT);
  sec->setKeySize(16);
  sec->setInitEncryptionKey(ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK);
  sec->setRespEncryptionKey(ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK);

  BLEAdvertising* adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(NUS_SERVICE_UUID);
  adv->setScanResponse(true);
  adv->setMinPreferred(0x06);   // iOS-friendly connection interval
  adv->setMaxPreferred(0x12);
  BLEDevice::startAdvertising();
  Serial.printf("[ble] advertising as '%s'\n", deviceName);
}

bool bleConnected() { return connected; }
bool bleSecure()    { return secure; }
uint32_t blePasskey() { return passkey; }

void bleConnParams(bool relaxed) {
  linkRelaxed = relaxed;
  _bleApplyConnParams();
}

void bleAllowPairing(bool open, uint32_t windowMs) {
  if (open) {
    uint32_t until = millis() + windowMs;
    pairWinUntil = until == 0 ? 1 : until;   // 0 means closed — dodge wraparound
    Serial.printf("[ble] pairwin open %lus\n",
                  (unsigned long)(windowMs / 1000));
    if (connected) {
      // The window courts NEW devices exclusively, and we don't advertise
      // while a link is up — so evict the current host first. It's bonded;
      // it will auto-reconnect once the window closes (its retry backoff is
      // its own). The window stamp is already set above, so even an instant
      // reconnect race lands on the bonded-peer reject in onConnect.
      // Advertising restarts in onDisconnect when the disconnect event
      // lands; the 500ms deadline below is the fallback path in
      // blePairingTick() for a stalled event.
      Serial.println("[ble] pairwin evict connected peer");
      pairWinAdvKickAt = millis() + 500;
      bleDisconnect();
    } else {
      // Not connected — advertising should already be running (it restarts
      // on every disconnect), but make sure the window never starts dark.
      // Starting an already-started advertiser is a harmless no-op.
      BLEDevice::startAdvertising();
    }
  } else {
    // Idempotent + quiet: the registry closes the window unconditionally on
    // every fresh bond (also the windowless out-of-box pairing), so only
    // log when a window was actually open.
    if (pairWinUntil != 0) Serial.println("[ble] pairwin close");
    pairWinUntil = 0;
    pairWinAdvKickAt = 0;
  }
}

void blePairingTick(uint32_t nowMs) {
  // Natural expiry: zero the stamp so "[ble] pairwin close" fires exactly
  // once per window no matter how it ends. (The gate itself never needs
  // this — _pairWindowOpen() is time-checked — it's for logs and so
  // blePairingRemainingMs() consumers see a clean 0.)
  uint32_t u = pairWinUntil;
  if (u != 0 && (int32_t)(nowMs - u) >= 0) {
    pairWinUntil = 0;
    pairWinAdvKickAt = 0;
    Serial.println("[ble] pairwin close (expired, nothing paired)");
  }
  // Eviction fallback: the disconnect event hasn't landed within 500ms of
  // opening the window (peer vanished mid-air; supervision timeout can run
  // seconds). Force advertising so the new device can find us — if the
  // event did land, onDisconnect cleared this deadline and already
  // restarted advertising.
  uint32_t k = pairWinAdvKickAt;
  if (k != 0 && (int32_t)(nowMs - k) >= 0) {
    pairWinAdvKickAt = 0;
    // Only if the link is actually still down: advertising on top of a live
    // accepted link would invite a second central onto the single-link
    // design (the new host may already have connected by this deadline).
    if (!connected) {
      Serial.println("[ble] pairwin adv fallback (disconnect event late)");
      BLEDevice::startAdvertising();
    }
  }
}

uint32_t blePairingRemainingMs() {
  uint32_t u = pairWinUntil;
  if (u == 0) return 0;
  int32_t left = (int32_t)(u - millis());
  return left > 0 ? (uint32_t)left : 0;
}

bool blePeerBda(uint8_t out[6]) {
  if (!hasPeer) return false;
  memcpy(out, (const void*)peerAddr, 6);
  return true;
}

// Resolve the bond-store key for the current link by matching the link's
// candidate addresses against the bond list itself — the exact list the
// boot reconcile diffs against and esp_ble_remove_bond_device() indexes,
// so whatever keying this Bluedroid uses, the returned address is the one
// that works there. Each list entry is checked on both its store key
// (bd_addr) and, when the peer distributed one, its identity
// (bond_key.pid_key.static_addr). Called from the main loop only (the
// static list buffer is single-caller, same pattern as _isBonded on the
// BLE task). False when nothing has authenticated on this link.
bool bleBondKeyBda(uint8_t out[6]) {
  if (!hasAuthAddr && !hasPidAddr) return false;
  const uint8_t* cand[3];
  int nc = 0;
  if (hasPidAddr)  cand[nc++] = (const uint8_t*)pidAddr;
  if (hasAuthAddr) cand[nc++] = (const uint8_t*)authAddr;
  if (hasPeer)     cand[nc++] = (const uint8_t*)peerAddr;

  int n = esp_ble_get_bond_device_num();
  if (n > 0) {
    static esp_ble_bond_dev_t list[BUDDY_MAX_HOSTS + 2];
    if (n > (int)(sizeof(list) / sizeof(list[0]))) n = sizeof(list) / sizeof(list[0]);
    if (esp_ble_get_bond_device_list(&n, list) == ESP_OK) {
      for (int i = 0; i < n; i++) {
        bool hasPid = (list[i].bond_key.key_mask & ESP_LE_KEY_PID) != 0;
        for (int c = 0; c < nc; c++) {
          if (memcmp(list[i].bd_addr, cand[c], 6) == 0 ||
              (hasPid && memcmp(list[i].bond_key.pid_key.static_addr,
                                cand[c], 6) == 0)) {
            memcpy(out, list[i].bd_addr, 6);
            if (memcmp(out, (const void*)peerAddr, 6) != 0) {
              // Key differs from the live link address — the case this
              // resolution exists for. Log once per lookup for the
              // hardware smoke (connect vs store key).
              Serial.printf("[ble] bond key %02x:%02x:%02x:%02x:%02x:%02x"
                            " (link %02x:%02x:%02x:%02x:%02x:%02x)\n",
                            out[0], out[1], out[2], out[3], out[4], out[5],
                            peerAddr[0], peerAddr[1], peerAddr[2],
                            peerAddr[3], peerAddr[4], peerAddr[5]);
            }
            return true;
          }
        }
      }
    }
  }
  // No list match (store write raced, or list API failed): best candidate,
  // identity first. Callers treat this the same — it only weakens the
  // guarantee back to best-effort instead of failing the adopt outright.
  Serial.println("[ble] bond key: no list match, using candidate");
  memcpy(out, cand[0], 6);
  return true;
}

void bleDisconnect() {
  if (server && connected) server->disconnect(server->getConnId());
}

#ifdef BUDDY_BLE_WHITELIST
// Optional adv-level pin enforcement: only the whitelisted address may even
// connect. OFF by default — a pinned host using resolvable private addresses
// can fail the controller whitelist match (risk R3) and get silently locked
// out at the radio layer, with none of the soft-pin path's self-healing.
// Soft-pin (accept, resolve, sel:false, disconnect) is the guaranteed path;
// this is a power/no-op-connect optimization for public/static-address hosts.
void bleWhitelistPin(const uint8_t* bda) {
  BLEAdvertising* adv = BLEDevice::getAdvertising();
  adv->stop();
  static uint8_t curWl[6];
  static bool haveWl = false;
  if (haveWl) {
    esp_ble_gap_update_whitelist(false, curWl, BLE_WL_ADDR_TYPE_PUBLIC);
    haveWl = false;
  }
  if (bda) {
    memcpy(curWl, bda, 6);
    esp_ble_gap_update_whitelist(true, curWl, BLE_WL_ADDR_TYPE_PUBLIC);
    haveWl = true;
    adv->setScanFilter(false, true);   // scan any, connect whitelist-only
  } else {
    adv->setScanFilter(false, false);
  }
  adv->start();
}
#endif

void bleRemoveCurrentBond() {
  // Resolve the store's own key for this link — the connect-time address
  // of a first-time-pairing privacy host is an RPA the store never heard
  // of, and removal by it fails silently.
  esp_bd_addr_t victim;
  if (!bleBondKeyBda(victim)) {
    if (!hasPeer) {
      Serial.println("[ble] unpair: no peer to remove");
      return;
    }
    memcpy(victim, (const void*)peerAddr, 6);
  }
  esp_ble_remove_bond_device(victim);
  Serial.printf("[ble] removed bond %02x:%02x:%02x:%02x:%02x:%02x\n",
    victim[0], victim[1], victim[2], victim[3], victim[4], victim[5]);
}

void bleClearBonds() {
  int n = esp_ble_get_bond_device_num();
  if (n <= 0) return;
  esp_ble_bond_dev_t* list = (esp_ble_bond_dev_t*)malloc(n * sizeof(esp_ble_bond_dev_t));
  if (!list) return;
  esp_ble_get_bond_device_list(&n, list);
  for (int i = 0; i < n; i++) esp_ble_remove_bond_device(list[i].bd_addr);
  free(list);
  Serial.printf("[ble] cleared %d bond(s)\n", n);
}

size_t bleAvailable() {
  portENTER_CRITICAL(&rxMux);
  size_t n = (rxHead + RX_CAP - rxTail) % RX_CAP;
  portEXIT_CRITICAL(&rxMux);
  return n;
}

int bleRead() {
  portENTER_CRITICAL(&rxMux);
  if (rxHead == rxTail) { portEXIT_CRITICAL(&rxMux); return -1; }
  int b = rxBuf[rxTail];
  rxTail = (rxTail + 1) % RX_CAP;
  portEXIT_CRITICAL(&rxMux);
  return b;
}

size_t bleWrite(const uint8_t* data, size_t len) {
  if (!connected || !txChar) return 0;
  // ATT notify payload is limited to (MTU - 3). macOS negotiates 185, so
  // the 182-byte chunk works there; use the live mtu so a peer that caps
  // at the 23-byte default doesn't get truncated notifies.
  size_t chunk = mtu > 3 ? mtu - 3 : 20;
  if (chunk > 180) chunk = 180;
  size_t sent = 0;
  while (sent < len) {
    size_t n = len - sent;
    if (n > chunk) n = chunk;
    txChar->setValue((uint8_t*)(data + sent), n);
    txChar->notify();
    sent += n;
    // Small yield so the BLE stack flushes before the next chunk.
    delay(4);
  }
  return sent;
}
