import React, { useState, useEffect, useCallback } from "react";
import { X, Key, Plus, Trash2, Copy, Check, Loader2, AlertCircle } from "lucide-react";
import { VirtualKey } from "../types";

interface VirtualKeysModalProps {
  isOpen: boolean;
  onClose: () => void;
  onCopySuccess: () => void;
  onToast: (title: string, message?: string, type?: "success" | "error" | "info") => void;
}

export const VirtualKeysModal: React.FC<VirtualKeysModalProps> = ({
  isOpen,
  onClose,
  onCopySuccess,
  onToast,
}) => {
  const [keys, setKeys] = useState<VirtualKey[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  // New key form state
  const [name, setName] = useState<string>("");
  const [rpm, setRpm] = useState<number>(60);
  const [budget, setBudget] = useState<number>(50);
  const [isCreating, setIsCreating] = useState<boolean>(false);

  const fetchKeys = useCallback(async () => {
    try {
      setLoading(true);
      const res = await fetch("/v1/keys");
      if (res.ok) {
        const data = await res.json();
        setKeys(data.keys || []);
      }
    } catch (err) {
      console.error("Failed to fetch virtual keys:", err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isOpen) {
      fetchKeys();
    }
  }, [isOpen, fetchKeys]);

  if (!isOpen) return null;

  const handleCreateKey = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;

    setIsCreating(true);
    try {
      const res = await fetch("/v1/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name.trim(),
          rate_limit_rpm: Number(rpm),
          monthly_budget_usd: Number(budget),
        }),
      });

      if (!res.ok) {
        throw new Error("Failed to create key");
      }

      setName("");
      await fetchKeys();
      onToast("Virtual Key Created", "New scoped token stored in SQLite WAL", "success");
    } catch (err) {
      onToast("Error", "Could not issue virtual key", "error");
    } finally {
      setIsCreating(false);
    }
  };

  const handleRevoke = async (keyVal: string) => {
    if (!confirm(`Revoke key ${keyVal}? Clients using this key will immediately receive 401 Unauthorized.`)) return;
    try {
      const res = await fetch(`/v1/keys/${encodeURIComponent(keyVal)}`, { method: "DELETE" });
      if (!res.ok) throw new Error("Revocation failed");
      await fetchKeys();
      onToast("Key Revoked", `Virtual key ${keyVal} marked inactive`, "info");
    } catch (err) {
      onToast("Error", "Failed to revoke key", "error");
    }
  };

  const handleCopy = (keyVal: string) => {
    navigator.clipboard.writeText(keyVal);
    setCopiedKey(keyVal);
    onCopySuccess();
    setTimeout(() => setCopiedKey(null), 2000);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/70 backdrop-blur-sm">
      <div className="w-full max-w-3xl bg-[#111113] border border-[#27272A] rounded-lg shadow-2xl overflow-hidden font-mono text-xs">
        {/* Header */}
        <div className="p-4 border-b border-[#27272A] flex items-center justify-between bg-[#0E0E10]">
          <div className="flex items-center space-x-2">
            <Key className="w-4 h-4 text-emerald-400" />
            <span className="font-semibold text-zinc-200 text-sm">
              SQLite Virtual Keys Manager
            </span>
            <span className="text-zinc-500 text-[11px]">
              ({keys.length} active)
            </span>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded hover:bg-zinc-800 text-zinc-400 hover:text-white transition"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-4 space-y-5">
          {/* Create Key Form */}
          <form onSubmit={handleCreateKey} className="bg-[#09090B] border border-[#27272A] rounded p-3 space-y-3">
            <div className="font-semibold text-zinc-300 text-xs">Issue New Scoped Virtual Key</div>
            <div className="grid grid-cols-1 sm:grid-cols-4 gap-2">
              <input
                type="text"
                placeholder="Key label (e.g. dev-cluster-1)"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                className="bg-[#111113] border border-[#27272A] rounded px-2.5 py-1.5 text-xs text-zinc-200 focus:outline-none focus:border-zinc-500"
              />
              <input
                type="number"
                placeholder="RPM Limit"
                value={rpm}
                onChange={(e) => setRpm(Number(e.target.value))}
                min={1}
                max={10000}
                className="bg-[#111113] border border-[#27272A] rounded px-2.5 py-1.5 text-xs text-zinc-200 focus:outline-none focus:border-zinc-500"
              />
              <input
                type="number"
                placeholder="Monthly Budget ($)"
                value={budget}
                onChange={(e) => setBudget(Number(e.target.value))}
                min={1}
                max={100000}
                className="bg-[#111113] border border-[#27272A] rounded px-2.5 py-1.5 text-xs text-zinc-200 focus:outline-none focus:border-zinc-500"
              />
              <button
                type="submit"
                disabled={isCreating || !name.trim()}
                className="flex items-center justify-center space-x-1 px-3 py-1.5 rounded bg-emerald-500 hover:bg-emerald-400 text-zinc-950 font-semibold transition disabled:opacity-50"
              >
                {isCreating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
                <span>Issue Key</span>
              </button>
            </div>
          </form>

          {/* Keys Table */}
          <div className="border border-[#27272A] rounded overflow-hidden">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="border-b border-[#27272A] text-[11px] text-zinc-500 bg-[#09090B]">
                  <th className="py-2 px-3">Token</th>
                  <th className="py-2 px-3">Name</th>
                  <th className="py-2 px-3 text-right">RPM Limit</th>
                  <th className="py-2 px-3 text-right">Monthly Budget</th>
                  <th className="py-2 px-3 text-center">Status</th>
                  <th className="py-2 px-2 text-center w-8"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#1F1F23]">
                {loading ? (
                  <tr>
                    <td colSpan={6} className="py-6 text-center text-zinc-500">
                      Loading virtual keys...
                    </td>
                  </tr>
                ) : keys.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="py-6 text-center text-zinc-500">
                      No virtual keys issued yet. Issue one above to enforce team rate limits.
                    </td>
                  </tr>
                ) : (
                  keys.map((k) => (
                    <tr key={k.key_val} className="hover:bg-[#18181B] transition">
                      <td className="py-2 px-3 whitespace-nowrap text-zinc-300">
                        <div className="flex items-center space-x-2">
                          <span>{k.key_val.slice(0, 10)}...</span>
                          <button
                            onClick={() => handleCopy(k.key_val)}
                            title="Copy full key token"
                            className="text-zinc-500 hover:text-white transition"
                          >
                            {copiedKey === k.key_val ? (
                              <Check className="w-3 h-3 text-emerald-400" />
                            ) : (
                              <Copy className="w-3 h-3" />
                            )}
                          </button>
                        </div>
                      </td>
                      <td className="py-2 px-3 text-zinc-300 font-medium">{k.name}</td>
                      <td className="py-2 px-3 text-right tabular-nums text-zinc-400">
                        {k.rate_limit_rpm} rpm
                      </td>
                      <td className="py-2 px-3 text-right tabular-nums text-zinc-400">
                        ${k.spent_this_month_usd?.toFixed(2) ?? "0.00"} / ${k.monthly_budget_usd}
                      </td>
                      <td className="py-2 px-3 text-center">
                        {Boolean(k.is_active) ? (
                          <span className="badge-cache-hit px-1.5 py-0.5 rounded text-[10px]">
                            ACTIVE
                          </span>
                        ) : (
                          <span className="badge-blocked px-1.5 py-0.5 rounded text-[10px]">
                            REVOKED
                          </span>
                        )}
                      </td>
                      <td className="py-2 px-2 text-center">
                        {Boolean(k.is_active) && (
                          <button
                            onClick={() => handleRevoke(k.key_val)}
                            title="Revoke key"
                            className="text-zinc-600 hover:text-rose-400 transition"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
};
