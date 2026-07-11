// WiFi OTA from GitHub releases — see ota.h. -DBUDDY_OTA envs only; the
// whole file compiles away in the default envs (build_src_filter stays a
// plain +<*>, same pattern as ir_remote.cpp). Hardening choices ported
// from the oh001738 OTA track: isolated WiFiClientSecure instances for the
// probe calls vs the download, forced redirect-follow for the GitHub asset
// → object-storage hop, and WiFi torn down again on every exit path.
//
// Release discovery is API-FREE: api.github.com rate-limits unauthenticated
// clients to 0..60 req/h per source IP (observed live: 403 with the limit
// exhausted), so the device instead GETs the stable redirect URL
//   https://github.com/<repo>/releases/latest/download/firmware.bin
// with redirects DISABLED and reads the 302 Location header —
//   .../releases/download/<tag>/firmware.bin
// which carries the latest tag (parsed by logic/ota_url_logic.h) AND is the
// exact versioned asset URL to download. No JSON, no auth, no rate limit.
#ifdef BUDDY_OTA

#include "ota.h"
#include "../config.h"
#include "../wifi_store.h"
#include "../logic/ota_url_logic.h"
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <HTTPUpdate.h>
#include "esp_task_wdt.h"

// Legacy api.github.com discovery, kept compilable for a future
// AUTHENTICATED build (a token lifts the rate limit and restores asset
// listing). Default OFF — the redirect probe needs neither.
#ifndef OTA_USE_API
#define OTA_USE_API 0
#endif
#if OTA_USE_API
#include <ArduinoJson.h>
#endif

#ifndef OTA_REPO
#define OTA_REPO "arikyeo/sticks3-buddy"
#endif

// Preferred release asset. Today's releases publish a single S3
// firmware.bin; if multi-board OTA assets appear later they're expected as
// firmware-<slug>.bin, so the slug URL is probed first and falls back to
// the plain name. (Slugs are compile-time — board::name() has spaces.)
#if defined(BOARD_STICKS3)
#  define OTA_BOARD_SLUG "s3"
#elif defined(BOARD_STICKC_PLUS2)
#  define OTA_BOARD_SLUG "plus2"
#else
#  define OTA_BOARD_SLUG "plus"
#endif

static const char* OTA_LATEST_PROBE =
    "https://github.com/" OTA_REPO "/releases/latest/download/firmware.bin";
#if OTA_USE_API
static const char* OTA_LATEST_URL =
    "https://api.github.com/repos/" OTA_REPO "/releases/latest";
#endif

static volatile bool _otaBusy = false;
bool otaInProgress() { return _otaBusy; }

static OtaProgressFn _cb = nullptr;
static OtaSoundFn _sound = nullptr;
static int _lastPct = -1;

// Every step report also pets the loop-task watchdog: the whole flow runs
// inside one loop() iteration.
static void _prog(const char* status, int pct) {
  esp_task_wdt_reset();
  if (_cb) _cb(status, pct);
}

static void _joinTick() { _prog("wifi: joining", -1); }

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
  if (_sound) _sound(false);   // every terminal-failure return, one motif
  return false;
}

// GET with redirects DISABLED — the status code and the Location header are
// the whole answer; the body is never read. loc may be NULL when only
// existence (3xx vs 404) matters. Own scoped TLS client per call, same
// heap discipline as the old API lookup.
static int _probeRedirect(const char* url, char* loc, size_t locLen) {
  if (loc && locLen) loc[0] = 0;
  WiFiClientSecure cl;
  cl.setInsecure();   // v1 tradeoff — risk + rationale in PROTOCOL_V2.md
  HTTPClient http;
  http.setConnectTimeout(8000);
  http.setTimeout(8000);
  http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
  if (!http.begin(cl, url)) return -1;
  static const char* HDRS[] = { "Location" };
  http.collectHeaders(HDRS, 1);
  http.addHeader("User-Agent", "sticks3-buddy-ota");   // GitHub requires one
  int code = http.GET();
  if (loc && locLen && http.hasHeader("Location")) {
    String l = http.header("Location");
    snprintf(loc, locLen, "%s", l.c_str());
  }
  http.end();
  return code;
}

bool otaRun(OtaProgressFn progress, OtaSoundFn sound, char* errBuf, size_t errLen) {
  _cb = progress;
  _sound = sound;
  _lastPct = -1;
  if (errLen) errBuf[0] = 0;
  _otaBusy = true;
  // The 12s TWDT would fire mid-TLS-handshake or mid-flash-erase; widen it
  // for the flow (every exit path restores 12s). A genuine wedge still
  // reboots — which for OTA is the safe abort, since the boot partition
  // isn't switched until the download completed and verified.
  esp_task_wdt_init(120, true);

  // --- stored networks (LittleFS list via wifi_store, provisioned over the
  //     v2 wifi command; legacy NVS creds were migrated at boot) ------------
  if (wifiStoreCount() == 0) return _fail(errBuf, errLen, "no wifi networks");

  // --- join: strongest visible stored network first, fall through the rest
  _prog("wifi: scanning", -1);
  {
    char jerr[64];
    if (!wifiJoinBest(45000, _joinTick, jerr, sizeof(jerr)))
      return _fail(errBuf, errLen, jerr);
  }

  // --- latest release lookup ------------------------------------------------
  _prog("github: checking", -1);
  char tag[32] = "";     // leading v already stripped by the parser
  char url[224] = "";    // versioned asset URL the download will follow
#if OTA_USE_API
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
    http.addHeader("User-Agent", "sticks3-buddy-ota");
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

    char rawTag[32] = "", urlSlug[224] = "";
    strncpy(rawTag, doc["tag_name"] | "", sizeof(rawTag) - 1);
    for (JsonObject a : doc["assets"].as<JsonArray>()) {
      const char* n = a["name"] | "";
      const char* u = a["browser_download_url"];
      if (!u) continue;
      if (strcmp(n, "firmware-" OTA_BOARD_SLUG ".bin") == 0)
        strncpy(urlSlug, u, sizeof(urlSlug) - 1);
      else if (strcmp(n, "firmware.bin") == 0 && !urlSlug[0])
        strncpy(url, u, sizeof(url) - 1);
    }
    if (urlSlug[0]) strncpy(url, urlSlug, sizeof(url) - 1);
    if (!rawTag[0]) return _fail(errBuf, errLen, "no release tag");
    const char* t = (rawTag[0] == 'v' || rawTag[0] == 'V') ? rawTag + 1 : rawTag;
    snprintf(tag, sizeof(tag), "%s", t);
    if (!url[0]) return _fail(errBuf, errLen, "no firmware asset");
  }
#else
  {
    int code = _probeRedirect(OTA_LATEST_PROBE, url, sizeof(url));
    if (code == HTTP_CODE_NOT_FOUND)
      return _fail(errBuf, errLen, "no release found");   // no releases yet
    if (code < 300 || code >= 400 || !url[0]) {
      char m[32];
      snprintf(m, sizeof(m), "github http %d", code);
      return _fail(errBuf, errLen, m);
    }
    // url is now .../releases/download/<tag>/firmware.bin — the tag pinned
    // at probe time, so a release published mid-flow can't torn-read us.
    if (!otaParseTagFromLocation(url, tag, sizeof(tag)))
      return _fail(errBuf, errLen, "release parse failed");
  }
#endif

  // Equality gate, tolerant of the tag's leading v (stripped above): any
  // published tag that differs from the running version counts as an update
  // (which also lets a user step back to a re-published older release).
  // Ordering semvers on the device buys nothing — consent is manual either
  // way. Equal versions are the good outcome, not an error; the message
  // reads accordingly on the toast.
  if (strcmp(tag, BUDDY_FW_VERSION) == 0) {
    char m[48];
    snprintf(m, sizeof(m), "up to date (%.20s)", tag);
    return _fail(errBuf, errLen, m);
  }

#if !OTA_USE_API
  // Multi-board assets can't be listed without the API; probe the slug URL
  // built from the versioned plain-asset path instead — 3xx means it
  // exists. 404 (today's single-asset releases) keeps the plain URL.
  {
    char slugUrl[240];
    const char* slash = strrchr(url, '/');
    if (slash) {
      int plen = (int)(slash - url) + 1;
      if (snprintf(slugUrl, sizeof(slugUrl), "%.*sfirmware-" OTA_BOARD_SLUG ".bin",
                   plen, url) < (int)sizeof(slugUrl)) {
        _prog("github: checking", -1);
        int c = _probeRedirect(slugUrl, nullptr, 0);
        if (c >= 300 && c < 400) snprintf(url, sizeof(url), "%s", slugUrl);
      }
    }
  }
#endif

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
    if (_sound) _sound(true);
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
