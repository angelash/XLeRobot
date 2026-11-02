// getRandomValues works on ordinary LAN HTTP, where randomUUID is unavailable.
export function createRequestId(cryptoProvider = globalThis.crypto) {
  if (typeof cryptoProvider?.randomUUID === 'function') return 'presence-' + cryptoProvider.randomUUID();
  if (typeof cryptoProvider?.getRandomValues !== 'function') throw new Error('浏览器无法生成请求编号，请刷新或更换浏览器');
  const bytes = cryptoProvider.getRandomValues(new Uint8Array(16));
  return 'presence-' + Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
}
