"""
Regression check: frequency gate matches ANY tuned radio, not only PTT.

Last-night failure mode: keyed intra-flight VHF (132.65) so SRS `selected`
was VHF, and UHF ATC / C2 triggers were blocked even though COMM1 was still
on Blackjack.

Run: py -3 check_freq_gate.py
"""

from __future__ import annotations

import json

import atc_phrase
import srs_radio

AIRPORT = atc_phrase.load_json(atc_phrase.AIRPORTS_PATH)["nellis"]
BLACKJACK_MHZ = 377.8
VHF_MHZ = 132.65
BANDSAW_MHZ = 378.225


def _ingest_vhf_keyed_uhf_tuned() -> None:
    """SRS CombinedRadioState: COMM1 UHF Blackjack, COMM2 VHF keyed."""
    payload = {
        "RadioInfo": {
            "name": "Fleece",
            "selected": 2,
            "radios": [
                {"freq": 0, "modulation": 3},  # intercom
                {"freq": int(round(BLACKJACK_MHZ * 1_000_000)), "modulation": 0},
                {"freq": int(round(VHF_MHZ * 1_000_000)), "modulation": 0},
            ],
        }
    }
    srs_radio._ingest_srs_udp_payload(json.dumps(payload).encode("utf-8"))


def _clear_srs() -> None:
    with srs_radio._srs_udp_lock:
        srs_radio._srs_udp_selected_mhz = None
        srs_radio._srs_udp_selected_index = -1
        srs_radio._srs_udp_radios_mhz = []
        srs_radio._srs_udp_name = ""
        srs_radio._srs_udp_received_at = 0.0
        srs_radio._srs_udp_error = ""


def main() -> int:
    bad = 0
    prev_eam = srs_radio.eam_enabled()
    srs_radio.set_eam_enabled(False)
    orig_dcs = srs_radio.read_dcs_radios
    orig_ensure = srs_radio.ensure_srs_udp_listener

    def _no_dcs(**_kwargs):
        return srs_radio.RadioState(source="none", fresh=False, age_s=None)

    srs_radio.read_dcs_radios = _no_dcs
    srs_radio.ensure_srs_udp_listener = lambda: None
    try:
        _ingest_vhf_keyed_uhf_tuned()
        bank = srs_radio.read_srs_client_radios()
        if not bank.fresh:
            print(f"  FAIL SRS bank should be fresh after ingest, got {bank}")
            bad += 1
        if abs((bank.selected_mhz or 0) - VHF_MHZ) > 0.01:
            print(f"  FAIL selected should be VHF {VHF_MHZ}, got {bank.selected_mhz}")
            bad += 1
        if not any(abs(f - BLACKJACK_MHZ) <= 0.05 for f in bank.freqs_mhz):
            print(f"  FAIL bank must include Blackjack UHF, got {bank.freqs_mhz}")
            bad += 1
        if not any(abs(f - VHF_MHZ) <= 0.05 for f in bank.freqs_mhz):
            print(f"  FAIL bank must include keyed VHF, got {bank.freqs_mhz}")
            bad += 1

        keyed_only = srs_radio.read_srs_client_selected()
        if len(keyed_only.freqs_mhz) != 1 or abs(keyed_only.freqs_mhz[0] - VHF_MHZ) > 0.01:
            print(
                f"  FAIL TX snapshot must be VHF-only, got {keyed_only.freqs_mhz}"
            )
            bad += 1

        # The actual bug: gate used selected-only, so Blackjack was a mismatch.
        if srs_radio.on_frequency(BLACKJACK_MHZ, bank) != "match":
            print("  FAIL gate must MATCH Blackjack while keyed on VHF")
            bad += 1
        else:
            print("gate matches Blackjack UHF while keyed on VHF — ok")
        if srs_radio.on_frequency(VHF_MHZ, bank) != "match":
            print("  FAIL gate must also MATCH the keyed VHF")
            bad += 1
        if srs_radio.on_frequency(251.0, bank) != "mismatch":
            print("  FAIL unrelated UHF must still mismatch")
            bad += 1

        state = srs_radio.current_radio_state({"freq_gate_enabled": True})
        if srs_radio.on_frequency(BLACKJACK_MHZ, state) != "match":
            print(
                f"  FAIL current_radio_state must expose UHF bank, got {state.freqs_mhz}"
            )
            bad += 1

        cfg = {
            "freq_gate_enabled": True,
            "freq_gate_tolerance_mhz": 0.05,
            "freq_gate_stale_s": 3.0,
        }
        allowed, msg, result = srs_radio.check_freq_gate(
            cfg, AIRPORT, {"channel": "blackjack"}
        )
        if not allowed or result != "match":
            print(f"  FAIL Blackjack step must be allowed while keyed VHF: {msg!r}")
            bad += 1
        else:
            print(f"Blackjack step allowed while keyed VHF — {msg}")

        # Tips / voice TX: VHF is not an agency, so fall through to Blackjack.
        tuned = srs_radio.channel_for_tuned_freq(AIRPORT, cfg, state=bank)
        if tuned != "blackjack":
            print(f"  FAIL fall-through agency should be blackjack, got {tuned!r}")
            bad += 1
        else:
            print("keyed VHF falls through to Blackjack agency — ok")

        keyed_bj = srs_radio.RadioState(
            source="srs",
            freqs_mhz=[BLACKJACK_MHZ, BANDSAW_MHZ, VHF_MHZ],
            selected_mhz=BANDSAW_MHZ,
            fresh=True,
            age_s=0.1,
        )
        tuned_sel = srs_radio.channel_for_tuned_freq(
            AIRPORT, cfg, state=keyed_bj
        )
        if tuned_sel != "bandsaw":
            print(
                f"  FAIL selected Bandsaw must win over Blackjack, got {tuned_sel!r}"
            )
            bad += 1
        else:
            print("selected Bandsaw wins over other tuned agencies — ok")

        JOSHUA_MHZ = 348.7
        keyed_vhf_josh = srs_radio.RadioState(
            source="srs",
            freqs_mhz=[JOSHUA_MHZ, VHF_MHZ],
            selected_mhz=VHF_MHZ,
            fresh=True,
            age_s=0.1,
        )
        tuned_josh = srs_radio.channel_for_tuned_freq(
            AIRPORT, cfg, state=keyed_vhf_josh
        )
        if tuned_josh == "joshua":
            print("  FAIL stacked Joshua must not steal Fly when VHF is keyed")
            bad += 1
        else:
            print("stacked Joshua ignored when VHF is keyed — ok")
        keyed_josh = srs_radio.RadioState(
            source="srs",
            freqs_mhz=[JOSHUA_MHZ, VHF_MHZ],
            selected_mhz=JOSHUA_MHZ,
            fresh=True,
            age_s=0.1,
        )
        if srs_radio.channel_for_tuned_freq(AIRPORT, cfg, state=keyed_josh) != "joshua":
            print("  FAIL selected Joshua UHF must still resolve")
            bad += 1

        # Ops in the stack with TX parked on the 251 "other" catch-all used to
        # paint Fly as Control and hide WORDS / start. Fall through like VHF.
        OPS_MHZ = float((AIRPORT.get("ops") or {}).get("freq_mhz") or 269.025)
        OTHER_MHZ = float((AIRPORT.get("other") or {}).get("freq_mhz") or 251.0)
        keyed_other_ops = srs_radio.RadioState(
            source="srs",
            freqs_mhz=[OPS_MHZ, OTHER_MHZ, 30.0, 124.8],
            selected_mhz=OTHER_MHZ,
            fresh=True,
            age_s=0.1,
        )
        tuned_ops = srs_radio.channel_for_tuned_freq(
            AIRPORT, cfg, state=keyed_other_ops
        )
        if tuned_ops != "ops":
            print(
                f"  FAIL Ops in the stack must win over keyed other/251, got {tuned_ops!r}"
            )
            bad += 1
        else:
            print("keyed other/251 falls through to Ops — ok")
        keyed_ops = srs_radio.RadioState(
            source="srs",
            freqs_mhz=[OPS_MHZ, OTHER_MHZ],
            selected_mhz=OPS_MHZ,
            fresh=True,
            age_s=0.1,
        )
        if srs_radio.channel_for_tuned_freq(AIRPORT, cfg, state=keyed_ops) != "ops":
            print("  FAIL selected Ops UHF must still resolve")
            bad += 1

        line = srs_radio.format_you_are_on(bank, AIRPORT)
        if "BLACKJACK" not in line or "TX" not in line or "132.650" not in line:
            print(f"  FAIL YOU ARE ON should list UHF + VHF TX, got {line!r}")
            bad += 1
        else:
            print(f"Fly line: {line}")

        # EAM strip: whole bank is receive, selected is TX only.
        _clear_srs()
        srs_radio.set_eam_enabled(True)
        srs_radio.set_eam_freqs_mhz([BLACKJACK_MHZ, VHF_MHZ])
        srs_radio.set_eam_active_index(1)
        eam = srs_radio.current_radio_state(
            {"freq_gate_eam_enabled": True, "freq_gate_stale_s": 3.0}
        )
        if srs_radio.on_frequency(BLACKJACK_MHZ, eam) != "match":
            print(f"  FAIL EAM gate must match unselected strip row, got {eam}")
            bad += 1
        else:
            print("EAM strip matches unselected UHF row — ok")
        if eam.selected_mhz is None or abs(eam.selected_mhz - VHF_MHZ) > 0.01:
            print(f"  FAIL EAM selected should be VHF, got {eam.selected_mhz}")
            bad += 1

        # Assigned NATCF sector wins over the step's hardcoded East channel.
        WEST = float((AIRPORT.get("control_west") or {}).get("freq_mhz") or 254.4)
        west_radio = srs_radio.RadioState(
            source="srs",
            freqs_mhz=[WEST, 138.250],
            selected_mhz=WEST,
            fresh=True,
            age_s=0.1,
        )
        allowed_w, msg_w, result_w = srs_radio.check_freq_gate(
            cfg,
            AIRPORT,
            {"channel": "control_east", "template": "control_check_in"},
            state={"control_channel": "control_west"},
            radio=west_radio,
        )
        if not allowed_w or result_w != "match":
            print(f"  FAIL West assignment must open the East step gate: {msg_w!r}")
            bad += 1
        else:
            print("control gate follows assigned West sector — ok")

        import agencies as agencies_mod

        adopted: dict = {
            "pending_contact": "control_west",
            "control_channel": "control_west",
        }
        agencies_mod.note_tx(adopted, "control_east", "control_check_in")
        if adopted.get("control_channel") != "control_east" or adopted.get("pending_contact"):
            print(f"  FAIL East check-in should adopt East and clear West pending: {adopted}")
            bad += 1
        else:
            print("NATCF check-in adopts the sector that answered — ok")

        parked = {"await_blackjack_checkin": True, "range_exit_skip_until_inside": True}
        if agencies_mod.range_exit_auto_wait(parked) != "waiting for Blackjack check-in":
            print("  FAIL range exit must wait for Blackjack check-in after Bandsaw")
            bad += 1
        else:
            print("range exit waits for Blackjack check-in — ok")
    finally:
        srs_radio.read_dcs_radios = orig_dcs
        srs_radio.ensure_srs_udp_listener = orig_ensure
        srs_radio.set_eam_enabled(prev_eam)
        _clear_srs()

    if bad:
        print(f"\n{bad} FAIL")
        return 1
    print("\nfreq gate radio bank — ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
