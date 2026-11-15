import React from "react";
import { X, Command } from "lucide-react";

interface ShortcutsModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export const ShortcutsModal: React.FC<ShortcutsModalProps> = ({ isOpen, onClose }) => {
  if (!isOpen) return null;

  const SHORTCUTS = [
    { key: "?", description: "Toggle this keyboard shortcuts dialog" },
    { key: "Esc", description: "Close open drawer or modal" },
    { key: "r", description: "Instantly refresh telemetry metrics" },
    { key: "c", description: "Toggle Chaos 429 fault injection" },
    { key: "p", description: "Pause / Resume live 2s metrics polling" },
    { key: "1 - 5", description: "Select interactive test preset (1 to 5)" },
    { key: "⌘ + Enter", description: "Execute test prompt in simulator" },
  ];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/70 backdrop-blur-sm">
      <div className="w-full max-w-md bg-[#111113] border border-[#27272A] rounded-lg shadow-2xl overflow-hidden font-mono text-xs">
        <div className="p-4 border-b border-[#27272A] flex items-center justify-between bg-[#0E0E10]">
          <div className="flex items-center space-x-2">
            <Command className="w-4 h-4 text-emerald-400" />
            <span className="font-semibold text-zinc-200 text-sm">Keyboard Shortcuts</span>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded hover:bg-zinc-800 text-zinc-400 hover:text-white transition"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-4 space-y-2">
          {SHORTCUTS.map((s) => (
            <div
              key={s.key}
              className="flex items-center justify-between py-2 border-b border-[#18181B] last:border-0"
            >
              <span className="text-zinc-400">{s.description}</span>
              <kbd className="px-2 py-1 rounded bg-[#18181B] border border-[#27272A] text-zinc-200 text-[11px] font-semibold">
                {s.key}
              </kbd>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};
