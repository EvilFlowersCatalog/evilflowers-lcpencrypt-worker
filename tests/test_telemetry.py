"""Tests for the telemetry module (collector discovery, setup gating, resource)."""

import os
import socket

import pytest

from evilflowers_lcpencrypt_worker import telemetry

# /proc/net/route with default gateway 172.28.0.1 (little-endian hex 01001CAC) and a link route.
ROUTE_TABLE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00000000\t01001CAC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t00001CAC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    for name in (
        *telemetry.ENDPOINT_VARIABLES,
        "HOST_IP",
        "OTEL_SDK_DISABLED",
        "OTEL_TRACES_EXPORTER",
        "OTEL_METRICS_EXPORTER",
        "OTEL_EXPORTER_OTLP_AUTODISCOVER",
        "OTEL_RESOURCE_ATTRIBUTES",
        "OTEL_SERVICE_NAME",
        telemetry._DISCOVERY_MARKER,
        "OTEL_EXPORTER_OTLP_PROTOCOL",
    ):
        # setenv first so monkeypatch records the original state and removes what the code under
        # test writes into os.environ itself (discover_endpoint exports the endpoint).
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setitem(telemetry._state, "configured", False)
    monkeypatch.setitem(telemetry._state, "enabled", False)


class TestParseDefaultGateway:
    def test_default_route(self):
        assert telemetry.parse_default_gateway(ROUTE_TABLE) == "172.28.0.1"

    def test_no_default_route(self):
        table = ROUTE_TABLE.splitlines()[0] + "\n" + ROUTE_TABLE.splitlines()[2] + "\n"
        assert telemetry.parse_default_gateway(table) is None

    def test_default_route_without_gateway_flag_is_ignored(self):
        assert telemetry.parse_default_gateway(ROUTE_TABLE.replace("\t0003\t", "\t0001\t", 1)) is None

    def test_garbage_lines_are_skipped(self):
        assert telemetry.parse_default_gateway("header\nshort\neth0\t00000000\tZZZZ\t0003\n") is None

    def test_missing_route_file(self, tmp_path):
        assert telemetry.default_gateway(tmp_path / "nope") is None


class TestDiscoverEndpoint:
    def test_explicit_endpoint_wins_without_probing(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        monkeypatch.setattr(telemetry, "probe", pytest.fail)
        assert telemetry.discover_endpoint() == "http://collector:4318"

    def test_host_ip_is_probed_and_exported(self, monkeypatch):
        monkeypatch.setenv("HOST_IP", "192.168.122.60")
        probed: list[str] = []
        monkeypatch.setattr(telemetry, "probe", lambda host: probed.append(host) or True)

        assert telemetry.discover_endpoint() == "http://192.168.122.60:4318"
        assert probed == ["192.168.122.60"]
        assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://192.168.122.60:4318"

    def test_host_ip_before_default_gateway(self, monkeypatch):
        monkeypatch.setenv("HOST_IP", "10.0.0.5")
        monkeypatch.setattr(telemetry, "default_gateway", pytest.fail)
        monkeypatch.setattr(telemetry, "probe", lambda host: True)
        assert telemetry.discover_endpoint() == "http://10.0.0.5:4318"

    def test_falls_back_to_default_gateway(self, monkeypatch):
        monkeypatch.setattr(telemetry, "default_gateway", lambda: "172.28.0.1")
        monkeypatch.setattr(telemetry, "probe", lambda host: True)
        assert telemetry.discover_endpoint() == "http://172.28.0.1:4318"

    def test_nothing_listening_means_off(self, monkeypatch):
        monkeypatch.setenv("HOST_IP", "10.0.0.5")
        monkeypatch.setattr(telemetry, "probe", lambda host: False)
        assert telemetry.discover_endpoint() is None

    def test_probes_once_per_process_tree(self, monkeypatch):
        monkeypatch.setenv("HOST_IP", "10.0.0.5")
        calls: list[str] = []
        monkeypatch.setattr(telemetry, "probe", lambda host: calls.append(host) or False)
        telemetry.discover_endpoint()
        telemetry.discover_endpoint()
        assert calls == ["10.0.0.5"]

    def test_autodiscover_off(self, monkeypatch):
        monkeypatch.setenv("HOST_IP", "10.0.0.5")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_AUTODISCOVER", "false")
        monkeypatch.setattr(telemetry, "probe", pytest.fail)
        assert telemetry.discover_endpoint() is None

    def test_ipv6_host_is_bracketed(self, monkeypatch):
        monkeypatch.setenv("HOST_IP", "fd00::1")
        monkeypatch.setattr(telemetry, "probe", lambda host: True)
        assert telemetry.discover_endpoint() == "http://[fd00::1]:4318"


class TestProbe:
    def test_listening_socket(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            assert telemetry.probe("127.0.0.1", server.getsockname()[1]) is True

    def test_closed_port(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            port = server.getsockname()[1]
        assert telemetry.probe("127.0.0.1", port, timeout=0.2) is False


class TestSetup:
    def test_off_without_collector(self, monkeypatch):
        monkeypatch.setattr(telemetry, "discover_endpoint", lambda: None)
        monkeypatch.setattr(telemetry, "_configure", pytest.fail)
        assert telemetry.setup() is False
        assert telemetry.is_enabled() is False

    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
        monkeypatch.setattr(telemetry, "discover_endpoint", pytest.fail)
        assert telemetry.setup() is False

    def test_configures_otlp_when_collector_found(self, monkeypatch):
        monkeypatch.setattr(telemetry, "discover_endpoint", lambda: "http://10.0.0.5:4318")
        seen: list[tuple[list[str], list[str]]] = []
        monkeypatch.setattr(telemetry, "_configure", lambda spans, metrics: seen.append((spans, metrics)))

        assert telemetry.setup() is True
        assert seen == [(["otlp"], ["otlp"])]

    def test_console_exporter_works_without_collector(self, monkeypatch):
        monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")
        monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
        monkeypatch.setattr(telemetry, "discover_endpoint", lambda: None)
        seen: list[tuple[list[str], list[str]]] = []
        monkeypatch.setattr(telemetry, "_configure", lambda spans, metrics: seen.append((spans, metrics)))

        assert telemetry.setup() is True
        assert seen == [(["console"], [])]

    def test_idempotent(self, monkeypatch):
        monkeypatch.setattr(telemetry, "discover_endpoint", lambda: "http://10.0.0.5:4318")
        calls: list[int] = []
        monkeypatch.setattr(telemetry, "_configure", lambda *_: calls.append(1))
        assert telemetry.setup() is True
        assert telemetry.setup() is True
        assert calls == [1]

    def test_missing_sdk_keeps_worker_running(self, monkeypatch):
        monkeypatch.setattr(telemetry, "discover_endpoint", lambda: "http://10.0.0.5:4318")

        def no_sdk(*_):
            raise ImportError("No module named 'opentelemetry.sdk'")

        monkeypatch.setattr(telemetry, "_configure", no_sdk)
        assert telemetry.setup() is False


class TestResource:
    def test_defaults(self):
        attributes = telemetry.build_resource().attributes
        assert attributes["service.name"] == "lcpencrypt"
        assert attributes["service.namespace"] == "elvira"
        assert str(attributes["service.instance.id"]).endswith(f"-{os.getpid()}")

    def test_environment_wins(self, monkeypatch):
        monkeypatch.setenv("OTEL_SERVICE_NAME", "lcpencrypt-dev")
        monkeypatch.setenv(
            "OTEL_RESOURCE_ATTRIBUTES", "service.namespace=elvira-test,deployment.environment.name=development"
        )
        attributes = telemetry.build_resource().attributes
        assert attributes["service.name"] == "lcpencrypt-dev"
        assert attributes["service.namespace"] == "elvira-test"
        assert attributes["deployment.environment.name"] == "development"


class TestExporterNames:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(None, ["otlp"]), ("none", []), ("otlp,console", ["otlp", "console"]), (" OTLP ", ["otlp"])],
    )
    def test_parsing(self, monkeypatch, raw, expected):
        if raw is None:
            monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)
        else:
            monkeypatch.setenv("OTEL_TRACES_EXPORTER", raw)
        assert telemetry.exporter_names("OTEL_TRACES_EXPORTER") == expected
