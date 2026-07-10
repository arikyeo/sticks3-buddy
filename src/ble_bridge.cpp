#include "ble_bridge.h"
#include "config.h"
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

// Pairing window (Track F P4): while non-zero and in the future, unbonded
// peers may connect and pair. Written from the main loop, read in the BLE
// stack task's onConnect.
static volatile uint32_t  pairWinUntil = 0;

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
    memcpy((void*)peerAddr, param->connect.remote_bda, sizeof(esp_bd_addr_t));
    hasPeer = true;
    // Anti drive-by-bonding gate: outside an explicit pairing window, only
    // already-bonded peers may stay connected — drop unknowns here, before
    // SMP pairing can even start. A device with zero bonds stays open so
    // the first out-of-box pairing needs no menu trip. (Bonded peers using
    // RPAs are resolved to their identity address by the stack before this
    // event, since their IRKs sit in the controller resolving list.)
    if (!_pairWindowOpen() && esp_ble_get_bond_device_num() > 0 &&
        !_isBonded(peerAddr)) {
      Serial.println("[ble] rejecting unbonded peer (pairing window closed)");
      hasPeer = false;
      s->disconnect(param->connect.conn_id);
      return;
    }
    connected = true;
    // Apply the latched link mode: tight if the device is awake, relaxed if
    // it's reconnecting while the screen is off (P7 power).
    _bleApplyConnParams();
    Serial.printf("[ble] connected %02x:%02x:%02x:%02x:%02x:%02x\n",
      peerAddr[0], peerAddr[1], peerAddr[2],
      peerAddr[3], peerAddr[4], peerAddr[5]);
  }
  void onDisconnect(BLEServer* s) override {
    connected = false;
    secure = false;
    passkey = 0;
    mtu = 23;
    hasPeer = false;
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
    passkey = 0;
    secure = cmpl.success;
    Serial.printf("[ble] auth %s\n", cmpl.success ? "ok" : "FAIL");
    if (!cmpl.success && server) server->disconnect(server->getConnId());
  }
};

void bleInit(const char* deviceName, uint8_t pairMode) {
  BLEDevice::init(deviceName);
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
    Serial.printf("[ble] pairing window open for %lus\n",
                  (unsigned long)(windowMs / 1000));
  } else {
    pairWinUntil = 0;
    Serial.println("[ble] pairing window closed");
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
  if (!hasPeer) {
    Serial.println("[ble] unpair: no peer to remove");
    return;
  }
  esp_ble_remove_bond_device(peerAddr);
  Serial.printf("[ble] removed bond %02x:%02x:%02x:%02x:%02x:%02x\n",
    peerAddr[0], peerAddr[1], peerAddr[2],
    peerAddr[3], peerAddr[4], peerAddr[5]);
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
