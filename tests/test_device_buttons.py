"""GPIO → room map helpers (issue #78)."""

from __future__ import annotations

import pytest
from shared import (
    buttons_from_manage_body,
    device_hello_payload,
    normalize_buttons_map,
    normalize_pins,
    pins_from_hello_body,
)


def test_normalize_pins_sorts_unique_and_caps():
    assert normalize_pins([13, 4, 4, 5]) == [4, 5, 13]
    assert normalize_pins([1, 2, 3, 4, 5, 6, 7, 8, 9]) == [1, 2, 3, 4, 5, 6, 7, 8]


def test_normalize_pins_rejects_non_list():
    with pytest.raises(ValueError):
        normalize_pins({"4": True})


def test_pins_from_hello_body():
    assert pins_from_hello_body({}) is None
    assert pins_from_hello_body({"firmware_version": "1"}) is None
    assert pins_from_hello_body({"pins": [5, 4]}) == [4, 5]
    assert pins_from_hello_body({"pins": "nope"}) is None


def test_normalize_buttons_drops_unknown_rooms_and_empty():
    mapping = normalize_buttons_map(
        {"4": "living", "5": "", "99": "bedroom", "x": "cinema", "12": "mars"},
        valid_rooms={"living", "bedroom"},
    )
    assert mapping == {"4": "living"}


def test_normalize_buttons_rejects_non_object():
    with pytest.raises(ValueError):
        normalize_buttons_map("study")


def test_buttons_from_manage_body():
    assert buttons_from_manage_body("x", {"living"}) == "invalid buttons"
    assert buttons_from_manage_body({"buttons": {"4": "living"}}, {"living"}) == {"4": "living"}


def test_hello_payload_includes_filtered_buttons():
    device = {
        "name": "Kitchen",
        "room": "",
        "pending": False,
        "buttons": {"4": "living", "5": "gone"},
    }
    payload = device_hello_payload(device, valid_rooms={"living"})
    assert payload["status"] == "ok"
    assert payload["buttons"] == {"4": "living"}


def test_hello_payload_empty_buttons_when_unconfigured():
    device = {"name": "Kitchen", "room": "", "pending": False}
    payload = device_hello_payload(device)
    assert payload["buttons"] == {}


def test_hello_payload_omits_buttons_when_pending():
    payload = device_hello_payload({"name": "X", "pending": True, "buttons": {"4": "living"}})
    assert payload == {"status": "pending"}
    assert "buttons" not in payload
