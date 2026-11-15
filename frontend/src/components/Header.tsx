import React from "react";
import {
  ShieldAlert,
  Zap,
  Terminal,
  Key,
  Download,
  HelpCircle,
  Pause,
  Play,
  RotateCw,
} from "lucide-react";
import { ChaosConfig } from "../types";

interface HeaderProps {
  isLive: boolean;
  onToggleLive: () => void;
  onRefresh: () => void;
  chaos?: ChaosConfig;
  onToggleChaos: () => void;
  onOpenConnect: () => void;
  onOpenKeys: () => void;
  onOpenShortcuts: () => void;
}

export const Header: React.FC<HeaderProps> = ({
  isLive,
  onToggleLive,
  onRefresh,
  chaos,
  onToggleChaos,
  onOpenConnect,
  onOpenKeys,
  onOpenShortcuts,
}) => {
  const chaosActive = Boolean(chaos?.enabled);

  return (
    <header className="border-b border-[#27272A] bg-[#09090B]/95 backdrop-blur sticky top-0 z-30 px-4 lg:px-6 py-3">
      <div className="max-w-7xl mx-auto flex flex-wrap items-center justify-between gap-3">
        {/* Brand & Status */}
        <div className="flex items-center space-x-3">
          <div className="w-8 h-8 rounded bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-center text-emerald-400">
            <Zap className="w-4 h-4" />
          </div>
          <div className="flex items-baseline space-x-2">
            <span className="font-semibold tracking-tight text-white text-base">SentinelGate</span>
            <span className="text-[11px] font-mono px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-400 border border-zinc-700">
              v0.3.0
            </span>
          </div>

          <div className="hidden sm:flex items-center space-x-2 pl-3 border-l border-zinc-800 text-xs font-mono">
            <span
              className={`w-2 h-2 rounded-full ${
                isLive ? "bg-emerald-400 animate-pulse" : "bg-amber-400"
              }`}
            />
            <span className="text-zinc-400">{isLive ? "Live (2s)" : "Paused"}</span>
          </div>
        </div>

        {/* Global Controls & Actions */}
        <div className="flex items-center flex-wrap gap-2 text-xs">
          {/* Chaos Fault Injection Switch */}
          <button
            onClick={onToggleChaos}
            title="Simulate upstream 429 rate limit to observe circuit-breaker failover"
            className={`flex items-center space-x-1.5 px-2.5 py-1.5 rounded font-mono border transition ${
              chaosActive
                ? "bg-amber-500/15 border-amber-500/50 text-amber-300 font-medium"
                : "bg-[#111113] border-[#27272A] text-zinc-400 hover:text-zinc-200 hover:border-zinc-700"
            }`}
          >
            <ShieldAlert className={`w-3.5 h-3.5 ${chaosActive ? "text-amber-400 animate-spin" : "text-zinc-500"}`} />
            <span>Chaos Fault {chaosActive ? "(429 Active)" : "(Off)"}</span>
          </button>

          {/* Connect Tool Modal */}
          <button
            onClick={onOpenConnect}
            className="flex items-center space-x-1.5 px-2.5 py-1.5 rounded font-mono bg-[#111113] border border-[#27272A] text-zinc-300 hover:text-white hover:border-zinc-600 transition"
          >
            <Terminal className="w-3.5 h-3.5 text-zinc-400" />
            <span>Connect Tool</span>
          </button>

          {/* Virtual Keys Modal */}
          <button
            onClick={onOpenKeys}
            className="flex items-center space-x-1.5 px-2.5 py-1.5 rounded font-mono bg-[#111113] border border-[#27272A] text-zinc-300 hover:text-white hover:border-zinc-600 transition"
          >
            <Key className="w-3.5 h-3.5 text-zinc-400" />
            <span>Virtual Keys</span>
          </button>

          {/* Export Evals Dropdown */}
          <a
            href="/api/export/traces.jsonl"
            download="sentinelgate_eval_traces.jsonl"
            className="flex items-center space-x-1.5 px-2.5 py-1.5 rounded font-mono bg-[#111113] border border-[#27272A] text-zinc-300 hover:text-white hover:border-zinc-600 transition"
            title="Download traces in OpenAI fine-tuning JSONL format"
          >
            <Download className="w-3.5 h-3.5 text-zinc-400" />
            <span className="hidden md:inline">Export</span>
            <span>.JSONL</span>
          </a>

          {/* Live / Pause Toggle */}
          <button
            onClick={onToggleLive}
            title={isLive ? "Pause polling" : "Resume live 2s polling"}
            className="p-1.5 rounded bg-[#111113] border border-[#27272A] text-zinc-400 hover:text-white hover:border-zinc-600 transition"
          >
            {isLive ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5 text-emerald-400" />}
          </button>

          {/* Manual Refresh */}
          <button
            onClick={onRefresh}
            title="Refresh metrics immediately"
            className="p-1.5 rounded bg-[#111113] border border-[#27272A] text-zinc-400 hover:text-white hover:border-zinc-600 transition"
          >
            <RotateCw className="w-3.5 h-3.5" />
          </button>

          {/* Shortcuts Modal */}
          <button
            onClick={onOpenShortcuts}
            title="Keyboard shortcuts (?)"
            className="p-1.5 rounded bg-[#111113] border border-[#27272A] text-zinc-400 hover:text-white hover:border-zinc-600 transition"
          >
            <HelpCircle className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
    </header>
  );
};
