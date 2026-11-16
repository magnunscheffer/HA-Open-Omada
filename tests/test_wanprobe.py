import pytest

import api.wanprobe as wp
from api.wanprobe import GOOD, OFFLINE, POOR, ProbeResult, ProbeTarget, Sample, classify, measure_wan, parse_targets

T = [ProbeTarget("a", "192.0.2.1"), ProbeTarget("b", "192.0.2.2"), ProbeTarget("c", "192.0.2.3")]


def sample(*lat):
    """lat = latency in ms or None for a failed probe."""
    return Sample([ProbeResult(t, x is not None, x) for t, x in zip(T, lat, strict=True)])


def test_classify_like_the_original_script():
    assert classify(sample(20, 30, 40), 150) == GOOD
    assert classify(sample(20, 30, None), 150) == GOOD  # 2 of 3 is a majority
    assert classify(sample(20, None, None), 150) == POOR  # minority succeeded
    assert classify(sample(200, 210, 220), 150) == POOR  # median above threshold
    assert classify(sample(None, None, None), 150) == OFFLINE


@pytest.mark.parametrize(
    ("first", "extra", "expected"),
    [
        ((200, 200, 200), [(20, 20, 20), (20, 20, 20)], GOOD),  # one bad blip is not confirmed
        ((200, 200, 200), [(200, 200, 200), (20, 20, 20)], POOR),  # confirmed 2 of 3
        ((None, None, None), [(None, None, None), (20, 20, 20)], OFFLINE),
        ((200, 200, 200), [(None, None, None), (20, 20, 20)], GOOD),  # no majority -> Good (script rule)
    ],
)
async def test_confirmation_majority(monkeypatch, first, extra, expected):
    queue = [sample(*first), *(sample(*e) for e in extra)]

    async def fake_sample(targets, probe_timeout=5.0):
        return queue.pop(0)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(wp, "measure_sample", fake_sample)
    q = await measure_wan(T, latency_ms=150, confirm_tests=2, confirm_interval=0, sleep=no_sleep)
    assert q.quality == expected and q.samples == 3


async def test_good_first_sample_is_not_retested(monkeypatch):
    calls = []

    async def fake_sample(targets, probe_timeout=5.0):
        calls.append(1)
        return sample(20, 30, 40)

    monkeypatch.setattr(wp, "measure_sample", fake_sample)
    q = await measure_wan(T)
    assert q.quality == GOOD and len(calls) == 1 and q.median_ms == 30


def test_parse_targets():
    assert parse_targets(" 1.1.1.1, nic.br\nNIC.BR ,, 8.8.4.4 ") == ["1.1.1.1", "nic.br", "8.8.4.4"]


async def test_resolve_ipv4_literal_and_probe_failure():
    assert await wp.resolve_ipv4("1.1.1.1") == ["1.1.1.1"]
    r = await wp.probe(ProbeTarget("x", "192.0.2.1", 9), probe_timeout=0.2)  # TEST-NET, nothing listens
    assert not r.ok and r.latency_ms is None and r.error


def test_sni_only_for_names():
    assert ProbeTarget("nic.br", "200.160.4.6").sni == "nic.br"
    assert ProbeTarget("1.1.1.1", "1.1.1.1").sni is None

