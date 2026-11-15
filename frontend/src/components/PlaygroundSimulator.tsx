import React, { useState } from "react";
import { Play, Sparkles, Send, Loader2, CheckCircle2 } from "lucide-react";

interface PlaygroundSimulatorProps {
  onSuccess: () => void;
  onError: (msg: string) => void;
}

interface Preset {
  id: string;
  label: string;
  model: string;
  prompt: string;
  description: string;
}

const PRESETS: Preset[] = [
  {
    id: "p1",
    label: "1. Baseline Prompt",
    model: "gpt-4o",
    prompt: "Explain Raft consensus in three crisp bullet points.",
    description: "Standard upstream invocation",
  },
  {
    id: "p2",
    label: "2. Semantic Paraphrase",
    model: "gpt-4o",
    prompt: "Can you summarize how the Raft consensus algorithm works in 3 short points?",
    description: "Triggers FastEmbed vector cache hit (~0.5ms)",
  },
  {
    id: "p3",
    label: "3. Secret Shield (Redaction)",
    model: "gpt-4o-mini",
    prompt: "Audit this database connection URI for performance: postgresql://admin:prod_secret_vault_9921@db.internal:5432/orders",
    description: "Reversibly masks password with __SG_SECRET_1__",
  },
  {
    id: "p4",
    label: "4. Auto Router (Routine)",
    model: "auto",
    prompt: "Translate 'Hello world, infrastructure is resilient' to Spanish.",
    description: "Scores low complexity -> routes to fast/cheapest tier",
  },
  {
    id: "p5",
    label: "5. Auto Router (Complex)",
    model: "auto",
    prompt: "Design an asynchronous fault-tolerant distributed transaction coordinator in Go with two-phase commit and idempotency keys.",
    description: "Scores high complexity -> routes to frontier model",
  },
];

export const PlaygroundSimulator: React.FC<PlaygroundSimulatorProps> = ({
  onSuccess,
  onError,
}) => {
  const [model, setModel] = useState<string>("auto");
  const [prompt, setPrompt] = useState<string>(PRESETS[0].prompt);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [lastResult, setLastResult] = useState<{
    status: string;
    model: string;
    latencyMs: number;
    response: string;
    cached: boolean;
  } | null>(null);

  const applyPreset = (preset: Preset) => {
    setModel(preset.model);
    setPrompt(preset.prompt);
  };

  const executePrompt = async () => {
    if (!prompt.trim() || isLoading) return;
    setIsLoading(true);
    setLastResult(null);

    const t0 = performance.now();
    try {
      const res = await fetch("/v1/chat/completions", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer sg-local-test",
        },
        body: JSON.stringify({
          model: model,
          messages: [{ role: "user", content: prompt.trim() }],
          temperature: 0.7,
        }),
      });

      const latencyMs = performance.now() - t0;
      if (!res.ok) {
        const errText = await res.text();
        throw new Error(`HTTP ${res.status}: ${errText}`);
      }

      const data = await res.json();
      const content = data.choices?.[0]?.message?.content || "(No response content)";
      const isCached = Boolean(data.sentinelgate?.cache_hit);
      const usedModel = data.model || model;
      const status = isCached ? "CACHE_HIT" : "UPSTREAM";

      setLastResult({
        status,
        model: usedModel,
        latencyMs: Math.round(latencyMs),
        response: content,
        cached: isCached,
      });

      onSuccess();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Execution failed";
      onError(msg);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="bg-[#111113] border border-[#27272A] rounded p-4 space-y-3">
      {/* Header & Preset Buttons */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#27272A] pb-3">
        <div className="flex items-center space-x-2">
          <Sparkles className="w-4 h-4 text-emerald-400" />
          <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-300">
            Interactive Test Console
          </h2>
        </div>

        {/* 1-Click Scenario Presets */}
        <div className="flex flex-wrap items-center gap-1.5">
          {PRESETS.map((p) => (
            <button
              key={p.id}
              onClick={() => applyPreset(p)}
              title={p.description}
              className="text-[11px] font-mono px-2 py-1 rounded bg-[#18181B] border border-[#27272A] text-zinc-300 hover:text-white hover:border-zinc-600 transition"
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>

      {/* Editor & Controls */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
        <div className="md:col-span-3 space-y-2">
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={3}
            placeholder="Enter a prompt to route through SentinelGate..."
            className="w-full bg-[#09090B] border border-[#27272A] rounded p-2.5 text-xs font-mono text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-500 transition resize-none"
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                executePrompt();
              }
            }}
          />
        </div>

        <div className="flex flex-col justify-between space-y-2">
          <div className="space-y-1">
            <label className="text-[11px] font-mono text-zinc-400">Target Model</label>
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="w-full bg-[#09090B] border border-[#27272A] rounded px-2.5 py-1.5 text-xs font-mono text-zinc-200 focus:outline-none focus:border-zinc-500 transition"
            >
              <option value="auto">auto (Smart Cost Router)</option>
              <option value="gpt-4o">gpt-4o</option>
              <option value="gpt-4o-mini">gpt-4o-mini</option>
              <option value="claude-3-5-sonnet-20241022">claude-3-5-sonnet</option>
              <option value="groq/llama-3.1-8b-instant">groq/llama-3.1-8b</option>
              <option value="ollama/llama3.2">ollama/llama3.2</option>
            </select>
          </div>

          <button
            onClick={executePrompt}
            disabled={isLoading || !prompt.trim()}
            className="w-full flex items-center justify-center space-x-2 py-2 px-3 rounded bg-zinc-100 hover:bg-white text-zinc-950 font-medium text-xs font-mono transition disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isLoading ? (
              <>
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                <span>Routing...</span>
              </>
            ) : (
              <>
                <Send className="w-3.5 h-3.5" />
                <span>Execute (⌘↵)</span>
              </>
            )}
          </button>
        </div>
      </div>

      {/* Response Preview Card */}
      {lastResult && (
        <div className="mt-3 p-3 bg-[#09090B] border border-[#27272A] rounded text-xs space-y-2">
          <div className="flex items-center justify-between text-[11px] font-mono border-b border-[#18181B] pb-1.5">
            <div className="flex items-center space-x-2">
              <span
                className={`px-1.5 py-0.5 rounded text-[10px] font-semibold tracking-wider ${
                  lastResult.cached ? "badge-cache-hit" : "badge-upstream"
                }`}
              >
                {lastResult.status}
              </span>
              <span className="text-zinc-400">Model: {lastResult.model}</span>
            </div>
            <div className="flex items-center space-x-3 text-zinc-400">
              <span className="tabular-nums">Latency: {lastResult.latencyMs} ms</span>
              {lastResult.cached && (
                <span className="text-emerald-400 flex items-center space-x-1">
                  <CheckCircle2 className="w-3 h-3" />
                  <span>Cost: $0.000000</span>
                </span>
              )}
            </div>
          </div>
          <div className="font-mono text-zinc-300 whitespace-pre-wrap leading-relaxed max-h-40 overflow-y-auto">
            {lastResult.response}
          </div>
        </div>
      )}
    </div>
  );
};
