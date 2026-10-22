/*
 * soil_node.ino  -  soil and environment monitoring node
 * Board    : ESP32 DevKit V1  (Arduino IDE -> "ESP32 Dev Module", default partition scheme)
 * Core     : arduino-esp32 2.0.x or 3.x
 * Libraries: OneWire, DallasTemperature, "DHT sensor library" (Adafruit),
 *            "Adafruit Unified Sensor".  Everything else ships with the ESP32 core.
 * Files    : this sketch + secrets.h (same folder)
 *
 * WHAT IT DOES (one cycle, then deep sleep):
 *   wake -> read battery (optional) -> sample all sensors (~2.5 s)
 *        -> store record in RTC memory -> connect hotspot -> NTP (if needed)
 *        -> POST backlog + new records as JSON to Supabase -> deep sleep until next 15-min mark
 *   If anything fails the records are kept (RTC memory, spilled to flash) and resent later.
 *
 * PINS:  Moisture VCC D25/D26/D27 (switched)   Moisture AO D34/D35/D36(VP)
 *        DS18B20 D13   DHT22 D14   BH1750 SDA D32 / SCL D33   Battery sense D39(VN), optional
 *
 * SUPABASE TABLE (run once in the SQL editor):
 *   create table public.readings (
 *     device_id text not null, boot_id bigint not null, seq bigint not null,
 *     ts timestamptz,
 *     m1_mv int, m2_mv int, m3_mv int, m1_mad int, m2_mad int, m3_mad int,
 *     soil_t_c real, air_t_c real, rh_pct real, lux real,
 *     vbat_mv int, rssi int, flags int, fw text,
 *     created_at timestamptz not null default now(),
 *     primary key (device_id, boot_id, seq)
 *   );
 *   grant insert on public.readings to anon;   -- needed: new projects do not expose tables by default
 *   alter table public.readings enable row level security;
 *   create policy "device can insert" on public.readings for insert to anon
 *     with check ((current_setting('request.headers', true)::json ->> 'x-device-key') = 'SAME_AS_DEVICE_KEY_IN_secrets.h');
 *
 * FLAGS (bit mask in each record):
 *   1 M1 suspicious   2 M2 suspicious   4 M3 suspicious   8 DS18B20 invalid
 *   16 DHT22 invalid  32 BH1750 invalid 64 BH1750 saturated
 *   128 timestamp estimated or missing  256 low battery   512 DHT22 had only one valid read
 */

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <LittleFS.h>
#include <Preferences.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <DHT.h>
#include <time.h>
#include <sys/time.h>
#include "esp_system.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "esp_sntp.h"
#include "driver/rtc_io.h"
#include "secrets.h"

// =====================================================================
//  CONFIGURATION
// =====================================================================
#define FW_VERSION              "1.0.1"
#define DEBUG_SERIAL            1       // 1 = print progress on Serial (115200); 0 for deployment

// ---- Pins ----
static const uint8_t PIN_MOIST_PWR[3] = {25, 26, 27};   // switched 3V3 for each moisture module
static const uint8_t PIN_MOIST_AO[3]  = {34, 35, 36};   // ADC1 inputs (36 = VP)
static const uint8_t PIN_DS18B20      = 13;
static const uint8_t PIN_DHT          = 14;
static const uint8_t PIN_SDA          = 32;
static const uint8_t PIN_SCL          = 33;
static const uint8_t PIN_VBAT         = 39;             // VN, only used if ENABLE_VBAT
static const uint8_t BH1750_ADDR      = 0x23;

// ---- Schedule ----
#define CYCLE_S                 900     // wake period in seconds (use 60 for bench testing)
#define ALIGN_TO_GRID           1       // wake on wall-clock marks (:00 :15 :30 :45) when time is known
#define UPLOAD_EVERY_N_CYCLES   1       // 1 = upload every wake; 4 = hourly batches (saves energy)
#define NTP_RESYNC_S            3600    // re-sync the clock at most this often
#define MAX_AWAKE_MS            60000   // hard cap: watchdog forces deep sleep after this
#define UPLOAD_MARGIN_MS        12000   // stop starting new uploads this close to the cap
#define CPU_MHZ                 80      // lower clock = less awake current (set 240 if a sensor misbehaves)

// ---- Sampling ----
#define MOIST_SETTLE_MS         300     // wait after switching a module on
#define MOIST_SAMPLES           15      // ADC reads per probe; median and MAD are stored
#define MOIST_MV_MIN_VALID      200     // below this the channel is flagged suspicious
#define BH_SAMPLES              3       // BH1750 one-shot reads (median)
#define DHT_MIN_GAP_MS          2100    // DHT22 needs >= 2 s between reads

// ---- Storage ----
#define RTC_CAP                 48      // records held in RTC memory (12 h at 15 min)
#define RTC_SPILL_AT            40      // move RTC records to flash when this many are waiting
#define BATCH_MAX               24      // records per HTTP POST
#define BACKLOG_PATH            "/backlog.bin"
#define FS_MIN_FREE_BYTES       32768   // if flash gets this full, the backlog is discarded

// ---- Network ----
#define WIFI_FAST_TIMEOUT_MS    5000    // try cached BSSID/channel first
#define WIFI_FULL_TIMEOUT_MS    12000   // then a normal scan-and-join
#define NTP_TIMEOUT_MS          8000
#define HTTP_CONNECT_TIMEOUT_MS 6000
#define HTTP_TIMEOUT_MS         8000
#define TLS_VERIFY              0       // 0 = no certificate check (bring-up only!), 1 = verify ROOT_CA_PEM
#define USE_STATIC_IP           0       // 1 = skip DHCP (needs STATIC_* in secrets.h)
#define FAIL_STREAK_BACKOFF     3       // after this many failed uploads, retry only every 4th cycle

// ---- Battery (optional divider on D39) ----
#define ENABLE_VBAT             0       // set 1 only after the divider is wired
#define VBAT_DIV_RATIO          2.0f    // (R1+R2)/R2 for 100k/100k
#define VBAT_LOW_MV             3500    // below: keep sampling, skip Wi-Fi, stretch the cycle
#define VBAT_CRIT_MV            3300    // below: do nothing, sleep long
#define LOW_BATT_CYCLE_MULT     4
#define CRIT_BATT_SLEEP_S       21600

// =====================================================================
//  DATA TYPES
// =====================================================================
#if DEBUG_SERIAL
  #define LOG(...) Serial.printf(__VA_ARGS__)
#else
  #define LOG(...) do {} while (0)
#endif

#define MIN_VALID_EPOCH 1700000000UL
#define U16_BAD         0xFFFF
#define I16_BAD         0x7FFF

#define F_M_BAD(i)      (1u << (i))     // bits 0..2
#define F_DS_BAD        (1u << 3)
#define F_DHT_BAD       (1u << 4)
#define F_BH_BAD        (1u << 5)
#define F_BH_SAT        (1u << 6)
#define F_TS_UNSYNCED   (1u << 7)
#define F_LOW_BATT      (1u << 8)
#define F_DHT_ONE_READ  (1u << 9)

struct Record {                 // one sample, 36 bytes, naturally aligned
  uint32_t boot_id;
  uint32_t seq;
  uint32_t ts;                  // unix seconds (UTC), 0 = clock was not synced
  float    lux;                 // NAN = invalid
  uint16_t m_mv[3];             // moisture median, millivolts
  int16_t  soil_t_c100;         // DS18B20, degC x100, I16_BAD = invalid
  int16_t  air_t_c100;          // DHT22 temperature, degC x100
  uint16_t rh_pct100;           // DHT22 humidity, % x100, U16_BAD = invalid
  uint16_t vbat_mv;             // 0 = not measured
  uint16_t flags;
  uint8_t  m_mad[3];            // moisture median absolute deviation, mV (capped at 255)
  int8_t   rssi;                // RSSI of the previous successful Wi-Fi connection, 0 = unknown
};
static_assert(sizeof(Record) == 36, "Record layout changed: also change BACKLOG_MAGIC");

static const uint32_t BACKLOG_MAGIC = 0x50534D31;   // file header; change when Record changes

// =====================================================================
//  STATE THAT SURVIVES DEEP SLEEP (RTC slow memory, lost on power loss)
// =====================================================================
#define RTC_MAGIC 0x50534D32
RTC_DATA_ATTR uint32_t rtc_magic;
RTC_DATA_ATTR uint32_t rtc_boot_id;        // random per cold start; with seq makes records unique
RTC_DATA_ATTR uint32_t rtc_seq;            // next sequence number
RTC_DATA_ATTR uint32_t rtc_cycle_counter;
RTC_DATA_ATTR uint32_t rtc_last_ntp;       // epoch of last NTP sync
RTC_DATA_ATTR uint16_t rtc_count;          // records waiting in rtc_buf
RTC_DATA_ATTR uint8_t  rtc_fail_streak;
RTC_DATA_ATTR bool     rtc_wifi_valid;
RTC_DATA_ATTR uint8_t  rtc_bssid[6];
RTC_DATA_ATTR int32_t  rtc_channel;
RTC_DATA_ATTR int8_t   rtc_last_rssi;
RTC_DATA_ATTR Record   rtc_buf[RTC_CAP];

// =====================================================================
//  GLOBALS
// =====================================================================
static uint32_t t_wake_ms = 0;
static volatile bool g_ntp_done = false;
static bool fsReady = false;

static OneWire oneWire(PIN_DS18B20);
static DallasTemperature ds(&oneWire);
static DHT dht(PIN_DHT, DHT22);

static void goToSleep(uint32_t cycle_s);

// =====================================================================
//  SMALL HELPERS
// =====================================================================
static bool clockValid() { return time(nullptr) > (time_t)MIN_VALID_EPOCH; }

static uint32_t medianU32(uint32_t *a, int n) {         // sorts a[] in place
  for (int i = 1; i < n; i++) {
    uint32_t k = a[i]; int j = i - 1;
    while (j >= 0 && a[j] > k) { a[j + 1] = a[j]; j--; }
    a[j + 1] = k;
  }
  return a[n / 2];
}

static float medianF(float *a, int n) {                 // sorts a[] in place
  for (int i = 1; i < n; i++) {
    float k = a[i]; int j = i - 1;
    while (j >= 0 && a[j] > k) { a[j + 1] = a[j]; j--; }
    a[j + 1] = k;
  }
  return a[n / 2];
}

// =====================================================================
//  POWER MANAGEMENT
// =====================================================================
// Safety net: if anything hangs, force deep sleep after MAX_AWAKE_MS.
static void moistPinsHoldLow();
static void awakeWatchdogCb(void *) {
  moistPinsHoldLow();
  esp_sleep_enable_timer_wakeup((uint64_t)CYCLE_S * 1000000ULL);
  esp_deep_sleep_start();
}

static void armAwakeWatchdog() {
  static esp_timer_handle_t h = nullptr;
  esp_timer_create_args_t a = {};
  a.callback = &awakeWatchdogCb;
  a.name = "awake_wd";
  if (esp_timer_create(&a, &h) == ESP_OK) esp_timer_start_once(h, (uint64_t)MAX_AWAKE_MS * 1000ULL);
}

// Moisture modules are powered only while measuring. Before sleeping, pin them low and
// latch the pad (RTC GPIO hold) so they stay off. Verify with a multimeter (see notes).
static void moistPinsHoldLow() {
  for (int i = 0; i < 3; i++) {
    gpio_num_t g = (gpio_num_t)PIN_MOIST_PWR[i];
    digitalWrite(PIN_MOIST_PWR[i], LOW);
    rtc_gpio_init(g);
    rtc_gpio_set_direction(g, RTC_GPIO_MODE_OUTPUT_ONLY);
    rtc_gpio_set_level(g, 0);
    rtc_gpio_hold_en(g);
  }
}

static void moistPinsInit() {
  for (int i = 0; i < 3; i++) {
    gpio_num_t g = (gpio_num_t)PIN_MOIST_PWR[i];
    rtc_gpio_hold_dis(g);
    rtc_gpio_deinit(g);
    pinMode(PIN_MOIST_PWR[i], OUTPUT);
    digitalWrite(PIN_MOIST_PWR[i], LOW);
  }
}

#if ENABLE_VBAT
static uint16_t readVbatMv() {
  uint32_t acc = 0;
  for (int i = 0; i < 16; i++) { acc += analogReadMilliVolts(PIN_VBAT); delay(2); }
  float mv = (acc / 16.0f) * VBAT_DIV_RATIO;
  if (mv < 0) mv = 0;
  if (mv > 65535) mv = 65535;
  return (uint16_t)mv;
}
#endif

static uint64_t sleepMicros(uint32_t cycle_s) {
  if (ALIGN_TO_GRID && clockValid()) {                  // wake on the next wall-clock mark
    uint32_t now = (uint32_t)time(nullptr);
    uint32_t next = (now / cycle_s + 1) * cycle_s;
    uint32_t d = next - now;
    if (d < 30) d += cycle_s;                           // too close to the mark: take the next one
    return (uint64_t)d * 1000000ULL;
  }
  int64_t ms = (int64_t)cycle_s * 1000 - (int64_t)(millis() - t_wake_ms);
  if (ms < 10000) ms = 10000;
  return (uint64_t)ms * 1000ULL;
}

static void goToSleep(uint32_t cycle_s) {
  uint64_t us = sleepMicros(cycle_s);
  LOG("Deep sleep for %llu s\n", (unsigned long long)(us / 1000000ULL));
  moistPinsHoldLow();
  esp_sleep_enable_timer_wakeup(us);
#if DEBUG_SERIAL
  Serial.flush();
#endif
  esp_deep_sleep_start();
}

// =====================================================================
//  SENSING
// =====================================================================
static void sampleMoisture(int i, uint16_t &mv, uint8_t &mad, bool &ok) {
  uint32_t s[MOIST_SAMPLES], d[MOIST_SAMPLES];
  digitalWrite(PIN_MOIST_PWR[i], HIGH);                 // one module at a time: probes share soil
  delay(MOIST_SETTLE_MS);
  for (int k = 0; k < MOIST_SAMPLES; k++) { s[k] = analogReadMilliVolts(PIN_MOIST_AO[i]); delay(2); }
  digitalWrite(PIN_MOIST_PWR[i], LOW);
  uint32_t med = medianU32(s, MOIST_SAMPLES);
  for (int k = 0; k < MOIST_SAMPLES; k++) d[k] = (s[k] > med) ? s[k] - med : med - s[k];
  uint32_t m = medianU32(d, MOIST_SAMPLES);
  mv  = (uint16_t)(med > 65534 ? 65534 : med);
  mad = (uint8_t)(m > 255 ? 255 : m);
  ok  = med >= MOIST_MV_MIN_VALID;
}

static bool bhReadOnce(float &lux, bool &sat) {
  Wire.beginTransmission(BH1750_ADDR); Wire.write(0x01);               // power on
  if (Wire.endTransmission() != 0) return false;
  Wire.beginTransmission(BH1750_ADDR); Wire.write(0x20);               // one-shot, 1 lx resolution
  if (Wire.endTransmission() != 0) return false;
  delay(180);                                                           // max conversion time
  if (Wire.requestFrom(BH1750_ADDR, (uint8_t)2) != 2) return false;
  uint8_t hi = Wire.read();                                             // read in two statements:
  uint8_t lo = Wire.read();                                             // evaluation order matters
  uint16_t raw = ((uint16_t)hi << 8) | lo;
  sat = (raw == 0xFFFF);
  lux = raw / 1.2f;
  return true;
}

static bool dhtValid(float t, float h) {
  return !isnan(t) && !isnan(h) && t > -40.0f && t < 80.0f && h >= 0.0f && h <= 100.0f;
}

static void sampleAll(Record &r) {
  uint16_t fl = 0;
  memset(&r, 0, sizeof r);
  for (int i = 0; i < 3; i++) r.m_mv[i] = U16_BAD;
  r.soil_t_c100 = I16_BAD; r.air_t_c100 = I16_BAD; r.rh_pct100 = U16_BAD; r.lux = NAN;

  // 1) DS18B20: start a 12-bit conversion; it finishes in the background (~750 ms)
  ds.begin();
  bool dsPresent = ds.getDeviceCount() > 0;
  uint32_t t_req = millis();
  if (dsPresent) { ds.setResolution(12); ds.setWaitForConversion(false); ds.requestTemperatures(); }

  // 2) DHT22 read #1
  dht.begin();
  float t1 = dht.readTemperature();
  float h1 = dht.readHumidity();
  uint32_t t_dht1 = millis();

  // 3) Moisture, one module at a time
  for (int i = 0; i < 3; i++) {
    uint16_t mv; uint8_t mad; bool ok;
    sampleMoisture(i, mv, mad, ok);
    r.m_mv[i] = mv; r.m_mad[i] = mad;
    if (!ok) fl |= F_M_BAD(i);
  }

  // 4) BH1750: median of a few one-shot reads
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(100000);
  float lx[BH_SAMPLES]; int nlx = 0; bool sat = false;
  for (int k = 0; k < BH_SAMPLES; k++) {
    float v; bool s;
    if (bhReadOnce(v, s)) { lx[nlx++] = v; if (s) sat = true; }
  }
  if (nlx > 0) r.lux = medianF(lx, nlx); else fl |= F_BH_BAD;
  if (sat) fl |= F_BH_SAT;
  Wire.end();

  // 5) DS18B20 result
  if (dsPresent) {
    while (millis() - t_req < 800) delay(10);
    float t = ds.getTempCByIndex(0);
    if (t > -55.0f && t < 125.0f && t != DEVICE_DISCONNECTED_C && t != 85.0f) r.soil_t_c100 = (int16_t)lroundf(t * 100.0f);
    else fl |= F_DS_BAD;
  } else fl |= F_DS_BAD;

  // 6) DHT22 read #2 (the sensor needs >= 2 s since the first read), then combine
  while (millis() - t_dht1 < DHT_MIN_GAP_MS) delay(10);
  float t2 = dht.readTemperature();
  float h2 = dht.readHumidity();
  bool v1 = dhtValid(t1, h1), v2 = dhtValid(t2, h2);
  float t = NAN, h = NAN;
  if (v1 && v2) { t = (t1 + t2) / 2; h = (h1 + h2) / 2; }
  else if (v1)  { t = t1; h = h1; fl |= F_DHT_ONE_READ; }
  else if (v2)  { t = t2; h = h2; fl |= F_DHT_ONE_READ; }
  else fl |= F_DHT_BAD;
  if (!isnan(t)) { r.air_t_c100 = (int16_t)lroundf(t * 100.0f); r.rh_pct100 = (uint16_t)lroundf(h * 100.0f); }

  r.flags = fl;
}

static void logRecord(const Record &r) {
  LOG("seq %lu  M[mV]=%u/%u/%u (mad %u/%u/%u)  soil=%.2fC  air=%.2fC  rh=%.1f%%  lux=%.1f  flags=0x%03X\n",
      (unsigned long)r.seq, r.m_mv[0], r.m_mv[1], r.m_mv[2], r.m_mad[0], r.m_mad[1], r.m_mad[2],
      r.soil_t_c100 == I16_BAD ? NAN : r.soil_t_c100 / 100.0,
      r.air_t_c100 == I16_BAD ? NAN : r.air_t_c100 / 100.0,
      r.rh_pct100 == U16_BAD ? NAN : r.rh_pct100 / 100.0,
      (double)r.lux, (unsigned)r.flags);
}

// =====================================================================
//  TIME AND WI-FI
// =====================================================================
static void ntpCb(struct timeval *) { g_ntp_done = true; }

static bool syncTime() {
  g_ntp_done = false;
  sntp_set_time_sync_notification_cb(ntpCb);
  configTime(0, 0, "pool.ntp.org", "time.google.com", "time.cloudflare.com");   // UTC; the browser localizes
  uint32_t t0 = millis();
  while (!g_ntp_done && millis() - t0 < NTP_TIMEOUT_MS) delay(20);
  if (g_ntp_done) { rtc_last_ntp = (uint32_t)time(nullptr); LOG("NTP ok (%lu)\n", (unsigned long)rtc_last_ntp); }
  else LOG("NTP timeout\n");
  return g_ntp_done;
}

static bool waitWifi(uint32_t ms) {
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < ms) delay(50);
  return WiFi.status() == WL_CONNECTED;
}

static bool wifiConnect() {
  WiFi.persistent(false);                               // no flash writes for credentials
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(false);
#if USE_STATIC_IP
  IPAddress ip, gw, mask, dns;
  ip.fromString(STATIC_IP); gw.fromString(STATIC_GATEWAY);
  mask.fromString(STATIC_SUBNET); dns.fromString(STATIC_DNS);
  WiFi.config(ip, gw, mask, dns);
#endif
  bool ok = false;
  if (rtc_wifi_valid) {                                 // fast path: skip the channel scan
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD, rtc_channel, rtc_bssid, true);
    ok = waitWifi(WIFI_FAST_TIMEOUT_MS);
    if (!ok) { WiFi.disconnect(false, false); delay(100); }
  }
  if (!ok) {                                            // slow path: normal scan and join
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    ok = waitWifi(WIFI_FULL_TIMEOUT_MS);
  }
  if (ok) {
    memcpy(rtc_bssid, WiFi.BSSID(), 6);
    rtc_channel = WiFi.channel();
    rtc_wifi_valid = true;
    rtc_last_rssi = (int8_t)WiFi.RSSI();
    LOG("Wi-Fi up, ch %ld, rssi %d, ip %s\n", (long)rtc_channel, (int)rtc_last_rssi, WiFi.localIP().toString().c_str());
  } else {
    rtc_wifi_valid = false;
    LOG("Wi-Fi failed\n");
  }
  return ok;
}

// =====================================================================
//  STORAGE: RTC buffer first, flash backlog when needed
// =====================================================================
static void rtcInit() {
  if (rtc_magic == RTC_MAGIC) return;
  memset(rtc_buf, 0, sizeof rtc_buf);
  rtc_magic = RTC_MAGIC;
  rtc_boot_id = esp_random();
  rtc_seq = 0; rtc_cycle_counter = 0; rtc_last_ntp = 0; rtc_count = 0;
  rtc_fail_streak = 0; rtc_wifi_valid = false; rtc_channel = 0; rtc_last_rssi = 0;
  LOG("Cold start: RTC state initialised, boot_id %lu\n", (unsigned long)rtc_boot_id);
}

static bool fsInit() {
  if (!fsReady) fsReady = LittleFS.begin(true);         // formats on first use
  return fsReady;
}

static void setBacklogOffset(uint32_t off) {
  Preferences p; p.begin("node", false); p.putUInt("off", off); p.end();
}

static bool spillRtcToFlash() {
  if (rtc_count == 0) return true;
  if (!fsInit()) { LOG("LittleFS unavailable\n"); return false; }
  if (LittleFS.totalBytes() - LittleFS.usedBytes() < FS_MIN_FREE_BYTES) {
    LOG("Flash nearly full: discarding old backlog\n");
    LittleFS.remove(BACKLOG_PATH);
    setBacklogOffset(4);
  }
  bool fresh = !LittleFS.exists(BACKLOG_PATH);
  if (!fresh) {                                          // detect a damaged file before appending
    File c = LittleFS.open(BACKLOG_PATH, FILE_READ);
    size_t sz = c ? c.size() : 0;
    if (c) c.close();
    if (sz < 4 || ((sz - 4) % sizeof(Record)) != 0) {
      LOG("Backlog damaged: starting a new file\n");
      LittleFS.remove(BACKLOG_PATH);
      fresh = true;
    }
  }
  File f = LittleFS.open(BACKLOG_PATH, fresh ? FILE_WRITE : FILE_APPEND);
  if (!f) return false;
  if (fresh) { uint32_t m = BACKLOG_MAGIC; f.write((const uint8_t *)&m, 4); setBacklogOffset(4); }
  size_t bytes = (size_t)rtc_count * sizeof(Record);
  size_t w = f.write((const uint8_t *)rtc_buf, bytes);
  f.close();
  if (w != bytes) { LOG("Flash write incomplete\n"); return false; }   // keep RTC copy
  LOG("Spilled %u record(s) to flash\n", rtc_count);
  rtc_count = 0;
  return true;
}

static void pushRecord(const Record &r) {
  if (rtc_count >= RTC_CAP) spillRtcToFlash();
  if (rtc_count >= RTC_CAP) {                            // flash failed too: drop the oldest
    memmove(rtc_buf, rtc_buf + 1, (RTC_CAP - 1) * sizeof(Record));
    rtc_count = RTC_CAP - 1;
    LOG("Buffer full: dropped oldest record\n");
  }
  rtc_buf[rtc_count++] = r;
}

// =====================================================================
//  JSON AND UPLOAD
// =====================================================================
static const char *fmtF(char *buf, size_t n, float v, int dec) {
  if (isnan(v)) return "null";
  snprintf(buf, n, "%.*f", dec, (double)v);
  return buf;
}
static const char *fmtI(char *buf, size_t n, long v, bool valid) {
  if (!valid) return "null";
  snprintf(buf, n, "%ld", v);
  return buf;
}

static String buildJson(const Record *recs, uint32_t n) {
  String s;
  s.reserve(n * 340 + 8);
  s += '[';
  time_t now = time(nullptr);
  bool nowOk = now > (time_t)MIN_VALID_EPOCH;
  char tsbuf[32], b[10][16], line[460];
  for (uint32_t i = 0; i < n; i++) {
    const Record &r = recs[i];
    uint16_t flags = r.flags;
    time_t ts = (time_t)r.ts;
    if (ts == 0) {                                       // sampled before the clock was synced
      flags |= F_TS_UNSYNCED;
      if (nowOk && r.boot_id == rtc_boot_id && rtc_seq > r.seq)
        ts = now - (time_t)(rtc_seq - 1 - r.seq) * CYCLE_S;   // estimate from sequence number
    }
    if (ts > 0) {
      struct tm tmv; gmtime_r(&ts, &tmv);
      strftime(tsbuf, sizeof tsbuf, "\"%Y-%m-%dT%H:%M:%SZ\"", &tmv);
    } else strcpy(tsbuf, "null");

    float soil = (r.soil_t_c100 == I16_BAD) ? NAN : r.soil_t_c100 / 100.0f;
    float air  = (r.air_t_c100  == I16_BAD) ? NAN : r.air_t_c100  / 100.0f;
    float rh   = (r.rh_pct100   == U16_BAD) ? NAN : r.rh_pct100   / 100.0f;

    snprintf(line, sizeof line,
      "{\"device_id\":\"%s\",\"boot_id\":%lu,\"seq\":%lu,\"ts\":%s,"
      "\"m1_mv\":%s,\"m2_mv\":%s,\"m3_mv\":%s,\"m1_mad\":%u,\"m2_mad\":%u,\"m3_mad\":%u,"
      "\"soil_t_c\":%s,\"air_t_c\":%s,\"rh_pct\":%s,\"lux\":%s,"
      "\"vbat_mv\":%s,\"rssi\":%s,\"flags\":%u,\"fw\":\"%s\"}",
      DEVICE_ID, (unsigned long)r.boot_id, (unsigned long)r.seq, tsbuf,
      fmtI(b[0], 16, r.m_mv[0], r.m_mv[0] != U16_BAD),
      fmtI(b[1], 16, r.m_mv[1], r.m_mv[1] != U16_BAD),
      fmtI(b[2], 16, r.m_mv[2], r.m_mv[2] != U16_BAD),
      (unsigned)r.m_mad[0], (unsigned)r.m_mad[1], (unsigned)r.m_mad[2],
      fmtF(b[3], 16, soil, 2), fmtF(b[4], 16, air, 2), fmtF(b[5], 16, rh, 2), fmtF(b[6], 16, r.lux, 1),
      fmtI(b[7], 16, r.vbat_mv, r.vbat_mv != 0),
      fmtI(b[8], 16, r.rssi, r.rssi != 0),
      (unsigned)flags, FW_VERSION);
    if (i) s += ',';
    s += line;
  }
  s += ']';
  return s;
}

static bool postBatch(const Record *recs, uint32_t n) {
  String body = buildJson(recs, n);
  String url = String(SUPABASE_URL) + "/rest/v1/" + SUPABASE_TABLE + "?on_conflict=device_id,boot_id,seq";

  WiFiClientSecure client;
#if TLS_VERIFY
  client.setCACert(ROOT_CA_PEM);
#else
  client.setInsecure();
#endif
  HTTPClient http;
  http.setConnectTimeout(HTTP_CONNECT_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(client, url)) { LOG("http.begin failed\n"); return false; }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("apikey", SUPABASE_KEY);
  http.addHeader("x-device-key", DEVICE_KEY);            // checked by the insert policy in Supabase
  http.addHeader("Authorization", String("Bearer ") + SUPABASE_KEY);
  http.addHeader("Prefer", "resolution=ignore-duplicates,return=minimal");   // resends are harmless
  int code = http.POST(body);
  bool ok = code >= 200 && code < 300;
  if (!ok) LOG("POST failed: %d %s\n", code, http.getString().substring(0, 160).c_str());
  else     LOG("POST ok: %lu record(s), HTTP %d\n", (unsigned long)n, code);
  http.end();
  return ok;
}

static bool uploadBacklog() {                            // true when the flash backlog is empty
  if (!fsInit() || !LittleFS.exists(BACKLOG_PATH)) return true;
  Preferences p; p.begin("node", false);
  uint32_t off = p.getUInt("off", 4);
  File f = LittleFS.open(BACKLOG_PATH, FILE_READ);
  if (!f) { p.end(); return false; }
  uint32_t size = f.size();
  uint32_t magic = 0;
  f.read((uint8_t *)&magic, 4);
  if (magic != BACKLOG_MAGIC || size < 4 || ((size - 4) % sizeof(Record)) != 0) {
    LOG("Backlog unreadable: removed\n");
    f.close(); LittleFS.remove(BACKLOG_PATH); p.putUInt("off", 4); p.end();
    return true;
  }
  if (off < 4 || off > size || ((off - 4) % sizeof(Record)) != 0) off = 4;

  Record batch[BATCH_MAX];
  bool ok = true;
  while (off < size) {
    if (millis() - t_wake_ms > (uint32_t)(MAX_AWAKE_MS - UPLOAD_MARGIN_MS)) { ok = false; break; }
    uint32_t n = (size - off) / sizeof(Record);
    if (n > BATCH_MAX) n = BATCH_MAX;
    f.seek(off);
    if (f.read((uint8_t *)batch, n * sizeof(Record)) != n * sizeof(Record)) { ok = false; break; }
    if (!postBatch(batch, n)) { ok = false; break; }
    off += n * sizeof(Record);
    p.putUInt("off", off);                               // progress survives a power cut
  }
  f.close();
  if (ok && off >= size) { LittleFS.remove(BACKLOG_PATH); p.putUInt("off", 4); LOG("Backlog cleared\n"); }
  p.end();
  return ok && off >= size;
}

static bool uploadRtc() {                                // true when the RTC buffer is empty
  uint16_t sent = 0;
  while (sent < rtc_count) {
    if (millis() - t_wake_ms > (uint32_t)(MAX_AWAKE_MS - UPLOAD_MARGIN_MS)) break;
    uint32_t n = rtc_count - sent;
    if (n > BATCH_MAX) n = BATCH_MAX;
    if (!postBatch(&rtc_buf[sent], n)) break;
    sent += n;
  }
  if (sent > 0 && sent < rtc_count) memmove(rtc_buf, rtc_buf + sent, (rtc_count - sent) * sizeof(Record));
  rtc_count -= sent;
  return rtc_count == 0;
}

static bool uploadAll() {
  if (!wifiConnect()) return false;
  if (!clockValid() || (uint32_t)time(nullptr) - rtc_last_ntp >= NTP_RESYNC_S) syncTime();
  bool ok = uploadBacklog();                             // oldest data first
  if (ok) ok = uploadRtc();
  WiFi.disconnect(true, false);
  WiFi.mode(WIFI_OFF);
  return ok;
}

// =====================================================================
//  MAIN
// =====================================================================
void setup() {
  t_wake_ms = millis();
  setCpuFrequencyMhz(CPU_MHZ);
#if DEBUG_SERIAL
  Serial.begin(115200);
  delay(50);
#endif
  armAwakeWatchdog();
  rtcInit();
  moistPinsInit();
  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);                        // ~0-3.1 V input range

  LOG("\n== soil_node %s | wake cause %d | boot_id %lu | next seq %lu | buffered %u ==\n",
      FW_VERSION, (int)esp_sleep_get_wakeup_cause(), (unsigned long)rtc_boot_id,
      (unsigned long)rtc_seq, rtc_count);

  uint16_t vbat = 0;
#if ENABLE_VBAT
  vbat = readVbatMv();
  LOG("Battery %u mV\n", vbat);
  if (vbat < VBAT_CRIT_MV) { LOG("Battery critical: sleeping long\n"); goToSleep(CRIT_BATT_SLEEP_S); }
#endif
  bool lowBatt = (vbat != 0 && vbat < VBAT_LOW_MV);

  // ---- measure ----
  Record r;
  sampleAll(r);
  r.boot_id = rtc_boot_id;
  r.seq = rtc_seq++;
  time_t now = time(nullptr);
  r.ts = (now > (time_t)MIN_VALID_EPOCH) ? (uint32_t)now : 0;
  r.vbat_mv = vbat;
  r.rssi = rtc_last_rssi;
  if (lowBatt) r.flags |= F_LOW_BATT;
  if (r.ts == 0) r.flags |= F_TS_UNSYNCED;
  logRecord(r);
  pushRecord(r);
  rtc_cycle_counter++;

  // ---- decide whether to upload this cycle ----
  bool due     = (rtc_cycle_counter % UPLOAD_EVERY_N_CYCLES == 0) || (rtc_count >= RTC_SPILL_AT);
  bool backoff = (rtc_fail_streak >= FAIL_STREAK_BACKOFF) && ((rtc_cycle_counter % 4) != 0);
  bool attempt = due && !lowBatt && !backoff;
  bool uploaded = false;
  if (attempt) {
    uploaded = uploadAll();
    if (uploaded) rtc_fail_streak = 0;
    else if (rtc_fail_streak < 255) rtc_fail_streak++;
  }
  // ---- make sure unsent data is safe in flash before sleeping ----
  if (!uploaded && (attempt || rtc_count >= RTC_SPILL_AT)) spillRtcToFlash();

  goToSleep(lowBatt ? (uint32_t)CYCLE_S * LOW_BATT_CYCLE_MULT : (uint32_t)CYCLE_S);
}

void loop() {
  delay(1000);                                           // never reached: setup() ends in deep sleep
}
