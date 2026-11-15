import React from "react";
import { MetricsSummary } from "../types";
import { Activity, Database, DollarSign, Clock, ShieldCheck, AlertTriangle } from "lucide-react";

interface TelemetryStripProps {
  metrics: MetricsSummary | null;
}

export const TelemetryStrip: React.FC<TelemetryStripProps> = ({ metrics }) => {
  const totalRequests = metrics?.total_requests ?? 0;
  const cacheHitRate = metrics?.cache_hit_rate ?? 0.0;
  const cacheHits = metrics?.cache_hits ?? 0;
  const totalSavedUsd = metrics?.total_saved_usd ?? 0.0;
  const spentToday = metrics?.budget?.spent_today_usd ?? 0.0;
  const budgetLimit = metrics?.budget?.limit_usd ?? 50.0;
  const p50 = metrics?.p50_latency_ms ?? 0.0;
  const p95 = metrics?.p95_latency_ms ?? 0.0;
  const secretsScrubbed = metrics?.secrets_scrubbed ?? 0;
  const blockedThreats = metrics?.blocked_guardrail_events ?? 0;

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
      {/* 1. Total Requests */}
      <div className="bg-[#111113] border border-[#27272A] rounded p-3 flex flex-col justify-between">
        <div className="flex items-center justify-between text-xs text-zinc-400 mb-1">
          <span>Total Requests</span>
          <Activity className="w-3.5 h-3.5 text-zinc-500" />
        </div>
        <div className="text-xl font-mono font-semibold text-white tabular-nums">
          {totalRequests.toLocaleString()}
        </div>
        <div className="text-[11px] font-mono text-zinc-500 mt-1 flex items-center justify-between">
          <span>In-Flight: 0</span>
          <span>{((metrics?.total_prompt_tokens ?? 0) + (metrics?.total_completion_tokens ?? 0)).toLocaleString()} tok</span>
        </div>
      </div>

      {/* 2. Semantic Cache Hit Rate */}
      <div className="bg-[#111113] border border-[#27272A] rounded p-3 flex flex-col justify-between">
        <div className="flex items-center justify-between text-xs text-zinc-400 mb-1">
          <span>Cache Hit Rate</span>
          <Database className="w-3.5 h-3.5 text-emerald-500/70" />
        </div>
        <div className="text-xl font-mono font-semibold text-emerald-400 tabular-nums">
          {cacheHitRate.toFixed(1)}%
        </div>
        <div className="text-[11px] font-mono text-zinc-500 mt-1 flex items-center justify-between">
          <span>{cacheHits.toLocaleString()} Hits</span>
          <span>Cosine ≥ 0.90</span>
        </div>
      </div>

      {/* 3. Cumulative USD Saved */}
      <div className="bg-[#111113] border border-[#27272A] rounded p-3 flex flex-col justify-between">
        <div className="flex items-center justify-between text-xs text-zinc-400 mb-1">
          <span>Cost Saved</span>
          <DollarSign className="w-3.5 h-3.5 text-emerald-500/70" />
        </div>
        <div className="text-xl font-mono font-semibold text-white tabular-nums">
          ${totalSavedUsd.toFixed(4)}
        </div>
        <div className="text-[11px] font-mono text-zinc-500 mt-1 flex items-center justify-between">
          <span>Today: ${spentToday.toFixed(3)}</span>
          <span>/ ${budgetLimit.toFixed(0)}</span>
        </div>
      </div>

      {/* 4. Median Overhead */}
      <div className="bg-[#111113] border border-[#27272A] rounded p-3 flex flex-col justify-between">
        <div className="flex items-center justify-between text-xs text-zinc-400 mb-1">
          <span>Overhead (p50)</span>
          <Clock className="w-3.5 h-3.5 text-blue-500/70" />
        </div>
        <div className="text-xl font-mono font-semibold text-zinc-200 tabular-nums">
          {p50.toFixed(1)} <span className="text-xs font-normal text-zinc-500">ms</span>
        </div>
        <div className="text-[11px] font-mono text-zinc-500 mt-1 flex items-center justify-between">
          <span>p95: {p95.toFixed(1)}ms</span>
          <span>TTFT: {metrics?.avg_ttft_ms ? metrics.avg_ttft_ms.toFixed(0) + "ms" : "N/A"}</span>
        </div>
      </div>

      {/* 5. Secrets Scrubbed */}
      <div className="bg-[#111113] border border-[#27272A] rounded p-3 flex flex-col justify-between">
        <div className="flex items-center justify-between text-xs text-zinc-400 mb-1">
          <span>Secrets Scrubbed</span>
          <ShieldCheck className="w-3.5 h-3.5 text-purple-500/70" />
        </div>
        <div className="text-xl font-mono font-semibold text-purple-300 tabular-nums">
          {secretsScrubbed}
        </div>
        <div className="text-[11px] font-mono text-zinc-500 mt-1">
          Reversible Tokenization
        </div>
      </div>

      {/* 6. Blocked Threats */}
      <div className="bg-[#111113] border border-[#27272A] rounded p-3 flex flex-col justify-between">
        <div className="flex items-center justify-between text-xs text-zinc-400 mb-1">
          <span>Threats Blocked</span>
          <AlertTriangle className="w-3.5 h-3.5 text-rose-500/70" />
        </div>
        <div className="text-xl font-mono font-semibold text-rose-400 tabular-nums">
          {blockedThreats}
        </div>
        <div className="text-[11px] font-mono text-zinc-500 mt-1">
          Jailbreak & Injection Shield
        </div>
      </div>
    </div>
  );
};
