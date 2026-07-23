from lora_payload import MCountTracker, parse_payload


def test_normal_packet():
    node, payload, status = parse_payload("s05_m42,t,25.3,h,60", MCountTracker())
    assert (node, status) == ("s05", "valid")
    assert payload["data"] == {"temperature": 25.3, "humidity": 60.0}
    assert payload["meta"]["mcount"] == 42


def test_relay_packet_and_radio_metrics():
    raw = "via_s02_m99,s10_m7,pm25,12,rssi:-80,snr:7.5"
    node, payload, status = parse_payload(raw, MCountTracker())
    assert (node, status) == ("s10", "valid")
    assert payload["meta"]["via"] == ["s02"]
    assert payload["meta"]["rssi"] == -80
    assert payload["meta"]["snr"] == 7.5


def test_legacy_meta_aliases_and_hop_metrics():
    raw = (
        "s10_m8,t,24,gw_rssi,-81,gw_snr,6.5,"
        "hop_rssi,-72,hop_snr:4.5,level,2,via,s02"
    )
    _node, payload, status = parse_payload(raw, MCountTracker())
    assert status == "valid"
    assert payload["meta"] == {
        "mcount": 8,
        "via": ["s02"],
        "rssi": -81.0,
        "snr": 6.5,
        "hop_rssi": -72.0,
        "hop_snr": 4.5,
        "level": 2.0,
        "loss": 0.0,
    }


def test_duplicate_and_out_of_order():
    tracker = MCountTracker()
    assert parse_payload("s05_m10,t,1", tracker)[2] == "valid"
    assert parse_payload("s05_m10,t,2", tracker)[2] == "duplicate"
    assert parse_payload("s05_m9,t,3", tracker)[2] == "out_of_order"
    assert tracker.last["s05"] == 10


def test_incomplete_does_not_advance_tracker():
    tracker = MCountTracker()
    assert parse_payload("s05_m11,t", tracker)[2] == "incomplete"
    assert "s05" not in tracker.last


def test_invalid_text_does_not_create_unknown_payload():
    assert parse_payload("garbage", MCountTracker()) == (None, None, "invalid")


def test_jump_updates_loss():
    tracker = MCountTracker()
    parse_payload("s12_m1,t,1", tracker)
    _node, payload, status = parse_payload("s12_m4,t,2", tracker)
    assert status == "valid"
    assert payload["meta"]["loss"] == 50.0


def test_snr_regex_rejects_malformed_number():
    _node, payload, status = parse_payload("s10_m1,t,2,snr:1.2.3", MCountTracker())
    assert status == "valid"
    assert "snr" not in payload["meta"]


def test_reboot_zero_after_grace_period():
    tracker = MCountTracker(reboot_grace_seconds=10)
    assert tracker.classify("s10", 50, now=0) == "valid"
    tracker.commit("s10", 50, now=0)
    assert tracker.classify("s10", 0, now=5) == "out_of_order"
    assert tracker.classify("s10", 0, now=11) == "valid"
