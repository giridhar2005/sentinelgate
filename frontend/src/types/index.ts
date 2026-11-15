export type RequestStatus =
  | "CACHE_HIT"
  | "UPSTREAM"
  | "FAILOVER"
  | "SECRET_REDACTED"
  | "BLOCKED"
  | "REDACTED";

export interface TimingBreakdown {
  guardrails_ms?: number;
  cache_lookup_ms?: number;
  provider_ms?: number;
  circuit_ms?: number;
  total_ms?: number;
  [key: string]: number | undefined;
}

export interface RequestMetric {
  request_id: string;
  model: string;
  provider: string;
  latency_ms: number;
  cache_hit: number | boolean;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  cost_saved_usd: number;
  timestamp: string;
  ttft_ms?: number;
  status: RequestStatus | string;
  prompt_snippet: string;
  similarity_score?: number;
  timing_breakdown?: string | TimingBreakdown;
  response_snippet: string;
}

export interface ShadowEval {
  request_id: string;
  original_model: string;
  shadow_model: string;
  judge_score: number;
  judge_reasoning: string;
  is_win: number | boolean;
  timestamp: string;
  prompt: string;
  original_response: string;
  shadow_response: string;
}

export interface ProviderCircuitStatus {
  state: "CLOSED" | "HALF_OPEN" | "OPEN";
  failure_count: number;
  cooldown_remaining_sec: number;
}

export interface ProviderStatus {
  status: "healthy" | "unhealthy" | "unknown";
  base_url?: string;
  models?: string[];
  keys_count?: number;
  circuit_breaker?: ProviderCircuitStatus;
}

export interface BudgetInfo {
  spent_today_usd: number;
  limit_usd: number;
  percent_used: number;
  is_allowed: boolean;
  request_count_today: number;
}

export interface ChaosConfig {
  enabled: boolean;
  target_provider: string;
  status_code: number;
}

export interface MetricsSummary {
  total_requests: number;
  cache_hits: number;
  cache_hit_rate: number;
  total_cost_usd: number;
  total_saved_usd: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  p50_latency_ms: number;
  p95_latency_ms: number;
  avg_ttft_ms: number;
  blocked_guardrail_events: number;
  secrets_scrubbed: number;
  budget?: BudgetInfo;
  providers?: Record<string, ProviderStatus>;
  chaos?: ChaosConfig;
  hedging?: {
    triggered: number;
    primary_won: number;
    secondary_won: number;
  };
  shadow_evals?: {
    total_evals: number;
    total_wins: number;
    win_rate: number;
    avg_score: number;
  };
  recent_requests: RequestMetric[];
  recent_shadow_evals?: ShadowEval[];
}

export interface VirtualKey {
  key_val: string;
  name: string;
  rate_limit_rpm: number;
  rate_limit_tpm?: number;
  monthly_budget_usd: number;
  spent_this_month_usd: number;
  created_at: string;
  is_active: number | boolean;
}

export interface ReplayPayload {
  prompt: string;
  original_model: string;
  candidate_model: string;
  original_latency_ms?: number;
  original_cost_usd?: number;
  original_response?: string;
}

export interface ReplayResult {
  prompt: string;
  original_model: string;
  original_provider: string;
  original_response: string;
  original_latency_ms: number;
  original_cost_per_req: number;
  candidate_model: string;
  candidate_provider: string;
  candidate_response: string;
  candidate_latency_ms: number;
  candidate_cost_per_req: number;
  latency_diff_ms: number;
  cost_saved_per_req: number;
  savings_percent: number;
  projected_monthly_savings_100k: number;
  projected_orig_monthly_usd: number;
  projected_cand_monthly_usd: number;
  tokens: {
    prompt: number;
    completion: number;
    total: number;
  };
}

export interface ToastMessage {
  id: string;
  title: string;
  message?: string;
  type?: "success" | "error" | "info";
}
