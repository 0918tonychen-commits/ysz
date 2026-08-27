from lora_payload import MCountTracker, parse_ack_line, parse_payload


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
        "rebooted": 0.0,
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


def test_fallback_flag_is_captured():
    raw = "s07_m3,t,22,level,3,fallback,1"
    _node, payload, status = parse_payload(raw, MCountTracker())
    assert status == "valid"
    assert payload["meta"]["level"] == 3.0
    assert payload["meta"]["fallback"] == 1.0


def test_router_hop_rssi_alias_does_not_leak_into_data():
    raw = "s02,L3,s03_m9,t,24.5,via,s02,r_in,-73,snr,5.2"
    _node, payload, status = parse_payload(raw, MCountTracker())
    assert status == "valid"
    assert payload["data"] == {"temperature": 24.5}
    assert payload["meta"]["hop_rssi"] == -73.0
    assert payload["meta"]["snr"] == 5.2
    assert "r_in" not in payload["data"]


def test_multi_hop_rssi_and_snr_are_kept_per_hop_not_overwritten():
    raw = "s01,L3,s05_m3,t,20,via,s02,r_in,-70,snr,5.0,via,s01,r_in,-60,snr,6.0"
    _node, payload, status = parse_payload(raw, MCountTracker())
    assert status == "valid"
    assert payload["meta"]["via"] == ["s02", "s01"]
    assert payload["meta"]["hop_rssi"] == [-70.0, -60.0]
    assert payload["meta"]["snr"] == [5.0, 6.0]


def test_parse_ack_line():
    line = "【ACK】from=s03, cmdId=C001, result=OK, rssi=-65, snr=6.1"
    ack = parse_ack_line(line)
    assert ack == {"node": "s03", "cmd_id": "C001", "result": "OK", "rssi": -65, "snr": 6.1}


def test_parse_ack_line_without_radio_metrics():
    line = "【ACK】from=s03, cmdId=C002, result=ERR:BAD_ARG"
    ack = parse_ack_line(line)
    assert ack == {"node": "s03", "cmd_id": "C002", "result": "ERR:BAD_ARG"}


def test_parse_ack_line_rejects_malformed():
    assert parse_ack_line("【ACK】from=notanode, cmdId=C003, result=OK") is None
    assert parse_ack_line("【路由】s03 via s02") is None


def test_reboot_zero_after_grace_period():
    tracker = MCountTracker(reboot_grace_seconds=10)
    assert tracker.classify("s10", 50, now=0) == "valid"
    tracker.commit("s10", 50, now=0)
    assert tracker.classify("s10", 0, now=5) == "out_of_order"
    assert tracker.classify("s10", 0, now=11) == "valid"


# --- boot_id: telling a restart apart from a lost packet ---------------------

def test_backwards_mcount_without_boot_id_is_still_dropped():
    # The pre-existing rule: a restart inside the grace window is indistinguishable
    # from a reordered packet, so it is refused.
    tracker = MCountTracker()
    parse_payload("s05_m40,t,25", tracker)
    _node, payload, status = parse_payload("s05_m1,t,25", tracker)
    assert status == "out_of_order"
    assert payload is None


def test_new_boot_id_rescues_a_backwards_mcount():
    tracker = MCountTracker()
    parse_payload("s05_m40,t,25,boot,A1B2C3D4", tracker)
    node, payload, status = parse_payload("s05_m1,t,25,boot,99887766", tracker)
    assert (node, status) == ("s05", "valid")
    assert payload["meta"]["boot_id"] == "99887766"
    assert payload["meta"]["rebooted"] == 1.0


def test_same_boot_id_still_rejects_a_backwards_mcount():
    tracker = MCountTracker()
    parse_payload("s05_m40,t,25,boot,A1B2C3D4", tracker)
    _node, _payload, status = parse_payload("s05_m1,t,25,boot,A1B2C3D4", tracker)
    assert status == "out_of_order"


def test_restart_is_not_counted_as_packet_loss():
    tracker = MCountTracker()
    for mcount in (1, 2, 3):
        parse_payload(f"s05_m{mcount},t,25,boot,AAAA1111", tracker)
    _node, payload, _status = parse_payload("s05_m1,t,25,boot,BBBB2222", tracker)
    # Without the guard the 1 -> 1 restart would be charged as lost packets.
    assert payload["meta"]["loss"] == 0.0
    assert tracker.lost["s05"] == 0


def test_boot_id_accepts_inline_form_and_is_not_read_as_a_sensor():
    node, payload, status = parse_payload(
        "s05_m7,boot=DEADBEEF,t,25.5,c,410", MCountTracker()
    )
    assert (node, status) == ("s05", "valid")
    assert payload["meta"]["boot_id"] == "DEADBEEF"
    assert payload["data"] == {"temperature": 25.5, "co2": 410.0}


def test_voltage_alias_still_reaches_data():
    # The brownout signal has to survive parsing to be diagnosable at all.
    _node, payload, status = parse_payload("s05_m7,v,3.71,t,25", MCountTracker())
    assert status == "valid"
    assert payload["data"]["voltage"] == 3.71


def test_first_boot_id_seen_is_not_treated_as_a_restart():
    tracker = MCountTracker()
    _node, payload, status = parse_payload("s05_m1,t,25,boot,A1B2C3D4", tracker)
    assert status == "valid"
    assert payload["meta"]["rebooted"] == 0.0


def test_delayed_packet_from_old_boot_cannot_switch_tracker_back():
    tracker = MCountTracker()
    parse_payload("s05_m40,t,25,boot,AAAA1111", tracker)
    parse_payload("s05_m1,t,25,boot,BBBB2222", tracker)

    _node, payload, status = parse_payload(
        "s05_m41,t,25,boot,AAAA1111", tracker
    )
    assert status == "out_of_order"
    assert payload is None
    assert tracker.boot["s05"] == "BBBB2222"

    _node, payload, status = parse_payload(
        "s05_m2,t,25,boot,BBBB2222", tracker
    )
    assert status == "valid"
    assert payload["meta"]["rebooted"] == 0.0


def test_boot_history_is_bounded():
    tracker = MCountTracker()
    for number in range(80):
        boot_id = f"{number:08X}"
        _node, _payload, status = parse_payload(
            f"s05_m1,t,25,boot,{boot_id}", tracker
        )
        assert status == "valid"
    assert len(tracker.seen_boots["s05"]) == 64
    assert tracker.boot["s05"] in tracker.seen_boots["s05"]
