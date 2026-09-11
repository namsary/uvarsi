export const TESCO_GRAPHQL_ENDPOINT =
  "https://api.prod.retail.tesco.com/marketing/leaflets-be/graphql";
export const MAX_MANIFEST_BYTES = 1_000_000;
export const MAX_MEDIA_BYTES = 10_000_000;

const MEDIA_HOST = "digitalcontent.api.tesco.com";
const MEDIA_PATH_PREFIX = "/v2/media/dotcom-hu/";
const MANIFEST_CACHE_SECONDS = 60;
const CLIENT_MANIFEST_CACHE_SECONDS = 30;
const MEDIA_TOKEN_SECONDS = 86_400;
const MAX_DATE_DISTANCE_DAYS = 14;
const MAX_REQUEST_BYTES = 1_024;
const MAX_TOKEN_LENGTH = 4_096;
const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: true });

function jsonResponse(body, status, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "X-Content-Type-Options": "nosniff",
      ...extraHeaders,
    },
  });
}

function errorResponse(status, error) {
  return jsonResponse({ error }, status, { "Cache-Control": "no-store" });
}

function isAuthorized(request, env) {
  return (
    typeof env?.BRIDGE_SECRET === "string" &&
    env.BRIDGE_SECRET.length > 0 &&
    request.headers.get("Authorization") === `Bearer ${env.BRIDGE_SECRET}`
  );
}

function parseContentLength(headers, maximum) {
  const raw = headers.get("Content-Length");
  if (raw === null) {
    return;
  }
  if (!/^\d+$/.test(raw) || Number(raw) > maximum) {
    throw new Error("body_limit");
  }
}

async function readLimitedBytes(message, maximum) {
  parseContentLength(message.headers, maximum);
  if (!message.body) {
    return new Uint8Array();
  }

  const reader = message.body.getReader();
  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      total += value.byteLength;
      if (total > maximum) {
        await reader.cancel();
        throw new Error("body_limit");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }

  const result = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}

function parseIsoDay(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return null;
  }
  const [year, month, day] = value.split("-").map(Number);
  const timestamp = Date.UTC(year, month - 1, day);
  const parsed = new Date(timestamp);
  if (
    parsed.getUTCFullYear() !== year ||
    parsed.getUTCMonth() !== month - 1 ||
    parsed.getUTCDate() !== day
  ) {
    return null;
  }
  return timestamp;
}

function parseUtcIsoTimestamp(value) {
  if (
    typeof value !== "string" ||
    !/^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,3})?Z$/.test(
      value,
    )
  ) {
    return null;
  }
  const day = parseIsoDay(value.slice(0, 10));
  const timestamp = Date.parse(value);
  return day !== null && Number.isFinite(timestamp) ? timestamp : null;
}

function isDateNearNow(timestamp, now) {
  const current = new Date(now);
  const currentDay = Date.UTC(
    current.getUTCFullYear(),
    current.getUTCMonth(),
    current.getUTCDate(),
  );
  const distance = Math.abs(timestamp - currentDay);
  return distance <= MAX_DATE_DISTANCE_DAYS * 24 * 60 * 60 * 1_000;
}

async function parseManifestInput(request, now) {
  const contentType = request.headers.get("Content-Type") ?? "";
  if (contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json") {
    return { error: 415 };
  }

  try {
    const body = decoder.decode(await readLimitedBytes(request, MAX_REQUEST_BYTES));
    const value = JSON.parse(body);
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return { error: 400 };
    }
    const keys = Object.keys(value).sort();
    if (keys.length !== 2 || keys[0] !== "date" || keys[1] !== "format") {
      return { error: 400 };
    }
    const timestamp = parseIsoDay(value.date);
    if (
      timestamp === null ||
      !isDateNearNow(timestamp, now) ||
      (value.format !== "HM" && value.format !== "SM")
    ) {
      return { error: 400 };
    }
    return { date: value.date, format: value.format };
  } catch {
    return { error: 400 };
  }
}

function buildTescoQuery(date, format) {
  const after = `${date}T00:00:00.000Z`;
  return `query CurrentSlovakLeaflets {
  leaflets(options: { filter: {
    country: { eq: sk }
    type: { eq: ${format} }
    validTo: { after: "${after}" }
  } }) {
    totalItems
    items {
      __typename id country countryId leafletUrl
      pages { __typename pagePNG }
      promoP1Name slug type validFrom validTo
    }
  }
}`;
}

function safeMediaUrl(value) {
  if (
    typeof value !== "string" ||
    value.length > 2_048 ||
    value !== value.trim()
  ) {
    return null;
  }

  try {
    const parsed = new URL(value);
    if (
      parsed.protocol !== "https:" ||
      parsed.hostname !== MEDIA_HOST ||
      parsed.port !== "" ||
      parsed.username !== "" ||
      parsed.password !== "" ||
      parsed.search !== "" ||
      parsed.hash !== "" ||
      !parsed.pathname.startsWith(MEDIA_PATH_PREFIX) ||
      !parsed.pathname.toLowerCase().endsWith(".jpeg") ||
      parsed.pathname.includes("%") ||
      parsed.pathname.includes("\\") ||
      parsed.href !== value
    ) {
      return null;
    }
    const segments = parsed.pathname.split("/");
    if (segments.includes(".") || segments.includes("..")) {
      return null;
    }
    return parsed.href;
  } catch {
    return null;
  }
}

function normalizeLeaflet(value, requestedDate, requestedFormat) {
  if (
    !value ||
    typeof value !== "object" ||
    value.__typename !== "Leaflet" ||
    value.country !== "sk" ||
    value.type !== requestedFormat ||
    typeof value.slug !== "string" ||
    !/^tesco-letak-\d{4}-\d{2}-\d{2}$/.test(value.slug) ||
    typeof value.validFrom !== "string" ||
    typeof value.validTo !== "string"
  ) {
    return null;
  }

  const validFrom = value.validFrom.slice(0, 10);
  const validTo = value.validTo.slice(0, 10);
  const fromTimestamp = parseUtcIsoTimestamp(value.validFrom);
  const toTimestamp = parseUtcIsoTimestamp(value.validTo);
  if (
    fromTimestamp === null ||
    toTimestamp === null ||
    validFrom > requestedDate ||
    requestedDate > validTo ||
    fromTimestamp > toTimestamp ||
    !Array.isArray(value.pages) ||
    value.pages.length < 8 ||
    value.pages.length > 120
  ) {
    return null;
  }

  const pages = [];
  const seen = new Set();
  for (const page of value.pages) {
    const url = safeMediaUrl(page?.pagePNG);
    if (!url) {
      return null;
    }
    const match = new URL(url).pathname.match(/\.([1-9]\d*)\.jpeg$/i);
    if (!match) {
      return null;
    }
    const number = Number(match[1]);
    if (!Number.isSafeInteger(number) || seen.has(number)) {
      return null;
    }
    seen.add(number);
    pages.push({ source_page: number, upstream_url: url });
  }
  pages.sort((left, right) => left.source_page - right.source_page);
  if (pages.some((page, index) => page.source_page !== index + 1)) {
    return null;
  }

  const segment = requestedFormat === "HM" ? "hypermarkety" : "supermarkety";
  return {
    country: "sk",
    format: requestedFormat,
    slug: value.slug,
    valid_from: validFrom,
    valid_to: validTo,
    source_url:
      `https://www.tesco.sk/akciove-ponuky/letaky-a-katalogy/` +
      `${segment}/${value.slug}/1`,
    declared_pages: pages.length,
    pages,
  };
}

function normalizeUpstreamPayload(payload, requestedDate, requestedFormat) {
  if (payload?.errors) {
    throw new Error("graphql_error");
  }
  const items = payload?.data?.leaflets?.items;
  if (!Array.isArray(items)) {
    throw new Error("invalid_manifest");
  }
  const candidates = items
    .map((item) => normalizeLeaflet(item, requestedDate, requestedFormat))
    .filter(Boolean)
    .sort((left, right) => {
      const byStart = right.valid_from.localeCompare(left.valid_from);
      return byStart || right.declared_pages - left.declared_pages;
    });
  if (candidates.length === 0) {
    throw new Error("missing_leaflet");
  }
  return candidates[0];
}

function base64UrlEncode(value) {
  let binary = "";
  const source = value instanceof Uint8Array ? value : new Uint8Array(value);
  for (let offset = 0; offset < source.length; offset += 0x8000) {
    binary += String.fromCharCode(...source.subarray(offset, offset + 0x8000));
  }
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function base64UrlDecode(value) {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) {
    throw new Error("invalid_base64");
  }
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const binary = atob(value.replaceAll("-", "+").replaceAll("_", "/") + padding);
  const result = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  if (base64UrlEncode(result) !== value) {
    throw new Error("noncanonical_base64");
  }
  return result;
}

async function importHmacKey(cryptoImpl, secret, usage) {
  return cryptoImpl.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    [usage],
  );
}

async function signMediaToken(url, expiresAt, key, cryptoImpl) {
  const payload = encoder.encode(JSON.stringify({ u: url, e: expiresAt }));
  const signature = await cryptoImpl.subtle.sign("HMAC", key, payload);
  return `${base64UrlEncode(payload)}.${base64UrlEncode(signature)}`;
}

async function verifyMediaToken(token, secret, cryptoImpl, now) {
  if (typeof token !== "string" || token.length > MAX_TOKEN_LENGTH) {
    throw new Error("invalid_token");
  }
  const parts = token.split(".");
  if (parts.length !== 2) {
    throw new Error("invalid_token");
  }
  const payloadBytes = base64UrlDecode(parts[0]);
  const signature = base64UrlDecode(parts[1]);
  if (signature.byteLength !== 32) {
    throw new Error("invalid_token");
  }
  const key = await importHmacKey(cryptoImpl, secret, "verify");
  const verified = await cryptoImpl.subtle.verify(
    "HMAC",
    key,
    signature,
    payloadBytes,
  );
  if (!verified) {
    throw new Error("invalid_token");
  }

  const payload = JSON.parse(decoder.decode(payloadBytes));
  const keys = Object.keys(payload).sort();
  const nowSeconds = Math.floor(now / 1_000);
  if (
    !payload ||
    typeof payload !== "object" ||
    Array.isArray(payload) ||
    keys.length !== 2 ||
    keys[0] !== "e" ||
    keys[1] !== "u" ||
    typeof payload.u !== "string" ||
    !Number.isSafeInteger(payload.e) ||
    payload.e <= nowSeconds ||
    payload.e > nowSeconds + MEDIA_TOKEN_SECONDS ||
    !safeMediaUrl(payload.u)
  ) {
    throw new Error("invalid_token");
  }
  return { url: payload.u, expiresAt: payload.e };
}

function manifestCacheKey(date, format) {
  return new Request(
    `https://tesco-bridge-cache.invalid/manifest?date=${date}&format=${format}`,
  );
}

async function scheduleCachePut(cache, key, response, context) {
  if (!cache) {
    return;
  }
  const operation = cache.put(key, response);
  if (typeof context?.waitUntil === "function") {
    context.waitUntil(operation);
  } else {
    await operation;
  }
}

async function fetchNormalizedLeaflet(
  date,
  format,
  fetchImpl,
  cache,
  context,
) {
  const cacheKey = manifestCacheKey(date, format);
  if (cache) {
    const cached = await cache.match(cacheKey);
    if (cached) {
      try {
        return JSON.parse(await cached.text());
      } catch {
        // Ignore a corrupt cache entry and refill it from the fixed upstream.
      }
    }
  }

  const upstream = await fetchImpl(TESCO_GRAPHQL_ENDPOINT, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ query: buildTescoQuery(date, format) }),
    redirect: "manual",
  });
  if (upstream.status !== 200) {
    throw new Error("upstream_status");
  }
  const contentType = upstream.headers.get("Content-Type") ?? "";
  if (contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json") {
    throw new Error("upstream_content_type");
  }
  const raw = await readLimitedBytes(upstream, MAX_MANIFEST_BYTES);
  const payload = JSON.parse(decoder.decode(raw));
  const normalized = normalizeUpstreamPayload(payload, date, format);

  await scheduleCachePut(
    cache,
    cacheKey,
    new Response(JSON.stringify(normalized), {
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": `public, max-age=${MANIFEST_CACHE_SECONDS}`,
      },
    }),
    context,
  );
  return normalized;
}

async function handleManifest(
  request,
  env,
  context,
  { fetchImpl, cryptoImpl, cache, now },
) {
  const input = await parseManifestInput(request, now());
  if (input.error) {
    return errorResponse(input.error, "invalid_request");
  }
  if (typeof env?.TOKEN_SECRET !== "string" || env.TOKEN_SECRET.length === 0) {
    return errorResponse(500, "internal_error");
  }

  try {
    const leaflet = await fetchNormalizedLeaflet(
      input.date,
      input.format,
      fetchImpl,
      cache,
      context,
    );
    const expiresAt = Math.floor(now() / 1_000) + MEDIA_TOKEN_SECONDS;
    const key = await importHmacKey(cryptoImpl, env.TOKEN_SECRET, "sign");
    const pages = await Promise.all(
      leaflet.pages.map(async (page) => {
        const token = await signMediaToken(
          page.upstream_url,
          expiresAt,
          key,
          cryptoImpl,
        );
        const imageUrl =
          `${new URL(request.url).origin}/v1/tesco/media/${token}`;
        return {
          source_page: page.source_page,
          thumbnail_url: imageUrl,
          image_url: imageUrl,
        };
      }),
    );
    return jsonResponse(
      { leaflet: { ...leaflet, pages } },
      200,
      {
        "Cache-Control": `private, max-age=${CLIENT_MANIFEST_CACHE_SECONDS}`,
        Vary: "Authorization",
      },
    );
  } catch {
    return errorResponse(502, "upstream_failed");
  }
}

function mediaClientResponse(body, expiresAt, now) {
  const remaining = Math.max(
    0,
    Math.min(MEDIA_TOKEN_SECONDS, expiresAt - Math.floor(now / 1_000) - 1),
  );
  return new Response(body, {
    status: 200,
    headers: {
      "Content-Type": "image/jpeg",
      "Cache-Control": `private, max-age=${remaining}, immutable`,
      "X-Content-Type-Options": "nosniff",
      Vary: "Authorization",
    },
  });
}

async function handleMedia(
  token,
  env,
  context,
  { fetchImpl, cryptoImpl, cache, now },
) {
  if (typeof env?.TOKEN_SECRET !== "string" || env.TOKEN_SECRET.length === 0) {
    return errorResponse(500, "internal_error");
  }

  let verified;
  try {
    verified = await verifyMediaToken(token, env.TOKEN_SECRET, cryptoImpl, now());
  } catch {
    return errorResponse(400, "invalid_token");
  }

  const cacheKey = new Request(verified.url);
  if (cache) {
    const cached = await cache.match(cacheKey);
    if (cached) {
      return mediaClientResponse(cached.body, verified.expiresAt, now());
    }
  }

  try {
    const upstream = await fetchImpl(verified.url, {
      method: "GET",
      headers: { Accept: "image/jpeg" },
      redirect: "manual",
    });
    if (upstream.status !== 200) {
      throw new Error("upstream_status");
    }
    const contentType = upstream.headers.get("Content-Type") ?? "";
    if (contentType.split(";", 1)[0].trim().toLowerCase() !== "image/jpeg") {
      throw new Error("upstream_content_type");
    }
    const body = await readLimitedBytes(upstream, MAX_MEDIA_BYTES);
    if (
      body.byteLength < 3 ||
      body[0] !== 0xff ||
      body[1] !== 0xd8 ||
      body[2] !== 0xff
    ) {
      throw new Error("invalid_jpeg");
    }

    await scheduleCachePut(
      cache,
      cacheKey,
      new Response(body, {
        headers: {
          "Content-Type": "image/jpeg",
          "Cache-Control": "public, max-age=31536000, immutable",
        },
      }),
      context,
    );
    return mediaClientResponse(body, verified.expiresAt, now());
  } catch {
    return errorResponse(502, "upstream_failed");
  }
}

export function createWorker(dependencies = {}) {
  const runtime = {
    fetchImpl: dependencies.fetchImpl ?? globalThis.fetch.bind(globalThis),
    cryptoImpl: dependencies.cryptoImpl ?? globalThis.crypto,
    cache: dependencies.cache ?? globalThis.caches?.default ?? null,
    now: dependencies.now ?? Date.now,
  };

  return {
    async fetch(request, env, context) {
      let url;
      try {
        url = new URL(request.url);
      } catch {
        return errorResponse(400, "invalid_request");
      }

      if (url.search !== "") {
        return errorResponse(404, "not_found");
      }

      if (url.pathname === "/v1/tesco/leaflets") {
        if (request.method !== "POST") {
          return errorResponse(405, "method_not_allowed");
        }
        if (!isAuthorized(request, env)) {
          return errorResponse(401, "unauthorized");
        }
        return handleManifest(request, env, context, runtime);
      }

      const mediaPrefix = "/v1/tesco/media/";
      if (
        url.pathname.startsWith(mediaPrefix) &&
        url.pathname.length > mediaPrefix.length &&
        !url.pathname.slice(mediaPrefix.length).includes("/")
      ) {
        if (request.method !== "GET") {
          return errorResponse(405, "method_not_allowed");
        }
        if (!isAuthorized(request, env)) {
          return errorResponse(401, "unauthorized");
        }
        return handleMedia(
          url.pathname.slice(mediaPrefix.length),
          env,
          context,
          runtime,
        );
      }

      return errorResponse(404, "not_found");
    },
  };
}

export default {
  fetch(request, env, context) {
    return createWorker().fetch(request, env, context);
  },
};
