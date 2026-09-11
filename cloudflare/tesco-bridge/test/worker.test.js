import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

import worker, {
  createWorker,
  MAX_MANIFEST_BYTES,
  MAX_MEDIA_BYTES,
  TESCO_GRAPHQL_ENDPOINT,
} from "../src/worker.js";

const NOW = Date.parse("2026-09-11T12:00:00.000Z");
const BRIDGE_SECRET = "unit-bridge-secret";
const TOKEN_SECRET = "unit-token-secret";
const WORKER_RELEASE = "a1b2c3d4e5f6";
const WORKER_VERSION_ID = "11aa22bb-33cc-44dd-88ee-99ff00112233";
const WORKER_ORIGIN = "https://tesco-bridge.example";
const MEDIA_PREFIX =
  "https://digitalcontent.api.tesco.com/v2/media/dotcom-hu/";
const encoder = new TextEncoder();

function bytes(value) {
  return value instanceof Uint8Array ? value : new Uint8Array(value);
}

function pseudoMac(key, data) {
  const input = bytes(data);
  const secret = bytes(key);
  const output = new Uint8Array(32);

  for (let index = 0; index < secret.length; index += 1) {
    output[index % output.length] =
      (output[index % output.length] + secret[index] + index) & 0xff;
  }
  for (let index = 0; index < input.length; index += 1) {
    const slot = index % output.length;
    output[slot] = ((output[slot] * 33) ^ input[index] ^ index) & 0xff;
  }
  return output;
}

function mockWebCrypto() {
  return {
    subtle: {
      async importKey(format, keyData, algorithm, extractable, usages) {
        assert.equal(format, "raw");
        assert.deepEqual(algorithm, { name: "HMAC", hash: "SHA-256" });
        assert.equal(extractable, false);
        assert.ok(
          usages.length === 1 && ["sign", "verify"].includes(usages[0]),
        );
        return { keyData: bytes(keyData), usage: usages[0] };
      },
      async sign(algorithm, key, data) {
        assert.equal(algorithm, "HMAC");
        assert.equal(key.usage, "sign");
        return pseudoMac(key.keyData, data).buffer;
      },
      async verify(algorithm, key, signature, data) {
        assert.equal(algorithm, "HMAC");
        assert.equal(key.usage, "verify");
        const expected = pseudoMac(key.keyData, data);
        const actual = bytes(signature);
        return (
          expected.length === actual.length &&
          expected.every((value, index) => value === actual[index])
        );
      },
    },
  };
}

function base64UrlEncode(value) {
  return Buffer.from(value).toString("base64url");
}

function base64UrlDecode(value) {
  return Buffer.from(value, "base64url");
}

function resignToken(token, changes) {
  const [encodedPayload] = token.split(".");
  const payload = JSON.parse(base64UrlDecode(encodedPayload).toString("utf8"));
  const replacement = encoder.encode(JSON.stringify({ ...payload, ...changes }));
  const signature = pseudoMac(encoder.encode(TOKEN_SECRET), replacement);
  return `${base64UrlEncode(replacement)}.${base64UrlEncode(signature)}`;
}

function mediaUrl(page, overrides = {}) {
  const defaults = {
    base: MEDIA_PREFIX,
    file: `page-${page}/20260907_2026_P27_SK_HM-CHM.${page}.jpeg`,
  };
  const value = { ...defaults, ...overrides };
  return `${value.base}${value.file}`;
}

function leaflet({
  format = "HM",
  country = "sk",
  slug = "tesco-letak-2026-09-07",
  validFrom = "2026-09-07T06:00:00.000Z",
  validTo = "2026-09-13T21:59:59.000Z",
  pageCount = 8,
  pages,
} = {}) {
  const suffix = format === "HM" ? "HM-CHM" : "SM";
  const generatedPages = Array.from({ length: pageCount }, (_, index) => {
    const page = index + 1;
    return {
      __typename: "LeafletMetadataPage",
      pagePNG:
        `${MEDIA_PREFIX}page-${page}/` +
        `20260907_2026_P27_SK_${suffix}.${page}.jpeg`,
    };
  }).reverse();

  return {
    __typename: "Leaflet",
    id: 691,
    country,
    countryId: 3,
    leafletUrl: `${MEDIA_PREFIX}pdf-id/20260907_2026_P27_SK_${suffix}.pdf`,
    pages: pages ?? generatedPages,
    promoP1Name: `2026_P27_SK_${suffix}_Product-Data`,
    slug,
    type: format,
    validFrom,
    validTo,
  };
}

function graphqlPayload(...leaflets) {
  return {
    data: {
      leaflets: {
        totalItems: leaflets.length,
        items: leaflets,
      },
    },
  };
}

function jsonUpstream(payload, init = {}) {
  return new Response(JSON.stringify(payload), {
    status: init.status ?? 200,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...init.headers,
    },
  });
}

function authorizedHeaders(extra = {}) {
  return {
    Authorization: `Bearer ${BRIDGE_SECRET}`,
    ...extra,
  };
}

function manifestRequest(body = { date: "2026-09-11", format: "HM" }, init = {}) {
  return new Request(`${WORKER_ORIGIN}/v1/tesco/leaflets`, {
    method: "POST",
    headers: authorizedHeaders({
      "Content-Type": "application/json",
      ...init.headers,
    }),
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

function createFakeCache() {
  const entries = new Map();
  return {
    async match(request) {
      const key = request instanceof Request ? request.url : String(request);
      return entries.get(key)?.clone();
    },
    async put(request, response) {
      const key = request instanceof Request ? request.url : String(request);
      entries.set(key, response.clone());
    },
  };
}

function createContext() {
  const pending = [];
  return {
    context: {
      waitUntil(promise) {
        pending.push(promise);
      },
    },
    async settle() {
      await Promise.all(pending);
    },
  };
}

function subject(fetchImpl, options = {}) {
  const bridge = createWorker({
    fetchImpl,
    cryptoImpl: mockWebCrypto(),
    cache: options.cache ?? null,
    now: options.now ?? (() => NOW),
  });
  return {
    fetch(request, env = {}, context) {
      return bridge.fetch(
        request,
        {
          WORKER_RELEASE,
          CF_VERSION_METADATA: { id: WORKER_VERSION_ID },
          ...env,
        },
        context,
      );
    },
  };
}

async function readJson(response) {
  return JSON.parse(await response.text());
}

async function issueMediaToken({ fetchImpl, cache = null, now } = {}) {
  const upstream =
    fetchImpl ?? (async () => jsonUpstream(graphqlPayload(leaflet())));
  const bridge = subject(upstream, { cache, now });
  const response = await bridge.fetch(
    manifestRequest(),
    { BRIDGE_SECRET, TOKEN_SECRET },
    createContext().context,
  );
  assert.equal(response.status, 200);
  const payload = await readJson(response);
  const media = new URL(payload.leaflet.pages[0].image_url);
  return {
    bridge,
    token: media.pathname.slice("/v1/tesco/media/".length),
    url: media.href,
    payload,
  };
}

test("exports a Cloudflare module Worker handler", () => {
  assert.equal(typeof worker.fetch, "function");
});

test("requires the bearer secret on both locked routes", async () => {
  const unexpectedFetch = async () => assert.fail("upstream fetch must not run");
  const bridge = subject(unexpectedFetch);
  const requests = [
    new Request(`${WORKER_ORIGIN}/v1/tesco/leaflets`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ date: "2026-09-11", format: "HM" }),
    }),
    new Request(`${WORKER_ORIGIN}/v1/tesco/leaflets`, {
      method: "POST",
      headers: authorizedHeaders({
        Authorization: "Bearer wrong-secret",
        "Content-Type": "application/json",
      }),
      body: JSON.stringify({ date: "2026-09-11", format: "HM" }),
    }),
    new Request(`${WORKER_ORIGIN}/v1/tesco/media/not-a-token`),
    new Request(`${WORKER_ORIGIN}/v1/tesco/media/not-a-token`, {
      headers: { Authorization: "Bearer wrong-secret" },
    }),
  ];

  for (const request of requests) {
    const response = await bridge.fetch(
      request,
      { BRIDGE_SECRET, TOKEN_SECRET },
      createContext().context,
    );
    assert.equal(response.status, 401);
    assert.deepEqual(await readJson(response), { error: "unauthorized" });
  }
});

test("rejects every route and method outside the two exact interfaces", async () => {
  const bridge = subject(async () => assert.fail("upstream fetch must not run"));
  const cases = [
    ["GET", "/v1/tesco/leaflets", 405],
    ["PUT", "/v1/tesco/leaflets", 405],
    ["POST", "/v1/tesco/media/token", 405],
    ["GET", "/v1/tesco/media", 404],
    ["GET", "/v1/tesco/media/token/extra", 404],
    ["POST", "/v1/tesco/leaflets/", 404],
    ["POST", "/v1/tesco/leaflets?query=query%20Evil", 404],
    ["GET", "/v1/tesco/media/token?url=https%3A%2F%2Fevil.test", 404],
    ["GET", "/health", 404],
  ];

  for (const [method, path, status] of cases) {
    const response = await bridge.fetch(
      new Request(`${WORKER_ORIGIN}${path}`, {
        method,
        headers: authorizedHeaders(),
      }),
      { BRIDGE_SECRET, TOKEN_SECRET },
      createContext().context,
    );
    assert.equal(response.status, status, `${method} ${path}`);
    assert.match(response.headers.get("Content-Type"), /^application\/json/);
  }
});

test("uses one server-owned Tesco query and returns a normalized HM leaflet", async () => {
  const calls = [];
  const fetchMock = async (input, init) => {
    calls.push({ input: String(input), init });
    return jsonUpstream(
      graphqlPayload(leaflet({ format: "SM" }), leaflet({ format: "HM" })),
    );
  };
  const bridge = subject(fetchMock);
  const response = await bridge.fetch(
    manifestRequest(),
    { BRIDGE_SECRET, TOKEN_SECRET },
    createContext().context,
  );

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].input, TESCO_GRAPHQL_ENDPOINT);
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.redirect, "manual");
  assert.equal(new Headers(calls[0].init.headers).get("Authorization"), null);
  assert.equal(
    new Headers(calls[0].init.headers).get("Content-Type"),
    "application/json",
  );
  const upstreamBody = JSON.parse(calls[0].init.body);
  assert.deepEqual(Object.keys(upstreamBody), ["query"]);
  assert.match(upstreamBody.query, /^query CurrentSlovakLeaflets/);
  assert.match(upstreamBody.query, /country:\s*\{\s*eq:\s*sk\s*\}/);
  assert.match(upstreamBody.query, /type:\s*\{\s*eq:\s*HM\s*\}/);
  assert.match(
    upstreamBody.query,
    /validTo:\s*\{\s*after:\s*"2026-09-11T00:00:00\.000Z"\s*\}/,
  );

  const payload = await readJson(response);
  assert.deepEqual(
    {
      country: payload.leaflet.country,
      format: payload.leaflet.format,
      slug: payload.leaflet.slug,
      valid_from: payload.leaflet.valid_from,
      valid_to: payload.leaflet.valid_to,
      declared_pages: payload.leaflet.declared_pages,
    },
    {
      country: "sk",
      format: "HM",
      slug: "tesco-letak-2026-09-07",
      valid_from: "2026-09-07",
      valid_to: "2026-09-13",
      declared_pages: 8,
    },
  );
  assert.equal(
    payload.leaflet.source_url,
    "https://www.tesco.sk/akciove-ponuky/letaky-a-katalogy/" +
      "hypermarkety/tesco-letak-2026-09-07/1",
  );
  assert.deepEqual(
    payload.leaflet.pages.map((page) => page.source_page),
    [1, 2, 3, 4, 5, 6, 7, 8],
  );
  assert.ok(
    payload.leaflet.pages.every(
      (page) =>
        page.image_url === page.thumbnail_url &&
        page.image_url.startsWith(`${WORKER_ORIGIN}/v1/tesco/media/`),
    ),
  );
  assert.equal(JSON.stringify(payload).includes("digitalcontent.api.tesco.com"), false);
  assert.equal(response.headers.get("Cache-Control"), "private, max-age=30");
  assert.equal(response.headers.get("Vary"), "Authorization");
});

test("binds the authenticated manifest to release and Cloudflare version", async () => {
  const bridge = subject(async () => jsonUpstream(graphqlPayload(leaflet())));
  const response = await bridge.fetch(
    manifestRequest(),
    { BRIDGE_SECRET, TOKEN_SECRET },
    createContext().context,
  );
  const payload = await readJson(response);
  const statement = {
    release: WORKER_RELEASE,
    version_id: WORKER_VERSION_ID,
    request_date: "2026-09-11",
    request_format: "HM",
    leaflet: payload.leaflet,
  };
  const expected = base64UrlEncode(
    pseudoMac(
      encoder.encode(BRIDGE_SECRET),
      encoder.encode(JSON.stringify(statement)),
    ),
  );

  assert.equal(response.status, 200);
  assert.deepEqual(payload.bridge, {
    release: WORKER_RELEASE,
    version_id: WORKER_VERSION_ID,
    attestation: expected,
  });
});

test("refuses manifests without deployed version identity", async () => {
  const bridge = subject(async () => jsonUpstream(graphqlPayload(leaflet())));
  for (const identity of [
    { WORKER_RELEASE: null },
    { CF_VERSION_METADATA: null },
  ]) {
    const response = await bridge.fetch(
      manifestRequest(),
      { BRIDGE_SECRET, TOKEN_SECRET, ...identity },
      createContext().context,
    );
    assert.equal(response.status, 500);
    assert.deepEqual(await readJson(response), { error: "internal_error" });
  }
});

test("selects only the requested SM format", async () => {
  const bridge = subject(async () =>
    jsonUpstream(
      graphqlPayload(leaflet({ format: "HM" }), leaflet({ format: "SM" })),
    ),
  );
  const response = await bridge.fetch(
    manifestRequest({ date: "2026-09-11", format: "SM" }),
    { BRIDGE_SECRET, TOKEN_SECRET },
    createContext().context,
  );
  const payload = await readJson(response);

  assert.equal(response.status, 200);
  assert.equal(payload.leaflet.format, "SM");
  assert.match(payload.leaflet.source_url, /\/supermarkety\//);
});

test("accepts only the exact JSON schema and a real date near the current day", async () => {
  const bridge = subject(async () => assert.fail("upstream fetch must not run"));
  const invalidBodies = [
    "{",
    "null",
    "[]",
    "{}",
    JSON.stringify({ date: "2026-09-11" }),
    JSON.stringify({ date: "2026-09-11", format: "HM", query: "query Evil" }),
    JSON.stringify({ date: "2026-09-11", format: "HM", upstream_url: "https://evil.test" }),
    JSON.stringify({ date: "2026-09-11", format: "hm" }),
    JSON.stringify({ date: "2026-02-30", format: "HM" }),
    JSON.stringify({ date: "2026-08-27", format: "HM" }),
    JSON.stringify({ date: "2026-09-26", format: "HM" }),
    JSON.stringify({ date: '2026-09-11" } query Evil {', format: "HM" }),
  ];

  for (const body of invalidBodies) {
    const response = await bridge.fetch(
      manifestRequest(body),
      { BRIDGE_SECRET, TOKEN_SECRET },
      createContext().context,
    );
    assert.equal(response.status, 400, body);
    assert.deepEqual(await readJson(response), { error: "invalid_request" });
  }

  const wrongType = new Request(`${WORKER_ORIGIN}/v1/tesco/leaflets`, {
    method: "POST",
    headers: authorizedHeaders({ "Content-Type": "text/plain" }),
    body: JSON.stringify({ date: "2026-09-11", format: "HM" }),
  });
  const response = await bridge.fetch(
    wrongType,
    { BRIDGE_SECRET, TOKEN_SECRET },
    createContext().context,
  );
  assert.equal(response.status, 415);
  assert.deepEqual(await readJson(response), { error: "invalid_request" });
});

test("rejects redirects, oversized manifests and invalid upstream JSON generically", async () => {
  const secretInput = "do-not-reflect-this";
  const cases = [
    new Response(null, {
      status: 302,
      headers: { Location: `https://evil.test/${secretInput}` },
    }),
    jsonUpstream(graphqlPayload(leaflet()), {
      headers: { "Content-Length": String(MAX_MANIFEST_BYTES + 1) },
    }),
    new Response("not-json", {
      headers: { "Content-Type": "application/json" },
    }),
    jsonUpstream({ errors: [{ message: secretInput }] }),
  ];

  for (const upstream of cases) {
    const bridge = subject(async () => upstream.clone());
    const response = await bridge.fetch(
      manifestRequest(),
      { BRIDGE_SECRET, TOKEN_SECRET },
      createContext().context,
    );
    const body = await response.text();
    assert.equal(response.status, 502);
    assert.equal(body.includes(secretInput), false);
    assert.equal(body.includes(BRIDGE_SECRET), false);
    assert.deepEqual(JSON.parse(body), { error: "upstream_failed" });
  }
});

test("rejects a manifest body that exceeds the limit while streaming", async () => {
  const body = `{"padding":"${"x".repeat(MAX_MANIFEST_BYTES)}"}`;
  const bridge = subject(async () =>
    new Response(body, { headers: { "Content-Type": "application/json" } }),
  );
  const response = await bridge.fetch(
    manifestRequest(),
    { BRIDGE_SECRET, TOKEN_SECRET },
    createContext().context,
  );

  assert.equal(response.status, 502);
  assert.deepEqual(await readJson(response), { error: "upstream_failed" });
});

test("isolates malformed candidates and rejects every untrusted media URL shape", async () => {
  const unsafeUrls = [
    "http://digitalcontent.api.tesco.com/v2/media/dotcom-hu/page/leaflet.1.jpeg",
    "https://evil.test/v2/media/dotcom-hu/page/leaflet.1.jpeg",
    "https://user:pass@digitalcontent.api.tesco.com/v2/media/dotcom-hu/page/leaflet.1.jpeg",
    "https://digitalcontent.api.tesco.com:444/v2/media/dotcom-hu/page/leaflet.1.jpeg",
    "https://digitalcontent.api.tesco.com/v2/media/other/page/leaflet.1.jpeg",
    "https://digitalcontent.api.tesco.com/v2/media/dotcom-hu/%2e%2e/leaflet.1.jpeg",
    "https://digitalcontent.api.tesco.com/v2/media/dotcom-hu/page/leaflet.1.jpeg?variant=evil",
    "https://digitalcontent.api.tesco.com/v2/media/dotcom-hu/page/leaflet.1.png",
  ];

  for (const unsafeUrl of unsafeUrls) {
    const malformed = leaflet({
      pages: [{ __typename: "LeafletMetadataPage", pagePNG: unsafeUrl }],
    });
    const bridge = subject(async () =>
      jsonUpstream(graphqlPayload(malformed, leaflet())),
    );
    const response = await bridge.fetch(
      manifestRequest(),
      { BRIDGE_SECRET, TOKEN_SECRET },
      createContext().context,
    );
    assert.equal(response.status, 200, unsafeUrl);
    const payload = await readJson(response);
    assert.equal(payload.leaflet.declared_pages, 8);
    assert.equal(JSON.stringify(payload).includes(unsafeUrl), false);
  }

  const bridge = subject(async () =>
    jsonUpstream(
      graphqlPayload(
        leaflet({
          pages: [{
            __typename: "LeafletMetadataPage",
            pagePNG: unsafeUrls[1],
          }],
        }),
      ),
    ),
  );
  const response = await bridge.fetch(
    manifestRequest(),
    { BRIDGE_SECRET, TOKEN_SECRET },
    createContext().context,
  );
  assert.equal(response.status, 502);
  assert.deepEqual(await readJson(response), { error: "upstream_failed" });
});

test("requires 8 through 120 contiguous media pages", async () => {
  const invalidLeaflets = [
    leaflet({ pageCount: 7 }),
    leaflet({ pageCount: 121 }),
    leaflet({
      pages: [
        ...leaflet().pages.slice(0, 7),
        {
          __typename: "LeafletMetadataPage",
          pagePNG: mediaUrl(9),
        },
      ],
    }),
  ];

  for (const invalidLeaflet of invalidLeaflets) {
    const bridge = subject(async () =>
      jsonUpstream(graphqlPayload(invalidLeaflet)),
    );
    const response = await bridge.fetch(
      manifestRequest(),
      { BRIDGE_SECRET, TOKEN_SECRET },
      createContext().context,
    );
    assert.equal(response.status, 502);
    assert.deepEqual(await readJson(response), { error: "upstream_failed" });
  }
});

test("requires complete ISO timestamps for the official validity window", async () => {
  const invalidLeaflets = [
    leaflet({ validFrom: "2026-09-07Tgarbage" }),
    leaflet({ validTo: "2026-09-13T21:59:59" }),
  ];

  for (const invalidLeaflet of invalidLeaflets) {
    const bridge = subject(async () =>
      jsonUpstream(graphqlPayload(invalidLeaflet)),
    );
    const response = await bridge.fetch(
      manifestRequest(),
      { BRIDGE_SECRET, TOKEN_SECRET },
      createContext().context,
    );
    assert.equal(response.status, 502);
    assert.deepEqual(await readJson(response), { error: "upstream_failed" });
  }
});

test("serves a signed official JPEG without forwarding caller credentials", async () => {
  const calls = [];
  const imageBytes = Uint8Array.from([0xff, 0xd8, 0xff, 0xdb, 0x01, 0x02]);
  const fetchMock = async (input, init) => {
    calls.push({ input: String(input), init });
    if (String(input) === TESCO_GRAPHQL_ENDPOINT) {
      return jsonUpstream(graphqlPayload(leaflet()));
    }
    return new Response(imageBytes, {
      headers: { "Content-Type": "image/jpeg; charset=binary" },
    });
  };
  const { bridge, url } = await issueMediaToken({ fetchImpl: fetchMock });
  const response = await bridge.fetch(
    new Request(url, { headers: authorizedHeaders() }),
    { BRIDGE_SECRET, TOKEN_SECRET },
    createContext().context,
  );

  assert.equal(response.status, 200);
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()), imageBytes);
  assert.equal(calls.length, 2);
  assert.equal(calls[1].input, mediaUrl(1));
  assert.equal(calls[1].init.method, "GET");
  assert.equal(calls[1].init.redirect, "manual");
  const headers = new Headers(calls[1].init.headers);
  assert.equal(headers.get("Authorization"), null);
  assert.equal(headers.get("Accept"), "image/jpeg");
  assert.equal(response.headers.get("Content-Type"), "image/jpeg");
  assert.match(response.headers.get("Cache-Control"), /^private, max-age=\d+, immutable$/);
  assert.equal(response.headers.get("X-Content-Type-Options"), "nosniff");
});

test("rejects tampered and expired media tokens before fetching media", async () => {
  let mediaFetches = 0;
  const fetchMock = async (input) => {
    if (String(input) === TESCO_GRAPHQL_ENDPOINT) {
      return jsonUpstream(graphqlPayload(leaflet()));
    }
    mediaFetches += 1;
    return new Response(Uint8Array.from([0xff, 0xd8]), {
      headers: { "Content-Type": "image/jpeg" },
    });
  };
  const { bridge, token } = await issueMediaToken({ fetchImpl: fetchMock });
  const tampered = `${token.slice(0, -1)}${token.endsWith("A") ? "B" : "A"}`;
  const expired = resignToken(token, { e: Math.floor(NOW / 1000) });

  for (const candidate of ["not-a-token", tampered, expired]) {
    const response = await bridge.fetch(
      new Request(`${WORKER_ORIGIN}/v1/tesco/media/${candidate}`, {
        headers: authorizedHeaders(),
      }),
      { BRIDGE_SECRET, TOKEN_SECRET },
      createContext().context,
    );
    assert.equal(response.status, 400);
    assert.deepEqual(await readJson(response), { error: "invalid_token" });
  }
  assert.equal(mediaFetches, 0);
});

test("keeps a media token valid through 23:59:59 but not at 24:00:00", async () => {
  let currentTime = NOW;
  let mediaFetches = 0;
  const fetchMock = async (input) => {
    if (String(input) === TESCO_GRAPHQL_ENDPOINT) {
      return jsonUpstream(graphqlPayload(leaflet()));
    }
    mediaFetches += 1;
    return new Response(Uint8Array.from([0xff, 0xd8, 0xff]), {
      headers: { "Content-Type": "image/jpeg" },
    });
  };
  const { bridge, url } = await issueMediaToken({
    fetchImpl: fetchMock,
    now: () => currentTime,
  });

  currentTime = NOW + 86_399_000;
  const beforeLimit = await bridge.fetch(
    new Request(url, { headers: authorizedHeaders() }),
    { BRIDGE_SECRET, TOKEN_SECRET },
    createContext().context,
  );
  assert.equal(beforeLimit.status, 200);

  currentTime = NOW + 86_400_000;
  const atLimit = await bridge.fetch(
    new Request(url, { headers: authorizedHeaders() }),
    { BRIDGE_SECRET, TOKEN_SECRET },
    createContext().context,
  );
  assert.equal(atLimit.status, 400);
  assert.deepEqual(await readJson(atLimit), { error: "invalid_token" });
  assert.equal(mediaFetches, 1);
});

test("caps client media caching one second before the 24-hour token boundary", async () => {
  const fetchMock = async (input) =>
    String(input) === TESCO_GRAPHQL_ENDPOINT
      ? jsonUpstream(graphqlPayload(leaflet()))
      : new Response(Uint8Array.from([0xff, 0xd8, 0xff]), {
          headers: { "Content-Type": "image/jpeg" },
        });
  const { bridge, url } = await issueMediaToken({ fetchImpl: fetchMock });
  const response = await bridge.fetch(
    new Request(url, { headers: authorizedHeaders() }),
    { BRIDGE_SECRET, TOKEN_SECRET },
    createContext().context,
  );

  assert.equal(response.status, 200);
  assert.equal(
    response.headers.get("Cache-Control"),
    "private, max-age=86399, immutable",
  );
});

test("revalidates the decoded media URL after a valid HMAC", async () => {
  let mediaFetches = 0;
  const fetchMock = async (input) => {
    if (String(input) === TESCO_GRAPHQL_ENDPOINT) {
      return jsonUpstream(graphqlPayload(leaflet()));
    }
    mediaFetches += 1;
    return new Response(Uint8Array.from([0xff, 0xd8]), {
      headers: { "Content-Type": "image/jpeg" },
    });
  };
  const { bridge, token } = await issueMediaToken({ fetchImpl: fetchMock });
  const unsafeUrls = [
    "http://digitalcontent.api.tesco.com/v2/media/dotcom-hu/page/leaflet.1.jpeg",
    "https://evil.test/v2/media/dotcom-hu/page/leaflet.1.jpeg",
    "https://user:pass@digitalcontent.api.tesco.com/v2/media/dotcom-hu/page/leaflet.1.jpeg",
    "https://digitalcontent.api.tesco.com:444/v2/media/dotcom-hu/page/leaflet.1.jpeg",
    "https://digitalcontent.api.tesco.com/v2/media/other/leaflet.1.jpeg",
    "https://digitalcontent.api.tesco.com/v2/media/dotcom-hu/%2e%2e/leaflet.1.jpeg",
    "https://digitalcontent.api.tesco.com/v2/media/dotcom-hu/page/leaflet.1.jpeg?x=1",
    "https://digitalcontent.api.tesco.com/v2/media/dotcom-hu/page/leaflet.1.png",
  ];

  for (const url of unsafeUrls) {
    const response = await bridge.fetch(
      new Request(
        `${WORKER_ORIGIN}/v1/tesco/media/${resignToken(token, { u: url })}`,
        { headers: authorizedHeaders() },
      ),
      { BRIDGE_SECRET, TOKEN_SECRET },
      createContext().context,
    );
    assert.equal(response.status, 400, url);
    assert.deepEqual(await readJson(response), { error: "invalid_token" });
  }
  assert.equal(mediaFetches, 0);
});

test("rejects redirected, oversized and non-JPEG media responses", async () => {
  const cases = [
    new Response(null, {
      status: 302,
      headers: { Location: "https://evil.test/stolen.jpeg" },
    }),
    new Response(Uint8Array.from([0xff, 0xd8]), {
      headers: {
        "Content-Type": "image/jpeg",
        "Content-Length": String(MAX_MEDIA_BYTES + 1),
      },
    }),
    new Response("<html>not an image</html>", {
      headers: { "Content-Type": "text/html" },
    }),
  ];

  for (const mediaResponse of cases) {
    const fetchMock = async (input) =>
      String(input) === TESCO_GRAPHQL_ENDPOINT
        ? jsonUpstream(graphqlPayload(leaflet()))
        : mediaResponse.clone();
    const { bridge, url } = await issueMediaToken({ fetchImpl: fetchMock });
    const response = await bridge.fetch(
      new Request(url, { headers: authorizedHeaders() }),
      { BRIDGE_SECRET, TOKEN_SECRET },
      createContext().context,
    );
    assert.equal(response.status, 502);
    assert.deepEqual(await readJson(response), { error: "upstream_failed" });
  }
});

test("rejects a media body that exceeds the limit while streaming", async () => {
  const oversizedImage = new Uint8Array(MAX_MEDIA_BYTES + 1);
  const fetchMock = async (input) =>
    String(input) === TESCO_GRAPHQL_ENDPOINT
      ? jsonUpstream(graphqlPayload(leaflet()))
      : new Response(oversizedImage, {
          headers: { "Content-Type": "image/jpeg" },
        });
  const { bridge, url } = await issueMediaToken({ fetchImpl: fetchMock });
  const response = await bridge.fetch(
    new Request(url, { headers: authorizedHeaders() }),
    { BRIDGE_SECRET, TOKEN_SECRET },
    createContext().context,
  );

  assert.equal(response.status, 502);
  assert.deepEqual(await readJson(response), { error: "upstream_failed" });
});

test("caches manifests briefly and immutable media behind authentication", async () => {
  const cache = createFakeCache();
  const calls = [];
  const fetchMock = async (input) => {
    calls.push(String(input));
    if (String(input) === TESCO_GRAPHQL_ENDPOINT) {
      return jsonUpstream(graphqlPayload(leaflet()));
    }
    return new Response(Uint8Array.from([0xff, 0xd8, 0xff]), {
      headers: { "Content-Type": "image/jpeg" },
    });
  };
  const bridge = subject(fetchMock, { cache });

  let context = createContext();
  let manifestResponse = await bridge.fetch(
    manifestRequest(),
    { BRIDGE_SECRET, TOKEN_SECRET },
    context.context,
  );
  await context.settle();
  const manifest = await readJson(manifestResponse);

  context = createContext();
  manifestResponse = await bridge.fetch(
    manifestRequest(),
    { BRIDGE_SECRET, TOKEN_SECRET },
    context.context,
  );
  await context.settle();
  assert.equal(manifestResponse.status, 200);
  assert.equal(
    calls.filter((url) => url === TESCO_GRAPHQL_ENDPOINT).length,
    1,
  );

  const mediaRequest = new Request(manifest.leaflet.pages[0].image_url, {
    headers: authorizedHeaders(),
  });
  context = createContext();
  let mediaResponse = await bridge.fetch(
    mediaRequest.clone(),
    { BRIDGE_SECRET, TOKEN_SECRET },
    context.context,
  );
  await context.settle();
  assert.equal(mediaResponse.status, 200);

  context = createContext();
  mediaResponse = await bridge.fetch(
    mediaRequest.clone(),
    { BRIDGE_SECRET, TOKEN_SECRET },
    context.context,
  );
  await context.settle();
  assert.equal(mediaResponse.status, 200);
  assert.equal(calls.filter((url) => url === mediaUrl(1)).length, 1);

  const unauthenticated = await bridge.fetch(
    new Request(manifest.leaflet.pages[0].image_url),
    { BRIDGE_SECRET, TOKEN_SECRET },
    createContext().context,
  );
  assert.equal(unauthenticated.status, 401);
});

test("declares required secrets in wrangler.jsonc and uses it in package scripts", async () => {
  const projectRoot = new URL("../", import.meta.url);
  const config = JSON.parse(
    await readFile(new URL("wrangler.jsonc", projectRoot), "utf8"),
  );
  const packageConfig = JSON.parse(
    await readFile(new URL("package.json", projectRoot), "utf8"),
  );

  assert.equal(config.main, "src/worker.js");
  assert.deepEqual(config.secrets, {
    required: ["BRIDGE_SECRET", "TOKEN_SECRET", "WORKER_RELEASE"],
  });
  assert.deepEqual(config.version_metadata, { binding: "CF_VERSION_METADATA" });
  assert.match(packageConfig.scripts.dev, /--config wrangler\.jsonc$/);
  assert.match(packageConfig.scripts.deploy, /--config wrangler\.jsonc$/);
  await assert.rejects(access(new URL("wrangler.toml", projectRoot)));
});
