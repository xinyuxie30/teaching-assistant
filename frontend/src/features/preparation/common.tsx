import { Button } from "antd";
import type { ReactNode } from "react";
import { useEffect, useRef } from "react";
import { ErrorNotice } from "../../components/common";
import type { OperationState } from "../../api/client";

export function useAccepted<T>(state: OperationState, accept: (result: T) => void) {
  const delivered = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (state.phase !== "succeeded" || !state.ticket || delivered.current === state.ticket.key) return;
    delivered.current = state.ticket.key;
    accept(state.result as T);
  }, [state.phase, state.ticket, state.result, accept]);
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return <div className="prep-field"><div className="prep-label">{label}</div>{children}
    {hint && <p className="muted small">{hint}</p>}</div>;
}
export function FormErrors({ errors }: { errors: string[] }) {
  return errors.length ? <ul className="notice notice-error prep-errors" role="alert">
    {errors.map((error, index) => <li key={index}>{error}</li>)}
  </ul> : null;
}
export function WriteFeedback({ state, retry }: { state: OperationState; retry: () => Promise<unknown> }) {
  return <><ErrorNotice error={state.error} />
    {state.phase === "uncertain" && <div className="notice warning" role="alert">
      这次提交的结果尚未确定。重发将复用原内容和提交标识。
      <Button onClick={() => { void retry().catch(() => undefined); }}>重试同一次提交</Button>
    </div>}</>;
}
