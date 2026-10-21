"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, getAgentState, setAgentPaused, setAuthenticatedStorageVerification } from "./api-client";

export type PauseStatus = "loading" | "ready" | "error" | "auth-required";
export function useAgentPause(accessToken: string | null) {
  const [status, setStatus] = useState<PauseStatus>(accessToken ? "loading" : "auth-required");
  const [paused, setPaused] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmedFor, setConfirmedFor] = useState<string | null>(null);
  const inFlight = useRef(false);
  const generation = useRef(0);
  const verificationToken = useRef<string | null>(null);

  const refresh = useCallback(async () => {
    const current = ++generation.current;
    if (!accessToken) {
      verificationToken.current = null;
      setAuthenticatedStorageVerification(false);
      setPaused(null); setError(null); setBusy(false); setStatus("auth-required");
      return;
    }
    if (verificationToken.current !== accessToken) {
      verificationToken.current = accessToken;
      setAuthenticatedStorageVerification(false);
    }
    setStatus("loading"); setError(null);
    try {
      const state = await getAgentState(accessToken);
      if (current === generation.current) { setAuthenticatedStorageVerification(true); setPaused(state.paused); setConfirmedFor(accessToken); setStatus("ready"); }
    } catch (reason) {
      if (current === generation.current) { setAuthenticatedStorageVerification(false); setPaused(null); setConfirmedFor(accessToken); setStatus("error"); setError(messageFor(reason)); }
    }
  }, [accessToken]);

  useEffect(() => { void refresh(); return () => { generation.current++; }; }, [refresh]);

  const update = useCallback(async () => {
    if (!accessToken || confirmedFor !== accessToken || status !== "ready" || paused === null || inFlight.current) return;
    const current = generation.current;
    inFlight.current = true; setBusy(true); setError(null);
    try {
      const result = await setAgentPaused(accessToken, !paused);
      if (current === generation.current) {
        setAuthenticatedStorageVerification(true);
        setPaused(result.paused); setConfirmedFor(accessToken); setStatus("ready");
      }
    } catch (reason) {
      // A lost response may follow a successful write. Mark state unknown until it is reloaded.
      if (current === generation.current) {
        setAuthenticatedStorageVerification(false);
        setPaused(null); setConfirmedFor(accessToken); setStatus("error"); setError(messageFor(reason));
      }
    } finally { inFlight.current = false; setBusy(false); }
  }, [accessToken, confirmedFor, paused, status]);

  const effectiveStatus = !accessToken ? "auth-required" : confirmedFor !== accessToken ? "loading" : status;
  const effectivePaused = confirmedFor === accessToken ? paused : null;
  return { status: effectiveStatus, paused: effectivePaused, error: confirmedFor === accessToken ? error : null, busy, refresh, update };
}

function messageFor(reason: unknown): string {
  if (reason instanceof ApiError) {
    if (reason.status === 401) return "Your session is missing, expired, or invalid. Sign in again to manage pause state.";
    if (reason.status === 403) return "The signed-in session was denied access to this user's state. Check the owner-scoped database grants and policies.";
    return reason.message;
  }
  return reason instanceof Error ? reason.message : "The server pause state could not be confirmed.";
}
