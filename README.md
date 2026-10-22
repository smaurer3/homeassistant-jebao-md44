# Home Assistant — Jebao MD-4.4 Dosing Pump

A **fully local** Home Assistant integration for the Jebao MD-4.4 4-channel
dosing pump. No cloud account, no internet connection, no vendor servers —
Home Assistant talks straight to the pump on your own network.

> Looking for the Jebao **MDP-20000 wavemaker**? Use
> [`jrigling/homeassistant-jebao`](https://github.com/jrigling/homeassistant-jebao)
> — this integration only handles the MD-4.4 doser.

## How it works

The pump speaks the GizWits LAN protocol on TCP port 12416. The integration:

1. **Finds the pump** by UDP broadcast on port 12414 — no IP to type in, and
   the reply identifies the exact model.
2. **Logs in locally.** The pump hands out its own passcode over the LAN, so
   no cloud-issued credential is involved.
3. **Receives changes as they happen.** The pump pushes a status report
   whenever anything changes — including changes you make in the phone app —
   so Home Assistant updates in about a second without polling. That makes
   this a `local_push` integration.
4. **Writes locally** over the same connection.

Polling only happens as a backstop in case a report is missed, and it is
local network traffic either way.

## What's exposed

For each MD-4.4 added:

- **Master power** switch
- **Channel 1..4** on/off switches
- **Timer 1..4** enable switches (whether the stored schedule runs)
- **Channel 1..4 schedule** — a single editable text entity per channel
  in the form `HH:MM=mL, HH:MM=mL, ...` (up to 24 entries)
- **Channel 1..4 interval** — "days to skip" between scheduled doses
- **Channel 1..4 next dose / schedule count** sensors
- **Calibration channel / value / device clock** sensors
- **Sync clock** button — push Home Assistant's local time to the pump's MCU
- **10x dose precision** toggle + calibration calculator helpers
- **Open circuit / UART fault** binary sensors

## Actions (services)

For day-to-day editing, the `Channel N schedule` text entity is the
easiest path — just type a new schedule into the entity. The actions below
are for automations that need to compute schedules dynamically (e.g. "bump
the morning dose by 0.1 mL if yesterday's pH was low").

| Action | What it does |
|---|---|
| `jebao_md44.set_schedule` | Overwrite a channel's full schedule (up to 24 entries). |
| `jebao_md44.set_schedule_slot` | Edit one entry, or append if the slot number is past the end. |
| `jebao_md44.delete_schedule_slot` | Remove one entry. |
| `jebao_md44.get_schedule` | Return the full schedule for one channel. Response action. |
| `jebao_md44.get_schedule_slot` | Return one entry. Response action — includes `exists: false` if the slot is empty. |

`device_id` is the Home Assistant device-registry ID, which the UI
device-picker fills in for you. If you're hand-writing YAML, you can find
it in Settings → Devices & Services → click your doser → the URL ends in
`.../device/<this-id>`. `channel` is 1..4. `slot` is 1-based and matches
the order entries appear in the schedule text entity (slot 1 = first entry).

If the **10x dose precision** toggle is on, all `quantity_ml` values in
these actions are real mL (e.g. `0.5`). With it off, they're whole mL.

### Example: overwrite Channel 1 with two daily doses

```yaml
action: jebao_md44.set_schedule
data:
  device_id: 4f8c92a1b3c4d5e6f7a8b9c0d1e2f3a4
  channel: 1
  entries:
    - hour: 9
      minute: 0
      quantity_ml: 0.3
    - hour: 21
      minute: 0
      quantity_ml: 0.3
```

### Example: edit one entry (e.g. bump tomorrow's morning dose)

```yaml
# Change Channel 1, slot 1, to 09:00 = 0.5 mL.
# If slot 1 doesn't exist yet it'll be appended.
action: jebao_md44.set_schedule_slot
data:
  device_id: 4f8c92a1b3c4d5e6f7a8b9c0d1e2f3a4
  channel: 1
  slot: 1
  hour: 9
  minute: 0
  quantity_ml: 0.5
```

> Tip: passing `quantity_ml: 0` to `set_schedule_slot` deletes that slot,
> so you only need to remember one action for normal edits.

### Example: delete a specific entry

```yaml
action: jebao_md44.delete_schedule_slot
data:
  device_id: 4f8c92a1b3c4d5e6f7a8b9c0d1e2f3a4
  channel: 1
  slot: 2
```

### Example: read a schedule into a variable

`get_schedule` and `get_schedule_slot` are *response actions*, so you
need `response_variable` to capture what they return:

```yaml
- action: jebao_md44.get_schedule
  data:
    device_id: 4f8c92a1b3c4d5e6f7a8b9c0d1e2f3a4
    channel: 1
  response_variable: ch1_schedule

- action: persistent_notification.create
  data:
    title: "Channel 1 schedule"
    message: >
      {{ ch1_schedule.entry_count }} entries:
      {% for e in ch1_schedule.entries %}
        - {{ e.time }} = {{ e.quantity_ml }} mL
      {% endfor %}
```

The response shape is:

```yaml
channel: 1
factor: 10              # 1 with 10x off, 10 with it on
entry_count: 2
entries:
  - slot: 1
    time: "09:00"
    hour: 9
    minute: 0
    quantity_ml: 0.3
  - slot: 2
    time: "21:00"
    hour: 21
    minute: 0
    quantity_ml: 0.3
```

### Example: read one slot and branch on whether it exists

```yaml
- action: jebao_md44.get_schedule_slot
  data:
    device_id: 4f8c92a1b3c4d5e6f7a8b9c0d1e2f3a4
    channel: 1
    slot: 1
  response_variable: slot1

- if:
    - condition: template
      value_template: "{{ slot1.exists }}"
  then:
    - action: notify.notify
      data:
        message: "Morning dose: {{ slot1.quantity_ml }} mL at {{ slot1.time }}"
  else:
    - action: notify.notify
      data:
        message: "Channel 1 has no schedule entries yet."
```

### Full example: nudge tomorrow's dose based on a sensor

```yaml
alias: pH-aware dose nudge
trigger:
  - platform: time
    at: "23:55:00"
condition:
  - condition: numeric_state
    entity_id: sensor.tank_ph_24h_avg
    below: 8.1
action:
  - action: jebao_md44.get_schedule_slot
    data:
      device_id: 4f8c92a1b3c4d5e6f7a8b9c0d1e2f3a4
      channel: 1
      slot: 1
    response_variable: morning

  - action: jebao_md44.set_schedule_slot
    data:
      device_id: 4f8c92a1b3c4d5e6f7a8b9c0d1e2f3a4
      channel: 1
      slot: 1
      hour: "{{ morning.hour }}"
      minute: "{{ morning.minute }}"
      quantity_ml: "{{ (morning.quantity_ml | float + 0.1) | round(1) }}"
```

## Installation

### HACS (custom repository)

1. HACS → Integrations → ⋮ → Custom repositories
2. Add `https://github.com/smaurer3/homeassistant-jebao-md44` as an
   "Integration"
3. Search HACS for "Jebao MD-4.4" and install
4. Restart Home Assistant
5. Settings → Devices & Services → Add Integration → "Jebao MD-4.4
   Dosing Pump"

### Manual

Copy `custom_components/jebao_md44/` into your Home Assistant
`config/custom_components/` directory and restart.

## Setup

Add the integration and it broadcasts for pumps on your network — pick
yours from the list and you're done. Nothing else to enter.

If nothing is found (usually because Home Assistant is on a different VLAN
or subnet to the pump) you'll be asked for the pump's IP address instead.
Either way, once configured the integration also watches DHCP, so the pump
is followed automatically if your router gives it a new address.

## Requirements

- The pump must be on your network and reachable on TCP port 12416.
- Home Assistant should ideally be on the same subnet, so UDP broadcast
  discovery works. It isn't essential — manual IP entry works fine.

The pump still needs to have been set up on your WiFi at least once using
the Jebao Aqua app (that's how it gets your WiFi credentials). After that,
this integration never contacts the vendor's servers, and the pump will
keep working locally even with no internet connection.

## Notes

- Tested against firmware `04X3000R` (ESP32-C3 module) on product key
  `1aa33c38ba9d4b78a9e7796705b2fad7` — "4-head doser WiFi_BLE". The closely
  related `3b181431...` declares the same datapoint set and is accepted; the
  layout is confirmed from the pump's reported buffer length at runtime.
  If your pump is rejected as unsupported, please open an issue with the
  product key from the log.
- All writes are sent in full every time — no "skip the write if the value
  didn't change" guards. That keeps the pump's state from drifting out of
  sync with Home Assistant.
- **The clock.** The pump fires its schedules against its own internal
  clock, which is set by writing `YMDData` / `HMSData`. Those fields are
  laid out as `[20, YY, MM, DD]` and `[0, HH, MM, SS]` — byte 0 of the date
  is the century and byte 0 of the time is reserved (it is *not* a weekday,
  despite appearances). They are a write-only latch: they hold whatever was
  last written and never tick, so the pump's running time cannot be read
  back. Consequently the **Device clock** sensor shows *when the clock was
  last set* — by this integration or by the official app — not the pump's
  current time. Press **Sync clock** after setup, and again if you ever
  suspect drift.

## Upgrading from 1.x

Version 1.x controlled the pump through the GizWits cloud. 2.0 is local-only,
and the entity IDs are unchanged, but the config entry format is different:
**remove the integration and add it again.** You will not need your Jebao
app credentials any more.

## Credits

The MDP-20000 wavemaker support in
[`jrigling/homeassistant-jebao`](https://github.com/jrigling/homeassistant-jebao)
was the original inspiration, and the base entity layer here started there.
