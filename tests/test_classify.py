#!/usr/bin/env python3
"""Preserve/improve ASTRA_PI v4 deterministic classification."""
import unittest

from astra.classify import classify_event


class TestClassifyEvent(unittest.TestCase):
    def test_ttfb_watchdog_is_not_a_stream_stall(self):
        raw = {
            "text": "2026-06-21 23:04:38,306 INFO [20260621_230402_895e09] agent.chat_completion_helpers: Disabling openai-codex no-byte TTFB watchdog for large request",
            "sig": "api.stream_stall",
            "profile": "ana-avatar-api-architect",
            "host": "dell",
        }
        result = classify_event(raw)
        self.assertEqual(result["event"], "api.watchdog_tuned")
        self.assertEqual(result["cause"], "watchdog_tuning")
        self.assertEqual(result["provider"], "openai-codex")
        self.assertEqual(result["kind"], "observation")
        self.assertNotIn("label", result)

    def test_real_stream_stall(self):
        raw = {
            "text": "2026-07-07 02:21:08 ERROR agent.chat_completion_helpers: Streaming failed before delivery: Codex stream produced no SSE events",
            "sig": "api.stream_stall",
        }
        result = classify_event(raw)
        self.assertEqual(result["event"], "api.stream_stalled")
        self.assertEqual(result["cause"], "stream_stalled")
        self.assertEqual(result["stream_stage"], "before_delivery")

    def test_discord_typing_is_not_quota_exhaustion(self):
        raw = {
            "text": "2026-08-12 14:10:00 WARNING discord.client: Typing indicator rate-limited",
            "sig": "ratelimit",
        }
        result = classify_event(raw)
        self.assertEqual(result["event"], "platform.operation_rate_limited")
        self.assertEqual(result["platform"], "discord")
        self.assertEqual(result["cause"], "typing_rate_limited")

    def test_llm_quota_exhaustion_extracts_provider_model_status(self):
        raw = {
            "text": "2026-09-08 19:45:12 ERROR [20260908_194500_abcd] agent.conversation_loop: The usage limit has been reached (429 RateLimitError) for openai-codex gpt-5.6-luna",
            "sig": "ratelimit",
        }
        result = classify_event(raw)
        self.assertEqual(result["event"], "api.quota_exhausted")
        self.assertEqual(result["cause"], "usage_quota_exhausted")
        self.assertEqual(result["provider"], "openai-codex")
        self.assertEqual(result["model"], "gpt-5.6-luna")
        self.assertEqual(result["http_status"], 429)

    def test_http_503_is_not_rate_limit(self):
        raw = {
            "text": "2026-07-20 11:15:22 ERROR gateway.platforms.discord: Failed to connect to Discord: 503 Service Unavailable",
            "sig": "ratelimit",
        }
        result = classify_event(raw)
        self.assertEqual(result["cause"], "service_unavailable")
        self.assertEqual(result["http_status"], 503)
        self.assertEqual(result["platform"], "discord")

    def test_critical_path_info_is_not_critical_log_level(self):
        raw = {
            "text": "2026-08-01 10:00:00 INFO [20260801_100000_1234] agent.auxiliary_client: Auxiliary compression: timeout on the critical path; retrying",
            "sig": "critical",
        }
        result = classify_event(raw)
        self.assertEqual(result["log_level"], "INFO")

    def test_traceback_frame_is_continuation(self):
        line = '  File "/home/pi/.hermes/agent/conversation_loop.py", line 145, in _attempt_stream'
        result = classify_event({"text": line, "sig": "traceback"})
        self.assertEqual(result["kind"], "continuation")
        self.assertEqual(result["event"], "runtime.traceback_continuation")

    def test_embedded_tool_errors(self):
        result = classify_event({
            "text": "WARNING agent.tool_executor: Tool terminal returned error (127): /bin/sh: line 1: hermes: command not found",
            "sig": "unclassified",
        })
        self.assertEqual(result["target"], "tool:terminal")
        self.assertEqual(result["cause"], "command_not_found")
        self.assertEqual(result["event"], "tool.execution_failed")

        result = classify_event({
            "text": "WARNING agent.tool_executor: Tool read_file returned error: [Errno 2] No such file or directory: 'missing.py'",
            "sig": "unclassified",
        })
        self.assertEqual(result["target"], "tool:read_file")
        self.assertEqual(result["cause"], "file_not_found")
        self.assertEqual(result["errno"], 2)

    def test_system_oom_on_pi_profile(self):
        result = classify_event({
            "text": "NetworkManager invoked oom-killer: gfp_mask=0x1100cca(GFP_HIGHUSER_MOVABLE), order=0, oom_score_adj=0",
            "sig": "unclassified",
            "profile": "system:NetworkManager",
            "host": "RpiClaude",
        })
        self.assertEqual(result["event"], "system.oom_invoked")
        self.assertEqual(result["cause"], "memory_exhausted")
        self.assertEqual(result["domain"], "system")

    def test_does_not_mutate_or_require_dropping_original_fields(self):
        raw = {
            "text": "INFO agent.loop: hello",
            "sig": "unclassified",
            "sev": "watch",
            "custom": {"keep": True},
            "event": "original-event",
        }
        snapshot = dict(raw)
        classify_event(raw)
        self.assertEqual(raw, snapshot)

    def test_recovered_platform_adapter_failed(self):
        line = "Fatal discord adapter error (discord_websocket_health_stale): Discord Gateway WebSocket health check failed: ack_stale"
        result = classify_event({"text": line, "sig": "unclassified"})
        self.assertEqual(result["event"], "platform.adapter_failed")
        self.assertEqual(result["platform"], "discord")
        self.assertEqual(result["cause"], "websocket_unhealthy")

    def test_recovered_database_handle_leak(self):
        line = "2026-09-13 15:12:00 WARNING hermes_state: 5 live SessionDB handles on in this process; each holds its own writer"
        result = classify_event({"text": line, "sig": "unclassified"})
        self.assertEqual(result["event"], "database.handle_leak")
        self.assertEqual(result["cause"], "handle_leak")

    def test_recovered_auth_token_mint_failed(self):
        line = "2026-09-13 12:00:00 ERROR agent.run: Failed to resolve Vertex AI credentials: Your default credentials were not found"
        result = classify_event({"text": line, "sig": "unclassified"})
        self.assertEqual(result["event"], "api.auth_token_mint_failed")
        self.assertEqual(result["cause"], "credential_missing")

    def test_recovered_runtime_event_loop_stalled(self):
        line = "2026-09-13 10:00:00 CRITICAL gateway.shutdown_watchdog: Gateway event loop missed 3 consecutive liveness probes"
        result = classify_event({"text": line, "sig": "unclassified"})
        self.assertEqual(result["event"], "runtime.event_loop_stalled")
        self.assertEqual(result["cause"], "liveness_probes_missed")

    def test_searx_space_separated_engine_rate_limited(self):
        line = "2026-09-13 11:55:37 WARNING:searx.engines.google cse: ErrorContext(searx/engines/google_cse.py, 147, SearxEngineTooManyRequestsException)"
        result = classify_event({"text": line, "profile": "system:user@1000.service", "sig": "unclassified"})
        self.assertEqual(result["event"], "search.engine_rate_limited")
        self.assertEqual(result["cause"], "rate_limited")


if __name__ == "__main__":
    unittest.main()
