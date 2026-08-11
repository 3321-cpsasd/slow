import { api } from './api/client';

export type ProductEventName =
  | 'home_viewed'
  | 'shelf_viewed'
  | 'learning_viewed'
  | 'profile_viewed'
  | 'section_viewed'
  | 'quiz_viewed'
  | 'feedback_opened'
  | 'explanation_style_requested'
  | 'explanation_style_feedback'
  | 'explanation_style_remembered'
  | 'active_reading_60s'
  | 'frontend_error';

type ProductEventContext = {
  view?: '' | 'home' | 'shelf' | 'learn' | 'profile';
  entityType?: '' | 'shelf' | 'series' | 'book' | 'chapter' | 'section';
  entityId?: string;
  properties?: Record<string, string | number | boolean>;
};

type QueuedEvent = ProductEventContext & {
  eventId: string;
  sessionId: string;
  eventName: ProductEventName;
  occurredAt: string;
  pagePath: string;
};

const newId = (prefix: string) => `${prefix}_${crypto.randomUUID().replaceAll('-', '')}`;

class FirstPartyTelemetry {
  private enabled = false;
  private started = false;
  private flushing = false;
  private readonly sessionId = newId('session');
  private queue: QueuedEvent[] = [];

  start() {
    if (this.started) return;
    this.started = true;
    window.setInterval(() => void this.flush(), 8_000);
    window.addEventListener('error', () => {
      this.track('frontend_error', { properties: { kind: 'window_error' } });
    });
    window.addEventListener('unhandledrejection', () => {
      this.track('frontend_error', { properties: { kind: 'unhandled_rejection' } });
    });
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') void this.flush();
    });
    window.addEventListener('pagehide', () => void this.flush());
  }

  setEnabled(enabled: boolean) {
    this.enabled = enabled;
    if (!enabled) this.queue = [];
  }

  track(eventName: ProductEventName, context: ProductEventContext = {}) {
    if (!this.enabled) return;
    this.queue.push({
      eventId: newId('event'),
      sessionId: this.sessionId,
      eventName,
      occurredAt: new Date().toISOString(),
      pagePath: window.location.pathname || '/',
      view: context.view ?? '',
      entityType: context.entityType ?? '',
      entityId: context.entityId ?? '',
      properties: context.properties ?? {},
    });
    if (this.queue.length > 200) this.queue.splice(0, this.queue.length - 200);
    if (this.queue.length >= 10) void this.flush();
  }

  async flush() {
    if (!this.enabled || this.flushing || this.queue.length === 0) return;
    this.flushing = true;
    const batch = this.queue.splice(0, 25);
    try {
      await api.productEvents({ events: batch });
    } catch {
      this.queue.unshift(...batch);
      if (this.queue.length > 200) this.queue.length = 200;
    } finally {
      this.flushing = false;
    }
  }
}

export const telemetry = new FirstPartyTelemetry();
