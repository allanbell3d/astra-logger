"""Deterministic, evidence-only rules for system/journald records."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import re
from typing import Callable, Pattern


Emit = Callable[[re.Match[str]], dict[str, object]]


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: Pattern[str]
    specificity: int
    emit: Emit


def _unit(match: re.Match[str]) -> str:
    return match.group("unit")


def _share(match: re.Match[str]) -> str:
    value = match.group("share")
    return "//" + value.lstrip("\\").replace("\\", "/")


def _unit_component(unit: str) -> str:
    return ("systemd:" + unit)


def _searx_target(match: re.Match[str]) -> str:
    value = match.group(0)
    body_engine = re.search(r"\)\s*(?P<engine>[A-Za-z0-9_.-]+):\s+", value)
    if body_engine:
        return body_engine.group("engine")
    logger_engine = re.search(r"\bsearx\.engines\.(?P<engine>[A-Za-z0-9_.-]+):", value, re.I)
    if logger_engine:
        return logger_engine.group("engine")
    return "searx"


def _failed_result(match: re.Match[str]) -> dict[str, object]:
    unit = _unit(match)
    result = match.group("result")
    cause = {
        "exit-code": "service_exit_nonzero",
        "timeout": "service_timeout",
        "oom-kill": "memory_exhausted",
        "watchdog": "watchdog_timeout",
    }.get(result, "service_failure")
    event = "service.exited" if result == "exit-code" else "service.failed"
    return {
        "event": event,
        "cause": cause,
        "operation": "service_failure",
        "target": unit,
        "component": _unit_component(unit),
        "kind": "incident",
        "service_result": result,
    }


UNIT_PATTERN = r"(?P<unit>[A-Za-z0-9_.@:\\-]+(?:\.service|\.mount))"


RULES: tuple[Rule, ...] = (
    Rule("system.oom.killed-victim", re.compile(
        r"(?:Out of memory:|oom-kill:).*?Killed process\s+(?P<pid>\d+)\s+\((?P<proc>[^)]+)\)", re.I), 100,
        lambda m: {"event": "process.oom_killed", "cause": "memory_exhausted", "operation": "oom_kill", "target": m.group("proc"), "component": "kernel", "kind": "incident", "platform": "linux", "signal": "SIGKILL"}),
    Rule("system.oom.task", re.compile(
        r"oom-kill:.*?\btask=(?P<task>[A-Za-z0-9_.@:-]+).*?\bpid=(?P<pid>\d+)", re.I), 95,
        lambda m: {"event": "process.oom_killed", "cause": "memory_exhausted", "operation": "oom_kill", "target": m.group("task"), "component": "kernel", "kind": "incident", "platform": "linux", "signal": "SIGKILL"}),
    Rule("system.oom.invoked", re.compile(
        r"\b(?P<actor>[A-Za-z0-9_.@:-]+)\s+invoked oom-killer:", re.I), 92,
        lambda m: {"event": "system.oom_invoked", "cause": "memory_exhausted", "operation": "oom_invoke", "target": m.group("actor"), "component": "kernel", "kind": "incident", "platform": "linux"}),
    Rule("system.service.oom-kill-result", re.compile(
        UNIT_PATTERN + r":.*?Failed with result ['\"]oom-kill['\"]", re.I), 100,
        lambda m: {"event": "service.oom_killed", "cause": "memory_exhausted", "operation": "service_failure", "target": _unit(m), "component": _unit_component(_unit(m)), "kind": "incident", "service_result": "oom-kill"}),
    Rule("system.service.watchdog", re.compile(
        UNIT_PATTERN + r":.*?Failed with result ['\"]watchdog['\"]", re.I), 100,
        lambda m: {"event": "service.watchdog_timeout", "cause": "watchdog_timeout", "operation": "watchdog", "target": _unit(m), "component": _unit_component(_unit(m)), "kind": "incident", "service_result": "watchdog"}),
    Rule("system.service.main-exit-code", re.compile(
        UNIT_PATTERN + r":\s+(?:Main|Mount) process exited,\s*code=exited,\s*status=(?P<code>\d+)(?:/(?P<name>[A-Z_]+))?", re.I), 88,
        lambda m: {"event": "service.exited", "cause": "service_exit_nonzero", "operation": "process_exit", "target": _unit(m), "component": _unit_component(_unit(m)), "kind": "incident", "exit_code": int(m.group("code")), "service_result": "exit-code"}),
    Rule("system.service.main-signal", re.compile(
        UNIT_PATTERN + r":\s+(?:Main|Mount) process exited,\s*code=killed,\s*status=(?P<signal>[A-Z0-9/]+)", re.I), 93,
        lambda m: {"event": "service.killed", "cause": "signal_termination", "operation": "process_kill", "target": _unit(m), "component": _unit_component(_unit(m)), "kind": "incident", "signal": m.group("signal"), "service_result": "signal"}),
    Rule("system.mount.failed", re.compile(r"Failed to mount\s+" + UNIT_PATTERN + r"\s+-\s+(?P<mountpoint>/\S+)", re.I), 92,
        lambda m: {"event": "filesystem.mount_failed", "cause": "mount_failure", "operation": "mount", "target": m.group("mountpoint"), "component": _unit_component(_unit(m)), "kind": "incident", "device": _unit(m)}),
    Rule("system.mount.timeout", re.compile(UNIT_PATTERN + r":\s+Mounting timed out\. Terminating\.", re.I), 91,
        lambda m: {"event": "filesystem.mount_timeout", "cause": "service_timeout", "operation": "mount", "target": _unit(m), "component": _unit_component(_unit(m)), "kind": "incident", "service_result": "timeout"}),
    Rule("system.service.failed-result", re.compile(
        UNIT_PATTERN + r":.*?Failed with result ['\"](?P<result>[^'\"]+)['\"]", re.I), 70,
        _failed_result),
    Rule("system.service.exit-code", re.compile(
        UNIT_PATTERN + r":.*?code=exited,\s*status=(?P<code>\d+)", re.I), 85,
        lambda m: {"event": "service.exited", "cause": "service_exit_nonzero", "operation": "process_exit", "target": _unit(m), "component": _unit_component(_unit(m)), "kind": "incident", "exit_code": int(m.group("code")), "service_result": "exit-code"}),
    Rule("system.service.signal", re.compile(
        UNIT_PATTERN + r":.*?code=killed,\s*status=(?P<signal>[A-Z0-9/]+)", re.I), 90,
        lambda m: {"event": "service.killed", "cause": "signal_termination", "operation": "process_kill", "target": _unit(m), "component": _unit_component(_unit(m)), "kind": "incident", "signal": m.group("signal"), "service_result": "signal"}),
    Rule("system.xrdp.tls", re.compile(r"\bxrdp.*?\bTLS\b.*?\b(?:error|failed|disconnect|fatal)\b", re.I), 96,
        lambda m: {"event": "xrdp.tls_failure", "cause": "tls_failure", "operation": "remote_desktop_tls", "target": "xrdp", "component": "xrdp", "platform": "xrdp", "kind": "incident"}),
    Rule("system.xrdp.transport", re.compile(r"\b(?:xrdp|sesexec|Connection Sequence|CR-TPDU|DisconnectProviderUltimatum).*?\b(?:trans_|transport|Connection Sequence|CR-TPDU|DisconnectProviderUltimatum).*?\b(?:failed|error)\b", re.I), 94,
        lambda m: {"event": "xrdp.transport_failure", "cause": "transport_failure", "operation": "remote_desktop_transport", "target": "xrdp", "component": "xrdp", "platform": "xrdp", "kind": "incident"}),
    Rule("system.xrdp.socket", re.compile(r"\b(?:xrdp|libxrdp).*?\b(?:socket|force_read|header read|send|read).*?\b(?:failed|error)\b", re.I), 93,
        lambda m: {"event": "xrdp.socket_failure", "cause": "socket_io_failure", "operation": "remote_desktop_io", "target": "xrdp", "component": "xrdp", "platform": "xrdp", "kind": "incident"}),
    Rule("system.xrdp.auth", re.compile(r"\b(?:xrdp|sesman|pam_authenticate).*?\b(?:Authentication failure|auth(?:entication)? failed|login failed|denied access)\b", re.I), 94,
        lambda m: {"event": "xrdp.auth_failure", "cause": "authentication_failure", "operation": "remote_desktop_auth", "target": "xrdp", "component": "xrdp", "platform": "xrdp", "kind": "incident"}),
    Rule("system.cifs.return-code", re.compile(
        r"\bCIFS:\s+.*?(?P<share>\\\\[A-Za-z0-9_.-]+\\[A-Za-z0-9_.-]+).*?\brc:(?P<errno>-?\d+)", re.I), 95,
        lambda m: {"event": "filesystem.cifs_error", "cause": "cifs_return_code", "operation": "share_access", "target": _share(m), "device": _share(m), "component": "cifs", "platform": "linux", "kind": "incident", "errno": int(m.group("errno"))}),
    Rule("system.cifs.status-code", re.compile(r"\bCIFS:\s+.*?Status code returned\s+(?P<status>0x[0-9a-f]+|STATUS_[A-Z0-9_]+)", re.I), 95,
        lambda m: {"event": "filesystem.cifs_error", "cause": "cifs_status_code", "operation": "share_access", "component": "cifs", "platform": "linux", "kind": "incident", "service_result": m.group("status")}),
    Rule("system.cifs.mount-return-code", re.compile(r"\bCIFS:\s+.*?\bcifs_mount failed w/return code = (?P<errno>-?\d+)", re.I), 94,
        lambda m: {"event": "filesystem.cifs_mount_failed", "cause": "cifs_return_code", "operation": "mount", "component": "cifs", "platform": "linux", "kind": "incident", "errno": int(m.group("errno"))}),
    Rule("system.cifs.socket", re.compile(r"\bCIFS:\s+.*?\bError connecting to socket\. Aborting operation\.", re.I), 93,
        lambda m: {"event": "filesystem.cifs_socket_failure", "cause": "socket_connect_failure", "operation": "share_connect", "component": "cifs", "platform": "linux", "kind": "incident"}),
    Rule("system.cifs.reconnect", re.compile(r"\bCIFS:\s+.*?\b(?:reconnect|has not responded|Close interrupted|Close unmatched open)\b", re.I), 88,
        lambda m: {"event": "filesystem.cifs_reconnect", "cause": "cifs_reconnect", "operation": "share_reconnect", "component": "cifs", "platform": "linux", "kind": "incident"}),
    Rule("system.cifs.share", re.compile(r"\bCIFS:\s+.*?(?P<share>\\\\[A-Za-z0-9_.-]+\\[A-Za-z0-9_.-]+)", re.I), 90,
        lambda m: {"event": "filesystem.cifs", "operation": "share_access", "target": _share(m), "device": _share(m), "component": "cifs", "platform": "linux", "kind": "incident"}),
    Rule("system.networkmanager.dhcp", re.compile(r"(?:\bNetworkManager(?:\[\d+\])?:.*?|\bdevice\s+\([^)]+\):\s*)\bDHCP\b.*?\b(?:failed|timeout|timed out|expired|no lease)\b", re.I), 90,
        lambda m: {"event": "network.dhcp_failure", "cause": "dhcp_failure", "operation": "dhcp", "component": "NetworkManager", "platform": "network", "kind": "incident"}),
    Rule("system.networkmanager.association", re.compile(r"(?:\bNetworkManager(?:\[\d+\])?:.*?|\bdevice\s+\([^)]+\):\s*)\b(?:association|Activation: failed for connection|no secrets)\b", re.I), 88,
        lambda m: {"event": "network.association_failure", "cause": "wifi_association_failure", "operation": "wifi_association", "component": "NetworkManager", "platform": "network", "kind": "incident"}),
    Rule("system.networkmanager.interface", re.compile(r"(?:\bNetworkManager(?:\[\d+\])?:.*?)?\bdevice\s+\((?P<iface>[^)]+)\):.*?\b(?:disconnected|unavailable|link timed out|failed)\b", re.I), 86,
        lambda m: {"event": "network.interface_failure", "cause": "network_link_failure", "operation": "interface_state", "target": m.group("iface"), "device": m.group("iface"), "component": "NetworkManager", "platform": "network", "kind": "incident"}),
    Rule("system.networkmanager.failure", re.compile(r"\bNetworkManager(?:\[\d+\])?:.*?\b(?:failed|failure|disconnected|unavailable)\b", re.I), 80,
        lambda m: {"event": "network.failure", "cause": "network_link_failure", "operation": "network_management", "component": "NetworkManager", "platform": "network", "kind": "incident"}),
    Rule("system.xrdp.failure", re.compile(r"\bxrdp(?:-sesman)?(?:\.service)?(?:\[\d+\])?:.*?\b(?:failed|failure|error|disconnect)\b", re.I), 85,
        lambda m: {"event": "xrdp.session_failure", "cause": "remote_desktop_failure", "operation": "remote_desktop", "target": "xrdp", "component": "xrdp", "platform": "xrdp", "kind": "incident"}),
    Rule("system.pam.failure", re.compile(r"\bpam_(?:unix|winbind|systemd)\b.*?\b(?:authentication failure|auth failed|failure|denied access|conversation failed|could not identify password|PAM_AUTH_ERR)\b", re.I), 87,
        lambda m: {"event": "security.auth_failure", "cause": "authentication_failure", "operation": "authentication", "component": "pam", "platform": "auth", "kind": "incident"}),
    Rule("system.ssh.auth-failure", re.compile(r"\bsshd(?:\[\d+\])?:.*?\b(?:Failed password|Invalid user|authentication failure|Connection closed by authenticating user)\b", re.I), 86,
        lambda m: {"event": "security.ssh_auth_failure", "cause": "authentication_failure", "operation": "ssh_authentication", "component": "sshd", "platform": "auth", "kind": "incident"}),
    Rule("system.ssh.auth-success", re.compile(r"\bsshd(?:\[\d+\])?:.*?\bAccepted\s+(?P<method>\w+)", re.I), 76,
        lambda m: {"event": "security.ssh_auth_success", "operation": "ssh_authentication", "component": "sshd", "platform": "auth", "kind": "observation"}),
    Rule("system.searx.captcha", re.compile(r"\bsearx\.[\w.]+(?:\.(?P<engine>[A-Za-z0-9_. -]+))?:.*?\b(?:SearxEngineCaptchaException|CAPTCHA)\b", re.I), 92,
        lambda m: {"event": "search.engine_blocked", "cause": "captcha_challenge", "operation": "search_engine", "target": _searx_target(m), "component": "searx", "platform": "search", "kind": "incident"}),
    Rule("system.searx.rate-limit", re.compile(r"\bsearx\.[\w.]+(?:\.(?P<engine>[A-Za-z0-9_. -]+))?:.*?\b(?:SearxEngineTooManyRequestsException|Too many request)\b", re.I), 92,
        lambda m: {"event": "search.engine_rate_limited", "cause": "rate_limited", "operation": "search_engine", "target": _searx_target(m), "component": "searx", "platform": "search", "kind": "incident"}),
    Rule("system.searx.access-denied", re.compile(r"\bsearx\.[\w.]+(?:\.(?P<engine>[A-Za-z0-9_. -]+))?:.*?\b(?:SearxEngineAccessDeniedException|HTTP error 403)\b", re.I), 91,
        lambda m: {"event": "search.engine_access_denied", "cause": "access_denied", "operation": "search_engine", "target": _searx_target(m), "component": "searx", "platform": "search", "kind": "incident"}),
    Rule("system.searx.engine-failure", re.compile(r"\bsearx\.[\w.]+:.*?(?P<engine>[A-Za-z0-9_. -]+):.*?\b(?:engine INIT failed|init engine failed|can't register engine|Too many request|HTTP error \d+)\b", re.I), 89,
        lambda m: {"event": "search.engine_failure", "cause": "search_engine_failure", "operation": "search_engine", "target": m.group("engine"), "component": "searx", "platform": "search", "kind": "incident"}),
    Rule("system.searx.network", re.compile(r"\bsearx\.network\.[\w.-]+:.*?\bHTTP Request failed\b", re.I), 84,
        lambda m: {"event": "search.network_failure", "cause": "search_network_failure", "operation": "search_request", "component": "searx", "platform": "search", "kind": "incident"}),
    Rule("system.searx", re.compile(r"\bsearx(?:ng)?(?:\.service)?(?:\[\d+\])?:", re.I), 70,
        lambda m: {"event": "search.service_event", "operation": "search", "target": "searx", "component": "searx", "platform": "search", "kind": "observation"}),
    Rule("system.hardware.wifi-missed-beacons", re.compile(r"\biwlwifi\s+(?P<device>[0-9a-f:.]+):\s+missed beacons exceeds threshold", re.I), 83,
        lambda m: {"event": "hardware.wifi_missed_beacons", "cause": "wireless_missed_beacons", "operation": "wireless_telemetry", "target": m.group("device"), "device": m.group("device"), "component": "iwlwifi", "platform": "linux", "kind": "observation"}),
    Rule("system.hardware.wifi-missed-beacons-continuation", re.compile(r"\biwlwifi\s+(?P<device>[0-9a-f:.]+):\s+missed_beacons:\d+,\s+missed_beacons_since_rx:\d+", re.I), 83,
        lambda m: {"event": "hardware.wifi_missed_beacons", "cause": "wireless_missed_beacons", "operation": "wireless_telemetry", "target": m.group("device"), "device": m.group("device"), "component": "iwlwifi", "platform": "linux", "kind": "continuation"}),
    Rule("system.hardware.cpu-vulnerability", re.compile(r"\b(?:MDS CPU bug|VMSCAPE:|Spectre|Meltdown|data leak possible|SMT on, STIBP is required)\b", re.I), 82,
        lambda m: {"event": "hardware.cpu_vulnerability", "cause": "cpu_vulnerability", "operation": "hardware_audit", "component": "kernel", "platform": "linux", "kind": "observation"}),
    Rule("system.hardware.firmware-bug", re.compile(r"\[Firmware Bug\]:\s*(?P<detail>.+)", re.I), 80,
        lambda m: {"event": "hardware.firmware_bug", "cause": "firmware_bug", "operation": "firmware_probe", "component": "kernel", "platform": "linux", "kind": "observation"}),
    Rule("system.hardware.microcode-error", re.compile(r"\bmicrocode\b.*?\b(?:error|failed|detected)\b", re.I), 82,
        lambda m: {"event": "hardware.microcode_error", "cause": "microcode_error", "operation": "hardware_probe", "component": "kernel", "platform": "linux", "kind": "incident"}),
    Rule("system.networkmanager", re.compile(r"\bNetworkManager(?:\[\d+\])?:", re.I), 55,
        lambda m: {"event": "network.manager_event", "operation": "network_management", "component": "NetworkManager", "platform": "network", "kind": "observation"}),
    Rule("system.xrdp", re.compile(r"\bxrdp(?:-sesman)?(?:\.service)?(?:\[\d+\])?:", re.I), 50,
        lambda m: {"event": "xrdp.session_event", "operation": "remote_desktop", "target": "xrdp", "component": "xrdp", "platform": "xrdp", "kind": "observation"}),
    Rule("system.pam", re.compile(r"\bpam_(?P<pam>unix|winbind|systemd)\b(?:\((?P<service>[^)]+)\))?", re.I), 50,
        lambda m: {"event": "security.auth", "operation": "authentication", "target": m.group("service") or "pam", "component": "pam_" + m.group("pam").lower(), "platform": "auth", "kind": "observation"}),
    Rule("system.kernel.continuation.call-trace", re.compile(r"\bCall Trace:\s*|\b<TASK>\b|\bRIP:\s*|\bMem-Info:\b|\bNode \d+ active_anon:|\bCPU:\s+\d+ UID:", re.I), 35,
        lambda m: {"event": "system.kernel_continuation", "cause": "kernel_trace_continuation", "operation": "kernel_trace", "component": "kernel", "kind": "continuation", "platform": "linux"}),
    Rule("system.device", re.compile(r"(?P<device>/dev/[A-Za-z0-9_.-]+)"), 45,
        lambda m: {"device": m.group("device")}),
    Rule("system.errno", re.compile(r"\[Errno\s+(?P<errno>-?\d+)\]", re.I), 60,
        lambda m: {"errno": int(m.group("errno"))}),
)


_CLEAR_SYSTEM = re.compile(
    r"(?:\bsystemd(?:\[\d+\])?:|\bkernel(?:\[[^\]]+\])?:|\bCIFS:\s|\bNetworkManager(?:\[\d+\])?:|\bxrdp(?:-sesman)?(?:\.service)?(?:\[\d+\])?:|\bpam_(?:unix|winbind|systemd)\b|\bsshd(?:\[\d+\])?:|\boom-kill\b|\binvoked oom-killer\b|\bOut of memory:\s|\bFailed with result ['\"][^'\"]+['\"]|\bsearx\.|\bMDS CPU bug\b|\bVMSCAPE:|\[Firmware Bug\]|\bmicrocode\b)",
    re.I,
)


def analyze_system(text: str, raw: object) -> dict[str, object]:
    """Analyze one system record, returning only attributes backed by matches."""
    raw_map = raw if isinstance(raw, dict) else {}
    profile = raw_map.get("profile")
    if not (isinstance(profile, str) and profile.startswith("system:")) and not _CLEAR_SYSTEM.search(text or ""):
        return {}

    candidates: defaultdict[str, list[tuple[int, int, object]]] = defaultdict(list)
    matched: list[dict[str, str]] = []
    event_values: list[str] = []
    cause_values: list[str] = []
    source = text or ""
    for index, rule in enumerate(RULES):
        for found in rule.pattern.finditer(source):
            matched.append({"id": rule.id, "matched": found.group(0)})
            values = rule.emit(found)
            for key, value in values.items():
                candidates[key].append((rule.specificity, -index, value))
                if key == "event" and isinstance(value, str) and value not in event_values:
                    event_values.append(value)
                if key == "cause" and isinstance(value, str) and value not in cause_values:
                    cause_values.append(value)

    result: dict[str, object] = {}
    for key, values in candidates.items():
        result[key] = max(values, key=lambda item: (item[0], item[1]))[2]
    if len(event_values) > 1:
        result["events"] = event_values
    if len(cause_values) > 1:
        result["causes"] = cause_values
    if matched:
        result["matched_rules"] = matched
    return result


__all__ = ["RULES", "analyze_system", "_CLEAR_SYSTEM"]
