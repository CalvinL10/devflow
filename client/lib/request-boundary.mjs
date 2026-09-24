// Check the browser-facing Host before Next rewrites replace it with the backend Host.
// Forwarded headers are deliberately not an authority for this local-only application.
export function requestBoundaryError(request, configuredOrigin = "http://127.0.0.1:3000") {
  let publicUrl;
  try {
    publicUrl = new URL(configuredOrigin);
    if (!["http:", "https:"].includes(publicUrl.protocol) || publicUrl.username || publicUrl.password
      || publicUrl.pathname !== "/" || publicUrl.search || publicUrl.hash) throw new Error();
  } catch { return "invalid_public_origin"; }

  const host = request.headers.get("host");
  const authority = host?.match(/^([a-zA-Z0-9.-]+)(?::[0-9]{1,5})?$/);
  if (!authority) return "host_denied";
  let target;
  try {
    const protocol = host.toLowerCase() === publicUrl.host ? publicUrl.protocol : new URL(request.url).protocol;
    target = new URL(`${protocol}//${host}`);
    if (!["http:", "https:"].includes(protocol) || target.port === "0"
      || authority[1].toLowerCase() !== target.hostname) return "host_denied";
  } catch { return "host_denied"; }
  const loopback = target.hostname === "localhost" || target.hostname === "127.0.0.1";
  if (!loopback && target.origin !== publicUrl.origin) return "host_denied";

  const origin = request.headers.get("origin");
  // Exact origin comparison includes scheme and port. "null" is not a trusted origin.
  if (origin !== null && origin !== target.origin) return "origin_denied";
  const site = request.headers.get("sec-fetch-site");
  if (site === "cross-site" || site === "same-site") return "origin_denied";
  if (!["GET", "HEAD", "OPTIONS"].includes(request.method.toUpperCase())
    && request.headers.get("x-devflow-request") !== "1") return "csrf_denied";
  return null;
}
