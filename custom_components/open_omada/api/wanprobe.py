"""WAN quality probes (HA independent).

Each WAN gets its own probe targets; the integration pins them to that WAN with Omada policy
routes, so measuring a target measures that link. A probe is a TCP connect + TLS handshake on
port 443 (no privileges needed, works through NAT); its duration is the latency.

Classification follows the owner's original script: Offline when no probe succeeds; Poor when
fewer than a majority succeed or the median latency is above the threshold; Good otherwise.
A Poor result is confirmed with extra samples (majority of all samples wins).
"""

from __future__ import annotations

import asyncio
import ipaddress
import ssl
import statistics
import time
from dataclasses import dataclass, field

GOOD, POOR, OFFLINE, UNKNOWN = "good", "poor", "offline", "unknown"

# Owner decision ("misto"): one public DNS pair + the hosts of the original script, 3 per WAN.
DEFAULT_PROBES = {
    1: ["1.1.1.1", "nic.br", "ec2.sa-east-1.amazonaws.com"],
    2: ["1.0.0.1", "iaas.sa-saopaulo-1.oraclecloud.com", "8.8.4.4"],
}

_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE  # we time the handshake; no data is exchanged


@dataclass(slots=True)
class ProbeTarget:
    host: str  # as configured (name or IPv4)
    ip: str  # resolved IPv4 the WAN is pinned to
    port: int = 443

    @property
    def sni(self) -> str | None:
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            return self.host
        return None


@dataclass(slots=True)
class ProbeResult:
    target: ProbeTarget
    ok: bool
    latency_ms: float | None
    error: str | None = None


@dataclass(slots=True)
class Sample:
    results: list[ProbeResult]
    at: float = field(default_factory=time.time)

    @property
    def ok(self) -> list[ProbeResult]:
        return [r for r in self.results if r.ok]

    @property
    def median_ms(self) -> float | None:
        lat = [r.latency_ms for r in self.ok if r.latency_ms is not None]
        return round(statistics.median(lat), 1) if lat else None

    @property
    def jitter_ms(self) -> float | None:
        lat = [r.latency_ms for r in self.ok if r.latency_ms is not None]
        return round(statistics.pstdev(lat), 1) if len(lat) > 1 else (0.0 if lat else None)


@dataclass(slots=True)
class WanQuality:
    quality: str
    median_ms: float | None
    jitter_ms: float | None
    ok: int
    total: int
    samples: int
    at: float
    results: list[ProbeResult]


def parse_targets(text: str | list[str]) -> list[str]:
    """'1.1.1.1, nic.br\\n8.8.4.4' -> ['1.1.1.1', 'nic.br', '8.8.4.4'] (keeps order, no dups)."""
    items = text if isinstance(text, list) else text.replace("\n", ",").split(",")
    out: list[str] = []
    for item in items:
        host = item.strip().lower()
        if host and host not in out:
            out.append(host)
    return out


async def resolve_ipv4(host: str) -> list[str]:
    """IPv4 addresses of a host (an IPv4 literal resolves to itself)."""
    try:
        if ipaddress.ip_address(host).version == 4:
            return [host]
        return []
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, family=2, type=1)  # AF_INET, SOCK_STREAM
    seen: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.append(ip)
    return seen


async def probe(target: ProbeTarget, probe_timeout: float = 5.0) -> ProbeResult:
    started = time.monotonic()
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(target.ip, target.port, ssl=_TLS, server_hostname=target.sni),
            probe_timeout,
        )
        return ProbeResult(target, True, round((time.monotonic() - started) * 1000, 1))
    except (TimeoutError, OSError, ssl.SSLError) as err:
        return ProbeResult(target, False, None, type(err).__name__)
    finally:
        if writer is not None:
            writer.close()


async def measure_sample(targets: list[ProbeTarget], probe_timeout: float = 5.0) -> Sample:
    return Sample(list(await asyncio.gather(*(probe(t, probe_timeout) for t in targets))))


def classify(sample: Sample, latency_ms: float) -> str:
    if not sample.results:
        return UNKNOWN
    ok = sample.ok
    if not ok:
        return OFFLINE
    if len(ok) < len(sample.results) // 2 + 1 or (sample.median_ms or 0) > latency_ms:
        return POOR
    return GOOD


async def measure_wan(
    targets: list[ProbeTarget],
    *,
    latency_ms: float = 150,
    confirm_tests: int = 2,
    confirm_interval: float = 5,
    probe_timeout: float = 5.0,
    sleep=asyncio.sleep,
) -> WanQuality:
    """One measurement; a non-Good first sample is confirmed with ``confirm_tests`` more."""
    samples = [await measure_sample(targets, probe_timeout)]
    states = [classify(samples[0], latency_ms)]
    if states[0] in (POOR, OFFLINE):
        for _ in range(confirm_tests):
            await sleep(confirm_interval)
            samples.append(await measure_sample(targets, probe_timeout))
            states.append(classify(samples[-1], latency_ms))
        need = len(states) // 2 + 1
        counts = {s: states.count(s) for s in (GOOD, POOR, OFFLINE)}
        quality = next((s for s, n in counts.items() if n >= need), GOOD)
    else:
        quality = states[0]
    last = samples[-1]
    all_ok = [r.latency_ms for s in samples for r in s.ok if r.latency_ms is not None]
    median = round(statistics.median(all_ok), 1) if all_ok else None
    return WanQuality(quality, median, last.jitter_ms, len(last.ok), len(last.results), len(samples),
                      last.at, last.results)
