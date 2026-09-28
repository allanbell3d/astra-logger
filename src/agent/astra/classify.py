#!/usr/bin/env python3
"""Evidence-preserving, multi-match deterministic log classification (stdlib only).

Ported from ASTRA_PI v4.1 (`log_enrichment_v4.py`) with severity removed. The
stored sig is provenance, never a classifier input. Rule matches retain their
exact evidence; events/causes preserve secondary meanings.
"""
import re

from astra.system_rules import analyze_system, _CLEAR_SYSTEM

RULE_VERSION = "v4.1-corpus"

def rx(pattern):
    return re.compile(pattern, re.I)

PREFIX = re.compile(
    r"^(?:(?P<ts>\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:\d\d)?)\s+)?"
    r"(?P<level>CRITICAL|ERROR|WARNING|WARN|INFO|DEBUG|NOTICE|ALERT|EMERGENCY)\b[ :]*"
    r"(?:\[(?P<session>[^\]]+)\]\s*)?(?:(?P<component>[\w.-]+):\s*)?"
)
LOGGER = re.compile(r"^(?P<component>(?:agent|tools|gateway|hermes_plugins|discord|telegram|searx)\.[\w.]+):\s*")
TS = re.compile(r"^\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:\d\d)?")
PROVIDER_PARAM = rx(r"(?<![\w-])provider\s*[=:]\s*['\"]?([\w./:-]+)")
MODEL_PARAM = rx(r"(?<![\w-])model(?:_name)?\s*[=:]\s*['\"]?([\w./:@+-]+)|\bmodel\s+['\"]([^'\"]+)['\"]")
MODEL_NAME = rx(r"\b(?:gpt-\d[\w.-]*|o[134](?:-[\w.-]+)?|gemini-\d[\w.-]*|claude-\d[\w.-]*|claude-(?:sonnet|opus|haiku)-[\w.-]+|deepseek-(?:chat|reasoner|v\d[\w.-]*)|grok-[\w.-]+)\b")
FALLBACK = rx(r"\b(?:falling back to|fallback(?:\s+(?:provider|model))?[=:])\s*['\"]?([\w./:@+-]+)")
TOOL = rx(r"\bTool\s+['\"]?([\w.-]+)['\"]?\s+(?:returned|completed|failed|timed|execution)|\btool_name[=:]\s*['\"]?([\w.-]+)")
EXCEPTION = re.compile(r"\b(?:[\w]+\.)*([A-Z][A-Za-z0-9]*(?:Error|Exception|Timeout)|gaierror|Forbidden|NotFound|BadRequest|TimedOut)\b")
HTTP = rx(r"\b(?:HTTP(?:/\d(?:\.\d)?)?(?:\s+(?:error|status))?|status(?:_code)?|error\s+code)\s*['\"]?\s*[:=]?\s*['\"]?([45]\d\d)\b|\b([45]\d\d)\s+(?:Bad Request|Unauthorized|Forbidden|Not Found|Request Timeout|Conflict|Gone|Payment Required|Too Many Requests|Internal Server Error|Bad Gateway|Service Unavailable|Gateway Timeout|RESOURCE_EXHAUSTED|UNAVAILABLE|RateLimitError)\b")
CODE = rx(r"['\"](?:code|type)['\"]\s*:\s*['\"]([\w.-]+)['\"]|\berror code\s*[:=]\s*(-?\d+)\b")
RPC = rx(r"\b(?:rpc(?:_code)?|MCP(?:\s+error)?|code)\s*['\"]?\s*[:=]\s*(-32\d{3})\b")
ERRNO = rx(r"\bErrno\s+(-?\d+)\b")
EXIT = rx(r"\bexit(?:_code| code| status)\s*['\"]?\s*[:=]?\s*(-?\d+)\b|\bcode=exited,\s*status=(\d+)\b|\bexited with code\s+(-?\d+)\b")
ATTEMPT = rx(r"\battempt\s+(\d+)(?:\s*(?:/|of)\s*(\d+))?|\bpolling conflict\s*\((\d+)/(\d+)\)")
TUNING = rx(r"\b(?:Disabling|Scaling|Capping)\s+openai-codex\b[^\n]*(?:TTFB|watchdog)")
FRAME = re.compile(r'^\s*(?:File "[^\"]+", line \d+|Traceback \(most recent call last\):|During handling of the above exception|The above exception was the direct cause|[~^]{3,}\s*$)')
CODE_FRAME = re.compile(r'^\s*(?:(?:return|raise|await|yield|async with)\s+|(?:[\w., ()]+)\s*=\s*(?:await\s+|self\.|[\w.]+\()|(?:self\.[\w.]+|[\w]+)\([^\n]*\)\s*$)')

def parse_log(text):
    m = PREFIX.match(text.lstrip())
    if m:
        return m.groupdict(), text.lstrip()[m.end():]
    m = LOGGER.match(text.lstrip())
    if m and re.search(r"(?:Error|Exception|TimedOut|Forbidden|NotFound)$", m.group("component")):
        return {}, text
    return (m.groupdict(), text.lstrip()[m.end():]) if m else ({}, text)

def extract_log_level(text):
    return parse_log(text)[0].get("level")

def is_continuation(text):
    # Prefix-bearing logs are complete records even if they quote code.
    return not PREFIX.match(text.lstrip()) and bool(FRAME.match(text) or CODE_FRAME.match(text))

# Cause rules are ordered from specific mechanisms to generic symptoms. Every
# fired rule is retained; generic symptoms do not erase more precise causes.
# Scope restricts words that have unrelated meanings outside their subsystem.
CAUSES = [
    ("usage_quota_exhausted", r"usage_limit_reached|usage limit has been reached|you have hit your usage limit", "llm"),
    ("allocation_quota_exhausted", r"token.plan quota (?:has been )?exceeded|allocation quota.*exceed", "llm"),
    ("credit_balance_exhausted", r"prepayment credits (?:are )?exhausted|insufficient (?:credits|credit balance)|credit balance.*(?:low|exhaust|insufficient)", "llm"),
    ("quota_exhausted", r"(?:quota.*(?:exceed|exhaust)|(?:exceed|exhaust).*quota)", "llm"),
    ("resource_exhausted", r"\bRESOURCE_EXHAUSTED\b", "llm"),
    ("provider_capacity_exhausted", r"(?:model|server).*(?:high demand|overloaded)|MODEL_CAPACITY_EXHAUSTED|capacity exhausted", "llm"),
    ("payment_required", r"\b402 Payment Required\b|\bpayment required\b", ""),
    ("oauth_refresh_invalid", r"refresh_token_reused|invalid_grant|(?:codex )?token refresh failed|oauth state quarantined", "llm"),
    ("model_not_available_for_integrator", r"model_not_available_for_integrator", "llm"),
    ("model_unsupported", r"not supported when using Codex|not supported.*ChatGPT account|model[^\n]{0,100}(?:not supported|unsupported)", "llm"),
    ("model_retired", r"model[^\n]{0,100}(?:reached its end of life|retired|no longer available)", "llm"),
    ("model_not_found", r"model_not_found|model[^\n]{0,100}(?:does not exist|not found)", "llm"),
    ("context_window_exceeded", r"context_length_exceeded|context (?:length|window).*(?:exceed|too (?:long|large))|maximum context length", "llm"),
    ("request_too_large", r"serialized_request_too_large|request (?:body |payload )?too large|\b413 (?:Request|Payload)|serialized_request_body_bytes.*(?:exceed|limit)", "llm"),
    ("invalid_parameter", r"unsupported_parameter|invalid_parameter|invalid (?:argument|parameter)|unknown (?:argument|parameter)|Unrecognized request argument|does not support parameter", "llm"),
    ("content_policy_blocked", r"content was flagged for possible cybersecurity risk|content_policy_violation|blocked by content policy", "llm"),
    ("missing_access", r"\bMissing Access\b", "platform"),
    ("unknown_channel", r"\bUnknown Channel\b", "platform"),
    ("unknown_interaction", r"\bUnknown interaction\b|interaction.*(?:expired|no longer valid)", "platform"),
    ("missing_permissions", r"\bMissing Permissions\b", "platform"),
    ("polling_conflict", r"polling conflict|terminated by other getUpdates request", "platform"),
    ("token_in_use", r"bot token already in use", "platform"),
    ("approval_timeout", r"approval (?:request )?timed out|approval timeout|timed out waiting for (?:user )?approval", "tool"),
    ("user_denied", r"(?:user|operator) (?:denied|rejected)|denied by (?:the )?user", "tool"),
    ("protected_file", r"protected (?:file|path)|cannot (?:modify|write).*protected", "tool"),
    ("curator_policy_refusal", r"curator.*(?:refus|only|patch)", "tool"),
    ("patch_ambiguous_target", r"(?:old_string|target|match).*(?:ambiguous|multiple (?:matches|occurrences))|matches found.*(?:unique|narrow)", "tool"),
    ("patch_identical_old_new", r"old_string.*new_string.*(?:identical|same)|no changes.*identical", "tool"),
    ("patch_target_not_found", r"could not find (?:a )?match for old_string|old_string.*not found|target string.*not found", "tool"),
    ("patch_validation_failed", r"patch validation failed", "tool"),
    ("subagent_not_live", r"subagent.*(?:not live|not (?:running|found)|unknown|does not exist)", "tool"),
    ("policy_blocked", r"blocked by smart approval|not allowlisted|\bBLOCKED:|foreground command uses '&'|(?:command|execution).*blocked by.*policy", "tool"),
    ("git_index_lock", r"index\.lock|another git process seems to be running|Unable to create '[^']+\.lock': File exists", ""),
    ("git_not_repository", r"not a git repository", ""),
    ("git_subrepo_no_commit", r"does not have a commit checked out", ""),
    ("git_timeout", r"git.*(?:timed out|timeout)", "tool"),
    ("database_corrupt", r"not a valid SQLite database|database disk image is malformed|file is not a database|KanbanDbCorruptError|Cron database corrupted and unrepairable", ""),
    ("dependency_vulnerable", r"linked SQLite .*is vulnerable to the WAL-reset corruption bug", ""),
    ("json_parse_error", r"Expecting (?:property name|value|',' delimiter)|JSONDecodeError", ""),
    ("database_locked", r"database (?:table )?is locked", ""),
    ("table_missing", r"no such table:", ""),
    ("dns_resolution_failed", r"Temporary failure in name resolution|Name or service not known|ClientConnectorDNSError|nodename nor servname provided|name resolution failed|DNS lookup failed", ""),
    ("network_unreachable", r"Network is unreachable|No route to host", ""),
    ("connection_refused", r"Connection refused", ""),
    ("connection_reset", r"Connection reset(?: by peer)?|ConnectionResetError", ""),
    ("broken_pipe", r"Broken pipe|BrokenPipeError", ""),
    ("tls_verification_failed", r"CERTIFICATE_VERIFY_FAILED|certificate verify failed", ""),
    ("auth_invalid", r"\bauth_invalid\b|\b401 Unauthorized\b|invalid (?:API key|authentication|token)|authentication failed", ""),
    ("permission_denied", r"Permission denied|\b403 Forbidden\b", ""),
    ("credential_missing", r"credentials (?:are )?missing|(?:API key|token|credentials).*not (?:set|configured|found)", ""),
    ("module_not_found", r"ModuleNotFoundError|No module named", ""),
    ("command_not_found", r"command not found|command .*not found", "tool"),
    ("path_not_found", r"(?:search path|directory|path).*does not exist|path not found", "tool"),
    ("file_not_found", r"No such file or directory|FileNotFoundError|file not found", ""),
    ("stream_ttfb_timeout", r"no bytes within TTFB|TTFB (?:threshold exceeded|cutoff)|no parsed stream event|produced no SSE events within", "llm"),
    ("stream_stalled", r"produced no SSE events|Stream stale for|stream stalled|no chunks received", "llm"),
    ("stream_dropped", r"stream drop on attempt|stream ended with no finish_reason|stream closed unexpectedly", "llm"),
    ("empty_response", r"empty response|empty model response", "llm"),
    ("read_timeout", r"\bReadTimeout\b|read timed out|read timeout", ""),
    ("connect_timeout", r"\bConnectTimeout\b|connect(?:ion)? timed out|connect timeout", ""),
    ("connection_closed", r"\bConnection closed\b|ConnectionClosedError", ""),
    ("rate_limited", r"rate[- ]?limit(?:ed|ing)?|Too Many Requests|\bFlood control\b|\bFloodWait\b", ""),
    ("service_unavailable", r"\b503 Service Unavailable\b|\bUNAVAILABLE\b.*(?:server|service)", ""),
    ("bad_gateway", r"\b502 Bad Gateway\b", ""),
    ("config_parse_error", r"python-dotenv could not parse statement|terminal policy unavailable: cannot parse|while (?:parsing a block mapping|scanning a simple key)|Failed to process config\.yaml|mapping values are not allowed here", ""),
    ("credential_missing", r"neither password_hash nor password is configured", ""),
    ("tool_incompatible_model", r"returned image content for non-vision model", "tool"),
    ("prompt_missing", r"Stored system prompt.*is null", ""),
    ("command_name_collision", r"slash command.*collides with a core Hermes command", ""),
    ("browser_charge_authorization_failed", r"BILLING_ERROR|Charge authorization failed|insufficient_funds", "tool"),
    ("required_argument_missing", r"--database is required|missing (?:a )?required (?:argument|parameter)", "tool"),
    ("dependency_check_failed", r"check_fn\s+\S+\s+returned False", "tool"),
    ("context_engine_missing", r"Context engine ['\"]\w+['\"] not found", ""),
    ("context_engine_uncopyable", r"Context engine.*could not be safely copied|cannot pickle 'sqlite3.Connection'", ""),
    ("context_file_truncated", r"Context file.*truncated", ""),
    ("instance_conflict", r"another gateway instance is already running", ""),
    ("stream_consumer_race", r"Normal final-send NOT suppressed despite active stream consumer", ""),
    ("heartbeat_blocked", r"heartbeat blocked for more than|heartbeat.*blocked", "platform"),
    ("websocket_unhealthy", r"websocket.*(?:unhealthy|not healthy|closed)|WebSocket.*(?:closed|failed)|discord_websocket_health_stale|ack_stale|Aborted unresponsive.*WebSocket|unhealthy.*client", "platform"),
    ("adapter_error", r"Fatal (?:discord|telegram) adapter error", "platform"),
    ("polling_retries_exhausted", r"polling.*(?:retries|reconnect).*exhaust|polling.*giving up", "platform"),
    ("handle_leak", r"\d+ live SessionDB handles on", ""),
    ("memory_pressure", r"system memory pressure is elevated", ""),
    ("zero_servers_discovered", r"Background MCP discovery.*(?:zero|no) connected servers", ""),
    ("circuit_open", r"skipped by open circuit|fallback chain exhausted", "llm"),
    ("token_mint_failed", r"could not mint token|Failed to resolve Vertex AI credentials", "llm"),
    ("config_unknown_keys", r"unknown config keys ignored:", ""),
    ("config_apply_failed", r"Could not apply.*config", ""),
    ("process_timeout", r"subprocess\.TimeoutExpired", ""),
    ("voice_transcode_failed", r"voice transcode to [\w/]+ failed", ""),
    ("shutdown_requested", r"Shutdown context: signal=SIGTERM", ""),
    ("button_approval_failed", r"Button-based approval failed", ""),
    ("liveness_probes_missed", r"missed \d+ consecutive liveness probes", ""),
    ("plugin_load_error", r"Failed to load plugin|dictionary changed size during iteration", ""),
    ("session_closed", r"Session is closed|session.*(?:closed|rejected)", ""),
    ("not_connected", r"Not connected|No connected messaging platforms remain", ""),
    ("git_skipped", r"Git command skipped", ""),
    ("address_in_use", r"Address already in use|Could not bind", ""),
    ("turn_lease_error", r"turn[- ]lease (?:contention|wait timed out|timeout)|Rejecting turn.*after turn-lease", ""),
    ("summarization_failed", r"LLM summarization failed", ""),
    ("warmup_timeout", r"warm-up still running after", ""),
    ("heartbeat_dead", r"client appears dead", ""),
    ("send_path_degraded", r"send_path_degraded|Fallback send also failed|Failed to deliver response after", ""),
    ("idle_timeout", r"(?:backend idle for \d+s|idle for \d+s with no client)", ""),
    ("search_backend_failed", r"(?:web_search|web_extract) backend.*failed|local[-_ ]web[-_ ]router.*failed", ""),
    ("websocket_lag", r"Can't keep up.*websocket is \d+.*behind|shard.*websocket is \d+.*behind", ""),
    ("session_not_found", r"Mirror:\s+no session found for", ""),
    ("sealed_reasoning_dropped", r"Dropping reasoning item minted by.*encrypted_content is sealed", ""),
    ("stream_superseded", r"Codex streaming attempt superseded by a newer stream", ""),
    ("compression_stalled", r"Context compression stalled", ""),
    ("compression_superseded", r"Discarding late compression candidate", ""),
    ("rebuild_limit_exceeded", r"Rebuilt-message restart limit.*exceeded", ""),
    ("prefetch_timeout", r"Memory provider.*prefetch timed out", ""),
    ("context_discovery_skipped", r"skipping project-context discovery:\s*working-directory resolution (?:failed|fell back)", ""),
    ("lifecycle_budget_exhausted", r"lifecycle guard scan budget exhausted", ""),
    ("unknown_skill", r"Unknown skill\(s\) requested,\s*skipping", ""),
    ("task_exception_unretrieved", r"Task exception was never retrieved", ""),
    ("cancellation_timeout", r"did not acknowledge cancellation", ""),
    ("concurrent_turn", r"concurrent Hermes turn", ""),
    ("stale_code_version", r"process is running code from.*but", ""),
    ("migration_in_progress", r"holds a progress lease", ""),
    ("provider_not_configured", r"provider not configured", ""),
    ("deleted_inode_held", r"a live process holds a deleted state\.db", ""),
    ("routing_save_failed", r"(?:state\.db\s+)?routing save failed", ""),
    ("websocket_slow", r"ws write slow", ""),
    ("rpc_rejected", r"session-scoped RPC rejected", ""),
    ("timeout", r"\btimed out\b|\bTimeoutError\b|\bTimeoutException\b|\[deadline\]|\bdid not finish within", ""),
    ("connection_error", r"\bConnection error\b|\bCannot connect to host\b|\bAPIConnectionError\b|All connection attempts failed|connection failed|path unreachable|failed to connect", ""),
    ("auth_invalid", r"\bauth_invalid\b|\b401 Unauthorized\b|invalid (?:API key|authentication|token)|API key (?:is )?not valid|authentication failed", ""),
    ("credential_missing", r"credentials (?:are )?missing|(?:API key|token|credentials).*not (?:set|configured|found)|no \w+ authentication found", ""),
]
CAUSE_RULES = [(name, rx(pattern), scope) for name, pattern, scope in CAUSES]

# Event rules independently describe the observed action. Specific outer
# envelopes precede symptoms; all event matches remain queryable.
EVENTS = [
    ("tool.execution_blocked", r"Tool\s+\S+\s+returned error", "tool", "execute", {"approval_timeout", "user_denied", "protected_file", "policy_blocked", "curator_policy_refusal"}),
    ("tool.validation_failed", r"Tool\s+\S+\s+returned error|patch validation failed", "tool", "patch", {"patch_ambiguous_target", "patch_identical_old_new", "patch_target_not_found", "patch_validation_failed"}),
    ("tool.timeout", r"Tool\s+\S+\s+(?:returned error|timed out)|\[deadline\]|timed out after|\bdid not finish within|MCP shutdown did not finish within", "tool", "execute", {"timeout", "approval_timeout", "command_timeout", "git_timeout"}),
    ("tool.execution_failed", r"Tool\s+\S+\s+returned error|Tool\s+\S+\s+failed", "tool", "execute", None),
    ("tool.completed", r"Tool\s+\S+\s+completed", "tool", "execute", None),
    ("tool.mcp_failed", r"MCP server\s+['\"]?\S+.*failed initial connection", "tool", "connect", None),
    ("tool.check_fn_false", r"check_fn\s+\S+\s+returned False", "tool", "dependency_check", None),
    ("security.policy_unavailable", r"terminal policy unavailable: cannot parse", "tool", "load_policy", None),
    ("platform.operation_rate_limited", r"Typing indicator rate-limited", "platform", "typing", None),
    ("platform.send_failed", r"Failed to send.*(?:message|document|photo)|send (?:message )?failed|Fallback send also failed|Failed to deliver response after", "platform", "send_message", None),
    ("platform.edit_failed", r"Failed to edit|error editing message|edit.*message.*failed", "platform", "edit_message", None),
    ("platform.polling_conflict", r"Telegram polling conflict|terminated by other getUpdates request", "platform", "get_updates", None),
    ("platform.startup_conflict", r"bot token already in use", "platform", "connect", None),
    ("platform.reconnect_scheduled", r"Reconnect\s+(?:discord|telegram)\s+error|attempting a reconnect|next retry in|Connecting to (?:discord|telegram)\s+\(attempt|\[Homeassistant\] Reconnect", "platform", "reconnect", None),
    ("platform.auth_failed", r"auth failed|authentication failed", "platform", "authenticate", None),
    ("platform.connect_failed", r"(?:Failed to connect|connect(?:ion)?\s+failed|failed to connect)|\bfailed to connect\b|connect(?:ion)? timed out|Cannot connect to host|path unreachable", "platform", "connect", None),
    ("platform.heartbeat_blocked", r"heartbeat blocked", "platform", "heartbeat", None),
    ("platform.adapter_failed", r"Fatal (?:discord|telegram) adapter error|No adapter could be created", "platform", "adapter", None),
    ("platform.disconnected", r"\bdisconnected\b|websocket.*(?:unhealthy|closed)|WebSocket.*health check failed|ack_stale|shard.*stopped|Timed out closing unhealthy.*client|Aborted unresponsive.*WebSocket|client appears dead|No connected messaging platforms remain|Gateway started with no connected platforms", "platform", "connect", None),
    ("platform.polling_failed", r"polling.*(?:failed|error)|get_updates.*(?:failed|error)", "platform", "get_updates", None),
    ("platform.rate_limited", r"rate[- ]?limit|Too Many Requests|Flood control", "platform", "request", None),
    ("platform.discovery", r"Discovering Telegram API fallback", "platform", "dns_discovery", None),
    ("platform.endpoint_failed", r"(?:IPv4 Telegram API IP|Fallback IP)\s+[\d.]+\s+failed:", "platform", "connect", None),
    ("platform.network_error", r"Telegram network error", "platform", "poll", None),
    ("platform.command_registered", r"Registered /\w+ command", "platform", "register_command", None),
    ("platform.configuration_disabled", r"(?:disabled|not enabled).*config|configuration.*disabled", "platform", "configure", None),
    ("api.auth_degraded", r"Copilot token exchange degraded", "llm", "token_exchange", None),
    ("api.auth_failed", r"token refresh failed|oauth state quarantined", "llm", "token_refresh", None),
    ("api.stream_stalled", r"produced no SSE events|no parsed stream event|Stream stale for|stream stalled|no chunks received|TTFB threshold exceeded", "llm", "streaming", None),
    ("api.stream_failed", r"Streaming failed (?:before|after)|stream drop on attempt|stream ended with no finish_reason", "llm", "streaming", None),
    ("api.retry_scheduled", r"Retrying API call|retrying.*(?:request|inference)", "llm", "inference", None),
    ("api.request_failed", r"API call failed|(?:inference|completion|request).*failed|\berror_type=|Non-retryable client error", "llm", "inference", None),
    ("api.quota_exhausted", r"usage_limit_reached|usage limit has been reached|prepayment credits|quota.*exceed|RESOURCE_EXHAUSTED", "llm", "inference", None),
    ("api.model_resolution_failed", r"not supported when using Codex|model_not_available_for_integrator|model.*(?:retired|not supported|end of life|no longer available)", "llm", "resolve_model", None),
    ("api.telemetry", r"API call #\d+:|Turn ended: reason=", "llm", "inference", None),
    ("api.fallback_selected", r"falling back to.*(?:model|provider)|fallback (?:model|provider)[=:]", "llm", "select_model", None),
    ("api.provider_cooldown", r"Auxiliary: marking\s+\S+\s+unhealthy", "llm", "provider_cooldown", None),
    ("api.request_aborted", r"auxiliary client aborted", "llm", "abort_request", None),
    ("api.credential_rotated", r"credential pool: marking.*exhausted.*rotating", "llm", "rotate_credential", None),
    ("api.title_generation_failed", r"Title generation failed", "llm", "title_generation", None),
    ("api.reference_model_failed", r"MoA reference model.*failed:", "llm", "inference", None),
    ("git.command_failed", r"git command failed:|fatal: not a git repository", "", "git", None),
    ("database.corruption_detected", r"not a valid SQLite database|KanbanDbCorruptError|database disk image is malformed", "", "query", None),
    ("database.query_failed", r"no such table:|database is locked|sqlite3\.OperationalError", "", "query", None),
    ("runtime.dispatch_failed", r"kanban (?:dispatcher|notifier):? tick failed|kanban dispatcher: tick failed", "", "dispatch", None),
    ("runtime.dispatch_stalled", r"kanban dispatcher stuck", "", "dispatch", None),
    ("runtime.config_warning", r"could not parse statement|Failed to process config\.yaml|Context engine.*(?:not found|could not be safely copied)|falling back to default config|unknown config keys ignored:|No watch_domains, watch_entities|Could not apply live compression config", "", "configure", None),
    ("runtime.context_truncated", r"Context file.*truncated", "", "load_context", None),
    ("runtime.startup_conflict", r"another gateway instance is already running", "", "start", None),
    ("runtime.gateway_exit", r"Previous gateway exited cleanly|gateway exiting cleanly|Stopping gateway(?: for restart)?|Shutdown context: signal=|Gateway drain timed out|Fatal-error handling for \S+ timed out", "", "shutdown", None),
    ("runtime.stream_race", r"Normal final-send NOT suppressed despite active stream consumer", "", "send_message", None),
    ("runtime.event_loop_stalled", r"event loop stalled|event loop.*blocked|event loop missed \d+ consecutive liveness probes", "", "event_loop", None),
    ("security.audit_warning", r"security posture audit|SSH password authentication is enabled|dashboard\.basic_auth|No env user allowlists configured", "", "audit", None),
    ("runtime.hook_registered", r"shell hook registered:", "", "register_hook", None),
    ("runtime.config_write_failed", r"Failed to persist model switch", "", "persist_config", None),
    ("runtime.prompt_rebuilt", r"Stored system prompt.*is null", "", "load_prompt", None),
    ("runtime.hook_skipped", r"Hook .*callback.*skipped after previous timeout or while still running", "", "invoke_hook", None),
    ("runtime.command_registration_skipped", r"slash command.*collides with a core Hermes command", "", "register_command", None),
    ("runtime.memory_telemetry", r"^\[MEMORY\] rss=", "", "monitor_memory", None),
    ("runtime.broadcast_failed", r"broadcast send failed for subscriber", "", "broadcast", None),
    ("database.repair_failed", r"Failed to auto-repair jobs\.json", "", "repair", None),
    ("database.corruption_detected", r"Cron database corrupted and unrepairable", "", "query", None),
    ("database.compatibility_mitigation", r"linked SQLite .*is vulnerable to the WAL-reset corruption bug|on-disk journal_mode was delete and has been switched", "", "configure_journal", None),
    ("database.handle_leak", r"\d+ live SessionDB handles on", "", "session_db", None),
    ("tool.dependency_missing", r"Tool search assembly skipped: No module named", "", "load_tool", None),
    ("tool.model_incompatible", r"returned image content for non-vision model", "tool", "deliver_result", None),
    ("tool.timeout", r"browser '[^']+' timed out", "tool", "browser", None),
    ("tool.browser_session_failed", r"Cloud provider.*failed.*Failed to create Browser Use session", "tool", "create_session", None),
    ("search.extract_failed", r"web_extract backend.*failed|local[-_ ]web[-_ ]router (?:extract )?failed", "", "extract", None),
    ("search.search_failed", r"web_search backend.*failed|local[-_ ]web[-_ ]router search failed", "", "search", None),
    ("search.query_started", r"^Web search via local-web-router:", "tool", "search", None),
    ("api.circuit_opened", r"LCM summary route (?:circuit opened|skipped by open circuit)|LCM summary fallback chain exhausted", "", "context_compression", None),
    ("platform.mcp_zero_servers", r"Background MCP discovery.*(?:zero|no) connected servers", "", "mcp_discovery", None),
    ("api.auth_token_mint_failed", r"Failed to resolve Vertex AI credentials|could not mint token", "", "token_mint", None),
    ("runtime.process_timeout", r"subprocess\.TimeoutExpired", "", "subprocess", None),
    ("runtime.voice_transcode_failed", r"voice transcode to [\w/]+ failed", "", "voice_transcode", None),
    ("platform.approval_fallback", r"Button-based approval failed.*falling back", "", "approval", None),
    ("runtime.relay_orphan_drained", r"Hermes Relay drained \d+ orphaned scope", "", "relay", None),
    ("api.slow_models_session_visit", r"\[SLOW\] models\.session_visit total=", "llm", "session_visit", None),
    ("runtime.memory_pressure", r"kanban dispatch: system memory pressure is elevated", "", "dispatch", None),
    ("api.summarization_failed", r"LLM summarization failed", "llm", "context_compression", None),
    ("runtime.plugin_load_failed", r"Failed to load plugin", "", "load_plugin", None),
    ("runtime.prompt_send_failed", r"Prompt send failed", "", "send_prompt", None),
    ("runtime.warmup_timeout", r"Turn-machinery warm-up still running after", "", "warmup", None),
    ("git.command_skipped", r"Git command skipped", "", "git", None),
    ("runtime.port_bind_failed", r"Could not bind [\d.]+:\d+", "", "bind_port", None),
    ("runtime.turn_lease_error", r"turn[- ]lease (?:contention|wait timed out|timeout)|Rejecting turn.*after turn-lease", "", "turn_lease", None),
    ("tool.circuit_opened", r"tirith circuit breaker opened", "tool", "circuit_breaker", None),
    ("platform.websocket_lag", r"Can't keep up.*websocket is \d+.*behind|shard.*websocket is \d+.*behind", "platform", "gateway", None),
    ("runtime.session_not_found", r"Mirror:\s+no session found for", "", "mirror", None),
    ("runtime.idle_exit", r"(?:backend idle for \d+s|idle for \d+s with no client).*exiting", "", "idle_exit", None),
    ("api.reasoning_dropped", r"Dropping reasoning item minted by", "", "reasoning", None),
    ("runtime.stream_superseded", r"Codex streaming attempt superseded by a newer stream", "llm", "stream", None),
    ("api.compression_stalled", r"Context compression stalled", "llm", "context_compression", None),
    ("api.compression_superseded", r"Discarding late compression candidate", "llm", "context_compression", None),
    ("runtime.rebuild_loop_aborted", r"Rebuilt-message restart limit.*exceeded", "", "rebuild_prompt", None),
    ("tool.prefetch_timeout", r"Memory provider.*prefetch timed out", "", "prefetch", None),
    ("runtime.context_discovery_skipped", r"skipping project-context discovery:\s*working-directory resolution (?:failed|fell back)", "", "context_discovery", None),
    ("runtime.lifecycle_budget_exhausted", r"lifecycle guard scan budget exhausted", "", "lifecycle_guard", None),
    ("runtime.unknown_skill_requested", r"Unknown skill\(s\) requested,\s*skipping", "", "load_skill", None),
    ("runtime.unretrieved_task_exception", r"Task exception was never retrieved", "", "async_task", None),
    ("api.client_aborted", r"OpenAI client aborted\s*\(codex_ttfb_kill", "llm", "client_abort", None),
    ("runtime.cancellation_unacknowledged", r"Background review did not acknowledge cancellation", "", "cancellation", None),
    ("runtime.relay_instrumentation_skipped", r"Skipping Relay instrumentation for concurrent Hermes turn", "", "relay", None),
    ("runtime.code_version_stale", r"refused:\s*This process is running code from", "", "web_api", None),
    ("runtime.startup_watchdog_warning", r"Gateway startup exceeded \d+s but phase", "", "watchdog", None),
    ("api.fallback_failed", r"Fallback to \w+ failed:\s*provider not configured", "", "fallback", None),
    ("database.routing_save_failed", r"(?:state\.db\s+)?routing save failed", "", "routing_save", None),
    ("database.deleted_inode_held", r"FATAL:\s+a live process holds a deleted state\.db", "", "state_db", None),
    ("database.wal_retired", r"Captured the retired WAL generation", "", "state_db", None),
    ("platform.websocket_disconnect", r"ws send failed.*WebSocketDisconnect", "platform", "send_ws", None),
    ("platform.rpc_rejected", r"session-scoped RPC rejected", "platform", "rpc", None),
    ("platform.websocket_slow", r"ws write slow", "platform", "send_ws", None),
]
EVENT_RULES = [(name, rx(pattern), scope, op, causes) for name, pattern, scope, op, causes in EVENTS]

def _first_group(match):
    return next((g for g in match.groups() if g is not None), None) if match else None

def _unique(values):
    return list(dict.fromkeys(v for v in values if v is not None))

def classify_event(raw, rule_version=RULE_VERSION):
    if not isinstance(raw, dict) or not isinstance(raw.get("text", ""), str):
        raise ValueError("event must be an object with string text")
    text = raw.get("text", "")
    prefix, body = parse_log(text)
    profile = raw.get("profile") or ""
    attrs = dict.fromkeys(("event domain provider model platform component target operation cause exception http_status provider_code rpc_code errno exit_code signal attempt max_attempts log_level tool fallback_provider fallback_model stream_stage event_ts event_ts_basis".split()))
    matches, evidence, confidence, conflicts = [], {}, {}, []

    def put(field, value, source=None, certainty="explicit"):
        if value is not None:
            attrs[field] = value
            confidence[field] = certainty
            if source is not None:
                evidence[field] = source

    def hit(rule_id, match):
        matches.append({"id": rule_id, "matched": match.group(0) if hasattr(match, "group") else match})

    put("log_level", prefix.get("level"), "log_prefix")
    put("component", prefix.get("component"), "log_prefix")
    # The textual timestamp is event time; its timezone is left as written.
    tm = TS.match(text)
    if tm:
        put("event_ts", tm.group().replace(" ", "T").replace(",", "."), "text_prefix")
        put("event_ts_basis", "text")
    elif raw.get("ts") and raw["ts"] != "UNKNOWN":
        put("event_ts", raw["ts"], "stored_ts", "derived")
        put("event_ts_basis", "stored_unverified")

    # SDK exception names and model families are not provider-route evidence.
    fallback_match = FALLBACK.search(body)
    primary_text = body[:fallback_match.start()] if fallback_match else body
    provider_matches = list(PROVIDER_PARAM.finditer(primary_text))
    if provider_matches:
        values = _unique(m.group(1).rstrip(".,;") for m in provider_matches)
        if len(values) == 1:
            put("provider", values[0], provider_matches[0].group())
        else:
            conflicts.append({"field": "provider", "candidates": values})
    else:
        routes = [("openai-codex", r"\bopenai-codex\b|chatgpt\.com/backend-api/codex|\bCodex (?:stream|token|OAuth)"), ("openrouter", r"openrouter\.ai"), ("gemini", r"generativelanguage\.googleapis\.com|\bGeminiAPIError\b"), ("vertex", r"aiplatform\.googleapis\.com"), ("openai", r"api\.openai\.com"), ("xai-oauth", r"\bxai-oauth\b"), ("copilot", r"\bCopilot token exchange")]
        route_values = [(p, re.search(pat, primary_text, re.I)) for p, pat in routes]
        route_values = [(p, m) for p, m in route_values if m]
        if len(route_values) == 1:
            put("provider", route_values[0][0], route_values[0][1].group(), "derived")
        elif len(route_values) > 1:
            conflicts.append({"field": "provider", "candidates": [p for p, _ in route_values]})
    model_matches = list(MODEL_PARAM.finditer(primary_text))
    if model_matches:
        models = _unique(_first_group(m).rstrip(".,;") for m in model_matches)
        if len(models) == 1:
            put("model", models[0], model_matches[0].group())
        else:
            conflicts.append({"field": "model", "candidates": models})
    else:
        models = _unique(m.group() for m in MODEL_NAME.finditer(primary_text))
        if len(models) == 1:
            put("model", models[0], models[0])
        elif len(models) > 1:
            conflicts.append({"field": "model", "candidates": models})
    route_model = rx(r"MoA reference model\s+([\w-]+):([\w./-]+)").search(primary_text)
    cooldown_provider = rx(r"Auxiliary: marking\s+([\w-]+)\s+unhealthy").search(primary_text)
    if route_model:
        put("provider", route_model.group(1), route_model.group())
        put("model", route_model.group(2), route_model.group())
    elif cooldown_provider and not attrs["provider"]:
        put("provider", cooldown_provider.group(1), cooldown_provider.group())
    if fallback_match:
        tail = body[fallback_match.start():]
        fp = PROVIDER_PARAM.search(tail)
        fm = MODEL_PARAM.search(tail) or MODEL_NAME.search(tail)
        if fp:
            put("fallback_provider", fp.group(1), fp.group())
        if fm:
            put("fallback_model", _first_group(fm) if fm.groups() else fm.group(), fm.group())
    for field in ("fallback_provider", "fallback_model"):
        explicit_fallback = rx(r"\b" + field + r"=['\"]?([\w./:-]+)").search(body)
        if explicit_fallback:
            put(field, explicit_fallback.group(1), explicit_fallback.group())

    comp = attrs["component"] or ""
    for platform in ("discord", "telegram", "homeassistant", "slack", "whatsapp", "wecom"):
        if (platform in comp.lower() or
            re.search(r"\[" + platform + r"\]", body, re.I) or
            re.search(r"\b" + platform + r"\s+(?:adapter|gateway|websocket|client|transport|polling|bot token|typing|failed to connect|error|connect timed out)", body, re.I) or
            re.search(r"\b(?:Reconnect|Failed to connect to|Discovering|Fatal|handling for|closing unhealthy|aborted unresponsive|Platform)\s+['\"]?" + platform + r"\b", body, re.I)):
            put("platform", platform, comp or body[:100], "derived")
            break
    tool_match = TOOL.search(body)
    if not tool_match:
        tool_match = rx(r"Tool\s+(\w+)\s+returned image content").search(body)
    if not tool_match:
        tool_match = rx(r"\[deadline\]\s*['\"]?(\w+)").search(body)
    if tool_match:
        put("tool", _first_group(tool_match), tool_match.group())
        put("target", "tool:" + attrs["tool"], tool_match.group(), "derived")
    mcp = rx(r"MCP server\s+['\"]?([\w.-]+)").search(body)
    if mcp:
        put("target", "mcp:" + mcp.group(1), mcp.group(), "derived")
    endpoint = rx(r"\bhost\s+([\w.-]+:\d+)").search(body)
    if endpoint and not attrs["target"]:
        put("target", endpoint.group(1), endpoint.group())
    db = rx(r"\b([\w.-]+\.(?:db|sqlite|sqlite3))\b").search(body)
    if db:
        put("database", db.group(1), db.group())
        if not attrs["target"]:
            put("target", db.group(1), db.group())
    path = rx(r"(?:[/\\]|['\"])([\w.-]+\.(?:yaml|yml|json|md|py|txt))['\"]?(?:\s|:|$)").search(body)
    if path:
        put("path_basename", path.group(1), path.group())
    check = rx(r"check_fn\s+(\w+)\s+returned False").search(body)
    if check:
        put("target", check.group(1), check.group())

    exceptions = _unique(m.group(1) for m in EXCEPTION.finditer(body))
    if exceptions:
        explicit_error = rx(r"error_type=(\w+)").search(body)
        put("exception", explicit_error.group(1) if explicit_error else exceptions[-1], explicit_error.group() if explicit_error else exceptions[-1])
    attrs["exceptions"] = exceptions
    status_matches = list(HTTP.finditer(body))
    statuses = _unique(int(_first_group(m)) for m in status_matches)
    # Warning predicts a future error; it is not an observed model failure.
    predicted = "Copilot token exchange degraded" in body
    if len(statuses) == 1 and not predicted:
        put("http_status", statuses[0], status_matches[0].group())
    elif len(statuses) > 1:
        conflicts.append({"field": "http_status", "candidates": statuses})
    codes = _unique(_first_group(m) for m in CODE.finditer(body))
    if codes:
        # 'type' can name the exception; preserve all codes, prefer actual code.
        attrs["provider_codes"] = codes
        meaningful = [c for c in codes if c not in ("error", "message")]
        if meaningful and not predicted:
            put("provider_code", meaningful[0], meaningful[0])
    else:
        for code in ("RESOURCE_EXHAUSTED", "usage_limit_reached", "model_not_available_for_integrator", "invalid_grant", "context_length_exceeded"):
            if code in body and not predicted:
                put("provider_code", code, code)
                break
    for field, pattern in (("rpc_code", RPC), ("errno", ERRNO), ("exit_code", EXIT)):
        m = pattern.search(body)
        if m:
            put(field, int(_first_group(m)), m.group())
    signal = rx(r"\b(SIG(?:KILL|TERM|INT|ABRT|SEGV|PIPE))\b|code=killed,\s*status=(\d+/\w+)").search(body)
    if signal:
        put("signal", _first_group(signal), signal.group())
    attempt = ATTEMPT.search(body)
    if attempt:
        gs = attempt.groups()
        put("attempt", int(gs[0] or gs[2]), attempt.group())
        if gs[1] or gs[3]:
            put("max_attempts", int(gs[1] or gs[3]), attempt.group())

    system = profile.startswith("system:") or bool(_CLEAR_SYSTEM.search(text))
    scopes = set()
    if attrs["tool"] or mcp or comp.startswith("tools.") or comp == "agent.tool_executor":
        scopes.add("tool")
    if attrs["platform"] or comp.startswith(("gateway.", "discord", "telegram", "tui_gateway.")) or rx(r"\bdiscord\b|\btelegram\b|\bapi_server failed to connect").search(body):
        scopes.add("platform")
    if not system and (attrs["provider"] or comp.startswith(("agent.conversation_loop", "agent.chat_completion", "agent.auxiliary", "agent.credential", "agent.title_generator", "agent.moa_loop")) or rx(r"\bAPI call|\bStreaming failed|\bCodex stream|\bGeminiAPIError").search(body)):
        scopes.add("llm")
    # A successful tool result can contain quoted error text. Do not turn that
    # text into the outer operation's failure, but keep it in raw evidence.
    observation = bool(TUNING.search(body) or rx(r"^API call #\d+:|^Turn ended: reason=(?:completed|success|stop)\b|^shell hook registered:|\bTool\s+\S+\s+completed|^Web search via local-web-router:|^\[MEMORY\] rss=|Registered /\w+ command").search(body))
    continuation = is_continuation(text)
    causes, events = [], []
    if not observation and not continuation:
        for name, pattern, scope in CAUSE_RULES:
            if scope and scope not in scopes:
                continue
            if predicted and name in ("model_not_available_for_integrator", "model_unsupported"):
                continue
            m = pattern.search(body)
            if m:
                causes.append(name)
                hit("cause." + name, m)
        if "tool" in scopes and attrs["exit_code"] in (124, 127, 130):
            name = {124: "command_timeout", 127: "command_not_found", 130: "command_interrupted"}[attrs["exit_code"]]
            causes.append(name)
            hit("cause." + name + ".exit_status", evidence["exit_code"])
        if not causes and attrs["http_status"] in (429, 502, 503, 504):
            # Status-derived interpretation remains weaker than message evidence.
            name = {429: "rate_limited", 502: "bad_gateway", 503: "service_unavailable", 504: "gateway_timeout"}[attrs["http_status"]]
            causes.append(name)
            hit("cause.http_status", evidence["http_status"])
    for name, pattern, scope, operation, required_causes in EVENT_RULES:
        if continuation or (scope and scope not in scopes):
            continue
        if observation and name not in ("api.telemetry", "runtime.hook_registered", "tool.completed", "search.query_started", "runtime.memory_telemetry", "platform.command_registered"):
            continue
        if required_causes and not required_causes.intersection(causes):
            continue
        if predicted and name == "api.model_resolution_failed":
            continue
        m = pattern.search(body)
        if m:
            events.append(name)
            hit("event." + name, m)
            if attrs["operation"] is None:
                put("operation", operation, m.group(), "derived")
    tuning = TUNING.search(body)
    if tuning:
        events, causes = ["api.watchdog_tuned"], ["watchdog_tuning"]
        put("operation", "watchdog_tuning", tuning.group(), "derived")
        hit("event.api.watchdog_tuned", tuning)
    if "platform.operation_rate_limited" in events:
        causes.insert(0, "typing_rate_limited")
    if predicted:
        causes.insert(0, "token_exchange_degraded")
    if continuation:
        events = ["runtime.traceback_continuation"]
        hit("evidence.traceback", body[:100])
    if observation:
        # Error codes/exception names quoted in successful telemetry or a search
        # query describe its content, not an observed failure of this operation.
        for field in ("http_status", "provider_code", "rpc_code", "exception", "errno"):
            attrs[field] = None
            evidence.pop(field, None)
            confidence.pop(field, None)
        attrs["exceptions"] = []
        attrs.pop("provider_codes", None)
    if not events and exceptions and not system:
        events = ["runtime.exception"]
        hit("event.runtime.exception", evidence["exception"])
    if attrs["tool"] and attrs["operation"] in (None, "execute"):
        put("operation", {"terminal": "command_exec", "delegate_task": "subagent_delegate"}.get(attrs["tool"], attrs["tool"]), evidence.get("tool"), "derived")
    if rx(r"Streaming failed before delivery").search(body):
        put("stream_stage", "before_delivery", "Streaming failed before delivery")
    elif rx(r"Streaming failed after").search(body):
        put("stream_stage", "after_delivery", "Streaming failed after")

    if system:
        system_attrs = analyze_system(text, raw)
        system_matches = system_attrs.pop("matched_rules", [])
        matches.extend(system_matches)
        events = _unique([system_attrs.get("event")] + system_attrs.pop("events", []) + events)
        causes = _unique([system_attrs.get("cause")] + system_attrs.pop("causes", []) + causes)
        for key, value in system_attrs.items():
            if value is not None:
                put(key, value, "system_rules", "derived")
        continuation = system_attrs.get("kind") == "continuation" or continuation
    causes, events = _unique(causes), _unique(events)
    put("cause", causes[0] if causes else None, next((m["matched"] for m in matches if m["id"].startswith("cause.")), None), "derived")
    put("event", events[0] if events else None, next((m["matched"] for m in matches if m["id"].startswith("event.")), None), "derived")
    event = attrs["event"] or ""
    domain = {"api": "llm", "tool": "tool", "platform": "platform", "git": "vcs", "database": "storage", "security": "security", "search": "search", "service": "system", "system": "system", "process": "system", "network": "network", "runtime": "runtime"}.get(event.split(".")[0])
    if domain is None and system:
        domain = "system"
    put("domain", domain, "event_family" if event else "profile", "derived")
    kind = "continuation" if continuation else ("observation" if observation or event in ("platform.discovery", "runtime.gateway_exit") else attrs.get("kind", "incident"))
    if kind is None:
        kind = "incident"
    classification = dict(attrs)
    classification.update(
        rule_version=rule_version,
        events=events,
        causes=causes,
        matched_rules=matches,
        field_confidence=confidence,
        field_evidence=evidence,
        conflicts=conflicts,
        kind=kind,
        confidence="derived" if events or causes else None,
        possible_truncation=raw.get("text_truncated", len(text) == 400),
        observation=observation,
    )
    return classification
