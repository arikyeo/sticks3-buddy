// WiFi OTA from GitHub releases — see ota.h. -DBUDDY_OTA envs only; the
// whole file compiles away in the default envs (build_src_filter stays a
// plain +<*>, same pattern as ir_remote.cpp). Hardening choices ported
// from the oh001738 OTA track: explicit creds into WiFi.begin (kills their
// SSID-after-begin NVS race at the root), isolated WiFiClientSecure
// instances for the API call vs the download, forced redirect-follow for
// the GitHub asset → object-storage hop, and WiFi torn down again on
// every exit path.
#ifdef BUDDY_OTA

#include "ota.h"
#include "../config.h"
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <HTTPUpdate.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include "esp_task_wdt.h"

// Preferred release asset. Today's releases publish a single S3
// firmware.bin; if multi-board OTA assets appear later they're expected as
// firmware-<slug>.bin, so the slug lookup runs first and falls back to the
// plain name. (Slugs are compile-time — board::name() has spaces.)
#if defined(BOARD_STICKS3)
#  define OTA_BOARD_SLUG "s3"
#elif defined(BOARD_STICKC_PLUS2)
#  define OTA_BOARD_SLUG "plus2"
#else
#  define OTA_BOARD_SLUG "plus"
#endif

static const char* OTA_LATEST_URL =
    "https://api.github.com/repos/arikyeo/sticks3-buddy/releases/latest";

static volatile bool _otaBusy = false;
bool otaInProgress() { return _otaBusy; }

static OtaProgressFn _cb = nullptr;
static int _lastPct = -1;

// Every step report also pets the loop-task watchdog: the whole flow runs
// inside one loop() iteration.
static void _prog(const char* status, int pct) {
  esp_task_wdt_reset();
  if (_cb) _cb(status, pct);
}

static void _dlProgress(int cur, int total) {
  if (total <= 0) return;
  int pct = (int)(((int64_t)cur * 100) / total);
  if (pct < 0) pct = 0;
  if (pct > 100) pct = 100;
  if (pct == _lastPct) return;   // flicker-free: repaint only on change
  _lastPct = pct;
  _prog("downloading", pct);
}

// Tear the WiFi stack down again. BLE never left — it keeps advertising
// and serving through the whole flow (coex) — but the radio time and
// ~50KB of heap go back to it.
static void _wifiOff() {
  WiFi.disconnect(true /*wifioff*/);
  WiFi.mode(WIFI_OFF);
}

static bool _fail(char* errBuf, size_t errLen, const char* msg) {
  if (errLen) snprintf(errBuf, errLen, "%s", msg);
  _wifiOff();
  esp_task_wdt_init(12, true);   // restore the loop watchdog budget
  _otaBusy = false;
  return false;
}

bool otaRun(OtaProgressFn progress, char* errBuf, size_t errLen) {
  _cb = progress;
  _lastPct = -1;
  if (errLen) errBuf[0] = 0;
  _otaBusy = true;
  // The 12s TWDT would fire mid-TLS-handshake or mid-flash-erase; widen it
  // for the flow (every exit path restores 12s). A genuine wedge still
  // reboots — which for OTA is the safe abort, since the boot partition
  // isn't switched until the download completed and verified.
  esp_task_wdt_init(120, true);

  // --- creds (NVS "wifi", provisioned via the v2 wifi command) -------------
  char ssid[33] = "", pass[65] = "";
  {
    Preferences p;
    if (p.begin("wifi", true)) {   // read-only; false when never provisioned
      p.getString("ssid", ssid, sizeof(ssid));
      p.getString("pass", pass, sizeof(pass));
      p.end();
    }
  }
  if (!ssid[0]) return _fail(errBuf, errLen, "wifi not set up");

  // --- join -----------------------------------------------------------------
  _prog("wifi: joining", -1);
  WiFi.persistent(false);   // creds live in OUR namespace, not the stack's NVS
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, pass);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - t0 > 20000) return _fail(errBuf, errLen, "wifi timeout");
    delay(250);
    _prog("wifi: joining", -1);
  }

  // --- latest release lookup --------------------------------------------------
  _prog("github: checking", -1);
  char tag[32] = "", urlSlug[224] = "", urlPlain[224] = "";
  {
    // Own TLS client, scoped so its ~45KB is back before the download's.
    WiFiClientSecure api;
    api.setInsecure();   // v1 tradeoff — risk + rationale in PROTOCOL_V2.md
    HTTPClient http;
    http.setConnectTimeout(8000);
    http.setTimeout(8000);
    http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
    if (!http.begin(api, OTA_LATEST_URL))
      return _fail(errBuf, errLen, "api begin failed");
    http.addHeader("User-Agent", "sticks3-buddy-ota");   // GitHub requires one
    http.addHeader("Accept", "application/vnd.github+json");
    int code = http.GET();
    if (code != HTTP_CODE_OK) {
      http.end();
      char m[32];
      snprintf(m, sizeof(m), "github http %d", code);
      return _fail(errBuf, errLen, m);
    }
    // Stream-parse with a filter — the full release JSON (body text and
    // all) would be tens of KB of heap for three fields.
    JsonDocument filter;
    filter["tag_name"] = true;
    filter["assets"][0]["name"] = true;
    filter["assets"][0]["browser_download_url"] = true;
    JsonDocument doc;
    DeserializationError err = deserializeJson(
        doc, http.getStream(), DeserializationOption::Filter(filter));
    http.end();
    if (err) return _fail(errBuf, errLen, "release parse failed");

    strncpy(tag, doc["tag_name"] | "", sizeof(tag) - 1);
    for (JsonObject a : doc["assets"].as<JsonArray>()) {
      const char* n = a["name"] | "";
      const char* u = a["browser_download_url"];
      if (!u) continue;
      if (strcmp(n, "firmware-" OTA_BOARD_SLUG ".bin") == 0)
        strncpy(urlSlug, u, sizeof(urlSlug) - 1);
      else if (strcmp(n, "firmware.bin") == 0)
        strncpy(urlPlain, u, sizeof(urlPlain) - 1);
    }
  }
  if (!tag[0]) return _fail(errBuf, errLen, "no release tag");

  // Equality gate, tolerant of the tag's leading v: any published tag that
  // differs from the running version counts as an update (which also lets
  // a user step back to a re-published older release). Ordering semvers on
  // the device buys nothing — consent is manual either way.
  const char* remote = (tag[0] == 'v' || tag[0] == 'V') ? tag + 1 : tag;
  if (strcmp(remote, BUDDY_FW_VERSION) == 0) {
    char m[48];
    snprintf(m, sizeof(m), "up to date (%.20s)", tag);
    return _fail(errBuf, errLen, m);
  }
  const char* url = urlSlug[0] ? urlSlug : urlPlain;
  if (!url[0]) return _fail(errBuf, errLen, "no firmware asset");

  // --- download into the inactive slot ---------------------------------------
  _prog("downloading", 0);
  WiFiClientSecure dl;
  dl.setInsecure();
  httpUpdate.rebootOnUpdate(false);   // manual: wifi-off + progress first
  httpUpdate.setLedPin(-1);
  httpUpdate.onProgress(_dlProgress);
  // The asset URL 302s to GitHub's object storage — must follow.
  httpUpdate.setFollowRedirects(HTTPC_FORCE_FOLLOW_REDIRECTS);
  t_httpUpdate_return r = httpUpdate.update(dl, url);

  if (r == HTTP_UPDATE_OK) {
    // Boot partition switched (state NEW → the bootloader's rollback
    // machinery takes over: main.cpp marks the slot valid after 15s of
    // healthy uptime, or the next reboot falls back to this slot).
    _prog("rebooting", 100);
    _wifiOff();
    _otaBusy = false;
    delay(600);   // let the progress screen land before the panel dies
    ESP.restart();
    return true;   // not reached
  }
  char m[64];
  snprintf(m, sizeof(m), "ota: %.48s", httpUpdate.getLastErrorString().c_str());
  return _fail(errBuf, errLen, m);
}

#endif  // BUDDY_OTA
