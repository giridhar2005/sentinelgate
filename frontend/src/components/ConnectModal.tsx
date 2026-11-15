import React, { useState } from "react";
import { X, Copy, Check, Terminal, ExternalLink } from "lucide-react";

interface ConnectModalProps {
  isOpen: boolean;
  onClose: () => void;
  onCopySuccess: () => void;
}

type TabType = "cursor" | "cli" | "python" | "typescript" | "curl";

export const ConnectModal: React.FC<ConnectModalProps> = ({
  isOpen,
  onClose,
  onCopySuccess,
}) => {
  const [activeTab, setActiveTab] = useState<TabType>("cursor");
  const [copied, setCopied] = useState<boolean>(false);

  if (!isOpen) return null;

  const SNIPPETS: Record<TabType, { title: string; filename?: string; code: string }> = {
    cursor: {
      title: "Cursor IDE Configuration",
      filename: "Cursor Settings > Models > OpenAI API Key",
      code: `// 1. Open Cursor Settings (Cmd + ,) -> Models
// 2. Override OpenAI Base URL:
http://localhost:8000/v1

// 3. Set OpenAI API Key:
sg-local-dev

// 4. In .cursorrules or prompt, use model="auto" for automatic cost-routing!`,
    },
    cli: {
      title: "Claude Code / Aider / Terminal CLI",
      filename: "Terminal Environment Variables",
      code: `# Point any AI coding assistant to SentinelGate localhost gateway
export OPENAI_API_BASE="http://localhost:8000/v1"
export OPENAI_API_KEY="sg-local"

# Now run aider or any LLM tool:
aider --model auto`,
    },
    python: {
      title: "Python (Official OpenAI SDK)",
      filename: "client.py",
      code: `from openai import OpenAI

# Zero code modifications besides pointing base_url to SentinelGate
client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="sg-local",
)

response = client.chat.completions.create(
    model="auto",  # SentinelGate complexity router
    messages=[{"role": "user", "content": "Explain Paxos vs Raft"}]
)

print(response.choices[0].message.content)`,
    },
    typescript: {
      title: "TypeScript / Node.js (OpenAI SDK)",
      filename: "client.ts",
      code: `import OpenAI from "openai";

const openai = new OpenAI({
  baseURL: "http://localhost:8000/v1",
  apiKey: "sg-local",
});

async function main() {
  const completion = await openai.chat.completions.create({
    model: "auto",
    messages: [{ role: "user", content: "Optimize this SQL query..." }],
  });

  console.log(completion.choices[0].message.content);
}

main();`,
    },
    curl: {
      title: "Standard cURL Request",
      filename: "curl",
      code: `curl -X POST http://localhost:8000/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer sg-local" \\
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "Ping gateway"}],
    "temperature": 0.7
  }'`,
    },
  };

  const current = SNIPPETS[activeTab];

  const handleCopy = () => {
    navigator.clipboard.writeText(current.code);
    setCopied(true);
    onCopySuccess();
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/70 backdrop-blur-sm">
      <div className="w-full max-w-2xl bg-[#111113] border border-[#27272A] rounded-lg shadow-2xl overflow-hidden font-mono text-xs">
        {/* Modal Header */}
        <div className="p-4 border-b border-[#27272A] flex items-center justify-between bg-[#0E0E10]">
          <div className="flex items-center space-x-2">
            <Terminal className="w-4 h-4 text-emerald-400" />
            <span className="font-semibold text-zinc-200 text-sm">
              Connect Any Tool in 5 Seconds
            </span>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded hover:bg-zinc-800 text-zinc-400 hover:text-white transition"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Tab Selection */}
        <div className="flex border-b border-[#27272A] bg-[#09090B] px-3 pt-2 gap-1 overflow-x-auto">
          {(["cursor", "cli", "python", "typescript", "curl"] as TabType[]).map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-3 py-1.5 rounded-t text-[11px] uppercase tracking-wider transition ${
                activeTab === tab
                  ? "bg-[#111113] text-white border-t border-x border-[#27272A] font-medium"
                  : "text-zinc-500 hover:text-zinc-300"
              }`}
            >
              {tab}
            </button>
          ))}
        </div>

        {/* Snippet Body */}
        <div className="p-4 space-y-3">
          <div className="flex items-center justify-between text-zinc-400">
            <span>{current.filename}</span>
            <button
              onClick={handleCopy}
              className="flex items-center space-x-1.5 px-2.5 py-1 rounded bg-[#18181B] border border-[#27272A] text-zinc-300 hover:text-white hover:border-zinc-600 transition"
            >
              {copied ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
              <span>{copied ? "Copied" : "Copy Code"}</span>
            </button>
          </div>

          <div className="p-3 bg-[#09090B] border border-[#27272A] rounded text-zinc-200 overflow-x-auto">
            <pre className="whitespace-pre font-mono leading-relaxed">{current.code}</pre>
          </div>

          <p className="text-[11px] text-zinc-500 leading-relaxed">
            All requests to <code className="text-zinc-300">http://localhost:8000/v1</code> automatically inherit sub-millisecond semantic caching, circuit-breaker failover, and secret redaction.
          </p>
        </div>
      </div>
    </div>
  );
};
