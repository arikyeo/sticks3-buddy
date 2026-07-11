#pragma once
#include <string.h>
#include <stddef.h>

// OTA release-tag extraction — pure string logic, native-testable
// (test/test_ota_url). The OTA flow probes
//   https://github.com/<owner>/<repo>/releases/latest/download/firmware.bin
// with redirects DISABLED and reads the 302 Location header, which GitHub
// shapes as .../releases/download/<tag>/firmware.bin. That tag IS the
// latest release version — no api.github.com call, no rate limit.

// Extracts the tag from a Location header value (absolute URL or bare
// path), strips a leading v/V, and copies it NUL-terminated into out.
// Returns false when the path shape is wrong, the tag is empty, or it
// wouldn't fit outLen — a truncated version must never reach the equality
// gate, so overflow fails instead of clipping.
inline bool otaParseTagFromLocation(const char* location, char* out, size_t outLen) {
  if (!location || !out || outLen == 0) return false;
  static const char MARK[] = "/releases/download/";
  const char* m = strstr(location, MARK);
  if (!m) return false;
  const char* tag = m + sizeof(MARK) - 1;
  const char* end = strchr(tag, '/');
  if (!end || end == tag) return false;      // need a tag AND a filename after it
  if (*tag == 'v' || *tag == 'V') tag++;
  if (end == tag) return false;              // tag was just "v"
  size_t len = (size_t)(end - tag);
  if (len >= outLen) return false;
  memcpy(out, tag, len);
  out[len] = 0;
  return true;
}
