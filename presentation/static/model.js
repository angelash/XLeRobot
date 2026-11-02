export const MAX_EVENTS = 500;

export function mergeEvents(current, batch) {
  const reset = batch.reset || (current.epoch && batch.epoch !== current.epoch);
  const events = reset ? [] : current.events;
  const known = new Set(events.map(event => event.event_id));
  const additions = (batch.events || []).filter(event => {
    if (known.has(event.event_id)) return false;
    known.add(event.event_id);
    return true;
  });
  return {epoch: batch.epoch, seq: batch.seq ?? current.seq,
    events: [...events, ...additions].slice(-MAX_EVENTS)};
}

export function filterEvents(events, filter) {
  if (filter === 'all') return events;
  if (filter === 'summary') return events.filter(e => ['summary', 'task', 'message', 'system'].includes(e.kind));
  return events.filter(e => e.kind === filter);
}

export function visualFrame(event) {
  if (!event.media_id && !event.demo_url) return null;
  return {id: event.frame_id || event.event_id, url: event.demo_url || `/api/presence/media/${event.media_id}`,
    overlays: event.frame_id ? (event.overlays || []) : [],
    at: event.captured_at, evidence: event.evidence, text: event.text, title: event.title};
}

export function viewState(events) {
  const last = events.at(-1);
  if (!last) return 'idle';
  if (['failed', 'interrupted'].includes(last.phase)) return 'error';
  if (['started', 'progress'].includes(last.phase) && last.kind !== 'system') return 'working';
  return last.kind === 'message' ? 'responding' : 'idle';
}
