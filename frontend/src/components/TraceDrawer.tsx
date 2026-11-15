import React, { useState } from "react";
import {
  X,
  Copy,
  Check,
  Zap,
  TrendingDown,
  Layers,
  ArrowRight,
  Loader2,
  Clock,
  Sparkles,
} from "lucide-react";
import { RequestMetric, ReplayResult, TimingBreakdown } from "../types";

interface TraceDrawerProps {
  trace: RequestMetric | null;
  onClose: () => void;
  onRunReplay: (payload: {
    prompt: string;
    original_model: string;
    candidate_model: string;
    original_latency_ms?: number;
    original_cost_usd?: number;
    original_response?: string;
  }) => Promise<ReplayResult>;
  onCopySuccess: () => void;
}

export const TraceDrawer: React.FC<TraceDrawerProps> = ({
  trace,
  onClose,
  onRunReplay,
  onCopySuccess,
}) => {
  const [copiedSection, setCopiedSection] = useState<string | null>(null);
  const [candidateModel, setCandidateModel] = useState<string>("groq/llama-3.1-8b-instant");
  const [isReplaying, setIsReplaying] = useState<boolean>(false);
  const [replayResult, setReplayResult] = useState<ReplayResult | null>(null);
  const [replayError, setReplayError] = useState<string | null>(null);

  if (!trace) return null;

  const copyToClipboard = (text: string, section: string) => {
    navigator.clipboard.writeText(text);
    setCopiedSection(section);
    onCopySuccess();
    setTimeout(() => setCopiedSection(null), 2000);
  };

  // Parse timing breakdown
  let breakdown: TimingBreakdown = {};
  if (trace.timing_breakdown) {
    if (typeof trace.timing_breakdown === "string") {
      try {
        breakdown = JSON.parse(trace.timing_breakdown);
      } catch {
        breakdown = {};
      }
    } else {
      breakdown = trace.timing_breakdown;
    }
  }

  const guardrailsMs = breakdown.guardrails_ms ?? 0.05;
  const cacheLookupMs = breakdown.cache_lookup_ms ?? (Boolean(trace.cache_hit) ? trace.latency_ms : 0.45);
  const providerMs = breakdown.provider_ms ?? (Boolean(trace.cache_hit) ? 0.0 : Math.max(0.0, trace.latency_ms - guardrailsMs - cacheLookupMs));
  const totalMs = trace.latency_ms ?? (guardrailsMs + cacheLookupMs + providerMs);

  const handleReplay = async () => {
    setIsReplaying(true);
    setReplayError(null);
    try {
      const result = await onRunReplay({
        prompt: trace.prompt_snippet,
        original_model: trace.model,
        candidate_model: candidateModel,
        original_latency_ms: trace.latency_ms,
        original_cost_usd: trace.cost_usd,
        original_response: trace.response_snippet,
      });
      setReplayResult(result);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Replay failed";
      setReplayError(msg);
    } finally {
      setIsReplaying(false);
    }
  };

  return (
    <div className="fixed inset-y-0 right-0 z-50 w-full max-w-2xl bg-[#09090B] border-l border-[#27272A] shadow-2xl flex flex-col text-xs font-mono">
      {/* Header */}
      <div className="p-4 border-b border-[#27272A] flex items-center justify-between bg-[#111113]">
        <div className="space-y-1">
          <div className="flex items-center space-x-2">
            <span className="text-zinc-400 font-semibold uppercase tracking-wider text-[11px]">
              Trace Inspector
            </span>
            <span className="px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-300 border border-zinc-700 text-[10px]">
              {trace.request_id}
            </span>
          </div>
          <div className="text-zinc-500 text-[11px]">
            Model: <span className="text-zinc-300">{trace.model}</span> · Provider:{" "}
            <span className="text-zinc-300">{trace.provider}</span>
          </div>
        </div>

        <button
          onClick={onClose}
          className="p-1.5 rounded hover:bg-zinc-800 text-zinc-400 hover:text-white transition"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Content Body */}
      <div className="flex-1 overflow-y-auto p-4 space-y-5">
        {/* Timing Breakdown Waterfall */}
        <div className="bg-[#111113] border border-[#27272A] rounded p-3 space-y-3">
          <div className="flex items-center justify-between text-zinc-300 font-semibold border-b border-[#1F1F23] pb-2">
            <div className="flex items-center space-x-2">
              <Clock className="w-3.5 h-3.5 text-blue-400" />
              <span>Execution Latency Waterfall</span>
            </div>
            <span className="text-emerald-400 tabular-nums font-mono">{totalMs.toFixed(2)} ms total</span>
          </div>

          <div className="space-y-2 text-[11px]">
            {/* Guardrail scan */}
            <div className="space-y-1">
              <div className="flex justify-between text-zinc-400">
                <span>Guardrails & Secret Redaction Firewall</span>
                <span className="tabular-nums text-zinc-300">{guardrailsMs.toFixed(2)} ms</span>
              </div>
              <div className="w-full bg-[#18181B] h-1.5 rounded-full overflow-hidden">
                <div
                  className="bg-purple-500 h-full rounded-full"
                  style={{ width: `${Math.min(100, Math.max(5, (guardrailsMs / totalMs) * 100))}%` }}
                />
              </div>
            </div>

            {/* Semantic Cache Vector Lookup */}
            <div className="space-y-1">
              <div className="flex justify-between text-zinc-400">
                <span>FastEmbed Semantic Vector Index Lookup</span>
                <span className="tabular-nums text-emerald-400">{cacheLookupMs.toFixed(2)} ms</span>
              </div>
              <div className="w-full bg-[#18181B] h-1.5 rounded-full overflow-hidden">
                <div
                  className="bg-emerald-500 h-full rounded-full"
                  style={{ width: `${Math.min(100, Math.max(5, (cacheLookupMs / totalMs) * 100))}%` }}
                />
              </div>
            </div>

            {/* Upstream Execution (if not cached) */}
            {providerMs > 0 && (
              <div className="space-y-1">
                <div className="flex justify-between text-zinc-400">
                  <span>Upstream Provider API Call</span>
                  <span className="tabular-nums text-blue-400">{providerMs.toFixed(2)} ms</span>
                </div>
                <div className="w-full bg-[#18181B] h-1.5 rounded-full overflow-hidden">
                  <div
                    className="bg-blue-500 h-full rounded-full"
                    style={{ width: `${Math.min(100, Math.max(5, (providerMs / totalMs) * 100))}%` }}
                  />
                </div>
              </div>
            )}
          </div>

          {trace.similarity_score !== undefined && trace.similarity_score > 0 && (
            <div className="mt-2 pt-2 border-t border-[#1F1F23] flex items-center justify-between text-[11px] text-zinc-400">
              <span>FastEmbed Cosine Similarity</span>
              <span className="text-emerald-400 font-mono font-semibold">
                {trace.similarity_score.toFixed(4)} (Threshold ≥ 0.90)
              </span>
            </div>
          )}
        </div>

        {/* Prompt Snippet */}
        <div className="bg-[#111113] border border-[#27272A] rounded p-3 space-y-2">
          <div className="flex items-center justify-between text-zinc-400 pb-1 border-b border-[#1F1F23]">
            <span className="font-semibold text-zinc-300">User Prompt Snippet</span>
            <button
              onClick={() => copyToClipboard(trace.prompt_snippet, "prompt")}
              className="flex items-center space-x-1 text-[11px] text-zinc-400 hover:text-white transition"
            >
              {copiedSection === "prompt" ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
              <span>{copiedSection === "prompt" ? "Copied" : "Copy"}</span>
            </button>
          </div>
          <div className="p-2 bg-[#09090B] border border-[#18181B] rounded text-zinc-200 whitespace-pre-wrap leading-relaxed max-h-48 overflow-y-auto">
            {trace.prompt_snippet || "(No prompt recorded)"}
          </div>
        </div>

        {/* Response Snippet */}
        <div className="bg-[#111113] border border-[#27272A] rounded p-3 space-y-2">
          <div className="flex items-center justify-between text-zinc-400 pb-1 border-b border-[#1F1F23]">
            <span className="font-semibold text-zinc-300">Model Output Snippet</span>
            <button
              onClick={() => copyToClipboard(trace.response_snippet, "response")}
              className="flex items-center space-x-1 text-[11px] text-zinc-400 hover:text-white transition"
            >
              {copiedSection === "response" ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
              <span>{copiedSection === "response" ? "Copied" : "Copy"}</span>
            </button>
          </div>
          <div className="p-2 bg-[#09090B] border border-[#18181B] rounded text-zinc-200 whitespace-pre-wrap leading-relaxed max-h-48 overflow-y-auto">
            {trace.response_snippet || "(No response recorded)"}
          </div>
        </div>

        {/* Killer Feature: Shadow Model Replay & Unit Economics Estimator */}
        <div className="bg-[#111113] border border-emerald-500/30 rounded p-3 space-y-3">
          <div className="flex items-center justify-between border-b border-[#1F1F23] pb-2">
            <div className="flex items-center space-x-2 text-emerald-400 font-semibold">
              <Sparkles className="w-4 h-4" />
              <span>Shadow Model Replay & Unit Economics</span>
            </div>
            <span className="text-[10px] text-zinc-500">100k req/mo Projection</span>
          </div>

          <p className="text-zinc-400 text-[11px] leading-relaxed">
            Replay this exact prompt against a cheaper model to evaluate latency delta and projected monthly USD savings.
          </p>

          <div className="flex items-center space-x-2">
            <select
              value={candidateModel}
              onChange={(e) => setCandidateModel(e.target.value)}
              className="flex-1 bg-[#09090B] border border-[#27272A] rounded px-2.5 py-1.5 text-xs font-mono text-zinc-200 focus:outline-none focus:border-zinc-500"
            >
              <option value="groq/llama-3.1-8b-instant">groq/llama-3.1-8b-instant (Fastest)</option>
              <option value="gpt-4o-mini">gpt-4o-mini (Cost-effective)</option>
              <option value="ollama/llama3.2">ollama/llama3.2 (Local Zero-Cost)</option>
            </select>

            <button
              onClick={handleReplay}
              disabled={isReplaying}
              className="flex items-center space-x-1.5 px-3 py-1.5 rounded bg-emerald-500 hover:bg-emerald-400 text-zinc-950 font-semibold text-xs transition disabled:opacity-50"
            >
              {isReplaying ? (
                <>
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  <span>Replaying...</span>
                </>
              ) : (
                <>
                  <Zap className="w-3.5 h-3.5" />
                  <span>Replay Model</span>
                </>
              )}
            </button>
          </div>

          {replayError && (
            <div className="p-2 rounded bg-rose-500/10 border border-rose-500/30 text-rose-400 text-[11px]">
              {replayError}
            </div>
          )}

          {/* Replay Results */}
          {replayResult && (
            <div className="space-y-3 pt-2 border-t border-[#1F1F23]">
              {/* Unit Economics Highlight Banner */}
              <div className="grid grid-cols-3 gap-2 bg-[#09090B] p-2.5 rounded border border-[#27272A]">
                <div>
                  <div className="text-[10px] text-zinc-500">Monthly Savings</div>
                  <div className="text-emerald-400 text-sm font-semibold tabular-nums">
                    +${replayResult.projected_monthly_savings_100k.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-zinc-500">Cost Reduction</div>
                  <div className="text-zinc-200 text-sm font-semibold tabular-nums">
                    {replayResult.savings_percent.toFixed(1)}%
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-zinc-500">Latency Delta</div>
                  <div className="text-blue-400 text-sm font-semibold tabular-nums">
                    {replayResult.latency_diff_ms > 0 ? `+${replayResult.latency_diff_ms}ms` : `${replayResult.latency_diff_ms}ms`}
                  </div>
                </div>
              </div>

              {/* Side-by-side Candidate Response */}
              <div className="space-y-1">
                <div className="text-[11px] text-zinc-400 font-semibold">
                  Candidate Response ({replayResult.candidate_model}):
                </div>
                <div className="p-2 bg-[#09090B] border border-[#18181B] rounded text-zinc-300 whitespace-pre-wrap leading-relaxed max-h-36 overflow-y-auto">
                  {replayResult.candidate_response}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
