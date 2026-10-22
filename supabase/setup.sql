-- Run once in the Supabase SQL editor.  Safe to re-run.

create table if not exists public.readings (
  device_id  text   not null,
  boot_id    bigint not null,
  seq        bigint not null,
  ts         timestamptz,
  m1_mv int, m2_mv int, m3_mv int,
  m1_mad int, m2_mad int, m3_mad int,
  soil_t_c real, air_t_c real, rh_pct real, lux real,
  vbat_mv int, rssi int, flags int, fw text,
  created_at timestamptz not null default now(),
  primary key (device_id, boot_id, seq)
);

-- The ESP32 uses the publishable (anon) key: it may INSERT and nothing else.
grant insert on public.readings to anon;

-- The scheduled GitHub job uses the secret key (service_role): it needs to READ.
-- (Since late 2026 new tables are not exposed to the Data API unless granted explicitly.)
grant select on public.readings to service_role;

-- Row level security is currently OFF by choice (small private project).  Even so, the grants above mean the
-- publishable key can only insert.  Anyone who obtains that key from the firmware could still insert junk rows.
-- To harden later:
--   alter table public.readings enable row level security;
--   create policy "device can insert" on public.readings for insert to anon
--     with check ((current_setting('request.headers', true)::json ->> 'x-device-key') = 'SAME_AS_DEVICE_KEY_IN_secrets.h');
-- (the secret key bypasses RLS, so the GitHub job keeps working.)
