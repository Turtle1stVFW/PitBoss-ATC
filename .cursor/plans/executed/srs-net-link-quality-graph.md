# NET LINK quality graph (Fly tab) — executed

## Summary

Opt-in Fly-tab sparkline that samples every second while toggled on: SRS TCP connect RTT, local SRS UDP age, and (Client role only) ATC Host `GET /v1/health` RTT. Green/amber/accent when healthy; red spikes on errors. Renamed strip title to **NET LINK**.

## Decisions (locked)

- **Signals:** SRS TCP reachability/RTT to `srs_host:srs_port`, local SRS client UDP freshness, and Client-only ATC Host health RTT.
- **Activation:** small ON/OFF toggle beside the graph; 1 Hz sampling while on; stays on until turned off.
- **Y-axis:** RTT / latency-like ms. TCP/ATC use real RTT; UDP maps `age_s * 1000`.
- **Placement:** strip under Host/Client (`fly_net`) at top of Fly in `atc/flow_ui.py` `_build_fly`.
- **ATC probe:** `GET /v1/health` via `atc_net.atc_host_probe` (~0.8 s timeout); short status labels (`timeout` / `refused`); full `describe_connect_failure` text stays on the amber strip.

## Backend

- `atc/srs_radio.py`: `srs_tcp_probe`, ring buffer (~90 samples), `srs_link_record_sample` / `srs_link_snapshot`; optional `atc_host` / `atc_port` fills `atc_*` sample fields.
- `atc/atc_net.py`: `atc_host_probe`, `short_connect_failure`.

## Fly UI

- Toggle, status text, Canvas sparkline (TCP green, UDP accent, ATC amber).
- Probe work on background thread; UI updates via `_ui_call`.
- Also fixed pre-existing `_fly_upcoming_radio` 4-vs-5 unpack crash on Fly refresh.

## Docs

- `docs/ARCHITECTURE.md` — NET LINK / ATC Host probe notes.

## Addon (approved mid-feature)

Third amber **ATC** series for Client role so sporadic “host unreachable / packets never reached the Host” timeouts appear as red spikes on the same graph, distinct from SRS TCP.
