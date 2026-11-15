import React from "react";
import { ToastMessage } from "../types";
import { CheckCircle2, AlertCircle, Info, X } from "lucide-react";

interface ToastContainerProps {
  toasts: ToastMessage[];
  onDismiss: (id: string) => void;
}

export const ToastContainer: React.FC<ToastContainerProps> = ({ toasts, onDismiss }) => {
  if (toasts.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col space-y-2 max-w-sm w-full font-mono text-xs">
      {toasts.map((toast) => {
        const isSuccess = toast.type === "success";
        const isError = toast.type === "error";

        return (
          <div
            key={toast.id}
            className={`p-3 rounded-lg border shadow-lg flex items-start space-x-2.5 transition-all bg-[#111113] ${
              isSuccess
                ? "border-emerald-500/40 text-emerald-200"
                : isError
                ? "border-rose-500/40 text-rose-200"
                : "border-zinc-700 text-zinc-200"
            }`}
          >
            <div className="mt-0.5">
              {isSuccess ? (
                <CheckCircle2 className="w-4 h-4 text-emerald-400" />
              ) : isError ? (
                <AlertCircle className="w-4 h-4 text-rose-400" />
              ) : (
                <Info className="w-4 h-4 text-blue-400" />
              )}
            </div>

            <div className="flex-1 space-y-0.5">
              <div className="font-semibold text-zinc-100">{toast.title}</div>
              {toast.message && <div className="text-zinc-400 text-[11px]">{toast.message}</div>}
            </div>

            <button
              onClick={() => onDismiss(toast.id)}
              className="text-zinc-500 hover:text-zinc-300 p-0.5"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        );
      })}
    </div>
  );
};
