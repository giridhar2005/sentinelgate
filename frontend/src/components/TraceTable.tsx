import React, { useState, useMemo } from "react";
import { RequestMetric } from "../types";
import { Search, ChevronRight, SlidersHorizontal } from "lucide-react";

interface TraceTableProps {
  traces: RequestMetric[];
  onSelectTrace: (trace: RequestMetric) => void;
  selectedTraceId?: string;
}

type FilterStatus = "ALL" | "CACHE_HIT" | "UPSTREAM" | "FAILOVER" | "REDACTED" | "BLOCKED";

export const TraceTable: React.FC<TraceTableProps> = ({
  traces,
  onSelectTrace,
  selectedTraceId,
}) => {
  const [filter, setFilter] = useState<FilterStatus>("ALL");
  const [search, setSearch] = useState<string>("");

  const filteredTraces = useMemo(() => {
    return traces.filter((t) => {
      // 1. Status filter
      if (filter !== "ALL") {
        const s = (t.status || "").toUpperCase();
        if (filter === "CACHE_HIT" && !s.includes("CACHE")) return false;
        if (filter === "UPSTREAM" && s !== "UPSTREAM") return false;
        if (filter === "FAILOVER" && !s.includes("FAILOVER")) return false;
        if (filter === "REDACTED" && !s.includes("REDACT")) return false;
        if (filter === "BLOCKED" && !s.includes("BLOCK")) return false;
      }
      // 2. Search query filter
      if (search.trim()) {
        const q = search.toLowerCase();
        const p = (t.prompt_snippet || "").toLowerCase();
        const m = (t.model || "").toLowerCase();
        const id = (t.request_id || "").toLowerCase();
        return p.includes(q) || m.includes(q) || id.includes(q);
      }
      return true;
    });
  }, [traces, filter, search]);

  const getStatusBadge = (status: string, cacheHit: number | boolean) => {
    const s = (status || "").toUpperCase();
    if (s.includes("CACHE") || cacheHit) {
      return <span className="badge-cache-hit px-2 py-0.5 rounded text-[10px] font-semibold">CACHE HIT</span>;
    }
    if (s.includes("FAILOVER")) {
      return <span className="badge-failover px-2 py-0.5 rounded text-[10px] font-semibold">FAILOVER</span>;
    }
    if (s.includes("REDACT")) {
      return <span className="badge-redacted px-2 py-0.5 rounded text-[10px] font-semibold">REDACTED</span>;
    }
    if (s.includes("BLOCK")) {
      return <span className="badge-blocked px-2 py-0.5 rounded text-[10px] font-semibold">BLOCKED</span>;
    }
    return <span className="badge-upstream px-2 py-0.5 rounded text-[10px] font-semibold">UPSTREAM</span>;
  };

  const formatTimestamp = (ts: string) => {
    if (!ts) return "--";
    try {
      const d = new Date(ts);
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    } catch {
      return ts.slice(11, 19) || ts;
    }
  };

  return (
    <div className="bg-[#111113] border border-[#27272A] rounded overflow-hidden">
      {/* Table Toolbar */}
      <div className="p-3 border-b border-[#27272A] flex flex-wrap items-center justify-between gap-3 bg-[#0E0E10]">
        {/* Status Filter Tabs */}
        <div className="flex items-center space-x-1 overflow-x-auto text-xs font-mono">
          {(["ALL", "CACHE_HIT", "UPSTREAM", "FAILOVER", "REDACTED", "BLOCKED"] as FilterStatus[]).map((tab) => (
            <button
              key={tab}
              onClick={() => setFilter(tab)}
              className={`px-2.5 py-1 rounded text-[11px] transition ${
                filter === tab
                  ? "bg-zinc-800 text-white font-medium border border-zinc-700"
                  : "text-zinc-400 hover:text-zinc-200"
              }`}
            >
              {tab.replace("_", " ")}
            </button>
          ))}
        </div>

        {/* Search Input */}
        <div className="relative w-full sm:w-64">
          <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-500" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search prompt, model, ID..."
            className="w-full bg-[#09090B] border border-[#27272A] rounded pl-8 pr-3 py-1 text-xs font-mono text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-500 transition"
          />
        </div>
      </div>

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-left border-collapse">
          <thead>
            <tr className="border-b border-[#27272A] text-[11px] font-mono text-zinc-500 uppercase tracking-wider bg-[#09090B]">
              <th className="py-2.5 px-3">Status</th>
              <th className="py-2.5 px-3">Route / Model</th>
              <th className="py-2.5 px-3">Prompt Snippet</th>
              <th className="py-2.5 px-3 text-right">Latency</th>
              <th className="py-2.5 px-3 text-right">Cost</th>
              <th className="py-2.5 px-3 text-right">Saved</th>
              <th className="py-2.5 px-3 text-right">Time</th>
              <th className="py-2.5 px-2 text-center w-8"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[#1F1F23] text-xs font-mono">
            {filteredTraces.length === 0 ? (
              <tr>
                <td colSpan={8} className="py-8 text-center text-zinc-500">
                  No traces match the current filter. Fire a request above to capture live telemetry.
                </td>
              </tr>
            ) : (
              filteredTraces.map((trace) => {
                const isSelected = trace.request_id === selectedTraceId;
                const isCached = Boolean(trace.cache_hit);
                const cost = trace.cost_usd ?? 0.0;
                const saved = trace.cost_saved_usd ?? 0.0;
                const latency = trace.latency_ms ?? 0.0;

                return (
                  <tr
                    key={trace.request_id}
                    onClick={() => onSelectTrace(trace)}
                    className={`cursor-pointer transition hover:bg-[#18181B] ${
                      isSelected ? "bg-[#18181B] border-l-2 border-emerald-400" : ""
                    }`}
                  >
                    <td className="py-2.5 px-3 whitespace-nowrap">
                      {getStatusBadge(trace.status, trace.cache_hit)}
                    </td>
                    <td className="py-2.5 px-3 whitespace-nowrap text-zinc-300 font-medium">
                      {trace.model}
                    </td>
                    <td className="py-2.5 px-3 max-w-xs md:max-w-md truncate text-zinc-400">
                      {trace.prompt_snippet || "(Empty prompt)"}
                    </td>
                    <td className="py-2.5 px-3 text-right whitespace-nowrap tabular-nums font-semibold">
                      <span className={latency < 5 ? "text-emerald-400" : latency < 500 ? "text-zinc-200" : "text-amber-400"}>
                        {latency.toFixed(1)}ms
                      </span>
                    </td>
                    <td className="py-2.5 px-3 text-right whitespace-nowrap text-zinc-400 tabular-nums">
                      ${cost.toFixed(5)}
                    </td>
                    <td className="py-2.5 px-3 text-right whitespace-nowrap tabular-nums">
                      {saved > 0 ? (
                        <span className="text-emerald-400 font-medium">+${saved.toFixed(5)}</span>
                      ) : (
                        <span className="text-zinc-600">$0.00000</span>
                      )}
                    </td>
                    <td className="py-2.5 px-3 text-right whitespace-nowrap text-zinc-500">
                      {formatTimestamp(trace.timestamp)}
                    </td>
                    <td className="py-2.5 px-2 text-center text-zinc-500">
                      <ChevronRight className="w-4 h-4" />
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
};
