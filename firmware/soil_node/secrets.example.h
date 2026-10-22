// Copy this file to secrets.h (same folder) and fill it in. secrets.h is git-ignored.

#pragma once

// ---------------- Wi-Fi (the hotspot the node will join) ----------------
#define WIFI_SSID        "YOUR_HOTSPOT_SSID"
#define WIFI_PASSWORD    "YOUR_HOTSPOT_PASSWORD"

// Only used when USE_STATIC_IP is 1 in soil_node.ino
#define STATIC_IP        "192.168.0.50"
#define STATIC_GATEWAY   "192.168.0.1"
#define STATIC_SUBNET    "255.255.255.0"
#define STATIC_DNS       "8.8.8.8"

// ---------------- Supabase ----------------
// Project URL, no trailing slash, e.g. https://abcdefghijklmnop.supabase.co
#define SUPABASE_URL     "https://YOUR_PROJECT_REF.supabase.co"
// Publishable (sb_publishable_...) or legacy anon key. Never the service/secret key.
#define SUPABASE_KEY     "YOUR_PUBLISHABLE_OR_ANON_KEY"
#define SUPABASE_TABLE   "readings"
// A long random string. The insert policy checks it, so the public dashboard key cannot insert rows.
#define DEVICE_KEY       "CHOOSE_A_LONG_RANDOM_STRING"

// ---------------- Device identity ----------------
#define DEVICE_ID        "node-01"

// ---------------- TLS root certificate ----------------
// Used only when TLS_VERIFY is 1 in soil_node.ino.
// Get the chain with:  openssl s_client -connect YOUR_PROJECT_REF.supabase.co:443 -showcerts
// and paste the ROOT certificate of that chain (the last one, or its issuer's root) below.
static const char ROOT_CA_PEM[] PROGMEM = R"PEM(
-----BEGIN CERTIFICATE-----
PASTE_ROOT_CA_CERTIFICATE_HERE
-----END CERTIFICATE-----
)PEM";
