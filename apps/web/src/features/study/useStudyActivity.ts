import { useCallback, useEffect, useRef, useState } from 'react';

import { api } from '../../api/client';
import type { StudyActivityKind } from '../../model/types';


const PULSE_INTERVAL_MS = 15_000;
const INACTIVITY_LIMIT_MS = 5 * 60_000;
const SESSION_STORAGE_KEY = 'slow-study-activity-session-v1';


function timezoneName() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
}


function clientSessionId() {
  try {
    const existing = window.sessionStorage.getItem(SESSION_STORAGE_KEY);
    if (existing) return existing;
    const created = `study_${crypto.randomUUID().replaceAll('-', '')}`;
    window.sessionStorage.setItem(SESSION_STORAGE_KEY, created);
    return created;
  } catch {
    return `study_${crypto.randomUUID().replaceAll('-', '')}`;
  }
}


export function useStudyActivity({
  sectionId,
  activityKind,
  keepActive,
}: {
  sectionId: string | null;
  activityKind: StudyActivityKind;
  keepActive: boolean;
}) {
  const [sessionSeconds, setSessionSeconds] = useState(0);
  const [paused, setPaused] = useState(false);
  const lastInteractionRef = useRef(Date.now());
  const interactionRequiredRef = useRef(false);
  const activeRef = useRef(false);
  const sequenceRef = useRef(0);
  const sessionIdRef = useRef('');
  const kindRef = useRef(activityKind);
  const keepActiveRef = useRef(keepActive);

  kindRef.current = activityKind;
  keepActiveRef.current = keepActive;
  if (!sessionIdRef.current && typeof window !== 'undefined') {
    sessionIdRef.current = clientSessionId();
  }

  const resume = useCallback(() => {
    lastInteractionRef.current = Date.now();
    interactionRequiredRef.current = false;
    activeRef.current = Boolean(sectionId);
    setPaused(false);
  }, [sectionId]);

  useEffect(() => {
    setSessionSeconds(0);
    lastInteractionRef.current = Date.now();
    interactionRequiredRef.current = false;
    activeRef.current = Boolean(sectionId);
    setPaused(false);
  }, [sectionId]);

  useEffect(() => {
    if (!sectionId) return undefined;
    const markInteraction = () => resume();
    const pauseForBackground = () => {
      if (document.visibilityState === 'hidden' || !document.hasFocus()) {
        interactionRequiredRef.current = true;
        activeRef.current = false;
        setPaused(true);
      }
    };
    const interactionEvents: (keyof DocumentEventMap)[] = [
      'pointerdown',
      'keydown',
      'touchstart',
      'wheel',
      'scroll',
    ];
    for (const name of interactionEvents) {
      document.addEventListener(name, markInteraction, true);
    }
    document.addEventListener('visibilitychange', pauseForBackground);
    window.addEventListener('blur', pauseForBackground);
    return () => {
      for (const name of interactionEvents) {
        document.removeEventListener(name, markInteraction, true);
      }
      document.removeEventListener('visibilitychange', pauseForBackground);
      window.removeEventListener('blur', pauseForBackground);
    };
  }, [resume, sectionId]);

  useEffect(() => {
    if (!sectionId) return undefined;
    const reconcile = () => {
      const foreground = document.visibilityState === 'visible' && document.hasFocus();
      const idle = Date.now() - lastInteractionRef.current >= INACTIVITY_LIMIT_MS;
      const shouldPause = !keepActiveRef.current && (
        !foreground || interactionRequiredRef.current || idle
      );
      activeRef.current = !shouldPause;
      setPaused(shouldPause);
      if (!shouldPause) setSessionSeconds((value) => value + 1);
    };
    const timer = window.setInterval(reconcile, 1000);
    return () => window.clearInterval(timer);
  }, [sectionId]);

  useEffect(() => {
    if (!sectionId || !keepActive) return;
    interactionRequiredRef.current = false;
    lastInteractionRef.current = Date.now();
    activeRef.current = true;
    setPaused(false);
  }, [keepActive, sectionId]);

  useEffect(() => {
    if (!sectionId) return undefined;
    const sendPulse = () => {
      if (
        !activeRef.current
        || document.visibilityState !== 'visible'
        || !document.hasFocus()
      ) return;
      const clientSequence = sequenceRef.current++;
      void api.studyActivityHeartbeat({
        eventId: `study_${crypto.randomUUID().replaceAll('-', '')}`,
        clientSessionId: sessionIdRef.current,
        clientSequence,
        activityKind: kindRef.current,
        sectionId,
        timezone: timezoneName(),
      }).catch(() => undefined);
    };
    sendPulse();
    const timer = window.setInterval(sendPulse, PULSE_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [sectionId]);

  return { sessionSeconds, paused, resume };
}
