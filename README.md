# Home Assistant — Jebao MD-4.4 Dosing Pump

A Home Assistant integration for the Jebao MD-4.4 4-channel dosing pump,
controlled through the Gizwits cloud (same backend the official Jebao
Aqua mobile app uses).

> Looking for the Jebao **MDP-20000 wavemaker**? Use
> [`jrigling/homeassistant-jebao`](https://github.com/jrigling/homeassistant-jebao)
> — this integration only handles the doser.

## Why cloud-only

The MD-4.4 firmware accepts state reads over its LAN protocol on
TCP/12416 but silently drops control writes — the cloud is the only path
that actually changes the device. This integration:

1. Logs in to the Gizwits REST API with your Jebao Aqua app credentials,
2. Opens a WebSocket subscription for real-time state pushes (~1 s
   latency end-to-end, no polling), and
3. Sends control writes over REST. The cloud forwards them to the pump
   over its existing MQTT channel and pushes the resulting state back to
   us automatically, so the UI updates near-realtime.

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
- **Sync clock** button — push HA's local time to the pump's MCU
- **10x dose precision** toggle + calibration calculator helpers
- **Open circuit / UART fault** binary sensors

## Actions (services)

For day-to-day editing, the `Channel N schedule` text entity is the
easiest path — just type a new schedule into the entity and HA writes it
straight through. The actions below are for automations that need to
compute schedules dynamically (e.g. "bump the morning dose by 0.1 mL if
yesterday's pH was low").

Five actions are registered:

| Action | What it does |
|---|---|
| `jebao_md44.set_schedule` | Overwrite a channel's full schedule (up to 24 entries). |
| `jebao_md44.set_schedule_slot` | Edit one entry, or append if the slot number is past the end. |
| `jebao_md44.delete_schedule_slot` | Remove one entry. |
| `jebao_md44.get_schedule` | Return the full schedule for one channel. Response action. |
| `jebao_md44.get_schedule_slot` | Return one entry. Response action — includes `exists: false` if the slot is empty. |

`device_id` is the HA device-registry ID, which the UI device-picker
fills in for you. If you're hand-writing YAML, you can find it in
Settings → Devices & Services → click your doser → the URL ends in
`.../device/<this-id>`. `channel` is 1..4. `slot` is 1-based and matches
the order entries appear in the schedule text entity (slot 1 = first
entry shown).

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

A bare-bones "if pH was low yesterday, add 0.1 mL to tomorrow's morning
dose" automation that reads the current value, adjusts it, and writes
back:

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

## Setup flow

1. Enter your Gizwits Aqua app email + password + region (usually `us`)
2. Pick which doser on your account to add

That's it — the WebSocket subscription starts automatically and you
should see entities populate immediately.

## Notes

- Tested against current firmware on a US-region account. The Gizwits
  cloud attribute names this integration speaks to (`channe1`..`channe4`,
  `Timer1ON`..`Timer4ON`, `IntervalT1`..`IntervalT4`,
  `CH1SWTime`..`CH4SWTime`, etc.) are the same ones the cloud exposes
  publicly through its standard `/app/devdata/<did>/latest` and
  `/app/control/<did>` endpoints — anyone with a Gizwits Aqua account
  can see them.
- All writes are sent verbatim every time — no "skip the write if value
  didn't change" guards. This keeps the cloud-side cache from drifting
  out of sync with HA.
- The WS heartbeat is 180 s. Reconnects use exponential backoff
  (2 → 60 s). If the socket is down the integration falls back to REST
  polling at the configured interval.

## Credits

The MDP-20000 wavemaker support that lives in
[`jrigling/homeassistant-jebao`](https://github.com/jrigling/homeassistant-jebao)
was the original inspiration and the base entity layer here was lifted
from there.
