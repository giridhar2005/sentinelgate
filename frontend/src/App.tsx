import React, { useState, useEffect, useCallback } from "react";
import { useMetrics } from "./hooks/useMetrics";
import { useToast } from "./hooks/useToast";
import { Header } from "./components/Header";
import { TelemetryStrip } from "./components/TelemetryStrip";
import { PlaygroundSimulator } from "./components/PlaygroundSimulator";
import { TraceTable } from "./components/TraceTable";
import { TraceDrawer } from "./components/TraceDrawer";
import { ConnectModal } from "./components/ConnectModal";
import { VirtualKeysModal } from "./components/VirtualKeysModal";
import { ShortcutsModal } from "./components/ShortcutsModal";
import { ToastContainer } from "./components/ToastContainer";
import { RequestMetric } from "./types";

export const App: React.FC = () => {
  const {
    metrics,
    loading,
    error,
    isLive,
    setIsLive,
    refresh,
    toggleChaos,
    runShadowReplay,
  } = useMetrics();

  const { toasts, addToast, removeToast } = useToast();

  const [selectedTrace, setSelectedTrace] = useState<RequestMetric | null>(null);
  const [isConnectOpen, setIsConnectOpen] = useState<boolean>(false);
  const [isKeysOpen, setIsKeysOpen] = useState<boolean>(false);
  const [isShortcutsOpen, setIsShortcutsOpen] = useState<boolean>(false);

  // Keyboard shortcuts handler
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Ignore if typing in input or textarea
      const target = e.target as HTMLElement;
      if (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable) {
        if (e.key === "Escape") {
          target.blur();
        }
        return;
      }

      if (e.key === "Escape") {
        setSelectedTrace(null);
        setIsConnectOpen(false);
        setIsKeysOpen(false);
        setIsShortcutsOpen(false);
      } else if (e.key === "?") {
        setIsShortcutsOpen((prev) => !prev);
      } else if (e.key === "r") {
        refresh();
        addToast("Refreshed", "Telemetry metrics synced from SQLite", "info");
      } else if (e.key === "c") {
        handleToggleChaos();
      } else if (e.key === "p") {
        setIsLive((prev) => !prev);
        addToast("Polling Toggled", !isLive ? "Live polling enabled (2s)" : "Polling paused", "info");
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [refresh, isLive, setIsLive, addToast]);

  const handleToggleChaos = async () => {
    try {
      const res = await toggleChaos();
      const enabled = res.enabled;
      addToast(
        enabled ? "Chaos Outage Active" : "Chaos Restored",
        enabled ? "Upstream provider returning 429 to trigger circuit failover" : "Upstream healthy",
        enabled ? "error" : "success"
      );
    } catch {
      addToast("Chaos Toggle Failed", "Could not reach gateway chaos endpoint", "error");
    }
  };

  const handleCopySuccess = () => {
    addToast("Copied to Clipboard", undefined, "success");
  };

  return (
    <div className="min-h-screen bg-[#09090B] text-zinc-100 flex flex-col font-sans selection:bg-zinc-800 selection:text-white">
      {/* Top Header */}
      <Header
        isLive={isLive}
        onToggleLive={() => setIsLive(!isLive)}
        onRefresh={() => {
          refresh();
          addToast("Telemetry Refreshed", undefined, "info");
        }}
        chaos={metrics?.chaos}
        onToggleChaos={handleToggleChaos}
        onOpenConnect={() => setIsConnectOpen(true)}
        onOpenKeys={() => setIsKeysOpen(true)}
        onOpenShortcuts={() => setIsShortcutsOpen(true)}
      />

      {/* Main Container */}
      <main className="flex-1 max-w-7xl w-full mx-auto px-4 lg:px-6 py-6 space-y-6">
        {/* Telemetry Strip (6 Cards) */}
        <TelemetryStrip metrics={metrics} />

        {/* Interactive Playground Simulator */}
        <PlaygroundSimulator
          onSuccess={() => {
            refresh();
            addToast("Request Processed", "Trace recorded in SQLite WAL", "success");
          }}
          onError={(msg) => addToast("Request Error", msg, "error")}
        />

        {/* Live Traces Table */}
        <section className="space-y-2">
          <div className="flex items-center justify-between text-xs font-mono text-zinc-400">
            <span className="font-semibold uppercase tracking-wider text-zinc-300">
              Live SQLite Traces & Telemetry
            </span>
            <span className="text-[11px] text-zinc-500">
              Showing last {metrics?.recent_requests?.length ?? 0} executions
            </span>
          </div>

          <TraceTable
            traces={metrics?.recent_requests ?? []}
            onSelectTrace={(trace) => setSelectedTrace(trace)}
            selectedTraceId={selectedTrace?.request_id}
          />
        </section>
      </main>

      {/* Footer */}
      <footer className="border-t border-[#27272A] py-4 px-6 text-center text-xs font-mono text-zinc-500">
        SentinelGate · Zero-Bloat Local AI Infrastructure · SQLite WAL + FastEmbed ONNX
      </footer>

      {/* Slide-Over Trace Inspector Drawer */}
      {selectedTrace && (
        <TraceDrawer
          trace={selectedTrace}
          onClose={() => setSelectedTrace(null)}
          onRunReplay={runShadowReplay}
          onCopySuccess={handleCopySuccess}
        />
      )}

      {/* Connect Any Tool Modal */}
      <ConnectModal
        isOpen={isConnectOpen}
        onClose={() => setIsConnectOpen(false)}
        onCopySuccess={handleCopySuccess}
      />

      {/* Virtual Keys Modal */}
      <VirtualKeysModal
        isOpen={isKeysOpen}
        onClose={() => setIsKeysOpen(false)}
        onCopySuccess={handleCopySuccess}
        onToast={addToast}
      />

      {/* Shortcuts Modal */}
      <ShortcutsModal
        isOpen={isShortcutsOpen}
        onClose={() => setIsShortcutsOpen(false)}
      />

      {/* Global Toasts */}
      <ToastContainer toasts={toasts} onDismiss={removeToast} />
    </div>
  );
};
export default App;
